"""orchestra — production multi-agent orchestration + eval harness on Claude.

One human orchestrates N Claude agents, with guardrails and evals:

    orchestra.Fleet          fan out N agents in parallel, bounded concurrency
    orchestra.MessageBus      durable, receipted inter-agent messaging
    orchestra.CircuitBreaker  per-dependency auth/failure breaker (no infinite error loops)
    orchestra.best_of_n       spawn N model/temperature variants, judge, pick a winner
    orchestra.eval            definition-of-done gate + adversarial LLM-judge verification

Design goals:
  * Standalone. No external database, no vendor lock beyond the Anthropic SDK.
  * BYOK. Reads ANTHROPIC_API_KEY from the environment; nothing is hardcoded.
  * Capability-vs-risk aware. Every agent runs under a permission tier and an
    audit log; irreversible actions require an explicit human-approval gate.

See README.md for the architecture diagram and quickstart.
"""

from __future__ import annotations

from orchestra.bus import Message, MessageBus, MessageStatus
from orchestra.circuit_breaker import BreakerState, CircuitBreaker
from orchestra.client import ClaudeClient
from orchestra.fleet import Fleet, RunHandle, RunStatus
from orchestra.permissions import (
    AuditLog,
    HumanApprovalRequired,
    PermissionDenied,
    PermissionTier,
)
from orchestra.variants import Variant, VariantResult, best_of_n

__all__ = [
    "Fleet",
    "RunHandle",
    "RunStatus",
    "MessageBus",
    "Message",
    "MessageStatus",
    "CircuitBreaker",
    "BreakerState",
    "ClaudeClient",
    "PermissionTier",
    "AuditLog",
    "PermissionDenied",
    "HumanApprovalRequired",
    "Variant",
    "VariantResult",
    "best_of_n",
]

__version__ = "0.1.0"
