from unittest.mock import MagicMock, patch

import pytest

import app.vm_console as vc


def _serial_port_device(uri: str):
    from pyVmomi import vim

    backing = MagicMock(spec=vim.vm.device.VirtualSerialPort.URIBackingInfo)
    backing.serviceURI = uri
    device = MagicMock(spec=vim.vm.device.VirtualSerialPort)
    device.backing = backing
    return device


def test_existing_serial_console_found():
    vm = MagicMock()
    vm.config.hardware.device = [_serial_port_device("tcp://:2000")]
    assert vc._existing_serial_console(vm) == 2000


def test_existing_serial_console_none_when_no_serial_port():
    vm = MagicMock()
    vm.config.hardware.device = []
    assert vc._existing_serial_console(vm) is None


def test_existing_serial_console_none_when_uri_has_no_port():
    vm = MagicMock()
    vm.config.hardware.device = [_serial_port_device("tcp://")]
    assert vc._existing_serial_console(vm) is None


def test_ensure_serial_console_is_idempotent_when_already_configured():
    """Already-configured VM: no reconfigure, no power cycle, existing port returned."""
    vm = MagicMock()
    vm.name = "VM1"
    vm.config.hardware.device = [_serial_port_device("tcp://:2000")]

    port, just_enabled = vc.ensure_serial_console(vm, {})

    assert (port, just_enabled) == (2000, False)
    vm.ReconfigVM_Task.assert_not_called()
    vm.PowerOffVM_Task.assert_not_called()


def test_ensure_serial_console_first_time_reconfigures_and_power_cycles_if_running():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM1"
    vm.config.hardware.device = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOn
    vm.ReconfigVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )
    vm.PowerOffVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )
    vm.PowerOnVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )

    with patch.object(vc, "_wait_until_port_open", return_value=True):
        port, just_enabled = vc.ensure_serial_console(vm, {})

    assert just_enabled is True
    assert port == 2000  # first port in the pool
    vm.ReconfigVM_Task.assert_called_once()
    vm.PowerOffVM_Task.assert_called_once()
    vm.PowerOnVM_Task.assert_called_once()
    vm.runtime.host.configManager.firewallSystem.EnableRuleset.assert_called_once_with(
        "remoteSerialPort"
    )


def test_ensure_serial_console_skips_power_cycle_if_already_off():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM1"
    vm.config.hardware.device = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff
    vm.ReconfigVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )

    with patch.object(vc, "_wait_until_port_open", return_value=True):
        _port, just_enabled = vc.ensure_serial_console(vm, {})

    assert just_enabled is True
    vm.PowerOffVM_Task.assert_not_called()
    vm.PowerOnVM_Task.assert_not_called()


def test_ensure_serial_console_assigns_different_ports_to_different_vms():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM2"
    vm.config.hardware.device = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff
    vm.ReconfigVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )

    assigned = {"VM1": 2000}
    with patch.object(vc, "_wait_until_port_open", return_value=True):
        port, _just_enabled = vc.ensure_serial_console(vm, assigned)

    assert port == 2001


def test_ensure_serial_console_raises_when_port_pool_exhausted():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM-overflow"
    vm.config.hardware.device = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff

    full_pool = {f"other-{p}": p for p in vc._SERIAL_PORT_RANGE}
    with pytest.raises(RuntimeError, match="no free serial port"):
        vc.ensure_serial_console(vm, full_pool)


def test_prepare_vm_console_happy_path(client, app_state):
    fake_vm = MagicMock()
    app_state.esxi_connection.get_vm.return_value = fake_vm

    with patch.object(vc, "ensure_serial_console", return_value=(2000, True)):
        resp = client.post("/vm-console/VM1/prepare")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"power_cycled": True}


def test_prepare_vm_console_unknown_node_maps_to_404(client, app_state):
    resp = client.post("/vm-console/GHOST/prepare")
    assert resp.status_code == 404


def test_prepare_vm_console_non_esxi_node_maps_to_404(client, app_state):
    resp = client.post("/vm-console/R1/prepare")
    assert resp.status_code == 404


def test_prepare_vm_console_vm_not_found_maps_to_404(client, app_state):
    app_state.esxi_connection.get_vm.return_value = None
    resp = client.post("/vm-console/VM1/prepare")
    assert resp.status_code == 404


def test_prepare_vm_console_runtime_error_maps_to_503(client, app_state):
    fake_vm = MagicMock()
    app_state.esxi_connection.get_vm.return_value = fake_vm

    with patch.object(vc, "ensure_serial_console", side_effect=RuntimeError("no free port")):
        resp = client.post("/vm-console/VM1/prepare")

    assert resp.status_code == 503
