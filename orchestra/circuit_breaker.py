"""orchestra.circuit_breaker — stop agents from looping forever on a dead dependency.

The failure mode this prevents: a credential expires (or an API starts 500ing),
and every scheduled agent run keeps hammering it, burning tokens and spewing
identical errors, with nothing noticing. A circuit breaker gives each dependency
a state machine that trips OPEN after N consecutive failures, short-circuits
further work during a cooldown, alerts exactly once, then probes for recovery.

State machine (per named breaker):

    CLOSED      normal. Work runs. Failures increment a counter; `threshold`
                consecutive failures trip it OPEN. Any success resets to 0.
    OPEN        tripped. should_run() -> False until `cooldown` seconds pass.
                Exactly ONE alert fires at the moment of the trip.
    HALF_OPEN   cooldown elapsed. should_run() -> True for one trial. A success
                closes it (recovery note); another failure re-OPENs it.

State can persist to a JSON file so it survives process restarts (the common
case for cron-driven agents). In-memory is the default for tests/embedding.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _State:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    alerted: bool = False


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        threshold: int = 3,
        cooldown: float = 300.0,
        state_dir: str | Path | None = None,
        on_alert: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._on_alert = on_alert
        self._clock = clock
        self._path: Path | None = None
        if state_dir is not None:
            self._path = Path(state_dir).expanduser() / f"{name}.json"
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> _State:
        if self._path and self._path.exists():
            try:
                d = json.loads(self._path.read_text())
                return _State(
                    state=BreakerState(d.get("state", "closed")),
                    consecutive_failures=int(d.get("consecutive_failures", 0)),
                    opened_at=float(d.get("opened_at", 0.0)),
                    alerted=bool(d.get("alerted", False)),
                )
            except Exception:
                pass
        return _State()

    def _save(self) -> None:
        if not self._path:
            return
        self._path.write_text(
            json.dumps(
                {
                    "state": self._state.state.value,
                    "consecutive_failures": self._state.consecutive_failures,
                    "opened_at": self._state.opened_at,
                    "alerted": self._state.alerted,
                }
            )
        )

    # ── state ────────────────────────────────────────────────────────────
    @property
    def state(self) -> BreakerState:
        # Lazily transition OPEN -> HALF_OPEN once the cooldown has elapsed.
        if (
            self._state.state == BreakerState.OPEN
            and self._clock() - self._state.opened_at >= self.cooldown
        ):
            self._state.state = BreakerState.HALF_OPEN
            self._save()
        return self._state.state

    def should_run(self) -> bool:
        """True when work may proceed (CLOSED, or HALF_OPEN trial)."""
        return self.state in (BreakerState.CLOSED, BreakerState.HALF_OPEN)

    def record_success(self) -> None:
        was_open = self._state.state in (BreakerState.OPEN, BreakerState.HALF_OPEN)
        self._state = _State()  # fully reset to CLOSED
        self._save()
        if was_open and self._on_alert:
            self._on_alert(self.name, f"circuit '{self.name}' recovered — closed")

    def record_failure(self, detail: str = "") -> None:
        self._state.consecutive_failures += 1
        if self._state.consecutive_failures >= self.threshold and self._state.state != BreakerState.OPEN:
            self._trip(detail)
        elif self._state.state == BreakerState.HALF_OPEN:
            # a failed trial re-opens and resets the cooldown clock
            self._trip(detail, realert=False)
        self._save()

    def _trip(self, detail: str, realert: bool = True) -> None:
        self._state.state = BreakerState.OPEN
        self._state.opened_at = self._clock()
        if realert and not self._state.alerted and self._on_alert:
            self._on_alert(
                self.name,
                f"circuit '{self.name}' OPEN after {self._state.consecutive_failures} "
                f"failures — short-circuiting for {self.cooldown:.0f}s. {detail}".strip(),
            )
            self._state.alerted = True

    # ── context-manager sugar ────────────────────────────────────────────
    def guard(self):
        """Use as ``with breaker.guard():`` — records success on clean exit,
        failure on exception, and re-raises."""
        return _Guard(self)


class _Guard:
    def __init__(self, cb: CircuitBreaker) -> None:
        self._cb = cb

    def __enter__(self) -> CircuitBreaker:
        return self._cb

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._cb.record_success()
        else:
            self._cb.record_failure(str(exc))
        return False  # never suppress
