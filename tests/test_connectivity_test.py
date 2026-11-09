from types import SimpleNamespace

import pytest
from src.graph.environment import Environment

import app.connectivity_test as ct
from app.config_push import CommandResult, DeviceResult


class FakeNode:
    def __init__(self, name, role, image="debian", env=Environment.ON_ESXI):
        self.name = name
        self.role = role
        self.image = image
        self.env = env
        self.interfaces = {}

    def get_neighbour(self, interface):
        return self.interfaces[interface]


def connect(a, b):
    a.interfaces[f"{a.name}->{b.name}"] = b
    b.interfaces[f"{b.name}->{a.name}"] = a


def lab():
    """SW1 joins LXA, LXB and WIN; LXC hangs behind the router R1 on its own."""
    nodes = {
        "SW1": FakeNode("SW1", "SWITCH", "Cisco IOSvL2", Environment.ON_GNS3),
        "R1": FakeNode("R1", "ROUTER", "Cisco IOSv", Environment.ON_GNS3),
        "LXA": FakeNode("LXA", "VM"),
        "LXB": FakeNode("LXB", "VM"),
        "WIN": FakeNode("WIN", "VM", "Windows Server 2022 Standard"),
        "LXC": FakeNode("LXC", "PC"),
    }
    for name in ("LXA", "LXB", "WIN"):
        connect(nodes["SW1"], nodes[name])
    connect(nodes["SW1"], nodes["R1"])
    connect(nodes["R1"], nodes["LXC"])
    return SimpleNamespace(nodes=nodes)


def test_vms_on_one_switch_form_a_segment_and_a_vm_behind_a_router_is_skipped():
    hosts, skipped = ct.plan(lab())
    assert [(h.name, h.address) for h in hosts] == [
        ("LXA", "198.18.200.1"),
        ("LXB", "198.18.200.2"),
        ("WIN", "198.18.200.3"),
    ]
    assert [h.windows for h in hosts] == [False, False, True]
    assert "router" in skipped["LXC"]


def test_two_vms_linked_directly_are_a_segment_of_their_own():
    graph = lab()
    graph.nodes["D1"] = FakeNode("D1", "VM")
    graph.nodes["D2"] = FakeNode("D2", "VM")
    connect(graph.nodes["D1"], graph.nodes["D2"])
    hosts, _ = ct.plan(graph)
    by_segment = {}
    for host in hosts:
        by_segment.setdefault(host.segment, []).append(host.address)
    assert sorted(by_segment.values()) == [
        ["198.18.200.1", "198.18.200.2"],
        ["198.18.201.1", "198.18.201.2", "198.18.201.3"],
    ]


def test_a_named_subset_is_planned_and_unknown_names_are_refused():
    hosts, _ = ct.plan(lab(), ["LXA", "LXB"])
    assert [h.name for h in hosts] == ["LXA", "LXB"]
    with pytest.raises(ValueError, match="NOPE"):
        ct.plan(lab(), ["NOPE"])


def test_nothing_to_test_when_no_two_vms_share_a_segment():
    graph = SimpleNamespace(nodes={"LXC": FakeNode("LXC", "VM")})
    with pytest.raises(ct.NothingToTest):
        ct.plan(graph)


def test_a_vm_that_is_not_on_esxi_is_skipped():
    graph = lab()
    graph.nodes["LXB"].env = Environment.ON_GNS3
    hosts, skipped = ct.plan(graph)
    assert [h.name for h in hosts] == ["LXA", "WIN"]
    assert "not an ESXi VM" in skipped["LXB"]


def test_commands_differ_by_os_and_cleanup_undoes_the_setup():
    linux = ct.Host("LXA", "198.18.200.1", windows=False, segment=0)
    windows = ct.Host("WIN", "198.18.200.3", windows=True, segment=0)
    assert "ip addr add 198.18.200.1/24" in ct.setup_commands(linux)[0]
    assert "managed no" in ct.setup_commands(linux)[0]
    assert "ip addr del 198.18.200.1/24" in ct.cleanup_commands(linux)[0]
    assert "managed yes" in ct.cleanup_commands(linux)[0]
    assert "New-NetIPAddress" in ct.setup_commands(windows)[0]
    assert ct.FIREWALL_RULE_NAME in ct.setup_commands(windows)[0]
    assert "Remove-NetIPAddress" in ct.cleanup_commands(windows)[0]
    assert "Remove-NetFirewallRule" in ct.cleanup_commands(windows)[0]
    assert ct.ping_command(linux, windows, 5, 100) == (
        "ping -c 5 -W 2 -s 100 -q 198.18.200.3"
    )
    assert ct.ping_command(windows, linux, 5, 100) == (
        "ping -n 5 -w 2000 -l 100 198.18.200.1"
    )


def test_ping_output_of_linux_and_of_german_and_english_windows_is_read():
    linux = (
        "10 packets transmitted, 9 received, 10% packet loss, time 9012ms\n"
        "rtt min/avg/max/mdev = 4.1/11.5/21.4/4.5 ms"
    )
    assert ct.parse_ping(linux, 10) == (10, 9, 11.5)
    german = "Pakete: Gesendet = 4, Empfangen = 4, Verloren = 0\nMittelwert = 3ms"
    assert ct.parse_ping(german, 4) == (4, 4, 3.0)
    english = "Packets: Sent = 4, Received = 0, Lost = 4 (100% loss)"
    assert ct.parse_ping(english, 4) == (4, 0, None)
    assert ct.parse_ping("connect: Network is unreachable", 7) == (7, 0, None)


def result(device, ok=True, outputs=(), error=None):
    return DeviceResult(
        device=device,
        transport="guest_ops",
        ok=ok,
        error=error,
        commands=[CommandResult(command="c", output=o, ok=True) for o in outputs],
    )


