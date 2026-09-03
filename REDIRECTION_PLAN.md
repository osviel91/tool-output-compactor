# REDIRECTION_PLAN — tool-output-compactor → type-aware extraction

Audit-first deliverable. Grounds the redirection of `tool-output-compactor` from
generic truncation/compaction toward type-aware, information-preserving tool-result
compaction. Findings verified against the local Hermes source tree and the
sibling `hermes-progress-guard` plugin; do not treat the original engineering
handoff's assumptions as ground truth where this document corrects them.

## 1. Current architecture

- Standalone git repo: `github.com/osviel91/tool-output-compactor` (independent;
  the AGENTS.md note about living inside `fast-brain` is obsolete — see §14).
- Single-file plugin: `__init__.py` (1291 lines), class `ToolOutputCompactorPlugin`
  + `register()`. Version 0.4.1, kept in sync across `VERSION`, `plugin.yaml`,
  `__version__`.
- Env-config via `TOOL_SLIM_*`; install = `cp -R` plugin dir with `plugin.yaml`
  into `~/.hermes/plugins/`, enabled via `plugins.enabled`.
- Self-check `_demo()` in `__main__`; synthetic benchmarks + real-session
  diagnostics in `benchmark.py`. Docs: README.md, roadmap PLAN.md.
- Design rule (AGENTS.md): "Prefer one boring file over abstractions."

## 2. Current compaction pipeline (verified in `__init__.py`)

`transform_tool_result(...)` (env-gated by `TOOL_SLIM_ENABLED`) → duplicate
`tool_call_id` guard → `_background_start_summary` (factual bg-start record +
repeat detection by session/cwd/command) → `_dedup` (md5-12hex per session+tool;
stub if ≥ `TOOL_SLIM_DEDUP_MIN_CHARS`; modes `stub`/`minimal`; informational
note only, never a directive) → skip if `len <= TOOL_SLIM_MAX_CHARS` or saving <
`TOOL_SLIM_MIN_SAVING_CHARS` → `_choose_compaction_mode`
(`session_search` → failures → `coding_assistant_output` →
`structured_tool_result` → `_looks_structured` → too-small-for-llm →
`llm_available` → `deterministic` | `hybrid`) → `_compact`
(`_compact_session_search` / `_compact_structured_text_result` / `_compact_json`
keys-shape / `_compact_text` critical lines + head/tail + code/diff snippets) →
`_assemble_compacted` (marker + banner + KPIs: `raw_chars`, `saved_chars_estimate`,
`reduction_pct_estimate`, `mode`, `decision_reason`).

## 3. Overlap with current Hermes (verified, head 1cb3ab6173)

| Capability | Owner | Status |
|---|---|---|
| Hard tool-output limits | Hermes `tools/tool_output_limits.py` (config `tool_output`, defaults 50KB / 2000 lines / 2000 chars/line; truncation at tool level BEFORE hooks) | Native — do not duplicate |
| Context compression / window mgmt | Hermes `agent/context_compressor.py`, `conversation_compression.py`, `context_engine.py` | Native — do not duplicate |
| Historical result pruning | Hermes context/compression layers | Native — do not duplicate |
| Loop guardrails | Hermes `agent/tool_guardrails.py`, `repetition_guard.py`, `empty_response_guard.py` | Native — do not duplicate |
| Reactive dedup during compression (min 200 chars) | Hermes `agent/context_compressor.py` | Native but late; keep plugin proactive hook-time dedup |

Implication: native 50KB tool-level truncation shrinks what reaches the hook.
Plugin value shifts to mid-size structured outputs + hook-time dedup + signal
extraction, not raw size cutting.

## 4. Hermes hook semantics (verified)

- `transform_tool_result`: all registered callbacks receive the SAME original raw
  `result` kwarg; the first non-None `str` return wins; the replacement is applied
  only after `invoke_hook` returns. Non-string/None returns ignored; exceptions
  fail open.
- Callback order = registration order = plugin load order = deterministic
  topological order (graphlib; ties alphabetical). No plugin priority system.
- The hook is timeout-bounded (`_HOOK_TIMEOUT_BOUNDED_HOOKS`, default 30s) and
  fail-open: on timeout the callback thread is abandoned and the callback
  suppressed for 60s. Only `pre_tool_call` fails closed.
- `post_tool_call` fires before `transform_tool_result`, also on the raw result.

## 5. Interaction/order with hermes-progress-guard (CORRECTED — supersedes the handoff §12)

- progress-guard performs all real detection (action/result fingerprinting,
  material-progress assessment, POLL state) in `post_tool_call` on the RAW
  result — that runs before compaction and is untouched by it. Its own
  `transform_tool_result` hook only injects recovery/thinking-recovery messages
  and returns `None` otherwise.
- **Conclusion: compaction cannot hide result changes from progress-guard.** The
  original handoff's concern (§12) is misplaced at the transform-hook level.
- Real risk (needs a contract test): when progress-guard injects a recovery
  string AND the compactor would compact on the same result, only the first
  registration-order return is honored and the other is dropped silently. Load
  order is alphabetical: `progress-guard` < `tool-output-compactor`, so on a
  recovery event progress-guard wins and the raw result + recovery message
  re-enter context uncompacted. Acceptable (recovery is rare) but must be
  documented and observable, not relied on as a stable contract.
- No cross-plugin metadata channel exists through the hook kwargs; none is needed.

## 6. Components to keep unchanged

- `_dedup` (proactive, informational stub, `TOOL_SLIM_DEDUP_*`) and the duplicate
  `tool_call_id` guard.
