from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import QDialog, QHeaderView, QTableWidget, QTableWidgetItem, QVBoxLayout

from ..formatting import human_size
from ..history import load_history


class HistoryDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run History")
        self.resize(700, 400)

        layout = QVBoxLayout(self)
        table = QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["When", "Status", "Directories", "Groups", "Reclaimable", "Duration"])
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(table)

        records = list(reversed(load_history()))
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
