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
- `__init__.py`: dependency-free base plugin with deterministic, hybrid and optional LLM-assisted compaction.
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

- Deterministic first: structured outputs, failures, listings and smaller results must not be sent to the LLM.
- LLM summarization is optional and should be used only for long unstructured tool output.
- No dependencies.
- Do not hide failures: preserve errors, warnings, tracebacks, exit codes and failed status.
- Compact only when output exceeds `TOOL_SLIM_MAX_CHARS`.
- Always say compaction happened and how much was omitted.
- Prefer one boring file over abstractions.

## Local Hermes Test Context

The user tests this plugin with local Hermes, not only unit self-checks.

Useful local Hermes locations:

- Hermes root: `~/.hermes/`
- Active config: `~/.hermes/config.yaml`
- Plugin install target: `~/.hermes/plugins/tool-slim/`
- Environment overrides and secrets: `~/.hermes/.env` (do not print API keys)
- Main runtime log: `~/.hermes/logs/agent.log`
- Error log: `~/.hermes/logs/errors.log`
- GUI/TUI logs: `~/.hermes/logs/gui.log`, `~/.hermes/logs/tui_gateway_crash.log`
- Session/message database: `~/.hermes/state.db`

The installed Hermes config should include:

```yaml
plugins:
  enabled:
    - tool-slim
```

To inspect whether a session used `tool-slim`, search `~/.hermes/logs/agent.log` for the session id and nearby lines like:

```text
tool-slim: compacted tool=... raw_chars=... output_chars=... mode=... status=...
```

To inspect persisted compacted results for a session, query `~/.hermes/state.db` read-only with SQLite:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, role, tool_name, length(content), substr(replace(content, char(10), ' '), 1, 240) FROM messages WHERE session_id='SESSION_ID' ORDER BY id;"
sqlite3 ~/.hermes/state.db "SELECT id, tool_name, content FROM messages WHERE session_id='SESSION_ID' AND content LIKE '%[tool-slim compacted tool result]%' ORDER BY id;"
```

Recent real session used for tuning: `20260823_143121_22e2e7`.

Findings from that session:

- `tool-slim` saved a lot of context, but LLM compaction was too aggressive for structured listings.
- Future improvements should focus on the decision pipeline: when to use deterministic, hybrid or LLM compaction.
- Structured `read_file` results, file listings, command outputs with rows, and failures should stay deterministic.
- LLM should only summarize long unstructured text/log noise, with deterministic sections preserved first.

## Test Command

```bash
python3 -m compileall tool-slim
python3 tool-slim/__init__.py
```

The second command runs minimal self-checks.

## Next Agent First Task

Test in a live Hermes profile and tune defaults if useful details are missing from compacted outputs.
