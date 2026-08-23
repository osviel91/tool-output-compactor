# Changelog

## 0.3.3

- Add an `action_hint` to duplicate-result stubs for background processes so the agent does not relaunch an already-running process.

## 0.3.2

- Add proactive exact-duplicate detection: identical tool results (same session, tool and content) are replaced with a small back-reference stub instead of re-entering context (`TOOL_SLIM_DEDUP`, `TOOL_SLIM_DEDUP_MIN_CHARS`, `TOOL_SLIM_DEDUP_WINDOW`).

## 0.3.1

- Add a one-line KPI banner to every compacted tool result (`tool-slim: compacted <tool> · <pct>% reduction · saved <n> chars · <mode>`), always visible in the tool card.

## 0.3.0

- `session_search` now preserves `key_actions` (commands, scripts, file paths from tool calls) so agents can reconstruct the workflow without re-searching sessions.
- Make the number of preserved `last_assistant_messages` configurable via `TOOL_SLIM_SESSION_TAIL` (default 8).

## 0.2.9

- Skip compaction when the potential saving is too small to justify the overhead (new `TOOL_SLIM_MIN_SAVING_CHARS`, default 500). Avoids truncating useful output for marginal savings.
- Ignore `exit_code_meaning` like "No matches found (not an error)" when detecting error lines inside `session_search` history.

## 0.2.8

- Ignore already-compacted tool results and `error: null` when detecting error lines inside `session_search` history.

## 0.2.7

- Report final KPIs (`saved_chars_estimate`, `reduction_pct_estimate`, `omitted_chars_estimate`) measured against the persisted output after header and budget truncation.
- Add a dedicated deterministic policy for `session_search`: preserve session metadata, first user message, last assistant messages and real error lines, and show how many messages were kept.
- Keep `session_search` deterministic in the decision pipeline (never sent to the LLM).
- Stop treating `exit_code: 0` / `returncode: 0` as critical lines.

## 0.2.6

- Add `benchmark.py` for dependency-free synthetic compaction checks and real Hermes session diagnostics.
- Document KPIs for reduction, saved characters, critical marker preservation, budget violations and large uncompressed tool results.
- Include `saved_chars_estimate` and `reduction_pct_estimate` in compacted tool results so Hermes can see per-result impact in context.

## 0.2.5

- Add an explicit compaction decision pipeline so structured results, failures and smaller outputs stay deterministic.
- Preserve structured `content` and `output` fields by line head/tail instead of sending useful listings to the LLM.
- Add `TOOL_SLIM_LLM_MIN_CHARS`, `TOOL_SLIM_HEAD_LINES` and `TOOL_SLIM_TAIL_LINES`.

## 0.2.4

- Preserve action facts separately from compacted noise: tool name, command-like args, paths, queries, exit codes, stderr, errors, status and approvals.
- Keep preserved action facts outside the LLM summary so concrete tool actions are not lost or paraphrased away.

## 0.2.3

- Preserve critical lines inside JSON string fields such as terminal `output`.
- Preserve non-empty critical JSON fields such as `stderr`, `error`, `traceback` and non-zero exit codes before compacted previews.

## 0.2.2

- Emit an INFO log whenever a tool result is compacted, including tool, sizes, mode and status.
- Add `TOOL_SLIM_NOTICE_IN_RESULT` for an optional in-result notice that compaction happened.

## 0.2.1

- Raise default LLM compaction timeout to 30 seconds for cold local model loads.
- Document 60 seconds as a practical local Hermes setting for slow first responses.

## 0.2.0

- Add optional OpenAI-compatible LLM compaction with deterministic fallback.
- Preserve deterministic critical lines alongside LLM summaries.
- Add optional debug logging with `TOOL_SLIM_DEBUG`.
- Declare Hermes `provides_hooks` capability.

## 0.1.0

- Initial deterministic Hermes `transform_tool_result` compactor.
- Compact large text with important lines, head and tail.
- Compact large JSON by shape and bounded previews.
