# tool-output-compactor Plan

> Updated 0.6.1: schema-once record-array compaction also applies when the
> uniform record list is a JSON string inside a tool's structured text field
> (terminal `output`/read `content`) — parsed and compacted schema-once with
> meta keys preserved, instead of head/tail truncation.
> Earlier: 0.6.0 introduced the schema-once `fields:` header + `|`-separated
> rows (deterministic, lossless) instead of repeating field names per record —
> the consumer-side form of the TOON idea (The New Stack, Aug 2026). See the
> redirect marker notes below.

## Redirect (REDIRECTION_PLAN.md)

- Phase 0 — repository + Hermes audit: done.
- Phase 1 — `classify → extract → budget → render` pipeline with a typed
  extractor registry and generic fallbacks: done (0.5.0).
- Phase 2 — first specialized extractors (pytest, git status/log) with fixture
  self-checks: done (0.5.0).
- Phase 3 — coexistence contract tests with `hermes-progress-guard` + telemetry
  (`extractor_selected` via `result_type`, `dedup` log fields, per-message KPIs):
  done (0.5.0, `coexistence_test.py` 6/6 scenarios).
- 0.6.0 — schema-once JSON record arrays (uniform, same-shape dict lists render
  as `fields:` header + one row per record) inside the generic JSON path.
- 0.6.1 — schema-once also fires for uniform record lists embedded as a JSON
  string in a tool's `output`/`content` text field (the real terminal shape).
- 0.6.x — live harness (`live/live_test.py` + workload generators) persisted in
  repo; real-guard coexistence verified `COEXISTENCE_REAL=1` 6/6; failure,
  noisy-log and listing paths re-validated live on `fast-new`. No default
  changes warranted.
- Future — more extractors only when justified by observed real workloads
  (Docker, compiler/build, npm/pip, mypy, ESLint, coverage); per-section token
  budgeting is speculative and deferred.

Non-goals (from the redirect): do not duplicate Hermes context management,
tool-output limits, pruning or loop guardrails; do not steer agent behavior;
do not add fast-brain API calls.

---

## Historical: Phase 1: Confirm Hermes Hook

Confirmed against `NousResearch/hermes-agent`.

Contract:

- Plugins register with `ctx.register_hook("transform_tool_result", callback)`.
- Hermes calls the callback with keyword arguments: `tool_name`, `args`, `result`, ids, `duration_ms`, `status`, `error_type` and `error_message`.
- The hook runs after `post_tool_call` and before appending the result back into conversation context.
- The first returned `str` replaces the result.
- `None` or non-string returns leave the result unchanged.
- Hook errors are fail-open.
- `plugin.yaml` declares hooks, but code registration is still required.

Implemented shape:

```python
def transform_tool_result(
    self,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> str | None:
    ...
```

## Phase 2: Deterministic Compaction

Implemented base behavior:

- Skip small results.
- Compact oversized plain text with head/tail.
- Preserve important lines containing errors, warnings, failures and tracebacks.
- Compact JSON by showing structure, top-level keys and bounded previews.
- Always include raw size and omitted character count.

Acceptance checks:

- Small output returns unchanged.
- Large log returns compact text under target size.
- JSON output shows top-level shape and avoids dumping huge arrays.
- Error lines survive compaction.

## Historical: Phase 3: Tool-Specific Policies

Superseded by the typed extractor registry in the redirect (Pytest/Git now; the
generic structured/text paths cover read_file/grep/JSON/session_search). Keep
specialized policies small; add a class per format only when generic compaction
is not enough.

## Historical: Phase 4: Optional LLM fallback

LLM compression exists and remains local/runtime-only:

- Disabled by default.
- Uses an OpenAI-compatible `/v1/chat/completions` endpoint when configured.
- Falls back to deterministic compaction on missing config, timeout or API failure.
- Preserves deterministic critical lines alongside the LLM summary.

Possible later flow (only if a concrete need to recover raw outputs appears):

```txt
raw tool output
  -> tool-output-compactor compact result for active context
  -> optional raw/summary archival
```

## Historical: Phase 5: Rollout

Start on one Hermes profile only.

Suggested long-task diagnostic settings:

```env
TOOL_SLIM_ENABLED=true
TOOL_SLIM_MAX_CHARS=12000
TOOL_SLIM_MIN_SAVING_CHARS=2000
TOOL_SLIM_HEAD_CHARS=1200
TOOL_SLIM_TAIL_CHARS=1200
TOOL_SLIM_HEAD_LINES=30
TOOL_SLIM_TAIL_LINES=10
TOOL_SLIM_IMPORTANT_LINES=40
TOOL_SLIM_DEDUP=true
TOOL_SLIM_DEDUP_MODE=stub
TOOL_SLIM_DEDUP_MIN_CHARS=4000
TOOL_SLIM_DEDUP_WINDOW=100
TOOL_SLIM_SESSION_TAIL=8
TOOL_SLIM_JSON_MAX_ITEMS=20
TOOL_SLIM_DEBUG=false
TOOL_SLIM_AUDIT=false
TOOL_SLIM_NOTICE_IN_RESULT=true
TOOL_SLIM_LLM_ENABLED=true
TOOL_SLIM_LLM_TIMEOUT_SECONDS=60
TOOL_SLIM_LLM_MIN_CHARS=12000
TOOL_SLIM_LLM_MAX_CHARS=3000
TOOL_SLIM_LLM_MAX_TOKENS=700
```

Observe:

- Does the agent still solve tasks?
- Does context pressure drop?
- Are needed details missing from compacted tool outputs?
- Which tools need special policy?
