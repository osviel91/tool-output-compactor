# Project Plan

Permanent planning for `tool-output-compactor`. Keep this file current; this is
the single source of truth for project direction.

## Direction

- Preserve or surface information for the model before tool results re-enter
  context.
- Stay deterministic first: structured outputs, failures, listings and smaller
  results must not go through LLM summarization.
- Use the optional LLM path only for long unstructured text/log noise, with
  deterministic critical sections preserved first.
- Do not duplicate Hermes responsibilities: context lifecycle, window management,
  hard tool-output limits, historical pruning, compression or loop guardrails.
- Do not steer agent behavior. Dedup stubs and compaction headers are factual,
  not directives.
- Historical audit conclusion: Hermes owns hard tool-output limits, context
  compression, historical pruning and loop guardrails. This plugin owns only
  pre-context tool-result compaction via `transform_tool_result`.

## Current Baseline

- Typed extractor pipeline is implemented in `__init__.py` with generic
  structured/text fallbacks.
- Specialized extractors exist for pytest output, git status and git log.
- Coexistence coverage exists in `coexistence_test.py`: progress-guard sees raw
  results before compaction, and guard recovery preempts compaction when both
  return strings.
- Schema-once record-array compaction works for direct JSON arrays and JSON
  arrays embedded in structured text fields such as terminal `output`.
- LLM compaction is bounded by a hard in-hook deadline; deterministic fallback
  wins on slow endpoints.
- Obvious binary/base64 blobs are replaced with omission stubs before sampling.

## Hook And Coexistence Notes

- Hermes passes the same raw tool result to every `transform_tool_result` hook;
  the first returned string wins, and `None` leaves the result unchanged.
- `post_tool_call` runs before `transform_tool_result`, so progress-guard sees
  raw results before compaction.
- If progress-guard injects recovery text and this plugin would compact the same
  result, first-string-wins means recovery may preempt compaction. That is
  covered by `coexistence_test.py` and is acceptable because recovery is rare.
- Hook callbacks are timeout-bounded by Hermes; optional LLM compaction must stay
  under that budget and fail open to deterministic output.

## Next Work

- Tune defaults only when a real Hermes session shows a useful detail missing
  from compacted output.
- Add a new extractor only when a real workload outgrows the generic JSON/text
  paths. Candidate areas: Docker/build logs, npm/pip, mypy, ESLint, coverage.
- If a new blob format leaks into context, add the smallest scrubber that
  preserves surrounding metadata and omission facts.
- Consider per-section token budgeting only after a real failure shows character
  budgets are insufficient.
- Keep the single-file plugin until the extractor registry becomes harder to
  maintain than a small split. Do not split for neatness alone.

## Release Hygiene

- Every user-visible change must update `CHANGELOG.md` and bump the plugin
  version in `VERSION`, `plugin.yaml` and `__init__.py`.
- Keep README version mentions in sync when they name current behavior.
- Before commit, run:

```bash
python3 -m compileall __init__.py benchmark.py coexistence_test.py
python3 __init__.py
python3 benchmark.py
python3 coexistence_test.py
```
