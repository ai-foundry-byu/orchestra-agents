"""Tests for orchestra.variants (best-of-N)."""

import json

import pytest

from orchestra.client import FakeClient
from orchestra.variants import Variant, best_of_n


def test_variant_temperature_validation():
    with pytest.raises(ValueError):
        Variant("m", 3.0)


def test_best_of_n_requires_variants():
    with pytest.raises(ValueError):
        best_of_n(FakeClient(), "task", [])


def test_best_of_n_single_variant_no_judge():
    fc = FakeClient(responder=lambda p: "only answer")
    outcome = best_of_n(fc, "task", [Variant("m", 0.7)])
    assert outcome.winner.text == "only answer"
    assert outcome.rationale == "single successful candidate"


def test_best_of_n_judge_picks_winner():
    # Judge returns JSON selecting candidate 1.
    def responder(prompt):
        if "adversarial evaluator" in prompt or "CANDIDATE" in prompt:
            return json.dumps({"scores": [3, 9], "winner": 1, "rationale": "cand 1 better"})
        return "candidate text"

    # The judge system prompt is passed via `system=`, so detect the judge call
    # by the presence of CANDIDATE blocks in the prompt body.
    fc = FakeClient(responder=responder)
    outcome = best_of_n(
        fc, "task", [Variant("m1", 0.5), Variant("m2", 1.0)]
    )
    assert outcome.winner_index == 1
    assert outcome.rationale == "cand 1 better"
    assert outcome.results[1].score == 9


def test_best_of_n_never_picks_failed_candidate():
    calls = {"n": 0}

    class FlakyClient(FakeClient):
        def complete(self, prompt, **kw):
            # judge call (has CANDIDATE blocks) picks index 0 which failed
            if "CANDIDATE" in prompt:
                return _fake_completion(
                    json.dumps({"scores": [10, 5], "winner": 0, "rationale": "x"})
                )
            calls["n"] += 1
            if calls["n"] == 1:  # first variant explodes
                raise RuntimeError("boom")
            return super().complete(prompt, **kw)

    fc = FlakyClient()
    outcome = best_of_n(fc, "task", [Variant("m1", 0.5), Variant("m2", 1.0)])
    # candidate 0 failed, so winner must be forced to 1 despite judge saying 0
    assert outcome.results[outcome.winner_index].error is None


def _fake_completion(text):
    from orchestra.client import Completion

    return Completion(text=text, model="fake", input_tokens=1, output_tokens=1)


def test_best_of_n_total_cost():
    fc = FakeClient(
        responder=lambda p: json.dumps({"scores": [5, 6], "winner": 1, "rationale": "r"})
        if "CANDIDATE" in p
        else "answer"
    )
    outcome = best_of_n(fc, "task", [Variant("m1", 0.5), Variant("m2", 1.0)])
    assert outcome.total_cost_usd > 0
