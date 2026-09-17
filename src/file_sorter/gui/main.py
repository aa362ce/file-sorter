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
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash

from ..dedupe import (
    DEFAULT_EXCLUDED_DIR_NAMES,
    DEFAULT_EXCLUDED_FILE_EXTENSIONS,
    DEFAULT_EXCLUDED_FILE_NAMES,
    VALID_FILE_TYPES,
    DuplicateGroup,
    ScanResult,
    file_matches_types,
    files_equal,
)
from ..folders import FolderGroup
from ..formatting import human_size
from ..store import (
    ResumeState,
    clear_resume_state,
    latest_resume_run_id,
    load_resume_state,
    record_run,
    save_resume_state,
)
from .file_explorer import FileExplorer
from .history_dialog import HistoryDialog
from .worker import ScanWorker

ICON_PATH = Path(__file__).resolve().parent / "resources" / "icon.png"

# A scan can easily produce thousands of groups -- neither reviewable by a
# person nor free to render (see _render_results) -- so only the most
# impactful ones (by how much space deleting all but one copy would
# actually reclaim) are ever shown.
TOP_RESULTS_LIMIT = 10


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
        self._run_id: Optional[str] = None
        # The finished scan currently on display, and the filter row's
        # per-category checkboxes -- see _render_results. Re-filtering the
        # view never re-scans: it's a pure re-render of this same result.
        self._last_result: Optional[ScanResult] = None
        self._last_result_cancelled_note = ""
        self._type_filter_checkboxes: dict[str, QCheckBox] = {}
        # Which types to scan for at all (empty selection = every file) --
        # distinct from _type_filter_checkboxes above, which only changes
        # what's *displayed* from a scan that already covered everything.
        self._scan_type_checkboxes: dict[str, QCheckBox] = {}

        central = QWidget()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        central_layout.addWidget(splitter)

        self.file_explorer = FileExplorer()
        self.file_explorer.directory_chosen.connect(self._add_directory_path)
        splitter.addWidget(self.file_explorer)

        main_panel = QWidget()
        layout = QVBoxLayout(main_panel)
        splitter.addWidget(main_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 640])

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

        self.exclude_common_checkbox = QCheckBox("Skip node_modules, virtualenvs, caches && temp files")
        self.exclude_common_checkbox.setChecked(True)
        self.exclude_common_checkbox.setToolTip(
            "Skips these directories entirely wherever found, not just at the top level:\n"
            + ", ".join(sorted(DEFAULT_EXCLUDED_DIR_NAMES))
            + "\n\nAnd these files (by name or extension), e.g. .DS_Store, Thumbs.db, "
            "editor swap/backup files -- OS/app markers, never meaningful as \"duplicates\":\n"
            + ", ".join(sorted(DEFAULT_EXCLUDED_FILE_NAMES | DEFAULT_EXCLUDED_FILE_EXTENSIONS))
        )
        layout.addWidget(self.exclude_common_checkbox)

        scan_type_row = QHBoxLayout()
        scan_type_row.addWidget(QLabel("Only scan:"))
        for type_name in sorted(VALID_FILE_TYPES):
            checkbox = QCheckBox(type_name.capitalize())
            self._scan_type_checkboxes[type_name] = checkbox
            scan_type_row.addWidget(checkbox)
        scan_type_row.addWidget(QLabel("(leave all unchecked to scan every file)"))
        scan_type_row.addStretch()
        scan_type_row.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(scan_type_row)

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

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter results:"))
        for type_name in sorted(VALID_FILE_TYPES):
            checkbox = QCheckBox(type_name.capitalize())
            checkbox.setEnabled(False)  # enabled once a scan has results to filter -- see _on_scan_finished
            checkbox.toggled.connect(self._render_results)
            self._type_filter_checkboxes[type_name] = checkbox
            filter_row.addWidget(checkbox)
        filter_row.addStretch()
        filter_row.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(filter_row)

        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["File", "Size"])
        self.results_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.results_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
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
        if directory:
            self._add_directory_path(directory)

    def _add_directory_path(self, directory: str) -> None:
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
        # The type filter operates on _last_result -- mid-scan, the tree
        # holds an unfiltered live preview (see _on_groups_found), not a
        # finished result, so filtering doesn't apply until the next
        # _on_scan_finished re-enables these and calls _render_results.
        self._last_result = None
        for checkbox in self._type_filter_checkboxes.values():
            checkbox.setEnabled(False)

        self._scan_directories = directories
        self._scan_start = time.monotonic()
        self._resume_run_id = run_id
        # The id checkpoints are saved under for *this* attempt -- reused
        # from `run_id` when resuming a stopped run, or freshly minted for
        # a new scan so a crash partway through (not just a clean Cancel)
        # still leaves recoverable progress behind.
        self._run_id = run_id if run_id is not None else str(time.time())

        # None -> find_duplicates' own default (DEFAULT_EXCLUDED_DIR_NAMES);
        # [] -> exclusion disabled, scan everything.
        exclude_dirs = None if self.exclude_common_checkbox.isChecked() else []
        exclude_temp_files = self.exclude_common_checkbox.isChecked()
        scan_types = {name for name, cb in self._scan_type_checkboxes.items() if cb.isChecked()} or None
        self._worker = ScanWorker(
            directories,
            resume_state=resume_state,
            run_id=self._run_id,
            exclude_dirs=exclude_dirs,
            exclude_temp_files=exclude_temp_files,
            file_types=scan_types,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.group_found.connect(self._on_groups_found)
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

    def _on_groups_found(self, groups: list[DuplicateGroup]) -> None:
        """Live preview only -- appends newly-confirmed groups to the tree
        as the scan runs, so there's something to look at during a long
        scan instead of a blank tree until the very end. Not the
        authoritative final layout: whether a file is covered by a
        confirmed duplicate folder isn't known until folder analysis runs
        (after stage 3, see `find_duplicates`), so these rows are always
        rendered as plain checkable groups, never folder-covered/informational.
        `_on_scan_finished` clears the tree and rebuilds it from the
        authoritative result, which is when that distinction is applied --
        so these live rows are guaranteed to be superseded, not merged
        with, the final ones.
        """
        self.results_tree.blockSignals(True)
        self.results_tree.setUpdatesEnabled(False)
        try:
            headers = [self._build_group_item(g) for g in groups]
            self.results_tree.addTopLevelItems(headers)
            for header in headers:
                header.setExpanded(True)
        finally:
            self.results_tree.setUpdatesEnabled(True)
            self.results_tree.blockSignals(False)
        self._update_reclaimable_label()

    def _on_scan_finished(self, result: ScanResult) -> None:
        self.scan_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

        # Record history/resume state *before* populating the results tree
        # below -- that population can take a long time on a huge result
        # set (a whole-drive scan can mean tens of thousands of groups),
        # and previously ran first, so the scan's completion (and clearing
        # its resume state) never got recorded until the UI thread finished
        # that work. If the app was closed or force-killed while still
        # populating -- which, unbatched, could look indistinguishable from
        # a genuine hang -- the scan would never show as done, and any
        # earlier stopped run it consumed would incorrectly stay marked
        # resumable forever.
        duration = time.monotonic() - self._scan_start if self._scan_start is not None else 0.0
        record_run(self._scan_directories, result, duration, run_id=self._run_id)

        # Whichever stopped run this attempt just consumed (if any), and any
        # mid-scan checkpoint saved under this attempt's own id, are both
        # done with now -- either the scan finished, or it got cancelled
        # again and a fresh resume state is saved below under the same id.
        # This never touches *other* stopped runs still sitting in history,
        # so each stays independently resumable.
        for stale_id in {self._resume_run_id, self._run_id} - {None}:
            clear_resume_state(stale_id)
        self._resume_run_id = None

        if result.cancelled and result.resume_state is not None:
            save_resume_state(self._run_id, result.resume_state)
        self.resume_btn.setEnabled(latest_resume_run_id() is not None)

        self._last_result = result
        self._last_result_cancelled_note = " (cancelled -- partial results)" if result.cancelled else ""
        for checkbox in self._type_filter_checkboxes.values():
            checkbox.setEnabled(True)
        self._render_results()
        self.delete_btn.setEnabled(bool(result.groups))
        self._worker = None

    def _render_results(self) -> None:
        """(Re)builds the results tree from `self._last_result`, applying
        whatever type-filter checkboxes are currently checked (see
        `_type_filter_checkboxes`) -- called both once a scan finishes and
        again every time the filter selection changes, so switching the
        filter never requires re-scanning. `None` selected (the default,
        every checkbox unchecked) means no filter: every group shown.

        Folder and file-level groups are ranked together by how much space
        deleting all but one copy would actually reclaim (size * (copies
        - 1) for each), and only the top `TOP_RESULTS_LIMIT` are ever
        rendered -- a scan easily produces thousands of groups, which is
        neither reviewable by a person nor free to render, and the
        biggest wins are what someone freeing up space actually wants
        first. Folder-duplicate rows are still never *filtered* by type --
        a directory doesn't have a single type the way a file does -- but
        they do compete for a top-10 slot on equal footing with file
        groups, since both represent real reclaimable space.
        """
        result = self._last_result
        if result is None:
            return

        selected_types = {name for name, cb in self._type_filter_checkboxes.items() if cb.isChecked()}

        def group_matches(group: DuplicateGroup) -> bool:
            if not selected_types:
                return True
            return any(file_matches_types(p, selected_types) for p in group.paths)

        groups_to_show = [g for g in result.groups if group_matches(g)]

        # Files inside a confirmed folder match are rendered informational
        # (no checkbox) in their own file-level group below -- their fate
        # is governed entirely by that folder's checkbox instead, since the
        # two are computed independently and could otherwise disagree
        # about which copy is "kept" for the very same file.
        confirmed_folder_dirs = [d for fg in result.folder_groups if fg.confirmed for d in fg.paths]

        ranked: list[tuple[int, object]] = [
            (fg.size * (len(fg.paths) - 1), fg) for fg in result.folder_groups
        ] + [(g.size * (len(g.paths) - 1), g) for g in groups_to_show]
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        top = ranked[:TOP_RESULTS_LIMIT]
        total_reclaimable = sum(reclaimable for reclaimable, _item in ranked)

        # Building potentially tens of thousands of tree items one at a
        # time -- each triggering its own model-update and, for every
        # checkbox set, an itemChanged signal (see _on_item_changed) -- is
        # what froze the UI thread for a very long time on a whole-drive
        # scan. Block both, build every item off-tree first, and attach
        # everything in a single bulk call instead. Less of a concern now
        # that at most TOP_RESULTS_LIMIT items are ever built, but the
        # pattern costs nothing to keep.
        self.results_tree.blockSignals(True)
        self.results_tree.setUpdatesEnabled(False)
        try:
            # Discard whatever's there -- either _on_groups_found's live
            # preview rows (see there) the first time this runs after a
            # scan, or this method's own previous filtered render the next
            # time the filter selection changes.
            self.results_tree.clear()
            headers = [
                self._build_folder_group_item(item)
                if isinstance(item, FolderGroup)
                else self._build_group_item(item, confirmed_folder_dirs)
                for _reclaimable, item in top
            ]
            self.results_tree.addTopLevelItems(headers)
            for header in headers:
                header.setExpanded(True)
        finally:
            self.results_tree.setUpdatesEnabled(True)
            self.results_tree.blockSignals(False)

        skipped_note = f", {len(result.skipped)} file(s) skipped" if result.skipped else ""
        filter_note = f" ({len(groups_to_show)}/{len(result.groups)} match filter)" if selected_types else ""
        top_note = (
            f" -- showing top {len(top)} by reclaimable space" if len(ranked) > len(top) else ""
        )
        self.status_label.setText(
            f"{len(result.groups)} duplicate group(s), {len(result.folder_groups)} duplicate folder(s), "
            f"{human_size(total_reclaimable)} reclaimable{filter_note}{top_note}{skipped_note}"
            f"{self._last_result_cancelled_note}"
        )
        self._update_reclaimable_label()

    def _show_history(self) -> None:
        dialog = HistoryDialog(self)
        dialog.resume_requested.connect(self._resume_from_history)
        dialog.exec()

    # -- results tree -----------------------------------------------------

    def _build_folder_group_item(self, group: FolderGroup) -> QTreeWidgetItem:
        """A confirmed folder group is checkable, same "keep first, check
        the rest" convention as `_build_group_item` -- checking a copy and
        deleting it removes the whole directory in one action. An
        unconfirmed (large-file) group is informational only: its files
        still appear as regular, individually-deletable groups via
        `_build_group_item`, since bulk-deleting an unverified folder isn't
        safe.

        Returns the header item rather than attaching it to the tree
        itself -- building a whole subtree off-tree and adding it (along
        with every other top-level item) in one bulk call afterward is
        what keeps populating a huge result set from freezing the UI
        thread; see `_on_scan_finished`.
        """
        label = f"\U0001F4C1 Folder duplicate: {len(group.paths)} copies ({group.file_count} files, {human_size(group.size)} each)"
        if not group.confirmed:
            label += "  (unverified -- large file(s), handled at the file level instead)"
        header = QTreeWidgetItem([label, ""])
        if group.confirmed:
            header.setData(0, Qt.ItemDataRole.UserRole, group)
        else:
            header.setFlags(header.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
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
        return header

    def _build_group_item(
        self, group: DuplicateGroup, confirmed_folder_dirs: Optional[list[Path]] = None
    ) -> QTreeWidgetItem:
        """Same off-tree-build-then-bulk-attach approach as
        `_build_folder_group_item` -- see there and `_on_scan_finished`.
        """
        confirmed_folder_dirs = confirmed_folder_dirs or []
        label = f"{len(group.paths)} copies, {human_size(group.size)} each"
        if not group.confirmed:
            label += "  (unverified -- large file, checked before deletion)"
        header = QTreeWidgetItem([label, ""])
        header.setData(0, Qt.ItemDataRole.UserRole, group)

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
        return header

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
