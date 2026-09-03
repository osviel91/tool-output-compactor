# tool-output-compactor

Hermes plugin for compacting large tool results before they are sent back into the model context.

This is intentionally separate from `fast-brain`:

- `fast-brain` handles persistent memory, retrieval, consolidation and context recommendation.
- `tool-output-compactor` handles active context pressure caused by large tool outputs.

## Goal

Small-context agents often fail during long tasks because tool results keep accumulating in the active prompt. `tool-output-compactor` uses Hermes' `transform_tool_result` hook to replace oversized tool results with compact summaries before they re-enter conversation context.

```txt
Tool runs
  -> raw result
  -> tool-output-compactor transform_tool_result
  -> compact result goes back to Hermes/model context
```

This is active-context compaction, not memory. It does not decide what the agent should remember later; it only reduces the size of the next tool result the model sees.

## Where It Intervenes In Hermes

Hermes exposes a plugin hook named `transform_tool_result`. `tool-output-compactor` registers for it in `register()`:

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

The hook receives keyword arguments such as `tool_name`, `args`, `result`, `task_id`, `session_id`, `tool_call_id`, `turn_id`, `api_request_id`, `duration_ms`, `status`, `error_type` and `error_message`. `tool-output-compactor` returns:

- `None` when the result should remain unchanged.
- `str` when the result should be replaced by a compacted version.

Known Hermes limitation observed in local testing: some inline runtime tools, notably `session_search`, may emit `post_tool_call` without applying `transform_tool_result`. In that case `tool-output-compactor` is installed and working, but the inline tool result can still enter context uncompressed. This should be fixed in Hermes by applying the same transform path to inline tools.

## Compaction Flow

`tool-output-compactor` follows a deterministic-first, type-aware pipeline:

```txt
raw result
  -> stringify JSON/non-string result
  -> skip if len(result) <= TOOL_SLIM_MAX_CHARS
  -> parse JSON if possible
  -> classify (first matching extractor in the registry)
  -> extract type-aware signal   (pytest / git status / git log / ...)
  -> generic structured/text fallback when no extractor matches
  -> budget + render: prepend header, truncate to TOOL_SLIM_MAX_CHARS
```

Decision order:

- Failures stay deterministic: errors, stderr, tracebacks, non-zero exit codes and non-success statuses are preserved without LLM summarization.
- Structured tool results stay deterministic: JSON `content`, JSON `output`, `read_file`, `glob`, `grep` and structured listings keep shape/head/tail rather than free-form summaries.
- Medium results stay deterministic: below `TOOL_SLIM_LLM_MIN_CHARS`, LLM summarization is skipped even if enabled.
- Large unstructured results may use LLM compaction only when `TOOL_SLIM_LLM_ENABLED=true` and endpoint/model settings exist.
- If LLM compaction fails, deterministic output is used.

### Type-aware extraction

Classification uses only cheap deterministic signals: tool name, command/args and
output patterns. No LLM classification. Typed extractors return a `result_type:`
in the header and a deterministic decision reason (`pytest output`,
`git status output`, `git log output`). Current extractors (0.5.0):

- `PytestExtractor`: pytest summary counts, failing test nodes, error evidence lines.
- `GitStatusExtractor`: branch, staged / modified-deleted / untracked counts, first paths (porcelain and long formats).
- `GitLogExtractor`: commit counts + subjects (oneline and full log).
- Generic fallbacks: `SessionSearchExtractor` (history), `JsonExtractor`
  (keys-shape), `TextExtractor` (critical lines + head/tail).

Every compacted result starts with a header like:

