import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from src.graph.environment import Environment

from app import ai_snippets, config_snippets

_TOKEN = "t" * 32
_GENERATOR = "https://generator.test"
_URL = "/config-snippets/ai"
_VLAN = {"kind": "vlan", "vlan_id": 30, "name": "Lab"}
_REQUEST = {"device": "SW1", "prompt": "VLAN 30 Lab"}


@pytest.fixture
def enable_ai(app_state, monkeypatch):
    ai_snippets.set_enabled(True)
    monkeypatch.setattr(ai_snippets, "list_node_ports", lambda *_: [{"name": "Gi0/1"}])
    monkeypatch.setenv("XEONIS_AI_SNIPPETS_TOKEN", _TOKEN)
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL", _GENERATOR)
    monkeypatch.setattr(app_state, "project_id", "test-project")
    monkeypatch.setattr(app_state, "push_job", None, raising=False)
    monkeypatch.setattr(app_state, "connectivity_job", None, raising=False)
    monkeypatch.setattr(app_state, "ai_snippet_drafts", {}, raising=False)
    monkeypatch.setitem(
        app_state.graph.nodes,
        "SW1",
        SimpleNamespace(
            image="Cisco IOSvL2 15.2", role="SWITCH", env=Environment.ON_GNS3
        ),
    )


def _draft(client, settings=_VLAN):
    reply = Mock(status_code=200)
    reply.json.return_value = [settings]
    with patch("app.ai_snippets.requests.post", return_value=reply) as upstream:
        response = client.post(f"{_URL}/generate", json=_REQUEST)
    return response, upstream


