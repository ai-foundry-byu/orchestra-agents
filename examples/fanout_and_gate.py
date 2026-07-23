"""End-to-end example: 1 orchestrator -> N agents -> judged -> done-gated.

Run offline (no API key, free) with the built-in FakeClient:

    python examples/fanout_and_gate.py

Run for real against Claude (BYOK):

    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/fanout_and_gate.py --live

This demonstrates the whole story in ~40 lines:
  1. Fan out N agents in parallel under a concurrency cap (orchestra.Fleet),
     each permission-gated and cost-attributed.
  2. Generate N variants of a prompt and pick a winner with an adversarial
     LLM judge (orchestra.best_of_n).
  3. Grade the winner against a rubric (orchestra.eval.grade_rubric).
  4. Refuse to call it "done" until every invariant holds (orchestra.eval.DoneGate).
"""

from __future__ import annotations

import sys

from orchestra import (
    AuditLog,
    ClaudeClient,
    Fleet,
    PermissionTier,
    Variant,
    best_of_n,
)
from orchestra.client import FakeClient
from orchestra.eval import DoneGate, Invariant, Rubric, RubricCriterion, grade_rubric
from orchestra.fleet import AgentTask
from orchestra.permissions import PermissionPolicy


def make_client(live: bool):
    if live:
        return ClaudeClient(default_model="claude-sonnet-4-20250514")
    # Offline demo brain: canned but structurally-correct responses.
    import json

    def responder(prompt: str) -> str:
        if "CANDIDATE" in prompt:  # best-of-N judge call
            return json.dumps({"scores": [6, 9, 7], "winner": 1, "rationale": "clearest"})
        if "RUBRIC:" in prompt:  # rubric grade call
            return json.dumps(
                {
                    "criteria": [
                        {"name": "correctness", "score": 1.0, "justification": "sound"},
                        {"name": "clarity", "score": 0.9, "justification": "clear"},
                    ],
                    "summary": "solid",
                }
            )
        return f"A concise answer to: {prompt.splitlines()[0][:60]}"

    return FakeClient(responder=responder)


def main() -> int:
    live = "--live" in sys.argv
    client = make_client(live)

    print("=== 1. Parallel fan-out (Fleet) ===")
    audit = AuditLog()
    policies = {
        "researcher": PermissionPolicy("researcher", PermissionTier.READ, audit=audit),
    }
    fleet = Fleet(client, max_parallel=4, policies=policies)
    tasks = [
        AgentTask(agent="researcher", prompt=f"Summarize topic {t}.", tier=PermissionTier.READ)
        for t in ("caching", "retries", "backpressure")
    ]
    handles = fleet.dispatch(tasks)
    print("  summary:", Fleet.summary(handles))
    print("  audit intact:", audit.verify())

    print("\n=== 2. Best-of-N with an adversarial judge ===")
    outcome = best_of_n(
        client,
        "Explain idempotency in one sentence.",
        [
            Variant("claude-sonnet-4-20250514", 0.3),
            Variant("claude-sonnet-4-20250514", 0.7),
            Variant("claude-sonnet-4-20250514", 1.0),
        ],
    )
    print(f"  winner=#{outcome.winner_index}  rationale={outcome.rationale!r}")
    print(f"  winning text: {outcome.winner.text!r}")

    print("\n=== 3. Rubric grade of the winner (LLM judge) ===")
    rubric = Rubric(
        [
            RubricCriterion("correctness", "Is it technically correct?", weight=2.0),
            RubricCriterion("clarity", "Is it clear and concise?"),
        ]
    )
    verdict = grade_rubric(
        client, "Explain idempotency.", outcome.winner.text, rubric, pass_threshold=0.7
    )
    print(f"  score={verdict.weighted_score}  passed={verdict.passed}  ({verdict.summary})")

    print("\n=== 4. Definition-of-Done gate ===")
    gate = DoneGate(
        [
            Invariant("all agents succeeded", lambda: (
                all(h.status.value == "done" for h in handles),
                f"{Fleet.summary(handles)['done']}/{len(handles)} done",
            )),
            Invariant("winner passed rubric", lambda: (verdict.passed, verdict.summary)),
        ]
    )
    result = gate.run()
    print(" ", result.summary)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