```txt
[tool-output-compactor compacted tool result]
tool-output-compactor: compacted terminal · 68.3% reduction · saved 8200 chars · deterministic (structured text)
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
- Coding-assistant outputs and code/diff signals stay deterministic: `opencode` commands, fenced code blocks, unified diffs, JSON patch parts and `diff`/`patch` fields are preserved before prose is compacted.
- Concrete action facts are preserved separately from summaries: tool name, command-like args, paths, queries, exit codes, stderr, errors, status and approvals.
- The compacted result always says compaction happened and reports omitted size.
- The compacted result includes lightweight KPIs for Hermes itself: `saved_chars_estimate` and `reduction_pct_estimate`.
- Terminal background-start results are normalized into a short factual record (`event`, `session_id`, `pid`, `command`, `notify_on_complete`) so agents can see the process handle clearly without receiving behavior directives.
- Repeated terminal background starts with the same Hermes session, cwd/workdir and command are surfaced as `event: repeated_background_process_start` while keeping current and previous process ids visible.

Optional LLM compaction can be enabled with an OpenAI-compatible `/v1/chat/completions` endpoint. The LLM only sees the deterministic compacted body, not the full raw result. If the LLM call fails, times out or returns empty text, `tool-output-compactor` falls back to deterministic compaction.

### What It Preserves

- Tool identity and command-like arguments.
- Paths, queries and command strings when present in args.
- Status, approvals, exit codes, stderr and error fields from structured results.
- Lines containing error markers: `error`, `exception`, `traceback`, `failed`, `failure`, `warning`, `warn`, `denied`, `unauthorized`, `forbidden`, `timeout`, `exit_code`, `stderr`.
- Literal code/diff sections from coding assistants, including Markdown fences, `diff --git` blocks, unified hunks and OpenCode JSON patch parts when available.
- Head and tail of structured text, listings and file-like outputs.
- JSON shape and bounded previews for large objects/arrays.

### What It Does Not Do

- It does not archive raw output anywhere.
- It does not call fast-brain.
- It does not use embeddings.
- It does not compact results below `TOOL_SLIM_MAX_CHARS`.
- It does not guarantee coverage for Hermes tools that bypass `transform_tool_result`.

### OpenCode And Coding Assistants

OpenCode's normal formatted CLI output is Markdown-style text; its public docs do not define special code-fragment markers beyond ordinary code fences and diffs. When possible, call `opencode run --format json` or the OpenCode SDK/server so patch parts and diffs arrive as structured data. If output arrives as plain text, `tool-output-compactor` falls back to conservative code/diff detection and preserves those sections literally before summarizing surrounding prose.

## Non-Goals

- No database.
- No embeddings.
- No dependency on fast-brain.
- No raw-output archive.

## Background Process Relaunches

`tool-output-compactor` can make background-process handles clearer, but it should not manage processes. The plugin now normalizes terminal background starts into factual records such as:

```txt
[tool-output-compactor normalized background process start]
event: background_process_started
tool: terminal
session_id: proc_...
pid: 12345
notify_on_complete: True
command: python3 worker.py
```

If an agent repeatedly launches the same long-running command, the robust fix belongs in Hermes runtime: `terminal(background=true)` should reuse an existing live process with the same `session_key`, `task_id`, `cwd` and `command` instead of spawning another copy. `tool-output-compactor` only sees the result after the launch already happened, so it can preserve/surface the handle but cannot prevent duplicate processes. A local Hermes patch for this lives at `patches/hermes-background-dedup.patch` and can be applied from a Hermes checkout with `patch -p1 < /path/to/tool-output-compactor/patches/hermes-background-dedup.patch`.

For small-context agents, keep `TOOL_SLIM_DEDUP_MIN_CHARS=4000` or higher unless real traces prove otherwise. Lower thresholds can hide short success confirmations and background handles, which may make weaker models verify or relaunch instead of polling the existing process.

## Responsibility (Bounded Context)

`tool-output-compactor` exists for one purpose: **keep the model's active context bounded without losing information that matters**. Its only lever is deciding what content enters the context (`transform_tool_result`).

It is responsible for:

- Compacting oversized results while preserving critical facts (errors, paths, commands, decisions).
- Surfacing its own impact so the model and the human can see it (banner + KPIs).
- Not letting duplicate tool output re-enter context (`TOOL_SLIM_DEDUP`).

It is **not** responsible for:

- Stopping, managing or relaunching background tasks. That is the agent's job (via `process` with `notify_on_complete`) and Hermes' `tool_loop_guardrails`.
- Telling the agent what to do. The dedup stub is informational (`note: this exact output has been returned N times this session`) — it surfaces the pattern as a fact, never as a directive like "do not relaunch". Behavior decisions stay with the agent and Hermes.
- Fixing prompt design. A task that demands immediate confirmation of a long-running result (e.g. "only answer once track 022 shows SUCCESS") pushes an agent into relaunch loops; that is a prompt/verification design problem, not a plugin one.

When adding features, ask: "does this preserve or surface information for the model?" If the answer is behavioral steering or task management, it belongs elsewhere.

## Example Results

Small successful output, under budget:

```txt
unchanged
```

Large terminal/log output, deterministic:

```txt
[tool-output-compactor compacted tool result]
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

Environment variables keep the existing `TOOL_SLIM_*` prefix for compatibility.

