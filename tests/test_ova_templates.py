from unittest.mock import MagicMock, patch

from app import ova_templates


def test_public_catalog_omits_paths_filenames_and_secrets():
    raw = {
        "id": "template-1",
        "name": "GNS3 VM",
        "file": "private.ova",
        "_source_dir": "/srv/private",
        "passwords": ["secret"],
        "category": "Infrastructure",
        "size": "4 GB",
        "tags": ["gns3"],
    }
    with patch("app.ova_templates._catalog", return_value=[raw]):
        result = ova_templates.list_public()

    assert result == [
        {
            "id": "template-1",
            "name": "GNS3 VM",
            "category": "Infrastructure",
            "size": "4 GB",
            "tags": ["gns3"],
        }
    ]
    assert "/srv/private" not in str(result)
    assert "private.ova" not in str(result)
    assert "secret" not in str(result)


def test_public_catalog_only_contains_gns3_vm_templates():
    catalog = [
        {"id": "host-1", "name": "Debian", "category": "Host", "tags": ["linux"]},
        {
            "id": "gns3-1",
            "name": "Controller",
            "category": "Infrastructure",
            "tags": ["gns3"],
        },
    ]
    with patch("app.ova_templates._catalog", return_value=catalog):
        result = ova_templates.list_public()

    assert [item["id"] for item in result] == ["gns3-1"]


def test_download_uses_resource_id_and_removes_temporary_file():
    response = MagicMock()
    response.status_code = 200
    response.iter_content.return_value = [b"ova-data"]
    response.__enter__.return_value = response
    with patch("app.ova_templates.requests.get", return_value=response) as get:
        with ova_templates.downloaded("template-1") as filename:
            with open(filename, "rb") as stream:
                assert stream.read() == b"ova-data"
        assert not ova_templates.Path(filename).exists()

    assert get.call_args.args[0].endswith("/api/download/template-1")


def test_ova_template_endpoint_returns_only_sanitized_metadata(gateway_client):
    # /ova-templates moved to the gateway - see app/routes_gateway.py.
    with patch(
        "app.ova_templates.list_public",
        return_value=[{"id": "template-1", "name": "GNS3 VM"}],
    ):
        response = gateway_client.get("/ova-templates")

    assert response.status_code == 200
    assert response.json() == {"templates": [{"id": "template-1", "name": "GNS3 VM"}]}


def _response(length=None, chunks=(b"data",)):
    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-length": str(length)} if length is not None else {}
    response.iter_content.return_value = list(chunks)
    response.__enter__.return_value = response
    return response


def test_download_goes_to_the_configured_directory(tmp_path, monkeypatch):
    target = tmp_path / "big-disk"
    monkeypatch.setenv("TOPOLOGYOPERATOR_DOWNLOAD_DIR", str(target))
    with patch("app.ova_templates.requests.get", return_value=_response(4)):
        with ova_templates.downloaded("template-1") as filename:
            assert ova_templates.Path(filename).parent == target


def test_download_refuses_up_front_when_the_disk_is_too_small(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("TOPOLOGYOPERATOR_DOWNLOAD_DIR", str(tmp_path))
    huge = 10**15
    with patch("app.ova_templates.requests.get", return_value=_response(huge)):
        with pytest.raises(RuntimeError, match="Not enough free space") as error:
            with ova_templates.downloaded("template-1"):
                raise AssertionError("must not get here")
    assert "TOPOLOGYOPERATOR_DOWNLOAD_DIR" in str(error.value)
    assert not list(tmp_path.glob("topologyoperator-ova-*"))


def test_a_write_error_becomes_a_runtime_error_and_leaves_no_file(
    tmp_path, monkeypatch
):
    import pytest

    monkeypatch.setenv("TOPOLOGYOPERATOR_DOWNLOAD_DIR", str(tmp_path))
    response = _response(4)
    response.iter_content.side_effect = OSError(28, "No space left on device")
    with patch("app.ova_templates.requests.get", return_value=response):
        with pytest.raises(RuntimeError, match="Could not store"):
            with ova_templates.downloaded("template-1"):
                raise AssertionError("must not get here")
    assert not list(tmp_path.glob("topologyoperator-ova-*"))


def test_filename_for_returns_the_catalog_file_and_rejects_unknown_ids():
    import pytest

    catalog = [{"id": "ABC-1", "name": "gns3-vm", "file": "GNS3_VM_ABC-1.ova"}]
    with patch("app.ova_templates._catalog", return_value=catalog):
        assert ova_templates.filename_for("ABC-1") == "GNS3_VM_ABC-1.ova"
        with pytest.raises(RuntimeError, match="not in the template catalog"):
            ova_templates.filename_for("OTHER")
