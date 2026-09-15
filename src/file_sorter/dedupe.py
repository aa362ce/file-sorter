from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .progress import Progress
from .resume import ResumeState

ProgressCallback = Callable[[str, int, Optional[int]], None]

PARTIAL_CHUNK_SIZE = 8192
FULL_READ_CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger(__name__)


def iter_files(directories: Iterable[Path]) -> Iterator[Path]:
    """Recursively yield every file under `directories`, following directory
    symlinks (e.g. an iCloud/Dropbox-synced folder, or a dotfiles symlink)
    but never re-entering a real directory already visited -- which both
    prevents symlink-cycle hangs and avoids the same file being reported
    twice through two different symlinked paths.

    File symlinks are skipped: a symlink to a file elsewhere isn't a real
    duplicate on disk, it's the same file, so counting it as a "copy" would
    be misleading.
    """
    visited: set[tuple[int, int]] = set()
    for directory in directories:
        yield from _walk(directory, visited)


def _walk(directory: Path, visited: set[tuple[int, int]]) -> Iterator[Path]:
    try:
        st = os.stat(directory)
        key = (st.st_dev, st.st_ino)
        if key in visited:
            return
        visited.add(key)
        entries = list(os.scandir(directory))
    except OSError:
        return

    for entry in entries:
        try:
            is_symlink = entry.is_symlink()
            if is_symlink and not entry.is_dir(follow_symlinks=True):
                continue
            if entry.is_dir(follow_symlinks=True):
                yield from _walk(Path(entry.path), visited)
            elif not is_symlink and entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
        except OSError:
            continue


