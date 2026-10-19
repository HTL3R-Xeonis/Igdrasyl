"""Structural changes to the live lab queue instead of overlapping (see app/live_lock.py)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import app.main as main_module
from app import live_lock, topology_write


def test_exclusive_blocks_a_second_holder_until_the_first_is_done():
    events: list[str] = []

    def worker(name: str) -> None:
        with live_lock.exclusive(name):
            events.append(f"{name}-start")
            time.sleep(0.15)
            events.append(f"{name}-end")

    first = threading.Thread(target=worker, args=("a",))
    second = threading.Thread(target=worker, args=("b",))
    first.start()
    time.sleep(0.05)
    second.start()
    first.join()
    second.join()

    assert events == ["a-start", "a-end", "b-start", "b-end"]


def test_the_lock_is_released_after_an_error():
    try:
        with live_lock.exclusive("failing"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with live_lock.exclusive("next"):
        pass


def test_concurrent_add_node_calls_never_deploy_at_the_same_time(app_state):
    app_state.graph = MagicMock()
    running = 0
    peak = 0
    guard = threading.Lock()

    def slow_add(*args):
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with guard:
            running -= 1

    backend = MagicMock()
    backend.add_node.side_effect = slow_add
    with (
        patch.object(topology_write.Settings, "TOPOLOGY_FILE", "t.yaml"),
        patch("app.topology_write.get_or_create_backend", return_value=backend),
        patch("app.topology_write.load_graph", return_value=MagicMock()),
    ):
        threads = [
            threading.Thread(
                target=topology_write.add_node,
                args=(main_module.app, f"N{i}", "PC", "VPCS"),
            )
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert backend.add_node.call_count == 4
    assert peak == 1
