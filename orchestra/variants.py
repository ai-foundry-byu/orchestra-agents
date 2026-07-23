"""orchestra.variants — best-of-N: N variants of a prompt, judged, winner picked.

Run the same prompt through N (model, temperature) variants in parallel, then
have an LLM *judge* score each candidate against a rubric and pick a winner.
This is the sampling half of "sample-and-verify": diversity from temperature +
model spread, quality from an adversarial judge that must justify its pick.

    variants = [
        Variant("claude-opus-4-20250514", 0.7),
        Variant("claude-sonnet-4-20250514", 0.5),
        Variant("claude-sonnet-4-20250514", 1.0),
    ]
    outcome = best_of_n(client, "Write a haiku about databases.", variants)
    print(outcome.winner.text, outcome.rationale)

The judge is itself just an :class:`AgentClient` call, so it runs offline against
:class:`~orchestra.client.FakeClient` in tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from orchestra.client import AgentClient
from orchestra.fleet import AgentTask, Fleet


@dataclass
class Variant:
    model: str
    temperature: float = 0.7

    def __post_init__(self) -> None:
        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError(f"temperature out of range [0,2]: {self.temperature}")


@dataclass
class VariantResult:
    variant: Variant
    text: str
    cost_usd: float = 0.0
    error: str | None = None
    score: float | None = None  # filled by the judge


@dataclass
class BestOfNOutcome:
    prompt: str
    results: list[VariantResult]
    winner_index: int
    rationale: str = ""
    judge_cost_usd: float = 0.0

    @property
    def winner(self) -> VariantResult:
        return self.results[self.winner_index]

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results) + self.judge_cost_usd


_JUDGE_SYSTEM = (
    "You are a strict, adversarial evaluator. You are given a task and several "
    "candidate answers. Score each candidate from 0-10 on how well it satisfies "
    "the task, being skeptical and penalizing hand-waving. Respond ONLY with a "
    "JSON object: {\"scores\": [<number per candidate in order>], "
    "\"winner\": <0-based index>, \"rationale\": \"<one sentence>\"}."
)


def _build_judge_prompt(task: str, results: list[VariantResult]) -> str:
    parts = [f"TASK:\n{task}\n"]
    for i, r in enumerate(results):
        body = r.text if r.error is None else f"(this candidate failed: {r.error})"
        parts.append(f"CANDIDATE {i}:\n{body}\n")
    return "\n".join(parts)


def _parse_judge(text: str, n: int) -> tuple[list[float], int, str]:
    """Extract scores/winner/rationale from the judge output, defensively."""
    scores: list[float] = []
    winner = 0
    rationale = ""
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        raw_scores = data.get("scores", [])
        scores = [float(s) for s in raw_scores][:n]
        winner = int(data.get("winner", 0))
        rationale = str(data.get("rationale", ""))
    except Exception:
        pass
    # normalize
    if len(scores) != n:
        scores = (scores + [0.0] * n)[:n]
    if not (0 <= winner < n):
        winner = max(range(n), key=lambda i: scores[i]) if scores else 0
    return scores, winner, rationale


def best_of_n(
    client: AgentClient,
    prompt: str,
    variants: list[Variant],
    *,
    system: str | None = None,
    max_tokens: int = 1024,
    judge_model: str | None = None,
    max_parallel: int = 6,
) -> BestOfNOutcome:
    """Generate N candidates in parallel and pick a winner via an LLM judge.

    A judge that only picks among *successful* candidates: failed variants stay
    in the result set (for transparency) but are scored 0 by construction.
    """
    if not variants:
        raise ValueError("best_of_n requires at least one variant")

    fleet = Fleet(client, max_parallel=max_parallel)
    tasks = [
        AgentTask(
            agent=f"variant-{i}",
            prompt=prompt,
            system=system,
            model=v.model,
            temperature=v.temperature,
            max_tokens=max_tokens,
        )
        for i, v in enumerate(variants)
    ]
    handles = fleet.dispatch(tasks)

    results = [
        VariantResult(
            variant=variants[i],
            text=h.result or "",
            cost_usd=h.cost_usd,
            error=h.error,
        )
        for i, h in enumerate(handles)
    ]

    # If only one candidate succeeded, no judge call is needed.
    ok = [i for i, r in enumerate(results) if r.error is None and r.text]
    if len(ok) <= 1:
        winner = ok[0] if ok else 0
        for i, r in enumerate(results):
            r.score = 10.0 if i == winner and ok else 0.0
        return BestOfNOutcome(
            prompt=prompt,
            results=results,
            winner_index=winner,
            rationale="single successful candidate" if ok else "no successful candidate",
        )

    judge_comp = client.complete(
        _build_judge_prompt(prompt, results),
        system=_JUDGE_SYSTEM,
        model=judge_model,
        temperature=0.0,
        max_tokens=512,
    )
    scores, winner, rationale = _parse_judge(judge_comp.text, len(results))
    for r, s in zip(results, scores):
        r.score = s
    # never let the judge pick a failed candidate
    if results[winner].error is not None:
        winner = max(ok, key=lambda i: scores[i])

    return BestOfNOutcome(
        prompt=prompt,
        results=results,
        winner_index=winner,
        rationale=rationale,
        judge_cost_usd=judge_comp.cost_usd,
    )
