"""The admin panel's Images section: the status of the automated image build, read from the build machine's status page."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import image_build_status as ibs

URL = "/admin/image-builds"
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def build(
    version="1.3.7", result="published", finished="2026-11-24T09:00:00Z", **extra
):
    return {
        "version": version,
        "result": result,
        "started_at": "2026-11-24T08:00:00Z",
        "finished_at": finished,
        "template": f"ginnungagap-appliance-v{version}",
        "reason": None,
        "acceptance": {"passed": 37, "total": 37},
        **extra,
    }


def document(**overrides):
    latest = overrides.pop("latest", build())
    doc = {
        "schema": 1,
        "generated_at": "2026-11-24T11:59:58Z",
        "worker": "183-Debian-Ginn-Imagebuild",
        "state": "idle",
        "paused": False,
        "active": None,
        "latest": latest,
        "builds": [latest] if latest else [],
    }
    doc.update(overrides)
    return doc


class Worker:
    """A real HTTP server on a free local port that plays the build machine's status page."""

    def __init__(self):
        self.status = 200
        self.body = json.dumps(document()).encode()
        self.headers: dict[str, str] = {}
        self.delay = 0.0
        self.requests = 0
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                worker.requests += 1
                time.sleep(worker.delay)
                self.send_response(worker.status)
                for key, value in worker.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(worker.body)))
                self.end_headers()
                self.wfile.write(worker.body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def serve(self, doc):
        self.body = json.dumps(doc).encode()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv(ibs.ENV_URL, raising=False)
    ibs.reset_cache()
    yield
    ibs.reset_cache()


@pytest.fixture
def worker(monkeypatch):
    server = Worker()
    monkeypatch.setenv(ibs.ENV_URL, server.url)
    yield server
    server.stop()


# ---- the address ----


def test_without_an_address_the_feature_is_off():
    assert ibs.status_url() is None
    view = ibs.overview(NOW)
    assert view["configured"] is False and view["health"] == "not_configured"
    assert view["builds"] == [] and "not set up" in view["message"]


@pytest.mark.parametrize(
    "given, expected",
    [
        ("http://10.20.20.183:8183", "http://10.20.20.183:8183/status.json"),
        ("http://worker.lab:8183/", "http://worker.lab:8183/status.json"),
        ("https://worker.lab/status.json", "https://worker.lab/status.json"),
        ("  http://[fd00::1]:9000  ", "http://[fd00::1]:9000/status.json"),
    ],
)
def test_the_address_is_normalised(monkeypatch, given, expected):
    monkeypatch.setenv(ibs.ENV_URL, given)
    assert ibs.status_url() == expected


@pytest.mark.parametrize(
    "given",
    [
        "ftp://worker/status.json",
        "worker:8183",
        "http://",
        "http://user:secret@worker:8183",
        "http://worker:8183/status.json?x=1",
        "http://worker:8183/status.json#top",
        "http://worker:8183/other",
        "http://worker:notaport",
    ],
)
def test_an_unusable_address_is_reported_not_used(monkeypatch, given):
    monkeypatch.setenv(ibs.ENV_URL, given)
    with pytest.raises(ibs.ConfigError):
        ibs.status_url()
    view = ibs.overview(NOW)
    assert view["health"] == "invalid" and view["configured"] is True
    assert "secret" not in json.dumps(view)


# ---- reading the document ----


def test_a_healthy_idle_worker(worker):
    view = ibs.overview(NOW)
    assert view["reachable"] is True and view["health"] == "ok"
    assert view["state"] == "idle" and view["worker"] == "183-Debian-Ginn-Imagebuild"
    assert view["latest"]["version"] == "1.3.7"
    assert view["latest"]["age_seconds"] == 3 * 3600
    assert view["latest"]["acceptance"] == {"passed": 37, "total": 37}
    assert "1.3.7" in view["message"] and "published" in view["message"]


@pytest.mark.parametrize("result", ["failed", "blocked", "interrupted"])
def test_a_bad_result_needs_attention_even_while_building(worker, result):
    worker.serve(
        document(
            latest=build(result=result, reason="acceptance failed"),
            state="building",
            active={
                "run_id": "r1",
                "release": "1.3.8",
                "phase": "P13",
                "started_at": "2026-11-24T11:00:00Z",
            },
        )
    )
    view = ibs.overview(NOW)
    assert view["health"] == "attention" and result in view["message"]
    assert view["latest"]["reason"] == "acceptance failed"


def test_building_and_paused_are_information(worker):
    worker.serve(
        document(
            state="building",
            active={
                "run_id": "r1",
                "release": "1.3.8",
                "phase": "P11",
                "started_at": None,
            },
        )
    )
    view = ibs.overview(NOW)
    assert view["health"] == "info" and "1.3.8" in view["message"]
    assert view["active"] == {
        "run_id": "r1",
        "release": "1.3.8",
        "phase": "P11",
        "started_at": None,
    }
    ibs.reset_cache()
    worker.serve(document(state="paused", paused=True))
    view = ibs.overview(NOW)
    assert (
        view["health"] == "info"
        and "paused" in view["message"]
        and view["paused"] is True
    )


def test_deferred_is_information_and_no_build_yet_is_unknown(worker):
    worker.serve(document(latest=build(result="deferred", reason="lab busy")))
    assert ibs.overview(NOW)["health"] == "info"
    ibs.reset_cache()
    worker.serve(document(latest=None))
    view = ibs.overview(NOW)
    assert (
        view["health"] == "unknown" and view["latest"] is None and view["builds"] == []
    )


def test_only_known_fields_survive_and_texts_are_cleaned():
    messy = build(
        version="1.3.7",
        result="exploded",
        reason="line one\x00\x07\nline two " + "x" * 500,
        secret_password="hunter2",
    )
    doc = document(
        latest=messy, builds=[messy, build(version="../../etc/passwd"), "junk", None]
    )
    doc["worker"] = "w" * 500
    doc["esxi_password"] = "hunter2"
    view = ibs.normalise(doc, NOW)
    assert "hunter2" not in json.dumps(view)
    assert view["latest"]["result"] == "unknown"
    assert (
        "\x00" not in view["latest"]["reason"]
        and len(view["latest"]["reason"]) == ibs.MAX_TEXT
    )
    assert [b["version"] for b in view["builds"]] == [
        "1.3.7"
    ]  # the path-like version and the junk entries are dropped
    assert len(view["worker"]) == 80


def test_at_most_twenty_builds_and_bad_acceptance_numbers_are_dropped():
    many = [build(version=f"1.3.{i}") for i in range(30)]
    view = ibs.normalise(document(builds=many), NOW)
    assert len(view["builds"]) == ibs.MAX_BUILDS
    for bad in (
        {"passed": 38, "total": 37},
        {"passed": True, "total": 1},
        {"passed": -1, "total": 3},
        "37/37",
        None,
    ):
        assert (
            ibs.normalise(document(latest=build(acceptance=bad)), NOW)["latest"][
                "acceptance"
            ]
            is None
        )


def test_the_age_comes_from_this_machines_clock_and_a_future_time_is_not_shown():
    view = ibs.normalise(document(latest=build(finished="2026-11-24T11:59:00Z")), NOW)
    assert view["latest"]["age_seconds"] == 60
    future = ibs.normalise(document(latest=build(finished="2026-11-25T12:00:00Z")), NOW)
    assert future["latest"]["age_seconds"] is None
    garbage = ibs.normalise(document(latest=build(finished="yesterday")), NOW)
    assert (
        garbage["latest"]["finished_at"] is None
        and garbage["latest"]["age_seconds"] is None
    )


@pytest.mark.parametrize("bad", [None, [], "text", {"schema": 2}, {"state": "idle"}])
def test_only_a_schema_one_document_is_accepted(bad):
    with pytest.raises(ValueError):
        ibs.normalise(bad, NOW)


# ---- the worker misbehaves ----


def test_a_worker_that_is_down_gives_a_sentence_not_an_error(monkeypatch):
    monkeypatch.setenv(ibs.ENV_URL, "http://127.0.0.1:1")  # nothing listens there
    view = ibs.overview(NOW)
    assert view["health"] == "unreachable" and view["reachable"] is False
    assert "cannot be reached" in view["message"] and "127.0.0.1" not in view["message"]


def test_an_error_status_a_redirect_and_garbage(worker):
    worker.status = 500
    assert "HTTP 500" in ibs.overview(NOW)["message"]
    ibs.reset_cache()
    worker.status = 302
    worker.headers = {"Location": "http://example.invalid/"}
    view = ibs.overview(NOW)
    assert view["health"] == "unreachable" and "HTTP 302" in view["message"]
    ibs.reset_cache()
    worker.status, worker.headers, worker.body = 200, {}, b"<html>not json</html>"
    assert ibs.overview(NOW)["health"] == "invalid"
    ibs.reset_cache()
    worker.body = b'{"schema": 7}'
    view = ibs.overview(NOW)
    assert view["health"] == "invalid" and "unexpected" in view["message"]


def test_a_slow_worker_times_out(worker, monkeypatch):
    monkeypatch.setattr(ibs, "TIMEOUT_SECONDS", 0.3)
    worker.delay = 1.0
    view = ibs.overview(NOW)
    assert view["health"] == "unreachable" and "in time" in view["message"]


def test_an_oversized_answer_is_refused(worker, monkeypatch):
    monkeypatch.setattr(ibs, "MAX_BYTES", 2000)
    worker.serve(document(builds=[build(reason="y" * 190) for _ in range(20)]))
    view = ibs.overview(NOW)
    assert view["health"] == "invalid" and "too large" in view["message"]


def test_the_answer_is_cached_for_a_few_seconds(worker):
    first = ibs.overview(NOW)
    second = ibs.overview(NOW)
    assert worker.requests == 1 and first["latest"] == second["latest"]
    ibs.reset_cache()
    ibs.overview(NOW)
    assert worker.requests == 2


# ---- the route ----


def test_the_admin_sees_the_section(gateway_client, worker):
    response = gateway_client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "ok" and body["latest"]["version"] == "1.3.7"


def test_the_admin_sees_that_nothing_is_set_up(gateway_client):
    body = gateway_client.get(URL).json()
    assert body["health"] == "not_configured" and body["configured"] is False


@pytest.mark.parametrize("fixture", ["gateway_user_client", "gateway_readonly_client"])
def test_only_an_admin_may_look(fixture, request, worker):
    assert request.getfixturevalue(fixture).get(URL).status_code == 403
    assert worker.requests == 0


def test_nobody_without_a_login_may_look(gateway_anonymous_client, worker):
    response = gateway_anonymous_client.get(URL, follow_redirects=False)
    assert response.status_code in (302, 303, 307, 401)
    assert worker.requests == 0


def test_the_page_script_and_the_section_are_served():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    html = (static / "admin.html").read_text()
    assert 'data-panel="images"' in html and "/admin-images.js" in html
    assert "loadImageBuilds()" in (static / "admin.js").read_text()
    assert (static / "admin-images.js").is_file()


def test_the_gateway_serves_the_script(gateway_client):
    response = gateway_client.get("/admin-images.js")
    assert response.status_code == 200 and "loadImageBuilds" in response.text


def test_the_gateway_serves_the_section_navigation_itself(gateway_client):
    # Without it the admin page shows no section while no host backend answers (the file used to come only from a backend).
    response = gateway_client.get("/admin-nav.js")
    assert response.status_code == 200 and "ap-nav" in response.text
