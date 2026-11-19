"""
Config-push engine: sends a device-name-keyed list of raw CLI commands to
each device, transport auto-selected from its role/env (already known
from the live topology graph) - GNS3 console devices go over Telnet via
Netmiko, ESXi-hosted VMs go over pyvmomi's Guest Operations API. Per the
project's design: stop pushing a device's remaining
commands on its first failure, but always continue on to the next
device - implemented by catching/recording failures per-device rather
than letting one raise out of push_all.

Deliberately NOT using Netmiko's send_config_set()/enable() abstractions
- commands are sent exactly as given, one at a time, uniformly across
the whole heterogeneous device mix (Cisco IOS, VPCS, Linux, Windows).
That's the whole reason this project's config file holds raw command
lists instead of a structured/templated schema: a uniform "send this
verbatim" transport works across all of them without a per-vendor
renderer. send_command_timing() (not the stricter, prompt-pattern-based
send_command()) is used for the same reason - it tolerates the prompt
actually changing mid-sequence (e.g. after "configure terminal"),
which a family of commands representing modal CLI transitions needs.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel
from src.graph.blocks.generic_node import GenericNode
from src.graph.environment import Environment
from src.settings import Settings

from app import esxi_serial, push_directives, serial_agent
from app.esxi_serial import serial_port_of
from app.gns3_state import NoLiveProject, compute_load, list_live_nodes
from app.guest_ops import guest_credentials_for_image, run_command_in_guest

# (device, commands done, commands total) - lets a long push report its progress.
ProgressCallback = Callable[[str, int, int], None]

_NETMIKO_CONNECT_TIMEOUT_SECONDS = 20

# USB HID usage 0xE1 (left shift) on the virtual keyboard: wakes a sleeping guest, types nothing.
_WAKE_KEY_CODE = (0xE1 << 16) | 7
_WAKE_WAIT_SECONDS = 20
_WAKE_POLL_SECONDS = 2
_RESUME_WAIT_SECONDS = 120

# Minimal, deliberately narrow: only recognized for the cisco_ios_telnet
# device type, where these prefixes are well-established. No attempt is
# made to detect failures on the generic_telnet fallback (VPCS, unknown
# devices) - there's no reliable, vendor-neutral signal for that, so
# those commands are always recorded as "ok" (best-effort, not verified).
_IOS_ERROR_MARKERS = ("% invalid", "% incomplete", "% unrecognized", "% ambiguous")


class CommandResult(BaseModel):
    command: str
    output: str
    ok: bool


# A long push must not produce a gigantic answer: beyond this many executed commands
# only the failed ones and the last few are kept in the result (see _compact).
MAX_RESULT_COMMANDS = 300
_KEPT_TAIL = 50
_COMPACT_OUTPUT_CHARS = 2000


class DeviceSpec(BaseModel):
    """
    One device's push: just a command list, or - to choose the way in - an object.
    via: "auto" (guest operations when VMware Tools runs, else the serial console if the
    VM has one), "guest_ops" or "serial". profile/username/password only matter for the
    serial console (default: by image, and the template catalog's account).
    continue_on_error: keep running the remaining commands after one that failed (a ping
    without answer, a grep without hit); the device still counts as failed. Default: stop at
    the first failing command. A command that could not even be started, a lost connection
    and a failed @reboot always stop the device.
    native_shell: run the commands the way the device's own shell expects them - a Cisco IOS console
    gets them in global configuration mode (entered before the first command, left again afterwards,
    also after a failure) and a Windows VM runs each one in PowerShell instead of cmd.exe; every other
    device gets them verbatim. Default: verbatim everywhere.
    """

    commands: list[str]
    continue_on_error: bool = False
    native_shell: bool = False
    via: Literal["auto", "guest_ops", "serial"] = "auto"
    profile: str | None = None
    username: str | None = None
    password: str | None = None


DeviceEntry = list[str] | DeviceSpec


def normalize(devices: dict[str, Any]) -> dict[str, DeviceSpec]:
    """Every entry as a DeviceSpec (a plain command list becomes {commands: [...], via: auto})."""
    result: dict[str, DeviceSpec] = {}
    for name, entry in devices.items():
        if isinstance(entry, DeviceSpec):
            result[name] = entry
        elif isinstance(entry, dict):
            result[name] = DeviceSpec(**entry)
        else:
            result[name] = DeviceSpec(commands=list(entry))
    return result


def _compact(results: list[CommandResult]) -> tuple[list[CommandResult], bool]:
    """Shrinks a very long per-command list to the failed commands plus the tail."""
    if len(results) <= MAX_RESULT_COMMANDS:
        return results, False
    failed = [i for i, r in enumerate(results) if not r.ok][
        : MAX_RESULT_COMMANDS - _KEPT_TAIL
    ]
    keep = sorted(set(failed) | set(range(len(results) - _KEPT_TAIL, len(results))))
    kept = [
        CommandResult(
            command=results[i].command,
            output=results[i].output[:_COMPACT_OUTPUT_CHARS],
            ok=results[i].ok,
        )
        for i in keep
    ]
    return kept, True


class DeviceResult(BaseModel):
    device: str
    transport: str
    ok: bool
    commands: list[CommandResult] = []
    error: str | None = None
    # How many commands the device was given / how many actually ran; with a very
    # long list `commands` holds only the failed ones and the tail (truncated=True).
    total_commands: int = 0
    executed: int = 0
    truncated: bool = False
    # True when the device could not be reached at all and NO command ran (still
    # booting, VMware Tools not up yet): trying the whole list again is safe.
    # False once a command was executed - repeating a half-applied config is not.
    not_ready: bool = False


def _finish(
    device: str,
    transport: str,
    ok: bool,
    results: list[CommandResult],
    total: int,
    error: str | None = None,
    not_ready: bool = False,
) -> DeviceResult:
    kept, truncated = _compact(results)
    return DeviceResult(
        device=device,
        transport=transport,
        ok=ok,
        commands=kept,
        error=error,
        not_ready=not_ready,
        total_commands=total,
        executed=len(results),
        truncated=truncated,
    )


def _netmiko_device_type(image: str) -> str:
    if "cisco ios" in image.lower():
        return "cisco_ios_telnet"
    return "generic_telnet"


def _looks_like_ios_error(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _IOS_ERROR_MARKERS)


# A command that already names its interpreter is left alone by native_shell.
_EXPLICIT_WINDOWS_SHELL = re.compile(
    r"^\s*(?:cmd|powershell|pwsh)(?:\.exe)?(?:\s|$)", re.IGNORECASE
)


def windows_powershell_command(command: str) -> str:
    """
    The cmd.exe command line that runs `command` in PowerShell. The script travels base64-encoded
    (-EncodedCommand), so quotes, pipes and % signs reach PowerShell exactly as written instead of
    being interpreted by cmd.exe first; -OutputFormat Text keeps progress and error records out of
    the output as XML. A command that starts with cmd, powershell or pwsh is returned unchanged.
    """
    if _EXPLICIT_WINDOWS_SHELL.match(command):
        return command
    script = f"$ProgressPreference = 'SilentlyContinue'; {command}"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-OutputFormat Text -EncodedCommand {encoded}"
    )


def _push_via_netmiko(
    node: GenericNode,
    commands: list[str],
    gns3_vm_ip: str,
    console_port: int,
    progress: Callable[[int, int], None] | None = None,
    continue_on_error: bool = False,
    native_shell: bool = False,
) -> DeviceResult:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
        ReadTimeout,
    )

    if push_directives.has_directive(commands):
        return DeviceResult(
            device=node.name,
            transport="netmiko",
            ok=False,
            error=(
                "@reboot / @wait-reboot are not supported on GNS3 console devices "
                "(save the config with 'write memory'; restart the node from the GUI)"
            ),
            total_commands=len(commands),
        )

    device_type = _netmiko_device_type(node.image)
    try:
        conn = ConnectHandler(
            device_type=device_type,
            host=gns3_vm_ip,
            port=console_port,
            username="",
            password="",
            timeout=_NETMIKO_CONNECT_TIMEOUT_SECONDS,
        )
    except ReadTimeout:
        # Connected, but the device never answered the session set-up: still booting (or starved).
        return DeviceResult(
            device=node.name,
            transport="netmiko",
            ok=False,
            error="couldn't connect: the console did not answer in time (the device is still booting)",
            not_ready=True,
            total_commands=len(commands),
        )
    except (NetmikoTimeoutException, NetmikoAuthenticationException, OSError) as exc:
        return DeviceResult(
            device=node.name,
            transport="netmiko",
            ok=False,
            error=f"couldn't connect: {exc}",
            not_ready=True,
            total_commands=len(commands),
        )

    results: list[CommandResult] = []
    overall_ok = True
    not_ready = False
    config_mode = native_shell and device_type == "cisco_ios_telnet"
    in_config_mode = False
    try:
        if config_mode:
            try:
                entered = conn.send_command_timing("configure terminal")
            except ReadTimeout:
                entered = None
            if entered is None or _looks_like_ios_error(entered):
                return DeviceResult(
                    device=node.name,
                    transport="netmiko",
                    ok=False,
                    error=(
                        "the console did not answer in time (the device is still booting)"
                        if entered is None
                        else f"could not enter configuration mode: {entered.strip()[:300]}"
                    ),
                    not_ready=entered is None,
                    total_commands=len(commands),
                )
            in_config_mode = True
        for command in commands:
            try:
                output = conn.send_command_timing(command)
            except ReadTimeout:
                # The console answered the login but then stopped answering (still booting, or
                # starved): nothing ran yet = safe to try the whole list again later.
                overall_ok = False
                if results:
                    results.append(
                        CommandResult(
                            command=command,
                            output="no answer from the console in time",
                            ok=False,
                        )
                    )
                else:
                    not_ready = True
                break
            failed = device_type == "cisco_ios_telnet" and _looks_like_ios_error(output)
            results.append(CommandResult(command=command, output=output, ok=not failed))
            if progress is not None:
                progress(len(results), len(commands))
            if failed:
                overall_ok = False
                if not continue_on_error:
                    break
    finally:
        if in_config_mode:
            try:
                conn.send_command_timing(
                    "end"
                )  # never leave the console in configuration mode
            except Exception as exc:
                logger.warning(
                    f"Config push: could not leave configuration mode: {exc}"
                )
        conn.disconnect()

    return _finish(
        node.name,
        "netmiko",
        overall_ok,
        results,
        len(commands),
        error="the console did not answer in time (the device is still booting)"
        if not_ready
        else None,
        not_ready=not_ready,
    )


def _guest_operations_ready(vm: Any) -> bool:
    return str(vm.guest.toolsRunningStatus) == "guestToolsRunning" and bool(
        vm.guest.guestOperationsReady
    )


def _resume_suspended(
    vm: Any,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """
    A VM that ESXi has suspended cannot run guest operations; powering it on resumes it where it
    stopped (nothing is lost). Waits until its VMware Tools answer again. Best effort - the push
    goes on either way and reports the real state.
    """
    from pyVmomi import vim

    if str(vm.runtime.powerState) != "suspended":
        return
    logger.info(f"Config push: VM '{vm.name}' is suspended, resuming it")
    try:
        task = vm.PowerOnVM_Task()
        while task.info.state not in (
            vim.TaskInfo.State.success,
            vim.TaskInfo.State.error,
        ):
            sleep(1)
        if task.info.state == vim.TaskInfo.State.error:
            logger.warning(
                f"Config push: could not resume '{vm.name}': {task.info.error}"
            )
            return
    except Exception as exc:
        logger.warning(f"Config push: could not resume '{vm.name}': {exc}")
        return
    deadline = clock() + _RESUME_WAIT_SECONDS
    while clock() < deadline and not _guest_operations_ready(vm):
        sleep(_WAKE_POLL_SECONDS)


def _wake_sleeping_windows(
    vm: Any,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """
    A Windows VM that sat idle goes to sleep: it stays powered on, but VMware Tools is gone and
    guest operations fail. A key press on the virtual keyboard wakes it (a few seconds). Done only
    for a powered-on VM whose Tools are not running; harmless for one that is merely booting.
    Best effort - the push goes on either way and reports the real state.
    """
    from pyVmomi import vim

    if (
        str(vm.runtime.powerState) != "poweredOn"
        or str(vm.guest.toolsRunningStatus) != "guestToolsNotRunning"
    ):
        return
    logger.info(
        f"Config push: VM '{vm.name}' has no VMware Tools, pressing a key to wake it"
    )
    try:
        vm.PutUsbScanCodes(
            vim.vm.UsbScanCodeSpec(
                keyEvents=[vim.vm.UsbScanCodeSpec.KeyEvent(usbHidCode=_WAKE_KEY_CODE)]
            )
        )
    except Exception as exc:
        logger.warning(f"Config push: could not press a key on '{vm.name}': {exc}")
        return
    deadline = clock() + _WAKE_WAIT_SECONDS
    while clock() < deadline and not _guest_operations_ready(vm):
        sleep(_WAKE_POLL_SECONDS)


def _guest_not_reachable_text(vm: Any, raw: str) -> str:
    """
    What a guest-operations failure on the very first command means, in words. The raw fault
    ('GuestOperationsUnavailable ...') says nothing about why - and for a VM that sits idle with
    no Tools the usual reason is one the push cannot fix (nothing boots), so say where to look.
    """
    power = str(vm.runtime.powerState)
    tools = str(vm.guest.toolsRunningStatus)
    if power != "poweredOn":
        what = f"the VM is {power}"
    elif tools == "guestToolsNotRunning":
        what = (
            "VMware Tools is not running in the guest. It may still be booting, or it is "
            "stuck before the operating system starts (for example no bootable disk, the "
            "firmware boot menu) - open the VM's screen to see which"
        )
    else:
        what = f"VMware Tools reports '{tools}'"
    return f"guest operations are not available: {what}.\n{raw[:300]}".strip()


def _run_guest_directive(
    vm: Any, directive: push_directives.Directive
) -> tuple[int, str]:
    """@reboot / @wait-reboot for a VM with VMware Tools: (0, text) once it is back, (1, reason) if not."""
    try:
        if directive.kind == "reboot":
            vm.RebootGuest()
        return 0, push_directives.wait_for_guest_reboot(vm, directive.timeout)
    except push_directives.RebootError as exc:
        return 1, str(exc)
    except Exception as exc:
        return 1, f"could not restart the guest ({type(exc).__name__}): {exc}"


def _push_via_guest_ops(
    node: GenericNode,
    commands: list[str],
    esxi_connection: Any,
    progress: Callable[[int, int], None] | None = None,
    continue_on_error: bool = False,
    native_shell: bool = False,
) -> DeviceResult:
    from pyVmomi import vim

    vm = esxi_connection.get_vm(node.name)
    if vm is None:
        return DeviceResult(
            device=node.name,
            transport="guest_ops",
            ok=False,
            error=f"VM '{node.name}' not found on ESXi",
            total_commands=len(commands),
        )

    creds = guest_credentials_for_image(node.image)
    if creds is None:
        return DeviceResult(
            device=node.name,
            transport="guest_ops",
            ok=False,
            error=f"no guest credentials found for image '{node.image}'",
            total_commands=len(commands),
        )
    username, password = creds
    auth = vim.vm.guest.NamePasswordAuthentication(username=username, password=password)
    guest_ops = esxi_connection.content.guestOperationsManager
    process_manager = guest_ops.processManager
    file_manager = guest_ops.fileManager
    is_windows = "windows" in node.image.lower()
    _resume_suspended(vm)
    if is_windows:
        _wake_sleeping_windows(vm)

    results: list[CommandResult] = []
    overall_ok = True
    not_ready = False
    for command in commands:
        directive = push_directives.parse(command)
        if directive is None:
            exit_code, output = run_command_in_guest(
                process_manager,
                vm,
                auth,
                windows_powershell_command(command)
                if native_shell and is_windows
                else command,
                is_windows,
                file_manager=file_manager,
                esxi_host=Settings.ESXI.IP,
            )
        else:
            exit_code, output = _run_guest_directive(vm, directive)
        ok = exit_code == 0
        results.append(CommandResult(command=command, output=output, ok=ok))
        if progress is not None:
            progress(len(results), len(commands))
        if not ok:
            overall_ok = False
            # exit_code None = the command could not even be started; for the
            # very first command that means "guest not ready", nothing was run.
            not_ready = directive is None and exit_code is None and len(results) == 1
            if not_ready:
                results[0].output = _guest_not_reachable_text(vm, output)
            # Only a command that ran and returned non-zero may be skipped over.
            if not (continue_on_error and directive is None and exit_code is not None):
                break

    return _finish(
        node.name,
        "guest_ops",
        overall_ok,
        results,
        len(commands),
        not_ready=not overall_ok and not_ready,
    )


def _push_via_serial(
    node: GenericNode,
    spec: DeviceSpec,
    esxi_connection: Any,
    progress: Callable[[int, int], None] | None = None,
) -> DeviceResult:
    """
    Logs in on the VM's serial console (see app/serial_agent.py) and runs the commands.
    A console that cannot be reached or does not show a prompt yet counts as "not ready"
    (nothing ran, safe to retry); wrong credentials and failed commands do not.
    """
    total = len(spec.commands)

    def failed(error: str, not_ready: bool = False) -> DeviceResult:
        return DeviceResult(
            device=node.name,
            transport="serial",
            ok=False,
            error=error,
            not_ready=not_ready,
            total_commands=total,
        )

    vm = esxi_connection.get_vm(node.name)
    if vm is None:
        return failed(f"VM '{node.name}' not found on ESXi")
    port = serial_port_of(vm)
    if port is None:
        return failed(
            f"VM '{node.name}' has no serial console - enable it first "
            f"(POST /nodes/{node.name}/serial/enable, or the node's menu in the GUI)"
        )
    if str(vm.runtime.powerState) != "poweredOn":
        return failed(f"VM '{node.name}' is not powered on", not_ready=True)

    profile = (
        serial_agent.PROFILES.get(spec.profile)
        if spec.profile
        else serial_agent.profile_for_image(node.image)
    )
    if profile is None:
        return failed(
            f"no serial profile for '{spec.profile or node.image}' "
            f"(available: {', '.join(serial_agent.PROFILES)})"
        )
    if spec.username is not None and spec.password is not None:
        username, password = spec.username, spec.password
    else:
        creds = guest_credentials_for_image(node.image)
        if creds is None:
            return failed(f"no login for image '{node.image}' in the template catalog")
        username, password = creds

    agent = serial_agent.SerialAgent(
        host=Settings.ESXI.IP,
        port=port,
        profile=profile,
        username=username,
        password=password,
    )
    outcomes: list[tuple[str, int | None, str]] = []
    try:
        with agent:
            agent.login()
            serial_agent.run_commands(
                agent,
                spec.commands,
                on_progress=progress,
                results=outcomes,
                continue_on_error=spec.continue_on_error,
            )
    except serial_agent.LoginFailed as exc:
        return failed(str(exc))
    except (serial_agent.AgentConnectionError, serial_agent.AgentTimeout) as exc:
        return failed(str(exc), not_ready=not outcomes)

    results = [
        CommandResult(
            command=command,
            output=output
            if code is not None
            else f"timed out waiting for the command to finish\n{output}".strip(),
            ok=code == 0,
        )
        for command, code, output in outcomes
    ]
    ok = len(results) == total and all(r.ok for r in results)
    return _finish(node.name, "serial", ok, results, total)


# How long a push waits for a device that is still booting. A router image on a small GNS3 VM can
# take a quarter of an hour (measured: ~11 minutes for IOSv with four of them booting at once).
DEFAULT_WAIT_SECONDS = 900


def _is_network_device(name: str, spec: DeviceSpec, app: Any) -> bool:
    """
    A router, switch or firewall: a GNS3 node, or an ESXi appliance that is configured over its serial console. The VMs
    behind them (guest operations) can only be configured and checked once these carry traffic, so they come second.
    """
    try:
        node = app.state.graph.nodes.get(name)
        if node is None:
            return False
        if node.env == Environment.ON_GNS3:
            return True
        if node.env == Environment.ON_ESXI:
            return spec.via == "serial" or (
                spec.via == "auto" and _is_serial_appliance(node)
            )
    except Exception:
        pass  # a push must not fail on its ordering
    return False


def _split_by_kind(devices: dict[str, Any], app: Any) -> tuple[dict, dict]:
    """(network devices, everything else), each in the given order."""
    specs = normalize(devices)
    network = {
        name: devices[name]
        for name in devices
        if _is_network_device(name, specs[name], app)
    }
    return network, {n: e for n, e in devices.items() if n not in network}


def _wait_and_push(
    pending: dict[str, Any],
    app: Any,
    deadline: float,
    interval_seconds: int,
    sleep: Any,
    on_progress: ProgressCallback | None,
    on_waiting: Callable[[list[str]], None] | None,
) -> dict[str, DeviceResult]:
    final: dict[str, DeviceResult] = {}
    while pending:
        for result in push_all(pending, app, on_progress):
            final[result.device] = result
        pending = {
            name: commands
            for name, commands in pending.items()
            if name in final and final[name].not_ready
        }
        if on_waiting is not None:
            on_waiting(sorted(pending))
        if not pending or time.monotonic() + interval_seconds > deadline:
            break
        logger.info(
            f"Config push: waiting for {sorted(pending)} to become reachable "
            f"(retrying in {interval_seconds}s)"
        )
        sleep(interval_seconds)
    return final


def push_all_waiting(
    devices: dict[str, Any],
    app: Any,
    wait_seconds: int = DEFAULT_WAIT_SECONDS,
    interval_seconds: int = 15,
    sleep: Any = time.sleep,
    on_progress: ProgressCallback | None = None,
    on_waiting: Callable[[list[str]], None] | None = None,
) -> list[DeviceResult]:
    """
    push_all that does not give up on devices that are still booting (after a deploy,
    a resume or a power-on): a device that could not be reached at all and ran no
    command (DeviceResult.not_ready) is tried again every `interval_seconds`, up
    to `wait_seconds`. A device where a command already ran or failed is never
    repeated - re-sending a half-applied config could do harm.
    Routers, switches and firewalls go first and are waited for; the VMs follow
    only then, because their commands (and the checks in them, e.g. a ping to the
    internet) need the network to carry traffic - measured: VMs configured while the
    switch was still booting failed their internet check and had to be repeated.
    `on_waiting(names)` is told which devices are being waited for.
    """
    deadline = time.monotonic() + wait_seconds
    network, guests = _split_by_kind(devices, app)
    final: dict[str, DeviceResult] = {}
    for group in (network, guests):
        if group:
            final.update(
                _wait_and_push(
                    group,
                    app,
                    deadline,
                    interval_seconds,
                    sleep,
                    on_progress,
                    on_waiting,
                )
            )
    return [final[name] for name in devices if name in final]


def _log_result(result: DeviceResult) -> None:
    """One line per device - never the commands or the device's output, which can hold secrets."""
    if result.ok:
        logger.info(
            f"Config push to {result.device} ({result.transport}): ok, "
            f"{result.executed} command(s)"
        )
        return
    where = (
        f", stopped after {result.executed} of {result.total_commands} command(s)"
        if result.executed
        else ""
    )
    logger.warning(
        f"Config push to {result.device} ({result.transport}): FAILED{where}"
        f"{f' - {result.error}' if result.error else ''}"
    )


def _esxi_transport(node: GenericNode, spec: DeviceSpec, esxi_connection: Any) -> str:
    """guest_ops or serial for an ESXi VM: as asked, else guest operations when VMware Tools is up, else the serial console if the VM has one."""
    if spec.via != "auto":
        return spec.via
    vm = esxi_connection.get_vm(node.name)
    if vm is not None:
        if str(vm.guest.toolsRunningStatus) == "guestToolsRunning":
            return "guest_ops"
        if serial_port_of(vm) is not None:
            return "serial"
        if _is_serial_appliance(node):
            return "serial"  # the console gets set up by _ensure_serial, there is no other way in
    return "guest_ops"


def _is_serial_appliance(node: GenericNode) -> bool:
    """MikroTik/pfSense/OPNsense: appliances that never run VMware Tools, so serial is their only way in."""
    profile = serial_agent.profile_for_image(node.image)
    return profile is not None and profile.name != "linux"


def _ensure_serial(node: GenericNode, app: Any) -> str | None:
    """
    Gives the VM a serial console if it has none (this briefly powers it off and on again,
    see app/esxi_serial.py). Returns an error text, None when the console exists afterwards.
    A Linux guest only gets here when serial was asked for explicitly or it is an appliance
    without VMware Tools - a VM that merely has not booted its Tools yet is never power-cycled.
    """
    vm = app.state.esxi_connection.get_vm(node.name)
    if vm is None:
        return f"VM '{node.name}' not found on ESXi"
    if serial_port_of(vm) is not None:
        return None
    logger.warning(
        f"Config push: VM '{node.name}' has no serial console yet, setting it up"
    )
    try:
        esxi_serial.enable(app, node.name)
    except esxi_serial.SerialSetupError as exc:
        return f"the serial console of '{node.name}' could not be set up automatically: {exc}"
    return None


_STARVED_CPU_PERCENT = 95


def _with_load_hint(error: str | None, app: Any) -> str | None:
    """
    A console that stays unreachable on a GNS3 VM whose CPU is saturated is not 'broken' - the
    images are starved while booting. Say so; the operator can then give the VM more vCPUs
    (a GNS3 VM deployed through this program gets 4).
    """
    load = compute_load(app.state.gns3_vm_ip, Settings.GNS3.PORT)
    cpu = load["cpu"] if load else None
    if cpu is None or cpu < _STARVED_CPU_PERCENT:
        return error
    return (
        f"{error or 'console not reachable'} - the GNS3 VM's CPU is at {cpu:.0f}%, so the "
        f"router/switch images are starved and boot very slowly (or never); give the GNS3 VM "
        f"more vCPUs"
    )


def push_all(
    devices: dict[str, Any],
    app: Any,
    on_progress: ProgressCallback | None = None,
) -> list[DeviceResult]:
    """
    Pushes every device's commands (a list, or a DeviceSpec choosing the way in) and
    returns one result per device. `on_progress(device, done, total)` is called after
    each command.
    """
    specs = normalize(devices)
    logger.info(
        f"Config push started for {len(specs)} device(s): {list(specs)} "
        f"({sum(len(spec.commands) for spec in specs.values())} command(s) in total)"
    )
    results: list[DeviceResult] = []

    live_by_name: dict[str, dict] = {}
    live_error: str | None = None
    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject as exc:
        live_error = str(exc)

    # Network devices before VMs (see push_all_waiting); the results go back in the order asked for
    ordered = sorted(
        specs.items(), key=lambda item: not _is_network_device(item[0], item[1], app)
    )
    for device_name, spec in ordered:
        try:
            commands = spec.commands

            def progress(done: int, total: int, name: str = device_name) -> None:
                if on_progress is not None:
                    on_progress(name, done, total)

            node = app.state.graph.nodes.get(device_name)
            if node is None:
                results.append(
                    DeviceResult(
                        device=device_name,
                        transport="n/a",
                        ok=False,
                        error="not found in topology",
                        total_commands=len(commands),
                    )
                )
                continue

            if node.env == Environment.ON_GNS3:
                live = live_by_name.get(device_name)
                if live is None:
                    results.append(
                        DeviceResult(
                            device=device_name,
                            transport="netmiko",
                            ok=False,
                            error=live_error or "no live console info for this node",
                            total_commands=len(commands),
                        )
                    )
                    continue
                console_port = live.get("console")
                if live.get("console_type") != "telnet" or console_port is None:
                    results.append(
                        DeviceResult(
                            device=device_name,
                            transport="netmiko",
                            ok=False,
                            error=f"no telnet console available (type={live.get('console_type')!r})",
                            total_commands=len(commands),
                        )
                    )
                    continue
                result = _push_via_netmiko(
                    node,
                    commands,
                    app.state.gns3_vm_ip,
                    console_port,
                    progress,
                    spec.continue_on_error,
                    spec.native_shell,
                )
                if result.not_ready:
                    result.error = _with_load_hint(result.error, app)
                results.append(result)
            elif node.env == Environment.ON_ESXI:
                if _esxi_transport(node, spec, app.state.esxi_connection) == "serial":
                    setup_error = _ensure_serial(node, app)
                    if setup_error is not None:
                        results.append(
                            DeviceResult(
                                device=device_name,
                                transport="serial",
                                ok=False,
                                error=setup_error,
                                total_commands=len(commands),
                            )
                        )
                    else:
                        results.append(
                            _push_via_serial(
                                node, spec, app.state.esxi_connection, progress
                            )
                        )
                else:
                    results.append(
                        _push_via_guest_ops(
                            node,
                            commands,
                            app.state.esxi_connection,
                            progress,
                            spec.continue_on_error,
                            spec.native_shell,
                        )
                    )
            else:
                results.append(
                    DeviceResult(
                        device=device_name,
                        transport="n/a",
                        ok=False,
                        error=f"node environment {node.env.name} has no config-push transport",
                        total_commands=len(commands),
                    )
                )
        except Exception as exc:
            # Whatever a transport raises that it did not foresee ends this device only.
            logger.opt(exception=exc).warning(
                f"Config push to {device_name} failed unexpectedly: {exc}"
            )
            results.append(
                DeviceResult(
                    device=device_name,
                    transport="n/a",
                    ok=False,
                    error=f"unexpected error ({type(exc).__name__}): {str(exc)[:300]}",
                    total_commands=len(spec.commands),
                )
            )

    position = {name: index for index, name in enumerate(specs)}
    results.sort(key=lambda result: position.get(result.device, len(position)))
    for result in results:
        _log_result(result)
    logger.info(
        f"Config push finished: {sum(r.ok for r in results)}/{len(results)} device(s) ok"
    )
    return results