```env
# Master switch. Keep true in normal use; set false to diagnose raw Hermes tool results.
TOOL_SLIM_ENABLED=true

# Result size that triggers compaction. This less-aggressive profile is tuned for
# remote Hermes long-running tasks where preserving medium outputs matters.
# Recommended: 4000-8000 for tight models, 12000 for long-task diagnostics.
TOOL_SLIM_MAX_CHARS=12000

# Skip compaction when the potential saving (raw_chars - max_chars) is below this.
# Prevents truncating useful output for a negligible gain (e.g. 4200 -> 4000).
TOOL_SLIM_MIN_SAVING_CHARS=2000

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

# Proactively replace exact duplicate tool results (same session, tool, content)
# with a small back-reference stub to avoid context bloat from repeated identical output.
TOOL_SLIM_DEDUP=true

# Dedup output style. stub (default) keeps a back-reference note for full traceability;
# minimal keeps only a factual marker (no "see above"/"seen N times") to avoid steering the model.
TOOL_SLIM_DEDUP_MODE=stub

# Minimum result size before duplicate detection applies.
# Keep this above cheap verification/control output: deduping small confirmations
# (dependency checks, directory checks, background-start handles) can make agents
# retry or relaunch instead of progressing. Use lower values only if repeated
# small outputs are proven to be the real context problem.
TOOL_SLIM_DEDUP_MIN_CHARS=4000

# How many unique results to remember per session for duplicate detection.
TOOL_SLIM_DEDUP_WINDOW=100

# How many background-start command fingerprints to remember for repeat detection.
TOOL_SLIM_BACKGROUND_WINDOW=50

# How many last assistant messages to preserve in a session_search result.
# Raise for "repeat the same process" tasks where the workflow steps matter.
TOOL_SLIM_SESSION_TAIL=8

# Max object/array entries shown per JSON level before omitting the rest.
# Recommended: 10-30. Raise for compact API payloads; keep low for huge arrays.
TOOL_SLIM_JSON_MAX_ITEMS=20

# Print concise compaction diagnostics to stderr in addition to INFO logs.
TOOL_SLIM_DEBUG=false

# Log every transform decision without changing tool output. Useful to separate
# plugin behavior from model/Hermes issues during live-session audits.
TOOL_SLIM_AUDIT=false

# Add a visible notice inside every compacted result. Useful during diagnostics.
TOOL_SLIM_NOTICE_IN_RESULT=true

# Optional OpenAI-compatible compressor. If unset or failing, deterministic compaction is used.
# Use LLM only for long unstructured output; structured content and failures stay deterministic.
TOOL_SLIM_LLM_ENABLED=true
TOOL_SLIM_LLM_BASE_URL=https://example.com/v1
TOOL_SLIM_LLM_MODEL=compressor
TOOL_SLIM_LLM_API_KEY=

# LLM request timeout. Recommended: 30-60s remote, 60-120s for cold local models.
TOOL_SLIM_LLM_TIMEOUT_SECONDS=60

# Minimum raw result size before LLM is allowed. Below this, deterministic is safer and cheaper.
# Recommended: 8000-20000. 12000 avoids summarizing medium structured results too early.
TOOL_SLIM_LLM_MIN_CHARS=12000

# Character budget for the LLM summary body, before preserved deterministic sections are added.
# Recommended: 1000-3000. Keep below TOOL_SLIM_MAX_CHARS.
TOOL_SLIM_LLM_MAX_CHARS=3000

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
cp -R tool-output-compactor ~/.hermes/plugins/tool-output-compactor
```

Then enable it in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - tool-output-compactor
```

Validate installation:

```bash
hermes plugins doctor tool-output-compactor
```

If testing from this repo, copy the current files into the local Hermes plugin directory after changes:

```bash
cp -R /path/to/tool-output-compactor ~/.hermes/plugins/tool-output-compactor
```

Restart the Hermes process/profile after updating plugin files so Python reloads the module.

## Live Diagnostics

Check whether Hermes loaded and used the plugin:

```bash
grep "tool-output-compactor" ~/.hermes/logs/agent.log
```

A successful compaction log looks like:

```txt
tool-output-compactor: compacted tool=skill_view raw_chars=4941 output_chars=3002 mode=deterministic status=ok
```

Enable decision audit logs when you need to prove whether `tool-output-compactor` was involved:

```env
TOOL_SLIM_AUDIT=true
```

Audit logs are informational only and do not change tool results. Example:

```txt
tool-output-compactor: decision tool=terminal action=unchanged reason=below max chars raw_chars=604 status=unknown
```

Inspect a session's persisted tool messages:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, role, tool_name, length(content), substr(replace(content, char(10), ' '), 1, 240) FROM messages WHERE session_id='SESSION_ID' ORDER BY id;"
```

