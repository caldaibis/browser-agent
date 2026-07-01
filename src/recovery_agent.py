"""Backward-compatible import shim for the renamed self-improvement agent."""
from .self_improvement_agent import *  # noqa: F401,F403
from .self_improvement_agent import (  # noqa: F401
    SelfImprovementResult as RecoveryResult,
    improve_after_apply as recover_after_apply,
    improve_exception as recover_exception,
    run_self_improvement as run_recovery,
)
