"""orchestra.permissions — capability-vs-risk guardrails for agent actions.

Multi-agent systems are dangerous precisely because they act. This module makes
*capability* and *risk* first-class:

  * Every action an agent takes is classified into a :class:`PermissionTier`.
  * Each agent is granted a maximum tier. Anything above it is denied.
  * Irreversible actions (the top tier) require an explicit human-approval
    callback — the system will not proceed on its own.
  * Every allow / deny / approval is written to a tamper-evident
    :class:`AuditLog` (append-only, hash-chained), so there is always a record
    of who did what and under whose authority.

This mirrors the "graduated autonomy + human-in-the-loop for irreversible
actions" posture: agents get the least privilege that lets them do the job, and
the blast radius of a misbehaving agent is bounded by policy, not by hope.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable


class PermissionTier(IntEnum):
    """Ordered by blast radius. Higher = more dangerous."""

    READ = 1  # read-only: fetch, query, inspect. Safe to run unattended.
    WRITE = 2  # local writes: files, scratch state. Reversible.
    EXTERNAL = 3  # outbound side effects: send email/message, call a paid API.
    IRREVERSIBLE = 4  # cannot be undone: deploy to prod, delete data, spend money.

    @classmethod
    def parse(cls, value: "str | int | PermissionTier") -> "PermissionTier":
        if isinstance(value, PermissionTier):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).strip().upper()]


class PermissionDenied(Exception):
    """Raised when an agent attempts an action above its granted tier."""


class HumanApprovalRequired(Exception):
    """Raised when an IRREVERSIBLE action is attempted with no approver."""


@dataclass
class AuditEntry:
    ts: float
    agent: str
    action: str
    tier: int
    decision: str  # allow | deny | approved | rejected
    detail: str
    prev_hash: str
    entry_hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "ts": self.ts,
                "agent": self.agent,
                "action": self.action,
                "tier": self.tier,
                "decision": self.decision,
                "detail": self.detail,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    """Append-only, hash-chained audit trail.

    Each entry's hash includes the previous entry's hash, so any tampering with
    a historical row invalidates every row after it. :meth:`verify` walks the
    chain and returns True only if it is intact.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self, agent: str, action: str, tier: PermissionTier, decision: str, detail: str = ""
    ) -> AuditEntry:
        prev = self._entries[-1].entry_hash if self._entries else "GENESIS"
        entry = AuditEntry(
            ts=time.time(),
            agent=agent,
            action=action,
            tier=int(tier),
            decision=decision,
            detail=detail,
            prev_hash=prev,
        )
        entry.entry_hash = entry.compute_hash()
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def verify(self) -> bool:
        prev = "GENESIS"
        for e in self._entries:
            if e.prev_hash != prev:
                return False
            if e.compute_hash() != e.entry_hash:
                return False
            prev = e.entry_hash
        return True

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(
                {
                    "ts": e.ts,
                    "agent": e.agent,
                    "action": e.action,
                    "tier": e.tier,
                    "decision": e.decision,
                    "detail": e.detail,
                    "hash": e.entry_hash,
                }
            )
            for e in self._entries
        )


# Approver signature: (agent, action, detail) -> bool. Return True to approve.
Approver = Callable[[str, str, str], bool]


@dataclass
class PermissionPolicy:
    """A per-agent grant + optional human approver for irreversible actions."""

    agent: str
    max_tier: PermissionTier
    approver: Approver | None = None
    audit: AuditLog = field(default_factory=AuditLog)

    def check(self, action: str, tier: "str | int | PermissionTier", detail: str = "") -> None:
        """Authorize an action or raise.

        * tier above ``max_tier`` -> PermissionDenied.
        * IRREVERSIBLE tier -> require an approver that returns True, else raise.
        * everything else -> allow.

        Every outcome is recorded to the audit log.
        """
        tier = PermissionTier.parse(tier)

        if tier > self.max_tier:
            self.audit.record(self.agent, action, tier, "deny", detail)
            raise PermissionDenied(
                f"agent '{self.agent}' (max {self.max_tier.name}) may not perform "
                f"{tier.name} action: {action}"
            )

        if tier == PermissionTier.IRREVERSIBLE:
            if self.approver is None:
                self.audit.record(self.agent, action, tier, "deny", "no approver")
                raise HumanApprovalRequired(
                    f"irreversible action '{action}' requires a human approver"
                )
            if not self.approver(self.agent, action, detail):
                self.audit.record(self.agent, action, tier, "rejected", detail)
                raise HumanApprovalRequired(
                    f"human approver rejected irreversible action '{action}'"
                )
            self.audit.record(self.agent, action, tier, "approved", detail)
            return

        self.audit.record(self.agent, action, tier, "allow", detail)
