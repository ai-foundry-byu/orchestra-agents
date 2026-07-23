"""Tests for orchestra.circuit_breaker."""

from orchestra.circuit_breaker import BreakerState, CircuitBreaker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def test_closed_by_default():
    cb = CircuitBreaker("x")
    assert cb.state == BreakerState.CLOSED
    assert cb.should_run()


def test_trips_open_after_threshold():
    alerts = []
    cb = CircuitBreaker("x", threshold=3, on_alert=lambda n, m: alerts.append(m))
    cb.record_failure("boom")
    cb.record_failure("boom")
    assert cb.state == BreakerState.CLOSED
    cb.record_failure("boom")
    assert cb.state == BreakerState.OPEN
    assert not cb.should_run()
    assert len(alerts) == 1  # exactly one alert at the trip


def test_success_resets():
    cb = CircuitBreaker("x", threshold=2)
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == BreakerState.CLOSED  # counter was reset


def test_half_open_after_cooldown():
    clk = Clock()
    cb = CircuitBreaker("x", threshold=1, cooldown=60, clock=clk)
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    clk.advance(61)
    assert cb.state == BreakerState.HALF_OPEN
    assert cb.should_run()  # one trial allowed


def test_half_open_success_closes_and_recovers():
    clk = Clock()
    recoveries = []
    cb = CircuitBreaker(
        "x", threshold=1, cooldown=60, clock=clk,
        on_alert=lambda n, m: recoveries.append(m),
    )
    cb.record_failure()
    clk.advance(61)
    assert cb.state == BreakerState.HALF_OPEN
    cb.record_success()
    assert cb.state == BreakerState.CLOSED
    assert any("recovered" in m for m in recoveries)


def test_half_open_failure_reopens():
    clk = Clock()
    cb = CircuitBreaker("x", threshold=1, cooldown=60, clock=clk)
    cb.record_failure()
    clk.advance(61)
    assert cb.state == BreakerState.HALF_OPEN
    cb.record_failure()
    assert cb.state == BreakerState.OPEN


def test_persistence_survives_reload(tmp_path):
    cb = CircuitBreaker("x", threshold=1, state_dir=tmp_path)
    cb.record_failure("boom")
    assert cb.state == BreakerState.OPEN
    # new instance reads the same file
    cb2 = CircuitBreaker("x", threshold=1, state_dir=tmp_path)
    assert cb2.state == BreakerState.OPEN


def test_guard_context_manager():
    cb = CircuitBreaker("x", threshold=1)
    try:
        with cb.guard():
            raise ValueError("boom")
    except ValueError:
        pass
    assert cb.state == BreakerState.OPEN

    cb2 = CircuitBreaker("y", threshold=1)
    with cb2.guard():
        pass  # success
    assert cb2.state == BreakerState.CLOSED
