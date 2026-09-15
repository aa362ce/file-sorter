from __future__ import annotations

from datetime import datetime
from pathlib import Path

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
from ..history import export_history, import_history, load_history


class HistoryDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run History")
        self.resize(700, 400)

        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["When", "Status", "Directories", "Groups", "Reclaimable", "Duration"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        button_row = QHBoxLayout()
        export_btn = QPushButton("Export...")
        export_btn.clicked.connect(self._export)
        import_btn = QPushButton("Import...")
        import_btn.clicked.connect(self._import)
        button_row.addWidget(export_btn)
        button_row.addWidget(import_btn)
        button_row.addStretch()
        layout.addLayout(button_row)

        self._reload()

    def _reload(self) -> None:
        records = list(reversed(load_history()))
        table = self.table
        table.clearSpans()

        if not records:
            table.setRowCount(1)
            table.setSpan(0, 0, 1, 6)
            table.setItem(0, 0, QTableWidgetItem("No run history yet."))
            return

        table.setRowCount(len(records))
        for row, record in enumerate(records):
            when = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
            status = "Cancelled" if record.cancelled else "Done"
            table.setItem(row, 0, QTableWidgetItem(when))
            table.setItem(row, 1, QTableWidgetItem(status))
            table.setItem(row, 2, QTableWidgetItem(", ".join(record.directories)))
            table.setItem(row, 3, QTableWidgetItem(str(record.groups)))
            table.setItem(row, 4, QTableWidgetItem(human_size(record.reclaimable_bytes)))
            table.setItem(row, 5, QTableWidgetItem(f"{record.duration_seconds:.1f}s"))

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
