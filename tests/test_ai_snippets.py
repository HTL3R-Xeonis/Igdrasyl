import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from src.graph.environment import Environment

import app.config_push as cp
from app import ai_snippets, config_snippets

_TOKEN = "t" * 32
_GENERATOR = "https://generator.test"
_URL = "/config-snippets/ai"
_COMMANDS = ["vlan 30", "name Lab"]
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
            name="SW1",
            image="Cisco IOSvL2 15.2",
            role="SWITCH",
            env=Environment.ON_GNS3,
        ),
    )


def _reply(commands=_COMMANDS, warnings=()):
    reply = Mock(status_code=200)
    reply.json.return_value = {"commands": commands, "warnings": list(warnings)}
    return reply


def _draft(client, commands=_COMMANDS, warnings=(), request=_REQUEST):
    with patch(
        "app.ai_snippets.requests.post", return_value=_reply(commands, warnings)
    ) as upstream:
        response = client.post(f"{_URL}/generate", json=request)
    return response, upstream


def _wait_for(client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get(f"/config-push/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    pytest.fail("push job did not finish")


def test_generation_and_target_selection_require_server_release(client, monkeypatch):
    with patch("app.ai_snippets.requests.post") as upstream:
        assert client.get(f"{_URL}/status").json()["enabled"] is False
        assert client.get(f"{_URL}/targets").status_code == 403
        assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 403
    upstream.assert_not_called()


def test_push_runs_only_after_confirmation_and_only_once(client, enable_ai):
    assert client.get(f"{_URL}/targets").json() == ["R1", "SW1", "VM1"]
    with patch("app.push_jobs.push_all", return_value=[]) as push:
        response, upstream = _draft(client, warnings=["Check the VLAN ID."])
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
        assert draft["device"] == "SW1" and draft["platform"] == "iosvl2"
        # the draft is what the generator returned: no mode changes of this program in it
        assert draft["commands"] == _COMMANDS
        assert draft["warnings"] == ["Check the VLAN ID."]
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
        assert _wait_for(client, started.json()["job_id"])["status"] == "done"
    push.assert_called_once()
    spec = push.call_args.args[0]["SW1"]
    assert spec.commands == _COMMANDS and spec.native_shell is True
    assert [row["note"] for row in client.get("/config-push/audit").json()] == [
        "AI snippet"
    ]


def test_the_pushed_draft_is_stored_as_plain_data_for_project_exports(
    client, app_state, enable_ai
):
    with patch("app.push_jobs.push_all", return_value=[]):
        draft = _draft(client)[0].json()
        started = client.post(f"{_URL}/push", json={"draft_id": draft["draft_id"]})
        _wait_for(client, started.json()["job_id"])
    assert app_state.last_config_push == {
        "SW1": {"commands": _COMMANDS, "native_shell": True}
    }


def test_the_generators_refusal_is_passed_on_word_for_word(
    client, enable_ai, app_state
):
    reply = Mock(status_code=422)
    reply.json.return_value = {"detail": "Interface Gi9/9 does not exist on IOSvL2."}
    with patch("app.ai_snippets.requests.post", return_value=reply):
        response = client.post(f"{_URL}/generate", json=_REQUEST)
    assert response.status_code == 422
    assert response.json()["detail"] == "Interface Gi9/9 does not exist on IOSvL2."
    assert app_state.ai_snippet_drafts == {}


def test_a_validation_list_as_refusal_is_joined(client, enable_ai):
    reply = Mock(status_code=422)
    reply.json.return_value = {"detail": [{"msg": "prompt too long"}, {"msg": "x"}]}
    with patch("app.ai_snippets.requests.post", return_value=reply):
        response = client.post(f"{_URL}/generate", json=_REQUEST)
    assert response.status_code == 422
    assert response.json()["detail"] == "prompt too long x"


def test_other_generator_errors_are_reported_as_unavailable(client, enable_ai):
    for status in (500, 401, 404):
        with patch(
            "app.ai_snippets.requests.post", return_value=Mock(status_code=status)
        ):
            assert client.post(f"{_URL}/generate", json=_REQUEST).status_code == 502, (
                status
            )


@pytest.mark.parametrize(
    "reply",
    [
        [],
        "ip address 1.2.3.4",
        {"warnings": []},
        {"commands": "vlan 30"},
        {"commands": []},
        {"commands": ["", "   "]},
        {"commands": [5]},
        {"commands": ["vlan 30\nend"]},
        {"commands": ["a\x00b"]},
        {"commands": ["a\rb"]},
        {"commands": ["a\x1b[2Jb"]},
        {"commands": ["a\u202eb"]},
        {"commands": ["```", "vlan 30", "```"]},
        {"commands": ["echo ```x```"]},
        {"commands": ["x" * 501]},
        {"commands": ["vlan 30"] * 201},
        {"commands": ["x" * 400] * 60},
        {"commands": ["vlan 30"], "warnings": "careful"},
    ],
)
def test_a_reply_that_is_not_plain_one_line_commands_is_refused(
    client, enable_ai, app_state, reply
):
    response = Mock(status_code=200)
    response.json.return_value = reply
    with patch("app.ai_snippets.requests.post", return_value=response):
        result = client.post(f"{_URL}/generate", json=_REQUEST)
    assert result.status_code == 422, result.text
    assert result.json()["detail"]
    assert app_state.ai_snippet_drafts == {}


def test_the_limits_are_met_exactly(client, enable_ai):
    commands = [f"description {'x' * 488}"] * 30  # 500 characters each
    assert len(commands[0]) == 500
    response, _ = _draft(client, commands)
    assert response.status_code == 200, response.text
    assert len(response.json()["commands"]) == 30
    response, _ = _draft(client, [f"vlan {n}" for n in range(200)])
    assert response.status_code == 200, response.text


def test_blank_lines_and_surrounding_spaces_are_dropped(client, enable_ai):
    response, _ = _draft(client, ["", "  vlan 30  ", "\t", "name Lab\n", " "])
    assert response.json()["commands"] == ["vlan 30", "name Lab"]


def test_any_command_is_accepted_without_a_whitelist(client, enable_ai):
    unusual = ["spanning-tree mode rapid-pvst", "banner motd #hello#", "do show vlan"]
    response, _ = _draft(client, unusual)
    assert response.status_code == 200, response.text
    assert response.json()["commands"] == unusual
    assert response.json()["warnings"] == []


def test_a_cisco_draft_does_not_enter_or_leave_configuration_mode_itself(
    client, enable_ai
):
    response, _ = _draft(client, ["conf t", "vlan 30", "name Lab", "end"])
    assert response.json()["commands"] == _COMMANDS
    response, _ = _draft(client, ["configure terminal", "end"])
    assert response.status_code == 422


def test_generator_and_local_warnings_are_merged_without_repeats(client, enable_ai):
    response, _ = _draft(
        client,
        ["interface Gi0/1", "shutdown", "enable secret hunter2"],
        warnings=[
            "Shuts the uplink.",
            (
                "Command 2 shuts down an interface or a network service "
                "(the connection may drop): shutdown"
            ),
            "  \x07 spaced \n out  ",
            "",
            7,
        ],
    )
    warnings = response.json()["warnings"]
    assert warnings[0] == "Shuts the uplink."
    assert warnings[1].startswith("Command 2 shuts down an interface")
    assert "spaced out" in warnings
    assert warnings.count(warnings[1]) == 1
    assert warnings[-1] == (
        "Command 3 sets or changes credentials (the command is not repeated here)."
    )
    assert "hunter2" not in " ".join(warnings)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("reload", "restarts or powers off"),
        ("reload in 5", "restarts or powers off"),
        ("sudo reboot", "restarts or powers off"),
        ("shutdown /r /t 0", "restarts or powers off"),
        ("shutdown -h now", "restarts or powers off"),
        ("Restart-Computer -Force", "restarts or powers off"),
        ("/system reboot", "restarts or powers off"),
        ("@reboot", "restarts or powers off"),
        ("erase startup-config", "erases or formats"),
        ("write erase", "erases or formats"),
        ("do erase nvram:", "erases or formats"),
        ("format flash:", "erases or formats"),
        ("format C: /q", "erases or formats"),
        ("mkfs.ext4 /dev/sdb1", "erases or formats"),
        ("dd if=/dev/zero of=/dev/sda", "erases or formats"),
        ("rm -rf /var/lib/app", "deletes files recursively"),
        ("sudo rm -fr /tmp/x", "deletes files recursively"),
        ("Remove-Item C:\\x -Recurse -Force", "deletes files recursively"),
        ("rd /s /q C:\\x", "deletes files recursively"),
        ("shutdown", "shuts down an interface"),
        ("ip link set dev eth0 down", "shuts down an interface"),
        ("Disable-NetAdapter -Name Ethernet0", "shuts down an interface"),
        ("/interface disable ether1", "shuts down an interface"),
        ("systemctl stop networking", "shuts down an interface"),
        ("iptables -F", "firewall"),
        ("ufw disable", "firewall"),
        ("ip route 0.0.0.0 0.0.0.0 10.0.0.1", "default route"),
        ("ip default-gateway 10.0.0.1", "default route"),
        ("ip route add default via 10.0.0.1", "default route"),
        ("route add 0.0.0.0 mask 0.0.0.0 10.0.0.1", "default route"),
        ("/ip route add dst-address=0.0.0.0/0 gateway=10.0.0.1", "default route"),
        ("enable secret 5 abc", "credentials"),
        ("username admin privilege 15 password x", "credentials"),
        ("echo 'root:x' | chpasswd", "credentials"),
        ("net user admin x /add", "credentials"),
        ("snmp-server community public RO", "credentials"),
    ],
)
def test_dangerous_commands_are_flagged_by_the_local_scan(command, expected):
    warnings = ai_snippets.advisory_warnings(["show clock", command])
    assert len(warnings) >= 1, command
    assert any(expected in warning for warning in warnings), warnings
    assert all(warning.startswith("Command 2 ") for warning in warnings)


