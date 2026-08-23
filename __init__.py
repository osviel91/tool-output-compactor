from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


__version__ = "0.2.5"


logger = logging.getLogger("tool-slim")


IMPORTANT_MARKERS = (
    "error",
    "exception",
    "traceback",
    "failed",
    "failure",
    "warning",
    "warn",
    "denied",
    "unauthorized",
    "forbidden",
    "timeout",
    "exit_code",
    "stderr",
)

CRITICAL_KEYS = {
    "error",
    "stderr",
    "traceback",
    "exception",
    "exit_code",
    "returncode",
    "status",
    "approval",
    "command",
}


@dataclass(frozen=True)
class CompactionDecision:
    mode: str
    reason: str


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


class ToolSlimPlugin:
    @property
    def name(self) -> str:
        return "tool-slim"

    def transform_tool_result(
        self,
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        duration_ms: int | None = None,
        status: str = "",
        error_type: str = "",
        error_message: str = "",
        **_: Any,
    ) -> str | None:
        if os.environ.get("TOOL_SLIM_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
            return None

        text = self._to_text(result)
        max_chars = _env_int("TOOL_SLIM_MAX_CHARS", 4000)
        if len(text) <= max_chars:
            return None
        if _env_bool("TOOL_SLIM_DEBUG"):
            print(
                f"[tool-slim] compacting tool={tool_name or 'unknown'} raw_chars={len(text)} target_chars={max_chars}",
                file=sys.stderr,
            )
        return self._compact(
            tool_name or "unknown",
            text,
            max_chars,
            args=args,
            duration_ms=duration_ms,
            status=status,
            error_type=error_type,
            error_message=error_message,
        )

    def _to_text(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False, indent=2)
        except TypeError:
            return str(result)

    def _compact(
        self,
        tool_name: str,
        text: str,
        max_chars: int,
        *,
        args: Any = None,
        duration_ms: int | None = None,
        status: str = "",
        error_type: str = "",
        error_message: str = "",
    ) -> str:
        parsed = self._try_json(text)
        if parsed is not None:
            deterministic_body = self._compact_json(parsed)
            important = self._important_from_value(parsed, _env_int("TOOL_SLIM_IMPORTANT_LINES", 40))
        else:
            deterministic_body = self._compact_text(text)
            important = self._important_lines(text, _env_int("TOOL_SLIM_IMPORTANT_LINES", 40))

        preserved = self._preserved_sections(tool_name, args, parsed, important)
        if preserved:
            deterministic_body = preserved + "\n\n---\n\n" + deterministic_body

        decision = self._choose_compaction_mode(tool_name, args, parsed, text, status, error_type, error_message)
        llm_body = None
        if decision.mode == "llm":
            llm_body = self._compact_with_llm(tool_name, text, deterministic_body, max_chars, preserved)
        mode = "hybrid" if llm_body else "deterministic"
        body = llm_body or deterministic_body

        header_lines = [
            "[tool-slim compacted tool result]",
            f"tool: {tool_name}",
            f"mode: {mode}",
            f"decision_reason: {decision.reason}",
            f"raw_chars: {len(text)}",
            f"target_chars: {max_chars}",
            f"omitted_chars_estimate: {max(0, len(text) - len(body))}",
        ]
        if status:
            header_lines.append(f"status: {status}")
        if duration_ms is not None:
            header_lines.append(f"duration_ms: {duration_ms}")
        if error_type:
            header_lines.append(f"error_type: {error_type}")
        if error_message:
            header_lines.append(f"error_message: {self._one_line(error_message, 500)}")
        if _env_bool("TOOL_SLIM_NOTICE_IN_RESULT"):
            header_lines.append(f"notice: tool-slim compacted this result using {mode} mode")
        header = "\n".join(header_lines) + "\n\n"
        compacted = header + body
        if len(compacted) <= max_chars:
            self._log_compaction(tool_name, len(text), len(compacted), mode, status)
            return compacted
        compacted = compacted[: max_chars - 80] + "\n\n[tool-slim: compacted output truncated to budget]"
        self._log_compaction(tool_name, len(text), len(compacted), mode, status)
        return compacted

    def _choose_compaction_mode(
        self,
        tool_name: str,
        args: Any,
        parsed: Any,
        text: str,
        status: str,
        error_type: str,
        error_message: str,
    ) -> CompactionDecision:
        checks = (
            self._decision_failures,
            self._decision_structured_tool_result,
            self._decision_structured_text,
            self._decision_too_small_for_llm,
            self._decision_llm_available,
        )
        for check in checks:
            decision = check(tool_name, args, parsed, text, status, error_type, error_message)
            if decision is not None:
                return decision
        return CompactionDecision("deterministic", "fallback deterministic")

    def _decision_failures(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if error_type or error_message:
            return CompactionDecision("deterministic", "tool error metadata")
        if status and status.lower() not in {"ok", "success", "completed"}:
            return CompactionDecision("deterministic", "non-success tool status")
        if isinstance(parsed, dict):
            exit_code = parsed.get("exit_code", parsed.get("returncode"))
            if exit_code not in (None, "", 0, "0"):
                return CompactionDecision("deterministic", "non-zero exit code")
            if parsed.get("stderr") or parsed.get("error"):
                return CompactionDecision("deterministic", "stderr or error field")
        important = self._important_from_value(parsed, 1) if parsed is not None else self._important_lines(text, 1)
        if important:
            return CompactionDecision("deterministic", "critical lines present")
        return None

    def _decision_structured_tool_result(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
            return CompactionDecision("deterministic", "structured content field")
        if isinstance(parsed, dict) and isinstance(parsed.get("output"), str) and self._looks_structured(parsed["output"]):
            return CompactionDecision("deterministic", "structured output field")
        if tool_name in {"read_file", "glob", "grep"}:
            return CompactionDecision("deterministic", f"structured tool {tool_name}")
        return None

    def _decision_structured_text(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if self._looks_structured(text):
            return CompactionDecision("deterministic", "structured text")
        return None

    def _decision_too_small_for_llm(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if len(text) < _env_int("TOOL_SLIM_LLM_MIN_CHARS", 12000):
            return CompactionDecision("deterministic", "below llm minimum")
        return None

    def _decision_llm_available(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if _env_bool("TOOL_SLIM_LLM_ENABLED"):
            return CompactionDecision("llm", "large unstructured result")
        return None

    def _try_json(self, text: str) -> Any | None:
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def _one_line(self, text: str, limit: int) -> str:
        text = text.replace("\n", " ")
        return text if len(text) <= limit else text[:limit] + "..."

    def _log_compaction(self, tool_name: str, raw_chars: int, output_chars: int, mode: str, status: str) -> None:
        message = (
            f"compacted tool={tool_name} raw_chars={raw_chars} "
            f"output_chars={output_chars} mode={mode} status={status or 'unknown'}"
        )
        logger.info(message)
        if _env_bool("TOOL_SLIM_DEBUG"):
            print(f"[tool-slim] {message}", file=sys.stderr)

    def _compact_with_llm(
        self,
        tool_name: str,
        raw_text: str,
        deterministic_body: str,
        max_chars: int,
        preserved: str,
    ) -> str | None:
        if not _env_bool("TOOL_SLIM_LLM_ENABLED"):
            return None
        base_url = os.environ.get("TOOL_SLIM_LLM_BASE_URL", "").rstrip("/")
        model = os.environ.get("TOOL_SLIM_LLM_MODEL", "")
        if not base_url or not model:
            return None

        budget = _env_int("TOOL_SLIM_LLM_MAX_CHARS", max(800, max_chars // 2))
        prompt = self._llm_prompt(tool_name, deterministic_body, budget)
        try:
            summary = self._call_llm(base_url, model, prompt)
        except Exception as exc:
            if _env_bool("TOOL_SLIM_DEBUG"):
                print(f"[tool-slim] llm_compaction_failed error={type(exc).__name__}", file=sys.stderr)
            return None

        summary = summary.strip()
        if not summary:
            return None
        if len(summary) > budget:
            summary = summary[:budget] + "..."

        parts = []
        if preserved:
            parts.append(preserved)
        parts.append("LLM summary:\n" + summary)
        return "\n\n---\n\n".join(parts)

    def _llm_prompt(self, tool_name: str, text: str, budget: int) -> str:
        return (
            "Compress this Hermes tool result for a small-context coding agent.\n"
            "Rules:\n"
            "- Preserve errors, warnings, tracebacks, exit codes, commands, file paths, ids and URLs.\n"
            "- Preserve facts needed to continue the task.\n"
            "- Remove repetition, progress noise, long listings and boilerplate.\n"
            "- Do not invent. If uncertain, say what is visible in the tool result only.\n"
            "- Return plain text only.\n"
            f"- Keep under {budget} characters.\n\n"
            f"Tool: {tool_name}\n"
            "Tool result:\n"
            f"{text}"
        )

    def _call_llm(self, base_url: str, model: str, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You compress tool output for coding agents."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": _env_int("TOOL_SLIM_LLM_MAX_TOKENS", 700),
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("TOOL_SLIM_LLM_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        timeout = _env_int("TOOL_SLIM_LLM_TIMEOUT_SECONDS", 30)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def _compact_json(self, value: Any, depth: int = 0) -> str:
        max_items = _env_int("TOOL_SLIM_JSON_MAX_ITEMS", 20)
        if depth >= 3:
            return self._json_leaf(value)

        if isinstance(value, dict):
            if isinstance(value.get("content"), str):
                return self._compact_structured_text_result(value, "content")
            if isinstance(value.get("output"), str) and len(value["output"]) > 1000:
                return self._compact_structured_text_result(value, "output")
            lines = [f"JSON object with {len(value)} keys: {', '.join(map(str, list(value)[:max_items]))}"]
            for key, item in list(value.items())[:max_items]:
                lines.append(f"- {key}: {self._compact_json(item, depth + 1)}")
            if len(value) > max_items:
                lines.append(f"- ... {len(value) - max_items} more keys omitted")
            return "\n".join(lines)

        if isinstance(value, list):
            lines = [f"JSON array with {len(value)} items"]
            for index, item in enumerate(value[:max_items]):
                lines.append(f"- [{index}]: {self._compact_json(item, depth + 1)}")
            if len(value) > max_items:
                lines.append(f"- ... {len(value) - max_items} more items omitted")
            return "\n".join(lines)

        return self._json_leaf(value)

    def _json_leaf(self, value: Any) -> str:
        text = self._to_text(value).replace("\n", " ")
        return text if len(text) <= 240 else text[:240] + "..."

    def _compact_structured_text_result(self, value: dict[str, Any], key: str) -> str:
        text = value[key]
        lines = [f"JSON object with {len(value)} keys: {', '.join(map(str, value.keys()))}"]
        for meta_key in ("total_lines", "file_size", "truncated", "hint", "is_binary", "is_image", "exit_code", "error", "status"):
            if meta_key in value:
                lines.append(f"- {meta_key}: {self._json_leaf(value[meta_key])}")
        lines.append(f"- {key}:")
        lines.append(self._compact_lines(text))
        return "\n".join(lines)

    def _compact_lines(self, text: str) -> str:
        all_lines = text.splitlines()
        if not all_lines:
            return ""
        head = _env_int("TOOL_SLIM_HEAD_LINES", 30)
        tail = _env_int("TOOL_SLIM_TAIL_LINES", 10)
        important = self._important_lines(text, _env_int("TOOL_SLIM_IMPORTANT_LINES", 40))

        parts = []
        if important:
            parts.append("Important lines:\n" + "\n".join(important))
        if len(all_lines) <= head + tail:
            parts.append("Lines:\n" + "\n".join(all_lines))
        else:
            omitted = len(all_lines) - head - tail
            parts.append(f"Head lines 1-{head}:\n" + "\n".join(all_lines[:head]))
            parts.append(f"... {omitted} lines omitted ...")
            parts.append(f"Tail lines {len(all_lines) - tail + 1}-{len(all_lines)}:\n" + "\n".join(all_lines[-tail:]))
        return "\n\n".join(parts)

    def _compact_text(self, text: str) -> str:
        head_chars = _env_int("TOOL_SLIM_HEAD_CHARS", 1200)
        tail_chars = _env_int("TOOL_SLIM_TAIL_CHARS", 1200)
        important_limit = _env_int("TOOL_SLIM_IMPORTANT_LINES", 40)

        important = self._important_lines(text, important_limit)
        parts = []
        if important:
            parts.append("Important lines:\n" + "\n".join(important))
        parts.append("Head:\n" + text[:head_chars])
        parts.append("Tail:\n" + text[-tail_chars:])
        return "\n\n---\n\n".join(parts)

    def _looks_structured(self, text: str) -> bool:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 8:
            return False
        short = sum(1 for line in lines if len(line) <= 180)
        paths = sum(1 for line in lines if "/" in line or "\\" in line)
        numbered = sum(1 for line in lines if line[:1].isdigit() or line.startswith(("- ", "* ", "|")))
        file_like = sum(1 for line in lines if any(line.lower().endswith(ext) for ext in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".txt", ".md", ".mp3", ".mp4", ".log")))
        return short / len(lines) >= 0.75 and (paths + numbered + file_like) >= 3

    def _important_lines(self, text: str, limit: int) -> list[str]:
        lines = []
        for line in text.splitlines():
            lower = line.lower()
            if any(marker in lower for marker in IMPORTANT_MARKERS):
                lines.append(line[:1000])
                if len(lines) >= limit:
                    break
        return lines

    def _important_from_value(self, value: Any, limit: int) -> list[str]:
        lines: list[str] = []

        def add(line: str) -> None:
            if line and line not in lines and len(lines) < limit:
                lines.append(line[:1000])

        def visit(item: Any, path: str, depth: int) -> None:
            if len(lines) >= limit or depth > 8:
                return
            if isinstance(item, dict):
                for key, child in item.items():
                    child_path = f"{path}.{key}" if path else str(key)
                    key_lower = str(key).lower()
                    if key_lower in CRITICAL_KEYS and child not in (None, "", [], {}, 0, "0", "ok", "success"):
                        add(f"{child_path}: {self._one_line(self._to_text(child), 1000)}")
                    visit(child, child_path, depth + 1)
                return
            if isinstance(item, list):
                for index, child in enumerate(item):
                    visit(child, f"{path}[{index}]", depth + 1)
                    if len(lines) >= limit:
                        break
                return
            if isinstance(item, str):
                for line in item.splitlines() or [item]:
                    lower = line.lower()
                    if any(marker in lower for marker in IMPORTANT_MARKERS):
                        add(f"{path}: {line}" if path else line)
                        if len(lines) >= limit:
                            break

        visit(value, "", 0)
        return lines

    def _preserved_sections(self, tool_name: str, args: Any, parsed: Any, important: list[str]) -> str:
        facts = self._action_facts(tool_name, args, parsed)
        sections = []
        if facts:
            sections.append("Preserved action facts:\n" + "\n".join(facts))
        if important:
            sections.append("Preserved critical lines:\n" + "\n".join(important))
        return "\n\n".join(sections)

    def _action_facts(self, tool_name: str, args: Any, parsed: Any) -> list[str]:
        facts: list[str] = []

        def add(label: str, value: Any) -> None:
            if value in (None, "", [], {}):
                return
            line = f"{label}: {self._one_line(self._to_text(value), 1000)}"
            if line not in facts:
                facts.append(line)

        if isinstance(args, dict):
            for key in ("command", "cmd", "path", "file_path", "query"):
                if key in args:
                    add(f"args.{key}", args[key])
        elif args not in (None, ""):
            add("args", args)

        if isinstance(parsed, dict):
            for key in ("command", "cmd", "exit_code", "returncode", "stderr", "error", "status", "approval"):
                if key in parsed:
                    add(key, parsed[key])

        if tool_name:
            facts.insert(0, f"tool: {tool_name}")
        return facts


def register(ctx: Any) -> None:
    plugin = ToolSlimPlugin()
    ctx.register_hook("transform_tool_result", plugin.transform_tool_result)


def _demo() -> None:
    root = Path(__file__).resolve().parent
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == __version__
    assert f"version: {__version__}" in (root / "plugin.yaml").read_text(encoding="utf-8")

    saved_env = {name: os.environ.get(name) for name in os.environ if name.startswith("TOOL_SLIM_LLM_")}
    for name in saved_env:
        os.environ.pop(name, None)

    plugin = ToolSlimPlugin()
    small = "ok"
    assert plugin.transform_tool_result(tool_name="terminal", result=small) is None

    large = "line\n" * 2000 + "ERROR: useful failure\n" + "tail\n" * 2000
    compact = plugin.transform_tool_result(
        tool_name="terminal",
        result=large,
        duration_ms=12,
        status="success",
    )
    assert compact is not None
    assert "[tool-slim compacted tool result]" in compact
    assert "status: success" in compact
    assert "duration_ms: 12" in compact
    assert "ERROR: useful failure" in compact
    assert len(compact) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)

    os.environ["TOOL_SLIM_NOTICE_IN_RESULT"] = "true"
    compact_notice = plugin.transform_tool_result(tool_name="terminal", result=large)
    assert compact_notice is not None
    assert "notice: tool-slim compacted this result" in compact_notice
    os.environ.pop("TOOL_SLIM_NOTICE_IN_RESULT", None)

    data = {"items": list(range(2000)), "status": "ok"}
    compact_json = plugin.transform_tool_result(tool_name="api", result=data)
    assert compact_json is not None
    assert "JSON object" in compact_json

    noisy_json = {"output": "normal line\n" * 250 + "ERROR: compact-test-marker\n" + "normal line\n" * 250, "exit_code": 0, "error": None}
    compact_json_error = plugin.transform_tool_result(tool_name="terminal", result=noisy_json)
    assert compact_json_error is not None
    assert "ERROR: compact-test-marker" in compact_json_error

    action_json = {"output": "noise\n" * 800, "exit_code": 7, "stderr": "fatal: action failed", "error": None}
    compact_action = plugin.transform_tool_result(tool_name="terminal", args={"command": "python script.py"}, result=action_json)
    assert compact_action is not None
    assert "Preserved action facts" in compact_action
    assert "mode: deterministic" in compact_action
    assert "decision_reason: non-zero exit code" in compact_action
    assert "args.command: python script.py" in compact_action
    assert "exit_code: 7" in compact_action
    assert "stderr: fatal: action failed" in compact_action

    listing = "\n".join(f"{i}|{i:02d} - Artist - Track {i} - long sortable library row.mp3" for i in range(1, 121))
    read_file_result = {
        "content": listing,
        "total_lines": 120,
        "file_size": len(listing),
        "truncated": False,
        "is_binary": False,
        "is_image": False,
    }
    os.environ["TOOL_SLIM_LLM_ENABLED"] = "true"
    os.environ["TOOL_SLIM_LLM_BASE_URL"] = "http://unused.test/v1"
    os.environ["TOOL_SLIM_LLM_MODEL"] = "mock"
    plugin._call_llm = lambda *_: "bad summary"  # type: ignore[method-assign]
    compact_read = plugin.transform_tool_result(tool_name="read_file", args={"path": "/tmp/listing.txt"}, result=read_file_result)
    assert compact_read is not None
    assert "mode: deterministic" in compact_read
    assert "decision_reason: structured content field" in compact_read
    assert "Head lines 1-30" in compact_read
    assert "Tail lines 111-120" in compact_read
    assert "01 - Artist - Track 1 - long sortable library row.mp3" in compact_read
    assert "120 - Artist - Track 120 - long sortable library row.mp3" in compact_read
    assert "bad summary" not in compact_read

    os.environ["TOOL_SLIM_LLM_ENABLED"] = "true"
    os.environ["TOOL_SLIM_LLM_BASE_URL"] = "http://unused.test/v1"
    os.environ["TOOL_SLIM_LLM_MODEL"] = "mock"
    plugin._call_llm = lambda *_: "kept useful summary"  # type: ignore[method-assign]
    unstructured = "This is a long narrative build log section without structured rows.\n" * 400
    compact_llm = plugin.transform_tool_result(tool_name="terminal", result=unstructured)
    assert compact_llm is not None
    assert "mode: hybrid" in compact_llm
    assert "decision_reason: large unstructured result" in compact_llm
    assert "LLM summary" in compact_llm
    assert "kept useful summary" in compact_llm

    for name in list(os.environ):
        if name.startswith("TOOL_SLIM_LLM_"):
            os.environ.pop(name, None)
    for name, value in saved_env.items():
        if value is not None:
            os.environ[name] = value


if __name__ == "__main__":
    _demo()
    print("tool-slim self-check ok")
