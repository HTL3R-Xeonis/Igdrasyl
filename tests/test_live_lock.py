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


def test_concurrent_add_node_calls_never_deploy_at_the_same_time(app_state, tmp_path):
    topology = tmp_path / "t.yaml"
    topology.write_text("nodes: []\nedges: []\n")
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
        patch.object(topology_write.Settings, "TOPOLOGY_FILE", str(topology)),
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


def test_a_name_is_taken_at_once_even_while_the_first_add_is_still_deploying(
    app_state, tmp_path
):
    import pytest

    topology = tmp_path / "t.yaml"
    topology.write_text("nodes: []\nedges: []\n")
    release = threading.Event()
    started = threading.Event()

    def slow_add(*args):
        started.set()
        release.wait(5)

    backend = MagicMock()
    backend.add_node.side_effect = slow_add
    app_state.graph = MagicMock()
    app_state.graph.nodes = {}
    errors: list[Exception] = []

    with (
        patch.object(topology_write.Settings, "TOPOLOGY_FILE", str(topology)),
        patch("app.topology_write.get_or_create_backend", return_value=backend),
        patch("app.topology_write.load_graph", return_value=app_state.graph),
    ):
        first = threading.Thread(
            target=topology_write.add_node,
            args=(main_module.app, "VM1", "VM", "debian"),
        )
        first.start()
        assert started.wait(5)

        # Same name, other capitalisation, while the first is still deploying.
        with pytest.raises(ValueError, match="just being deployed"):
            topology_write.add_node(main_module.app, "vm1", "VM", "MikroTik")
        # A different name is fine (it waits for the lock; do not block here).
        release.set()
        first.join()
        topology_write.add_node(main_module.app, "VM2", "VM", "debian")

    assert not errors
    assert backend.add_node.call_count == 2


def test_an_existing_name_is_refused_case_insensitively(app_state, tmp_path):
    import pytest

    topology = tmp_path / "t.yaml"
    topology.write_text("nodes: []\nedges: []\n")
    app_state.graph = MagicMock()
    app_state.graph.nodes = {"R1": object()}
    with (
        patch.object(topology_write.Settings, "TOPOLOGY_FILE", str(topology)),
        patch("app.topology_write.get_or_create_backend") as backend,
    ):
        with pytest.raises(ValueError, match="already exists"):
            topology_write.add_node(main_module.app, "r1", "ROUTER", "x")
        with pytest.raises(ValueError, match="must not be empty"):
            topology_write.add_node(main_module.app, "  ", "ROUTER", "x")
    backend.assert_not_called()


def test_a_released_name_can_be_used_again_after_a_failed_add(app_state, tmp_path):
    import pytest

    topology = tmp_path / "t.yaml"
    topology.write_text("nodes: []\nedges: []\n")
    app_state.graph = MagicMock()
    app_state.graph.nodes = {}
    backend = MagicMock()
    backend.add_node.side_effect = [RuntimeError("deploy failed"), None]
    with (
        patch.object(topology_write.Settings, "TOPOLOGY_FILE", str(topology)),
        patch("app.topology_write.get_or_create_backend", return_value=backend),
        patch("app.topology_write.load_graph", return_value=app_state.graph),
    ):
        with pytest.raises(RuntimeError):
            topology_write.add_node(main_module.app, "VM1", "VM", "debian")
        topology_write.add_node(main_module.app, "VM1", "VM", "debian")
    assert backend.add_node.call_count == 2
