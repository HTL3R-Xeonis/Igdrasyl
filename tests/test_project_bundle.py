"""
Tests app/project_bundle.py against a fake TopologyBackend, same
reasoning as test_topology_write.py - a real TopologyBackend opens a
real pyvmomi/GNS3 session. build_export/apply_import's own zip
handling is real (not mocked) - only the live deploy itself is faked.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
import yaml
from src.settings import Settings

from app import project_bundle

_FIXTURE_TOPOLOGY = Path(__file__).parent / "fixtures" / "topology.yaml"


def _make_bundle(
    *,
    format_version=1,
    esxi_ip=None,
    esxi_vm_name=None,
    omit=(),
    drawings=None,
    layout=None,
    auto_config_push=False,
) -> bytes:
    manifest = {
        "format_version": format_version,
        "project_name": "test",
        "exported_at": "now",
        "auto_config_push": auto_config_push,
    }
    settings = {
        "esxi": {
            "ip": esxi_ip if esxi_ip is not None else Settings.ESXI.IP,
            "gns3_vm_name": esxi_vm_name
            if esxi_vm_name is not None
            else Settings.ESXI.GNS3_VM_NAME,
        },
        "gns3": {"username": Settings.GNS3.USERNAME, "port": Settings.GNS3.PORT},
    }
    members = {
        "manifest.json": json.dumps(manifest),
        "topology.yaml": _FIXTURE_TOPOLOGY.read_text(),
        "settings.yml": yaml.safe_dump(settings),
        "configs.json": json.dumps({"devices": {"R1": ["show version"]}}),
    }
    # drawings.json/layout.json are deliberately optional - a bundle
    # exported before they existed won't have them, and that's not a
    # validation failure (see apply_import) - only add one here when a
    # test asks for it.
    if drawings is not None:
        members["drawings.json"] = json.dumps({"drawings": drawings})
    if layout is not None:
        members["layout.json"] = json.dumps({"layout": layout})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            if name in omit:
                continue
            zf.writestr(name, content)
    return buf.getvalue()


class _FakeBackend:
    def __init__(self):
        self.calls: list[tuple] = []

    def deploy_topology(self, topology_file, topology_yaml_text):
        self.calls.append(("deploy_topology", topology_file, topology_yaml_text))
        Path(topology_file).write_text(topology_yaml_text)


def test_build_export_produces_expected_zip_members(
    app_state, temp_topology_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    app_state.last_config_push = {"R1": ["show version"]}
    app_state.last_drawings = [
        {"id": 1, "type": "text", "x1": 10, "y1": 20, "text": "hi"}
    ]

    data = project_bundle.build_export(_app())

    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == {
        "manifest.json",
        "topology.yaml",
        "settings.yml",
        "configs.json",
        "drawings.json",
        "layout.json",
    }

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == 1
    assert manifest["project_name"] == app_state.project_name

    assert zf.read("topology.yaml").decode() == temp_topology_file.read_text()

    configs = json.loads(zf.read("configs.json"))
    assert configs == {"devices": {"R1": ["show version"]}}

    settings = yaml.safe_load(zf.read("settings.yml"))
    assert settings["esxi"]["ip"] == Settings.ESXI.IP
    assert settings["esxi"]["gns3_vm_name"] == Settings.ESXI.GNS3_VM_NAME

    # A live export always starts a bundle with auto_config_push off -
    # opting a specific bundle in is a manual manifest.json edit (or set
    # when building one by hand), not something export ever infers.
    assert manifest["auto_config_push"] is False

    drawings = json.loads(zf.read("drawings.json"))
    assert drawings == {"drawings": app_state.last_drawings}

    # No prior drag in this test, so every node falls back to a computed
    # default position (see app/layout.py's load_layout) - just assert
    # every node got one, not any specific coordinates.
    layout = json.loads(zf.read("layout.json"))["layout"]
    assert set(layout) == set(app_state.graph.nodes)


def _app():
    import app.main as main_module

    return main_module.app


def test_apply_import_happy_path_deploys_and_reloads_graph(
    app_state, temp_topology_file
):
    fake = _FakeBackend()
    app_state.topology_backend = fake
    bundle = _make_bundle()

    result = project_bundle.apply_import(_app(), bundle)

    assert fake.calls[0][0] == "deploy_topology"
    assert fake.calls[0][1] == str(temp_topology_file)
    assert result["manifest"]["format_version"] == 1
    assert result["configs"] == {"R1": ["show version"]}
    assert "R1" in app_state.graph.nodes
    # No drawings.json in this bundle (pre-existing helper default) -
    # backward-compatible with bundles exported before drawings existed.
    assert result["drawings"] == []
    assert app_state.last_drawings == []
    assert result["auto_config_push"] is False


def test_apply_import_surfaces_auto_config_push_from_the_manifest(
    app_state, temp_topology_file
):
    app_state.topology_backend = _FakeBackend()
    bundle = _make_bundle(auto_config_push=True)

    result = project_bundle.apply_import(_app(), bundle)

    assert result["auto_config_push"] is True


def test_apply_import_defaults_auto_config_push_false_for_older_bundles(
    app_state, temp_topology_file
):
    """A bundle built before this field existed has no manifest key at all - must default to False, never crash."""
    app_state.topology_backend = _FakeBackend()
    bundle = _make_bundle()
    zf_in = zipfile.ZipFile(io.BytesIO(bundle))
    manifest = json.loads(zf_in.read("manifest.json"))
    del manifest["auto_config_push"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf_out:
        for name in zf_in.namelist():
            content = (
                json.dumps(manifest) if name == "manifest.json" else zf_in.read(name)
            )
            zf_out.writestr(name, content)

    result = project_bundle.apply_import(_app(), buf.getvalue())

    assert result["auto_config_push"] is False


def test_apply_import_restores_bundled_drawings(app_state, temp_topology_file):
    app_state.topology_backend = _FakeBackend()
    bundled_drawings = [{"id": 1, "type": "text", "x1": 5, "y1": 5, "text": "hi"}]
    bundle = _make_bundle(drawings=bundled_drawings)

    result = project_bundle.apply_import(_app(), bundle)

    assert result["drawings"] == bundled_drawings
    assert app_state.last_drawings == bundled_drawings


def test_apply_import_restores_bundled_layout(
    app_state, temp_topology_file, tmp_path, monkeypatch
):
    monkeypatch.setenv("TOPOLOGYOPERATOR_LAYOUT_FILE", str(tmp_path / "layout.json"))
    app_state.topology_backend = _FakeBackend()
    bundle = _make_bundle(layout={"R1": {"x": 42, "y": 84}, "GHOST": {"x": 1, "y": 2}})

    project_bundle.apply_import(_app(), bundle)

    from app.layout import load_layout

    saved = load_layout(app_state.graph.nodes)
    assert saved["R1"] == {"x": 42, "y": 84}
    # GHOST isn't a real node (post-deploy graph), so it's dropped rather
    # than saved as a position pointing at nothing.
    assert "GHOST" not in saved


def test_apply_import_re_resolves_a_project_id_the_deploy_changed(
    app_state, temp_topology_file
):
    """
    Real bug found live: a non-incremental deploy deleted and recreated
    the whole GNS3 project under a new id, but the already-running
    server kept using the old one (cached at startup) - every GNS3 call
    404ed until the process was manually restarted. apply_import must
    re-resolve project_id after every deploy so that doesn't happen.
    """
    app_state.topology_backend = _FakeBackend()
    app_state.gns3_vm_ip = "10.20.20.231"
    app_state.project_id = "stale-id"
    bundle = _make_bundle()

    with patch(
        "app.project_bundle.resolve_project_id", return_value="fresh-id"
    ) as fake_resolve:
        project_bundle.apply_import(_app(), bundle)

    fake_resolve.assert_called_once_with("10.20.20.231", app_state.project_name)
    assert app_state.project_id == "fresh-id"


def test_apply_import_keeps_old_project_id_when_gns3_unreachable(
    app_state, temp_topology_file
):
    app_state.topology_backend = _FakeBackend()
    app_state.gns3_vm_ip = "10.20.20.231"
    app_state.project_id = "stale-id"
    bundle = _make_bundle()

    with patch(
        "app.project_bundle.resolve_project_id", side_effect=requests.ConnectionError()
    ):
        project_bundle.apply_import(_app(), bundle)

    assert app_state.project_id == "stale-id"


def test_apply_import_clears_a_stale_startup_warning_once_the_project_resolves(
    app_state, temp_topology_file
):
    """
    Real bug found live: startup_warning is set once in lifespan (e.g.
    "no GNS3 project named X yet") and never revisited elsewhere, so
    /health's warning field - and the frontend's warning banner - stayed
    stuck showing that stale message forever, even after a real import
    successfully created the project it was complaining didn't exist.
    """
    app_state.topology_backend = _FakeBackend()
    app_state.gns3_vm_ip = "10.20.20.231"
    app_state.startup_warning = (
        "No GNS3 project named 'x' - has this topology actually been deployed?"
    )
    bundle = _make_bundle()

    with patch("app.project_bundle.resolve_project_id", return_value="fresh-id"):
        project_bundle.apply_import(_app(), bundle)

    assert app_state.startup_warning is None


def test_apply_import_keeps_the_warning_when_project_still_cannot_be_resolved(
    app_state, temp_topology_file
):
    app_state.topology_backend = _FakeBackend()
    app_state.gns3_vm_ip = "10.20.20.231"
    app_state.startup_warning = "some pre-existing warning"
    bundle = _make_bundle()

    with patch("app.project_bundle.resolve_project_id", return_value=None):
        project_bundle.apply_import(_app(), bundle)

    assert app_state.startup_warning == "some pre-existing warning"


def test_apply_import_rejects_mismatched_esxi_target(app_state, temp_topology_file):
    app_state.topology_backend = MagicMock()
    bundle = _make_bundle(esxi_ip="9.9.9.9")

    with pytest.raises(project_bundle.BundleValidationError, match="9.9.9.9"):
        project_bundle.apply_import(_app(), bundle)

    app_state.topology_backend.deploy_topology.assert_not_called()


def test_apply_import_rejects_malformed_zip(app_state, temp_topology_file):
    with pytest.raises(project_bundle.BundleValidationError, match="not a valid"):
        project_bundle.apply_import(_app(), b"not a zip file at all")


def test_apply_import_rejects_missing_member(app_state, temp_topology_file):
    bundle = _make_bundle(omit=("configs.json",))

    with pytest.raises(project_bundle.BundleValidationError, match="configs.json"):
        project_bundle.apply_import(_app(), bundle)


def test_apply_import_rejects_unsupported_format_version(app_state, temp_topology_file):
    bundle = _make_bundle(format_version=99)

    with pytest.raises(project_bundle.BundleValidationError, match="99"):
        project_bundle.apply_import(_app(), bundle)


def test_apply_import_propagates_deploy_failure_and_leaves_file_untouched(
    app_state, temp_topology_file
):
    fake = MagicMock()
    fake.deploy_topology.side_effect = RuntimeError("deploy failed")
    app_state.topology_backend = fake
    original_content = temp_topology_file.read_text()
    bundle = _make_bundle()

    with pytest.raises(RuntimeError, match="deploy failed"):
        project_bundle.apply_import(_app(), bundle)

    assert temp_topology_file.read_text() == original_content
