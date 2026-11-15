"""
Crawls every route with bad input and with unreachable backends and asserts nothing answers 500: an unhandled exception
means a state we did not think of (a down host, a missing project, a malformed body) reached the user as a raw error.
Deliberate refusals (4xx, 502/503) are fine.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from src.settings import Settings

import app.main as main_module
from app import auth, esxi_session


def _authenticated_client(**kwargs) -> TestClient:
    """A raw TestClient logged in as an admin - every route here now needs a session (app/auth_middleware.py)."""
    client = TestClient(main_module.app, **kwargs)
    client.cookies.set(
        auth.SESSION_COOKIE, auth.create_session_token("test-admin", "admin")
    )
    return client


_SKIP = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/links/{node1}/{node2}/capture/stream",  # an endless stream by design
    "/settings-profiles/{filename}/activate",  # switches the process-wide host
}
_PATH_VALUES = ["R1", "nope", "..%2Fx"]
_VALID_BODIES: dict = {}
_BODIES = [
    {"json": {}},
    {"json": {"name": 1, "devices": "x", "nodes": None, "image": [], "x": "a"}},
    {"json": [1, 2]},
    {"content": b"{not json", "headers": {"content-type": "application/json"}},
    {"files": {"file": ("x.topoproj", b"junk", "application/zip")}},
]


def _sample(schema: dict, defs: dict, depth: int = 0):
    """A well-formed example value for a JSON schema (OpenAPI): the first enum value, 'x', 1, one-element lists."""
    if "$ref" in schema:
        return _sample(defs[schema["$ref"].split("/")[-1]], defs, depth)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _sample(options[0], defs, depth)
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        if depth > 4:
            return {}
        return {
            name: _sample(prop, defs, depth + 1)
            for name, prop in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [] if depth > 4 else [_sample(schema.get("items", {}), defs, depth + 1)]
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return True
    return "x"


def _schema_bodies() -> dict[tuple[str, str], dict]:
    """(METHOD, path template) -> a well-formed JSON body, from the app's own OpenAPI document."""
    document = main_module.app.openapi()
    defs = document.get("components", {}).get("schemas", {})
    bodies = {}
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            content = operation.get("requestBody", {}).get("content", {})
            if "application/json" in content:
                bodies[(method.upper(), path)] = _sample(
                    content["application/json"]["schema"], defs
                )
    return bodies


def _no_network(*args, **kwargs):
    raise ConnectionRefusedError("network blocked by the robustness test")


_real_connect = socket.socket.connect


def _only_localhost(sock, address, *args, **kwargs):
    if address[0] not in ("127.0.0.1", "::1"):
        raise ConnectionRefusedError(f"{address[0]} blocked by the robustness test")
    return _real_connect(sock, address, *args, **kwargs)


def _crawl(client: TestClient) -> list[str]:
    failures: list[str] = []
    _VALID_BODIES.update(_schema_bodies())
    for route in main_module.app.routes:
        if not isinstance(route, APIRoute) or route.path in _SKIP:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            for value in _PATH_VALUES:
                path = route.path
                for param in route.param_convertors:
                    path = path.replace("{" + param + "}", value)
                variants = (
                    list(_BODIES) if method in {"POST", "PUT", "DELETE"} else [{}]
                )
                if (method, route.path) in _VALID_BODIES:
                    variants.append({"json": _VALID_BODIES[(method, route.path)]})
                for variant in variants:
                    started = time.monotonic()
                    try:
                        response = client.request(method, path, **variant)
                    except (
                        Exception
                    ) as exc:  # TestClient re-raises what the app did not handle
                        failures.append(
                            f"{method} {path} {list(variant)} -> {type(exc).__name__}: {str(exc)[:120]}"
                        )
                        continue
                    if time.monotonic() - started > 3:
                        print(
                            f"SLOW {time.monotonic() - started:.0f}s {method} {path} {list(variant)}",
                            file=sys.stderr,
                            flush=True,
                        )
                    if response.status_code == 500:
                        failures.append(f"{method} {path} {list(variant)} -> 500")
    return sorted(set(failures))


@pytest.fixture
def robust_client(app_state, tmp_path, monkeypatch):
    monkeypatch.chdir(
        tmp_path
    )  # files the app writes relative to its folder stay in the test folder
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    return _authenticated_client(raise_server_exceptions=True)


def test_no_route_answers_500_when_the_backends_are_unreachable(
    robust_client, app_state
):
    esxi = MagicMock()
    esxi.get_vm.side_effect = ConnectionError("ESXi is down")
    esxi.content.viewManager.CreateContainerView.side_effect = ConnectionError(
        "ESXi is down"
    )
    app_state.esxi_connection = esxi
    failures = _crawl(robust_client)
    assert not failures, "\n" + "\n".join(failures)


