# Reasoning truncation = silent stall

(from AGENTS.md, verbatim; see git history for context)

**Reasoning truncation = silent stall.** Reasoning models can emit hidden
reasoning tokens (counted against the completion budget) before any
content/tool_call. Over a big page snapshot that reasoning can exhaust the
completion cap mid-thought, so the API returns `finish_reason="length"` with
empty content AND no tool_calls. The loop reads that as "the model stopped",
burns its 2 nudges, and bails after a few seconds with no real attempt. This
sank kamernet submission #25 (29-06-2026). Fixes in `browser_agent.py`:
(1) thinking is **disabled by default** via
`extra_body={"thinking":{"type":"disabled"}}` — form-filling needs no heavy reasoning. Re-enable with
`APPLY_REASONING_EFFORT`.
(2) explicit `max_tokens` headroom; (3) truncated-empty turns (`finish_reason=
length`) are retried, not counted as a conclusion; (4) per-turn log of
`finish_reason` + `completion/reasoning_tokens`. NB: the reasoning *text* stays
hidden — only the token *count* is exposed (`usage.completion_tokens_details`).
Also: the transcript's tool-arg log no longer clamps urls/refs to 60 chars (that
clamp made a full URL look truncated and masked the real cause).
