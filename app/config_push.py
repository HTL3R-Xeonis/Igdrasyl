"""
Config-push engine: sends a device-name-keyed list of raw CLI commands to
each device, transport auto-selected from its role/env (already known
from the live topology graph) - GNS3 console devices go over Telnet via
Netmiko, ESXi-hosted VMs go over pyvmomi's Guest Operations API. Per the
project's design (see HANDOFF.md): stop pushing a device's remaining
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
from app.gns3_state import NoLiveProject, list_live_nodes
from app.guest_ops import guest_credentials_for_image, run_command_in_guest

# (device, commands done, commands total) - lets a long push report its progress.
ProgressCallback = Callable[[str, int, int], None]

_NETMIKO_CONNECT_TIMEOUT_SECONDS = 20

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
    """

    commands: list[str]
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


def _push_via_netmiko(
    node: GenericNode,
    commands: list[str],
    gns3_vm_ip: str,
    console_port: int,
    progress: Callable[[int, int], None] | None = None,
) -> DeviceResult:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
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
    try:
        for command in commands:
            output = conn.send_command_timing(command)
            failed = device_type == "cisco_ios_telnet" and _looks_like_ios_error(output)
            results.append(CommandResult(command=command, output=output, ok=not failed))
            if progress is not None:
                progress(len(results), len(commands))
            if failed:
                overall_ok = False
                break
    finally:
        conn.disconnect()

    return _finish(node.name, "netmiko", overall_ok, results, len(commands))


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
                command,
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
                agent, spec.commands, on_progress=progress, results=outcomes
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


def push_all_waiting(
    devices: dict[str, Any],
    app: Any,
    wait_seconds: int = 300,
    interval_seconds: int = 15,
    sleep: Any = time.sleep,
    on_progress: ProgressCallback | None = None,
) -> list[DeviceResult]:
    """
    push_all for an automatic push right after a deploy or resume, when devices
    are still booting: a device that could not be reached at all and ran no
    command (DeviceResult.not_ready) is tried again every `interval_seconds`, up
    to `wait_seconds`. A device where a command already ran or failed is never
    repeated - re-sending a half-applied config could do harm.
    """
    deadline = time.monotonic() + wait_seconds
    pending = dict(devices)
    final: dict[str, DeviceResult] = {}
    while pending:
        for result in push_all(pending, app, on_progress):
            final[result.device] = result
        pending = {
            name: commands
            for name, commands in pending.items()
            if name in final and final[name].not_ready
        }
        if not pending or time.monotonic() + interval_seconds > deadline:
            break
        logger.info(
            f"Config push: waiting for {sorted(pending)} to become reachable "
            f"(retrying in {interval_seconds}s)"
        )
        sleep(interval_seconds)
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

    for device_name, spec in specs.items():
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
            results.append(
                _push_via_netmiko(
                    node, commands, app.state.gns3_vm_ip, console_port, progress
                )
            )
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
                        node, commands, app.state.esxi_connection, progress
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

    for result in results:
        _log_result(result)
    logger.info(
        f"Config push finished: {sum(r.ok for r in results)}/{len(results)} device(s) ok"
    )
    return results