def _partial_hash(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.sha256(f.read(PARTIAL_CHUNK_SIZE)).hexdigest()


def _full_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(FULL_READ_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class DuplicateGroup:
    file_hash: str
    size: int
    paths: list[Path] = field(default_factory=list)


@dataclass
class ScanResult:
    groups: list[DuplicateGroup]
    skipped: list[Path]
    cancelled: bool = False
    resume_state: Optional[ResumeState] = None


def _partial_key(size: int, partial_hash: str) -> str:
    return f"{size}:{partial_hash}"


def find_duplicates(
    directories: Iterable[Path],
    *,
    show_progress: bool = False,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    resume_state: Optional[ResumeState] = None,
) -> ScanResult:
    """Find duplicate files across directories using a staged lookup table.

    Each stage is a dict keyed by an increasingly expensive signature, and
    only buckets with 2+ files carry forward -- most files drop out after
    the free `stat()` call, so full-file hashing only happens for files
    that already share a size and a few KB of leading bytes.

    Stage 1 (size):          dict[int, list[Path]]
    Stage 2 (size + partial): dict[(int, str), list[Path]]
    Stage 3 (full hash):      dict[str, list[Path]]

    Files that can't be read (permission-protected, removed mid-scan, ...)
    are skipped rather than aborting the whole scan.

    `on_progress(stage_label, count, total)` is called after every file if
    given -- `total` is None for stage 1 (unknown until the walk finishes).
    This is how the GUI drives its progress bar without depending on the
    terminal-oriented `Progress` class.

    `cancel_event`, if given, is checked between files; when set, the
    current stage stops early. A group only counts as a confirmed
    duplicate once its full hash is computed, so cancelling during stage 1
    or 2 yields zero groups (nothing confirmed yet) -- cancelling during
    stage 3 still returns whichever groups were already confirmed. Either
    way `ScanResult.cancelled` is set so callers can report it, and
    `ScanResult.resume_state` carries whatever partial progress was made so
    a later call can pass it back in as `resume_state` to pick up where
    this one left off instead of redoing already-hashed files.

    `resume_state`, if given, must describe a scan that was cancelled
    partway through stage 2 or 3 (`stage` "quick_hash" or "full_hash") --
    the results already computed for the interrupted stage and any earlier
    stage are reused, and only the files not yet processed at cancellation
    time are (re)hashed. A `resume_state` with `stage` "scanning" (cancelled
    during the directory walk, which has nothing worth resuming) is
    equivalent to passing none.
    """
    directories = list(directories)
    skipped: list[Path] = []
    cancelled = False
    cancelled_stage: Optional[str] = None

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    resume_stage = resume_state.stage if resume_state is not None else None

    by_size: dict[int, list[Path]] = defaultdict(list)
    if resume_stage in ("quick_hash", "full_hash"):
        assert resume_state is not None
        for size_str, paths in resume_state.by_size.items():
            by_size[int(size_str)] = [Path(p) for p in paths]
        skipped.extend(Path(p) for p in resume_state.skipped)
        logger.info("Resuming: stage 1/3 already done (%d distinct sizes)", len(by_size))
    else:
        logger.info("Stage 1/3: scanning directories")
        progress = Progress("Scanning", enabled=show_progress)
        for path in iter_files(directories):
            if is_cancelled():
                cancelled = True
                cancelled_stage = "scanning"
                break
            try:
                size = path.stat().st_size
            except OSError as exc:
                logger.debug("Skipping unreadable file %s: %s", path, exc)
                skipped.append(path)
                continue
            by_size[size].append(path)
            progress.update()
            if on_progress:
                on_progress("Scanning", progress.count, None)
        progress.close()
        logger.info("Stage 1/3 done: %d files, %d distinct sizes", progress.count, len(by_size))

    by_partial: dict[tuple[int, str], list[Path]] = defaultdict(list)
    processed_stage2: list[Path] = []
    if not cancelled:
        if resume_stage == "full_hash":
            assert resume_state is not None
            for key, paths in resume_state.by_partial.items():
                size_str, hash_str = key.split(":", 1)
                by_partial[(int(size_str), hash_str)] = [Path(p) for p in paths]
            partial_candidates: list[tuple[int, Path]] = []
            logger.info("Resuming: stage 2/3 already done")
        else:
            already_processed: set[Path] = set()
            if resume_stage == "quick_hash":
                assert resume_state is not None
                for key, paths in resume_state.by_partial.items():
                    size_str, hash_str = key.split(":", 1)
                    by_partial[(int(size_str), hash_str)] = [Path(p) for p in paths]
                already_processed = {Path(p) for p in resume_state.processed}

            partial_candidates = [
                (size, path)
                for size, paths in by_size.items()
                if len(paths) >= 2
                for path in paths
                if path not in already_processed
            ]
            if resume_stage == "quick_hash":
                logger.info("Resuming stage 2/3: %d candidate file(s) remaining", len(partial_candidates))
            else:
                logger.info("Stage 2/3: quick-hashing %d candidate file(s)", len(partial_candidates))

        partial_total = len(partial_candidates)
        progress = Progress("Quick hash", total=partial_total, enabled=show_progress)
        for size, path in partial_candidates:
            if is_cancelled():
                cancelled = True
                cancelled_stage = "quick_hash"
                break
            try:
                partial = _partial_hash(path)
            except OSError as exc:
                logger.debug("Skipping unreadable file %s: %s", path, exc)
                skipped.append(path)
                processed_stage2.append(path)
                continue
            by_partial[(size, partial)].append(path)
            processed_stage2.append(path)
            progress.update()
            if on_progress:
                on_progress("Quick hash", progress.count, partial_total)
        progress.close()

    by_full: dict[str, list[Path]] = defaultdict(list)
    processed_stage3: list[Path] = []
    if not cancelled:
        already_processed = set()
        if resume_stage == "full_hash":
            assert resume_state is not None
            for full_hash, paths in resume_state.by_full.items():
                by_full[full_hash] = [Path(p) for p in paths]
            already_processed = {Path(p) for p in resume_state.processed}

        full_candidates = [
            path
            for paths in by_partial.values()
            if len(paths) >= 2
            for path in paths
            if path not in already_processed
        ]
        full_total = len(full_candidates)
        if resume_stage == "full_hash":
            logger.info("Resuming stage 3/3: %d candidate file(s) remaining", full_total)
        else:
            logger.info("Stage 3/3: full-hashing %d candidate file(s)", full_total)
        progress = Progress("Full hash", total=full_total, enabled=show_progress)
        for path in full_candidates:
            if is_cancelled():
                cancelled = True
                cancelled_stage = "full_hash"
                break
            try:
                full = _full_hash(path)
            except OSError as exc:
                logger.debug("Skipping unreadable file %s: %s", path, exc)
                skipped.append(path)
                processed_stage3.append(path)
                continue
            by_full[full].append(path)
            processed_stage3.append(path)
            progress.update()
            if on_progress:
                on_progress("Full hash", progress.count, full_total)
        progress.close()

    groups = [
        DuplicateGroup(file_hash=file_hash, size=paths[0].stat().st_size, paths=paths)
        for file_hash, paths in by_full.items()
        if len(paths) > 1
    ]
    groups.sort(key=lambda g: g.size * len(g.paths), reverse=True)

    new_resume_state: Optional[ResumeState] = None
    if cancelled:
        logger.info("Cancelled: %d duplicate group(s) confirmed so far, %d file(s) skipped", len(groups), len(skipped))
        if cancelled_stage in ("quick_hash", "full_hash"):
            new_resume_state = ResumeState(
                directories=[str(d) for d in directories],
                stage=cancelled_stage,
                by_size={str(size): [str(p) for p in paths] for size, paths in by_size.items()},
                by_partial={_partial_key(*key): [str(p) for p in paths] for key, paths in by_partial.items()},
                by_full={h: [str(p) for p in paths] for h, paths in by_full.items()},
                processed=[str(p) for p in (processed_stage2 if cancelled_stage == "quick_hash" else processed_stage3)],
                skipped=[str(p) for p in skipped],
            )
        else:
            new_resume_state = ResumeState(
                directories=[str(d) for d in directories],
                stage="scanning",
                skipped=[str(p) for p in skipped],
            )
    else:
        logger.info("Done: %d duplicate group(s), %d file(s) skipped", len(groups), len(skipped))

    return ScanResult(groups=groups, skipped=skipped, cancelled=cancelled, resume_state=new_resume_state)
