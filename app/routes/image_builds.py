"""Admin-only "Images" section: how the automated appliance image build is doing (app/image_build_status.py). Read-only."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app import image_build_status
from app.auth import require_admin

router = APIRouter(prefix="/admin/image-builds")
Admin = Annotated[tuple[str, str], Depends(require_admin)]


@router.get("")
async def get_image_builds(_admin: Admin) -> dict[str, Any]:
    # One short request to the build worker (cached for a few seconds): off the event loop, like every other blocking call here.
    return await run_in_threadpool(image_build_status.overview)
