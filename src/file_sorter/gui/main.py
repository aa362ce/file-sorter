from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from ..dedupe import DuplicateGroup, ScanResult
from ..formatting import human_size
from ..history import record_run
from ..resume import ResumeState, clear_resume_state, load_resume_state, save_resume_state
from .history_dialog import HistoryDialog
from .worker import ScanWorker

ICON_PATH = Path(__file__).resolve().parent / "resources" / "icon.png"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("file-sorter — duplicate finder")
        self.setWindowIcon(QIcon(str(ICON_PATH)))

        self._worker: Optional[ScanWorker] = None
        self._updating_check = False
        self._scan_directories: list[Path] = []
        self._scan_start: Optional[float] = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        dir_row = QHBoxLayout()
        self.dir_list = QListWidget()
        self.dir_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        dir_buttons = QVBoxLayout()
        add_btn = QPushButton("Add Directory...")
        add_btn.clicked.connect(self._add_directory)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected_directories)
        dir_buttons.addWidget(add_btn)
        dir_buttons.addWidget(remove_btn)
        dir_buttons.addStretch()
        dir_row.addWidget(self.dir_list)
        dir_row.addLayout(dir_buttons)
        layout.addLayout(dir_row)

        scan_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan for Duplicates")
        self.scan_btn.clicked.connect(lambda: self._start_scan())
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_scan)
        self.cancel_btn.setEnabled(False)
        self.resume_btn = QPushButton("Resume Last Run")
        self.resume_btn.clicked.connect(self._resume_scan)
        self.resume_btn.setEnabled(load_resume_state() is not None)
        self.history_btn = QPushButton("History")
        self.history_btn.clicked.connect(self._show_history)
        self.progress_bar = QProgressBar()
        self.status_label = QLabel("")
        scan_row.addWidget(self.scan_btn)
        scan_row.addWidget(self.cancel_btn)
        scan_row.addWidget(self.resume_btn)
        scan_row.addWidget(self.progress_bar, 1)
        scan_row.addWidget(self.status_label)
        scan_row.addWidget(self.history_btn)
        layout.addLayout(scan_row)

        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["File", "Size"])
        self.results_tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.results_tree, 1)

        bottom_row = QHBoxLayout()
        self.reclaimable_label = QLabel("Checked for deletion: 0B")
        self.delete_btn = QPushButton("Delete Checked (to Trash)")
        self.delete_btn.clicked.connect(self._delete_checked)
        self.delete_btn.setEnabled(False)
        bottom_row.addWidget(self.reclaimable_label, 1)
        bottom_row.addWidget(self.delete_btn)
        layout.addLayout(bottom_row)

    # -- directory list -----------------------------------------------

    def _add_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Select directory to scan")
        if not directory:
            return
        resolved = str(Path(directory).resolve())
        existing = {self.dir_list.item(i).text() for i in range(self.dir_list.count())}
        if resolved not in existing:
            self.dir_list.addItem(resolved)

    def _remove_selected_directories(self) -> None:
        for item in self.dir_list.selectedItems():
            self.dir_list.takeItem(self.dir_list.row(item))

    # -- scanning -------------------------------------------------------

    def _start_scan(self, resume_state: Optional[ResumeState] = None) -> None:
        directories = [Path(self.dir_list.item(i).text()) for i in range(self.dir_list.count())]
        if not directories:
            QMessageBox.information(self, "No directories", "Add at least one directory to scan.")
            return

        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.resume_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.results_tree.clear()
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Resuming scan..." if resume_state else "Scanning...")

        self._scan_directories = directories
        self._scan_start = time.monotonic()

        self._worker = ScanWorker(directories, resume_state=resume_state)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_scan.connect(self._on_scan_finished)
        self._worker.start()

    def _resume_scan(self) -> None:
        resume_state = load_resume_state()
        if resume_state is None:
            QMessageBox.information(self, "Nothing to resume", "There's no stopped run to resume.")
            self.resume_btn.setEnabled(False)
            return

        self.dir_list.clear()
        for directory in resume_state.directories:
            self.dir_list.addItem(directory)
        self._start_scan(resume_state=resume_state)

    def _cancel_scan(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("Cancelling...")

    def _on_progress(self, stage: str, count: int, total: object) -> None:
        if total:
            self.progress_bar.setRange(0, int(total))
            self.progress_bar.setValue(count)
            self.status_label.setText(f"{stage}: {count}/{total}")
        else:
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(f"{stage}: {count}")

    def _on_scan_finished(self, result: ScanResult) -> None:
        self.scan_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

        for group in result.groups:
            self._add_group(group)

        duration = time.monotonic() - self._scan_start if self._scan_start is not None else 0.0
        record_run(self._scan_directories, result, duration)

        if result.cancelled and result.resume_state is not None:
            save_resume_state(result.resume_state)
            self.resume_btn.setEnabled(True)
        else:
            clear_resume_state()
            self.resume_btn.setEnabled(False)

        skipped_note = f", {len(result.skipped)} file(s) skipped" if result.skipped else ""
        cancelled_note = " (cancelled -- partial results)" if result.cancelled else ""
        self.status_label.setText(f"{len(result.groups)} duplicate group(s) found{skipped_note}{cancelled_note}")
        self.delete_btn.setEnabled(bool(result.groups))
        self._update_reclaimable_label()
        self._worker = None

    def _show_history(self) -> None:
        HistoryDialog(self).exec()

    # -- results tree -----------------------------------------------------

    def _add_group(self, group: DuplicateGroup) -> None:
        header = QTreeWidgetItem([f"{len(group.paths)} copies, {human_size(group.size)} each", ""])
        self.results_tree.addTopLevelItem(header)
        for index, path in enumerate(group.paths):
            child = QTreeWidgetItem([str(path), human_size(group.size)])
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setData(0, Qt.ItemDataRole.UserRole, (path, group.size))
            # Default: keep the first copy, mark the rest for deletion.
            child.setCheckState(0, Qt.CheckState.Unchecked if index == 0 else Qt.CheckState.Checked)
            header.addChild(child)
        header.setExpanded(True)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating_check or item.parent() is None:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            group = item.parent()
            checked = sum(
                1
                for i in range(group.childCount())
                if group.child(i).checkState(0) == Qt.CheckState.Checked
            )
            if checked == group.childCount():
                self._updating_check = True
                item.setCheckState(0, Qt.CheckState.Unchecked)
                self._updating_check = False
                QMessageBox.warning(
                    self,
                    "Keep at least one copy",
                    "At least one file in each duplicate group must stay unchecked.",
                )
                return
        self._update_reclaimable_label()

    def _checked_items(self) -> list[QTreeWidgetItem]:
        items = []
        for i in range(self.results_tree.topLevelItemCount()):
            group = self.results_tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    items.append(child)
        return items

    def _update_reclaimable_label(self) -> None:
        total = sum(item.data(0, Qt.ItemDataRole.UserRole)[1] for item in self._checked_items())
        self.reclaimable_label.setText(f"Checked for deletion: {human_size(total)}")

    # -- deletion -----------------------------------------------------

    def _delete_checked(self) -> None:
        items = self._checked_items()
        if not items:
            return
        confirm = QMessageBox.question(
            self,
            "Confirm deletion",
            f"Move {len(items)} file(s) to Trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        failures = []
        for item in items:
            path, _size = item.data(0, Qt.ItemDataRole.UserRole)
            try:
                send2trash(str(path))
            except OSError as exc:
                failures.append(f"{path}: {exc}")
                continue
            group = item.parent()
            group.removeChild(item)
            if group.childCount() <= 1:
                index = self.results_tree.indexOfTopLevelItem(group)
                self.results_tree.takeTopLevelItem(index)

        self._update_reclaimable_label()
        if failures:
            QMessageBox.warning(self, "Some files could not be deleted", "\n".join(failures))

    # -- window lifecycle -----------------------------------------------

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(2000)
        event.accept()


def run() -> None:
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.resize(900, 600)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
