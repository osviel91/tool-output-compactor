from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parent
HEADER = "[tool-output-compactor compacted tool result]"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("tool_output_compactor_plugin", ROOT / "__init__.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load tool-output-compactor plugin")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ToolOutputCompactorPlugin()


def _case_small() -> dict[str, Any]:
    return {
        "name": "small unchanged",
        "tool": "terminal",
        "args": {"command": "pwd"},
        "result": "ok\n",
        "required": ["ok"],
        "should_compact": False,
    }


def _case_terminal_failure() -> dict[str, Any]:
    result = {
        "output": "setup noise\n" * 500 + "FAILED tests/test_login.py::test_login\nAssertionError: expected 200 got 500\n" + "tail noise\n" * 500,
        "exit_code": 1,
        "stderr": "ERROR: login regression",
    }
    return {
        "name": "terminal failure",
        "tool": "terminal",
        "args": {"command": "pytest tests/test_login.py"},
        "result": result,
        "required": ["pytest tests/test_login.py", "exit_code: 1", "ERROR: login regression", "FAILED tests/test_login.py::test_login"],
        "should_compact": True,
    }


def _case_read_listing() -> dict[str, Any]:
    listing = "\n".join(f"{i:03d}|/repo/src/module_{i}.py" for i in range(1, 181))
    return {
        "name": "read_file listing",
        "tool": "read_file",
        "args": {"path": "/repo/files.txt"},
        "result": {"content": listing, "total_lines": 180, "file_size": len(listing), "truncated": False},
        "required": ["/repo/files.txt", "Head lines 1-30", "Tail lines 171-180", "/repo/src/module_1.py", "/repo/src/module_180.py"],
        "should_compact": True,
    }


def _case_session_search() -> dict[str, Any]:
    messages = []
    messages.append({"id": 1, "role": "user", "content": "Fix metadata in /Volumes/music/Album and preserve original files"})
    messages.append({"id": 2, "role": "assistant", "content": "Decision: convert six .mp4.part files, leave long audiobook alone"})
    for i in range(3, 90):
        messages.append({"id": i, "role": "tool", "tool_name": "terminal", "content": f'{{"output": "progress row {i}", "exit_code": 0, "error": null}}'})
    messages.append({"id": 90, "role": "tool", "tool_name": "terminal", "content": "ERROR: ffmpeg failed for /Volumes/music/Album/07.mp4.part"})
    messages.append({"id": 91, "role": "assistant", "content": "Final: 113 of 113 tagged, 7 .part files left untouched"})
    result = {
        "success": True,
        "mode": "read",
        "session_id": "20260823_160703_cd024c",
        "session_meta": {"title": "Resume music metadata repair", "model": "gpt-5.5"},
        "message_count": len(messages),
        "truncated": True,
        "messages": messages,
    }
    return {
        "name": "session_search history",
        "tool": "session_search",
        "args": {"session_id": "20260823_160703_cd024c"},
        "result": result,
        "required": [
            "20260823_160703_cd024c",
            "session_meta",
            "ERROR: ffmpeg failed",
            "/Volumes/music/Album/07.mp4.part",
            "Final: 113 of 113 tagged, 7 .part files left untouched",
            "first_user_message",
            "last_assistant_messages",
            "messages_shown",
        ],
        "should_compact": True,
    }


def _case_opencode_patch() -> dict[str, Any]:
    patch = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,6 @@
 def greet(name):
