"""Browser agent package — the apply loop, split by responsibility.

loop.py holds `_run`/`run_agent` (the tool-calling loop over agent-browser
MCP); guards.py the deterministic in-loop guards; result.py the
AgentResult/outcome contract; transport.py the MCP/OpenAI plumbing, Logger,
and teardown watchdog. This facade re-exports the public + test-visible
surface so `from src.browser_agent import ...` keeps working; tests that
patch loop internals (AsyncOpenAI, stdio_client) patch `src.browser_agent.loop`.
"""
from .guards import (
    PRUNE_MIN_CHARS as PRUNE_MIN_CHARS,
    STALE_DUMP_STUB as STALE_DUMP_STUB,
    TOOL_RESULT_MAX_CHARS as TOOL_RESULT_MAX_CHARS,
    _clamp_tool_result as _clamp_tool_result,
    _is_payment_url as _is_payment_url,
    _prune_stale_page_dumps as _prune_stale_page_dumps,
    _recent_form_activity as _recent_form_activity,
    _should_nudge_snapshot_overuse as _should_nudge_snapshot_overuse,
    _trailing_cycle_repeats as _trailing_cycle_repeats,
)
from .loop import run_agent as run_agent
from .result import (
    NO_CREDIT_RC as NO_CREDIT_RC,
    VALID_OUTCOMES as VALID_OUTCOMES,
    AgentResult as AgentResult,
    _extract_outcome as _extract_outcome,
    _parse_outcome as _parse_outcome,
)
from .transport import Logger as Logger
