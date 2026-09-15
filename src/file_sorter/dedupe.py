from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .progress import Progress

PARTIAL_CHUNK_SIZE = 8192
FULL_READ_CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger(__name__)


def iter_files(directories: Iterable[Path]) -> Iterator[Path]:
    for directory in directories:
        try:
            entries = directory.rglob("*")
        except OSError:
            continue
        while True:
            try:
                path = next(entries)
            except StopIteration:
                break
            except OSError:
                continue
            if path.is_symlink():
                continue
            try:
                if path.is_file():
                    yield path
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


def find_duplicates(directories: Iterable[Path], *, show_progress: bool = False) -> ScanResult:
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
    progress.close()

    groups = [
        DuplicateGroup(file_hash=file_hash, size=paths[0].stat().st_size, paths=paths)
        for file_hash, paths in by_full.items()
        if len(paths) > 1
    ]
    groups.sort(key=lambda g: g.size * len(g.paths), reverse=True)
    logger.info("Done: %d duplicate group(s), %d file(s) skipped", len(groups), len(skipped))
    return ScanResult(groups=groups, skipped=skipped)
