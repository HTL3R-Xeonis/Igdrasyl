"""The admin panel's Updates section: what the appliance's updater left, the admin-only check/apply requests, the audit log."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import audit_log, update_panel

URL = "/admin/updates"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def stamp(delta_seconds: float = 0) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=delta_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def component(version: str) -> dict:
    return {"version": version, "tag": f"v{version}"}


INSTALLED = {
    "schema": 1,
    "appliance": {"version": "1.3.0", "tag": "v1.3.0", "channel": "stable"},
    "components": {
        "appliance": component("1.3.0"),
        "operator": component("1.2.1"),
        "builder": component("1.0.0"),
        "template-services": component("1.0.0"),
        "generator": component("1.0.0"),
    },
    "installed_at": "2026-12-01T10:00:00Z",
    "adopted": False,
}


def change(name, old, new, kind, services):
    return {
        "component": name,
        "installed": old,
        "available": new,
        "change": kind,
        "services": services,
    }


AVAILABLE = {
    "checked_at": "2026-12-02T08:00:00Z",
    "source": "github HTL3R-Xeonis/Ginnungagap-Appliance",
    "channel": "stable",
    "update_available": True,
    "installed": "1.3.0",
    "available": {
        "version": "1.4.0",
        "tag": "v1.4.0",
        "channel": "stable",
        "released": "2026-12-02T07:00:00Z",
        "notes": "",
        "data_migration": False,
    },
    "signature": "valid (key SHA256:abc)",
    "changes": [
        change("appliance", "1.3.0", "1.4.0", "minor", ["setup"]),
        change("operator", "1.2.1", "1.3.0", "minor", ["ginnungagap"]),
        change("builder", "1.0.0", "1.0.0", "same", []),
        change("template-services", "1.0.0", "1.0.1", "patch", ["template-esxi"]),
        change("generator", "1.0.0", "1.0.0", "same", []),
    ],
    "error": None,
}


class Panel:
    def __init__(self, root: Path) -> None:
        self.state = root / "update"
        self.requests = root / "update-requests"
        self.state.mkdir()
        self.requests.mkdir()

    def put(self, name: str, data) -> None:
        (self.state / name).write_text(
            data if isinstance(data, str) else json.dumps(data)
        )

    def request(self) -> dict | None:
        path = self.requests / "request.json"
        return json.loads(path.read_text()) if path.exists() else None

    def leftovers(self) -> list[str]:
        return sorted(
            p.name for p in self.requests.iterdir() if p.name != "request.json"
        )


@pytest.fixture
def panel(tmp_path, monkeypatch):
    folders = Panel(tmp_path)
    monkeypatch.setenv(update_panel.STATE_ENV, str(folders.state))
    monkeypatch.setenv(update_panel.REQUEST_ENV, str(folders.requests))
    folders.put("installed.json", INSTALLED)
    return folders


@pytest.fixture
def checked(panel):
    panel.put("available.json", AVAILABLE)
    return panel


def events(name: str | None = None) -> list[dict]:
    found = audit_log.recent()
    return [e for e in found if name is None or e["event"] == name]


# ---- where it is not available ----


def test_without_the_appliance_folders_the_section_says_so_and_offers_nothing(
    client, monkeypatch
):
    monkeypatch.delenv(update_panel.STATE_ENV, raising=False)
    monkeypatch.delenv(update_panel.REQUEST_ENV, raising=False)
    view = client.get(URL).json()
    assert view["enabled"] is False and "appliance" in view["reason"]
    assert client.post(URL + "/check").status_code == 503
    body = {"version": "1.4.0", "confirm": True}
    assert client.post(URL + "/apply", json=body).status_code == 503


def test_folders_that_are_named_but_not_mounted_are_reported(
    client, tmp_path, monkeypatch
):
    monkeypatch.setenv(update_panel.STATE_ENV, str(tmp_path / "missing"))
    monkeypatch.setenv(update_panel.REQUEST_ENV, str(tmp_path / "also-missing"))
    view = client.get(URL).json()
    assert view["enabled"] is False and "not mounted" in view["reason"]
    assert client.post(URL + "/check").status_code == 503


# ---- admin only ----


@pytest.mark.parametrize(
    "fixture",
    [
        "user_client",
        "readonly_client",
        "gateway_user_client",
        "gateway_readonly_client",
    ],
)
def test_only_an_admin_may_look_or_ask(fixture, request, checked):
    other = request.getfixturevalue(fixture)
    assert other.get(URL).status_code == 403
    assert other.post(URL + "/check").status_code == 403
    assert (
        other.post(
            URL + "/apply", json={"version": "1.4.0", "confirm": True}
        ).status_code
        == 403
    )
    assert checked.request() is None and events() == []


def test_the_section_is_served_by_the_gateway_too(gateway_client, checked):
    assert gateway_client.get(URL).json()["enabled"] is True
    assert gateway_client.post(URL + "/check").status_code == 202
    assert gateway_client.get("/admin-updates.js").status_code == 200


def test_nobody_logged_out_can_use_it(app_state, checked):
    from fastapi.testclient import TestClient

    import app.main as main_module

    anonymous = TestClient(main_module.app)
    assert anonymous.get(URL).status_code == 401
    assert anonymous.post(URL + "/check").status_code == 401
    assert checked.request() is None


# ---- what is shown ----


def test_current_and_available_versions_come_with_the_code_names(client, checked):
    view = client.get(URL).json()
    assert view["enabled"] is True and view["can_apply"] is True and not view["busy"]
    assert view["release"] == {
        "version": "1.3.0",
        "adopted": False,
        "installed_at": "2026-12-01T10:00:00Z",
    }
    rows = {row["key"]: row for row in view["components"]}
    assert [row["name"] for row in view["components"]] == [
        "Ginnungagap appliance",
        "Igdrasyl",
        "Mjonyr",
        "Hugyn and Munyn",
        "Mimyr",
    ]
    assert (
        rows["operator"]["installed"],
        rows["operator"]["available"],
        rows["operator"]["change"],
    ) == ("1.2.1", "1.3.0", "minor")
    assert rows["operator"]["restarts_web_app"] is True and rows["operator"][
        "services"
    ] == ["ginnungagap"]
    assert rows["builder"]["change"] == "same" and rows["builder"]["services"] == []
    assert rows["template-services"]["restarts_web_app"] is False
    assert view["check"]["version"] == "1.4.0" and view["check"][
        "signature"
    ].startswith("valid")


def test_before_any_check_only_the_installed_versions_show(client, panel):
    view = client.get(URL).json()
    assert view["check"] is None and view["can_apply"] is False
    assert all(row["available"] is None for row in view["components"])
    assert {row["key"]: row["installed"] for row in view["components"]}[
        "operator"
    ] == "1.2.1"


def test_a_machine_with_no_installed_state_still_shows_the_section(client, panel):
    (panel.state / "installed.json").unlink()
    view = client.get(URL).json()
    assert view["enabled"] and view["release"]["version"] is None
    assert all(row["installed"] is None for row in view["components"])


def test_adopted_checkouts_are_no_release(client, panel):
    panel.put(
        "installed.json",
        {**INSTALLED, "appliance": {"version": "adopted"}, "adopted": True},
    )
    release = client.get(URL).json()["release"]
    assert release["adopted"] is True and release["version"] is None


def test_a_failed_check_is_shown_and_offers_no_update(client, panel):
    panel.put(
        "available.json",
        {
            "checked_at": "2026-12-02T08:00:00Z",
            "update_available": False,
            "error": "GitHub answered 401: the token is not valid",
        },
    )
    view = client.get(URL).json()
    assert "401" in view["check"]["error"] and view["can_apply"] is False


def test_an_error_always_wins_over_an_available_flag(client, panel):
    panel.put("available.json", {**AVAILABLE, "error": "boom"})
    assert client.get(URL).json()["can_apply"] is False


def test_nothing_to_update_offers_no_button(client, panel):
    panel.put("available.json", {**AVAILABLE, "update_available": False})
    assert client.get(URL).json()["can_apply"] is False


def test_a_check_older_than_the_installed_release_is_out_of_date(client, checked):
    # an update or a rollback ran after the check: nothing is on offer any more, and nothing can be installed from it
    checked.put("installed.json", {**INSTALLED, "installed_at": "2026-12-03T09:00:00Z"})
    view = client.get(URL).json()
    assert view["check"]["stale"] is True and view["check"]["update_available"] is False
    assert view["can_apply"] is False
    assert all(row["available"] is None for row in view["components"])
    body = {"version": "1.4.0", "confirm": True}
    assert client.post(URL + "/apply", json=body).status_code == 409
    assert checked.request() is None


def test_a_check_newer_than_the_installed_release_is_current(client, checked):
    assert client.get(URL).json()["check"]["stale"] is False


# ---- the request: check ----


def test_a_check_leaves_one_request_for_the_host_and_an_audit_event(client, checked):
    reply = client.post(URL + "/check")
    assert reply.status_code == 202
    sent = checked.request()
    assert reply.json() == {"id": sent["id"]}
    assert re.fullmatch(r"[0-9a-f]{16}", sent["id"])
    assert set(sent) == {"id", "action", "requested_by", "role", "at"}
    assert (sent["action"], sent["requested_by"], sent["role"]) == (
        "check",
        "test-admin",
        "admin",
    )
    assert (
        checked.leftovers() == []
    )  # the file appeared all at once, no temp file stays
    assert oct((checked.requests / "request.json").stat().st_mode & 0o777) == "0o644"
    logged = events("update_check_requested")
    assert len(logged) == 1 and logged[0]["username"] == "test-admin"
    assert sent["id"] in logged[0]["detail"]


def test_a_check_makes_no_change_to_the_state_it_may_only_read(client, checked):
    before = {p.name: p.read_bytes() for p in checked.state.iterdir()}
    client.post(URL + "/check")
    client.get(URL)
    assert {p.name: p.read_bytes() for p in checked.state.iterdir()} == before


# ---- the request: apply ----


def test_installing_needs_the_confirmed_version_of_the_last_check(client, checked):
    reply = client.post(URL + "/apply", json={"version": "1.4.0", "confirm": True})
    assert reply.status_code == 202
    sent = checked.request()
    assert reply.json() == {"id": sent["id"]}
    assert (sent["action"], sent["version"], sent["confirmed"]) == (
        "apply",
        "1.4.0",
        True,
    )
    assert sent["requested_by"] == "test-admin" and sent["role"] == "admin"
    assert set(sent) == {
        "id",
        "action",
        "requested_by",
        "role",
        "at",
        "version",
        "confirmed",
    }
    logged = events("update_apply_requested")
    assert len(logged) == 1 and logged[0]["username"] == "test-admin"
    assert "1.4.0" in logged[0]["detail"] and sent["id"] in logged[0]["detail"]


def test_without_an_explicit_yes_nothing_is_installed(client, checked):
    assert (
        client.post(
            URL + "/apply", json={"version": "1.4.0", "confirm": False}
        ).status_code
        == 400
    )
    assert client.post(URL + "/apply", json={"version": "1.4.0"}).status_code == 422
    assert (
        client.post(
            URL + "/apply", json={"version": "1.4.0", "confirm": "yes"}
        ).status_code
        == 422
    )
    assert client.post(URL + "/apply").status_code == 422
    assert checked.request() is None and events() == []


def test_a_release_that_is_not_the_one_the_check_found_is_refused(client, checked):
    reply = client.post(URL + "/apply", json={"version": "1.5.0", "confirm": True})
    assert (
        reply.status_code == 409 and "Check for updates again" in reply.json()["detail"]
    )
    assert checked.request() is None


def test_installing_without_a_check_or_after_a_failed_one_is_refused(client, panel):
    body = {"version": "1.4.0", "confirm": True}
    assert client.post(URL + "/apply", json=body).status_code == 409
    panel.put("available.json", {**AVAILABLE, "error": "boom"})
    assert client.post(URL + "/apply", json=body).status_code == 409
    panel.put("available.json", {**AVAILABLE, "update_available": False})
    assert client.post(URL + "/apply", json=body).status_code == 409
    assert panel.request() is None


@pytest.mark.parametrize(
    "extra",
    ["force", "no_backup", "allow_downgrade", "offline_dir", "component", "source"],
)
def test_the_browser_can_not_ask_for_anything_but_the_confirmed_release(
    client, checked, extra
):
    body = {"version": "1.4.0", "confirm": True, extra: True}
    assert client.post(URL + "/apply", json=body).status_code == 422
    assert checked.request() is None


def test_a_version_that_is_not_a_version_number_is_never_written(client, panel):
    panel.put(
        "available.json",
        {
            **AVAILABLE,
            "available": {**AVAILABLE["available"], "version": "latest; rm -rf /"},
        },
    )
    assert client.get(URL).json()["can_apply"] is False
    body = {"version": "latest; rm -rf /", "confirm": True}
    assert client.post(URL + "/apply", json=body).status_code in (400, 409)
    assert panel.request() is None


def test_installing_is_never_started_by_looking_or_by_checking(client, checked):
    client.get(URL)
    client.post(URL + "/check")
    assert checked.request()["action"] == "check"
    assert events("update_apply_requested") == []


# ---- busy ----


def test_a_second_request_waits_while_one_is_pending_running_or_installing(
    client, checked
):
    body = {"version": "1.4.0", "confirm": True}
    assert client.post(URL + "/check").status_code == 202
    assert client.post(URL + "/check").status_code == 409  # the first is still there
    assert client.post(URL + "/apply", json=body).status_code == 409
    (checked.requests / "request.json").unlink()
    checked.put("transaction.json", {"phase": "built"})
    assert client.post(URL + "/check").status_code == 409
    assert client.post(URL + "/apply", json=body).status_code == 409
    (checked.state / "transaction.json").unlink()
    checked.put("web-status.json", {"id": "r1", "state": "running", "started": stamp()})
    assert client.post(URL + "/check").status_code == 409
    assert client.get(URL).json()["can_apply"] is False
    checked.put(
        "web-status.json",
        {"id": "r1", "state": "done", "started": stamp(), "finished": stamp()},
    )
    assert client.post(URL + "/check").status_code == 202


def test_a_request_the_host_does_not_pick_up_is_reported_as_overdue(client, checked):
    client.post(URL + "/check")
    assert client.get(URL).json()["request_pending"] == {"overdue": False}
    old = time.time() - 300
    os.utime(checked.requests / "request.json", (old, old))
    view = client.get(URL).json()
    assert view["request_pending"] == {"overdue": True} and view["busy"] is True


def test_the_progress_of_a_running_update_comes_from_its_journal(client, checked):
    checked.put(
        "transaction.json",
        {
            "phase": "built",
            "started": "2026-12-02T08:05:00Z",
            "requester": "web:alice",
            "to": {"appliance": {"version": "1.4.0"}},
        },
    )
    view = client.get(URL).json()
    assert view["busy"] is True and view["can_apply"] is False
    assert view["progress"] == {
        "phase": "built",
        "label": "Images built",
        "step": 7,
        "steps": 8,
        "rolling_back": False,
        "failed": False,
        "to": "1.4.0",
        "started": "2026-12-02T08:05:00Z",
        "requested_by": "web:alice",
    }


@pytest.mark.parametrize(
    "phase, rolling, failed",
    [
        ("rolling_back", True, False),
        ("rollback_failed", True, True),
        ("restarting", False, False),
    ],
)
def test_a_rollback_is_shown_as_one(client, checked, phase, rolling, failed):
    checked.put("transaction.json", {"phase": phase})
    progress = client.get(URL).json()["progress"]
    assert (progress["rolling_back"], progress["failed"]) == (rolling, failed)


def test_an_unknown_phase_is_shown_as_work_in_progress(client, checked):
    checked.put("transaction.json", {"phase": "something-new"})
    progress = client.get(URL).json()["progress"]
    assert progress["label"] == "Working" and progress["step"] is None


def test_a_status_that_says_running_for_hours_with_no_journal_is_an_interrupted_run(
    client, checked
):
    checked.put(
        "web-status.json",
        {"id": "r1", "action": "apply", "state": "running", "started": stamp(-7200)},
    )
    view = client.get(URL).json()
    assert view["busy"] is False
    assert (
        view["result"]["state"] == "failed"
        and view["result"]["outcome"] == "interrupted"
    )
    checked.put(
        "web-status.json", {"id": "r1", "state": "running", "started": stamp(-30)}
    )
    assert client.get(URL).json()["busy"] is True


# ---- the result, and the audit log ----


RESULT = {
    "id": "0123456789abcdef",
    "action": "apply",
    "requested_by": "alice",
    "version": "1.4.0",
    "state": "done",
    "outcome": "installed",
    "started": "2026-12-02T08:05:00Z",
    "finished": "2026-12-02T08:12:00Z",
    "exit_code": 0,
    "message": "Updated to release 1.4.0.",
    "log": ["Downloading ...", "Installed: appliance v1.4.0."],
}


def test_the_result_of_the_last_request_is_shown(client, checked):
    checked.put("web-status.json", RESULT)
    view = client.get(URL).json()
    result = view["result"]
    assert (result["state"], result["outcome"], result["requested_by"]) == (
        "done",
        "installed",
        "alice",
    )
    assert result["message"] == "Updated to release 1.4.0." and result["log"][
        -1
    ].startswith("Installed")
    assert view["busy"] is False


def test_the_result_joins_the_audit_log_once_with_the_name_of_who_asked(
    client, checked
):
    checked.put("web-status.json", RESULT)
    client.get(URL)
    client.get(URL)
    found = events("update_result")
    assert len(found) == 1 and found[0]["username"] == "alice"
    assert (
        "apply 1.4.0: installed" in found[0]["detail"]
        and "Updated to release 1.4.0." in found[0]["detail"]
    )
    checked.put(
        "web-status.json",
        {
            **RESULT,
            "id": "fedcba9876543210",
            "action": "check",
            "version": None,
            "outcome": "up_to_date",
            "message": "Everything is up to date.",
        },
    )
    client.get(URL)
    assert len(events("update_result")) == 2


def test_a_run_that_is_still_going_is_not_recorded_yet(client, checked):
    checked.put("web-status.json", {**RESULT, "state": "running", "outcome": None})
    client.get(URL)
    assert events("update_result") == []


def test_a_failed_and_a_refused_result_are_recorded_too(client, checked):
    checked.put(
        "web-status.json",
        {
            **RESULT,
            "state": "failed",
            "outcome": "rolled_back",
            "message": "The update failed and was rolled back.",
        },
    )
    client.get(URL)
    assert "rolled_back" in events("update_result")[0]["detail"]


def test_a_requester_name_that_is_not_a_user_name_is_not_trusted_into_the_log(
    client, checked
):
    checked.put("web-status.json", {**RESULT, "requested_by": "x\nforged line"})
    client.get(URL)
    assert events("update_result")[0]["username"] == "unknown"


def test_the_audit_log_shows_up_in_the_admin_audit_endpoint(client, checked):
    client.post(URL + "/check")
    names = [e["event"] for e in client.get("/admin/audit-log").json()]
    assert "update_check_requested" in names


# ---- the folders hold untrusted text ----


def test_a_symlinked_or_oversized_or_broken_state_file_is_ignored(
    client, panel, tmp_path
):
    secret = tmp_path / "elsewhere.json"
    secret.write_text(json.dumps(AVAILABLE))
    (panel.state / "available.json").symlink_to(secret)
    assert client.get(URL).json()["check"] is None
    (panel.state / "available.json").unlink()
    panel.put("available.json", "x" * (update_panel.MAX_STATE_BYTES + 10))
    assert client.get(URL).json()["check"] is None
    panel.put("available.json", "{not json")
    assert client.get(URL).json()["check"] is None
    panel.put("available.json", "[1, 2]")
    assert client.get(URL).json()["check"] is None
    panel.put("installed.json", "{not json")
    assert client.get(URL).json()["release"]["version"] is None


def test_only_known_fields_come_through_and_text_is_cut_and_cleaned(client, panel):
    hostile = {
        **AVAILABLE,
        "secret": "ghp_secret",
        "source": "a" * 5000,
        "signature": "valid\n\n\x1b[31mred",
        "changes": [
            *AVAILABLE["changes"],
            change("not-a-component", "1", "2", "major", ["x"]),
            {"component": "operator", "services": "ginnungagap"},
            "nonsense",
        ],
    }
    panel.put("available.json", hostile)
    panel.put(
        "web-status.json",
        {
            **RESULT,
            "log": [f"line {n}" for n in range(500)],
            "message": "m" * 5000,
            "token": "ghp_x",
        },
    )
    view = client.get(URL).json()
    text = json.dumps(view)
    assert "ghp_" not in text and "not-a-component" not in text
    assert (
        len(view["check"]["source"]) <= 200
        and len(view["result"]["message"]) <= update_panel.MAX_TEXT
    )
    assert "\n" not in view["check"]["signature"]
    assert len(view["result"]["log"]) == update_panel.MAX_LOG_LINES
    assert view["result"]["log"][-1] == "line 499"


def test_an_unknown_state_word_is_shown_as_failed(client, checked):
    checked.put("web-status.json", {**RESULT, "state": "banana"})
    assert client.get(URL).json()["result"]["state"] == "failed"


def test_a_request_that_can_not_be_left_is_reported_not_swallowed(client, checked):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    checked.requests.chmod(0o500)
    try:
        reply = client.post(URL + "/check")
    finally:
        checked.requests.chmod(0o700)
    assert reply.status_code == 503 and events("update_check_requested") == []


def test_no_temp_file_is_left_when_the_rename_fails(checked, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(update_panel.os, "replace", broken)
    with pytest.raises(update_panel.PanelError):
        update_panel.write_request("check", "alice")
    assert checked.leftovers() == [] and checked.request() is None


def test_a_user_name_outside_the_user_name_rule_is_never_written(checked):
    with pytest.raises(update_panel.PanelError):
        update_panel.write_request("check", "a b")
    with pytest.raises(update_panel.PanelError):
        update_panel.write_request("rollback", "alice")
    assert checked.request() is None


# ---- the page ----


def admin_html() -> str:
    return (STATIC / "admin.html").read_text(encoding="utf-8")


def test_the_page_has_the_updates_section_with_the_two_buttons_and_a_confirmation():
    html = admin_html()
    assert 'data-panel="updates"' in html and 'href="#updates"' in html
    assert ">Check for updates<" in html and ">Update now" in html
    assert (
        '<dialog id="ap-updates-dialog"' in html and 'id="ap-updates-install"' in html
    )
    for script in ("connection-monitor.js", "info-popover.js", "admin-updates.js"):
        assert f'src="/{script}?v=ginnungagap-' in html


def test_all_the_explanation_sits_behind_the_info_icon():
    html = admin_html()
    start = html.index('<section id="ap-updates-section"')
    section = html[start : html.index("</section>", start)]
    title = section[: section.index("</h2>")]
    assert 'class="info-btn"' in title and 'aria-label="More information"' in title
    assert "Idynoun" in title and "Nothing is installed automatically" in title
    assert "title=" not in section and "placeholder" not in section.lower()
    assert "<p>" not in section  # every permanent line is a status line with an id


def test_the_updater_is_named_in_the_panel_and_not_in_the_overlay_or_with_hover_effects():
    html = admin_html()
    overlay = html[
        html.index('<div id="connection-lost"') : html.index('<p id="cl-restored"')
    ]
    assert "Idynoun" not in overlay
    source = (STATIC / "admin-updates.js").read_text(encoding="utf-8")
    assert not re.search(r"hover|mouseover|tooltip|\.title\s*=", source)


def test_the_overlay_covers_the_admin_page_and_the_scripts_share_one_cache_token():
    html = admin_html()
    assert 'id="connection-lost"' in html and 'id="cl-retry"' in html
    assert '"ap"' in (STATIC / "connection-monitor.js").read_text(encoding="utf-8")
    tokens = set(re.findall(r"\?v=(ginnungagap-\d+)", html))
    assert len(tokens) == 1, tokens
    pages = [(STATIC / "index.html"), (STATIC / "logs.html")]
    for page in pages:
        assert tokens == set(
            re.findall(r"\?v=(ginnungagap-\d+)", page.read_text(encoding="utf-8"))
        )


def test_the_page_tells_a_restart_from_an_error_and_keeps_asking():
    source = (STATIC / "admin-updates.js").read_text(encoding="utf-8")
    assert (
        "error instanceof TypeError" in source
    )  # a request that does not even arrive: the web app restarts
    assert "The web app is restarting. This page reconnects by itself." in source
    assert "UPDATES_BACKOFF_MS = [2000, 4000, 8000, 15000]" in source
    assert (
        "connectionMonitor.onRestored" in source
    )  # the overlay's "back again" asks for the state at once


def test_the_script_never_installs_without_the_confirmation_button():
    source = (STATIC / "admin-updates.js").read_text(encoding="utf-8")
    assert source.count('postUpdateRequest("apply"') == 1
    handler = source[source.index('updatesEl("ap-updates-install").addEventListener') :]
    assert handler.index('postUpdateRequest("apply"') < handler.index("});")
    assert "confirm: true" in handler
    assert "setInterval" not in source  # no timer that could ever act by itself


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_is_valid_javascript():
    result = subprocess.run(
        ["node", "--check", str(STATIC / "admin-updates.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
