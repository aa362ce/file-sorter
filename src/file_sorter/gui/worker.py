from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

from ..dedupe import ScanResult, find_duplicates
from ..resume import ResumeState


class ScanWorker(QThread):
    """Runs find_duplicates off the UI thread so the window stays responsive."""

    progress = Signal(str, int, object)
    finished_scan = Signal(object)

    def __init__(
        self,
        directories: Iterable[Path],
        parent=None,
        *,
        resume_state: Optional[ResumeState] = None,
    ) -> None:
        super().__init__(parent)
        self._directories = list(directories)
        self._resume_state = resume_state
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        result = find_duplicates(
            self._directories,
            on_progress=lambda stage, count, total: self.progress.emit(stage, count, total),
            cancel_event=self._cancel_event,
            resume_state=self._resume_state,
        )
        self.finished_scan.emit(result)
