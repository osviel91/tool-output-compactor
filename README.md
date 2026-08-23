# tool-slim

Hermes plugin for compacting large tool results before they are sent back into the model context.

This is intentionally separate from `fast-brain`:

- `fast-brain` handles persistent memory, retrieval, consolidation and context recommendation.
- `tool-slim` handles active context pressure caused by large tool outputs.

## Goal

Small-context agents often fail during long tasks because tool results keep accumulating in the active prompt. `tool-slim` uses Hermes' `transform_tool_result` hook to replace oversized tool results with compact summaries before they re-enter conversation context.

```txt
Tool runs
  -> raw result
  -> tool-slim transform_tool_result
  -> compact result goes back to Hermes/model context
```

## Behavior

By default, compaction is deterministic and dependency-free:

- Small results are left unchanged.
- Large plain text keeps important lines, head and tail.
- Large JSON keeps shape, keys and bounded item previews.
- Errors, warnings, tracebacks, stderr and exit codes are prioritized, including inside JSON string fields like terminal `output`.
- Concrete action facts are preserved separately from summaries: tool name, command-like args, paths, queries, exit codes, stderr, errors, status and approvals.
- The compacted result always says compaction happened and reports omitted size.

Optional LLM compaction can be enabled with an OpenAI-compatible `/v1/chat/completions` endpoint. The LLM only sees the deterministic compacted body, not the full raw result. If the LLM call fails, times out or returns empty text, `tool-slim` falls back to deterministic compaction.

## Non-Goals

- No database.
- No embeddings.
- No dependency on fast-brain.
- No raw-output archive.

## Environment

```env
# Master switch. Keep true in normal use; set false to diagnose raw Hermes tool results.
TOOL_SLIM_ENABLED=true

# Result size that triggers compaction. 4000 is conservative for small-context agents.
# Recommended: 4000-8000 for small models, 12000-20000 for larger local models.
TOOL_SLIM_MAX_CHARS=4000

# Character head/tail fallback for unstructured deterministic text compaction.
# Recommended: 800-2000 each; raise only if command endings keep losing useful context.
TOOL_SLIM_HEAD_CHARS=1200
TOOL_SLIM_TAIL_CHARS=1200

# Line head/tail for structured content fields such as read_file outputs and listings.
# Recommended: head 20-50, tail 10-30. Lower values avoid cutting the final budget.
TOOL_SLIM_HEAD_LINES=30
TOOL_SLIM_TAIL_LINES=10

# Max preserved lines matching error/warning/traceback/exit markers.
# Recommended: 20-80. Raise for noisy test/build logs with many distinct failures.
TOOL_SLIM_IMPORTANT_LINES=40

# Max object/array entries shown per JSON level before omitting the rest.
# Recommended: 10-30. Raise for compact API payloads; keep low for huge arrays.
TOOL_SLIM_JSON_MAX_ITEMS=20

# Print concise compaction diagnostics to stderr in addition to INFO logs.
TOOL_SLIM_DEBUG=false

# Add a visible notice inside every compacted result. Useful while testing, noisy in daily use.
TOOL_SLIM_NOTICE_IN_RESULT=false

# Optional OpenAI-compatible compressor. If unset or failing, deterministic compaction is used.
# Use LLM only for long unstructured output; structured content and failures stay deterministic.
TOOL_SLIM_LLM_ENABLED=false
TOOL_SLIM_LLM_BASE_URL=https://example.com/v1
TOOL_SLIM_LLM_MODEL=compressor
TOOL_SLIM_LLM_API_KEY=

# LLM request timeout. Recommended: 30s remote, 60-120s for cold local models.
TOOL_SLIM_LLM_TIMEOUT_SECONDS=30

# Minimum raw result size before LLM is allowed. Below this, deterministic is safer and cheaper.
# Recommended: 8000-20000. 12000 avoids summarizing medium structured results too early.
TOOL_SLIM_LLM_MIN_CHARS=12000

# Character budget for the LLM summary body, before preserved deterministic sections are added.
# Recommended: 1000-3000. Keep below TOOL_SLIM_MAX_CHARS.
TOOL_SLIM_LLM_MAX_CHARS=2000

# Token cap for the compressor response. Recommended: 400-1000.
TOOL_SLIM_LLM_MAX_TOKENS=700
```

Example LLM compressor config:

```env
TOOL_SLIM_LLM_ENABLED=true
TOOL_SLIM_LLM_BASE_URL=https://omlx.osviel.duckdns.org/v1
TOOL_SLIM_LLM_MODEL=compressor
TOOL_SLIM_LLM_API_KEY=your-key-here
TOOL_SLIM_LLM_TIMEOUT_SECONDS=60
```

Do not commit API keys.

## Install

Copy this directory into the Hermes plugins directory:

```bash
cp -R tool-slim ~/.hermes/plugins/tool-slim
```

Then enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - tool-slim
```

Validate installation:

```bash
hermes plugins doctor tool-slim
```

## Development

Run the built-in checks:

```bash
python3 -m compileall tool-slim
python3 tool-slim/__init__.py
```

The self-check includes deterministic compaction and a mocked LLM path. It does not call a real LLM endpoint.

Compaction emits an `INFO` log through Python logging. Enable `TOOL_SLIM_DEBUG=true` to also write concise compaction lines to `stderr` while testing.

## Versioning

Version lives in three places and must be bumped together on plugin updates:

- `VERSION`
- `plugin.yaml`
- `__init__.py` as `__version__`

Record user-visible changes in `CHANGELOG.md` so future agents can see what changed between installed versions.

## Current State

The code uses Hermes' native plugin hook registration:

```python
ctx.register_hook("transform_tool_result", callback)
```

Hermes calls the hook with keyword arguments including `tool_name`, `args`, `result`, ids, duration and status fields. Returning a string replaces the tool result in model context; returning `None` leaves it unchanged.

See `AGENTS.md` and `PLAN.md` before continuing.
