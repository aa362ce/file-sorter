from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

from ..dedupe import ScanResult, find_duplicates
from ..store import CheckpointDelta, ResumeState, checkpoint_progress


class ScanWorker(QThread):
    """Runs find_duplicates off the UI thread so the window stays responsive."""

    progress = Signal(str, int, object)
    group_found = Signal(object)
    finished_scan = Signal(object)

    def __init__(
        self,
        directories: Iterable[Path],
        parent=None,
        *,
        resume_state: Optional[ResumeState] = None,
        run_id: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._directories = list(directories)
        self._resume_state = resume_state
        self._run_id = run_id
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _checkpoint(self, delta: CheckpointDelta) -> None:
        # Runs on this worker thread, not the GUI thread -- safe since
        # checkpoint_progress opens and closes its own SQLite connection
        # per call rather than sharing one across threads.
        if self._run_id is not None:
            checkpoint_progress(self._run_id, delta)

    def run(self) -> None:
        result = find_duplicates(
            self._directories,
            on_progress=lambda stage, count, total: self.progress.emit(stage, count, total),
            cancel_event=self._cancel_event,
            resume_state=self._resume_state,
            on_checkpoint=self._checkpoint if self._run_id is not None else None,
            on_group_found=lambda groups: self.group_found.emit(groups),
        )
        self.finished_scan.emit(result)
