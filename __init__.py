from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any


__version__ = "0.2.2"


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
        duration_ms: int | None = None,
        status: str = "",
        error_type: str = "",
        error_message: str = "",
    ) -> str:
        parsed = self._try_json(text)
        if parsed is not None:
            deterministic_body = self._compact_json(parsed)
        else:
            deterministic_body = self._compact_text(text)

        llm_body = self._compact_with_llm(tool_name, text, deterministic_body, max_chars)
        mode = "llm" if llm_body else "deterministic"
        body = llm_body or deterministic_body

        header_lines = [
            "[tool-slim compacted tool result]",
            f"tool: {tool_name}",
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
    ) -> str | None:
        if not _env_bool("TOOL_SLIM_LLM_ENABLED"):
            return None
        base_url = os.environ.get("TOOL_SLIM_LLM_BASE_URL", "").rstrip("/")
        model = os.environ.get("TOOL_SLIM_LLM_MODEL", "")
        if not base_url or not model:
            return None

        important = self._important_lines(raw_text, _env_int("TOOL_SLIM_IMPORTANT_LINES", 40))
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
        if important:
            parts.append("Preserved critical lines:\n" + "\n".join(important))
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

    def _important_lines(self, text: str, limit: int) -> list[str]:
        lines = []
        for line in text.splitlines():
            lower = line.lower()
            if any(marker in lower for marker in IMPORTANT_MARKERS):
                lines.append(line[:1000])
                if len(lines) >= limit:
                    break
        return lines


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

    os.environ["TOOL_SLIM_LLM_ENABLED"] = "true"
    os.environ["TOOL_SLIM_LLM_BASE_URL"] = "http://unused.test/v1"
    os.environ["TOOL_SLIM_LLM_MODEL"] = "mock"
    plugin._call_llm = lambda *_: "kept useful summary"  # type: ignore[method-assign]
    compact_llm = plugin.transform_tool_result(tool_name="terminal", result=large)
    assert compact_llm is not None
    assert "Preserved critical lines" in compact_llm
    assert "ERROR: useful failure" in compact_llm
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
