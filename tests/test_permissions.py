"""Tests for orchestra.permissions."""

import pytest

from orchestra.permissions import (
    AuditLog,
    HumanApprovalRequired,
    PermissionDenied,
    PermissionPolicy,
    PermissionTier,
)


def test_tier_parse():
    assert PermissionTier.parse("write") == PermissionTier.WRITE
    assert PermissionTier.parse(3) == PermissionTier.EXTERNAL
    assert PermissionTier.parse(PermissionTier.READ) == PermissionTier.READ


def test_read_agent_allowed_read():
    pol = PermissionPolicy("reader", PermissionTier.READ)
    pol.check("fetch page", PermissionTier.READ)  # no raise
    assert pol.audit.entries[-1].decision == "allow"


def test_read_agent_denied_write():
    pol = PermissionPolicy("reader", PermissionTier.READ)
    with pytest.raises(PermissionDenied):
        pol.check("write file", PermissionTier.WRITE)
    assert pol.audit.entries[-1].decision == "deny"


def test_irreversible_needs_approver():
    pol = PermissionPolicy("deployer", PermissionTier.IRREVERSIBLE)
    with pytest.raises(HumanApprovalRequired):
        pol.check("deploy prod", PermissionTier.IRREVERSIBLE)


def test_irreversible_approver_yes():
    pol = PermissionPolicy(
        "deployer", PermissionTier.IRREVERSIBLE, approver=lambda a, act, d: True
    )
    pol.check("deploy prod", PermissionTier.IRREVERSIBLE)
    assert pol.audit.entries[-1].decision == "approved"


def test_irreversible_approver_no():
    pol = PermissionPolicy(
        "deployer", PermissionTier.IRREVERSIBLE, approver=lambda a, act, d: False
    )
    with pytest.raises(HumanApprovalRequired):
        pol.check("deploy prod", PermissionTier.IRREVERSIBLE)
    assert pol.audit.entries[-1].decision == "rejected"


def test_audit_chain_intact():
    log = AuditLog()
    log.record("a", "x", PermissionTier.READ, "allow")
    log.record("a", "y", PermissionTier.WRITE, "allow")
    log.record("a", "z", PermissionTier.EXTERNAL, "deny")
    assert log.verify() is True


def test_audit_chain_tamper_detected():
    log = AuditLog()
    log.record("a", "x", PermissionTier.READ, "allow")
    log.record("a", "y", PermissionTier.WRITE, "allow")
    # tamper with a historical entry's content
    log.entries  # snapshot
    log._entries[0].action = "MALICIOUS"
    assert log.verify() is False


def test_audit_jsonl():
    log = AuditLog()
    log.record("a", "x", PermissionTier.READ, "allow", "detail")
    line = log.to_jsonl()
    assert '"agent": "a"' in line
    assert '"hash"' in line
