"""
Configures loguru for the whole process - and, through it, everything
that logs via the standard library (uvicorn, third-party code).

The log file is deliberately verbose: DEBUG and up, with the function
name in every location, every HTTP request and WebSocket session (see
app/request_logging.py), and the configuration at startup. Its size is
kept in check by line count instead - at most log_store.MAX_TOTAL_LINES
(100 000) lines are ever on disk (see app/log_store.py) - so being
verbose costs history, not disk. The console stays at INFO.

The line format stays "time | LEVEL | file:function:line - message", so
app/logs.py's parser and TopologyBuilder's own `logs` command keep
working on it; only the location token has grown by the function name.

Never put a secret in here: no passwords, no request bodies or headers.
Tracebacks are written without variable values (diagnose=False) for the
same reason.

Called once from main.py's own lifespan, after Settings.
initialise_settings so a settings.yml's own log_file_path override (if
any) is already in effect before the sink is created.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

from loguru import logger
from src.settings import Settings

from app import log_store

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{file}:{function}:{line} - "
    "{message}"
)
_PRINT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> - "
    "<level>{message}</level>"
)

# Chatty libraries: their warnings are worth keeping, their per-request
# DEBUG output is not (it would push real history out of the line budget).
_QUIET_LIBRARIES = ("urllib3", "paramiko", "asyncio", "multipart", "httpx", "httpcore")

_file_sink: log_store.LineLimitedFile | None = None


class _InterceptHandler(logging.Handler):
    """Routes standard-library log records into loguru, keeping the caller's real location."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _intercept_standard_logging() -> None:
    handler = _InterceptHandler()
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    for name in ("uvicorn", "uvicorn.error"):
        library_logger = logging.getLogger(name)
        library_logger.handlers = [handler]
        library_logger.propagate = False
    # app/request_logging.py logs every request itself, with timing.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    for name in _QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def _describe_configuration() -> dict[str, Any]:
    """The settings worth having in a log - never a credential."""
    topology = Settings.TOPOLOGY_FILE
    return {
        "esxi_host": Settings.ESXI.IP,
        "esxi_user": Settings.ESXI.USERNAME,
        "gns3_vm_name": Settings.ESXI.GNS3_VM_NAME,
        "gns3_port": Settings.GNS3.PORT,
        "gns3_user": Settings.GNS3.USERNAME,
        "gns3_project": Settings.GNS3.PROJECT_NAME,
        "topology_file": Path(topology).name if topology else None,
        "settings_file": Path(os.getenv("TOPOLOGYOPERATOR_SETTINGS", "")).name or None,
        "virtual_switch": Settings.ESXI.VIRTUAL_SWITCH,
        "trunk_port_group": Settings.ESXI.TRUNK_PORT_GROUP,
        "incremental_deploy": Settings.INCREMENTAL_DEPLOY,
        "esxi_template_api": Settings.API.ESXI_TEMPLATE_SERVER_URL,
        "gns3_template_api": Settings.API.GNS3_TEMPLATE_SERVER_URL,
        "topology_generator_api": Settings.API.TOPOLOGY_GENERATOR_URL,
        "literal_api_values": Settings.API.LITERAL_API_VALUES,
    }


def configure_logging() -> None:
    global _file_sink
    logger.remove()
    if _file_sink is not None:
        _file_sink.stop()
    _file_sink = log_store.LineLimitedFile(Settings.LOG_FILE_PATH)
    logger.add(
        _file_sink.write,
        format=_FILE_FORMAT,
        level="DEBUG",
        backtrace=True,
        diagnose=False,
        colorize=False,
    )
    logger.add(sys.stdout, format=_PRINT_FORMAT, level="INFO")
    _intercept_standard_logging()

    logger.info(
        f"TopologyOperator starting - pid {os.getpid()}, python "
        f"{platform.python_version()} on {platform.system()} {platform.release()}"
    )
    logger.info(
        f"Logging at DEBUG to {Path(Settings.LOG_FILE_PATH).name}, keeping at most "
        f"{log_store.MAX_TOTAL_LINES:,} lines "
        f"({log_store.LINES_PER_FILE:,} per file, current + one previous)"
    )
    logger.info(f"Configuration: {_describe_configuration()}")