-    return "hi " + name
+    if not name:
+        raise ValueError("name required")
+    return f"hi {name}"
"""
    result = {
        "type": "message.part.updated",
        "properties": {
            "part": {"type": "patch", "files": ["src/app.py"]},
            "delta": "analysis noise\n" * 250 + patch + "tail noise\n" * 250,
        },
    }
    return {
        "name": "opencode patch output",
        "tool": "terminal",
        "args": {"command": "opencode run --format json add validation"},
        "result": result,
        "required": ["opencode run --format json", "diff --git", "@@", "src/app.py", "ValueError(\"name required\")"],
        "should_compact": True,
    }


def _case_binary_payload() -> dict[str, Any]:
    jpeg_bytes = "b'\\xff\\xd8" + "\\x00" * 3000 + "\\xff\\xd9'"
    result = {
        "output": f"APIC frame mime=image/jpeg desc=cover data={jpeg_bytes}\ncover_art_present=True\n",
        "exit_code": 0,
        "error": None,
    }
    return {
        "name": "binary payload omitted",
        "tool": "terminal",
        "args": {"command": "python3 debug_cover.py"},
        "result": result,
        "required": ["python3 debug_cover.py", "APIC", "image/jpeg", "binary payload omitted", "exit_code: 0"],
        "should_compact": True,
    }


def synthetic_cases() -> list[dict[str, Any]]:
    return [_case_small(), _case_terminal_failure(), _case_read_listing(), _case_session_search(), _case_opencode_patch(), _case_binary_payload()]


def _mode(output: str | None) -> str:
    if not output:
        return "unchanged"
    match = re.search(r"^mode: (.+)$", output, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _measure_case(plugin: Any, case: dict[str, Any], max_chars: int) -> dict[str, Any]:
    raw = plugin._to_text(case["result"])
    start = perf_counter()
    compact = plugin.transform_tool_result(tool_name=case["tool"], args=case["args"], result=case["result"], status="ok")
    elapsed_ms = (perf_counter() - start) * 1000
    output = compact if compact is not None else raw
    missing = [token for token in case["required"] if token not in output]
    return {
        "name": case["name"],
        "tool": case["tool"],
        "raw_chars": len(raw),
        "output_chars": len(output),
        "saved_chars": max(0, len(raw) - len(output)),
        "reduction_pct": round((1 - len(output) / max(1, len(raw))) * 100, 1),
        "compacted": compact is not None,
        "mode": _mode(compact),
        "elapsed_ms": round(elapsed_ms, 2),
        "required_missing": missing,
        "over_budget": compact is not None and len(output) > max_chars,
        "pass": (compact is not None) == case["should_compact"] and not missing and (compact is None or len(output) <= max_chars),
    }


def run_synthetic(max_chars: int) -> dict[str, Any]:
    saved_env = {name: os.environ.get(name) for name in ("TOOL_SLIM_MAX_CHARS", "TOOL_SLIM_LLM_ENABLED")}
    os.environ["TOOL_SLIM_MAX_CHARS"] = str(max_chars)
    os.environ["TOOL_SLIM_LLM_ENABLED"] = "false"
    try:
        plugin = _load_plugin()
        cases = [_measure_case(plugin, case, max_chars) for case in synthetic_cases()]
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    raw_total = sum(item["raw_chars"] for item in cases)
    output_total = sum(item["output_chars"] for item in cases)
    return {
        "kind": "synthetic",
        "max_chars": max_chars,
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "passed": sum(1 for item in cases if item["pass"]),
            "raw_chars": raw_total,
            "output_chars": output_total,
            "saved_chars": max(0, raw_total - output_total),
            "reduction_pct": round((1 - output_total / max(1, raw_total)) * 100, 1),
            "critical_marker_failures": sum(len(item["required_missing"]) for item in cases),
            "over_budget": sum(1 for item in cases if item["over_budget"]),
        },
    }


def analyze_session(session_id: str, db_path: Path, max_chars: int) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        messages_total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, tool_name, content FROM messages WHERE session_id=? AND role='tool' ORDER BY id",
            (session_id,),
        ).fetchall()
        bad_workdir_calls = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant' AND tool_calls LIKE '%workdir%' AND tool_calls LIKE '%&&%'",
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    tools: dict[str, dict[str, Any]] = {}
    large_uncompacted = []
    compacted = []
    estimated_saved = 0
    blocked_workdir = 0
    tool_loop_warnings = 0
    repeated_exact_warnings = 0
    for message_id, tool_name, content in rows:
        tool_name = tool_name or "unknown"
        length = len(content or "")
        item = tools.setdefault(tool_name, {"count": 0, "chars": 0, "compacted": 0, "large_uncompacted": 0})
        item["count"] += 1
        item["chars"] += length
        text = content or ""
        if "Blocked: workdir contains disallowed character" in text:
            blocked_workdir += 1
        if "Tool loop warning" in text:
            tool_loop_warnings += 1
        if "repeated_exact_failure_warning" in text:
            repeated_exact_warnings += 1
        if content and HEADER in content:
            item["compacted"] += 1
            compacted.append(message_id)
            match = re.search(r"saved_chars_estimate: (\d+)", content)
            if match:
                estimated_saved += int(match.group(1))
        elif length > max_chars:
            item["large_uncompacted"] += 1
            large_uncompacted.append({"id": message_id, "tool": tool_name, "chars": length})

    return {
        "kind": "session",
        "session_id": session_id,
        "messages_total": messages_total,
        "max_chars": max_chars,
        "tool_messages": len(rows),
        "tool_chars": sum(item["chars"] for item in tools.values()),
        "compacted_messages": len(compacted),
        "estimated_saved_chars": estimated_saved,
        "large_uncompacted": large_uncompacted,
        "diagnosis": {
            "plugin_acted": bool(compacted),
            "plugin_not_involved": not compacted and not large_uncompacted,
            "model_tool_schema_error": bool(bad_workdir_calls and blocked_workdir),
            "hermes_loop_guard_warned": tool_loop_warnings > 0,
            "hermes_loop_guard_ignored": repeated_exact_warnings >= 3,
            "blocked_workdir_results": blocked_workdir,
            "assistant_bad_workdir_calls": bad_workdir_calls,
            "tool_loop_warnings": tool_loop_warnings,
            "repeated_exact_warnings": repeated_exact_warnings,
        },
        "tools": tools,
    }


def print_report(report: dict[str, Any]) -> None:
    if report["kind"] == "synthetic":
        summary = report["summary"]
        print(f"synthetic: passed={summary['passed']}/{summary['cases']} saved={summary['saved_chars']} reduction={summary['reduction_pct']}% marker_failures={summary['critical_marker_failures']} over_budget={summary['over_budget']}")
        for item in report["cases"]:
            missing = ",".join(item["required_missing"]) or "-"
            print(f"- {item['name']}: pass={item['pass']} tool={item['tool']} mode={item['mode']} raw={item['raw_chars']} out={item['output_chars']} saved={item['saved_chars']} reduction={item['reduction_pct']}% missing={missing}")
        return

    print(f"session: {report['session_id']} messages={report['messages_total']} tool_messages={report['tool_messages']} tool_chars={report['tool_chars']} compacted={report['compacted_messages']} estimated_saved={report['estimated_saved_chars']}")
    diagnosis = report.get("diagnosis", {})
    if diagnosis:
        flags = ", ".join(name for name, value in diagnosis.items() if isinstance(value, bool) and value) or "none"
        print(f"diagnosis: {flags}")
        print(f"diagnosis_counts: blocked_workdir={diagnosis['blocked_workdir_results']} bad_workdir_calls={diagnosis['assistant_bad_workdir_calls']} loop_warnings={diagnosis['tool_loop_warnings']} repeated_exact={diagnosis['repeated_exact_warnings']}")
    if report["large_uncompacted"]:
        print("large_uncompacted:")
        for item in report["large_uncompacted"]:
            print(f"- id={item['id']} tool={item['tool']} chars={item['chars']}")
    for tool, item in sorted(report["tools"].items(), key=lambda pair: pair[1]["chars"], reverse=True):
        print(f"- {tool}: count={item['count']} chars={item['chars']} compacted={item['compacted']} large_uncompacted={item['large_uncompacted']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark and diagnose tool-output-compactor compaction.")
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--session-id", help="Analyze a real Hermes session instead of synthetic fixtures.")
    parser.add_argument("--state-db", type=Path, default=Path.home() / ".hermes/state.db")
    args = parser.parse_args()

    report = analyze_session(args.session_id, args.state_db, args.max_chars) if args.session_id else run_synthetic(args.max_chars)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    if report["kind"] == "synthetic":
        summary = report["summary"]
        return 0 if summary["passed"] == summary["cases"] else 1
    return 0 if report["messages_total"] and not report["large_uncompacted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
