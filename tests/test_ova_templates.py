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


def test_ova_template_endpoint_returns_only_sanitized_metadata(client):
    with patch(
        "app.ova_templates.list_public",
        return_value=[{"id": "template-1", "name": "GNS3 VM"}],
    ):
        response = client.get("/ova-templates")

    assert response.status_code == 200
    assert response.json() == {"templates": [{"id": "template-1", "name": "GNS3 VM"}]}
