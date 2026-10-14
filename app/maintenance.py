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
from src.settings import Settings

from app.topology_write import get_or_create_backend


def run_verify(app: FastAPI, fix: bool) -> dict[str, Any]:
    """
    :param fix: apply the automatic repairs first, then check
    :return: {"actions": [str], "results": [{"ok": bool, "description": str}], "passed": int, "total": int}
    """
    backend = get_or_create_backend(app)
    actions, results = backend.verify(Settings.TOPOLOGY_FILE, fix=fix)
    return {
        "actions": actions,
        "results": [{"ok": ok, "description": desc} for ok, desc in results],
        "passed": sum(1 for ok, _ in results if ok),
        "total": len(results),
    }


def find_orphans(app: FastAPI) -> list[dict[str, str]]:
    return get_or_create_backend(app).find_orphans(Settings.TOPOLOGY_FILE)


def delete_orphans(app: FastAPI, orphans: list[dict[str, str]]) -> int:
    """
    Permanently deletes the given orphans (live state is re-resolved by
    TopologyBuilder itself, so an orphan that vanished in the meantime is
    simply skipped).
    :return: how many orphans were requested for deletion
    """
    get_or_create_backend(app).delete_orphans(Settings.TOPOLOGY_FILE, orphans)
    return len(orphans)
