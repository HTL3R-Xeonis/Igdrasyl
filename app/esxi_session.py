"""
The app's ESXi session, opened when it is first needed instead of at process start.

`lifespan` used to open the pyvmomi session itself, so a host that was down (or not yet configured, as on a fresh
install) kept the whole app from starting - landing page and design mode included - and once started without it the app
never connected later. This wrapper looks like the real connection (every attribute is the real one's), connects on first
use, and after a failed attempt waits a few seconds before trying again, so a host that comes back is picked up by the
next request or by the startup retry without a restart, and a host that stays down does not make every request wait for
a connect timeout.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger


class EsxiUnavailable(RuntimeError):
    """The ESXi host can't be reached (or refused the login); the message is for the operator."""


class LazyESXiConnection:
    RETRY_AFTER_SECONDS = 10.0

    def __init__(
        self,
        connect: Callable[[], Any],
        host: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connect
        self._host = host
        self._clock = clock
        self._lock = threading.Lock()
        self._connection: Any = None
        self._failed_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def connection(self) -> Any:
        """The real connection, opened now if it is not yet. :raises EsxiUnavailable: it can't be."""
        with self._lock:
            if self._connection is not None:
                return self._connection
            now = self._clock()
            if (
                self._failed_at is not None
                and now - self._failed_at < self.RETRY_AFTER_SECONDS
            ):
                raise EsxiUnavailable(self._message())
            try:
                self._connection = self._connect()
            except Exception as exc:
                self._failed_at = now
                logger.warning(f"Couldn't connect to ESXi host {self._host}: {exc!r}")
                raise EsxiUnavailable(self._message()) from exc
            self._failed_at = None
            logger.info(f"Connected to ESXi host {self._host}")
            return self._connection

    def _message(self) -> str:
        return (
            f"The ESXi host {self._host} can't be reached or refused the login "
            f"(see the log); it is tried again shortly."
        )

    def __getattr__(self, name: str) -> Any:
        # Only called for what this object does not have itself. Dunder lookups (copy, pickle, hasattr probes) must not
        # open a session.
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.connection(), name)
