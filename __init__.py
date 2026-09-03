from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


__version__ = "0.6.2"


PLUGIN_NAME = "tool-output-compactor"
LEGACY_MARKER = "[tool-slim compacted tool result]"
COMPACTED_MARKER = f"[{PLUGIN_NAME} compacted tool result]"


logger = logging.getLogger(PLUGIN_NAME)


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

CODING_TOOL_NAMES = {
    "opencode",
    "codex",
    "claude",
    "aider",
    "gemini",
    "cursor",
}

STRUCTURED_TOOL_NAMES = {"read_file", "glob", "grep", "session_search"}


@dataclass(frozen=True)
class CompactionDecision:
    mode: str
    reason: str


@dataclass
class ExtractedResult:
    """Intermediate, type-aware representation produced by an extractor.

    `body` is the base deterministic model-facing text for this result type.
    `important` lists critical lines to surface in the preserved sections.
    `decision` is set by typed extractors (always deterministic); generic
    extractors leave it None and the decision pipeline chooses afterwards."""

    result_type: str
    body: str = ""
    important: list[str] = field(default_factory=list)
    decision: CompactionDecision | None = None


class _Extractor:
    """Base class for type-aware extractors. Registered instances are tried in
    order; the first whose match() succeeds produces the base body."""

    kind = "generic"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        raise NotImplementedError

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        raise NotImplementedError


