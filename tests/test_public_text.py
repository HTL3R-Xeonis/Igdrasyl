"""Tests app/public_text.py's log redaction: server paths go, request URLs stay."""

from __future__ import annotations

from app.public_text import redact_log_text, redact_paths


def test_a_request_url_with_several_segments_survives_redaction():
    line = "HTTP GET /maintenance/orphans -> 200 in 0.280s (client 127.0.0.1)"

    assert redact_log_text(line) == line


def test_the_same_url_is_what_plain_path_redaction_would_have_destroyed():
    # why redact_log_text exists
    assert "[server file]" in redact_paths("HTTP GET /maintenance/orphans -> 200")


def test_a_websocket_line_keeps_its_url():
    line = "WS /ws/console/R1 accepted (client 127.0.0.1)"

    assert redact_log_text(line) == line


def test_a_file_path_in_an_error_after_the_request_is_still_redacted():
    line = (
        "HTTP POST /project/import raised ValueError: cannot read /srv/private/x.yaml"
    )

    assert redact_log_text(line) == (
        "HTTP POST /project/import raised ValueError: cannot read [server file]"
    )


def test_a_full_formatted_line_keeps_the_url_and_redacts_the_rest():
    line = (
        "2026-10-14 13:14:52.579 | DEBUG    | request_logging.py:_http:94 - "
        "HTTP GET /design/layout -> 200 in 0.007s (client 127.0.0.1)"
    )

    assert redact_log_text(line) == line


def test_other_messages_are_redacted_as_before():
    result = redact_log_text("loaded /srv/private/topology.yaml ok")
    assert result.startswith("loaded [server file]")
    assert "/srv/private" not in result
    assert redact_log_text("nothing to hide here") == "nothing to hide here"


def test_a_traceback_after_a_request_line_is_redacted_line_by_line_content():
    text = (
        "HTTP GET /boom raised RuntimeError: x\n"
        'Traceback:\n  File "/srv/app/main.py", line 1'
    )

    result = redact_log_text(text)

    assert result.startswith("HTTP GET /boom raised")
    assert "/srv/app" not in result
