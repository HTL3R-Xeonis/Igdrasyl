from __future__ import annotations

import hashlib
import io
import json
import tarfile
from urllib.parse import quote

import pytest
import yaml

URL = "/admin/template-share"
FIELDS = {
    "name": "Linux lab",
    "category": "Linux",
    "os_family": "Linux",
    "os_version": "Debian 13",
    "cpu": 2,
    "memory": "4GB",
    "disk": "20GB",
    "hostname": "linux-lab",
    "tags": ["linux"],
    "users": ["operator"],
}


@pytest.fixture
def share(tmp_path, monkeypatch):
    path = tmp_path / "template-share"
    path.mkdir()
    monkeypatch.setenv("TOPOLOGYOPERATOR_TEMPLATE_SHARE_DIR", str(path))
    monkeypatch.setenv("TOPOLOGYOPERATOR_TEMPLATE_REFRESH_MINUTES", "15")
    return path


@pytest.fixture
def ova():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in [("guest.ovf", b"<Envelope/>"), ("disk.vmdk", b"disk")]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _upload(client, content, **overrides):
    header = quote(json.dumps({**FIELDS, **overrides}))
    return client.post(
        URL + "/import", content=content, headers={"X-Template-Metadata": header}
    )


def test_add_and_edit_a_template_without_an_active_host(gateway_client, share, ova):
    created = _upload(gateway_client, ova)
    assert created.status_code == 201, created.text
    template_id = hashlib.sha256(ova).hexdigest().upper()
    assert created.json() == {
        "id": template_id,
        "name": "Linux lab",
        "refresh_minutes": 15,
    }
    row = gateway_client.get(URL).json()["templates"][0]
    manifest = share / row["manifest"]
    document = yaml.safe_load(manifest.read_text())
    entry = document["templates"][0]
    assert document["version"] == 1
    assert entry["os"] == {"family": "Linux", "version": "Debian 13"}
    assert entry["file"].endswith(f"_{template_id}.ova")
    assert (manifest.parent / entry["file"]).read_bytes() == ova
    assert row["file_exists"]

    patch = {
        "manifest": row["manifest"],
        "id": row["id"],
        "revision": row["revision"],
        "metadata": {**FIELDS, "cpu": 4, "note": "Updated metadata"},
    }
    assert gateway_client.patch(URL, json=patch).status_code == 200
    assert gateway_client.patch(URL, json=patch).status_code == 409
    changed = yaml.safe_load(manifest.read_text())["templates"][0]
    assert changed == {**entry, "cpu": 4, "note": "Updated metadata"}
    assert {
        event["event"] for event in gateway_client.get("/admin/audit-log").json()
    } >= {
        "template_share_import",
        "template_share_edit",
    }


def test_import_blocks_bad_metadata_bad_archives_and_duplicates(client, share, ova):
    assert _upload(client, ova, cpu=0).status_code == 422
    assert _upload(client, ova, hostname="").status_code == 422
    assert _upload(client, b"not an archive").status_code == 400
    assert list(share.iterdir()) == []
    assert _upload(client, ova).status_code == 201
    assert _upload(client, ova, name="linux LAB").status_code == 409
    assert _upload(client, ova, name="Another template").status_code == 409
    assert [item.name for item in share.iterdir()] == ["esxi-linux-lab"]


def test_upload_limit_does_not_leave_part_files(client, share, monkeypatch, ova):
    monkeypatch.setattr("app.template_share.MAX_UPLOAD_BYTES", len(ova) - 1)
    assert _upload(client, ova).status_code == 413
    assert list(share.iterdir()) == []


def test_preserves_legacy_guest_login_fields_and_ignores_paths_outside_share(
    client, share
):
    folder = share / "legacy"
    folder.mkdir()
    entry = {
        "id": "legacy",
        "name": FIELDS["name"],
        "file": "legacy.ova",
        "users": [{"username": "test-guest", "password": "fixture-only"}],
        "os": {"family": "Linux", "version": "old", "extra": "kept"},
        "custom": "kept",
    }
    manifest = folder / "legacy.yaml"
    manifest.write_text(yaml.safe_dump({"version": 1, "templates": [entry]}))
    response = client.get(URL)
    assert "fixture-only" not in response.text
    row = response.json()["templates"][0]
    assert row["metadata"]["users"] is None and not row["file_exists"]
    patch = {
        "manifest": "../legacy.yaml",
        "id": row["id"],
        "revision": row["revision"],
        "metadata": {key: value for key, value in FIELDS.items() if key != "users"},
    }
    assert client.patch(URL, json=patch).status_code == 400
    patch["manifest"] = row["manifest"]
    assert client.patch(URL, json=patch).status_code == 200
    changed = yaml.safe_load(manifest.read_text())["templates"][0]
    assert changed["users"] == entry["users"]
    assert changed["custom"] == changed["os"]["extra"] == "kept"


def test_share_is_admin_only_and_opt_in(
    gateway_client, gateway_user_client, monkeypatch, ova
):
    monkeypatch.delenv("TOPOLOGYOPERATOR_TEMPLATE_SHARE_DIR", raising=False)
    assert gateway_client.get(URL).json()["enabled"] is False
    assert _upload(gateway_client, ova).status_code == 409
    assert gateway_user_client.get(URL).status_code == 403
    assert _upload(gateway_user_client, ova).status_code == 403
    assert gateway_client.get("/admin.html").status_code == 200
    assert gateway_client.get("/admin-template-share.js").status_code == 200