def _result_output_text(parsed: Any, text: str) -> str:
    """Textual payload of a result: unwraps dicts that carry a text field."""
    if isinstance(parsed, dict):
        for key in ("output", "content", "stdout", "text"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value
    return text


def _command_hint(args: Any) -> str:
    if isinstance(args, dict):
        for key in ("command", "cmd"):
            value = args.get(key)
            if isinstance(value, str):
                return value.lower()
    return ""


def _env_int_or_none(name: str) -> int | None:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return None


_PYTEST_COUNTS_RE = re.compile(
    r"(?m)^=+\s*(\d+ (?:passed|failed|error|skipped|xfailed|xpassed)"
    r"(?:, \d+ (?:passed|failed|error|skipped|xfailed|xpassed))* in [\d.]+s)\s*=+\s*$"
)
_PYTEST_VERBOSE_RE = re.compile(r"(?m)^.*\.py::[^\s:]+ (?:PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s*$")
_GIT_PORCELAIN_RE = re.compile(r"(?m)^([ MADRCU?]{1,2}) ([^\n]+)$")


def _is_pytest_output(out: str) -> bool:
    if not out or len(out) < 200:
        return False
    if _PYTEST_COUNTS_RE.search(out):
        return True
    has_header = "test session starts" in out
    has_short = "short test summary info" in out
    has_verbose = _PYTEST_VERBOSE_RE.search(out) is not None
    return has_header and (has_short or has_verbose)


def _pytest_failure_lines(out: str) -> list[str]:
    seen: list[str] = []
    for raw in out.splitlines():
        stripped = raw.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            if stripped not in seen:
                seen.append(stripped)
        elif raw.startswith("FAILED ") or raw.startswith("ERROR "):
            if stripped not in seen:
                seen.append(stripped)
    return seen


def _pytest_error_evidence(out: str, limit: int = 12) -> list[str]:
    lines: list[str] = []
    in_body = False
    for raw in out.splitlines():
        if re.match(r"^_{5,}", raw):
            in_body = True
            continue
        if re.match(r"^=+ ?(?:FAILURES|ERRORS|short test summary info)", raw):
            continue
        if in_body:
            stripped = raw.strip()
            if stripped.startswith("E "):
                if stripped not in lines:
                    lines.append(stripped)
                    if len(lines) >= limit:
                        break
    return lines


def _is_porcelain_row(row: str) -> bool:
    if len(row) < 4 or row[2] != " " or not row[3:].strip():
        return False
    if row[0] == " " and row[1] == " ":
        return False
    return True


def _git_porcelain_lines(out: str) -> list[str]:
    return [match.group(0) for match in _GIT_PORCELAIN_RE.finditer(out) if _is_porcelain_row(match.group(0))]


def _git_status_signal(out: str) -> bool:
    markers = ("On branch ", "Changes not staged for commit", "Changes to be committed", "Untracked files:", "nothing to commit")
    if any(marker in out for marker in markers):
        return True
    return len(_git_porcelain_lines(out)) >= 5


def _git_normal_path(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"^(modified|new file|deleted|renamed|typechange):\s*", "", cleaned)
    return cleaned.split(" -> ")[-1] if cleaned else ""


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


class ToolOutputCompactorPlugin:
    def __init__(self) -> None:
        self._seen: dict[str, dict[str, int]] = {}
        self._seen_call_ids: set[str] = set()
        self._background_starts: dict[str, dict[str, Any]] = {}
        self._extractors: list[_Extractor] = [
            PytestExtractor(),
            GitStatusExtractor(),
            GitLogExtractor(),
            SessionSearchExtractor(),
            JsonExtractor(),
            TextExtractor(),
        ]

    @property
    def name(self) -> str:
        return PLUGIN_NAME

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

        background_start = self._background_start_summary(tool_name or "unknown", args, text, session_id)
        if background_start is not None:
            self._audit_decision(tool_name or "unknown", "normalized", "background process start", len(text), status)
            return background_start

        duplicate = self._dedup(tool_name or "unknown", session_id, text, args=args, status=status)
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
                f"[{PLUGIN_NAME}] compacting tool={tool_name or 'unknown'} raw_chars={len(text)} target_chars={max_chars}",
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

    def _dedup(self, tool_name: str, session_id: str, text: str, args: Any = None, status: str = "") -> str | None:
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
            fact_lines = self._action_facts(tool_name, args, self._try_json(text))
            if status:
                fact_lines.append(f"status: {status}")
            facts = "Preserved action facts:\n" + "\n".join(fact_lines) + "\n" if fact_lines else f"tool: {tool_name}\n"
            if os.environ.get("TOOL_SLIM_DEDUP_MODE", "stub").lower() == "minimal":
                stub = (
                    f"[{PLUGIN_NAME} duplicate omitted]\n"
                    "mode: dedup\n"
                    f"{facts}"
                    f"raw_chars: {len(text)}\n"
                    f"saved_chars_estimate: {len(text)}\n"
                    "reduction_pct_estimate: 100.0\n"
                )
                self._log_compaction(tool_name, len(text), len(stub), "dedup", status=status,
                                     result_type="", deduped=True)
                return stub
            stub = (
                f"{COMPACTED_MARKER}\n"
                "mode: dedup\n"
                "decision_reason: duplicate tool result\n"
                f"{facts}"
                f"raw_chars: {len(text)}\n"
                f"saved_chars_estimate: {len(text)}\n"
                "reduction_pct_estimate: 100.0\n"
                f"notice: {PLUGIN_NAME} replaced an exact duplicate of a previous {tool_name} result (seen {count + 1} times); see above\n"
                f"note: this exact output has been returned {count + 1} times this session; see the first occurrence above.\n"
            )
            self._log_compaction(tool_name, len(text), len(stub), "dedup", status=status,
                                 result_type="", deduped=True)
            return stub
        bucket[key] = 1
        if len(bucket) > _env_int("TOOL_SLIM_DEDUP_WINDOW", 50):
            bucket.pop(next(iter(bucket)))
        return None

    def _background_start_summary(self, tool_name: str, args: Any, text: str, hermes_session_id: str = "") -> str | None:
        if tool_name != "terminal":
            return None
        parsed = self._try_json(text)
        if not isinstance(parsed, dict):
            return None
        output = parsed.get("output")
        if output not in {"Background process started", "Background process already running"}:
            return None
        event = "background_process_already_running" if parsed.get("reused_existing") else "background_process_started"
        command = self._arg_value(args, "command")
        cwd = self._arg_value(args, "cwd") or self._arg_value(args, "workdir")
        repeat = self._record_background_start(hermes_session_id, command, cwd, parsed)
        if repeat and not parsed.get("reused_existing"):
            event = "repeated_background_process_start"

        lines = [
            f"[{PLUGIN_NAME} normalized background process start]",
            f"event: {event}",
            f"tool: {tool_name}",
        ]
        if repeat and not parsed.get("reused_existing"):
            lines.append(f"repeat_count: {repeat['count']}")
            lines.append(f"previous_session_id: {repeat.get('session_id', '')}")
            lines.append(f"previous_pid: {repeat.get('pid', '')}")
            lines.append(f"current_session_id: {parsed.get('session_id', '')}")
            lines.append(f"current_pid: {parsed.get('pid', '')}")
        for key in ("session_id", "pid", "exit_code", "notify_on_complete", "reused_existing"):
            if key in parsed:
                lines.append(f"{key}: {parsed[key]}")
        if cwd:
            lines.append(f"cwd: {self._one_line(cwd, 1000)}")
        if command:
            lines.append(f"command: {self._one_line(command, 1000)}")
        if parsed.get("error"):
            lines.append(f"error: {self._one_line(parsed['error'], 500)}")
        return "\n".join(lines) + "\n"

    def _record_background_start(self, hermes_session_id: str, command: str, cwd: str, parsed: dict[str, Any]) -> dict[str, Any] | None:
        if not command:
            return None
        key = "\0".join((hermes_session_id or "", cwd or "", command))
        previous = self._background_starts.get(key)
        current = {"session_id": parsed.get("session_id", ""), "pid": parsed.get("pid", ""), "count": 1}
        if previous:
            current["count"] = int(previous.get("count", 1)) + 1
            self._background_starts[key] = current
            return {**previous, "count": current["count"]}
        self._background_starts[key] = current
        if len(self._background_starts) > _env_int("TOOL_SLIM_BACKGROUND_WINDOW", 50):
            self._background_starts.pop(next(iter(self._background_starts)))
        return None

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

    def _classify_extractor(self, tool_name: str, args: Any, parsed: Any, text: str) -> _Extractor:
        for extractor in self._extractors:
            if extractor.match(self, tool_name, args, parsed, text):
                return extractor
        return TextExtractor()

    def _extract_result(self, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        extractor = self._classify_extractor(tool_name, args, parsed, text)
        extracted = extractor.extract(self, tool_name, args, parsed, text)
        if extracted.decision is None and extractor.kind != "generic":
            extracted.decision = CompactionDecision("deterministic", f"{extractor.kind} extractor")
        return extracted

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
        # classify -> extract: pick the first matching extractor (typed first,
        # generic json/text/session-search as fallback) and get the base body.
        extracted = self._extract_result(tool_name, args, parsed, text)
        deterministic_body = extracted.body

        # decorate with preserved action facts + critical lines
        preserved = self._preserved_sections(tool_name, args, parsed, extracted.important)
        if preserved:
            deterministic_body = preserved + "\n\n---\n\n" + deterministic_body

        # keep code/diff sections for coding-assistant outputs
        code_sections = self._preserved_code_diff_sections(tool_name, args, parsed, text, max_chars)
        if code_sections:
            deterministic_body = code_sections + "\n\n---\n\n" + deterministic_body

        # budget/mode decision: typed extractors are deterministic by design;
        # generic results go through the decision pipeline (LLM last).
        decision = extracted.decision or self._choose_compaction_mode(tool_name, args, parsed, text, status, error_type, error_message)
        llm_body = None
        if decision.mode == "llm":
            llm_body = self._compact_with_llm(tool_name, text, deterministic_body, max_chars, preserved)
        mode = "hybrid" if llm_body else "deterministic"
        body = llm_body or deterministic_body

        header_lines = [
            COMPACTED_MARKER,
            f"tool: {tool_name}",
        ]
        if extracted.decision is not None:
            header_lines.append(f"result_type: {extracted.result_type}")
        header_lines += [
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
            header_lines.append(f"notice: {PLUGIN_NAME} compacted this result using {mode} mode")

        compacted = self._assemble_compacted(text, body, max_chars, header_lines, tool_name=tool_name, mode=mode, reason=decision.reason)
        self._log_compaction(tool_name, len(text), len(compacted), mode, status,
                             result_type=extracted.result_type, deduped=False)
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
            banner = f"{PLUGIN_NAME}: compacted {tool_name or 'unknown'} · {reduction}% reduction · saved {saved} chars · {mode or 'unknown'}"
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
            return compacted[: max_chars - 80] + f"\n\n[{PLUGIN_NAME}: compacted output truncated to budget]"

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
            self._decision_coding_assistant_output,
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

    def _decision_coding_assistant_output(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if self._is_coding_assistant_output(tool_name, args, parsed, text):
            return CompactionDecision("deterministic", "coding assistant output")
        return None

    def _decision_structured_tool_result(self, tool_name: str, args: Any, parsed: Any, text: str, status: str, error_type: str, error_message: str) -> CompactionDecision | None:
        if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
            return CompactionDecision("deterministic", "structured content field")
        if isinstance(parsed, dict) and isinstance(parsed.get("output"), str) and self._looks_structured(parsed["output"]):
            return CompactionDecision("deterministic", "structured output field")
        if tool_name in STRUCTURED_TOOL_NAMES:
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

    def _log_compaction(self, tool_name: str, raw_chars: int, output_chars: int, mode: str, status: str, result_type: str = "", deduped: bool = False) -> None:
        message = (
            f"compacted tool={tool_name} raw_chars={raw_chars} "
            f"output_chars={output_chars} mode={mode} status={status or 'unknown'}"
            f" result_type={result_type or 'none'} dedup={deduped}"
        )
        logger.info(message)
        if _env_bool("TOOL_SLIM_DEBUG"):
            print(f"[{PLUGIN_NAME}] {message}", file=sys.stderr)

    def _audit_decision(self, tool_name: str, action: str, reason: str, raw_chars: int, status: str) -> None:
        if not _env_bool("TOOL_SLIM_AUDIT"):
            return
        message = (
            f"decision tool={tool_name} action={action} reason={reason} "
            f"raw_chars={raw_chars} status={status or 'unknown'}"
        )
        logger.info(message)
        if _env_bool("TOOL_SLIM_DEBUG"):
            print(f"[{PLUGIN_NAME}] {message}", file=sys.stderr)

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
            summary = self._call_llm_bounded(base_url, model, prompt)
        except Exception as exc:
            if _env_bool("TOOL_SLIM_DEBUG"):
                print(f"[{PLUGIN_NAME}] llm_compaction_failed error={type(exc).__name__}", file=sys.stderr)
            return None

        if summary is None:
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

    def _call_llm_bounded(self, base_url: str, model: str, prompt: str) -> str | None:
        """Call the LLM under a hard wall-clock deadline.

        ``urlopen(timeout=N)`` is only an idle/socket-operation bound, not a
        total cap: a slow-trickling endpoint can keep the hook alive past
        Hermes' hook-callback budget. Run the request in a daemon thread and
        give up once ``TOOL_SLIM_LLM_DEADLINE_SECONDS`` elapses so the
        deterministic body is always returned in time. Returns None on expiry.
        """
        deadline = _env_int("TOOL_SLIM_LLM_DEADLINE_SECONDS", 15)
        result: dict[str, str] = {}

        def _run() -> None:
            try:
                result["summary"] = self._call_llm(base_url, model, prompt)
            except Exception:
                result["summary"] = ""

        worker = threading.Thread(target=_run, name=f"{PLUGIN_NAME}-llm", daemon=True)
        worker.start()
        worker.join(timeout=deadline)
        if worker.is_alive():
            if _env_bool("TOOL_SLIM_DEBUG"):
                print(
                    f"[{PLUGIN_NAME}] llm_compaction_deadline_exceeded after {deadline}s; using deterministic body",
                    file=sys.stderr,
                )
            return None
        return result.get("summary") or None

    def _compact_json(self, value: Any, depth: int = 0) -> str:
        max_items = _env_int("TOOL_SLIM_JSON_MAX_ITEMS", 20)
        if depth >= 3:
            return self._json_leaf(value)

        if isinstance(value, dict):
            for field in ("content", "output"):
                if not isinstance(value.get(field), str):
                    continue
                records = self._compact_text_field_records(value, field, max_items)
                if records is not None:
                    return records
                if field == "output" and len(value["output"]) <= 1000:
                    continue
                return self._compact_structured_text_result(value, field)
            lines = [f"JSON object with {len(value)} keys: {', '.join(map(str, list(value)[:max_items]))}"]
            for key, item in list(value.items())[:max_items]:
                lines.append(f"- {key}: {self._compact_json(item, depth + 1)}")
            if len(value) > max_items:
                lines.append(f"- ... {len(value) - max_items} more keys omitted")
            return "\n".join(lines)

        if isinstance(value, list):
            rows = self._json_record_rows(value, max_items)
            if rows is not None:
                return rows
            lines = [f"JSON array with {len(value)} items"]
            for index, item in enumerate(value[:max_items]):
                lines.append(f"- [{index}]: {self._compact_json(item, depth + 1)}")
            if len(value) > max_items:
                lines.append(f"- ... {len(value) - max_items} more items omitted")
            return "\n".join(lines)

        return self._json_leaf(value)

    def _json_record_rows(self, value: list[Any], max_items: int) -> str | None:
        """Schema-once rendering for a uniform array of records.

        When every element is a dict sharing the identical key set, emit the
        field names once as a header and each record as a | -separated row, so
        the model is not re-told the meaning of every field per record. This is
        the lossless, format-aware compaction the TOON article motivates.
        Returns None for irregular/nested/mixed arrays (existing expansion)."""
        min_records = 5  # ponytail: uniform rows pay off on bulk arrays; tune if real workloads differ
        if len(value) < min_records or not all(isinstance(item, dict) for item in value):
            return None
        keys = sorted(value[0].keys())
        if not keys or any(sorted(item.keys()) != keys for item in value[1:]):
            return None

        lines = [f"JSON records: {len(value)} rows", "fields: " + ", ".join(keys)]
        for index, item in enumerate(value[:max_items]):
            cells = " | ".join(self._json_record_cell(item[key]) for key in keys)
            lines.append(f"  [{index}] {cells}")
        if len(value) > max_items:
            lines.append(f"- ... {len(value) - max_items} more rows omitted")
        return "\n".join(lines)

    def _json_record_cell(self, value: Any) -> str:
        cell = self._json_leaf(value)
        return cell.replace(" | ", " / ")

    def _compact_text_field_records(self, value: dict[str, Any], field: str, max_items: int) -> str | None:
        """A text field (terminal ``output``, read ``content``) may itself hold a
        JSON record array as an escaped string. When it does, compact it
        schema-once instead of head/tail truncation. Returns None otherwise."""
        text = value.get(field)
        if not isinstance(text, str) or len(text) <= 1000:
            return None
        if text.lstrip()[:1] not in ("[", "{"):
            return None
        parsed = self._try_json(text)
        if not isinstance(parsed, list):
            return None
        rows = self._json_record_rows(parsed, max_items)
        if rows is None:
            return None

        lines = [f"JSON object with {len(value)} keys: {', '.join(map(str, value.keys()))}"]
        for meta_key in ("total_lines", "file_size", "truncated", "hint", "is_binary", "is_image", "exit_code", "error", "status"):
            if meta_key in value and meta_key != field:
                lines.append(f"- {meta_key}: {self._json_leaf(value[meta_key])}")
        lines.append(f"- {field}: parsed as JSON (below)")
        lines.extend(f"  {line}" for line in rows.splitlines())
        return "\n".join(lines)


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
        if content.lstrip().startswith((COMPACTED_MARKER, LEGACY_MARKER)):
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

    def _is_coding_assistant_output(self, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        name = tool_name.lower()
        if any(token in name for token in CODING_TOOL_NAMES):
            return True
        if isinstance(args, dict):
            command = self._to_text(args.get("command") or args.get("cmd") or "").strip().lower()
            executable = os.path.basename(command.split()[0]) if command.split() else ""
            if executable in CODING_TOOL_NAMES:
                return True
        return self._has_code_diff_signal(text) or self._value_has_code_artifact(parsed)

    def _has_code_diff_signal(self, text: str) -> bool:
        if "```" in text or "diff --git" in text:
            return True
        return bool(re.search(r"(?m)^@@ |^\+\+\+ [ab]/|^--- [ab]/|^Index: |^new file mode |^deleted file mode ", text))

    def _value_has_code_artifact(self, value: Any, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(value, dict):
            if value.get("type") == "patch" or "diff" in value or "patch" in value:
                return True
            return any(self._value_has_code_artifact(child, depth + 1) for child in value.values())
        if isinstance(value, list):
            return any(self._value_has_code_artifact(child, depth + 1) for child in value)
        return isinstance(value, str) and self._has_code_diff_signal(value)

    def _preserved_code_diff_sections(self, tool_name: str, args: Any, parsed: Any, text: str, max_chars: int) -> str:
        if not self._is_coding_assistant_output(tool_name, args, parsed, text):
            return ""
        budget = _env_int("TOOL_SLIM_CODE_MAX_CHARS", max(1200, max_chars // 2))
        snippets = self._code_diff_snippets(text)
        if parsed is not None:
            snippets += self._code_diff_snippets_from_value(parsed)

        kept: list[str] = []
        seen: set[str] = set()
        used = 0
        omitted = 0
        truncated = False
        for snippet in snippets:
            snippet = snippet.strip()
            key = snippet[:500]
            if not snippet or key in seen:
                continue
            seen.add(key)
            if used + len(snippet) + 2 <= budget:
                kept.append(snippet)
                used += len(snippet) + 2
                continue
            remaining = budget - used - 80
            if remaining > 200:
                kept.append(snippet[:remaining] + "\n[code/diff snippet truncated]")
                used = budget
                truncated = True
            else:
                omitted += 1
            omitted += len(snippets) - len(seen)
            break

        if not kept:
            return ""
        lines = ["Preserved code/diff sections:"]
        lines.extend(kept)
        if omitted:
            lines.append(f"code_diff_sections_omitted: {omitted}")
        if truncated:
            lines.append("code_diff_truncated: true")
        return "\n\n".join(lines)

    def _code_diff_snippets_from_value(self, value: Any, depth: int = 0) -> list[str]:
        if depth > 8:
            return []
        if isinstance(value, dict):
            snippets: list[str] = []
            if value.get("type") == "patch" and value.get("files"):
                snippets.append("patch files: " + self._one_line(self._to_text(value.get("files")), 1000))
            for key, child in value.items():
                if key in {"diff", "output", "content", "text"} and isinstance(child, str):
                    snippets.extend(self._code_diff_snippets(child))
                elif key == "patch":
                    snippets.append(self._one_line(self._to_text(child), 2000))
                snippets.extend(self._code_diff_snippets_from_value(child, depth + 1))
            return snippets
        if isinstance(value, list):
            snippets = []
            for child in value:
                snippets.extend(self._code_diff_snippets_from_value(child, depth + 1))
            return snippets
        if isinstance(value, str):
            return self._code_diff_snippets(value)
        return []

    def _code_diff_snippets(self, text: str) -> list[str]:
        snippets = [match.group(0) for match in re.finditer(r"```[\s\S]*?```", text)]
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("diff --git") or line.startswith("Index: "):
                start = i
                i += 1
                while i < len(lines) and not lines[i].startswith(("diff --git", "Index: ")):
                    i += 1
                snippets.append("\n".join(lines[start:i]))
                continue
            if line.startswith("@@ "):
                start = max(0, i - 2)
                end = min(len(lines), i + 60)
                snippets.append("\n".join(lines[start:end]))
            i += 1
        return snippets

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
    plugin = ToolOutputCompactorPlugin()
    ctx.register_hook("transform_tool_result", plugin.transform_tool_result)


class PytestExtractor(_Extractor):
    """Type-aware extractor for pytest / test-runner output."""

    kind = "pytest"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        return _is_pytest_output(_result_output_text(parsed, text))

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        out = _result_output_text(parsed, text)
        lines: list[str] = []
        match = _PYTEST_COUNTS_RE.search(out)
        lines.append("pytest summary: " + match.group(1).strip() if match else "pytest run")
        fails = _pytest_failure_lines(out)
        if fails:
            lines.append("Failing tests:")
            lines.extend("  " + line for line in fails[:20])
            if len(fails) > 20:
                lines.append(f"  ... {len(fails) - 20} more failing/erroring tests")
        evidence = _pytest_error_evidence(out)
        if evidence:
            lines.append("Error evidence:")
            lines.extend("  " + line for line in evidence[:12])
        return ExtractedResult(
            result_type="pytest",
            body="\n".join(lines),
            decision=CompactionDecision("deterministic", "pytest output"),
        )


class GitStatusExtractor(_Extractor):
    """Type-aware extractor for git status (short/porcelain or long form)."""

    kind = "git_status"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        out = _result_output_text(parsed, text)
        cmd = _command_hint(args)
        if "git status" in cmd or "git -s" in cmd:
            return _git_status_signal(out)
        if not cmd and not isinstance(args, dict):
            return _git_status_signal(out) and len(_git_porcelain_lines(out)) >= 8
        return False

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        out = _result_output_text(parsed, text)
        porcelain = _git_porcelain_lines(out)
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        branch = ""
        match = re.search(r"(?m)^On branch (\S+)", out)
        if match:
            branch = match.group(1)
        if porcelain:
            for row in porcelain:
                x, y, path = row[0], row[1], row[3:].strip()
                if x == "?":
                    untracked.append(path)
                elif x != " ":
                    staged.append(path)
                if y in "MD":
                    unstaged.append(path)
                elif y not in " ?" and x == " ":
                    unstaged.append(path)
        else:
            heading = ""
            for raw in out.splitlines():
                if raw.startswith(("Changes to be committed:", "Changes not staged for commit:", "Untracked files:")):
                    heading = raw.split(":")[0]
                    continue
                if heading == "" or not raw.strip() or raw.lstrip().startswith(("(", 'use "')):
                    continue
                if not raw.startswith((" ", "\t")):
                    continue
                if raw.startswith("  "):
                    path = _git_normal_path(raw)
                    if not path:
                        continue
                    if heading == "Changes to be committed":
                        staged.append(path)
                    elif heading == "Untracked files":
                        untracked.append(path)
                    else:
                        unstaged.append(path)
        lines = ["git status summary:"]
        if branch:
            lines.append(f"branch: {branch}")
        lines.append(f"changes_to_be_committed: {len(staged)}")
        lines.append(f"changes_not_staged: {len(unstaged)}")
        lines.append(f"untracked_files: {len(untracked)}")
        for label, items in (("staged", staged), ("modified/deleted", unstaged), ("untracked", untracked)):
            if items:
                shown = items[:20]
                lines.append(f"{label} ({len(items)} total, first {len(shown)}):")
                lines.extend("  " + path for path in shown)
        return ExtractedResult(
            result_type="git_status",
            body="\n".join(lines),
            decision=CompactionDecision("deterministic", "git status output"),
        )


class GitLogExtractor(_Extractor):
    """Type-aware extractor for git log output."""

    kind = "git_log"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        cmd = _command_hint(args)
        if "git log" not in cmd:
            return False
        return True

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        out = _result_output_text(parsed, text)
        lines: list[str] = []
        if bool(re.search(r"(?m)^[0-9a-f]{7,40} .+", out)) and not re.search(r"(?m)^commit [0-9a-f]{40}", out):
            rows = [line for line in out.splitlines() if re.match(r"^[0-9a-f]{7,40} ", line)]
            lines.append(f"git log --oneline summary: {len(rows)} commits")
            lines.extend("  " + row for row in rows[:25])
        else:
            blocks = out.split("\ncommit ")
            lines.append(f"git log summary: {len(blocks)} commits")
            subjects = []
            for block in blocks:
                subject = ""
                for raw in block.splitlines()[1:]:
                    if raw.strip() and not raw.startswith(("Author:", "Date:", "    ")):
                        subject = raw.strip()
                        break
                if subject:
                    subjects.append(subject)
            lines.extend("  " + subject for subject in subjects[:15])
        return ExtractedResult(
            result_type="git_log",
            body="\n".join(lines),
            decision=CompactionDecision("deterministic", "git log output"),
        )


class SessionSearchExtractor(_Extractor):
    """Generic structured extractor for Hermes session_search results."""

    kind = "generic"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        return tool_name == "session_search" and parsed is not None

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        return ExtractedResult(result_type="session_search", body=plugin._compact_session_search(parsed))


class JsonExtractor(_Extractor):
    """Generic structured extractor for any JSON-shaped result."""

    kind = "generic"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        return parsed is not None

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        limit = _env_int("TOOL_SLIM_IMPORTANT_LINES", 40)
        return ExtractedResult(
            result_type="json",
            body=plugin._compact_json(parsed),
            important=plugin._important_from_value(parsed, limit),
        )


class TextExtractor(_Extractor):
    """Terminal generic fallback: any remaining textual result."""

    kind = "generic"

    def match(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> bool:
        return True

    def extract(self, plugin: Any, tool_name: str, args: Any, parsed: Any, text: str) -> ExtractedResult:
        limit = _env_int("TOOL_SLIM_IMPORTANT_LINES", 40)
        return ExtractedResult(
            result_type="text",
            body=plugin._compact_text(text),
            important=plugin._important_lines(text, limit),
        )


ToolSlimPlugin = ToolOutputCompactorPlugin


def _demo() -> None:
    root = Path(__file__).resolve().parent
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == __version__
    assert f"version: {__version__}" in (root / "plugin.yaml").read_text(encoding="utf-8")

    saved_env = {name: os.environ.get(name) for name in os.environ if name.startswith("TOOL_SLIM_LLM_")}
    for name in saved_env:
        os.environ.pop(name, None)

    plugin = ToolOutputCompactorPlugin()
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

    bg_repeat_plugin = ToolOutputCompactorPlugin()
    bg_first = '{"output": "Background process started", "session_id": "proc_one", "pid": 111, "exit_code": 0}'
    bg_second = '{"output": "Background process started", "session_id": "proc_two", "pid": 222, "exit_code": 0}'
    assert bg_repeat_plugin.transform_tool_result(
        tool_name="terminal",
        args={"command": "python3 worker.py", "cwd": "/repo"},
        result=bg_first,
        session_id="sessBg",
    ) is not None
    bg_repeat = bg_repeat_plugin.transform_tool_result(
        tool_name="terminal",
        args={"command": "python3 worker.py", "cwd": "/repo"},
        result=bg_second,
        session_id="sessBg",
    )
    assert bg_repeat is not None
    assert "event: repeated_background_process_start" in bg_repeat
    assert "repeat_count: 2" in bg_repeat
    assert "previous_session_id: proc_one" in bg_repeat
    assert "current_session_id: proc_two" in bg_repeat
    assert "DO NOT" not in bg_repeat

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

    dedup_read = ToolOutputCompactorPlugin()
    read_body = {"content": "row\n" * 2000, "total_lines": 2000, "file_size": 8000, "truncated": False}
    first_read = dedup_read.transform_tool_result(tool_name="read_file", args={"path": "/tmp/listing.txt"}, result=read_body, session_id="sessRead", status="ok")
    second_read = dedup_read.transform_tool_result(tool_name="read_file", args={"path": "/tmp/listing.txt"}, result=read_body, session_id="sessRead", status="ok")
    assert first_read is not None and "mode: dedup" not in first_read
    assert second_read is not None and len(second_read) > 0
    assert "mode: dedup" in second_read
    assert "args.path: /tmp/listing.txt" in second_read
    assert "status: ok" in second_read
    assert "raw_chars:" in second_read
    assert "saved_chars_estimate:" in second_read

    call_plugin = ToolOutputCompactorPlugin()
    first_call = call_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessCall", tool_call_id="call1")
    second_call = call_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessCall", tool_call_id="call1")
    third_call = call_plugin.transform_tool_result(tool_name="process", result=dup_body, session_id="sessCall", tool_call_id="call2")
    assert first_call is not None and "mode: dedup" not in first_call
    assert second_call is None
    assert third_call is not None and "mode: dedup" in third_call

    os.environ["TOOL_SLIM_DEDUP_MODE"] = "minimal"
    min_plugin = ToolOutputCompactorPlugin()
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
    assert COMPACTED_MARKER in compact
    assert f"{PLUGIN_NAME}: compacted terminal" in compact
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
    assert f"notice: {PLUGIN_NAME} compacted this result" in compact_notice
    os.environ.pop("TOOL_SLIM_NOTICE_IN_RESULT", None)

    data = {"items": list(range(2000)), "status": "ok"}
    compact_json = plugin.transform_tool_result(tool_name="api", result=data)
    assert compact_json is not None
    assert "JSON object" in compact_json

    record_severities = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
    records = [
        {
            "key": f"AZ{i:08d}",
            "severity": record_severities[i % len(record_severities)],
            "component": f"src/main/java/com/acme/Module{i % 7}.java",
            "line": i % 300,
            "status": "OPEN",
        }
        for i in range(200)
    ]
    compact_records = plugin.transform_tool_result(tool_name="api", result=records)
    assert compact_records is not None
    assert "JSON records: 200 rows" in compact_records
    assert "fields: component, key, line, severity, status" in compact_records
    assert compact_records.count("fields: ") == 1
    assert "src/main/java/com/acme/Module0.java | AZ00000000 | 0 | BLOCKER | OPEN" in compact_records
    assert "more rows omitted" in compact_records
    assert len(compact_records) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)

    mixed_records = [{"a": i, "b": "x" * 50} for i in range(150)]
    mixed_records += [{"c": i, "d": "y" * 50} for i in range(50)]
    compact_mixed = plugin.transform_tool_result(tool_name="api", result=mixed_records)
    assert compact_mixed is not None
    assert "JSON records:" not in compact_mixed
    assert "JSON array with 200 items" in compact_mixed

    import json as _json

    nested_records = {"output": _json.dumps(records), "exit_code": 0, "error": None}
    compact_nested = plugin.transform_tool_result(tool_name="terminal", args={"command": "python gen_records.py"}, result=nested_records)
    assert compact_nested is not None
    assert "JSON records: 200 rows" in compact_nested
    assert "fields: component, key, line, severity, status" in compact_nested
    assert compact_nested.count("fields: ") == 1
    assert "src/main/java/com/acme/Module0.java | AZ00000000 | 0 | BLOCKER | OPEN" in compact_nested
    assert "more rows omitted" in compact_nested
    assert "- exit_code: 0" in compact_nested
    assert "Head lines" not in compact_nested

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

    structured_llm_plugin = ToolOutputCompactorPlugin()
    structured_llm_plugin._call_llm = lambda *_: "bad summary"  # type: ignore[method-assign]
    glob_result = [f"/repo/src/file_{i}.py" for i in range(300)]
    compact_glob = structured_llm_plugin.transform_tool_result(tool_name="glob", args={"path": "/repo"}, result=glob_result)
    assert compact_glob is not None
    assert "mode: deterministic" in compact_glob
    assert "decision_reason: structured tool glob" in compact_glob
    assert "bad summary" not in compact_glob

    grep_result = "\n".join(f"/repo/src/file_{i}.py:{i}:match text" for i in range(300))
    compact_grep = structured_llm_plugin.transform_tool_result(tool_name="grep", args={"query": "match"}, result=grep_result)
    assert compact_grep is not None
    assert "mode: deterministic" in compact_grep
    assert "decision_reason: structured tool grep" in compact_grep
    assert "bad summary" not in compact_grep

    compact_structured_search = structured_llm_plugin.transform_tool_result(
        tool_name="session_search",
        result={
            "session_id": "sess_structured",
            "message_count": 220,
            "truncated": True,
            "messages": [{"role": "tool", "tool_name": "terminal", "content": f"progress {i} /repo/file_{i}.py"} for i in range(220)],
        },
    )
    assert compact_structured_search is not None
    assert "mode: deterministic" in compact_structured_search
    assert "decision_reason: structured tool session_search" in compact_structured_search
    assert "bad summary" not in compact_structured_search

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

    os.environ["TOOL_SLIM_LLM_DEADLINE_SECONDS"] = "1"
    slow_llm = ToolOutputCompactorPlugin()
    slow_llm._call_llm = lambda *_: time.sleep(30)  # type: ignore[method-assign]
    slow_compact = slow_llm.transform_tool_result(tool_name="terminal", result="noisy narrative prose\n" * 3000)
    assert slow_compact is not None
    assert "mode: deterministic" in slow_compact
    assert "LLM summary" not in slow_compact
    os.environ.pop("TOOL_SLIM_LLM_DEADLINE_SECONDS", None)

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

    already_compacted = {"id": 99, "role": "tool", "tool_name": "session_search", "content": f"{COMPACTED_MARKER}\ntool: session_search\nmode: deterministic\n... error: null ..."}
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

    filler = "\n".join(f"test_worker_{i} -> running stage {i % 7} of pipeline\n" for i in range(120))
    pytest_body = (
        "============================= test session starts =============================\n"
        "collected 1042 items\n\n"
        + filler
        + "\n============================= FAILURES =============================\n"
        "_______________________________ test_refresh _______________________________\n"
        "tests/test_auth.py:42: in test_refresh\n"
        "E       AssertionError: expected 200, got 401\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_auth.py::test_refresh - AssertionError: expected 200, got 401\n"
        "========================= 1 failed, 1041 passed in 12.45s =========================\n"
    )
    compact_pytest = plugin.transform_tool_result(
        tool_name="terminal",
        args={"command": "pytest -v tests/test_auth.py"},
        result=pytest_body,
        status="ok",
    )
    assert compact_pytest is not None
    assert "result_type: pytest" in compact_pytest
    assert "mode: deterministic" in compact_pytest
    assert "decision_reason: pytest output" in compact_pytest
    assert "pytest summary: 1 failed, 1041 passed in 12.45s" in compact_pytest
    assert "FAILED tests/test_auth.py::test_refresh - AssertionError: expected 200, got 401" in compact_pytest
    assert "E       AssertionError: expected 200, got 401" in compact_pytest
    assert "test_worker_7" not in compact_pytest
    assert len(compact_pytest) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)

    git_status_rows = [f" M src/module_{i:04d}.py" for i in range(200)]
    git_status_rows += [f"A  src/new_{i}.py" for i in range(40)]
    git_status_rows += [f"?? untracked_dir/file_{i}.txt" for i in range(30)]
    compact_git_status = plugin.transform_tool_result(
        tool_name="terminal",
        args={"command": "git status --porcelain"},
        result="\n".join(git_status_rows) + "\n",
    )
    assert compact_git_status is not None
    assert "result_type: git_status" in compact_git_status
    assert "mode: deterministic" in compact_git_status
    assert "decision_reason: git status output" in compact_git_status
    assert "git status summary:" in compact_git_status
    assert "changes_to_be_committed: 40" in compact_git_status
    assert "changes_not_staged: 200" in compact_git_status
    assert "untracked_files: 30" in compact_git_status
    assert "src/module_0000.py" in compact_git_status
    assert "src/new_0.py" in compact_git_status
    assert len(compact_git_status) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)

    git_log_rows = [f"{i:07x} Refactor subsystem {i}" for i in range(400, 0, -1)]
    compact_git_log = plugin.transform_tool_result(
        tool_name="terminal",
        args={"command": "git log --oneline -200"},
        result="\n".join(git_log_rows) + "\n",
    )
    assert compact_git_log is not None
    assert "result_type: git_log" in compact_git_log
    assert "git log --oneline summary: 400 commits" in compact_git_log
    assert "Refactor subsystem 400" in compact_git_log
    assert len(compact_git_log) <= _env_int("TOOL_SLIM_MAX_CHARS", 4000)

    for name in list(os.environ):
        if name.startswith("TOOL_SLIM_LLM_"):
            os.environ.pop(name, None)
    for name, value in saved_env.items():
        if value is not None:
            os.environ[name] = value


if __name__ == "__main__":
    _demo()
    print(f"{PLUGIN_NAME} self-check ok")
