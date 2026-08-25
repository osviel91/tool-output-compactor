# tool-slim Plan

## Phase 1: Confirm Hermes Hook

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

## Phase 3: Tool-Specific Policies

Add specialized handling only when generic compaction is not enough.

Likely policies:

- `terminal`: command, exit code, stderr, important lines, tail.
- `read_file`: path, line range, head/tail.
- `grep`/search: query, total matches, first matches.
- MCP JSON APIs: top-level keys, status, ids, counts, errors.
- logs: warnings/errors plus tail.

Keep these as small `if tool_name` branches. No class hierarchy unless it becomes unavoidable.

## Phase 4: Optional fast-brain Integration

Not for V1.

Optional LLM compression exists before fast-brain integration and remains local/runtime-only:

- Disabled by default.
- Uses an OpenAI-compatible `/v1/chat/completions` endpoint when configured.
- Falls back to deterministic compaction on missing config, timeout or API failure.
- Preserves deterministic critical lines alongside the LLM summary.

Possible later flow:

```txt
raw tool output
  -> tool-slim compact result for active context
  -> optional raw/summary storage in fast-brain/archive
```

Only add this if there is a concrete need to recover raw outputs later.

## Phase 5: Rollout

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
