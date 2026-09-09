from unittest.mock import MagicMock, patch

import pytest

import app.vm_screen as vs


def _extra_config(**kv):
    return [MagicMock(key=k, value=v) for k, v in kv.items()]


def test_existing_vnc_config_case_insensitive_enabled_flag():
    """
    Real regression test: ESXi echoes RemoteDisplay.vnc.enabled back as
    "TRUE" (uppercase) regardless of the case it was written in. An
    exact-match check here made /prepare think VNC was never configured
    on every single call, re-writing config and power-cycling the VM
    every time instead of being idempotent after the first call.
    """
    vm = MagicMock()
    vm.config.extraConfig = _extra_config(**{
        "RemoteDisplay.vnc.enabled": "TRUE",
        "RemoteDisplay.vnc.port": "5900",
        "RemoteDisplay.vnc.password": "secret",
    })
    assert vs._existing_vnc_config(vm) == (5900, "secret")


def test_existing_vnc_config_none_when_disabled():
    vm = MagicMock()
    vm.config.extraConfig = _extra_config(**{"RemoteDisplay.vnc.enabled": "false"})
    assert vs._existing_vnc_config(vm) is None


def test_existing_vnc_config_none_when_no_extra_config():
    vm = MagicMock()
    vm.config.extraConfig = None
    assert vs._existing_vnc_config(vm) is None


def test_ensure_vnc_enabled_is_idempotent_when_already_configured():
    """Already-enabled VM: no reconfigure, no power cycle, existing port/password returned."""
    vm = MagicMock()
    vm.name = "VM1"
    vm.config.extraConfig = _extra_config(**{
        "RemoteDisplay.vnc.enabled": "TRUE",
        "RemoteDisplay.vnc.port": "5900",
        "RemoteDisplay.vnc.password": "secret",
    })

    port, password, just_enabled = vs.ensure_vnc_enabled(vm, {})

    assert (port, password, just_enabled) == (5900, "secret", False)
    vm.ReconfigVM_Task.assert_not_called()
    vm.PowerOffVM_Task.assert_not_called()


def test_ensure_vnc_enabled_first_time_reconfigures_and_power_cycles_if_running():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM1"
    vm.config.extraConfig = []
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

    with patch.object(vs, "_wait_until_port_open", return_value=True):
        port, password, just_enabled = vs.ensure_vnc_enabled(vm, {})

    assert just_enabled is True
    assert port == 5900  # first port in the pool
    assert password  # a real random password was generated
    vm.ReconfigVM_Task.assert_called_once()
    vm.PowerOffVM_Task.assert_called_once()
    vm.PowerOnVM_Task.assert_called_once()


def test_ensure_vnc_enabled_skips_power_cycle_if_already_off():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM1"
    vm.config.extraConfig = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff
    vm.ReconfigVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )

    with patch.object(vs, "_wait_until_port_open", return_value=True):
        _port, _password, just_enabled = vs.ensure_vnc_enabled(vm, {})

    assert just_enabled is True
    vm.PowerOffVM_Task.assert_not_called()
    vm.PowerOnVM_Task.assert_not_called()


def test_ensure_vnc_enabled_assigns_different_ports_to_different_vms():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM2"
    vm.config.extraConfig = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff
    vm.ReconfigVM_Task.return_value = MagicMock(
        info=MagicMock(state=vim.TaskInfo.State.success)
    )

    assigned = {"VM1": 5900}
    with patch.object(vs, "_wait_until_port_open", return_value=True):
        port, _password, _just_enabled = vs.ensure_vnc_enabled(vm, assigned)

    assert port == 5901


def test_ensure_vnc_enabled_raises_when_port_pool_exhausted():
    from pyVmomi import vim

    vm = MagicMock()
    vm.name = "VM-overflow"
    vm.config.extraConfig = []
    vm.runtime.powerState = vim.VirtualMachine.PowerState.poweredOff

    full_pool = {f"other-{p}": p for p in vs._VNC_PORT_RANGE}
    with pytest.raises(RuntimeError, match="no free VNC port"):
        vs.ensure_vnc_enabled(vm, full_pool)
