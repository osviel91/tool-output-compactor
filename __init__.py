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

    def transform_tool_result(self, *args: Any, **kwargs: Any) -> Any:
        if os.environ.get("TOOL_SLIM_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
            return args[1] if len(args) > 1 else kwargs.get("result")

        tool_name, result = self._parse_call(args, kwargs)
        text = self._to_text(result)
        max_chars = _env_int("TOOL_SLIM_MAX_CHARS", 4000)
        if len(text) <= max_chars:
            return result
        return self._compact(tool_name, text, max_chars)

    def _parse_call(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, Any]:
        tool_name = str(kwargs.get("tool_name") or kwargs.get("name") or "")
        result = kwargs.get("result")

        if len(args) >= 2 and isinstance(args[0], str):
            tool_name = args[0]
            result = args[1]
        elif args:
            result = args[0]

        return tool_name or "unknown", result

    def _to_text(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False, indent=2)
        except TypeError:
            return str(result)

    def _compact(self, tool_name: str, text: str, max_chars: int) -> str:
        parsed = self._try_json(text)
        if parsed is not None:
            body = self._compact_json(parsed)
        else:
            body = self._compact_text(text)

        header = (
            "[tool-slim compacted tool result]\n"
            f"tool: {tool_name}\n"
            f"raw_chars: {len(text)}\n"
            f"target_chars: {max_chars}\n"
            f"omitted_chars_estimate: {max(0, len(text) - len(body))}\n\n"
        )
        compacted = header + body
        if len(compacted) <= max_chars:
            return compacted
        return compacted[: max_chars - 80] + "\n\n[tool-slim: compacted output truncated to budget]"

    def _try_json(self, text: str) -> Any | None:
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

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
    if hasattr(ctx, "register_plugin"):
        ctx.register_plugin(plugin)
    elif hasattr(ctx, "register_tool_result_transformer"):
        ctx.register_tool_result_transformer(plugin)
    else:
        # Exact Hermes registration API for transform_tool_result still needs confirmation.
        setattr(ctx, "tool_slim_plugin", plugin)


def _demo() -> None:
    plugin = ToolSlimPlugin()
    small = "ok"
    assert plugin.transform_tool_result("terminal", small) == small

    large = "line\n" * 2000 + "ERROR: useful failure\n" + "tail\n" * 2000
    compact = plugin.transform_tool_result("terminal", large)
    assert "[tool-slim compacted tool result]" in compact
    assert "ERROR: useful failure" in compact
    assert len(compact) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)

    data = {"items": list(range(100)), "status": "ok"}
    compact_json = plugin.transform_tool_result("api", data)
    assert "JSON object" in compact_json or compact_json == data


if __name__ == "__main__":
    _demo()
    print("tool-slim self-check ok")