Find compacted results in a session:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, tool_name, content FROM messages WHERE session_id='SESSION_ID' AND content LIKE '%[tool-output-compactor compacted tool result]%' ORDER BY id;"
```

Find large uncompressed tool results that may have bypassed the hook:

```bash
sqlite3 ~/.hermes/state.db "SELECT id, tool_name, length(content) FROM messages WHERE session_id='SESSION_ID' AND role='tool' AND length(content) > 4000 ORDER BY length(content) DESC;"
```

If a large result appears without `[tool-output-compactor compacted tool result]`, likely causes are:

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

The real-session report includes a `diagnosis` line to separate responsibilities:

- `plugin_acted`: persisted tool messages contain `tool-output-compactor` compacted output.
- `plugin_not_involved`: no compacted messages and no large uncompressed tool results.
- `model_tool_schema_error`: assistant tool calls put shell syntax such as `&&` into `workdir`, and Hermes blocked it.
- `hermes_loop_guard_warned`: Hermes emitted tool-loop warnings.
- `hermes_loop_guard_ignored`: repeated exact failure warnings continued several times.

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
- `large_uncompacted`: real session tool results above budget that did not contain a `tool-output-compactor` header; target `0`.
- `compacted_messages`: count of real tool messages compacted in a session.

Runtime KPIs visible to Hermes in each compacted tool result:

- `raw_chars`: original result size.
- `target_chars`: configured compaction budget.
- `saved_chars_estimate`: estimated characters removed from the active context.
- `reduction_pct_estimate`: estimated percentage reduction for that tool result.
- `mode`: `deterministic` or `hybrid`.
- `decision_reason`: why that mode was selected.

The benchmark intentionally checks boring invariants, not semantic intelligence. A good run means `tool-output-compactor` saved context while preserving known critical markers. It does not prove that every future task has enough detail; use real-session diagnostics for that.

## Development

Run the built-in checks and benchmark (from this repo root; the plugin installs
as a directory, so the same commands work from `~/.hermes/plugins/` with the
`tool-output-compactor/` prefix):

```bash
python3 -m compileall __init__.py benchmark.py coexistence_test.py
python3 __init__.py
python3 benchmark.py
python3 coexistence_test.py
```

The self-check includes deterministic compaction and a mocked LLM path. It does not call a real LLM endpoint.

`coexistence_test.py` is self-contained and always runs its 6 contract scenarios
against a local stub guard: it emulates Hermes' `model_tools.py` hook flow
(observers first on the raw result, then `transform_tool_result` callbacks in
plugin load order, first string wins). It proves compaction never hides
raw-result change from a progress guard, that guard recovery injection preempts
compaction (documented ordering), and that identical/changed/polling/
repeated-failure/noisy-with-semantic-change scenarios coexist. It never needs a
sibling checkout. `COEXISTENCE_REAL=1 python3 coexistence_test.py` additionally
runs the same scenarios against the real `hermes-progress-guard` source
(override its plugin dir with `PROGRESS_GUARD_PLUGIN_DIR`).

Compaction emits an `INFO` log through Python logging. Enable `TOOL_SLIM_DEBUG=true` to also write concise compaction lines to `stderr` while testing.

## Context Heuristic

`tool-output-compactor` only shrinks what is already there; it cannot fix a model whose real context window is unknown to Hermes. A long agentic session overflowed with `Prompt too long: 65986 tokens exceeds max context window of 65536 tokens` because Hermes had fallen back to a 256k assumption when its probe of the endpoint failed.

Heuristic to apply on local setups:

1. **Pin the real context window per model.** Discover it from the endpoint (`GET /v1/models`, key `max_model_len`/`context_length`) and set it explicitly so Hermes never guesses:
   - Root model: `model.context_length`.
   - Per-model override inside a custom provider: `custom_providers[].models.<id>.context_length` (this is the single source of truth used by startup, `/model` switch, `/info` and `get_model_context_length`).
2. **Expect probe failure on non-standard endpoints.** If `agent.log` shows `Could not detect context length ... defaulting to 256,000 tokens (probe-down)`, the window is wrong; compression triggers at `compression.threshold` of the *assumed* window, so an overestimated window means Hermes compresses too late or never.
3. **Compression is reactive, dedup is proactive.** Hermes only deduplicates identical tool results during context compression (`agent/context_compressor.py`, min 200 chars) — after the prompt has already grown. `tool-output-compactor` deduplicates at hook time (before the result re-enters context), which is earlier and complementary.
4. **A long task that re-queries the same tool output repeatedly is the biggest risk.** Repeated `process`/`terminal` results of the same content multiply unchanged; exact-duplicate detection collapses them to a stub.

## Current Hermes Gap

Local session testing found that `skill_view` was compacted correctly, while a `session_search` result of about 30k characters entered context uncompressed. The plugin was active; the issue was that `session_search` ran through Hermes' inline executor path, not the normal registry path where `model_tools.py` applies `transform_tool_result`.

Minimal fix belongs in Hermes, not in `tool-output-compactor`: route inline tool results through the same transform hook before appending them to the conversation. `tool-output-compactor` should stay boring and only implement the hook contract.

Separately, a long download session overflowed the model window because the endpoint's real limit was not configured in Hermes (see Context Heuristic above). That was a configuration issue, not a `tool-output-compactor` one.

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
