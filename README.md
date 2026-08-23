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

This is active-context compaction, not memory. It does not decide what the agent should remember later; it only reduces the size of the next tool result the model sees.

## Where It Intervenes In Hermes

Hermes exposes a plugin hook named `transform_tool_result`. `tool-slim` registers for it in `register()`:

```python
ctx.register_hook("transform_tool_result", plugin.transform_tool_result)
```

For normal registry-dispatched tools, Hermes runs the hook after tool execution and after observational `post_tool_call` hooks, but before the result is appended back into the conversation and sent to the model.

```txt
model requests tool
  -> Hermes executes tool
  -> Hermes emits post_tool_call observers with original result
  -> Hermes invokes transform_tool_result
  -> first string returned by a plugin replaces the result
  -> replaced result is persisted and enters model context
```

The hook receives keyword arguments such as `tool_name`, `args`, `result`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `duration_ms`, `status`, `error_type` and `error_message`. `tool-slim` returns:

- `None` when the result should remain unchanged.
- `str` when the result should be replaced by a compacted version.

Known Hermes limitation observed in local testing: some inline runtime tools, notably `session_search`, may emit `post_tool_call` without applying `transform_tool_result`. In that case `tool-slim` is installed and working, but the inline tool result can still enter context uncompressed. This should be fixed in Hermes by applying the same transform path to inline tools.

## Compaction Flow

`tool-slim` follows a deterministic-first pipeline:

```txt
raw result
  -> stringify JSON/non-string result
  -> skip if len(result) <= TOOL_SLIM_MAX_CHARS
  -> parse JSON if possible
  -> extract action facts and critical lines
  -> choose deterministic or LLM-assisted mode
  -> prepend compaction header
  -> truncate to TOOL_SLIM_MAX_CHARS if needed
```

Decision order:

- Failures stay deterministic: errors, stderr, tracebacks, non-zero exit codes and non-success statuses are preserved without LLM summarization.
- Structured tool results stay deterministic: JSON `content`, JSON `output`, `read_file`, `glob`, `grep` and structured listings keep shape/head/tail rather than free-form summaries.
- Medium results stay deterministic: below `TOOL_SLIM_LLM_MIN_CHARS`, LLM summarization is skipped even if enabled.
- Large unstructured results may use LLM compaction only when `TOOL_SLIM_LLM_ENABLED=true` and endpoint/model settings exist.
- If LLM compaction fails, deterministic output is used.

Every compacted result starts with a header like:

```txt
[tool-slim compacted tool result]
tool-slim: compacted terminal · 68.3% reduction · saved 8200 chars · deterministic (structured text)
tool: terminal
mode: deterministic
decision_reason: structured text
raw_chars: 12000
target_chars: 4000
omitted_chars_estimate: 8200
saved_chars_estimate: 8200
reduction_pct_estimate: 68.3
status: ok
duration_ms: 1234
```

The second line is a one-line banner visible to the human in the tool card, reported every time the plugin compacts a result.

## Behavior

By default, compaction is deterministic and dependency-free:

- Small results are left unchanged.
- Large plain text keeps important lines, head and tail.
- Large JSON keeps shape, keys and bounded item previews.
- Errors, warnings, tracebacks, stderr and exit codes are prioritized, including inside JSON string fields like terminal `output`.
- Concrete action facts are preserved separately from summaries: tool name, command-like args, paths, queries, exit codes, stderr, errors, status and approvals.
- The compacted result always says compaction happened and reports omitted size.
- The compacted result includes lightweight KPIs for Hermes itself: `saved_chars_estimate` and `reduction_pct_estimate`.

Optional LLM compaction can be enabled with an OpenAI-compatible `/v1/chat/completions` endpoint. The LLM only sees the deterministic compacted body, not the full raw result. If the LLM call fails, times out or returns empty text, `tool-slim` falls back to deterministic compaction.

### What It Preserves

- Tool identity and command-like arguments.
- Paths, queries and command strings when present in args.
- Status, approvals, exit codes, stderr and error fields from structured results.
- Lines containing error markers: `error`, `exception`, `traceback`, `failed`, `failure`, `warning`, `warn`, `denied`, `unauthorized`, `forbidden`, `timeout`, `exit_code`, `stderr`.
- Head and tail of structured text, listings and file-like outputs.
- JSON shape and bounded previews for large objects/arrays.

### What It Does Not Do

- It does not archive raw output anywhere.
- It does not call fast-brain.
- It does not use embeddings.
- It does not compact results below `TOOL_SLIM_MAX_CHARS`.
- It does not guarantee coverage for Hermes tools that bypass `transform_tool_result`.

## Non-Goals

- No database.
- No embeddings.
- No dependency on fast-brain.
- No raw-output archive.

## Example Results

Small successful output, under budget:

```txt
unchanged
```

Large terminal/log output, deterministic:

```txt
[tool-slim compacted tool result]
tool: terminal
mode: deterministic
decision_reason: critical lines present
raw_chars: 18000
target_chars: 4000
omitted_chars_estimate: 14500

Preserved action facts:
tool: terminal
args.command: pytest
exit_code: 1

Preserved critical lines:
FAILED tests/test_app.py::test_login
AssertionError: expected 200, got 500

---

Important lines:
...

Head:
...

Tail:
...
```

