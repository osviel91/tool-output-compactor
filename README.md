# tool-slim

Hermes plugin concept for compacting large tool results before they are sent back into the model context.

This is intentionally separate from `fast-brain`:

- `fast-brain` handles persistent memory, retrieval, consolidation and context recommendation.
- `tool-slim` handles active context pressure caused by large tool outputs.

## Goal

Small-context agents often fail during long tasks because tool results keep accumulating in the active prompt. `tool-slim` should use Hermes' `transform_tool_result` hook to replace oversized tool results with compact, useful summaries before they re-enter the conversation context.

```txt
Tool runs
  -> raw result
  -> tool-slim transform_tool_result
  -> compact result goes back to Hermes/model context
```

## Non-Goals For V1

- No database.
- No embeddings.
- No LLM summarizer.
- No dependency on fast-brain.
- No raw-output archive.

V1 is deterministic and local only.

## Environment

```env
TOOL_SLIM_ENABLED=true
TOOL_SLIM_MAX_CHARS=4000
TOOL_SLIM_HEAD_CHARS=1200
TOOL_SLIM_TAIL_CHARS=1200
TOOL_SLIM_IMPORTANT_LINES=40
TOOL_SLIM_JSON_MAX_ITEMS=20
```

## Install Sketch

Copy this directory into the Hermes plugins directory once the exact Hermes plugin registration API is confirmed:

```bash
cp -R tool-slim ~/.hermes/plugins/tool-slim
```

Then enable the plugin according to the current Hermes plugin system.

## Current State

The code includes a working deterministic compactor and a flexible `transform_tool_result` method, but the exact Hermes hook signature still needs to be verified against the recent Hermes release.

See `AGENTS.md` and `PLAN.md` before continuing.
