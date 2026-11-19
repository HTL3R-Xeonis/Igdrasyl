"""Guest interface names of ESXi VMs (app/guest_nics.py), with fakes for ESXi, VMware Tools and the guest."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import guest_nics

# The VM measured live on ESXi (Rocky 9.2): interfaces added as eth2, eth0, eth1, eth3 hot-added later.
_MACS = {
    "eth2": "00:50:56:a1:00:01",
    "eth0": "00:50:56:a1:00:02",
    "eth1": "00:50:56:a1:00:03",
    "eth3": "00:50:56:a1:00:04",
}
_IP_LINK = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
    "2: ens160: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether 00:50:56:a1:00:01 brd ff:ff:ff:ff:ff:ff\\    altname enp3s0\n"
    "3: ens192: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether 00:50:56:a1:00:02 brd ff:ff:ff:ff:ff:ff\\    altname enp11s0\n"
    "4: ens224: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether 00:50:56:a1:00:03 brd ff:ff:ff:ff:ff:ff\\    altname enp19s0\n"
    "5: ens161: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether 00:50:56:a1:00:04 brd ff:ff:ff:ff:ff:ff\\    altname enp3s1\n"
    "6: ens160.10@ens160: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT group default qlen 1000\\    link/ether 00:50:56:a1:00:01 brd ff:ff:ff:ff:ff:ff\n"
)
_WINDOWS_PIPE = (
    "Ethernet0|00-50-56-A1-00-01\r\n"
    "Ethernet1|00-50-56-A1-00-02\r\n"
    "Ethernet 2|00-50-56-A1-00-03\r\n"
)
_WINDOWS_TABLE = (
    "Name                      InterfaceDescription                    ifIndex Status       MacAddress             LinkSpeed\n"
    "----                      --------------------                    ------- ------       ----------             ---------\n"
    "Ethernet0                 vmxnet3 Ethernet Adapter                      7 Up           00-50-56-A1-00-01        10 Gbps\n"
    "Ethernet 2                vmxnet3 Ethernet Adapter #2                   9 Up           00-50-56-A1-00-02        10 Gbps\n"
)


def _vm(tools_running=True):
    return SimpleNamespace(
        name="web1",
        guest=SimpleNamespace(
            toolsRunningStatus="guestToolsRunning"
            if tools_running
            else "guestToolsNotRunning",
            guestOperationsReady=tools_running,
        ),
    )


class _Esxi:
    """Just what the resolver asks the connection: the VM, the MAC behind a port group and the guest-ops managers."""

    def __init__(self, vm, macs):
        self._vm = vm
        self._macs = macs  # port group -> MAC
        self.content = MagicMock()

    def get_vm(self, name):
        return self._vm

    def find_vm_nic_mac_by_port_group(self, vm, port_group):
        return self._macs.get(port_group)


def _node(image="Rocky 9.2", interfaces=()):
    node = SimpleNamespace(name="web1", image=image, interfaces={})
    for name in interfaces:
        node.interfaces[name] = SimpleNamespace(vlan=None)
    return node


def _app(esxi):
    return SimpleNamespace(state=SimpleNamespace(esxi_connection=esxi))


@pytest.fixture
def guest(monkeypatch):
    """Fake guest-ops: `guest.output` is what the NIC list command prints; the command that ran is kept."""
    state = SimpleNamespace(
        output=_IP_LINK, exit_code=0, commands=[], creds=("root", "s3cret-pw")
    )

    def run(process_manager, vm, auth, command, is_windows, **_):
        assert auth.password == "s3cret-pw"
        state.commands.append((command, is_windows))
        return state.exit_code, state.output

    monkeypatch.setattr(guest_nics, "run_command_in_guest", run)
    monkeypatch.setattr(
        guest_nics, "guest_credentials_for_image", lambda image: state.creds
    )
    return state


def _port_groups(macs, node="web1"):
    return {f"{node}_{name}": mac for name, mac in macs.items()}


def test_linux_nic_list_is_read_by_mac_and_skips_loopback_and_subinterfaces():
    found = guest_nics.parse_linux(_IP_LINK)
    assert found == {
        "00:50:56:a1:00:01": ["ens160"],
        "00:50:56:a1:00:02": ["ens192"],
        "00:50:56:a1:00:03": ["ens224"],
        "00:50:56:a1:00:04": ["ens161"],
    }


def test_windows_nic_list_in_pipe_and_table_form():
    assert guest_nics.parse_windows(_WINDOWS_PIPE) == {
        "00:50:56:a1:00:01": ["Ethernet0"],
        "00:50:56:a1:00:02": ["Ethernet1"],
        "00:50:56:a1:00:03": ["Ethernet 2"],
    }
    assert guest_nics.parse_windows(_WINDOWS_TABLE) == {
        "00:50:56:a1:00:01": ["Ethernet0"],
        "00:50:56:a1:00:02": ["Ethernet 2"],
    }
    assert guest_nics.parse_windows("garbage\nwithout any address\n") == {}
    assert guest_nics.parse_linux("") == {}


def test_the_hot_added_nic_gets_its_own_guest_name(guest):
    """eth2=ens160, eth0=ens192, eth1=ens224 and the newest NIC eth3=ens161 (it sorts second)."""
    esxi = _Esxi(_vm(), _port_groups(_MACS))
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0", "eth1", "eth2", "eth3"])
    assert result.names == {
        "eth2": "ens160",
        "eth0": "ens192",
        "eth1": "ens224",
        "eth3": "ens161",
    }
    assert result.warnings == []
    assert guest.commands == [("ip -o link", False)]


def test_the_port_group_name_comes_from_the_graph_and_replaces_slashes(guest):
    node = _node(interfaces=["gi0/1"])
    node.interfaces["gi0/1"].vlan = SimpleNamespace(name="web1_custom")
    esxi = _Esxi(_vm(), {"web1_custom": _MACS["eth0"], "web1_ens-3": _MACS["eth1"]})
    result = guest_nics.resolve(_app(esxi), node, ["gi0/1", "ens/3"])
    assert result.names == {"gi0/1": "ens192", "ens/3": "ens224"}


def test_windows_adapters_are_read_with_a_powershell_command(guest):
    guest.output = _WINDOWS_PIPE
    macs = {"eth0": "00:50:56:A1:00:01", "eth1": "00:50:56:a1:00:03"}
    esxi = _Esxi(_vm(), _port_groups(macs))
    result = guest_nics.resolve(_app(esxi), _node("Windows 10 Pro"), ["eth0", "eth1"])
    assert result.names == {"eth0": "Ethernet0", "eth1": "Ethernet 2"}
    ((command, is_windows),) = guest.commands
    assert is_windows is True
    assert command.startswith("powershell -NoProfile") and "-EncodedCommand" in command


def test_vmware_tools_not_running_falls_back_with_a_warning(guest):
    esxi = _Esxi(_vm(tools_running=False), _port_groups(_MACS))
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0"])
    assert result.names == {}
    assert result.warnings == [guest_nics.FALLBACK_WARNING]
    assert guest.commands == []


def test_a_mac_that_the_guest_does_not_list_falls_back_with_a_warning(guest):
    esxi = _Esxi(_vm(), _port_groups({"eth0": "00:50:56:ff:ff:ff"}))
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0"])
    assert result.names == {}
    assert result.warnings == [guest_nics.FALLBACK_WARNING]


def test_only_the_interfaces_that_could_not_be_matched_keep_their_topology_name(guest):
    macs = {"eth0": _MACS["eth0"], "eth1": "00:50:56:ff:ff:ff"}
    esxi = _Esxi(_vm(), _port_groups(macs))
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0", "eth1", "eth9"])
    assert result.names == {"eth0": "ens192"}
    assert result.warnings == [
        "could not resolve the guest interface names of eth1, eth9, topology names used"
    ]


def test_two_guest_interfaces_with_one_mac_are_not_guessed(guest):
    guest.output = _IP_LINK + (
        "7: br0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT group default qlen 1000\\    link/ether 00:50:56:a1:00:02 brd ff:ff:ff:ff:ff:ff\n"
    )
    esxi = _Esxi(_vm(), _port_groups({"eth0": _MACS["eth0"], "eth1": _MACS["eth1"]}))
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0", "eth1"])
    assert result.names == {"eth1": "ens224"}
    assert result.warnings == [
        "could not resolve the guest interface name of eth0, topology name used"
    ]


def test_a_node_without_an_esxi_vm_falls_back(guest):
    esxi = _Esxi(None, {})
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0"])
    assert result.names == {} and result.warnings == [guest_nics.FALLBACK_WARNING]
    assert guest.commands == []


def test_no_nic_on_the_port_groups_falls_back_without_reading_the_guest(guest):
    result = guest_nics.resolve(_app(_Esxi(_vm(), {})), _node(), ["eth0"])
    assert result.warnings == [guest_nics.FALLBACK_WARNING]
    assert guest.commands == []


def test_failing_guest_command_and_missing_credentials_fall_back(guest):
    esxi = _Esxi(_vm(), _port_groups(_MACS))
    guest.exit_code = None
    assert guest_nics.resolve(_app(esxi), _node(), ["eth0"]).warnings == [
        guest_nics.FALLBACK_WARNING
    ]
    guest.exit_code = 0
    guest.creds = None
    assert guest_nics.resolve(_app(esxi), _node(), ["eth0"]).warnings == [
        guest_nics.FALLBACK_WARNING
    ]
    guest.creds = ("root", "s3cret-pw")
    guest.output = "exit code 0"
    assert guest_nics.resolve(_app(esxi), _node(), ["eth0"]).warnings == [
        guest_nics.FALLBACK_WARNING
    ]


def test_an_unexpected_error_never_raises_and_never_logs_its_text(guest, monkeypatch):
    class Boom(Exception):
        pass

    def broken(*_args, **_kwargs):
        raise Boom("login failed for root with s3cret-pw")

    monkeypatch.setattr(guest_nics, "run_command_in_guest", broken)
    logged = []
    monkeypatch.setattr(
        guest_nics.logger, "warning", lambda *args, **_: logged.append(args)
    )
    esxi = _Esxi(_vm(), _port_groups(_MACS))
    result = guest_nics.resolve(_app(esxi), _node(), ["eth0"])
    assert result.warnings == [guest_nics.FALLBACK_WARNING]
    assert logged and "s3cret-pw" not in repr(logged) and "Boom" in repr(logged)


def test_nothing_is_asked_for_a_node_without_interfaces():
    esxi = MagicMock()
    result = guest_nics.resolve(_app(esxi), _node(), [])
    assert result.names == {} and result.warnings == []
    esxi.get_vm.assert_not_called()