Structured file/listing output:

```txt
JSON object with 6 keys: content, total_lines, file_size, truncated, is_binary, is_image
- total_lines: 120
- file_size: 7200
- truncated: false
- content:
Head lines 1-30:
...

... 80 lines omitted ...

Tail lines 111-120:
...
```

## Environment

```env
# Master switch. Keep true in normal use; set false to diagnose raw Hermes tool results.
TOOL_SLIM_ENABLED=true

# Result size that triggers compaction. 4000 is conservative for small-context agents.
# Recommended: 4000-8000 for small models, 12000-20000 for larger local models.
TOOL_SLIM_MAX_CHARS=4000

# Skip compaction when the potential saving (raw_chars - max_chars) is below this.
# Prevents truncating useful output for a negligible gain (e.g. 4200 -> 4000).
TOOL_SLIM_MIN_SAVING_CHARS=500

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

# How many last assistant messages to preserve in a session_search result.
# Raise for "repeat the same process" tasks where the workflow steps matter.
TOOL_SLIM_SESSION_TAIL=8

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

If testing from this repo, copy the current files into the local Hermes plugin directory after changes:

```bash
cp -R /path/to/tool-slim ~/.hermes/plugins/tool-slim
```

Restart the Hermes process/profile after updating plugin files so Python reloads the module.

## Live Diagnostics

Check whether Hermes loaded and used the plugin:

```bash
grep "tool-slim" ~/.hermes/logs/agent.log
```

A successful compaction log looks like:

```txt
tool-slim: compacted tool=skill_view raw_chars=4941 output_chars=3002 mode=deterministic status=ok
```

Inspect a session's persisted tool messages:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, role, tool_name, length(content), substr(replace(content, char(10), ' '), 1, 240) FROM messages WHERE session_id='SESSION_ID' ORDER BY id;"
```

Find compacted results in a session:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, tool_name, content FROM messages WHERE session_id='SESSION_ID' AND content LIKE '%[tool-slim compacted tool result]%' ORDER BY id;"
```

Find large uncompressed tool results that may have bypassed the hook:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, tool_name, length(content) FROM messages WHERE session_id='SESSION_ID' AND role='tool' AND length(content) > 4000 ORDER BY length(content) DESC;"
```

If a large result appears without `[tool-slim compacted tool result]`, likely causes are:

- The result is from an inline Hermes runtime tool that bypasses `transform_tool_result`.
- The plugin was not enabled or Hermes was not restarted after installation.
- `TOOL_SLIM_ENABLED=false` is set in the environment.
- `TOOL_SLIM_MAX_CHARS` is higher than the result size.

## Benchmark And KPIs

Use `benchmark.py` to make compaction quality measurable before and after changes. It has no dependencies and runs in two modes.

Synthetic fixtures:

```bash
python3 benchmark.py
```

Real Hermes session diagnostics:

```bash
python3 benchmark.py --session-id SESSION_ID
```

Machine-readable output for dashboards or for giving Hermes its own KPIs:

```bash
python3 benchmark.py --json
python3 benchmark.py --session-id SESSION_ID --json
```

Primary KPIs:

- `reduction_pct`: how much active context was saved.
- `saved_chars`: raw characters avoided in model context.
- `critical_marker_failures`: required facts/errors lost by synthetic benchmark cases; target `0`.
- `over_budget`: compacted outputs still above `TOOL_SLIM_MAX_CHARS`; target `0`.
- `large_uncompacted`: real session tool results above budget that did not contain a `tool-slim` header; target `0`.
- `compacted_messages`: count of real tool messages compacted in a session.

Runtime KPIs visible to Hermes in each compacted tool result:

- `raw_chars`: original result size.
- `target_chars`: configured compaction budget.
- `saved_chars_estimate`: estimated characters removed from the active context.
- `reduction_pct_estimate`: estimated percentage reduction for that tool result.
- `mode`: `deterministic` or `hybrid`.
- `decision_reason`: why that mode was selected.

The benchmark intentionally checks boring invariants, not semantic intelligence. A good run means `tool-slim` saved context while preserving known critical markers. It does not prove that every future task has enough detail; use real-session diagnostics for that.

## Development

Run the built-in checks and benchmark:

```bash
python3 -m compileall tool-slim
python3 tool-slim/__init__.py
python3 tool-slim/benchmark.py
```

The self-check includes deterministic compaction and a mocked LLM path. It does not call a real LLM endpoint.

Compaction emits an `INFO` log through Python logging. Enable `TOOL_SLIM_DEBUG=true` to also write concise compaction lines to `stderr` while testing.

## Current Hermes Gap

Local session testing found that `skill_view` was compacted correctly, while a `session_search` result of about 30k characters entered context uncompressed. The plugin was active; the issue was that `session_search` ran through Hermes' inline executor path, not the normal registry path where `model_tools.py` applies `transform_tool_result`.

Minimal fix belongs in Hermes, not in `tool-slim`: route inline tool results through the same transform hook before appending them to the conversation. `tool-slim` should stay boring and only implement the hook contract.

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
