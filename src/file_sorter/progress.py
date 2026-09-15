from __future__ import annotations

import sys
import time
from typing import TextIO


class Progress:
    """Reports periodic progress to a stream without cluttering stdout.

    On a TTY, updates a single line in place. Otherwise (piped/redirected
    output), emits one line per interval so the log stays readable.
    """

    def __init__(
        self,
        label: str,
        total: int | None = None,
        *,
        interval: float = 0.2,
        stream: TextIO | None = None,
        enabled: bool = True,
    ) -> None:
        self.label = label
        self.total = total
        self.interval = interval
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.count = 0
        self._last_emit = 0.0
        self._is_tty = self.enabled and self.stream.isatty()

    def update(self, n: int = 1) -> None:
        self.count += n
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last_emit >= self.interval:
            self._emit()
            self._last_emit = now

    def _emit(self) -> None:
        msg = f"{self.label}: {self.count}/{self.total}" if self.total else f"{self.label}: {self.count}"
        if self._is_tty:
            self.stream.write(f"\r\033[K{msg}")
        else:
            self.stream.write(msg + "\n")
        self.stream.flush()

    def close(self) -> None:
        if not self.enabled:
            return
        self._emit()
        if self._is_tty:
            self.stream.write("\n")
        self.stream.flush()
