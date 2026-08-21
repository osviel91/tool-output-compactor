# Changelog

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
