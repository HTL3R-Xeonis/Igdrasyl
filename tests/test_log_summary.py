from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests
from src.settings import Settings

from app import log_summary


@pytest.fixture
def generator(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"summary": "short summary"}
    post = Mock(return_value=response)
    monkeypatch.setattr(log_summary.requests, "post", post)
    return post


def test_recent_log_text_redacts_paths(monkeypatch):
    monkeypatch.setattr(
        log_summary.log_store,
        "read_lines",
        lambda path, lines: ["ok line", "/home/user/secret/app.py:12 boom"],
    )
    text = log_summary.recent_log_text()
    assert "ok line" in text
    assert "/home/user" not in text
    assert "[server file]" in text


def test_summarize_sends_the_text_and_returns_the_summary(generator):
    result = log_summary.summarize("log text")
    assert result == "short summary"
    args, kwargs = generator.call_args
    assert args == (
        Settings.API.TOPOLOGY_GENERATOR_URL.rstrip("/") + "/api/summarize-text",
    )
    assert kwargs["timeout"] == Settings.API.TOPOLOGY_GENERATOR_TIMEOUT_SECONDS
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"]["text"] == "log text"
    assert "errors and warnings" in kwargs["json"]["goal"]


def test_summarize_raises_runtime_error_on_404(generator):
    generator.return_value.status_code = 404
    with pytest.raises(RuntimeError, match="does not support log summaries"):
        log_summary.summarize("log text")


def test_summarize_raises_timeout_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(log_summary.requests, "post", _raise)
    with pytest.raises(TimeoutError):
        log_summary.summarize("log text")


def test_summarize_raises_runtime_error_on_request_failure(monkeypatch):
    def _raise(*args, **kwargs):
        raise requests.ConnectionError()

    monkeypatch.setattr(log_summary.requests, "post", _raise)
    with pytest.raises(RuntimeError, match="failed"):
        log_summary.summarize("log text")


def test_summarize_raises_runtime_error_on_a_missing_summary_field(generator):
    generator.return_value.json.return_value = {}
    with pytest.raises(RuntimeError, match="invalid response"):
        log_summary.summarize("log text")
