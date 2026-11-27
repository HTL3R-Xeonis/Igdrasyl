"""
Shared fixtures. LITERAL_API_VALUES must be set before src.settings is
first imported anywhere (its value is read once, at class-body execution
time) - this module is guaranteed to run before any test module's own
imports, so it's set here, at the very top, before anything else.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

os.environ["LITERAL_API_VALUES"] = "true"

import pytest
from fastapi.testclient import TestClient
from src.settings import Settings

import app.gateway_main as gateway_module
import app.main as main_module
from app import auth, design, users
from app.routes import common
from app.topology import load_graph, resolve_project_name

_FIXTURE_TOPOLOGY = str(Path(__file__).parent / "fixtures" / "topology.yaml")


@pytest.fixture(autouse=True)
def _service_urls_in_the_environment(monkeypatch):
    """
    A host profile must name its service addresses itself or the environment must (app/required_settings.py); the
    test profiles are bare `esxi: ip` files, so the environment provides them. Tests of the missing case delete these.
    """
    for name in (
        "ESXI_TEMPLATE_SERVER_URL",
        "GNS3_TEMPLATE_SERVER_URL",
        "TOPOLOGY_GENERATOR_URL",
        "OVA_BASE_URL",
    ):
        monkeypatch.setenv(name, "http://services.test:8000")


@pytest.fixture(autouse=True)
def _design_store_tmp_file(tmp_path, monkeypatch):
    """
    Every /design/* mutation now autosaves (see app/design_store.py) -
    without this, every test touching a design endpoint would write a
    real design_state.json into the repo root as a side effect. Autouse
    (unlike TOPOLOGYOPERATOR_LAYOUT_FILE's per-test opt-in) since design
    saves happen on nearly every design mutation across many test files,
    not just a handful that opt in deliberately.
    """
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_DESIGN_FILE", str(tmp_path / "design_state.json")
    )


@pytest.fixture(autouse=True)
def _label_layout_tmp_file(tmp_path, monkeypatch):
    """POST /labels writes a local file (app/label_layout.py); never into the repo root."""
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_LABELS_FILE", str(tmp_path / "label_layout.json")
    )


@pytest.fixture(autouse=True)
def _node_notes_tmp_file(tmp_path, monkeypatch):
    """PUT /notes writes a local file (app/node_notes.py); never into the repo root."""
    monkeypatch.setenv("TOPOLOGYOPERATOR_NOTES_FILE", str(tmp_path / "node_notes.json"))


@pytest.fixture(autouse=True)
def _audit_log_tmp_file(tmp_path, monkeypatch):
    """The admin-events audit log (app/audit_log.py) writes a file; never into the repo root during a test."""
    monkeypatch.setenv("TOPOLOGYOPERATOR_AUDIT_LOG_FILE", str(tmp_path / "audit.log"))


@pytest.fixture(autouse=True)
def _push_storage(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_CONFIG_SNIPPETS_DIR", str(tmp_path / "snippets")
    )
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_PUSH_AUDIT_FILE", str(tmp_path / "push_audit.jsonl")
    )


_TEST_PASSWORD_HASH: str | None = None


@pytest.fixture
def empty_user_store():
    """For tests about accounts themselves (first boot, "last admin" rules): start without the fixtures' standard accounts."""
    store = users._store()
    if store.exists():
        store.unlink()


@pytest.fixture(autouse=True)
def _auth_storage_tmp_file(tmp_path, monkeypatch):
    """
    Accounts (app/users.py), the signed-session secret (app/auth.py) and
    the AI snippet topic allow-list (app/ai_snippets.py) each persist to a
    file - never the repo root during a test. auth._cached_secret is a
    module-level cache (the whole point is to avoid re-reading the secret
    file on every request), so it's reset here too, or every test after
    the first one in a pytest run would silently keep reusing whatever
    secret the very first test happened to generate instead of its own
    fresh tmp_path.
    """
    monkeypatch.setenv("TOPOLOGYOPERATOR_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_SESSION_SECRET_FILE", str(tmp_path / "session_secret.key")
    )
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_AI_CONFIG_FILE",
        str(tmp_path / "ai_config.json"),
    )
    monkeypatch.setattr(auth, "_cached_secret", None)
    # A session token only counts for an account that still exists (auth.verify_account): the fixtures' standard callers need
    # one. Written straight into the store with one password hash made once - hashing three passwords for every one of the
    # ~1500 tests would take most of the run (PBKDF2 is slow on purpose).
    global _TEST_PASSWORD_HASH
    if _TEST_PASSWORD_HASH is None:
        _TEST_PASSWORD_HASH = users.hash_password("correct-horse-battery")
    users._write(
        {
            name: {
                "password_hash": _TEST_PASSWORD_HASH,
                "role": role,
                "created_at": 0.0,
                "allowed_hosts": "ALL",
            }
            for name, role in (
                ("test-admin", "admin"),
                ("test-user", "user"),
                ("test-readonly", "readonly"),
            )
        }
    )


@pytest.fixture
def temp_topology_file(tmp_path, app_state):
    """
    Points Settings.TOPOLOGY_FILE at a mutable copy of the fixture
    topology, so a fake/real write path (add_node, deploy_topology,
    ...) can write to it without touching the shared, checked-in
    fixture file.
    """
    dest = tmp_path / "topology.yaml"
    shutil.copy(_FIXTURE_TOPOLOGY, dest)
    original = Settings.TOPOLOGY_FILE
    Settings.TOPOLOGY_FILE = str(dest)
    try:
        yield dest
    finally:
        Settings.TOPOLOGY_FILE = original


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
def app_state(graph, monkeypatch):
    """
    A real FastAPI app with app.state populated by hand, deliberately
    bypassing the real `lifespan` (which would open a real pyvmomi
    session) - esxi_connection is a MagicMock callers configure per test.
    Matches the pattern used throughout this project's own manual
    verification this session; TestClient is never entered as a context
    manager (`with client:`), which is what would trigger the real
    lifespan.
    """
    # These live route fixtures represent an already selected host. Avoid setting the
    # profile path itself, since routes also use it to locate settings on disk.
    monkeypatch.setattr(common, "has_active_host", lambda: True)
    app = main_module.app
    app.state.graph = graph
    app.state.project_name = resolve_project_name()
    app.state.esxi_connection = MagicMock()
    app.state.gns3_vm_ip = "127.0.0.1"
    app.state.project_id = None
    app.state.startup_warning = None
    app.state.design = design.new_state()
    app.state.design_history = None
    app.state.staged_edit = None
    app.state.gns3_vm_deploy_job = None
    return app.state


@pytest.fixture
def client(app_state):
    """
    An admin-authenticated client - almost every endpoint now needs a
    logged-in session (app/auth_middleware.py), and most of this test
    suite predates accounts/roles entirely, so the default client stays
    able to reach everything it always could. Tests that specifically
    exercise role restrictions use `user_client` or log in as a
    deliberately unauthenticated/non-admin client themselves.
    """
    test_client = TestClient(main_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-admin", "admin")
    )
    return test_client


@pytest.fixture
def user_client(app_state):
    """Same as `client`, but logged in as a non-admin - for tests that check a regular user is refused an admin action."""
    test_client = TestClient(main_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-user", "user")
    )
    return test_client


@pytest.fixture
def readonly_client(app_state):
    """Same as `client`, but logged in as the 'readonly' role - every mutating HTTP request should 403."""
    test_client = TestClient(main_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-readonly", "readonly")
    )
    return test_client


@pytest.fixture
def gateway_client():
    """
    Phase 3's gateway (app/gateway_main.py), admin-authenticated - the
    counterpart to `client` above for everything that moved there (see
    app/routes_gateway.py). Never entered as a context manager, for the
    same reason `client` isn't: that would run the real lifespan
    (users.ensure_bootstrap_admin(), real logging setup).
    """
    test_client = TestClient(gateway_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-admin", "admin")
    )
    return test_client


@pytest.fixture
def gateway_user_client():
    """Same as `gateway_client`, but logged in as a non-admin."""
    test_client = TestClient(gateway_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-user", "user")
    )
    return test_client


@pytest.fixture
def gateway_readonly_client():
    """Same as `gateway_client`, but logged in as the 'readonly' role."""
    test_client = TestClient(gateway_module.app)
    test_client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-readonly", "readonly")
    )
    return test_client


@pytest.fixture
def gateway_anonymous_client():
    """Same gateway app, no session cookie at all - for the "not logged in" paths (AuthMiddleware's redirect)."""
    return TestClient(gateway_module.app)


def _start_fake_tcp_server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any],
) -> tuple[int, Callable[[], None]]:
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
    console.py and Netmiko's actual wire protocol in config_push.py,
    against real sockets rather than mocks.
    """
    stoppers = []

    def start(handler):
        port, stop = _start_fake_tcp_server(handler)
        stoppers.append(stop)
        return port

    yield start

    for stop in stoppers:
        stop()


def _start_fake_ws_server(
    handler: Callable[[Any], Any],
) -> tuple[int, Callable[[], None]]:
    import websockets

    loop = asyncio.new_event_loop()
    port_holder: dict[str, int] = {}
    ready = threading.Event()
    task_holder: dict[str, asyncio.Task] = {}

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port_holder["port"] = server.sockets[0].getsockname()[1]
            ready.set()
            try:
                await asyncio.Future()  # run until cancelled
            except asyncio.CancelledError:
                pass

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
def fake_ws_server():
    """
    app/ws_bridge.py's own counterpart to fake_tcp_server - a real local
    websockets server for a test-provided per-connection handler
    `async def handler(websocket)`. Used to test app/vm_screen.py's real
    WebSocket<->WebSocket byte-pumping against a real upstream socket
    rather than a mock.
    """
    stoppers = []

    def start(handler):
        port, stop = _start_fake_ws_server(handler)
        stoppers.append(stop)
        return port

    yield start

    for stop in stoppers:
        stop()
