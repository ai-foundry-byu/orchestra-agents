"""orchestra.eval — the eval / QA harness half of the story.

Two complementary gates you run before trusting agent output:

    LLMJudge / grade_rubric   an adversarial LLM judge scores an agent's output
                              against an explicit rubric and must justify a
                              pass/fail. Cheap, model-agnostic, offline-testable.

    DoneGate                  a definition-of-done gate: an ordered list of
                              machine-checkable invariants (e.g. "the deployed
                              artifact matches the expected version", "the smoke
                              test passes") that ALL must hold before work may be
                              called done. Kills "it should be live soon."
"""

from __future__ import annotations

from orchestra.eval.done_gate import (
    DoneGate,
    DoneGateResult,
    Invariant,
    InvariantResult,
)
from orchestra.eval.judge import (
    JudgeVerdict,
    LLMJudge,
    Rubric,
    RubricCriterion,
    grade_rubric,
)

__all__ = [
    "LLMJudge",
    "Rubric",
    "RubricCriterion",
    "JudgeVerdict",
    "grade_rubric",
    "DoneGate",
    "DoneGateResult",
    "Invariant",
    "InvariantResult",
]
