"""
State of the configured GNS3 VM, for the warning banners' Start / Restart / Deploy button.

A GNS3 VM that is switched off (after an ESXi host restart nothing powers it on by itself) makes everything that needs
GNS3 fail with a message that only says "start it". status() tells the page whether that is the case (or that the VM is missing
altogether and could be deployed from its template), start() powers it on or resets it.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from loguru import logger
from src.settings import Settings

from app import gns3_vm_deploy_jobs

_OFF_STATES = ("poweredOff", "suspended")


def _vm(app: FastAPI):
    vm = app.state.esxi_connection.get_vm(Settings.ESXI.GNS3_VM_NAME)
    if vm is None:
        raise ValueError(f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' not found on ESXi")
    return vm


def _action(power_state: str, has_ip: bool) -> str | None:
    if power_state in _OFF_STATES:
        return "start"
    if power_state == "poweredOn" and not has_ip:
        return "restart"
    return None


def status(app: FastAPI) -> dict[str, Any] | None:
    """
    {"name", "power_state", "action"} for the configured GNS3 VM, where action is "start" (it is off), "restart" (it is on
    but reports no address), "deploy" (it does not exist on the host and a template for a new one is selected) or None
    (nothing to offer). None when ESXi is not connected (never waits for a connect: this is read by the polled /health) or
    the state can't be read.
    """
    connection = getattr(app.state, "esxi_connection", None)
    if not getattr(connection, "connected", False):
        return None
    try:
        vm = connection.get_vm(Settings.ESXI.GNS3_VM_NAME)
        if vm is None:
            return {
                "name": Settings.ESXI.GNS3_VM_NAME,
                "power_state": None,
                "action": "deploy" if gns3_vm_deploy_jobs.is_configured() else None,
            }
        power_state = str(vm.runtime.powerState)
    except Exception as exc:
        logger.debug(f"GNS3 VM power state not readable: {exc}")
        return None
    if power_state not in (*_OFF_STATES, "poweredOn"):
        return None
    return {
        "name": Settings.ESXI.GNS3_VM_NAME,
        "power_state": power_state,
        "action": _action(power_state, bool(getattr(app.state, "gns3_vm_ip", None))),
    }


def start(app: FastAPI) -> str:
    """
    Powers the GNS3 VM on (also resumes a suspended one) or, when it is on without an address, resets it.
    :returns: "started", "restarted" or "running" (it was on and has an address - nothing done).
    :raises ValueError: the VM does not exist on the ESXi host.
    :raises RuntimeError: the ESXi task failed.
    """
    esxi = app.state.esxi_connection
    vm = _vm(app)
    power_state = str(vm.runtime.powerState)
    if power_state in _OFF_STATES:
        logger.info(
            f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' ({power_state}): power on requested"
        )
        esxi.power_on_vm(vm)
        return "started"
    if app.state.gns3_vm_ip:
        return "running"
    logger.info(
        f"GNS3 VM '{Settings.ESXI.GNS3_VM_NAME}' is on without an address: reset requested"
    )
    esxi.reset_vm(vm)
    return "restarted"
