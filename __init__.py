from __future__ import annotations

import json
import os
from typing import Any


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
            body = self._compact_json(parsed)
        else:
            body = self._compact_text(text)

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
        header = "\n".join(header_lines) + "\n\n"
        compacted = header + body
        if len(compacted) <= max_chars:
            return compacted
        return compacted[: max_chars - 80] + "\n\n[tool-slim: compacted output truncated to budget]"

    def _try_json(self, text: str) -> Any | None:
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def _one_line(self, text: str, limit: int) -> str:
        text = text.replace("\n", " ")
        return text if len(text) <= limit else text[:limit] + "..."

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

    data = {"items": list(range(2000)), "status": "ok"}
    compact_json = plugin.transform_tool_result(tool_name="api", result=data)
    assert compact_json is not None
    assert "JSON object" in compact_json


if __name__ == "__main__":
    _demo()
    print("tool-slim self-check ok")
