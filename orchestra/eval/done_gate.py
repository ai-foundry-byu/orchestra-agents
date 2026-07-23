"""orchestra.eval.done_gate — a machine-checkable definition-of-done gate.

Agents (and humans) love to declare victory early. A done-gate makes "done" a
verifiable predicate instead of a vibe: an ordered list of invariants, each a
callable returning ``(ok: bool, detail: str)``, ALL of which must hold before
work may be called done.

    gate = DoneGate([
        Invariant("version matches", lambda: (deployed_sha() == expected, "...")),
        Invariant("smoke test passes", run_smoke_test),
    ])
    result = gate.run()
    if not result.passed:
        raise RuntimeError(result.summary)

Invariants run in order and short-circuit on the first failure by default (the
usual case: no point smoke-testing a build that isn't even deployed). Pass
``stop_on_failure=False`` to evaluate them all and get a full report.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

# An invariant check returns (ok, human-readable detail).
CheckFn = Callable[[], "tuple[bool, str]"]


@dataclass
class Invariant:
    name: str
    check: CheckFn
    critical: bool = True  # a non-critical failure warns but does not fail the gate


@dataclass
class InvariantResult:
    name: str
    ok: bool
    detail: str
    critical: bool
    skipped: bool = False
    duration_ms: float = 0.0


@dataclass
class DoneGateResult:
    passed: bool
    results: list[InvariantResult] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "invariants": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "detail": r.detail,
                    "critical": r.critical,
                    "skipped": r.skipped,
                    "duration_ms": round(r.duration_ms, 2),
                }
                for r in self.results
            ],
        }


class DoneGate:
    def __init__(self, invariants: list[Invariant], *, stop_on_failure: bool = True) -> None:
        if not invariants:
            raise ValueError("a done-gate needs at least one invariant")
        self.invariants = invariants
        self.stop_on_failure = stop_on_failure

    def run(self) -> DoneGateResult:
        results: list[InvariantResult] = []
        gate_failed = False

        for inv in self.invariants:
            if gate_failed and self.stop_on_failure:
                results.append(
                    InvariantResult(
                        name=inv.name,
                        ok=False,
                        detail="skipped (a prior critical invariant failed)",
                        critical=inv.critical,
                        skipped=True,
                    )
                )
                continue

            start = time.time()
            try:
                ok, detail = inv.check()
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"invariant raised: {type(exc).__name__}: {exc}"
            dur = (time.time() - start) * 1000.0

            results.append(
                InvariantResult(
                    name=inv.name,
                    ok=ok,
                    detail=detail,
                    critical=inv.critical,
                    duration_ms=dur,
                )
            )
            if not ok and inv.critical:
                gate_failed = True

        passed = not any(r.critical and not r.ok for r in results)
        failed_names = [r.name for r in results if r.critical and not r.ok]
        warned = [r.name for r in results if not r.critical and not r.ok]
        if passed:
            summary = f"DONE — all {len([r for r in results if r.critical])} critical invariants hold"
            if warned:
                summary += f" (warnings: {', '.join(warned)})"
        else:
            summary = f"NOT DONE — failed: {', '.join(failed_names)}"
        return DoneGateResult(passed=passed, results=results, summary=summary)
