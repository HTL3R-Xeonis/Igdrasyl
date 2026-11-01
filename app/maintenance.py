"""
Health check / orphan cleanup against the live lab, via TopologyBuilder's
own TopologyBackend (verify/find_orphans/delete_orphans) - the same
checks its CLI's `verify [--fix]` and `orphans [--fix]` run. Kept in its
own module, independently testable, same separation main.py already uses
for topology_write.py/config_push.py. See TopologyBuilder's
VMOrchestrator.find_orphans/repair_graph docstrings for exactly what is
detected and which repairs are applied (deliberately a low-risk subset:
never a redeploy of a genuinely missing VM/node).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from loguru import logger
from src.settings import Settings

from app.topology_write import get_or_create_backend


def run_verify(
    app: FastAPI, fix: bool, fix_vlan_subinterfaces: bool = False
) -> dict[str, Any]:
    """
    :param fix: apply the automatic repairs first, then check
    :param fix_vlan_subinterfaces: also re-create the GNS3 VM's missing VLAN subinterfaces. Not part of
        `fix`: it re-creates and restarts the Cloud nodes, which once coincided with the ESXi host
        going offline, so it only ever runs on its own explicit request.
    :return: {"actions": [str], "results": [{"ok": bool, "description": str}], "passed": int, "total": int}
    """
    logger.info(
        f"Health check started (automatic fixes: {'on' if fix else 'off'}, "
        f"VLAN subinterface repair: {'on' if fix_vlan_subinterfaces else 'off'})"
    )
    backend = get_or_create_backend(app)
    extra = {"fix_vlan_subinterfaces": True} if fix_vlan_subinterfaces else {}
    actions, results = backend.verify(Settings.TOPOLOGY_FILE, fix=fix, **extra)
    for action in actions:
        logger.warning(f"Health check repaired: {action}")
    failed = [description for ok, description in results if not ok]
    for description in failed:
        logger.warning(f"Health check FAILED: {description}")
    logger.info(
        f"Health check finished: {len(results) - len(failed)}/{len(results)} checks "
        f"passed, {len(actions)} repair(s) applied"
    )
    return {
        "actions": actions,
        "results": [{"ok": ok, "description": desc} for ok, desc in results],
        "passed": sum(1 for ok, _ in results if ok),
        "total": len(results),
    }


def find_orphans(app: FastAPI) -> list[dict[str, str]]:
    orphans = get_or_create_backend(app).find_orphans(Settings.TOPOLOGY_FILE)
    if orphans:
        logger.warning(
            f"Orphan scan found {len(orphans)}: "
            f"{[(o['type'], o['name']) for o in orphans]}"
        )
    else:
        logger.info("Orphan scan found nothing")
    return orphans


def delete_orphans(app: FastAPI, orphans: list[dict[str, str]]) -> int:
    """
    Permanently deletes the given orphans (live state is re-resolved by
    TopologyBuilder itself, so an orphan that vanished in the meantime is
    simply skipped).
    :return: how many orphans were requested for deletion
    """
    logger.warning(
        f"Deleting {len(orphans)} orphan(s) permanently: "
        f"{[(o['type'], o['name']) for o in orphans]}"
    )
    get_or_create_backend(app).delete_orphans(Settings.TOPOLOGY_FILE, orphans)
    logger.info("Orphan deletion finished")
    return len(orphans)