@pytest.mark.parametrize(
    "command",
    [
        "no shutdown",
        "interface GigabitEthernet0/1",
        "ip address 10.0.0.1 255.255.255.0",
        "ip route 10.1.0.0 255.255.0.0 10.0.0.2",
        "ip link set dev eth0 up",
        "rm /tmp/file",
        "show running-config",
        "set community 100:1",
        "reloading is not a command",
    ],
)
def test_harmless_commands_are_not_flagged(command):
    assert ai_snippets.advisory_warnings([command]) == []


def test_a_flagged_command_stays_in_the_draft_and_is_never_blocked(client, enable_ai):
    response, _ = _draft(client, ["interface Gi0/1", "shutdown"])
    assert response.status_code == 200
    assert response.json()["commands"][-1] == "shutdown"
    assert len(response.json()["warnings"]) == 1


def test_warning_text_is_plain_and_bounded():
    commands = ["rm -rf " + "<script>alert(1)</script>" * 20]
    (warning,) = ai_snippets.advisory_warnings(commands)
    assert len(warning) < 150 and "\n" not in warning
    many = ai_snippets.all_warnings(
        [f"generator warning {n}" for n in range(80)], ["reload"]
    )
    assert len(many) == ai_snippets.MAX_WARNINGS
    long = ai_snippets.validate_reply(
        {"commands": ["a"], "warnings": ["w" * 900]}, "linux"
    )[1]
    assert len(long[0]) == ai_snippets.MAX_WARNING_LENGTH


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


