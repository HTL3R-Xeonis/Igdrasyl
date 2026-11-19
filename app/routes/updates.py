"""Admin-only update section: what is installed, "Check for updates", and a confirmed "Update now" (app/update_panel.py)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import update_panel
from app.auth import require_admin

router = APIRouter(prefix="/admin/updates")
Admin = Annotated[tuple[str, str], Depends(require_admin)]


class ApplyRequest(BaseModel):
    """The release the admin was shown and an explicit yes - nothing else can be asked for from the browser."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: str = Field(min_length=5, max_length=40)
    confirm: bool


def _refused(exc: update_panel.PanelError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("")
def get_updates(_admin: Admin) -> dict[str, Any]:
    view = update_panel.overview()
    update_panel.record_result(view)
    return view


@router.post("/check", status_code=202)
def check_for_updates(admin: Admin) -> dict[str, str]:
    """Asks the appliance to look for a new release. It only looks: nothing is installed."""
    try:
        return {"id": update_panel.request_check(admin[0])}
    except update_panel.PanelError as exc:
        raise _refused(exc) from exc


@router.post("/apply", status_code=202)
def apply_update(body: ApplyRequest, admin: Admin) -> dict[str, str]:
    """Asks the appliance to install the release the last check found - only with the admin's explicit confirmation."""
    try:
        return {"id": update_panel.request_apply(admin[0], body.version, body.confirm)}
    except update_panel.PanelError as exc:
        raise _refused(exc) from exc
