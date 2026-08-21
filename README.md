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
- Errors, warnings, tracebacks, stderr and exit codes are prioritized.
- The compacted result always says compaction happened and reports omitted size.

Optional LLM compaction can be enabled with an OpenAI-compatible `/v1/chat/completions` endpoint. The LLM only sees the deterministic compacted body, not the full raw result. If the LLM call fails, times out or returns empty text, `tool-slim` falls back to deterministic compaction.

## Non-Goals

- No database.
- No embeddings.
- No dependency on fast-brain.
- No raw-output archive.

## Environment

```env
TOOL_SLIM_ENABLED=true
TOOL_SLIM_MAX_CHARS=4000
TOOL_SLIM_HEAD_CHARS=1200
TOOL_SLIM_TAIL_CHARS=1200
TOOL_SLIM_IMPORTANT_LINES=40
TOOL_SLIM_JSON_MAX_ITEMS=20
TOOL_SLIM_DEBUG=false

# Optional OpenAI-compatible compressor. If unset or failing, deterministic compaction is used.
TOOL_SLIM_LLM_ENABLED=false
TOOL_SLIM_LLM_BASE_URL=https://example.com/v1
TOOL_SLIM_LLM_MODEL=compressor
TOOL_SLIM_LLM_API_KEY=
TOOL_SLIM_LLM_TIMEOUT_SECONDS=5
TOOL_SLIM_LLM_MAX_CHARS=2000
TOOL_SLIM_LLM_MAX_TOKENS=700
```

Example LLM compressor config:

```env
TOOL_SLIM_LLM_ENABLED=true
TOOL_SLIM_LLM_BASE_URL=https://omlx.osviel.duckdns.org/v1
TOOL_SLIM_LLM_MODEL=compressor
TOOL_SLIM_LLM_API_KEY=your-key-here
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

## Current State

The code uses Hermes' native plugin hook registration:

```python
ctx.register_hook("transform_tool_result", callback)
```

Hermes calls the hook with keyword arguments including `tool_name`, `args`, `result`, ids, duration and status fields. Returning a string replaces the tool result in model context; returning `None` leaves it unchanged.

See `AGENTS.md` and `PLAN.md` before continuing.
