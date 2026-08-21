# tool-slim Plan

## Phase 1: Confirm Hermes Hook

Find the exact `transform_tool_result` contract in Hermes.

Questions to answer:

- What method name is called?
- What arguments are passed?
- Does Hermes expect string, dict or original result type back?
- Is the hook synchronous or async?
- How is a non-memory plugin registered?
- Can plugins declare `transform_tool_result` in `plugin.yaml` only, or is code registration required?

Expected shape might be one of:

```python
def transform_tool_result(self, tool_name: str, result: Any, **kwargs: Any) -> Any:
    ...
```

```python
def transform_tool_result(self, result: Any, *, tool_name: str = "", **kwargs: Any) -> Any:
    ...
```

The current implementation accepts flexible `*args, **kwargs` so it can be adapted quickly.

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

Possible later flow:

```txt
raw tool output
  -> tool-slim compact result for active context
  -> optional raw/summary storage in fast-brain/archive
```

Only add this if there is a concrete need to recover raw outputs later.

## Phase 5: Rollout

Start on one Hermes profile only.

Suggested safe settings:

```env
TOOL_SLIM_ENABLED=true
TOOL_SLIM_MAX_CHARS=4000
TOOL_SLIM_HEAD_CHARS=1000
TOOL_SLIM_TAIL_CHARS=1000
TOOL_SLIM_IMPORTANT_LINES=40
```

Observe:

- Does the agent still solve tasks?
- Does context pressure drop?
- Are needed details missing from compacted tool outputs?
- Which tools need special policy?
