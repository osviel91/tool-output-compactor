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
- `benchmark.py`: dependency-free synthetic benchmark plus real Hermes session diagnostics.
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
- Skip compaction when the potential saving is too small to justify overhead (`TOOL_SLIM_MIN_SAVING_CHARS`).
- Replace exact duplicate tool results with a stub before they re-enter context (`TOOL_SLIM_DEDUP`).
- Always say compaction happened and how much was omitted.
- Include runtime KPIs in compacted results so Hermes can see impact: `saved_chars_estimate`, `reduction_pct_estimate` and a one-line banner (`tool-slim: compacted <tool> · <pct>% reduction · saved <n> chars · <mode>`).
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

Compacted tool messages should also contain header KPIs visible to Hermes in-context:

```text
saved_chars_estimate: ...
reduction_pct_estimate: ...
decision_reason: ...
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

## Context Heuristic

A long download session overflowed the model window (`Prompt too long: 65986 > 65536 tokens`) because Hermes did not know the real limit: its probe of the custom endpoint failed and it assumed 256k, so compression (at `compression.threshold` of the assumed window) fired too late or never.

When diagnosing "session ended without finishing" or "context overflow":

1. Check `agent.log` for `defaulting to 256,000 tokens (probe-down)` — that means the real window is unknown.
2. Query the endpoint's real limits: `GET <base_url>/models` → `max_model_len` per model.
3. Pin them in `~/.hermes/config.yaml`:
   - `model.context_length` for the active model.
   - `custom_providers[].models.<id>.context_length` for each model of a custom provider (single source of truth for startup, `/model`, `/info`).
4. Remember: Hermes deduplicates identical tool results only during compression (reactively, `agent/context_compressor.py`, min 200 chars). `tool-slim` deduplicates at hook time (proactively, before the result re-enters context) via `TOOL_SLIM_DEDUP`.

Local omlx model windows (verified against `/v1/models`): `main`=65536, `advanced-vision`=65536, `assistive`/`reasoning`/`fast`/`operator`=131072, `compressor`/`embedding`/`whisper`=32768.

## Test Command

```bash
python3 -m compileall tool-slim
python3 tool-slim/__init__.py
python3 tool-slim/benchmark.py
```

The second command runs minimal self-checks.
The benchmark should pass `4/4` synthetic cases with `critical_marker_failures=0` and `over_budget=0` before tuning defaults.

## Next Agent First Task

Test in a live Hermes profile and tune defaults if useful details are missing from compacted outputs.
