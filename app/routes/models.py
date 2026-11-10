"""Request models shared by more than one route module."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app import (
    label_layout,
    node_notes,
)


class LayoutUpdate(BaseModel):
    name: str
    x: float
    y: float


class LayoutPosition(BaseModel):
    x: float
    y: float


class PositionsUpdate(BaseModel):
    positions: dict[str, LayoutPosition]


class LabelOffset(BaseModel):
    dx: float
    dy: float


class LabelsUpdate(BaseModel):
    """Label key ("<node>|<interface>", or "<node>|" for the node's name) -> offset; null puts a label back."""

    labels: dict[str, LabelOffset | None]


def label_changes(
    update: LabelsUpdate, known_nodes
) -> dict[str, dict[str, float] | None]:
    changes = {
        k: (None if v is None else {"dx": v.dx, "dy": v.dy})
        for k, v in update.labels.items()
    }
    for key in changes:
        node = label_layout.node_of(key)
        if not node:
            raise HTTPException(
                422, f"{key!r} is not a label key (expected '<node>|<interface>')"
            )
        if node not in known_nodes:
            raise HTTPException(404, f"no node named {node!r}")
    return changes


class NoteUpdate(BaseModel):
    text: str = Field(max_length=node_notes.MAX_LENGTH)


class AddNodeRequest(BaseModel):
    name: str
    role: str
    image: str


class AddLinkRequest(BaseModel):
    node1: str
    if1: str
    node2: str
    if2: str


class DrawingsRequest(BaseModel):
    drawings: list[dict[str, Any]]


class ConnectionSettingsUpdate(BaseModel):
    fields: dict[str, str]
    # Write-only: entered passwords (see app/profile_secrets.py). Blank or
    # absent keeps what is stored; no endpoint ever returns a value.
    secrets: dict[str, str] = {}
