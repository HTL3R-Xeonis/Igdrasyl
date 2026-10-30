"""The background retry that heals the startup warning by itself."""

from __future__ import annotations

import threading

from app import live_lock, startup_retry


class _Stop:
    """A stop event that lets `ticks` waits pass, recording how long each one was."""

    def __init__(self, ticks):
        self.ticks = ticks
        self.waits = []

    def wait(self, seconds):
        self.waits.append(seconds)
        return len(self.waits) > self.ticks


def _run(warning_states, refresh_results, ticks, intervals=(15, 30, 60)):
    states = iter(warning_states)
    results = iter(refresh_results)
    calls = []

    def should_retry():
        return next(states)

    def refresh():
        calls.append(1)
        return next(results)

    stop = _Stop(ticks)
    startup_retry.run(should_retry, refresh, stop, intervals)
    return stop.waits, len(calls)


def test_it_retries_while_a_warning_stands_and_slows_down():
    # warning stays for four ticks: should_retry is asked once before and once after each refresh
    waits, calls = _run([True, True] * 4, [True] * 4, ticks=4)
    assert calls == 4
    assert waits[:4] == [15, 30, 60, 60]


def test_nothing_is_retried_while_there_is_no_warning():
    waits, calls = _run([False] * 3, [], ticks=3)
    assert calls == 0
    assert waits[:3] == [15, 15, 15]


def test_a_healed_warning_restarts_the_fast_interval():
    # tick 1 refreshes and heals, tick 2 has nothing to do - interval is back to 15
    waits, calls = _run([True, False, False], [True], ticks=2)
    assert calls == 1
    assert waits[:2] == [15, 15]


def test_a_busy_lab_does_not_count_as_an_attempt():
    waits, calls = _run([True, True], [False, True], ticks=2)
    assert calls == 2
    assert waits[:2] == [15, 15]


def test_a_failing_refresh_never_ends_the_loop():
    states = iter([True] * 3)

    def refresh():
        raise RuntimeError("template service still down")

    stop = _Stop(2)
    startup_retry.run(lambda: next(states), refresh, stop, (15, 30))
    assert stop.waits[:3] == [15, 30, 30]


def test_it_stops_when_asked_to():
    stop = threading.Event()
    stop.set()
    thread = startup_retry.start(lambda: True, lambda: True, stop)
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_try_exclusive_never_waits_and_reports_a_busy_lab():
    with live_lock.exclusive("test change"):
        with live_lock.try_exclusive() as got:
            assert got is False
    with live_lock.try_exclusive() as got:
        assert got is True
        with live_lock.try_exclusive() as again:
            assert again is False
