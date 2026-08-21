# Changelog

## 0.2.0

- Add optional OpenAI-compatible LLM compaction with deterministic fallback.
- Preserve deterministic critical lines alongside LLM summaries.
- Add optional debug logging with `TOOL_SLIM_DEBUG`.
- Declare Hermes `provides_hooks` capability.

## 0.1.0

- Initial deterministic Hermes `transform_tool_result` compactor.
- Compact large text with important lines, head and tail.
- Compact large JSON by shape and bounded previews.
