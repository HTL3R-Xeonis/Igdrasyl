"""
Heals the startup warning by itself.

The app comes up in a degraded mode when something it needs is not there yet - typically the GNS3 VM
(still booting after the host came back) or the template-listing service. That used to stay so until
somebody pressed Retry or restarted the process. This retries in the background, quickly at first and
then every minute, until the warning is gone; it never gives up and never runs while a hold is parked on
purpose (that is no fault) or while another structural change to the lab is under way.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from loguru import logger

RETRY_INTERVALS_SECONDS = (15, 15, 30, 30, 60)


def run(
    should_retry: Callable[[], bool],
    refresh: Callable[[], bool],
    stop: threading.Event,
    intervals: tuple[int, ...] = RETRY_INTERVALS_SECONDS,
) -> None:
    """
    Loops until `stop` is set. `should_retry()` says whether there is anything to heal;
    `refresh()` makes one attempt and returns False when it could not run it (the lab
    was busy) - that attempt does not count and is simply repeated at the next tick.
    """
    attempt = 0
    while True:
        wait = intervals[min(attempt, len(intervals) - 1)]
        if stop.wait(wait):
            return
        if not should_retry():
            attempt = 0
            continue
        try:
            if refresh():
                attempt += 1
                if not should_retry():
                    logger.info("Startup warning healed by the background retry")
                    attempt = 0
        except Exception as exc:
            attempt += 1
            logger.warning(f"Background retry of the startup state failed: {exc}")


def start(
    should_retry: Callable[[], bool],
    refresh: Callable[[], bool],
    stop: threading.Event,
) -> threading.Thread:
    thread = threading.Thread(
        target=run,
        args=(should_retry, refresh, stop),
        name="startup-retry",
        daemon=True,
    )
    thread.start()
    return thread
