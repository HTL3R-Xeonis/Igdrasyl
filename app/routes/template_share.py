"""Installation-wide template share actions available to administrators."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app import audit_log, template_share
from app.auth import require_admin

router = APIRouter(prefix="/admin/template-share")
Admin = Annotated[tuple[str, str], Depends(require_admin)]
IO_ERROR = "The template share could not be accessed. Check the mount, available space and write permissions."


class EditRequest(BaseModel):
    manifest: str = Field(min_length=1, max_length=1024)
    id: str = Field(min_length=1, max_length=120)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: template_share.Metadata


def _root() -> Path | None:
    try:
        return template_share.store_path()
    except template_share.StoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, IO_ERROR) from exc


def _required_root() -> Path:
    root = _root()
    if root is None:
        raise HTTPException(
            409, "Enable template management in the appliance setup first."
        )
    return root


@router.get("")
def list_share(_admin: Admin) -> dict:
    root = _root()
    try:
        return {
            "enabled": root is not None,
            "templates": template_share.templates(root) if root is not None else [],
            "refresh_minutes": template_share.refresh_minutes(),
        }
    except OSError as exc:
        raise HTTPException(503, IO_ERROR) from exc


@router.post("/import", status_code=201)
async def import_template(request: Request, admin: Admin) -> dict:
    root = await run_in_threadpool(_required_root)
    header = request.headers.get("x-template-metadata")
    if not header or len(header) > 12000:
        raise HTTPException(422, "Template metadata is missing or too large.")
    try:
        metadata = template_share.Metadata.model_validate_json(unquote(header))
    except ValidationError as exc:
        errors = exc.errors(
            include_input=False, include_url=False, include_context=False
        )
        raise HTTPException(422, errors) from exc
    maximum = template_share.MAX_UPLOAD_BYTES
    length = request.headers.get("content-length", "")
    if (
        length.isascii()
        and length.isdecimal()
        and (len(length) > 11 or int(length) > maximum)
    ):
        raise HTTPException(413, "The OVA exceeds the template share upload limit.")

    temp = None
    try:
        await run_in_threadpool(template_share.validate_new_name, root, metadata.name)
        fd, name = tempfile.mkstemp(dir=root, prefix=".upload-", suffix=".part")
        temp = Path(name)
        os.close(fd)
        digest = hashlib.sha256()
        size = 0
        async with await anyio.open_file(temp, "wb") as file:
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(
                        413, "The OVA exceeds the template share upload limit."
                    )
                digest.update(chunk)
                await file.write(chunk)
        template_id = digest.hexdigest().upper()
        await run_in_threadpool(
            template_share.publish, root, temp, template_id, size, metadata
        )
    except template_share.StoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except OSError as exc:
        disk_full = (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))
        status = 507 if exc.errno in disk_full else 503
        raise HTTPException(status, IO_ERROR) from exc
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass  # a mount that went away will be reported by the request

    audit_log.record("template_share_import", admin[0], metadata.name)
    return {
        "id": template_id,
        "name": metadata.name,
        "refresh_minutes": template_share.refresh_minutes(),
    }


@router.patch("")
def edit_template(body: EditRequest, admin: Admin) -> dict[str, str | int | None]:
    root = _required_root()
    try:
        template_share.edit(root, body.manifest, body.id, body.revision, body.metadata)
    except template_share.StoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, IO_ERROR) from exc
    audit_log.record("template_share_edit", admin[0], body.metadata.name)
    return {
        "name": body.metadata.name,
        "refresh_minutes": template_share.refresh_minutes(),
    }
