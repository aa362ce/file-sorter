from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

ProgressCallback = Callable[[str, int, Optional[int]], None]

from .progress import Progress

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


def find_duplicates(
    directories: Iterable[Path],
    *,
    show_progress: bool = False,
    on_progress: Optional[ProgressCallback] = None,
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
    """
    skipped: list[Path] = []

    logger.info("Stage 1/3: scanning directories")
    by_size: dict[int, list[Path]] = defaultdict(list)
    progress = Progress("Scanning", enabled=show_progress)
    for path in iter_files(directories):
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

    partial_candidates = [p for p in by_size.values() if len(p) >= 2]
    partial_total = sum(len(p) for p in partial_candidates)
    logger.info("Stage 2/3: quick-hashing %d candidate file(s)", partial_total)
    by_partial: dict[tuple[int, str], list[Path]] = defaultdict(list)
    progress = Progress("Quick hash", total=partial_total, enabled=show_progress)
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                partial = _partial_hash(path)
            except OSError as exc:
                logger.debug("Skipping unreadable file %s: %s", path, exc)
                skipped.append(path)
                continue
            by_partial[(size, partial)].append(path)
            progress.update()
            if on_progress:
                on_progress("Quick hash", progress.count, partial_total)
    progress.close()

    full_candidates = [p for p in by_partial.values() if len(p) >= 2]
    full_total = sum(len(p) for p in full_candidates)
    logger.info("Stage 3/3: full-hashing %d candidate file(s)", full_total)
    by_full: dict[str, list[Path]] = defaultdict(list)
    progress = Progress("Full hash", total=full_total, enabled=show_progress)
    for paths in by_partial.values():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                full = _full_hash(path)
            except OSError as exc:
                logger.debug("Skipping unreadable file %s: %s", path, exc)
                skipped.append(path)
                continue
            by_full[full].append(path)
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
    logger.info("Done: %d duplicate group(s), %d file(s) skipped", len(groups), len(skipped))
    return ScanResult(groups=groups, skipped=skipped)