def run_with(push, count=2):
    hosts, _ = ct.plan(lab())
    job = ct.ConnectivityJob(id="j")
    ct.run(SimpleNamespace(), job, hosts, count, 56, push=push)
    return job


GOOD_PING = "2 packets transmitted, 2 received, 0% packet loss\nrtt min/avg/max/mdev = 1/2/3/1 ms"


def test_a_run_sets_pings_and_always_cleans_up():
    calls = []

    def push(devices, app, on_progress=None):
        calls.append(devices)
        if len(calls) == 2:  # the pings: two peers each
            return [result(name, outputs=[GOOD_PING, GOOD_PING]) for name in devices]
        return [result(name) for name in devices]

    job = run_with(push)

    assert job.status == "done" and job.error is None and job.problems == {}
    assert len(job.pairs) == 6
    assert all(p["received"] == p["sent"] == 2 for p in job.pairs)
    assert job.pairs[0]["rtt_avg_ms"] == 2.0
    assert len(calls) == 3
    assert set(calls[0]) == {"LXA", "LXB", "WIN"}
    assert all(spec["continue_on_error"] for spec in calls[1].values())
    assert set(calls[2]) == {"LXA", "LXB", "WIN"}  # the clean-up reaches every VM
    assert job.snapshot()["all_ok"] is True


def test_a_vm_whose_setup_failed_is_not_pinged_and_still_cleaned_up():
    calls = []

    def push(devices, app, on_progress=None):
        calls.append(devices)
        if len(calls) == 1:
            return [
                result("LXA"),
                result("LXB"),
                result("WIN", ok=False, error="VMware Tools not running"),
            ]
        if len(calls) == 2:
            return [result(name, outputs=[GOOD_PING]) for name in devices]
        return [result(name) for name in devices]

    job = run_with(push)

    assert set(calls[1]) == {"LXA", "LXB"}
    assert {(p["source"], p["target"]) for p in job.pairs} == {
        ("LXA", "LXB"),
        ("LXB", "LXA"),
    }
    assert "WIN (setup)" in job.problems
    assert set(calls[2]) == {"LXA", "LXB", "WIN"}
    assert job.snapshot()["all_ok"] is False


def test_lost_pings_are_reported_as_loss():
    def push(devices, app, on_progress=None):
        if all(
            isinstance(s, dict) and len(s["commands"]) == 2 for s in devices.values()
        ):
            bad = "2 packets transmitted, 0 received, 100% packet loss"
            return [
                result(name, ok=False, outputs=[bad, GOOD_PING]) for name in devices
            ]
        return [result(name) for name in devices]

    job = run_with(push)
    lost = [p for p in job.pairs if p["received"] == 0]
    assert len(lost) == 3 and all(p["loss_percent"] == 100.0 for p in lost)
    assert job.snapshot()["all_ok"] is False


def test_the_cleanup_runs_even_when_the_pinging_blows_up():
    calls = []

    def push(devices, app, on_progress=None):
        calls.append(devices)
        if len(calls) == 2:
            raise RuntimeError("ESXi went away")
        return [result(name) for name in devices]

    job = run_with(push)

    assert job.status == "error" and "ESXi went away" in job.error
    assert len(calls) == 3


def test_a_failing_cleanup_says_which_address_to_remove():
    calls = []

    def push(devices, app, on_progress=None):
        calls.append(devices)
        if len(calls) == 3:
            return [result("LXA", ok=False, error="guest not reachable")]
        if len(calls) == 2:
            return [result(name, outputs=[GOOD_PING, GOOD_PING]) for name in devices]
        return [result(name) for name in devices]

    job = run_with(push)
    assert "198.18.200.1" in job.problems["LXA (cleanup)"]


def test_start_job_refuses_bad_parameters_and_a_second_run(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace(graph=lab()))
    monkeypatch.setattr(
        ct.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None)
    )
    with pytest.raises(ValueError):
        ct.start_job(app, count=0)
    with pytest.raises(ValueError):
        ct.start_job(app, size=5000)
    job = ct.start_job(app, count=3)
    assert app.state.connectivity_job is job
    assert job.segments == [
        {
            "segment": 0,
            "hosts": {
                "LXA": "198.18.200.1",
                "LXB": "198.18.200.2",
                "WIN": "198.18.200.3",
            },
        }
    ]
    with pytest.raises(ct.AlreadyRunning):
        ct.start_job(app)


def test_endpoints(client, app_state, monkeypatch):
    app_state.graph = lab()
    monkeypatch.setattr(
        ct.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None)
    )
    started = client.post("/maintenance/connectivity-test", json={"count": 3})
    assert started.status_code == 202
    body = started.json()
    assert "LXC" in body["skipped"]
    status = client.get(f"/maintenance/connectivity-test/{body['job_id']}")
    assert status.status_code == 200 and status.json()["status"] == "running"
    assert client.post("/maintenance/connectivity-test").status_code == 409
    assert client.get("/maintenance/connectivity-test/nope").status_code == 404
    app_state.connectivity_job.status = "done"
    assert (
        client.post("/maintenance/connectivity-test", json={"count": 0}).status_code
        == 400
    )
    assert (
        client.post(
            "/maintenance/connectivity-test", json={"nodes": ["NOPE"]}
        ).status_code
        == 400
    )


def test_a_config_push_waits_while_a_connectivity_test_runs(client, app_state):
    app_state.connectivity_job = ct.ConnectivityJob(id="busy")
    response = client.post("/config-push/jobs", json={"devices": {"VM1": ["echo"]}})
    assert response.status_code == 409
    assert "connectivity test" in response.json()["detail"]
    app_state.connectivity_job = None
