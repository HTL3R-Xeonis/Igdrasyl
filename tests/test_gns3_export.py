import io
import zipfile
from unittest.mock import Mock

import pytest
import requests
from src.settings import Settings

from app import gns3_export

_EXPORT_URL = "/export/gns3project"


@pytest.fixture
def gns3_controller(monkeypatch, app_state):
    app_state.gns3_vm_ip = "192.0.2.1"
    app_state.project_id = "gns3-project-id"
    app_state.project_name = "Office East"
    controller = Mock()
    monkeypatch.setattr(gns3_export.requests, "get", controller)
    return controller


def test_native_archive_is_streamed_without_repackaging(client, gns3_controller):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("project.gns3", '{"name":"Office East"}')
    data = buffer.getvalue()
    # A streamed response need not have .content or a Content-Length.
    upstream = Mock(spec=["status_code", "iter_content", "close"])
    upstream.status_code = 200
    upstream.iter_content.return_value = iter([data[:23], data[23:]])
    gns3_controller.return_value = upstream

    response = client.get(_EXPORT_URL)

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "application/gns3project"
    assert response.headers["content-disposition"] == (
        'attachment; filename="Office_East.gns3project"'
    )
    gns3_controller.assert_called_once_with(
        f"http://192.0.2.1:{Settings.GNS3.PORT}/v2/projects/gns3-project-id/export",
        stream=True,
        timeout=15,
        allow_redirects=False,
    )
    upstream.iter_content.assert_called_once_with(chunk_size=64 * 1024)
    upstream.close.assert_called_once()


def test_no_gns3_project_does_not_contact_the_controller(
    client, app_state, gns3_controller
):
    app_state.project_id = None
    response = client.get(_EXPORT_URL)
    assert response.status_code == 503
    assert "No live GNS3 project" in response.json()["detail"]
    gns3_controller.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(requests.ReadTimeout, 504), (requests.ConnectionError, 502)],
)
def test_gns3_connection_error_is_reported(
    client, gns3_controller, failure, expected_status
):
    gns3_controller.side_effect = failure("private-controller")
    response = client.get(_EXPORT_URL)
    assert response.status_code == expected_status
    assert "private-controller" not in response.text


@pytest.mark.parametrize(
    ("upstream_status", "expected"), [(302, 502), (409, 409), (404, 404), (500, 502)]
)
def test_gns3_refusal_does_not_start_a_download(
    client, gns3_controller, upstream_status, expected
):
    upstream = gns3_controller.return_value
    upstream.status_code = upstream_status
    response = client.get(_EXPORT_URL)
    assert response.status_code == expected
    if upstream_status == 409:
        assert "stop them and try again" in response.json()["detail"]
    assert "content-disposition" not in response.headers
    upstream.iter_content.assert_not_called()
    upstream.close.assert_called_once()


def test_upstream_is_closed_if_stream_breaks_mid_download(client, gns3_controller):
    def fail_midstream(*, chunk_size):
        yield b"first chunk"
        raise requests.exceptions.ChunkedEncodingError("connection lost")

    upstream = gns3_controller.return_value
    upstream.status_code = 200
    upstream.iter_content.side_effect = fail_midstream
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        client.get(_EXPORT_URL)
    upstream.close.assert_called_once()
