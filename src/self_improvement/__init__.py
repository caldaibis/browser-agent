"""Self-improvement agent internals, split by responsibility.

`src.self_improvement_agent` remains the public facade (triggers, the
two-phase engine, commit/push/deploy policy); these modules hold its
separable leaves: prompt text, worktree lifecycle, cost estimation,
browser diagnostics tools, and context redaction.
"""
