"""The FastAPI instance. Route modules import it from here; app/main.py wires everything onto it."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Imported on first use: app.startup needs this module, so it cannot be imported here at the top.
    from app.startup import lifespan

    async with lifespan(app):
        yield


app = FastAPI(title="Ginnungagap", lifespan=_lifespan)
