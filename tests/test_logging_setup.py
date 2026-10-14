"""
Tests app/logging_setup.py: what ends up in the log file, that the
standard library's loggers feed into it, that no credential is ever
written, and that the retention limit is the one promised.
"""

from __future__ import annotations

import logging

import pytest
from loguru import logger
from src.settings import Settings

from app import log_store, logging_setup


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "app.log"
    monkeypatch.setattr(Settings, "LOG_FILE_PATH", str(path))

    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved_named = {
        name: (list(lg.handlers), lg.propagate, lg.level)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "urllib3")
        for lg in [logging.getLogger(name)]
    }
    yield path
    logger.remove()
    if logging_setup._file_sink is not None:
        logging_setup._file_sink.stop()
        logging_setup._file_sink = None
    root.handlers[:], root.level = saved_root[0], saved_root[1]
    for name, (handlers, propagate, level) in saved_named.items():
        lg = logging.getLogger(name)
        lg.handlers[:], lg.propagate = handlers, propagate
        lg.setLevel(level)


def _text(path) -> str:
    return "\n".join(log_store.read_lines(path))


def test_debug_messages_reach_the_file(log_path):
    logging_setup.configure_logging()

    logger.debug("a very detailed thing")

    assert "a very detailed thing" in _text(log_path)


def test_every_line_keeps_the_format_the_parsers_expect(log_path):
    logging_setup.configure_logging()
    logger.info("hello")

    entries = log_store.parse_entries(log_store.read_lines(log_path))

    hello = next(e for e in entries if e["message"] == "hello")
    assert hello["level"] == "INFO"
    # file:function:line - the function name is the part that was added
    assert hello["location"].startswith("test_logging_setup.py:")
    assert hello["location"].count(":") == 2


def test_standard_library_loggers_feed_the_same_file(log_path):
    logging_setup.configure_logging()

    logging.getLogger("uvicorn.error").info("server says hi")
    logging.getLogger("some.other.library").warning("library warning")

    text = _text(log_path)
    assert "server says hi" in text
    assert "library warning" in text


def test_uvicorns_own_access_log_is_left_out_because_requests_are_logged_directly(
    log_path,
):
    logging_setup.configure_logging()

    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET / HTTP/1.1" 200')

    assert "GET / HTTP/1.1" not in _text(log_path)


def test_chatty_libraries_are_kept_to_warnings(log_path):
    logging_setup.configure_logging()

    logging.getLogger("urllib3.connectionpool").debug("Starting new HTTP connection")
    logging.getLogger("urllib3.connectionpool").warning("Retrying (Retry(total=2))")

    text = _text(log_path)
    assert "Starting new HTTP connection" not in text
    assert "Retrying" in text


def test_the_startup_lines_describe_the_setup_but_never_a_password(
    log_path, monkeypatch
):
    monkeypatch.setattr(Settings.ESXI, "PASSWORD", "hunter2-esxi", raising=False)
    monkeypatch.setattr(Settings.GNS3, "PASSWORD", "hunter2-gns3", raising=False)

    logging_setup.configure_logging()
    text = _text(log_path)

    assert "TopologyOperator starting" in text
    assert "at most 100,000 lines" in text
    assert "esxi_host" in text and "gns3_vm_name" in text
    assert "hunter2" not in text
    assert "password" not in text.lower()


def test_a_traceback_is_written_without_variable_values(log_path):
    logging_setup.configure_logging()
    secret_value = "s3cr3t-in-a-local-variable"

    try:
        _ = secret_value
        raise ValueError("boom")
    except ValueError:
        logger.exception("it failed")

    text = _text(log_path)
    assert "it failed" in text and "ValueError: boom" in text
    assert secret_value not in text


def test_the_retention_is_one_hundred_thousand_lines_over_two_files(log_path):
    logging_setup.configure_logging()

    sink = logging_setup._file_sink
    assert sink is not None
    assert sink._lines_per_file * 2 == log_store.MAX_TOTAL_LINES == 100_000


def test_configuring_twice_replaces_the_sink_instead_of_doubling_every_line(
    log_path,
):
    logging_setup.configure_logging()
    logging_setup.configure_logging()

    logger.info("only once please")

    assert _text(log_path).count("only once please") == 1


def test_a_standard_library_record_keeps_the_callers_real_location(log_path):
    logging_setup.configure_logging()

    logging.getLogger("some.library").warning("where am I")

    entry = next(
        e
        for e in log_store.parse_entries(log_store.read_lines(log_path))
        if e["message"] == "where am I"
    )
    # not the logging module's own frames (__init__.py:callHandlers:...)
    assert entry["location"].startswith("test_logging_setup.py:")
