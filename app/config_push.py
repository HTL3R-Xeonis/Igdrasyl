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
from typing import Any

from loguru import logger
from pydantic import BaseModel
from src.graph.blocks.generic_node import GenericNode
from src.graph.environment import Environment
from src.settings import Settings

from app.gns3_state import NoLiveProject, list_live_nodes
from app.guest_ops import guest_credentials_for_image, run_command_in_guest

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


class DeviceResult(BaseModel):
    device: str
    transport: str
    ok: bool
    commands: list[CommandResult] = []
    error: str | None = None
    # True when the device could not be reached at all and NO command ran (still
    # booting, VMware Tools not up yet): trying the whole list again is safe.
    # False once a command was executed - repeating a half-applied config is not.
    not_ready: bool = False


def _netmiko_device_type(image: str) -> str:
    if "cisco ios" in image.lower():
        return "cisco_ios_telnet"
    return "generic_telnet"


def _looks_like_ios_error(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _IOS_ERROR_MARKERS)


def _push_via_netmiko(
    node: GenericNode, commands: list[str], gns3_vm_ip: str, console_port: int
) -> DeviceResult:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
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
        )

    results: list[CommandResult] = []
    overall_ok = True
    try:
        for command in commands:
            output = conn.send_command_timing(command)
            failed = device_type == "cisco_ios_telnet" and _looks_like_ios_error(output)
            results.append(CommandResult(command=command, output=output, ok=not failed))
            if failed:
                overall_ok = False
                break
    finally:
        conn.disconnect()

    return DeviceResult(
        device=node.name, transport="netmiko", ok=overall_ok, commands=results
    )


def _push_via_guest_ops(
    node: GenericNode, commands: list[str], esxi_connection: Any
) -> DeviceResult:
    from pyVmomi import vim

    vm = esxi_connection.get_vm(node.name)
    if vm is None:
        return DeviceResult(
            device=node.name,
            transport="guest_ops",
            ok=False,
            error=f"VM '{node.name}' not found on ESXi",
        )

    creds = guest_credentials_for_image(node.image)
    if creds is None:
        return DeviceResult(
            device=node.name,
            transport="guest_ops",
            ok=False,
            error=f"no guest credentials found for image '{node.image}'",
        )
    username, password = creds
    auth = vim.vm.guest.NamePasswordAuthentication(username=username, password=password)
    guest_ops = esxi_connection.content.guestOperationsManager
    process_manager = guest_ops.processManager
    file_manager = guest_ops.fileManager
    is_windows = "windows" in node.image.lower()

    results: list[CommandResult] = []
    overall_ok = True
    for command in commands:
        exit_code, output = run_command_in_guest(
            process_manager,
            vm,
            auth,
            command,
            is_windows,
            file_manager=file_manager,
            esxi_host=Settings.ESXI.IP,
        )
        ok = exit_code == 0
        results.append(CommandResult(command=command, output=output, ok=ok))
        if not ok:
            overall_ok = False
            # exit_code None = the command could not even be started; for the
            # very first command that means "guest not ready", nothing was run.
            not_ready = exit_code is None and len(results) == 1
            break
    else:
        not_ready = False

    return DeviceResult(
        device=node.name,
        transport="guest_ops",
        ok=overall_ok,
        commands=results,
        not_ready=not overall_ok and not_ready,
    )


def push_all_waiting(
    devices: dict[str, list[str]],
    app: Any,
    wait_seconds: int = 300,
    interval_seconds: int = 15,
    sleep: Any = time.sleep,
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
        for result in push_all(pending, app):
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


def _log_result(result: DeviceResult, command_count: int) -> None:
    """One line per device - never the commands or the device's output, which can hold secrets."""
    if result.ok:
        logger.info(
            f"Config push to {result.device} ({result.transport}): ok, "
            f"{command_count} command(s)"
        )
        return
    failed = [n for n, c in enumerate(result.commands, 1) if not c.ok]
    where = f", command {failed[0]} of {command_count} failed" if failed else ""
    logger.warning(
        f"Config push to {result.device} ({result.transport}): FAILED{where}"
        f"{f' - {result.error}' if result.error else ''}"
    )


def push_all(devices: dict[str, list[str]], app: Any) -> list[DeviceResult]:
    logger.info(
        f"Config push started for {len(devices)} device(s): {list(devices)} "
        f"({sum(len(c) for c in devices.values())} command(s) in total)"
    )
    results: list[DeviceResult] = []

    live_by_name: dict[str, dict] = {}
    live_error: str | None = None
    try:
        live_by_name = {n["name"]: n for n in list_live_nodes(app)}
    except NoLiveProject as exc:
        live_error = str(exc)

    for device_name, commands in devices.items():
        node = app.state.graph.nodes.get(device_name)
        if node is None:
            results.append(
                DeviceResult(
                    device=device_name,
                    transport="n/a",
                    ok=False,
                    error="not found in topology",
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
                    )
                )
                continue
            results.append(
                _push_via_netmiko(node, commands, app.state.gns3_vm_ip, console_port)
            )
        elif node.env == Environment.ON_ESXI:
            results.append(
                _push_via_guest_ops(node, commands, app.state.esxi_connection)
            )
        else:
            results.append(
                DeviceResult(
                    device=device_name,
                    transport="n/a",
                    ok=False,
                    error=f"node environment {node.env.name} has no config-push transport",
                )
            )

    for result in results:
        _log_result(result, len(devices.get(result.device, [])))
    logger.info(
        f"Config push finished: {sum(r.ok for r in results)}/{len(results)} device(s) ok"
    )
    return results
