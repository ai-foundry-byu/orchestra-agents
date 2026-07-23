"""orchestra.fleet — fan out N Claude agents in parallel, with guardrails.

The orchestration core: one caller dispatches many agent runs, each with its own
prompt/model/temperature, bounded by a concurrency cap so you never blow past
rate limits. Every run is:

  * permission-gated — the run's declared tier is checked against its policy
    before a single token is spent (see orchestra.permissions);
  * cost-attributed — token usage rolls up into a real dollar figure;
  * failure-isolated — one agent raising never takes down its siblings; the
    exception is captured on that run's handle.

This is a lightweight, dependency-free supervisor (a thread pool over the
:class:`~orchestra.client.AgentClient`). It deliberately does not shell out to
tmux/worktrees — that heavier "each agent in its own OS process + git worktree"
runtime is a swap-in behind the same :class:`Fleet` API. For an in-process fan
out over the Anthropic API, threads are the right tool.
"""

from __future__ import annotations

import concurrent.futures
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from orchestra.client import AgentClient, Completion
from orchestra.permissions import PermissionPolicy, PermissionTier


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class AgentTask:
    """One unit of work for a fleet agent."""

    agent: str
    prompt: str
    system: str | None = None
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 2048
    tier: PermissionTier = PermissionTier.READ
    action: str = "complete"
    # optional post-processing of the raw model text into the final result
    transform: Callable[[str], str] | None = None


@dataclass
class RunHandle:
    """The record of a single agent run."""

    run_id: str
    task: AgentTask
    status: RunStatus = RunStatus.PENDING
    result: str | None = None
    completion: Completion | None = None
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def cost_usd(self) -> float:
        return self.completion.cost_usd if self.completion else 0.0

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000.0
        return 0.0


class FleetCapacityError(RuntimeError):
    """Raised when a synchronous spawn would exceed the concurrency cap."""


class Fleet:
    """Bounded-concurrency supervisor over an :class:`AgentClient`.

    Parameters
    ----------
    client:
        Any object satisfying :class:`~orchestra.client.AgentClient`.
    max_parallel:
        Hard ceiling on concurrent runs. Protects you from rate limits.
    policies:
        Optional ``{agent_name: PermissionPolicy}``. If a task's agent has a
        policy, its declared tier is authorized before the run; a denial fails
        that run without spending tokens. Agents with no policy run ungated.
    """

    def __init__(
        self,
        client: AgentClient,
        *,
        max_parallel: int = 6,
        policies: dict[str, PermissionPolicy] | None = None,
    ) -> None:
        self.client = client
        self.max_parallel = max(1, max_parallel)
        self.policies = policies or {}

    # ── single run ───────────────────────────────────────────────────────
    def _run_one(self, task: AgentTask) -> RunHandle:
        handle = RunHandle(run_id=uuid.uuid4().hex[:12], task=task, status=RunStatus.RUNNING)
        handle.started_at = time.time()
        try:
            policy = self.policies.get(task.agent)
            if policy is not None:
                policy.check(task.action, task.tier, detail=task.prompt[:120])
            comp = self.client.complete(
                task.prompt,
                system=task.system,
                model=task.model,
                temperature=task.temperature,
                max_tokens=task.max_tokens,
            )
            handle.completion = comp
            handle.result = task.transform(comp.text) if task.transform else comp.text
            handle.status = RunStatus.DONE
        except Exception as exc:  # noqa: BLE001 — isolate sibling failures
            handle.error = f"{type(exc).__name__}: {exc}"
            handle.status = RunStatus.FAILED
        finally:
            handle.finished_at = time.time()
        return handle

    # ── fan out ──────────────────────────────────────────────────────────
    def dispatch(self, tasks: list[AgentTask]) -> list[RunHandle]:
        """Run all tasks with bounded concurrency; return handles in input order.

        Never raises for individual failures — inspect each handle's ``status``
        and ``error``. Raises only for a truly empty task list.
        """
        if not tasks:
            raise ValueError("dispatch() requires at least one task")
        handles: list[RunHandle | None] = [None] * len(tasks)
        workers = min(self.max_parallel, len(tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_idx = {
                pool.submit(self._run_one, t): i for i, t in enumerate(tasks)
            }
            for fut in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[fut]
                handles[idx] = fut.result()
        return [h for h in handles if h is not None]

    def map(self, agent: str, prompts: list[str], **task_kwargs) -> list[RunHandle]:
        """Convenience: run one agent over many prompts."""
        tasks = [AgentTask(agent=agent, prompt=p, **task_kwargs) for p in prompts]
        return self.dispatch(tasks)

    @staticmethod
    def total_cost(handles: list[RunHandle]) -> float:
        return sum(h.cost_usd for h in handles)

    @staticmethod
    def summary(handles: list[RunHandle]) -> dict:
        done = [h for h in handles if h.status == RunStatus.DONE]
        failed = [h for h in handles if h.status == RunStatus.FAILED]
        return {
            "total": len(handles),
            "done": len(done),
            "failed": len(failed),
            "total_cost_usd": round(Fleet.total_cost(handles), 6),
            "errors": [h.error for h in failed if h.error],
        }
