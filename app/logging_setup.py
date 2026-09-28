"""
Configures loguru the same way TopologyBuilder's own main.py does (kept
byte-for-byte identical on purpose, including the colorized format
strings, "TopologyBuilder GUI style" per the request that added this) -
without it, this app only ever had loguru's out-of-the-box default
sink (stderr, with {name}:{function}:{line} locations rather than the
shorter {file}:{line}), and no persisted log file at all. GET /logs
(see app/logs.py) tails whatever this writes to Settings.LOG_FILE_PATH,
so the View menu's Logs panel and TopologyBuilder's own `logs` CLI
command end up reading the exact same kind of file.

Called once from main.py's own lifespan, after Settings.
initialise_settings so a settings.yml's own log_file_path override (if
any) is already in effect before the sink is created.
"""

from __future__ import annotations

import sys

from loguru import logger
from src.settings import Settings

_FILE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{file}:{line}</cyan> - "
    "<level>{message}</level>"
)
_PRINT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> - "
    "<level>{message}</level>"
)


def configure_logging() -> None:
    logger.remove()
    logger.add(
        Settings.LOG_FILE_PATH, format=_FILE_FORMAT, level="INFO", rotation="10MB"
    )
    logger.add(sys.stdout, format=_PRINT_FORMAT)