- `_background_start_summary` + background repeat detection.
- Deterministic compaction: critical lines / `IMPORTANT_MARKERS`, JSON keys-shape,
  head/tail, code/diff snippets, `_compact_session_search`.
- Banner + KPIs; `_demo()` self-check; `benchmark.py` targets
  (`critical_marker_failures=0`, `over_budget=0`, 4/4 synthetic cases).
- All `TOOL_SLIM_*` env vars (backward compatibility) + flat install layout.
- LLM fallback as an optional exception path with deterministic fallback on any error.

## 7. Components to refactor

- Type dispatch inside `_compact` + `_choose_compaction_mode` → a thin
  `classify → extract → budget → render` pipeline; current functions become the
  **generic fallback extractor** so behavior is preserved.
- Versioning (stays in 3 places; consider a tiny consistency check in CI/self-check).

## 8. Components to eventually deprecate (only with evidence)

- `LEGACY_MARKER '[tool-slim compacted tool result]'` recognition: keep for
  reading old state.db rows; stop emitting once no live sessions rely on it.
- Character-only decision thresholds as primary logic (superseded by token
  estimates; chars remain as hard safety bounds).

## 9. Proposed internal architecture (MINIMAL, per repo design rule)

Do NOT split into the handoff's 12-module package now. Stay in the existing
single `__init__.py`; introduce a second boring module
(`plugins/.../extractors.py` style, or a plain `extractors.py` beside
`__init__.py`) only once ≥2 typed extractors exist and the generic fallback
stays clean. Introduce only what is proven necessary:

- `CompactResult` dataclass only if ≥2 extractors share rendering; otherwise keep
  returning text directly.
- Registry of small `extract_tool_result(tool_name, args, result, ...) -> str|None`
  functions.
- Detection = cheap deterministic signals only (tool name / command / args /
  output patterns / JSON shape). No LLM classification, no regex cascade.
- Phase-1 extractors: pytest-family and git (status/diff/log). grep/rg and JSON
  already ride the existing generic structured paths — extract first, split later.

## 10. Migration / backward-compatibility risks

- **Hook callback timeout (30s default) vs `TOOL_SLIM_LLM_TIMEOUT_SECONDS`**
  (default 30; README suggests 60-120): a cold LLM compaction inside the hook
  risks thread abandonment + 60s suppression. Mitigation: keep effective
  in-hook timeout comfortably under the hook budget or align the README;
  measure `llm_fallback_rate` with the timeout in mind.
- Alphabetical ordering is not a stable public contract across Hermes releases →
  contract tests must assert both plugins stay functional, never which one wins.
- Internal renames must keep every `_demo()` assertion green.
- New `TOOL_SLIM_*` vars must default to current behavior (opt-in, e.g.
  `TOOL_SLIM_TYPE_AWARE`).
- Nothing is removed until a replacement is tested (no big-bang rewrite).

## 11. Phased implementation plan (collapsed from the handoff's 7 phases)

**Phase 1 — Pipeline extraction, no behavior change.** Refactor `_compact`
dispatch into `classify → extract(registry) → budget → render`; register current
heuristics as the generic extractor/fallback. All existing self-checks and
benchmarks stay green. Document Hermes boundaries + corrected progress-guard
interaction in README. Housekeeping: fix the obsolete fast-brain note in
AGENTS.md and check CHANGELOG/PLAN.md/README against the standalone repo state.

**Phase 2 — First specialized extractors.** `pytest` (summary + FAILED lines +
exit code + traceback head) and `git` (status/diff/log → changed files, hunks,
refs, counts). Fixture tests with realistic large outputs; measure input chars →
output chars → preserved signal; assert `decision_reason`, no critical marker
dropped, benchmark 4/4.

**Phase 3 — Coexistence contract tests + telemetry.** Cross-plugin tests (both
installed under ~/.hermes): identical large result, changed build errors, polling,
repeated failure, large noisy output with a small semantic change — plus the
recovery-vs-compaction ordering case. Add per-message KPIs
(`extractor_selected`, `dedup_hit`) and log-side counters
(`llm_fallback_count`, `extractor_hit_rate`, `dedup_hit_rate`).

Future (only if real workloads justify): grep/JSON extractors, Docker/compiler/
npm/pip/ESLint/mypy/coverage; per-section token budget (handoff §10 — speculative,
deferred until a real failure demands it).

## 12. Proposed tests

- Keep: `python3 tool-output-compactor/__init__.py` self-check; `benchmark.py`
  4/4 with `critical_marker_failures=0`, `over_budget=0`.
- Add: pytest + git extractor fixture tests with realistic large outputs;
  cross-plugin integration fixtures for the six Phase-3 scenarios; an assertion
  that progress-guard fingerprinting at `post_tool_call` is unaffected by
  compaction (raw-result visibility).

## 13. Unresolved questions

1. Which real pytest/git workloads dominate observed sessions? (Pull from
   `~/.hermes/state.db` / `benchmark.py` diagnostics before finalizing extractor
   field sets.)
2. Split into a second `extractors.py` module when? Rule of thumb: when ≥2 typed
   extractors exist and the generic fallback stays clean.
3. Is Hermes hook-callback timeout config already tuned in `~/.hermes/config.yaml`?
   Confirm before shipping LLM-in-hook defaults.

## 14. Resolved questions (out of scope of the original handoff)

- Repository status: `tool-output-compactor` is already an independent git repo
  (`github.com/osviel91/tool-output-compactor`), NOT inside fast-brain. The
  AGENTS.md note claiming otherwise is stale and should be corrected in Phase 1.
  No extraction work needed.
