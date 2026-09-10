"""
Shared fixtures. LITERAL_API_VALUES must be set before src.settings is
first imported anywhere (its value is read once, at class-body execution
time) - this module is guaranteed to run before any test module's own
imports, so it's set here, at the very top, before anything else.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

os.environ["LITERAL_API_VALUES"] = "true"

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.topology import load_graph, resolve_project_name
from src.settings import Settings

_FIXTURE_TOPOLOGY = str(Path(__file__).parent / "fixtures" / "topology.yaml")


@pytest.fixture
def graph():
    """The small fixture topology (R1 router, SW1 VPCS switch, VM1 ESXi VM)."""
    original = Settings.TOPOLOGY_FILE
    Settings.TOPOLOGY_FILE = _FIXTURE_TOPOLOGY
    try:
        yield load_graph()
    finally:
        Settings.TOPOLOGY_FILE = original


@pytest.fixture
def app_state(graph):
    """
    A real FastAPI app with app.state populated by hand, deliberately
    bypassing the real `lifespan` (which would open a real pyvmomi
    session) - esxi_connection is a MagicMock callers configure per test.
    Matches the pattern used throughout this project's own manual
    verification this session; TestClient is never entered as a context
    manager (`with client:`), which is what would trigger the real
    lifespan.
    """
    app = main_module.app
    app.state.graph = graph
    app.state.project_name = resolve_project_name()
    app.state.esxi_connection = MagicMock()
    app.state.vm_console_ports = {}
    app.state.gns3_vm_ip = "127.0.0.1"
    app.state.project_id = None
    app.state.startup_warning = None
    return app.state


@pytest.fixture
def client(app_state):
    return TestClient(main_module.app)


def _start_fake_tcp_server(handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any]) -> tuple[int, Callable[[], None]]:
    loop = asyncio.new_event_loop()
    port_holder: dict[str, int] = {}
    ready = threading.Event()
    task_holder: dict[str, asyncio.Task] = {}

    async def main():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port_holder["port"] = server.sockets[0].getsockname()[1]
        ready.set()
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            server.close()
            await server.wait_closed()

    def run():
        asyncio.set_event_loop(loop)
        task = loop.create_task(main())
        task_holder["task"] = task
        loop.run_until_complete(task)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)

    def stop():
        loop.call_soon_threadsafe(task_holder["task"].cancel)
        thread.join(timeout=5)

    return port_holder["port"], stop


@pytest.fixture
def fake_tcp_server():
    """
    Starts a real local asyncio TCP server for a test-provided per-
    connection handler `async def handler(reader, writer)`. Returns the
    bound port. Used to test the real TCP<->WebSocket byte-pumping in
    console.py/vm_console.py and Netmiko's actual wire protocol in
    config_push.py, against real sockets rather than mocks.
    """
    stoppers = []

    def start(handler):
        port, stop = _start_fake_tcp_server(handler)
        stoppers.append(stop)
        return port

    yield start

    for stop in stoppers:
        stop()