def test_generation_and_target_selection_require_server_release(client, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_AI_SNIPPET_TOPICS", "vlan")
    with patch("app.ai_snippets.requests.post") as upstream:
        assert client.get(f"{_URL}/status").json()["enabled"] is False
        assert client.get(f"{_URL}/targets").status_code == 403
        assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 403
    upstream.assert_not_called()


def test_push_runs_only_after_confirmation_and_only_once(client, enable_ai):
    assert client.get(f"{_URL}/targets").json() == ["R1", "SW1", "VM1"]
    with patch("app.push_jobs.push_all", return_value=[]) as push:
        response, upstream = _draft(client)
        assert response.status_code == 200, response.text
        assert upstream.call_args.args == (_GENERATOR + "/api/config-draft",)
        assert upstream.call_args.kwargs["headers"] == {
            "Authorization": f"Bearer {_TOKEN}"
        }
        assert upstream.call_args.kwargs["allow_redirects"] is False
        assert upstream.call_args.kwargs["json"] == {
            "prompt": "VLAN 30 Lab",
            "context": {
                "device": "SW1",
                "role": "SWITCH",
                "image": "Cisco IOSvL2 15.2",
                "platform": "iosvl2",
                "ports": ["GigabitEthernet0/1"],
                "links": [
                    {"port": "Ethernet0", "neighbor": "R1", "neighbor_port": "gi0/0"}
                ],
            },
        }
        draft = response.json()
        assert draft["device"] == "SW1"
        assert draft["commands"] == [
            "configure terminal",
            "vlan 30",
            "name Lab",
            "exit",
            "end",
        ]
        push.assert_not_called()
        assert config_snippets.list_snippets() == []
        payload = {"draft_id": draft["draft_id"]}
        assert (
            client.post(f"{_URL}/push", json={**payload, "device": "R1"}).status_code
            == 422
        )
        started = client.post(f"{_URL}/push", json=payload)
        assert started.status_code == 202, started.text
        assert client.post(f"{_URL}/push", json=payload).status_code == 409
        path = f"/config-push/jobs/{started.json()['job_id']}"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get(path).json()["status"] == "done":
                break
            time.sleep(0.01)
        else:
            pytest.fail("push job did not finish")
    push.assert_called_once()
    assert push.call_args.args[0] == {"SW1": draft["commands"]}
    assert [row["note"] for row in client.get("/config-push/audit").json()] == [
        "AI snippet"
    ]


@pytest.mark.parametrize(
    "settings",
    [
        {**_VLAN, "vlan_id": True},
        {**_VLAN, "vlan_id": 1002},
        {**_VLAN, "name": "Lab\nend"},
        {**_VLAN, "commands": ["unexpected"]},
    ],
)
def test_invalid_model_values_cannot_become_commands(
    client, enable_ai, app_state, settings
):
    response, _ = _draft(client, settings)
    assert response.status_code == 422
    assert app_state.ai_snippet_drafts == {}


def test_generation_rejects_unknown_device_and_obsolete_topic_field(client, enable_ai):
    with patch("app.ai_snippets.requests.post") as upstream:
        assert (
            client.post(
                f"{_URL}/generate", json={**_REQUEST, "device": "unknown"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"{_URL}/generate", json={**_REQUEST, "topic": "interface_description"}
            ).status_code
            == 422
        )
    upstream.assert_not_called()


def test_changed_project_or_revoked_release_blocks_push(
    client, app_state, enable_ai, user_client
):
    response, _ = _draft(client)
    payload = {"draft_id": response.json()["draft_id"]}
    with patch("app.push_jobs.start_job") as push:
        assert user_client.post(f"{_URL}/push", json=payload).status_code == 409
        app_state.project_id = "changed-project"
        try:
            assert client.post(f"{_URL}/push", json=payload).status_code == 409
        finally:
            app_state.project_id = "test-project"
        ai_snippets.set_enabled(False)
        assert client.post(f"{_URL}/push", json=payload).status_code == 403
    push.assert_not_called()


def test_description_and_access_vlan_require_a_known_physical_port():
    port = "GigabitEthernet0/1"
    description = {
        "kind": "description",
        "interface": port,
        "description": "Uplink Raum 2",
    }
    access = {"kind": "access_vlan", "interface": port, "vlan_id": 30}
    assert ai_snippets._compile([description, access], [port], "iosvl2") == [
        "configure terminal",
        "interface GigabitEthernet0/1",
        "description Uplink Raum 2",
        "exit",
        "interface GigabitEthernet0/1",
        "switchport mode access",
        "switchport access vlan 30",
        "exit",
        "end",
    ]
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([description], ["GigabitEthernet0/2"], "iosvl2")
    description["interface"] = "range GigabitEthernet0/1 - 4"
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([description], [port], "iosvl2")


def test_ipv4_settings_require_iosv_and_a_valid_cidr():
    port = "GigabitEthernet0/1"
    settings = {"kind": "ipv4", "interface": port, "address": "192.0.2.4/26"}
    commands = ai_snippets._compile([settings], [port], "iosv")
    assert "ip address 192.0.2.4 255.255.255.192" in commands
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([settings], [port], "iosvl2")
    settings["address"] = "192.0.2.400/26"
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([settings], [port], "iosv")


def test_missing_fixed_generator_url_keeps_the_feature_disabled(
    client, enable_ai, monkeypatch
):
    monkeypatch.delenv("TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL")
    assert client.get(f"{_URL}/status").json() == {"enabled": True, "available": False}
    assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 503


def test_classroom_mode_blocks_older_ai_fix_endpoint(client, monkeypatch):
    monkeypatch.setenv("XEONIS_CLASSROOM_MODE", "1")
    response = client.post("/ai/suggest-fix", json={"device": "R1", "check": "test"})
    assert response.status_code == 403


# ---- every node kind (Linux, Windows, RouterOS, VPCS, unsupported) ----


def _node(image, env=Environment.ON_ESXI, role="VM"):
    return SimpleNamespace(image=image, role=role, env=env)


@pytest.fixture
def mixed_nodes(app_state, enable_ai, monkeypatch):
    def gns3_ports(app, name):
        # GNS3 knows no ESXi VM: the real list_node_ports raises ValueError for them
        if app.state.graph.nodes[name].env == Environment.ON_ESXI:
            raise ValueError(f"node {name!r} has no live GNS3 port information")
        return [{"name": "Gi0/1"}]

    monkeypatch.setattr(ai_snippets, "list_node_ports", gns3_ports)
    nodes = app_state.graph.nodes
    nodes["web1"] = _node("ubuntu")
    nodes["win1"] = _node("Windows Server 2022 Standard")
    nodes["mt1"] = _node("MikroTik")
    nodes["fw1"] = _node("pfSense")
    nodes["pc9"] = _node("VPCS", Environment.ON_GNS3, "PC")
    nodes["odd1"] = _node("Some Other Template", Environment.ON_GNS3, "ROUTER")
    return nodes


@pytest.mark.parametrize(
    ("image", "env", "platform"),
    [
        ("ubuntu", Environment.ON_ESXI, "linux"),
        ("debian", Environment.ON_ESXI, "linux"),
        ("Windows 10 Pro", Environment.ON_ESXI, "windows"),
        ("MikroTik", Environment.ON_ESXI, "routeros"),
        ("pfSense", Environment.ON_ESXI, None),
        ("OPNsense", Environment.ON_ESXI, None),
        ("VPCS", Environment.ON_GNS3, "vpcs"),
        ("Cisco IOSv 15.9(3)M6", Environment.ON_GNS3, "iosv"),
        ("Cisco IOSvL2 15.2", Environment.ON_GNS3, "iosvl2"),
        ("Some Other Template", Environment.ON_GNS3, None),
    ],
)
def test_platform_of_every_kind_of_node(image, env, platform):
    assert ai_snippets.platform_of(_node(image, env)) == platform


def test_a_new_ubuntu_vm_on_an_esxi_host_is_selectable(client, app_state, enable_ai):
    """The reported case: an Ubuntu VM added on a host - even with no GNS3 project selected - must be offered."""
    app_state.graph.nodes["web1"] = _node("ubuntu")
    app_state.project_id = None
    app_state.gns3_vm_ip = None
    assert "web1" in client.get(f"{_URL}/targets").json()
    rows = {row["name"]: row for row in client.get(f"{_URL}/devices").json()}
    assert rows["web1"] == {
        "name": "web1",
        "platform": "linux",
        "label": "Linux",
        "supported": True,
        "reason": "",
    }
    assert rows["SW1"]["supported"] is False  # a GNS3 node needs the selected project
    assert "GNS3 project" in rows["SW1"]["reason"]


def test_the_device_list_gives_a_reason_for_every_unsupported_node(client, mixed_nodes):
    rows = {row["name"]: row for row in client.get(f"{_URL}/devices").json()}
    assert rows["fw1"]["supported"] is False and "menu" in rows["fw1"]["reason"]
    assert (
        rows["odd1"]["supported"] is False
        and "No configuration generator" in rows["odd1"]["reason"]
    )
    for name in ("web1", "win1", "mt1", "pc9", "SW1", "R1", "VM1"):
        assert rows[name]["supported"] is True, name
    targets = client.get(f"{_URL}/targets").json()
    assert "fw1" not in targets and "odd1" not in targets and "web1" in targets


def test_an_unsupported_device_is_refused_when_generating(client, mixed_nodes):
    with patch("app.ai_snippets.requests.post") as upstream:
        response = client.post(
            f"{_URL}/generate", json={"device": "fw1", "prompt": "ip"}
        )
    assert response.status_code == 409
    upstream.assert_not_called()


def test_linux_names_are_not_rewritten_like_cisco_ports(client, mixed_nodes):
    app = client.app
    app.state.graph._edges.append(("web1", "eth0", "SW1", "Ethernet2"))
    try:
        context = ai_snippets._context(
            app, ai_snippets.fix_jobs.current_target(app, "web1")
        )
    finally:
        app.state.graph._edges.remove(("web1", "eth0", "SW1", "Ethernet2"))
    assert context["platform"] == "linux"
    assert context["ports"] == ["eth0"]
    assert ai_snippets._canonical_port("eth0") == "Ethernet0"  # Cisco only


@pytest.mark.parametrize(
    ("platform", "settings", "expected"),
    [
        (
            "linux",
            [{"kind": "ipv4", "interface": "ens160", "address": "10.0.0.5/24"}],
            [
                "# runtime change with ip(8): lost on reboot",
                "ip address replace 10.0.0.5/24 dev ens160",
                "ip link set dev ens160 up",
            ],
        ),
        (
            "linux",
            [{"kind": "hostname", "name": "web-1"}],
            ["hostnamectl set-hostname web-1"],
        ),
        (
            "windows",
            [{"kind": "ipv4", "interface": "Ethernet0", "address": "10.0.0.6/24"}],
            [
                'netsh interface ip set address name="Ethernet0" static 10.0.0.6 255.255.255.0'
            ],
        ),
        (
            "routeros",
            [
                {"kind": "ipv4", "interface": "ether1", "address": "10.0.0.7/24"},
                {"kind": "hostname", "name": "edge1"},
            ],
            [
                "/ip address add address=10.0.0.7/24 interface=ether1",
                "/system identity set name=edge1",
            ],
        ),
        (
            "vpcs",
            [{"kind": "ipv4", "interface": "Ethernet0", "address": "10.0.0.8/24"}],
            ["ip 10.0.0.8/24", "save"],
        ),
    ],
)
def test_host_platforms_render_commands_from_validated_settings(
    platform, settings, expected
):
    ports = ["ens160", "Ethernet0", "ether1"]
    assert ai_snippets._compile(settings, ports, platform) == expected


@pytest.mark.parametrize(
    "settings",
    [
        [
            {"kind": "ipv4", "interface": "ens999", "address": "10.0.0.5/24"}
        ],  # not a port of the device
        [{"kind": "ipv4", "interface": "ens160; reboot", "address": "10.0.0.5/24"}],
        [{"kind": "ipv4", "interface": "ens160", "address": "224.0.0.1/24"}],
        [{"kind": "ipv4", "interface": "ens160", "address": "0.0.0.0/8"}],
        [{"kind": "ipv4", "interface": "ens160", "address": "10.0.0.300/24"}],
        [{"kind": "hostname", "name": "bad name; rm -rf /"}],
        [{"kind": "vlan", "vlan_id": 30, "name": "LAB"}],  # a Cisco setting on a host
        [{"kind": "ipv4", "interface": "ens160", "address": "10.0.0.5/24", "x": 1}],
        [],
    ],
)
def test_host_settings_that_are_unsafe_or_unsupported_are_refused(settings):
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile(settings, ["ens160"], "linux")


def test_windows_hostnames_are_refused():
    with pytest.raises(ai_snippets.InvalidDraft):
        ai_snippets._compile([{"kind": "hostname", "name": "pc1"}], [], "windows")


def test_the_generator_gets_the_platform_and_untouched_linux_port_names(
    client, mixed_nodes
):
    reply = Mock(status_code=200)
    reply.json.return_value = [
        {"kind": "ipv4", "interface": "ens160", "address": "10.0.0.5/24"}
    ]
    mixed_nodes_edges = client.app.state.graph._edges
    mixed_nodes_edges.append(("web1", "ens160", "SW1", "Ethernet1"))
    try:
        with patch("app.ai_snippets.requests.post", return_value=reply) as upstream:
            response = client.post(
                f"{_URL}/generate",
                json={"device": "web1", "prompt": "address 10.0.0.5/24"},
            )
    finally:
        mixed_nodes_edges.remove(("web1", "ens160", "SW1", "Ethernet1"))
    assert response.status_code == 200, response.text
    context = upstream.call_args.kwargs["json"]["context"]
    assert context["platform"] == "linux" and context["ports"] == ["ens160"]
    assert context["links"] == [
        {"port": "ens160", "neighbor": "SW1", "neighbor_port": "Ethernet1"}
    ]
    assert response.json()["commands"][-2:] == [
        "ip address replace 10.0.0.5/24 dev ens160",
        "ip link set dev ens160 up",
    ]


def test_a_draft_for_a_linux_vm_is_pushed_like_any_other(client, mixed_nodes):
    reply = Mock(status_code=200)
    reply.json.return_value = [{"kind": "hostname", "name": "web-1"}]
    with patch("app.ai_snippets.requests.post", return_value=reply):
        draft = client.post(
            f"{_URL}/generate", json={"device": "web1", "prompt": "hostname web-1"}
        ).json()
    with patch("app.push_jobs.push_all", return_value=[]) as push:
        started = client.post(f"{_URL}/push", json={"draft_id": draft["draft_id"]})
        assert started.status_code == 202, started.text
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (
                client.get(f"/config-push/jobs/{started.json()['job_id']}").json()[
                    "status"
                ]
                == "done"
            ):
                break
            time.sleep(0.01)
    assert push.call_args.args[0] == {"web1": ["hostnamectl set-hostname web-1"]}


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("iosvl2", "VLANs"),
        ("iosv", "IPv4 addresses"),
        ("windows", "no host name"),
        ("linux", "host name"),
    ],
)
def test_an_unmappable_prompt_names_what_the_device_supports(platform, expected):
    text = ai_snippets._supported_text(platform)
    assert expected in text and text.startswith("Nothing in the prompt")
