"""Progress stays on stderr and only counts known totals."""

from __future__ import annotations

import io

from lupaxa.git_archaeologist.progress import Progress


def test_quiet_progress_writes_nothing() -> None:
    stream = io.StringIO()
    progress = Progress(enabled=False, tty=True, stream=stream)
    progress.update("Reading commits", 1, 10)
    progress.stop()
    assert stream.getvalue() == ""


def test_pipe_prints_one_line_per_phase() -> None:
    stream = io.StringIO()
    progress = Progress(enabled=True, tty=False, stream=stream)
    progress.update("Reading commits")
    progress.update("Reading commits", 1, 4)
    progress.update("Reading changes", 1, 4)
    progress.stop()
    assert stream.getvalue() == "phase: Reading commits\nphase: Reading changes\n"


def test_tty_frame_shows_spinner_count_and_bar_without_blue() -> None:
    stream = io.StringIO()
    progress = Progress(enabled=True, tty=True, color=True, stream=stream)
    progress.update("Reading changes", 5, 10)
    text = progress.frame(0)
    progress.stop()
    assert "Reading changes" in text
    assert "5/10" in text
    assert "[##########----------]" in text
    assert "\x1b[34" not in text
    assert "\x1b[38;5;4" not in text
    assert "\x1b[K" in stream.getvalue()
    assert "\x1b[?25h" in stream.getvalue()


def test_unknown_total_has_no_bar() -> None:
    progress = Progress(enabled=True, tty=True, stream=io.StringIO())
    progress.update("Reading commits")
    text = progress.frame(1)
    progress.stop()
    assert "Reading commits" in text
    assert "[" not in text
    assert "/" not in text
