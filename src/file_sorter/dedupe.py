from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

PARTIAL_CHUNK_SIZE = 8192
FULL_READ_CHUNK_SIZE = 1024 * 1024


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


def find_duplicates(directories: Iterable[Path]) -> ScanResult:
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

    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in iter_files(directories):
        try:
            size = path.stat().st_size
        except OSError:
            skipped.append(path)
            continue
        by_size[size].append(path)

    by_partial: dict[tuple[int, str], list[Path]] = defaultdict(list)
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                partial = _partial_hash(path)
            except OSError:
                skipped.append(path)
                continue
            by_partial[(size, partial)].append(path)

    by_full: dict[str, list[Path]] = defaultdict(list)
    for paths in by_partial.values():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                full = _full_hash(path)
            except OSError:
                skipped.append(path)
                continue
            by_full[full].append(path)

    groups = [
        DuplicateGroup(file_hash=file_hash, size=paths[0].stat().st_size, paths=paths)
        for file_hash, paths in by_full.items()
        if len(paths) > 1
    ]
    groups.sort(key=lambda g: g.size * len(g.paths), reverse=True)
    return ScanResult(groups=groups, skipped=skipped)
