"""
Tests app/log_store.py - above all the promise that gives it its reason to
exist: no more than MAX_TOTAL_LINES lines are ever on disk, however and
in whatever message sizes the log is written.
"""

from __future__ import annotations

import random

from app import log_store
from app.log_store import LineLimitedFile


def _total_lines(path) -> int:
    return len(log_store.read_lines(path))


def _write_numbered(sink, count, start=1, prefix="line"):
    for number in range(start, start + count):
        sink.write(f"{prefix} {number}\n")


def test_the_limit_is_100000_lines_split_over_two_files():
    assert log_store.MAX_TOTAL_LINES == 100_000
    assert 2 * log_store.LINES_PER_FILE == log_store.MAX_TOTAL_LINES


def test_lines_are_written_and_read_back_in_order(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=10)
    _write_numbered(sink, 4)

    assert log_store.read_lines(path) == ["line 1", "line 2", "line 3", "line 4"]
    assert sink.line_count == 4


def test_a_full_file_rotates_into_the_backup_and_nothing_recent_is_lost(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=5)
    _write_numbered(sink, 12)

    assert (tmp_path / "app.log.1").exists()
    assert log_store.read_lines(path)[-1] == "line 12"
    # Reading spans both files, so the lines from before the rotation survive.
    # Lines 1-5 fell away with the first rotation, 6-10 sit in the backup.
    assert log_store.read_lines(path) == [f"line {n}" for n in range(6, 13)]


def test_the_oldest_lines_fall_away_instead_of_the_total_growing(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=5)
    _write_numbered(sink, 100)

    lines = log_store.read_lines(path)
    assert len(lines) <= 10
    assert "line 1" not in lines
    assert lines[-1] == "line 100"


def test_the_total_never_exceeds_the_limit_whatever_is_written(tmp_path):
    path = tmp_path / "app.log"
    limit_per_file = 50
    sink = LineLimitedFile(path, lines_per_file=limit_per_file)
    rng = random.Random(7)

    for step in range(2000):
        # Single lines and multi-line messages (a traceback) mixed together.
        size = rng.choice([1, 1, 1, 2, 5, 12])
        sink.write("".join(f"step {step} part {i}\n" for i in range(size)))
        assert _total_lines(path) <= 2 * limit_per_file, step


def test_a_multi_line_message_counts_every_physical_line(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=5)
    sink.write("a\nb\nc\n")
    sink.write("d\ne\nf\n")  # would make 6 > 5 -> rotates first

    assert (tmp_path / "app.log.1").read_text() == "a\nb\nc\n"
    assert path.read_text() == "d\ne\nf\n"


def test_an_oversized_file_from_an_older_version_is_trimmed_on_start(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("".join(f"old {n}\n" for n in range(1, 31)))
    (tmp_path / "app.log.1").write_text("".join(f"older {n}\n" for n in range(1, 21)))

    sink = LineLimitedFile(path, lines_per_file=10)
    sink.write("new\n")

    assert _total_lines(path) <= 20
    assert log_store.read_lines(path)[-1] == "new"
    assert "old 30" in log_store.read_lines(path)
    assert "old 1" not in log_store.read_lines(path)


def test_an_existing_file_is_continued_and_counted(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("one\ntwo\nthree\n")

    sink = LineLimitedFile(path, lines_per_file=10)

    assert sink.line_count == 3
    sink.write("four\n")
    assert log_store.read_lines(path) == ["one", "two", "three", "four"]


def test_a_missing_log_reads_as_empty(tmp_path):
    assert log_store.read_lines(tmp_path / "nope.log") == []
    assert log_store.info(tmp_path / "nope.log")["lines"] == 0


def test_a_traceback_stays_part_of_its_entry():
    entries = log_store.parse_entries(
        [
            "2026-10-14 09:00:00.000 | INFO     | a.py:f:1 - first",
            "2026-10-14 09:00:01.000 | ERROR    | a.py:g:2 - boom",
            "Traceback (most recent call last):",
            '  File "x.py", line 1',
            "ValueError: bad",
            "2026-10-14 09:00:02.000 | INFO     | a.py:f:3 - after",
        ]
    )

    assert [e["level"] for e in entries] == ["INFO", "ERROR", "INFO"]
    assert entries[1]["message"].startswith("boom\nTraceback")
    assert entries[1]["message"].endswith("ValueError: bad")
    assert entries[1]["location"] == "a.py:g:2"


def _sample_log(path):
    path.write_text(
        "2026-10-14 09:00:00.000 | DEBUG    | a.py:f:1 - polling nodes\n"
        "2026-10-14 09:00:01.000 | INFO     | a.py:f:2 - deployed lab\n"
        "2026-10-14 09:00:02.000 | WARNING  | a.py:f:3 - template service slow\n"
        "2026-10-14 09:00:03.000 | ERROR    | a.py:f:4 - ESXi login failed\n"
        "Traceback (most recent call last):\n"
        "ValueError: x\n"
    )


def test_a_level_filter_keeps_that_level_and_above(tmp_path):
    path = tmp_path / "app.log"
    _sample_log(path)

    entries = log_store.read_entries(path, 100, min_level="warning")

    assert [e["level"] for e in entries] == ["WARNING", "ERROR"]
    assert "Traceback" in entries[1]["message"]


def test_a_search_matches_case_insensitively_in_any_field(tmp_path):
    path = tmp_path / "app.log"
    _sample_log(path)

    assert [e["level"] for e in log_store.read_entries(path, 100, search="ESXI")] == [
        "ERROR"
    ]
    assert len(log_store.read_entries(path, 100, search="a.py:f:2")) == 1


def test_only_the_most_recent_entries_are_returned(tmp_path):
    path = tmp_path / "app.log"
    _sample_log(path)

    entries = log_store.read_entries(path, 2)

    assert [e["level"] for e in entries] == ["WARNING", "ERROR"]


def test_info_reports_both_files_against_the_limit(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=5)
    _write_numbered(sink, 8)

    result = log_store.info(path)

    assert result["max_lines"] == 100_000
    assert result["lines"] == 8
    assert [f["name"] for f in result["files"]] == ["app.log.1", "app.log"]


def test_a_download_is_the_backup_followed_by_the_current_file(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=3)
    _write_numbered(sink, 5)

    assert (
        "".join(log_store.iter_text(path)) == "line 1\nline 2\nline 3\nline 4\nline 5\n"
    )


def test_lines_can_be_streamed_oldest_first_across_both_files(tmp_path):
    path = tmp_path / "app.log"
    sink = LineLimitedFile(path, lines_per_file=3)
    _write_numbered(sink, 5)

    assert list(log_store.iter_lines(path)) == [f"line {n}" for n in range(1, 6)]


def test_clear_empties_both_files_and_logging_continues(tmp_path):
    path = tmp_path / "app.log"
    sink = log_store.LineLimitedFile(path, lines_per_file=3)
    for number in range(5):
        sink.write(f"line {number}\n")
    assert log_store.backup_path(path).exists()
    sink.clear()
    assert not log_store.backup_path(path).exists()
    assert log_store.read_lines(path) == []
    assert sink.line_count == 0
    sink.write("after\n")
    sink.stop()
    assert log_store.read_lines(path) == ["after"]
