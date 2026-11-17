"""
The app's ESXi session, opened when it is first needed instead of at process start.

`lifespan` used to open the pyvmomi session itself, so a host that was down (or not yet configured, as on a fresh
install) kept the whole app from starting - landing page and design mode included - and once started without it the app
never connected later. This wrapper looks like the real connection (every attribute is the real one's), connects on first
use, and after a failed attempt waits a few seconds before trying again, so a host that comes back is picked up by the
next request or by the startup retry without a restart, and a host that stays down does not make every request wait for
a connect timeout.

A host that does not answer at all (switched off, wrong address: packets are dropped, not refused) can take minutes to
time out. The connect therefore runs in a thread and a caller waits at most `CONNECT_WAIT_SECONDS` for it; the thread keeps
going, and its result is adopted by a later call if it succeeds in the end.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger


class EsxiUnavailable(RuntimeError):
    """The ESXi host can't be reached (or refused the login); the message is for the operator."""


def password_missing_message(service: str, host: str) -> str:
    """The one wording for "this host has no password stored" - the login is not even tried (never contains a password)."""
    return (
        f"{service} password for host {host} is not set - enter it in the host profile "
        f"(Host settings > Credentials). No connection attempted."
    )


class _Attempt:
    """One connect attempt, running in its own thread."""

    def __init__(self, connect: Callable[[], Any], started: float) -> None:
        self.started = started
        self.done = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None
        threading.Thread(
            target=self._run, args=(connect,), daemon=True, name="esxi-connect"
        ).start()

    def _run(self, connect: Callable[[], Any]) -> None:
        try:
            self.result = connect()
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


class LazyESXiConnection:
    RETRY_AFTER_SECONDS = 10.0
    CONNECT_WAIT_SECONDS = 15.0

    def __init__(
        self,
        connect: Callable[[], Any],
        host: str,
        clock: Callable[[], float] = time.monotonic,
        connect_wait_seconds: float | None = None,
        has_password: bool = True,
    ) -> None:
        self._connect = connect
        self._host = host
        self._has_password = has_password
        self._missing_password_logged = False
        self._clock = clock
        self._wait = (
            self.CONNECT_WAIT_SECONDS
            if connect_wait_seconds is None
            else connect_wait_seconds
        )
        self._lock = threading.Lock()
        self._connection: Any = None
        self._failed_at: float | None = None
        self._attempt: _Attempt | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    def connection(self) -> Any:
        """The real connection, opened now if it is not yet. :raises EsxiUnavailable: it can't be."""
        if not self._has_password:
            message = password_missing_message("ESXi", self._host)
            if not self._missing_password_logged:
                self._missing_password_logged = True
                logger.error(message)
            raise EsxiUnavailable(message)
        with self._lock:
            if self._connection is not None:
                return self._connection
            attempt = self._attempt
            if attempt is None:
                now = self._clock()
                if (
                    self._failed_at is not None
                    and now - self._failed_at < self.RETRY_AFTER_SECONDS
                ):
                    raise EsxiUnavailable(self._message())
                attempt = self._attempt = _Attempt(self._connect, now)
            remaining = max(0.0, self._wait - (self._clock() - attempt.started))
        attempt.done.wait(
            remaining
        )  # outside the lock: other callers must not queue behind a slow host
        with self._lock:
            if self._connection is not None:
                return self._connection
            if not attempt.done.is_set():
                raise EsxiUnavailable(self._message(still_trying=True))
            if (
                self._attempt is attempt
            ):  # the first caller to see the result records it
                self._attempt = None
                if attempt.error is not None:
                    self._failed_at = self._clock()
                    logger.warning(
                        f"Couldn't connect to ESXi host {self._host}: {attempt.error!r}"
                    )
                else:
                    self._failed_at = None
                    self._connection = attempt.result
                    logger.info(f"Connected to ESXi host {self._host}")
                    return self._connection
            raise EsxiUnavailable(self._message())

    def _message(self, still_trying: bool = False) -> str:
        if still_trying:
            return (
                f"The ESXi host {self._host} did not answer within {self._wait:g} seconds "
                f"(still trying in the background)."
            )
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
