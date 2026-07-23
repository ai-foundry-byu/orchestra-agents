"""Tests for orchestra.fleet."""

import pytest

from orchestra.client import FakeClient
from orchestra.fleet import AgentTask, Fleet, RunStatus
from orchestra.permissions import PermissionDenied, PermissionPolicy, PermissionTier


def test_dispatch_runs_all_in_order():
    fleet = Fleet(FakeClient(responder=lambda p: p.upper()))
    tasks = [AgentTask(agent=f"a{i}", prompt=f"p{i}") for i in range(5)]
    handles = fleet.dispatch(tasks)
    assert len(handles) == 5
    assert [h.task.prompt for h in handles] == [f"p{i}" for i in range(5)]
    assert all(h.status == RunStatus.DONE for h in handles)
    assert handles[0].result == "P0"


def test_dispatch_empty_raises():
    fleet = Fleet(FakeClient())
    with pytest.raises(ValueError):
        fleet.dispatch([])


def test_sibling_failure_isolated():
    def responder(p):
        if "boom" in p:
            raise RuntimeError("kaboom")
        return "ok"

    class ExplodingClient(FakeClient):
        def complete(self, prompt, **kw):
            if "boom" in prompt:
                raise RuntimeError("kaboom")
            return super().complete(prompt, **kw)

    fleet = Fleet(ExplodingClient())
    handles = fleet.dispatch(
        [AgentTask(agent="a", prompt="fine"), AgentTask(agent="b", prompt="boom"),
         AgentTask(agent="c", prompt="fine2")]
    )
    statuses = {h.task.agent: h.status for h in handles}
    assert statuses["a"] == RunStatus.DONE
    assert statuses["b"] == RunStatus.FAILED
    assert statuses["c"] == RunStatus.DONE


def test_permission_gate_denies_before_spend():
    fc = FakeClient()
    pol = PermissionPolicy("writer", PermissionTier.READ)  # only read allowed
    fleet = Fleet(fc, policies={"writer": pol})
    handles = fleet.dispatch(
        [AgentTask(agent="writer", prompt="x", tier=PermissionTier.EXTERNAL)]
    )
    assert handles[0].status == RunStatus.FAILED
    assert "PermissionDenied" in handles[0].error
    assert len(fc.calls) == 0  # no tokens spent


def test_cost_rollup():
    fleet = Fleet(FakeClient())
    handles = fleet.map("a", ["one", "two", "three"])
    assert Fleet.total_cost(handles) > 0
    summ = Fleet.summary(handles)
    assert summ["total"] == 3
    assert summ["done"] == 3
    assert summ["total_cost_usd"] > 0


def test_transform_applied():
    fleet = Fleet(FakeClient(responder=lambda p: "raw"))
    handles = fleet.dispatch(
        [AgentTask(agent="a", prompt="x", transform=lambda t: t + "!")]
    )
    assert handles[0].result == "raw!"


def test_concurrency_cap_respected():
    # with max_parallel=2 and 6 tasks, all still complete (correctness under cap)
    fleet = Fleet(FakeClient(), max_parallel=2)
    handles = fleet.map("a", [str(i) for i in range(6)])
    assert len([h for h in handles if h.status == RunStatus.DONE]) == 6
