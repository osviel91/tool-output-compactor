# AGENTS.md

## Handoff

This folder is a seed for a new standalone Hermes plugin named `tool-slim`.

The user wants to optimize small-context Hermes agents. The main pain is not persistent memory; it is active context growth from large tool outputs. Hermes recently added a `transform_tool_result` hook. This plugin should use that hook to compact tool results before they are sent back to the model.

## Current Repository Context

This folder currently lives inside the `fast-brain` repo only for convenience. It should be extracted into a separate project later.

Keep responsibilities separate:

- `fast-brain`: memory, retrieval, consolidation, context recommendation.
- `tool-slim`: runtime tool-result compaction.

Do not add fast-brain API calls in V1.

## What Exists

- `plugin.yaml`: declares the intended Hermes hook.
- `__init__.py`: dependency-free base plugin with deterministic compaction.
- `README.md`: user-facing overview and install sketch.
- `PLAN.md`: implementation roadmap.
- `AGENTS.md`: this handoff.

## Hermes Hook Contract

Confirmed against `NousResearch/hermes-agent`:

- Register with `ctx.register_hook("transform_tool_result", callback)`.
- Hermes passes keyword arguments including `tool_name`, `args`, `result`, ids, `duration_ms`, `status`, `error_type` and `error_message`.
- Return a `str` to replace the tool result; return `None` to leave it unchanged.
- `plugin.yaml` declares the hook, but `register()` must register it.

Current method:

```python
def transform_tool_result(
    self,
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> str | None:
```

Adapt it once the real contract is known.

## Design Rules

- Deterministic first.
- No LLM summarizer in V1.
- No dependencies.
- Do not hide failures: preserve errors, warnings, tracebacks, exit codes and failed status.
- Compact only when output exceeds `TOOL_SLIM_MAX_CHARS`.
- Always say compaction happened and how much was omitted.
- Prefer one boring file over abstractions.

## Test Command

```bash
python3 -m compileall tool-slim
python3 tool-slim/__init__.py
```

The second command runs minimal self-checks.

## Next Agent First Task

Test in a live Hermes profile and tune defaults if useful details are missing from compacted outputs.
