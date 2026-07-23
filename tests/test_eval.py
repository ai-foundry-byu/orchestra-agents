"""Tests for orchestra.eval (judge + done-gate)."""

import json

import pytest

from orchestra.client import FakeClient
from orchestra.eval import (
    DoneGate,
    Invariant,
    Rubric,
    RubricCriterion,
    grade_rubric,
)


# ── judge ──────────────────────────────────────────────────────────────────
def test_rubric_validation():
    with pytest.raises(ValueError):
        Rubric([])
    with pytest.raises(ValueError):
        Rubric([RubricCriterion("a", "?"), RubricCriterion("a", "?")])  # dup name
    with pytest.raises(ValueError):
        RubricCriterion("a", "?", weight=0)


def test_grade_rubric_pass():
    def responder(p):
        return json.dumps(
            {
                "criteria": [
                    {"name": "correct", "score": 1.0, "justification": "yes"},
                    {"name": "complete", "score": 0.8, "justification": "mostly"},
                ],
                "summary": "good",
            }
        )

    fc = FakeClient(responder=responder)
    rubric = Rubric(
        [RubricCriterion("correct", "?", weight=2.0), RubricCriterion("complete", "?")]
    )
    v = grade_rubric(fc, "task", "output", rubric, pass_threshold=0.7)
    # weighted = (1.0*2 + 0.8*1) / 3 = 0.933
    assert v.weighted_score == pytest.approx(0.9333, abs=1e-3)
    assert v.passed is True
    assert v.summary == "good"


def test_grade_rubric_fail():
    fc = FakeClient(
        responder=lambda p: json.dumps(
            {"criteria": [{"name": "correct", "score": 0.3, "justification": "no"}], "summary": "bad"}
        )
    )
    rubric = Rubric([RubricCriterion("correct", "?")])
    v = grade_rubric(fc, "task", "output", rubric, pass_threshold=0.7)
    assert v.passed is False


def test_grade_rubric_malformed_judge_output_scores_zero():
    fc = FakeClient(responder=lambda p: "not json at all")
    rubric = Rubric([RubricCriterion("correct", "?")])
    v = grade_rubric(fc, "task", "output", rubric)
    assert v.weighted_score == 0.0
    assert v.passed is False


# ── done gate ──────────────────────────────────────────────────────────────
def test_done_gate_all_pass():
    gate = DoneGate(
        [
            Invariant("sha matches", lambda: (True, "sha ok")),
            Invariant("smoke passes", lambda: (True, "200 OK")),
        ]
    )
    result = gate.run()
    assert result.passed
    assert "DONE" in result.summary


def test_done_gate_short_circuits():
    ran = {"smoke": False}

    def smoke():
        ran["smoke"] = True
        return True, "ran"

    gate = DoneGate(
        [
            Invariant("sha matches", lambda: (False, "sha mismatch")),
            Invariant("smoke passes", smoke),
        ]
    )
    result = gate.run()
    assert not result.passed
    assert ran["smoke"] is False  # short-circuited
    assert result.results[1].skipped


def test_done_gate_no_short_circuit_when_disabled():
    ran = {"smoke": False}

    def smoke():
        ran["smoke"] = True
        return True, "ran"

    gate = DoneGate(
        [
            Invariant("sha matches", lambda: (False, "mismatch")),
            Invariant("smoke passes", smoke),
        ],
        stop_on_failure=False,
    )
    gate.run()
    assert ran["smoke"] is True


def test_done_gate_non_critical_warns_not_fails():
    gate = DoneGate(
        [
            Invariant("core", lambda: (True, "ok")),
            Invariant("nice to have", lambda: (False, "meh"), critical=False),
        ]
    )
    result = gate.run()
    assert result.passed  # non-critical failure doesn't fail the gate
    assert "warnings" in result.summary


def test_done_gate_invariant_exception_is_failure():
    gate = DoneGate([Invariant("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))])
    result = gate.run()
    assert not result.passed
    assert "raised" in result.results[0].detail


def test_done_gate_requires_invariants():
    with pytest.raises(ValueError):
        DoneGate([])
