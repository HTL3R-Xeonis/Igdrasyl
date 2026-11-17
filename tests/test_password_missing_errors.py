"""A host without a stored password gets a clear error and no login attempt - never a pyvmomi InvalidLogin."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from src.settings import Settings

from app import esxi_session, topology_write

ESXI_TEXT = (
    "ESXi password for host 10.0.0.9 is not set - enter it in the host profile "
    "(Host settings > Credentials). No connection attempted."
)


def test_the_message_names_the_service_and_the_host():
    assert esxi_session.password_missing_message("ESXi", "10.0.0.9") == ESXI_TEXT
    assert esxi_session.password_missing_message("GNS3", "10.0.0.9").startswith(
        "GNS3 password for host 10.0.0.9 is not set"
    )


def test_a_lazy_connection_without_a_password_never_tries_the_login():
    connect = MagicMock()
    lazy = esxi_session.LazyESXiConnection(connect, host="10.0.0.9", has_password=False)

    with pytest.raises(esxi_session.EsxiUnavailable) as raised:
        lazy.connection()
    with pytest.raises(esxi_session.EsxiUnavailable):
        lazy.get_vm_ip_address("x")  # attribute access goes through connection()

    assert str(raised.value) == ESXI_TEXT
    connect.assert_not_called()


def test_the_missing_password_is_logged_once_and_without_a_value():
    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="ERROR")
    try:
        lazy = esxi_session.LazyESXiConnection(
            MagicMock(), host="10.0.0.9", has_password=False
        )
        for _ in range(3):
            with pytest.raises(esxi_session.EsxiUnavailable):
                lazy.connection()
    finally:
        logger.remove(sink)
    assert len(records) == 1 and ESXI_TEXT in records[0]


def test_with_a_password_the_connection_works_as_before():
    lazy = esxi_session.LazyESXiConnection(lambda: "real", host="10.0.0.9")
    assert lazy.connection() == "real"


@pytest.mark.parametrize(
    ("esxi", "gns3", "service"),
    [(None, "g", "ESXi"), ("", "g", "ESXi"), ("e", None, "GNS3"), ("e", "", "GNS3")],
)
def test_the_live_backend_is_not_built_without_both_passwords(
    app_state, monkeypatch, esxi, gns3, service
):
    monkeypatch.setattr(Settings.ESXI, "IP", "10.0.0.9")
    monkeypatch.setattr(Settings.ESXI, "PASSWORD", esxi)
    monkeypatch.setattr(Settings.GNS3, "PASSWORD", gns3)
    app = MagicMock()
    app.state.topology_backend = None

    with patch("app.topology_write.TopologyBackend") as backend:
        with pytest.raises(esxi_session.EsxiUnavailable) as raised:
            topology_write.get_or_create_backend(app)

    backend.assert_not_called()
    assert str(raised.value) == esxi_session.password_missing_message(
        service, "10.0.0.9"
    )
    assert "No connection attempted" in str(raised.value)


def test_the_live_backend_is_built_when_both_passwords_exist(monkeypatch):
    monkeypatch.setattr(Settings.ESXI, "PASSWORD", "e")
    monkeypatch.setattr(Settings.GNS3, "PASSWORD", "g")
    app = MagicMock()
    app.state.topology_backend = None
    with patch("app.topology_write.TopologyBackend") as backend:
        topology_write.get_or_create_backend(app)
    backend.assert_called_once()