def test_no_route_answers_500_when_nothing_is_loaded(robust_client, app_state):
    app_state.graph = None
    app_state.gns3_vm_ip = None
    app_state.esxi_connection = esxi_session.LazyESXiConnection(
        _no_network, host="10.0.0.1", connect_wait_seconds=0.05
    )
    failures = _crawl(robust_client)
    assert not failures, "\n" + "\n".join(failures)


class _BadGns3(BaseHTTPRequestHandler):
    """A GNS3 server that answers everything wrongly: HTML errors, or a 200 that is not JSON."""

    mode = "500"

    def _answer(self):
        if self.mode == "500":
            self.send_response(500)
            body = b"<html>boom</html>"
        elif self.mode == "404":
            self.send_response(404)
            body = b'{"message": "not found"}'
        else:
            self.send_response(200)
            body = b"this is not json"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _answer

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("mode", ["500", "404", "garbage"])
def test_no_route_answers_500_when_gns3_answers_wrongly(
    mode, app_state, tmp_path, monkeypatch
):
    _BadGns3.mode = mode
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BadGns3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(socket.socket, "connect", _only_localhost)
        monkeypatch.setattr(Settings.GNS3, "PORT", server.server_address[1])
        app_state.gns3_vm_ip = "127.0.0.1"
        app_state.project_id = "11111111-1111-1111-1111-111111111111"
        app_state.esxi_connection = MagicMock()
        client = _authenticated_client(raise_server_exceptions=True)
        failures = _crawl(client)
    finally:
        server.shutdown()
    assert not failures, "\n" + "\n".join(failures)


_WEIRD_NAMES = [
    "R1",
    "r1",
    "SW 1",
    "ä-ö-ü",
    "a" * 200,
    "x/y",
    "..",
    "../etc",
    "<script>",
    "R1;DROP",
    "",
    " ",
    "emoji-😀",
    "nul\x00l",
    "R1\n",
    "%2e%2e",
]


def test_the_design_canvas_survives_random_operations_with_odd_names(
    app_state, tmp_path, monkeypatch
):
    """Random add/link/move/delete/undo/redo/export with odd names: never a 500, and the export stays a readable zip."""
    import io
    import random
    import zipfile

    monkeypatch.chdir(tmp_path)
    client = _authenticated_client(raise_server_exceptions=True)
    images = client.get("/design/templates").json()
    flat = [
        (role, image)
        for role in ("router", "switch", "server")
        for image in images["gns3"] + images["esxi"]
    ]
    assert flat, "the test fixture knows no templates"
    rng = random.Random(20261108)
    names: list[str] = []
    for _ in range(400):
        op = rng.choice(
            [
                "node",
                "node",
                "link",
                "layout",
                "delete",
                "undo",
                "redo",
                "notes",
                "labels",
                "paste",
                "export",
            ]
        )
        name = rng.choice(_WEIRD_NAMES + names)
        other = rng.choice(_WEIRD_NAMES + names)
        if op == "node":
            role, image = rng.choice(flat)
            response = client.post(
                "/design/nodes", json={"name": name, "role": role, "image": image}
            )
            if response.status_code == 200:
                names.append(name)
        elif op == "link":
            response = client.post(
                "/design/links",
                json={
                    "node1": name,
                    "if1": rng.choice(["eth0", "gi0/0", "", "x" * 100]),
                    "node2": other,
                    "if2": rng.choice(["eth0", "gi0/1", ""]),
                },
            )
        elif op == "layout":
            response = client.post(
                "/design/layout",
                json={
                    "name": name,
                    "x": rng.choice([0, -5, 1e9, 3.5]),
                    "y": rng.choice([0, 10, -1e9]),
                },
            )
        elif op == "delete":
            response = client.post(
                "/design/delete", json={"nodes": [name], "links": [[name, other]]}
            )
        elif op == "undo":
            response = client.post("/design/undo")
        elif op == "redo":
            response = client.post("/design/redo")
        elif op == "notes":
            response = client.put(
                f"/design/notes/{quote(name, safe='')}",
                json={"note": rng.choice(["", "hi", "x" * 5000])},
            )
        elif op == "labels":
            response = client.post(
                "/design/labels", json={"name": name, "dx": 1, "dy": 2}
            )
        elif op == "paste":
            response = client.post(
                "/design/paste",
                json={
                    "nodes": [
                        {
                            "name": name,
                            "role": flat[0][0],
                            "image": flat[0][1],
                            "x": 1,
                            "y": 2,
                        }
                    ],
                    "links": [],
                },
            )
        else:
            response = client.get("/design/export")
            assert response.status_code == 200
            assert zipfile.ZipFile(io.BytesIO(response.content)).testzip() is None
        assert response.status_code < 500, (
            op,
            name,
            other,
            response.status_code,
            response.text[:200],
        )
    assert client.get("/design/topology").status_code == 200
