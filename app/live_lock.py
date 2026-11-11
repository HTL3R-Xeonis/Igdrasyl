"""
One lock for everything that changes the live lab's structure.

Adding or removing a node/link edits the topology file and then deploys the
WHOLE graph (TopologyBackend), a hold/resume renames and powers VMs, a project
import deploys a bundle. Two of them at once corrupt each other: three quick
"Add node" clicks produced three overlapping deploys, with a VM imported half
way while another deploy already listed it (found live). FastAPI runs these
plain-`def` endpoints in a thread pool, so they really do overlap - this makes
them queue instead. Reads and plain start/stop are not affected.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from loguru import logger

_LOCK = threading.Lock()
_OWNER = threading.local()


@contextmanager
def exclusive(what: str) -> Iterator[None]:
    """Serialize structural changes, allowing an outer batch to call individual writes."""
    if getattr(_OWNER, "exclusive", False):
        yield
        return
    if not _LOCK.acquire(blocking=False):
        logger.info(f"{what}: waiting for another change to the lab to finish")
        _LOCK.acquire()
    _OWNER.exclusive = True
    try:
        yield
    finally:
        _OWNER.exclusive = False
        _LOCK.release()


@contextmanager
def try_exclusive() -> Iterator[bool]:
    """Like exclusive(), but never waits: yields False (and holds nothing) when another change is running."""
    got = _LOCK.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            _LOCK.release()
