# Changelog

## 0.6.3

- Replace obvious binary/base64 blobs in compacted tool output with omission
  stubs before head/tail sampling. Preserves useful context such as APIC/image
  metadata, payload kind, character count and short hash while keeping raw
  image/audio bytes out of model context.

## 0.6.2

- Bound LLM-assisted compaction with a hard in-hook deadline so the deterministic
  body always wins within Hermes' hook-callback budget. The LLM summarizer call
  now runs in a worker thread with a real wall-clock deadline
  (`TOOL_SLIM_LLM_DEADLINE_SECONDS`, default 15s, safely under Hermes' 30s hook
  budget); on expiry the plugin returns the deterministic fallback instead of
  risking the whole hook being abandoned. Fixes a live defect where a slow LLM
  endpoint kept the hook past its 30s budget, leaving tool output completely
  uncompacted (`urllib` `urlopen(timeout=N)` is a per-socket idle bound, not a
  total wall-clock cap).

## 0.6.1

- Schema-once record-array compaction now also fires when the uniform record list is nested as a JSON string inside a tool's structured text field (the real terminal shape: `{output: "<json array string>", exit_code, error}`). The `output`/`content` text field is parsed, and if it holds a uniform record array it is compacted schema-once with `exit_code`/`error` preserved, instead of head/tail line truncation. Irregular or non-JSON text fields keep the existing head/tail rendering.

## 0.6.0

- Compact uniform arrays of same-shape JSON records with a schema-once layout (`JSON records: N rows` + `fields:` header + one `|`-separated row per record) instead of repeating field names per record. Deterministic, lossless, still budget-bounded; irregular/mixed/nested arrays keep the existing per-record expansion. Based on the "schema header once, records as rows" idea from the TOON article (The New Stack, Aug 2026).

## 0.5.0

- Introduce a `classify → extract → budget → render` pipeline with a typed extractor registry (`_Extractor` base; `PytestExtractor`, `GitStatusExtractor`, `GitLogExtractor`) ahead of generic structured/text fallbacks. REDIRECTION_PLAN.md phases 1 + 2.
- Typed extraction stays deterministic: pytest summary + failing tests + error evidence; git status (staged/modified/untracked counts + first paths); git log (oneline/normal summaries).
- Compacted headers from typed extractors include `result_type:`; decisions are `deterministic` with reasons like `pytest output` / `git status output` / `git log output`.
- Compaction INFO logs now include `result_type` and `dedup` fields (telemetry).
- Add `coexistence_test.py`: contract tests proving compaction cannot hide raw-result change from a progress guard and that guard recovery injection wins over compaction on the shared `transform_tool_result` hook. Self-contained by default (local stub guard, 6/6, no external checkout); `COEXISTENCE_REAL=1` additionally validates against a real `hermes-progress-guard` import (hermes-progress-guard evolves independently, so it must never be a hard test dependency).
- Existing generic paths, `TOOL_SLIM_*` env vars, dedup and LLM fallback unchanged (backward compatible).

## 0.4.1

- Keep structured tools (`read_file`, `glob`, `grep`, `session_search`) deterministic even when LLM compaction is enabled.
- Surface repeated terminal background starts by command/cwd while preserving dynamic process ids as facts.
- Preserve coding-assistant code/diff sections before compacting surrounding output.

## 0.4.0

- Rename the Hermes plugin from `tool-slim` to `tool-output-compactor`; existing `TOOL_SLIM_*` environment variables remain unchanged.

## 0.3.6

- Preserve action facts and status in duplicate-result stubs so deduped outputs keep enough metadata to continue the task.

## 0.3.5

- Replace directive `action_hint`s in duplicate-result stubs with a single informational `note` (output seen N times; see first occurrence). `tool-slim` surfaces the pattern but does not tell the agent what to do — behavior decisions stay with the agent and Hermes guardrails.

## 0.3.4

- Add a general `action_hint` to duplicate-result stubs for `process`, `terminal`, `read_file` and `search_files`, telling the agent not to repeat the same command/read. The background-process hint still fires first when relevant.

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
