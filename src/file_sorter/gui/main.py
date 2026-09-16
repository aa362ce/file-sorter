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

from ..dedupe import DuplicateGroup, ScanResult, files_equal
from ..folders import FolderGroup
from ..formatting import human_size
from ..history import record_run
from ..resume import ResumeState, clear_resume_state, latest_resume_run_id, load_resume_state, save_resume_state
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
        self._resume_run_id: Optional[str] = None

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
        self.resume_btn.setEnabled(latest_resume_run_id() is not None)
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

    def _start_scan(self, resume_state: Optional[ResumeState] = None, run_id: Optional[str] = None) -> None:
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
        self._resume_run_id = run_id

        self._worker = ScanWorker(directories, resume_state=resume_state)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_scan.connect(self._on_scan_finished)
        self._worker.start()

    def _resume_scan(self) -> None:
        run_id = latest_resume_run_id()
        resume_state = load_resume_state(run_id) if run_id is not None else None
        if resume_state is None:
            QMessageBox.information(self, "Nothing to resume", "There's no stopped run to resume.")
            self.resume_btn.setEnabled(False)
            return

        self.dir_list.clear()
        for directory in resume_state.directories:
            self.dir_list.addItem(directory)
        self._start_scan(resume_state=resume_state, run_id=run_id)

    def _resume_from_history(self, run_id: str, resume_state: ResumeState) -> None:
        self.dir_list.clear()
        for directory in resume_state.directories:
            self.dir_list.addItem(directory)
        self._start_scan(resume_state=resume_state, run_id=run_id)

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

        # Files inside a confirmed folder match are rendered informational
        # (no checkbox) in their own file-level group below -- their fate
        # is governed entirely by that folder's checkbox instead, since the
        # two are computed independently and could otherwise disagree
        # about which copy is "kept" for the very same file.
        confirmed_folder_dirs = [d for fg in result.folder_groups if fg.confirmed for d in fg.paths]

        for folder_group in result.folder_groups:
            self._add_folder_group(folder_group)
        for group in result.groups:
            self._add_group(group, confirmed_folder_dirs)

        duration = time.monotonic() - self._scan_start if self._scan_start is not None else 0.0
        record = record_run(self._scan_directories, result, duration)

        # Whichever stopped run this attempt just consumed (if any) is done
        # with -- either it finished, or it got cancelled again and a fresh
        # resume state was saved below under this new record's own id. That
        # never touches *other* stopped runs still sitting in history, so
        # each stays independently resumable.
        if self._resume_run_id is not None:
            clear_resume_state(self._resume_run_id)
            self._resume_run_id = None

        if result.cancelled and result.resume_state is not None:
            save_resume_state(str(record.timestamp), result.resume_state)
        self.resume_btn.setEnabled(latest_resume_run_id() is not None)

        folder_note = f", {len(result.folder_groups)} duplicate folder(s)" if result.folder_groups else ""
        skipped_note = f", {len(result.skipped)} file(s) skipped" if result.skipped else ""
        cancelled_note = " (cancelled -- partial results)" if result.cancelled else ""
        self.status_label.setText(
            f"{len(result.groups)} duplicate group(s) found{folder_note}{skipped_note}{cancelled_note}"
        )
        self.delete_btn.setEnabled(bool(result.groups))
        self._update_reclaimable_label()
        self._worker = None

    def _show_history(self) -> None:
        dialog = HistoryDialog(self)
        dialog.resume_requested.connect(self._resume_from_history)
        dialog.exec()

    # -- results tree -----------------------------------------------------

    def _add_folder_group(self, group: FolderGroup) -> None:
        """A confirmed folder group is checkable, same "keep first, check
        the rest" convention as `_add_group` -- checking a copy and
        deleting it removes the whole directory in one action. An
        unconfirmed (large-file) group is informational only: its files
        still appear as regular, individually-deletable groups via
        `_add_group`, since bulk-deleting an unverified folder isn't safe.
        """
        label = f"\U0001F4C1 Folder duplicate: {len(group.paths)} copies ({group.file_count} files, {human_size(group.size)} each)"
        if not group.confirmed:
            label += "  (unverified -- large file(s), handled at the file level instead)"
        header = QTreeWidgetItem([label, ""])
        if group.confirmed:
            header.setData(0, Qt.ItemDataRole.UserRole, group)
        else:
            header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        self.results_tree.addTopLevelItem(header)
        for index, path in enumerate(group.paths):
            child = QTreeWidgetItem([str(path), human_size(group.size)])
            if group.confirmed:
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, Qt.ItemDataRole.UserRole, (path, group.size))
                child.setCheckState(0, Qt.CheckState.Unchecked if index == 0 else Qt.CheckState.Checked)
            else:
                # No UserRole group data is set on `header` in this branch,
                # so these rows must stay non-checkable -- _delete_checked()
                # would crash trying to read a group off it otherwise.
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            header.addChild(child)
        header.setExpanded(True)

    def _add_group(self, group: DuplicateGroup, confirmed_folder_dirs: Optional[list[Path]] = None) -> None:
        confirmed_folder_dirs = confirmed_folder_dirs or []
        label = f"{len(group.paths)} copies, {human_size(group.size)} each"
        if not group.confirmed:
            label += "  (unverified -- large file, checked before deletion)"
        header = QTreeWidgetItem([label, ""])
        header.setData(0, Qt.ItemDataRole.UserRole, group)
        self.results_tree.addTopLevelItem(header)

        def covered(path: Path) -> bool:
            return any(path.is_relative_to(d) for d in confirmed_folder_dirs)

        # Among files NOT covered by a confirmed folder match (handled
        # below), the first one defaults to kept/unchecked -- not
        # necessarily group.paths[0], since that could be a covered file.
        first_uncovered = next((i for i, p in enumerate(group.paths) if not covered(p)), None)

        for index, path in enumerate(group.paths):
            child = QTreeWidgetItem([str(path), human_size(group.size)])
            if covered(path):
                # This copy's fate is decided by its folder's checkbox in
                # the "Folder duplicate" entry above, not here -- deciding
                # both independently risks disagreeing about which copy of
                # this very file is the one being kept.
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            else:
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, Qt.ItemDataRole.UserRole, (path, group.size))
                # Default: keep the first (uncovered) copy, check the rest.
                child.setCheckState(0, Qt.CheckState.Unchecked if index == first_uncovered else Qt.CheckState.Checked)
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

    def _kept_paths(self, group_item: QTreeWidgetItem) -> list[Path]:
        """Paths under `group_item` currently unchecked (i.e. being kept),
        used as references to verify an unconfirmed group's files against
        right before deletion -- see `_add_group`/`DuplicateGroup.confirmed`.
        """
        kept = []
        for i in range(group_item.childCount()):
            child = group_item.child(i)
            if child.checkState(0) == Qt.CheckState.Unchecked:
                path, _size = child.data(0, Qt.ItemDataRole.UserRole)
                kept.append(path)
        return kept

    def _remove_item(self, item: QTreeWidgetItem) -> None:
        parent = item.parent()
        parent.removeChild(item)
        if parent.childCount() <= 1:
            index = self.results_tree.indexOfTopLevelItem(parent)
            self.results_tree.takeTopLevelItem(index)

    def _delete_checked(self) -> None:
        checked = self._checked_items()
        if not checked:
            return

        folder_items = []
        file_items = []
        for item in checked:
            if isinstance(item.parent().data(0, Qt.ItemDataRole.UserRole), FolderGroup):
                folder_items.append(item)
            else:
                file_items.append(item)

        confirm = QMessageBox.question(
            self,
            "Confirm deletion",
            f"Move {len(folder_items)} folder(s) and {len(file_items)} file(s) to Trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # A file's own group might have its "kept" copy sitting in a
        # folder that's about to be bulk-deleted (the per-file and
        # per-folder "keep first, check rest" choices are independent) --
        # so a file living anywhere under one of these folders must be
        # left entirely to the folder-level deletion below, never
        # individually deleted, or the only remaining copy could be lost.
        delete_dirs = [item.data(0, Qt.ItemDataRole.UserRole)[0] for item in folder_items]

        def under_any(path: Path, roots: list[Path]) -> bool:
            return any(path.is_relative_to(root) for root in roots)

        # A file-level group's own "kept" pick (index 0, unchecked) is
        # computed independently of which folder copy is being kept, so a
        # checked file could be the last surviving copy of its group once
        # folder-level (and other checked file-level) deletions are
        # applied. Guard against that generally: a file is only actually
        # deleted if at least one other member of its group will still
        # exist afterward.
        checked_file_paths = {item.data(0, Qt.ItemDataRole.UserRole)[0] for item in file_items}

        def has_survivor(group: DuplicateGroup, path: Path) -> bool:
            return any(
                p != path and not under_any(p, delete_dirs) and p not in checked_file_paths for p in group.paths
            )

        failures = []

        for item in folder_items:
            path, _size = item.data(0, Qt.ItemDataRole.UserRole)
            try:
                send2trash(str(path))
            except OSError as exc:
                failures.append(f"{path}: {exc}")
                continue
            self._remove_item(item)

        for item in file_items:
            path, _size = item.data(0, Qt.ItemDataRole.UserRole)
            if under_any(path, delete_dirs):
                # Already handled by a folder-level deletion above.
                self._remove_item(item)
                continue

            group_item = item.parent()
            group: DuplicateGroup = group_item.data(0, Qt.ItemDataRole.UserRole)

            if not has_survivor(group, path):
                failures.append(
                    f"{path}: deleting it would remove the last remaining copy of this file -- skipped"
                )
                continue

            if not group.confirmed:
                # Confirmation of this group was deferred during the scan
                # (a very large file) -- do it now, against whichever
                # file(s) in the group are being kept, before actually
                # deleting anything.
                verified = False
                for reference in self._kept_paths(group_item):
                    try:
                        if files_equal(reference, path):
                            verified = True
                            break
                    except OSError:
                        continue
                if not verified:
                    failures.append(
                        f"{path}: not verified as an actual duplicate of the kept file(s) -- "
                        "skipped rather than risk deleting a non-duplicate"
                    )
                    continue

            try:
                send2trash(str(path))
            except OSError as exc:
                failures.append(f"{path}: {exc}")
                continue
            self._remove_item(item)

        self._update_reclaimable_label()
        if failures:
            QMessageBox.warning(self, "Some items could not be deleted", "\n".join(failures))

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
