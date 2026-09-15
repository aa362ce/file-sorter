from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from ..dedupe import ScanResult, find_duplicates


class ScanWorker(QThread):
    """Runs find_duplicates off the UI thread so the window stays responsive."""

    progress = Signal(str, int, object)
    finished_scan = Signal(object)

    def __init__(self, directories: Iterable[Path], parent=None) -> None:
        super().__init__(parent)
        self._directories = list(directories)

    def run(self) -> None:
        result = find_duplicates(
            self._directories,
            on_progress=lambda stage, count, total: self.progress.emit(stage, count, total),
        )
        self.finished_scan.emit(result)
