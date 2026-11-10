"""Health, reload, logs and template-service status."""

from __future__ import annotations

from typing import Any

from fastapi.responses import StreamingResponse
from src.settings import Settings

from app import (
    gns3_vm_power,
    log_store,
    logging_setup,
    logs,
    public_text,
    template_health,
)
from app.application import app
from app.startup import (
    deployable_project,
    gns3_vm_power_state,
    held_project,
    refresh_startup_state,
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "deployable_project": deployable_project(),
        "held_project": held_project(),
        "esxi_ip": Settings.ESXI.IP,
        "gns3_vm_ip": app.state.gns3_vm_ip,
        "gns3_vm": gns3_vm_power_state(),
        "gns3_port": Settings.GNS3.PORT,
        "gns3_project_name": app.state.project_name,
        "gns3_project_id": app.state.project_id,
        "warning": public_text.redact_paths(app.state.startup_warning),
        "incremental_deploy": Settings.INCREMENTAL_DEPLOY,
    }


@app.post("/reload")
def reload_startup_state() -> dict[str, Any]:
    """
    Manual retry for whatever refresh_startup_state couldn't resolve at
    startup (template service was down, GNS3 VM/project not up yet) -
    the frontend's warning banner offers this instead of requiring a full
    process restart once the dependency is back. Always 200; the result
    is the same shape as GET /health so the frontend can refresh from it
    directly.
    """
    refresh_startup_state(app)
    return health()


@app.get("/logs")
def get_logs(lines: int = 300) -> list[dict[str, Any]]:
    """
    The most recent `lines` entries from Settings.LOG_FILE_PATH,
    parsed for the View menu's Logs panel (see app/logs.py) - same log
    file and format TopologyBuilder's own `logs` CLI command reads,
    just with a more generous default line count for a scrollable GUI
    panel instead of a terminal.
    """
    return logs.tail_log_lines(lines)


@app.get("/logs/entries")
def get_log_entries(
    lines: int = 500, level: str | None = None, search: str | None = None
) -> dict[str, Any]:
    """
    The most recent `lines` log entries (a traceback stays one entry),
    optionally only at/above `level` and containing `search`, plus how
    full the log is - for the standalone log page (/logs.html). Server
    file paths are redacted, same as everything else sent to a browser.
    """
    lines = max(1, min(lines, log_store.MAX_TOTAL_LINES))
    return {
        "entries": [
            {**entry, "message": public_text.redact_log_text(entry["message"])}
            for entry in log_store.read_entries(
                Settings.LOG_FILE_PATH, lines, level, search
            )
        ],
        "info": log_store.info(Settings.LOG_FILE_PATH),
    }


@app.delete("/logs")
def clear_logs() -> dict[str, str]:
    """Permanently deletes every retained log line (both rotation files)."""
    logging_setup.clear_logs()
    return {"status": "cleared"}


@app.get("/logs/download")
def download_logs() -> StreamingResponse:
    """The whole retained log (at most 100 000 lines) as plain text, paths redacted."""
    return StreamingResponse(
        (
            public_text.redact_log_text(line) + "\n"
            for line in log_store.iter_lines(Settings.LOG_FILE_PATH)
        ),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="topologyoperator.log"'},
    )


@app.get("/templates/health")
def templates_health() -> dict[str, Any]:
    """
    Whether the template service (which TopologyBuilder asks what images
    exist) works - see app/template_health.py. Cheap and cached for a few
    seconds, the UI polls it.
    """
    result = template_health.check()
    if not result["ok"]:
        # A switched-off GNS3 VM is the usual reason the GNS3 template list is empty - the banner offers to start it.
        result = {**result, "gns3_vm": gns3_vm_power.status(app)}
    return public_text.redact_paths(result)


@app.post("/templates/refresh")
def templates_refresh() -> dict[str, Any]:
    """
    Makes the template services read their sources again right now - the banner's "Check again" button and, once a started
    GNS3 VM has an address, the page call this so the templates are listed again without waiting for the services' next
    timer run. "requested" is true when at least one service took the request.
    """
    result = template_health.request_refresh()
    return {"requested": any(value is True for value in result.values()), **result}