def test_an_expired_draft_cannot_be_pushed(client, enable_ai, monkeypatch):
    draft = _draft(client)[0].json()
    now = time.monotonic()
    monkeypatch.setattr(ai_snippets.time, "monotonic", lambda: now + 1000)
    response = client.post(f"{_URL}/push", json={"draft_id": draft["draft_id"]})
    assert response.status_code == 409


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


# ---- every node kind ----


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
    nodes["none1"] = _node("Whatever", Environment.ON_NOTHING)
    for name in ("web1", "win1", "mt1", "fw1", "pc9", "odd1", "none1"):
        nodes[name].name = name
    return nodes


@pytest.mark.parametrize(
    ("image", "env", "platform"),
    [
        ("ubuntu", Environment.ON_ESXI, "linux"),
        ("debian", Environment.ON_ESXI, "linux"),
        ("Windows 10 Pro", Environment.ON_ESXI, "windows"),
        ("MikroTik", Environment.ON_ESXI, "routeros"),
        ("pfSense", Environment.ON_ESXI, "pfsense"),
        ("OPNsense", Environment.ON_ESXI, "pfsense"),
        ("VPCS", Environment.ON_GNS3, "vpcs"),
        ("Cisco IOSv 15.9(3)M6", Environment.ON_GNS3, "iosv"),
        ("Cisco IOSvL2 15.2", Environment.ON_GNS3, "iosvl2"),
        ("Some Other Template", Environment.ON_GNS3, "other"),
        ("Whatever", Environment.ON_NOTHING, None),
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


def test_every_node_that_can_receive_a_push_is_supported(client, mixed_nodes):
    rows = {row["name"]: row for row in client.get(f"{_URL}/devices").json()}
    for name in ("web1", "win1", "mt1", "fw1", "pc9", "odd1", "SW1", "R1", "VM1"):
        assert rows[name]["supported"] is True, name
        assert rows[name]["reason"] == ""
    # only a node without any push transport is not
    assert rows["none1"]["supported"] is False and rows["none1"]["reason"]
    targets = client.get(f"{_URL}/targets").json()
    assert "none1" not in targets
    assert {"fw1", "odd1", "web1", "win1", "mt1", "pc9"} <= set(targets)


def test_an_unsupported_device_is_refused_when_generating(client, mixed_nodes):
    with patch("app.ai_snippets.requests.post") as upstream:
        response = client.post(
            f"{_URL}/generate", json={"device": "none1", "prompt": "ip"}
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
    ("device", "platform", "commands"),
    [
        ("web1", "linux", ["ip address replace 10.0.0.5/24 dev ens160"]),
        ("win1", "windows", ["Get-NetIPAddress | Format-Table"]),
        ("mt1", "routeros", ["/ip address add address=10.0.0.7/24 interface=ether1"]),
        ("fw1", "pfsense", ["pfctl -sr"]),
        ("pc9", "vpcs", ["ip 10.0.0.8/24", "save"]),
        ("odd1", "other", ["show status"]),
        ("SW1", "iosvl2", ["vlan 30", "name Lab"]),
        ("R1", "iosv", ["interface Gi0/0", "ip address 10.0.0.1 255.255.255.0"]),
    ],
)
def test_the_generators_commands_become_the_draft_of_every_platform(
    client, mixed_nodes, device, platform, commands
):
    if device == "R1":
        mixed_nodes["R1"] = _node("Cisco IOSv 15.9", Environment.ON_GNS3, "ROUTER")
        mixed_nodes["R1"].name = "R1"
    response, upstream = _draft(
        client, commands, request={"device": device, "prompt": "do it"}
    )
    assert response.status_code == 200, response.text
    assert upstream.call_args.kwargs["json"]["context"]["platform"] == platform
    assert response.json()["commands"] == commands
    assert response.json()["platform"] == platform


def _ok(name, transport):
    return cp.DeviceResult(device=name, transport=transport, ok=True)


@pytest.mark.parametrize(
    ("device", "transport", "native_shell"),
    [
        ("web1", "guest_ops", True),
        ("win1", "guest_ops", True),
        ("mt1", "serial", True),
        ("fw1", "serial", True),
        ("pc9", "netmiko", True),
        ("odd1", "netmiko", True),
        ("SW1", "netmiko", True),
    ],
)
def test_a_draft_reaches_every_node_kind_through_the_normal_push_path(
    client, mixed_nodes, monkeypatch, device, transport, native_shell
):
    """Draft -> push job -> push_all -> the transport of the node, with fake device backends."""
    calls = []

    def netmiko(node, commands, ip, port, progress=None, *rest):
        calls.append(("netmiko", node.name, list(commands), rest))
        return _ok(node.name, "netmiko")

    def guest_ops(node, commands, esxi, progress=None, *rest):
        calls.append(("guest_ops", node.name, list(commands), rest))
        return _ok(node.name, "guest_ops")

    def serial(node, spec, esxi, progress=None):
        calls.append(("serial", node.name, list(spec.commands), spec.native_shell))
        return _ok(node.name, "serial")

    monkeypatch.setattr(cp, "_push_via_netmiko", netmiko)
    monkeypatch.setattr(cp, "_push_via_guest_ops", guest_ops)
    monkeypatch.setattr(cp, "_push_via_serial", serial)
    monkeypatch.setattr(cp, "_ensure_serial", lambda node, app: None)
    monkeypatch.setattr(
        cp,
        "_esxi_transport",
        lambda node, spec, esxi: (
            "serial" if node.name in {"mt1", "fw1"} else "guest_ops"
        ),
    )
    monkeypatch.setattr(
        cp,
        "list_live_nodes",
        lambda app: [
            {"name": n, "console": 5000, "console_type": "telnet"}
            for n in ("SW1", "pc9", "odd1")
        ],
    )
    commands = ["first", "second"]
    draft = _draft(client, commands, request={"device": device, "prompt": "x"})[
        0
    ].json()
    started = client.post(f"{_URL}/push", json={"draft_id": draft["draft_id"]})
    assert started.status_code == 202, started.text
    job = _wait_for(client, started.json()["job_id"])
    assert job["status"] == "done", job
    assert job["results"][0]["ok"] is True
    assert job["results"][0]["transport"] == transport
    ((used, name, sent, flag),) = calls
    assert (used, name, sent) == (transport, device, commands)
    # netmiko and guest ops get native_shell as their last argument, serial through the spec
    assert (flag if used == "serial" else flag[-1]) is native_shell


def test_a_draft_for_a_linux_vm_is_pushed_as_root_shell_commands(client, mixed_nodes):
    """Linux commands run as given: guest operations start /bin/bash -c with the template's root login."""
    fake_esxi = MagicMock()
    client.app.state.esxi_connection = fake_esxi
    process_manager = fake_esxi.content.guestOperationsManager.processManager
    process_manager.StartProgramInGuest.return_value = 3
    process_manager.ListProcessesInGuest.return_value = [
        MagicMock(endTime="t", exitCode=0)
    ]
    fake_esxi.get_vm.return_value.guest.toolsRunningStatus = "guestToolsRunning"
    draft = _draft(
        client,
        ["hostnamectl set-hostname web-1"],
        request={"device": "web1", "prompt": "hostname web-1"},
    )[0].json()
    with (
        patch.object(cp, "guest_credentials_for_image", return_value=("root", "pw")),
        patch.object(cp, "list_live_nodes", return_value=[]),
    ):
        started = client.post(f"{_URL}/push", json={"draft_id": draft["draft_id"]})
        job = _wait_for(client, started.json()["job_id"])
    assert job["results"][0]["ok"] is True
    spec = process_manager.StartProgramInGuest.call_args.args[2]
    assert spec.programPath == "/bin/bash"
    assert "hostnamectl set-hostname web-1" in spec.arguments


def test_the_generator_gets_untouched_linux_port_names(client, mixed_nodes):
    edges = client.app.state.graph._edges
    edges.append(("web1", "ens160", "SW1", "Ethernet1"))
    try:
        response, upstream = _draft(
            client,
            ["ip address replace 10.0.0.5/24 dev ens160"],
            request={"device": "web1", "prompt": "address 10.0.0.5/24"},
        )
    finally:
        edges.remove(("web1", "ens160", "SW1", "Ethernet1"))
    assert response.status_code == 200, response.text
    context = upstream.call_args.kwargs["json"]["context"]
    assert context["platform"] == "linux" and context["ports"] == ["ens160"]
    assert context["links"] == [
        {"port": "ens160", "neighbor": "SW1", "neighbor_port": "Ethernet1"}
    ]
