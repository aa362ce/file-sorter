from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

from ..dedupe import ScanResult, find_duplicates, scan_or_reuse
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
        exclude_dirs: Optional[Iterable[str]] = None,
        exclude_temp_files: bool = True,
        file_types: Optional[Iterable[str]] = None,
    ) -> None:
        super().__init__(parent)
        self._directories = list(directories)
        self._resume_state = resume_state
        self._run_id = run_id
        self._exclude_dirs = exclude_dirs
        self._exclude_temp_files = exclude_temp_files
        self._file_types = file_types
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
        common_kwargs = dict(
            on_progress=lambda stage, count, total: self.progress.emit(stage, count, total),
            cancel_event=self._cancel_event,
            resume_state=self._resume_state,
            on_checkpoint=self._checkpoint if self._run_id is not None else None,
            on_group_found=lambda groups: self.group_found.emit(groups),
            exclude_dirs=self._exclude_dirs,
            exclude_temp_files=self._exclude_temp_files,
            file_types=self._file_types,
        )
        if self._run_id is not None:
            # scan_or_reuse needs a run_id to attribute a freshly-collected
            # manifest to (see there) -- checks whether this directory set
            # is unchanged since a previous scan, and if so replays that
            # scan's saved results instead of rehashing everything.
            result = scan_or_reuse(self._directories, run_id=self._run_id, **common_kwargs)
        else:
            result = find_duplicates(self._directories, **common_kwargs)
        self.finished_scan.emit(result)
