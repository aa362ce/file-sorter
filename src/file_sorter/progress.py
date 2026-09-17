from __future__ import annotations

import sys
import time
from typing import Callable, Optional, TextIO


class Progress:
    """Reports periodic progress to a stream without cluttering stdout, and
    optionally to an external callback (e.g. the GUI's progress bar).

    On a TTY, updates a single line in place. Otherwise (piped/redirected
    output), emits one line per interval so the log stays readable.

    `on_progress(label, count, total)`, if given, is throttled to the same
    `interval` as the terminal output, independently of `enabled` -- a
    caller processing many files per second (a fast SSD, or many worker
    threads) would otherwise invoke it once per file, and for a very large
    scan that can mean hundreds of thousands of calls in rapid succession.
    Across a queued cross-thread connection (as in the GUI, where this
    drives a Qt signal from a worker thread from the main one) that can
    flood the receiving thread's event queue faster than it can drain,
    making the UI appear frozen even though the scan itself is proceeding
    normally. `close()` always fires it once more unconditionally, so the
    final count/total is never left stuck at a stale throttled value.
    """

    def __init__(
        self,
        label: str,
        total: int | None = None,
        *,
        interval: float = 0.2,
        stream: TextIO | None = None,
        enabled: bool = True,
        on_progress: Optional[Callable[[str, int, int | None], None]] = None,
    ) -> None:
        self.label = label
        self.total = total
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.on_progress = on_progress
        self.count = 0
        self._last_emit = 0.0
        self._last_callback = 0.0
        self._is_tty = self.enabled and self.stream.isatty()

    def update(self, n: int = 1) -> None:
        self.count += n
        now = time.monotonic()
        if self.enabled and now - self._last_emit >= self.interval:
            self._emit()
            self._last_emit = now
        if self.on_progress is not None and now - self._last_callback >= self.interval:
            self.on_progress(self.label, self.count, self.total)
            self._last_callback = now

    def _emit(self) -> None:
        msg = f"{self.label}: {self.count}/{self.total}" if self.total else f"{self.label}: {self.count}"
        if self._is_tty:
            self.stream.write(f"\r\033[K{msg}")
        else:
            self.stream.write(msg + "\n")
        self.stream.flush()

    def close(self) -> None:
        if self.enabled:
            self._emit()
            if self._is_tty:
                self.stream.write("\n")
            self.stream.flush()
        if self.on_progress is not None:
            self.on_progress(self.label, self.count, self.total)
