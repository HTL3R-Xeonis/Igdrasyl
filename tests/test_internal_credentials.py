"""The collector's login lookup: only with the service token, only for a known host, never leaking a secret."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from loguru import logger

from app import internal_credentials, profile_secrets, settings_profiles

TOKEN = "t" * 40
PASSWORD = "esxi-secret-pw"
URL = "/internal/host-credentials"


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(internal_credentials.TOKEN_ENV, TOKEN)
    return TOKEN


@pytest.fixture
def host():
    settings_profiles.save_profile(
        "lab-a.yml", b"esxi:\n  ip: 10.20.20.205\n  username: admin\n"
    )
    profile_secrets.save("lab-a.yml", {"esxi_password": PASSWORD})


@pytest.fixture
def captured_logs():
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    logger.remove(handler_id)


def bearer(value=TOKEN):
    return {"Authorization": f"Bearer {value}"}


def test_without_a_configured_token_the_endpoint_is_disabled(
    gateway_anonymous_client, monkeypatch
):
    monkeypatch.delenv(internal_credentials.TOKEN_ENV, raising=False)
    resp = gateway_anonymous_client.get(URL, params={"ip": "10.20.20.205"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == internal_credentials.DISABLED_TEXT


def test_a_too_short_token_counts_as_not_configured(
    gateway_anonymous_client, monkeypatch
):
    monkeypatch.setenv(internal_credentials.TOKEN_ENV, "short")
    resp = gateway_anonymous_client.get(
        URL, params={"ip": "10.20.20.205"}, headers=bearer("short")
    )
    assert resp.status_code == 503


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": TOKEN},
    ],
)
def test_a_wrong_or_missing_token_is_refused(
    gateway_anonymous_client, token, host, headers
):
    resp = gateway_anonymous_client.get(
        URL, params={"ip": "10.20.20.205"}, headers=headers
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == internal_credentials.BAD_TOKEN_TEXT
    assert PASSWORD not in resp.text


def test_the_token_is_checked_before_anything_else(gateway_anonymous_client, token):
    resp = gateway_anonymous_client.get(URL, params={"ip": "9.9.9.9"})
    assert (
        resp.status_code == 401
    )  # an unknown ip must not be told apart without the token


def test_a_missing_ip_is_a_bad_request(gateway_anonymous_client, token):
    resp = gateway_anonymous_client.get(URL, headers=bearer())
    assert resp.status_code == 400
    assert resp.json()["detail"] == internal_credentials.MISSING_IP_TEXT


def test_an_unknown_ip_gets_a_clear_404(gateway_anonymous_client, token, host):
    resp = gateway_anonymous_client.get(
        URL, params={"ip": "10.9.9.9"}, headers=bearer()
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == internal_credentials.not_found_text("10.9.9.9")


def test_a_host_without_a_stored_password_is_a_404(gateway_anonymous_client, token):
    settings_profiles.save_profile("nopw.yml", b"esxi:\n  ip: 10.20.20.77\n")
    resp = gateway_anonymous_client.get(
        URL, params={"ip": "10.20.20.77"}, headers=bearer()
    )
    assert resp.status_code == 404


def test_a_known_host_returns_its_login(gateway_anonymous_client, token, host):
    resp = gateway_anonymous_client.get(
        URL, params={"ip": "10.20.20.205"}, headers=bearer()
    )
    assert resp.status_code == 200
    assert resp.json() == {"username": "admin", "password": PASSWORD}
    assert resp.headers["cache-control"] == "no-store"


def test_the_ip_match_ignores_case_and_surrounding_spaces(
    gateway_anonymous_client, token
):
    settings_profiles.save_profile("named.yml", b"esxi:\n  ip: ESXI-One.lab\n")
    profile_secrets.save("named.yml", {"esxi_password": PASSWORD})
    resp = gateway_anonymous_client.get(
        URL, params={"ip": " esxi-one.LAB "}, headers=bearer()
    )
    assert resp.status_code == 200 and resp.json()["username"] == "root"


def test_the_gns3_password_is_never_served(gateway_anonymous_client, token):
    settings_profiles.save_profile("both.yml", b"esxi:\n  ip: 10.1.1.1\n")
    profile_secrets.save(
        "both.yml", {"esxi_password": "esxi-pw", "gns3_password": "gns3-pw"}
    )
    resp = gateway_anonymous_client.get(
        URL, params={"ip": "10.1.1.1"}, headers=bearer()
    )
    assert "gns3-pw" not in resp.text and resp.json()["password"] == "esxi-pw"


def test_neither_the_secret_nor_the_token_reach_the_log_or_the_audit_file(
    gateway_anonymous_client, token, host, captured_logs
):
    gateway_anonymous_client.get(URL, params={"ip": "10.20.20.205"}, headers=bearer())
    gateway_anonymous_client.get(
        URL, params={"ip": "10.20.20.205"}, headers=bearer("x" * 40)
    )
    gateway_anonymous_client.get(URL, params={"ip": "10.9.9.9"}, headers=bearer())
    audit = Path(os.environ["TOPOLOGYOPERATOR_AUDIT_LOG_FILE"]).read_text(
        encoding="utf-8"
    )
    everything = "".join(captured_logs) + audit
    assert PASSWORD not in everything
    assert TOKEN not in everything and "x" * 40 not in everything
    assert '"event": "credentials_served"' in audit and "10.20.20.205" in audit
    assert (
        audit.count("credentials_served") == 1
    )  # only the successful lookup is an event


def test_every_other_path_still_needs_a_session(gateway_anonymous_client, token, host):
    for path in (
        "/admin/users",
        "/settings-profiles",
        "/pick-backend",
        "/internal/other",
    ):
        resp = gateway_anonymous_client.get(
            path, headers={"Accept": "application/json"}
        )
        assert resp.status_code == 401, path


def test_the_bearer_token_is_not_a_session(gateway_anonymous_client, token, host):
    resp = gateway_anonymous_client.get(
        "/admin/users",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_only_a_get_skips_the_session_check(gateway_anonymous_client, token):
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(gateway_anonymous_client, method)(
            URL, headers={**bearer(), "Accept": "application/json"}
        )
        assert resp.status_code == 401, method
