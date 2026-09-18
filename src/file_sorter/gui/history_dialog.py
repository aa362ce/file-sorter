from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..formatting import human_size
from ..store import (
    RunRecord,
    export_history,
    import_history,
    load_history,
    load_resume_state,
    load_run_groups,
    resumable_run_ids,
)


class HistoryDialog(QDialog):
    # Emits (run_id, ResumeState) when the user picks a resumable run and
    # clicks "Resume Selected" -- the main window does the actual resuming,
    # since it owns the directory list and the scan worker.
    resume_requested = Signal(str, object)
    # Emits (RunRecord, groups, folder_groups) when the user picks a run
    # with saved detailed results and clicks "Load Selected" -- the main
    # window renders them into the results tree, same as a fresh scan's
    # result, without re-scanning anything.
    load_requested = Signal(object, object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run History")
        self.resize(700, 400)

        self._records: list[RunRecord] = []
        self._resumable: set[str] = set()

        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["When", "Status", "Directories", "Groups", "Reclaimable", "Duration"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self._update_button_states)
        layout.addWidget(self.table)

        button_row = QHBoxLayout()
        self.resume_btn = QPushButton("Resume Selected")
        self.resume_btn.clicked.connect(self._resume_selected)
        self.resume_btn.setEnabled(False)
        self.load_btn = QPushButton("Load Selected")
        self.load_btn.clicked.connect(self._load_selected)
        self.load_btn.setEnabled(False)
        export_btn = QPushButton("Export...")
        export_btn.clicked.connect(self._export)
        import_btn = QPushButton("Import...")
        import_btn.clicked.connect(self._import)
        button_row.addWidget(self.resume_btn)
        button_row.addWidget(self.load_btn)
        button_row.addWidget(export_btn)
        button_row.addWidget(import_btn)
        button_row.addStretch()
        layout.addLayout(button_row)

        self._reload()

    def _reload(self) -> None:
        self._records = list(reversed(load_history()))
        self._resumable = resumable_run_ids()
        records = self._records
        table = self.table
        table.clearSpans()

        if not records:
            table.setRowCount(1)
            table.setSpan(0, 0, 1, 6)
            table.setItem(0, 0, QTableWidgetItem("No run history yet."))
            self._update_button_states()
            return

        table.setRowCount(len(records))
        for row, record in enumerate(records):
            when = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
            if not record.cancelled:
                status = "Done"
            elif self._is_resumable(row):
                status = "Cancelled (resumable)"
            else:
                status = "Cancelled"
            table.setItem(row, 0, QTableWidgetItem(when))
            table.setItem(row, 1, QTableWidgetItem(status))
            table.setItem(row, 2, QTableWidgetItem(", ".join(record.directories)))
            table.setItem(row, 3, QTableWidgetItem(str(record.groups)))
            table.setItem(row, 4, QTableWidgetItem(human_size(record.reclaimable_bytes)))
            table.setItem(row, 5, QTableWidgetItem(f"{record.duration_seconds:.1f}s"))
        self._update_button_states()

    def _is_resumable(self, row: int) -> bool:
        if row < 0 or row >= len(self._records):
            return False
        record = self._records[row]
        return record.cancelled and str(record.timestamp) in self._resumable

    def _selected_record(self, row: int) -> Optional[RunRecord]:
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    def _update_button_states(self) -> None:
        row = self.table.currentRow()
        self.resume_btn.setEnabled(self._is_resumable(row))
        self.load_btn.setEnabled(self._selected_record(row) is not None)

    def _resume_selected(self) -> None:
        row = self.table.currentRow()
        if not self._is_resumable(row):
            return
        record = self._records[row]
        run_id = str(record.timestamp)
        resume_state = load_resume_state(run_id)
        if resume_state is None:
            QMessageBox.information(
                self, "Nothing to resume", "This run's saved progress is no longer available."
            )
            self._reload()
            return
        self.resume_requested.emit(run_id, resume_state)
        self.accept()

    def _load_selected(self) -> None:
        row = self.table.currentRow()
        record = self._selected_record(row)
        if record is None:
            return
        run_id = str(record.timestamp)
        loaded = load_run_groups(run_id)
        if loaded is None:
            QMessageBox.information(
                self,
                "Nothing to load",
                "This run has no saved detailed results (only its summary is kept) -- "
                "it may predate this feature, or have been imported from a history export.",
            )
            return
        groups, folder_groups = loaded
        self.load_requested.emit(record, groups, folder_groups)
        self.accept()

    def _export(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export run history", "file-sorter-history.json", "JSON files (*.json)"
        )
        if not path_str:
            return
        try:
            count = export_history(Path(path_str))
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Exported {count} run(s) to {path_str}")

    def _import(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(self, "Import run history", "", "JSON files (*.json)")
        if not path_str:
            return
        try:
            added = import_history(Path(path_str))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self._reload()
        QMessageBox.information(self, "Import complete", f"Imported {added} new run(s) from {path_str}")
