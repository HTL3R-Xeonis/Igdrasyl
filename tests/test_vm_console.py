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

    with patch.object(vc, "ensure_serial_console", return_value=(2000, True)), patch.object(
        vc, "ensure_guest_console_configured", return_value=True
    ):
        resp = client.post("/vm-console/VM1/prepare")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"power_cycled": True, "guest_console_configured": True}


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


def test_ensure_guest_console_configured_skips_windows_image():
    vm = MagicMock()
    with patch.object(vc, "guest_credentials_for_image") as mock_creds:
        result = vc.ensure_guest_console_configured(vm, "Windows Server 2022 Standard", MagicMock())

    assert result is None
    mock_creds.assert_not_called()


def test_ensure_guest_console_configured_skips_when_tools_not_running():
    """
    Real bug found live: OPNsense (a FreeBSD appliance with no VMware
    Tools installed) hit this exact path - guest-ops can't run at all
    without Tools, regardless of credentials, so this must be checked
    before ever touching guest_credentials_for_image/run_command_in_guest.
    """
    vm = MagicMock()
    vm.guest.toolsRunningStatus = "guestToolsNotRunning"
    with patch.object(vc, "guest_credentials_for_image") as mock_creds:
        result = vc.ensure_guest_console_configured(vm, "OPNsense", MagicMock())

    assert result is None
    mock_creds.assert_not_called()


def test_ensure_guest_console_configured_skips_when_no_credentials():
    vm = MagicMock()
    vm.guest.toolsRunningStatus = "guestToolsRunning"
    with patch.object(vc, "guest_credentials_for_image", return_value=None):
        result = vc.ensure_guest_console_configured(vm, "debian", MagicMock())

    assert result is None


def test_ensure_guest_console_configured_idempotent_when_already_configured():
    vm = MagicMock()
    vm.guest.toolsRunningStatus = "guestToolsRunning"
    with patch.object(
        vc, "guest_credentials_for_image", return_value=("junioradmin", "junioradmin")
    ), patch.object(vc, "run_command_in_guest", return_value=(0, "exit code 0")) as mock_run:
        result = vc.ensure_guest_console_configured(vm, "debian", MagicMock())

    assert result is False
    mock_run.assert_called_once()
    vm.PowerOffVM_Task.assert_not_called()
    vm.PowerOnVM_Task.assert_not_called()


def test_ensure_guest_console_configured_first_time_configures_and_power_cycles():
    """
    Real regression test: a soft vm.RebootGuest() was confirmed live
    against real infrastructure to NOT reliably bring the network-backed
    serial port back - only a full VM power cycle does (see this
    function's own docstring). Asserts the power-cycle path specifically,
    not RebootGuest.
    """
    from pyVmomi import vim

    vm = MagicMock()
    vm.guest.toolsRunningStatus = "guestToolsRunning"
    vm.PowerOffVM_Task.return_value = MagicMock(info=MagicMock(state=vim.TaskInfo.State.success))
    vm.PowerOnVM_Task.return_value = MagicMock(info=MagicMock(state=vim.TaskInfo.State.success))
    with patch.object(
        vc, "guest_credentials_for_image", return_value=("junioradmin", "junioradmin")
    ), patch.object(
        vc,
        "run_command_in_guest",
        side_effect=[(1, "exit code 1"), (0, "exit code 0"), (0, "exit code 0")],
    ) as mock_run, patch.object(vc, "_existing_serial_console", return_value=None):
        result = vc.ensure_guest_console_configured(vm, "debian", MagicMock())

    assert result is True
    assert mock_run.call_count == 3
    vm.PowerOffVM_Task.assert_called_once()
    vm.PowerOnVM_Task.assert_called_once()
    vm.RebootGuest.assert_not_called()


def test_ensure_guest_console_configured_gives_up_if_script_write_fails():
    vm = MagicMock()
    vm.guest.toolsRunningStatus = "guestToolsRunning"
    with patch.object(
        vc, "guest_credentials_for_image", return_value=("junioradmin", "junioradmin")
    ), patch.object(
        vc, "run_command_in_guest", side_effect=[(1, "exit code 1"), (1, "exit code 1")]
    ):
        result = vc.ensure_guest_console_configured(vm, "debian", MagicMock())

    assert result is None
    vm.PowerOffVM_Task.assert_not_called()
    vm.PowerOnVM_Task.assert_not_called()
