from src.settings import Settings

from app import logs


def test_tail_log_lines_returns_empty_list_when_file_missing(tmp_path):
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(tmp_path / "nope.log")
    try:
        assert logs.tail_log_lines(10) == []
    finally:
        Settings.LOG_FILE_PATH = original


def test_tail_log_lines_parses_well_formed_lines(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "2026-09-24 21:34:04.917 | INFO     | main.py:42 - Application startup complete\n"
        "2026-09-24 21:34:05.100 | ERROR    | api_handler.py:50 - GET request timed out\n"
    )
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(log_file)
    try:
        result = logs.tail_log_lines(10)
    finally:
        Settings.LOG_FILE_PATH = original

    assert result == [
        {
            "time": "2026-09-24 21:34:04.917",
            "level": "INFO",
            "location": "main.py:42",
            "message": "Application startup complete",
        },
        {
            "time": "2026-09-24 21:34:05.100",
            "level": "ERROR",
            "location": "api_handler.py:50",
            "message": "GET request timed out",
        },
    ]


def test_tail_log_lines_respects_the_requested_count(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "".join(
            f"2026-09-24 21:34:0{i}.000 | INFO     | main.py:{i} - line {i}\n"
            for i in range(5)
        )
    )
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(log_file)
    try:
        result = logs.tail_log_lines(2)
    finally:
        Settings.LOG_FILE_PATH = original

    assert [r["message"] for r in result] == ["line 3", "line 4"]


def test_tail_log_lines_keeps_unparseable_lines_as_raw_message(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "2026-09-24 21:34:04.917 | ERROR    | main.py:1 - something failed\n"
        "Traceback (most recent call last):\n"
        '  File "main.py", line 1, in <module>\n'
    )
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(log_file)
    try:
        result = logs.tail_log_lines(10)
    finally:
        Settings.LOG_FILE_PATH = original

    assert result[1] == {
        "time": "",
        "level": "",
        "location": "",
        "message": "Traceback (most recent call last):",
    }
    assert result[2]["message"] == '  File "main.py", line 1, in <module>'


def test_tail_log_lines_redacts_server_paths(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "2026-09-24 21:34:04.917 | ERROR    | main.py:1 - failed at /srv/private/topology.yaml\n"
    )
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(log_file)
    try:
        result = logs.tail_log_lines(10)
    finally:
        Settings.LOG_FILE_PATH = original

    assert result[0]["message"] == "failed at [server file]"


def test_get_logs_endpoint_returns_parsed_lines(client, app_state, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("2026-09-24 21:34:04.917 | INFO     | main.py:1 - hello\n")
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(log_file)
    try:
        resp = client.get("/logs")
    finally:
        Settings.LOG_FILE_PATH = original

    assert resp.status_code == 200
    assert resp.json() == [
        {
            "time": "2026-09-24 21:34:04.917",
            "level": "INFO",
            "location": "main.py:1",
            "message": "hello",
        }
    ]


def test_get_logs_endpoint_respects_lines_query_param(client, app_state, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "".join(
            f"2026-09-24 21:34:0{i}.000 | INFO     | main.py:{i} - line {i}\n"
            for i in range(5)
        )
    )
    original = Settings.LOG_FILE_PATH
    Settings.LOG_FILE_PATH = str(log_file)
    try:
        resp = client.get("/logs?lines=2")
    finally:
        Settings.LOG_FILE_PATH = original

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[-1]["message"] == "line 4"
