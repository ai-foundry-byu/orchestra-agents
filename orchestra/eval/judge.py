"""orchestra.eval.judge — adversarial LLM-judge rubric grading.

Score agent output against an explicit, weighted rubric using a skeptical LLM
judge that must return a per-criterion score AND a justification. The judge is a
plain :class:`~orchestra.client.AgentClient` call, so it is model-agnostic and
runs offline against :class:`~orchestra.client.FakeClient` in tests.

    rubric = Rubric([
        RubricCriterion("correctness", "Is the answer factually correct?", weight=2.0),
        RubricCriterion("completeness", "Does it address every part of the task?"),
    ])
    verdict = grade_rubric(client, task="...", output="...", rubric=rubric, pass_threshold=0.7)
    assert verdict.passed
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from orchestra.client import AgentClient


@dataclass
class RubricCriterion:
    name: str
    question: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise ValueError("criterion weight must be > 0")


@dataclass
class Rubric:
    criteria: list[RubricCriterion]

    def __post_init__(self) -> None:
        if not self.criteria:
            raise ValueError("a rubric needs at least one criterion")
        names = [c.name for c in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("rubric criterion names must be unique")

    @property
    def total_weight(self) -> float:
        return sum(c.weight for c in self.criteria)


@dataclass
class CriterionScore:
    name: str
    score: float  # 0..1
    justification: str = ""


@dataclass
class JudgeVerdict:
    weighted_score: float  # 0..1
    passed: bool
    threshold: float
    criteria: list[CriterionScore] = field(default_factory=list)
    summary: str = ""
    cost_usd: float = 0.0


_JUDGE_SYSTEM = (
    "You are a rigorous, adversarial grader. Score the OUTPUT against each rubric "
    "criterion from 0.0 (fails completely) to 1.0 (fully satisfies). Be skeptical: "
    "penalize vagueness, unsupported claims, and partial answers. Respond ONLY with "
    "a JSON object of the form: {\"criteria\": [{\"name\": str, \"score\": number, "
    "\"justification\": str}], \"summary\": str}."
)


def _build_prompt(task: str, output: str, rubric: Rubric) -> str:
    lines = [f"TASK:\n{task}\n", f"OUTPUT TO GRADE:\n{output}\n", "RUBRIC:"]
    for c in rubric.criteria:
        lines.append(f"- {c.name} (weight {c.weight}): {c.question}")
    return "\n".join(lines)


def _parse(text: str, rubric: Rubric) -> tuple[list[CriterionScore], str]:
    by_name = {c.name: 0.0 for c in rubric.criteria}
    justifications: dict[str, str] = {}
    summary = ""
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        for item in data.get("criteria", []):
            name = item.get("name")
            if name in by_name:
                by_name[name] = max(0.0, min(1.0, float(item.get("score", 0.0))))
                justifications[name] = str(item.get("justification", ""))
        summary = str(data.get("summary", ""))
    except Exception:
        pass
    scores = [
        CriterionScore(name=c.name, score=by_name[c.name], justification=justifications.get(c.name, ""))
        for c in rubric.criteria
    ]
    return scores, summary


class LLMJudge:
    """A reusable adversarial judge bound to a client + model."""

    def __init__(self, client: AgentClient, *, model: str | None = None) -> None:
        self.client = client
        self.model = model

    def grade(
        self, task: str, output: str, rubric: Rubric, *, pass_threshold: float = 0.7
    ) -> JudgeVerdict:
        comp = self.client.complete(
            _build_prompt(task, output, rubric),
            system=_JUDGE_SYSTEM,
            model=self.model,
            temperature=0.0,
            max_tokens=800,
        )
        scores, summary = _parse(comp.text, rubric)
        weighted = sum(
            s.score * c.weight for s, c in zip(scores, rubric.criteria)
        ) / rubric.total_weight
        return JudgeVerdict(
            weighted_score=round(weighted, 4),
            passed=weighted >= pass_threshold,
            threshold=pass_threshold,
            criteria=scores,
            summary=summary,
            cost_usd=comp.cost_usd,
        )


def grade_rubric(
    client: AgentClient,
    task: str,
    output: str,
    rubric: Rubric,
    *,
    pass_threshold: float = 0.7,
    model: str | None = None,
) -> JudgeVerdict:
    """Functional shorthand for a one-shot rubric grade."""
    return LLMJudge(client, model=model).grade(
        task, output, rubric, pass_threshold=pass_threshold
    )
