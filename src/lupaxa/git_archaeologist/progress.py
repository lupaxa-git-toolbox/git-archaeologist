"""Stderr progress. A terminal gets a spinner; a pipe gets one line per phase."""

from __future__ import annotations

import sys
import threading
from typing import TextIO

from colored import Fore, Style

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Progress:
    """Keep a long phase visibly alive without writing to stdout."""

    def __init__(
        self,
        *,
        enabled: bool,
        tty: bool,
        color: bool = False,
        stream: TextIO | None = None,
    ) -> None:
        self.enabled = enabled
        self.tty = tty and enabled
        self.color = color and self.tty
        self.stream = stream if stream is not None else sys.stderr
        self._label = ""
        self._current: int | None = None
        self._total: int | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick = 0
        self._painted = False
        self._cursor_hidden = False
        self._closed = False

    def update(self, label: str, current: int | None = None, total: int | None = None) -> None:
        """Set the visible phase. Counts are shown only when ``total`` is known."""
        if not self.enabled or self._closed:
            return
        with self._lock:
            label_changed = label != self._label
            self._label = label
            if current is not None:
                self._current = current
            elif label_changed:
                self._current = None
            if total is not None:
                self._total = total
            elif label_changed:
                self._total = None
            if not self.tty:
                if label_changed:
                    self.stream.write(f"phase: {label}\n")
                    self.stream.flush()
                return
            if self._thread is None:
                self._hide_cursor()
                self._thread = threading.Thread(
                    target=self._spin,
                    name="git-archaeologist-progress",
                    daemon=True,
                )
                self._thread.start()
            self._paint_unlocked()

    def frame(self, tick: int) -> str:
        """One spinner line, without the carriage return."""
        with self._lock:
            return self._frame_unlocked(tick)

    def stop(self) -> None:
        """Clear the spinner line and restore the cursor. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self._lock:
            if self.tty and self._painted:
                self.stream.write("\r\x1b[K")
            if self._cursor_hidden:
                self.stream.write("\x1b[?25h")
                self._cursor_hidden = False
            self.stream.flush()

    def _spin(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._tick += 1
                self._paint_unlocked()

    def _hide_cursor(self) -> None:
        self.stream.write("\x1b[?25l")
        self.stream.flush()
        self._cursor_hidden = True

    def _paint_unlocked(self) -> None:
        self.stream.write("\r" + self._frame_unlocked(self._tick) + "\x1b[K")
        self.stream.flush()
        self._painted = True

    def _frame_unlocked(self, tick: int) -> str:
        glyph = _FRAMES[tick % len(_FRAMES)]
        if self.color:
            glyph = f"{Fore.cyan}{glyph}{Style.reset}"
        text = f"{glyph} {self._label}"
        if self._current is not None and self._total:
            text += f"  {self._current:,}/{self._total:,}  {self._bar()}"
        return text

    def _bar(self) -> str:
        total = self._total or 0
        current = self._current or 0
        width = 20
        if total <= 0:
            return "[" + ("-" * width) + "]"
        filled = min(width, int(width * current / total))
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"
