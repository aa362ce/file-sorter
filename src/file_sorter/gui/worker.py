from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal
from send2trash import send2trash

from ..dedupe import DuplicateGroup, ScanResult, files_equal, find_duplicates, scan_or_reuse
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


def _move_into(path: Path, dest_dir: Path) -> None:
    """Moves `path` (a file, or a whole duplicate-folder directory) into
    `dest_dir`, appending " (1)", " (2)", etc. to the name if something's
    already there -- duplicate copies pulled from different source
    directories can easily share a filename, and a plain move would
    otherwise silently overwrite whatever's already at the destination.
    """
    target = dest_dir / path.name
    counter = 1
    while target.exists():
        target = dest_dir / f"{path.stem} ({counter}){path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))


@dataclass
class DeleteOperation:
    """One planned removal of a single file or folder -- either a
    move-to-Trash or, when `dest_dir` is set, a move into that folder
    instead -- prepared by the GUI thread (where the tree/checkbox state
    and the safety checks that depend on it live) and then handed to
    `DeleteWorker` to actually carry out off the UI thread. `item` is only
    touched again once this comes back on the GUI thread via
    `DeleteOutcome` -- the worker itself never reads or writes it, so
    holding a `QTreeWidgetItem` reference here is safe despite running on
    a worker thread.
    """

    kind: str  # "folder" | "file"
    path: Path
    item: object = None
    group: Optional[DuplicateGroup] = None
    # Reference copy/copies to verify an unconfirmed group's file against
    # right before deleting it (see DuplicateGroup.confirmed) -- empty
    # when group is None or already confirmed.
    kept_refs: list[Path] = field(default_factory=list)
    # Precomputed reason this item must NOT be removed (e.g. it would be
    # the last surviving copy) -- set, skip straight to a failure outcome.
    skip_reason: Optional[str] = None
    # Already accounted for by another operation in this same batch (e.g.
    # a file sitting under a folder that's also being removed) -- no
    # actual Trash/move call is made, but it still counts as a successful
    # outcome so the caller cleans up bookkeeping (tree item / surviving
    # list) the same way as an actually-removed item.
    already_handled: bool = False
    # None -> move to Trash (the default). Set -> move into this folder
    # instead, via `_move_into` -- see `_move_checked`/`_move_all_duplicates`.
    dest_dir: Optional[Path] = None


@dataclass
class DeleteOutcome:
    op: DeleteOperation
    ok: bool
    message: Optional[str] = None


class DeleteWorker(QThread):
    """Carries out a batch of `DeleteOperation`s (Trash moves) off the UI
    thread, same reasoning as `ScanWorker`: send2trash and the
    byte-for-byte verification of an unconfirmed group can each be slow
    enough, over many items, to freeze the window if run inline with a
    button click.
    """

    progress = Signal(int, int)  # done, total
    finished_delete = Signal(list)  # list[DeleteOutcome]

    def __init__(self, operations: list[DeleteOperation], parent=None) -> None:
        super().__init__(parent)
        self._operations = operations

    def run(self) -> None:
        outcomes: list[DeleteOutcome] = []
        total = len(self._operations)
        for index, op in enumerate(self._operations, start=1):
            outcomes.append(self._execute(op))
            self.progress.emit(index, total)
        self.finished_delete.emit(outcomes)

    def _execute(self, op: DeleteOperation) -> DeleteOutcome:
        if op.already_handled:
            return DeleteOutcome(op, True)
        if op.skip_reason is not None:
            return DeleteOutcome(op, False, f"{op.path}: {op.skip_reason}")

        if op.group is not None and not op.group.confirmed:
            # Confirmation of this group was deferred during the scan (a
            # very large file) -- do it now, against whichever file(s) in
            # the group are being kept, before actually removing anything.
            verified = False
            for reference in op.kept_refs:
                try:
                    if files_equal(reference, op.path):
                        verified = True
                        break
                except OSError:
                    continue
            if not verified:
                verb = "moving" if op.dest_dir is not None else "deleting"
                return DeleteOutcome(
                    op,
                    False,
                    f"{op.path}: not verified as an actual duplicate of the kept file(s) -- "
                    f"skipped rather than risk {verb} a non-duplicate",
                )

        try:
            if op.dest_dir is not None:
                _move_into(op.path, op.dest_dir)
            else:
                send2trash(str(op.path))
        except OSError as exc:
            return DeleteOutcome(op, False, f"{op.path}: {exc}")
        return DeleteOutcome(op, True)
