from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


__version__ = "0.3.5"


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
    def __init__(self) -> None:
        self._seen: dict[str, dict[str, int]] = {}
        self._seen_call_ids: set[str] = set()

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
        session_id: str = "",
        tool_call_id: str = "",
        **_: Any,
    ) -> str | None:
        if os.environ.get("TOOL_SLIM_ENABLED", "true").lower() in {"0", "false", "no", "off"}:
            self._audit_decision(tool_name or "unknown", "unchanged", "disabled", 0, status)
            return None

        text = self._to_text(result)
        if tool_call_id:
            call_key = f"{session_id}:{tool_name or 'unknown'}:{tool_call_id}"
            if call_key in self._seen_call_ids:
                self._audit_decision(tool_name or "unknown", "unchanged", "duplicate tool_call_id", len(text), status)
                return None
            self._seen_call_ids.add(call_key)

        background_start = self._background_start_summary(tool_name or "unknown", args, text)
        if background_start is not None:
            self._audit_decision(tool_name or "unknown", "normalized", "background process start", len(text), status)
            return background_start

        duplicate = self._dedup(tool_name or "unknown", session_id, text)
        if duplicate is not None:
            self._audit_decision(tool_name or "unknown", "dedup", "duplicate large tool result", len(text), status)
            return duplicate

        max_chars = _env_int("TOOL_SLIM_MAX_CHARS", 4000)
        if len(text) <= max_chars:
            self._audit_decision(tool_name or "unknown", "unchanged", "below max chars", len(text), status)
            return None
        min_saving = _env_int("TOOL_SLIM_MIN_SAVING_CHARS", 500)
        if len(text) - max_chars < min_saving:
            self._audit_decision(tool_name or "unknown", "unchanged", "below min saving", len(text), status)
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

    def _dedup(self, tool_name: str, session_id: str, text: str) -> str | None:
        """Replace an exact repeat of a tool result (same session, same tool,
        same content) with a small back-reference stub. Proactive guard against
        context bloat from repeated identical tool output."""
        if not _env_bool("TOOL_SLIM_DEDUP", True):
            return None
        if len(text) < _env_int("TOOL_SLIM_DEDUP_MIN_CHARS", 4000):
            return None
        if not session_id:
            return None
        h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()[:12]
        bucket = self._seen.setdefault(session_id, {})
        key = f"{tool_name}:{h}"
        count = bucket.get(key, 0)
        if count:
            bucket[key] = count + 1
            self._log_compaction(tool_name, len(text), 0, "dedup", status="")
            if os.environ.get("TOOL_SLIM_DEDUP_MODE", "stub").lower() == "minimal":
                return (
                    "[tool-slim duplicate omitted]\n"
                    f"tool: {tool_name}\n"
                    "mode: dedup\n"
                    f"raw_chars: {len(text)}\n"
                    f"saved_chars_estimate: {len(text)}\n"
                    "reduction_pct_estimate: 100.0\n"
                )
            return (
                "[tool-slim compacted tool result]\n"
                f"tool: {tool_name}\n"
                "mode: dedup\n"
                "decision_reason: duplicate tool result\n"
                f"raw_chars: {len(text)}\n"
                f"saved_chars_estimate: {len(text)}\n"
                "reduction_pct_estimate: 100.0\n"
                f"notice: tool-slim replaced an exact duplicate of a previous {tool_name} result (seen {count + 1} times); see above\n"
                f"note: this exact output has been returned {count + 1} times this session; see the first occurrence above.\n"
            )
        bucket[key] = 1
        if len(bucket) > _env_int("TOOL_SLIM_DEDUP_WINDOW", 50):
            bucket.pop(next(iter(bucket)))
        return None

    def _background_start_summary(self, tool_name: str, args: Any, text: str) -> str | None:
        if tool_name != "terminal":
            return None
        parsed = self._try_json(text)
        if not isinstance(parsed, dict):
            return None
        output = parsed.get("output")
        if output not in {"Background process started", "Background process already running"}:
            return None
        event = "background_process_already_running" if parsed.get("reused_existing") else "background_process_started"

        lines = [
            "[tool-slim normalized background process start]",
            f"event: {event}",
            f"tool: {tool_name}",
        ]
        command = self._arg_value(args, "command")
        for key in ("session_id", "pid", "exit_code", "notify_on_complete", "reused_existing"):
            if key in parsed:
                lines.append(f"{key}: {parsed[key]}")
        if command:
            lines.append(f"command: {self._one_line(command, 1000)}")
        if parsed.get("error"):
            lines.append(f"error: {self._one_line(parsed['error'], 500)}")
        return "\n".join(lines) + "\n"

    def _arg_value(self, args: Any, key: str) -> str:
        if isinstance(args, dict):
            value = args.get(key)
            return value if isinstance(value, str) else ""
        return ""

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
            if tool_name == "session_search":
                deterministic_body = self._compact_session_search(parsed)
                important: list[str] = []
            else:
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

        compacted = self._assemble_compacted(text, body, max_chars, header_lines, tool_name=tool_name, mode=mode, reason=decision.reason)
        self._log_compaction(tool_name, len(text), len(compacted), mode, status)
        return compacted

    def _assemble_compacted(
        self,
        raw_text: str,
        body: str,
        max_chars: int,
        header_lines: list[str],
        *,
        tool_name: str = "",
        mode: str = "",
        reason: str = "",
    ) -> str:
        """Assemble header + body, truncate to budget, then report final KPIs
        measured against the persisted output (including header and truncation)."""
        raw_len = len(raw_text)

        def build(saved: int) -> str:
            reduction = round(saved * 100 / max(1, raw_len), 1)
            banner = f"tool-slim: compacted {tool_name or 'unknown'} · {reduction}% reduction · saved {saved} chars · {mode or 'unknown'}"
            if reason:
                banner += f" ({reason})"
            lines = [header_lines[0], banner, *header_lines[1:]]
            lines += [
                f"omitted_chars_estimate: {saved}",
                f"saved_chars_estimate: {saved}",
                f"reduction_pct_estimate: {reduction}",
            ]
            compacted = "\n".join(lines) + "\n\n" + body
            if len(compacted) <= max_chars:
                return compacted
            return compacted[: max_chars - 80] + "\n\n[tool-slim: compacted output truncated to budget]"

        first = build(max(0, raw_len - len(body)))
        final_saved = max(0, raw_len - len(first))
        return build(final_saved)

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
            self._decision_session_search,
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

    def _decision_session_search(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if tool_name == "session_search" and isinstance(parsed, dict):
            return CompactionDecision("deterministic", "structured tool session_search")
        return None

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
        if tool_name in {"read_file", "glob", "grep", "session_search"}:
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

    def _audit_decision(self, tool_name: str, action: str, reason: str, raw_chars: int, status: str) -> None:
        if not _env_bool("TOOL_SLIM_AUDIT"):
            return
        message = (
            f"decision tool={tool_name} action={action} reason={reason} "
            f"raw_chars={raw_chars} status={status or 'unknown'}"
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

    def _compact_session_search(self, value: Any) -> str:
        if not isinstance(value, dict):
            return self._compact_json(value)

        lines = [
            f"session_id: {value.get('session_id', '')}",
            f"message_count: {value.get('message_count', '')}",
            f"truncated: {value.get('truncated', '')}",
        ]
        meta = value.get("session_meta")
        if isinstance(meta, dict):
            for key in ("when", "source", "model", "title"):
                if meta.get(key):
                    lines.append(f"session_meta.{key}: {self._one_line(self._to_text(meta[key]), 500)}")

        messages = value.get("messages")
        if not isinstance(messages, list):
            lines.append("messages: none")
            return "\n".join(lines)

        tail = _env_int("TOOL_SLIM_SESSION_TAIL", 8)
        user_msgs = []
        assistant_msgs = []
        error_msgs = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                user_msgs.append(content.strip())
            elif role == "assistant":
                assistant_msgs.append(content.strip())
            if self._session_search_has_error(msg):
                error_msgs.append(f"[{role}/{msg.get('tool_name', '')}] {self._one_line(content, 700)}")

        if user_msgs:
            lines.append("first_user_message:")
            lines.append(self._one_line(user_msgs[0], 1000))
        actions = self._session_search_key_actions(messages)
        if actions:
            lines.append("key_actions:")
            for action in actions:
                lines.append("- " + action)
        if error_msgs:
            lines.append("error_messages:")
            for msg in error_msgs[:10]:
                lines.append("- " + msg)
        if assistant_msgs:
            lines.append("last_assistant_messages:")
            for msg in assistant_msgs[-tail:]:
                lines.append("- " + self._one_line(msg, 800))

        shown = min(1, len(user_msgs)) + min(6, len(actions)) + min(10, len(error_msgs)) + min(tail, len(assistant_msgs))
        lines.append(f"messages_shown: {shown} of {len(messages)}")
        return "\n".join(lines)

    def _session_search_key_actions(self, messages: list[Any], limit: int = 6) -> list[str]:
        actions: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            tool_calls = msg.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else call
                name = fn.get("name", "")
                try:
                    args = fn.get("arguments") or "{}"
                    parsed_args = json.loads(args) if isinstance(args, str) else args
                    if not isinstance(parsed_args, dict):
                        parsed_args = {}
                except (TypeError, ValueError):
                    parsed_args = {}
                action = self._session_search_action_line(name, parsed_args)
                if action and action not in actions:
                    actions.append(action)
                    if len(actions) >= limit:
                        return actions
        return actions

    def _session_search_action_line(self, name: str, args: dict[str, Any]) -> str:
        if not name:
            return ""
        if name in {"write_file", "patch"} and args.get("path"):
            return f"{name}: {self._one_line(str(args['path']), 300)}"
        if name in {"terminal", "execute_code", "command"} and args.get("command"):
            return f"{name}: {self._one_line(str(args['command']), 400)}"
        if name in {"read_file", "glob", "grep", "search_files"} and args.get("path"):
            return f"{name}: {self._one_line(str(args['path']), 300)}"
        if args.get("command"):
            return f"{name}: {self._one_line(str(args['command']), 400)}"
        return f"{name}"

    def _session_search_has_error(self, msg: dict[str, Any]) -> bool:
        content = msg.get("content", "")
        if not isinstance(content, str):
            return False
        if content.lstrip().startswith("[tool-slim compacted tool result]"):
            return False
        low = content.lower()
        if "traceback" in low or "exception" in low:
            return True
        if "error: null" in low or "error:null" in low:
            low = low.replace("error: null", "").replace("error:null", "")
        for marker in ("error:", "failed", "failure", "denied", "unauthorized", "forbidden", "timeout"):
            if marker in low:
                return True
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    return True
                meaning = str(parsed.get("exit_code_meaning") or "").lower()
                if any(token in meaning for token in ("not an error", "no matches", "not found")):
                    return False
                exit_code = parsed.get("exit_code", parsed.get("returncode"))
                if exit_code not in (None, "", 0, "0"):
                    return True
        except (TypeError, ValueError):
            pass
        return False

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
            if self._line_is_critical(lower):
                lines.append(line[:1000])
                if len(lines) >= limit:
                    break
        return lines

    def _line_is_critical(self, lower: str) -> bool:
        if not any(marker in lower for marker in IMPORTANT_MARKERS):
            return False
        if "exit_code" in lower or "returncode" in lower:
            try:
                exit_code = int(re.search(r"(?:exit_code|returncode)[\"']?\s*[:=]\s*[\"']?(-?\d+)", lower).group(1))
                if exit_code == 0:
                    return False
            except (AttributeError, ValueError):
                pass
        return True

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

    bg_start = '{"output": "Background process started", "session_id": "proc_abc", "pid": 123, "exit_code": 0, "error": null, "notify_on_complete": true}'
    bg_summary = plugin.transform_tool_result(
        tool_name="terminal",
        args={"command": "python3 worker.py", "background": True},
        result=bg_start,
    )
    assert bg_summary is not None
    assert "background_process_started" in bg_summary
    assert "session_id: proc_abc" in bg_summary
    assert "pid: 123" in bg_summary
    assert "command: python3 worker.py" in bg_summary
    assert "DO NOT" not in bg_summary
    bg_reused = '{"output": "Background process already running", "session_id": "proc_abc", "pid": 123, "exit_code": 0, "error": null, "notify_on_complete": true, "reused_existing": true}'
    bg_reused_summary = plugin.transform_tool_result(tool_name="terminal", args={"command": "python3 worker.py"}, result=bg_reused)
    assert bg_reused_summary is not None
    assert "background_process_already_running" in bg_reused_summary
    assert "reused_existing: True" in bg_reused_summary

    barely_over = "x" * (_env_int("TOOL_SLIM_MAX_CHARS", 4000) + 100)
    assert plugin.transform_tool_result(tool_name="terminal", result=barely_over) is None

    comfortably_over = "y" * (_env_int("TOOL_SLIM_MAX_CHARS", 4000) + _env_int("TOOL_SLIM_MIN_SAVING_CHARS", 500) + 100)
    assert plugin.transform_tool_result(tool_name="terminal", result=comfortably_over) is not None

    dup_body = "line\n" * 2000
    first = plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessA")
    assert first is not None
    second = plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessA")
    assert second is not None
    assert "mode: dedup" in second
    assert "duplicate tool result" in second
    assert "reduction_pct_estimate: 100.0" in second
    third = plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessA")
    assert third is not None and "mode: dedup" in third
    other_session = plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessB")
    assert other_session is not None and "mode: dedup" not in other_session
    other_tool = plugin.transform_tool_result(tool_name="terminal", result=dup_body, session_id="sessA")
    assert other_tool is not None and "mode: dedup" not in other_tool

    call_plugin = ToolSlimPlugin()
    first_call = call_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessCall", tool_call_id="call1")
    second_call = call_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessCall", tool_call_id="call1")
    third_call = call_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessCall", tool_call_id="call2")
    assert first_call is not None and "mode: dedup" not in first_call
    assert second_call is None
    assert third_call is not None and "mode: dedup" in third_call

    os.environ["TOOL_SLIM_DEDUP_MODE"] = "minimal"
    min_plugin = ToolSlimPlugin()
    first_min = min_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessMin")
    assert first_min is not None
    second_min = min_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessMin")
    assert second_min is not None
    assert "duplicate omitted" in second_min
    assert "mode: dedup" in second_min
    assert "reduction_pct_estimate: 100.0" in second_min
    assert "note:" not in second_min
    assert "see above" not in second_min
    assert "times this session" not in second_min
    os.environ.pop("TOOL_SLIM_DEDUP_MODE", None)

    bg_body = '{"output": "Background process started", "session_id": "proc_x", "pid": 1234, "exit_code": 0, "note": "' + "x" * 250 + '"}'
    first_bg = plugin.transform_tool_result(tool_name="process", result=bg_body, session_id="sessC")
    assert first_bg is None
    second_bg = plugin.transform_tool_result(tool_name="process", result=bg_body, session_id="sessC")
    assert second_bg is None

    rf_body = '{"content": "file listing ' + "x" * 300 + '", "total_lines": 1}'
    first_rf = plugin.transform_tool_result(tool_name="read_file", result=rf_body, session_id="sessD")
    assert first_rf is None
    second_rf = plugin.transform_tool_result(tool_name="read_file", result=rf_body, session_id="sessD")
    assert second_rf is None


    small_dup = "ok" * 30
    first_small = plugin.transform_tool_result(tool_name="terminal", result=small_dup, session_id="sessA")
    second_small = plugin.transform_tool_result(tool_name="terminal", result=small_dup, session_id="sessA")
    assert first_small is None
    assert second_small is None

    large = "line\n" * 2000 + "ERROR: useful failure\n" + "tail\n" * 2000
    compact = plugin.transform_tool_result(
        tool_name="terminal",
        result=large,
        duration_ms=12,
        status="success",
    )
    assert compact is not None
    assert "[tool-slim compacted tool result]" in compact
    assert "tool-slim: compacted terminal" in compact
    assert "reduction" in compact
    assert "status: success" in compact
    assert "duration_ms: 12" in compact
    assert "saved_chars_estimate:" in compact
    assert "reduction_pct_estimate:" in compact
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

    session_search_messages = [
        {"id": 1, "role": "user", "content": "Check metadata for tracks in album"},
        {"id": 2, "role": "assistant", "content": "Decision: apply ID3 tags to all 113 tracks", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"python3 audit_tags.py\"}"}}]},
        {"id": 3, "role": "tool", "tool_name": "terminal", "content": '{"output": "ok", "exit_code": 0, "error": null}'},
    ]
    for i in range(4, 220):
        session_search_messages.append({"id": i, "role": "tool", "tool_name": "terminal", "content": f'{{"output": "progress row {i} /Volumes/music/track_{i}.mp3", "exit_code": 0, "error": null}}'})
    session_search_data = {
        "success": True,
        "mode": "read",
        "session_id": "20260823_143121_22e2e7",
        "session_meta": {"when": "August 23, 2026", "source": "desktop", "model": "reasoning", "title": "Tag music album"},
        "message_count": len(session_search_messages),
        "truncated": True,
        "messages": session_search_messages,
    }
    os.environ.pop("TOOL_SLIM_LLM_ENABLED", None)
    os.environ.pop("TOOL_SLIM_LLM_BASE_URL", None)
    os.environ.pop("TOOL_SLIM_LLM_MODEL", None)
    compact_search = plugin.transform_tool_result(tool_name="session_search", result=session_search_data)
    assert compact_search is not None
    assert "mode: deterministic" in compact_search
    assert "decision_reason: structured tool session_search" in compact_search
    assert "first_user_message" in compact_search
    assert "key_actions:" in compact_search
    assert "python3 audit_tags.py" in compact_search
    assert "last_assistant_messages" in compact_search
    assert "Decision: apply ID3 tags to all 113 tracks" in compact_search
    assert "20260823_143121_22e2e7" in compact_search
    assert "messages_shown" in compact_search

    session_search_failure_messages = [
        {"id": 1, "role": "assistant", "content": '{"output": "ok", "exit_code": 0, "error": null}'},
        {"id": 2, "role": "tool", "tool_name": "terminal", "content": "Traceback (most recent call last):\\nRuntimeError: boom"},
    ]
    for i in range(3, 220):
        session_search_failure_messages.append({"id": i, "role": "tool", "tool_name": "terminal", "content": f"noise line {i}"})
    session_search_failure = {
        "success": True,
        "session_meta": {"title": "Old"},
        "message_count": len(session_search_failure_messages),
        "messages": session_search_failure_messages,
    }
    compact_search_fail = plugin.transform_tool_result(tool_name="session_search", result=session_search_failure)
    assert compact_search_fail is not None
    assert "error_messages" in compact_search_fail
    assert "Traceback" in compact_search_fail
    assert "RuntimeError: boom" in compact_search_fail

    already_compacted = {"id": 99, "role": "tool", "tool_name": "session_search", "content": "[tool-slim compacted tool result]\ntool: session_search\nmode: deterministic\n... error: null ..."}
    assert not plugin._session_search_has_error(already_compacted)
    error_null = {"id": 98, "role": "tool", "tool_name": "terminal", "content": '{"output": "ok", "exit_code": 0, "error": null}'}
    assert not plugin._session_search_has_error(error_null)

    no_matches = {"id": 97, "role": "tool", "tool_name": "terminal", "content": '{"output": "", "exit_code": 1, "error": null, "exit_code_meaning": "No matches found (not an error)"}'}
    assert not plugin._session_search_has_error(no_matches)

    kpi_large = {"output": "row\n" * 3000, "exit_code": 0, "error": None}
    compact_kpi = plugin.transform_tool_result(tool_name="terminal", result=kpi_large)
    assert compact_kpi is not None
    assert len(compact_kpi) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)
    import re as _re
    saved_match = _re.search(r"saved_chars_estimate: (\d+)", compact_kpi)
    reduction_match = _re.search(r"reduction_pct_estimate: ([\d.]+)", compact_kpi)
    assert saved_match and int(saved_match.group(1)) > 0
    assert reduction_match and float(reduction_match.group(1)) > 0
    assert "omitted_chars_estimate: " in compact_kpi

    for name in list(os.environ):
        if name.startswith("TOOL_SLIM_LLM_"):
            os.environ.pop(name, None)
    for name, value in saved_env.items():
        if value is not None:
            os.environ[name] = value


if __name__ == "__main__":
    _demo()
    print("tool-slim self-check ok")
