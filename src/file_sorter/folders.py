from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from .progress import Progress

if TYPE_CHECKING:
    from .dedupe import DuplicateGroup

ProgressCallback = Callable[[str, int, Optional[int]], None]


@dataclass
class FolderGroup:
    """Two or more directories whose entire recursive contents are
    byte-for-byte duplicates of each other: the same set of relative file
    paths, each pair an exact match. `confirmed` is False if any of the
    underlying file matches was itself unconfirmed (see
    `DuplicateGroup.confirmed`) -- the same "verify before deleting"
    caution applies to the folder as a whole.
    """

    paths: list[Path]
    file_count: int
    size: int
    confirmed: bool = True


def find_duplicate_folders(
    all_files: Iterable[Path],
    skipped: Iterable[Path],
    groups: Iterable["DuplicateGroup"],
    scan_roots: Iterable[Path],
    *,
    show_progress: bool = False,
    on_progress: Optional[ProgressCallback] = None,
) -> list[FolderGroup]:
    """Find directories whose entire recursive file contents exactly match
    another directory's, built entirely on top of already-computed
    file-level duplicate groups rather than doing any extra hashing.

    A directory is only a candidate if every file under it (recursively)
    already has a match somewhere in `groups` -- a directory containing
    even one file with no duplicate anywhere in the scan can never have a
    matching sibling, since that file would need a match too. `skipped`
    (unreadable files) disqualify their directory the same way: its true
    contents can't be verified. This means folder detection costs nothing
    extra in I/O or hashing: it's pure bookkeeping over results the
    file-level scan already produced.

    Nested duplicates are collapsed: if two directories match, matching
    subdirectories under them aren't reported separately, since that's
    already implied by the parent match.

    This is pure in-memory bookkeeping, not I/O, but for a scan root with
    hundreds of thousands of files (e.g. many `node_modules`/`.venv` trees
    under a general-purpose "projects" folder) processing every directory
    still takes long enough to be noticeable -- and previously did so with
    zero feedback after the file-level stages hit 100%, making a scan that
    was still genuinely working look identical to a hung one. `on_progress`
    (throttled the same way as the hashing stages, see `Progress`) reports
    "Analyzing folders" progress through the same mechanism so the GUI's
    progress bar keeps moving instead of sitting frozen at the prior
    stage's final count.
    """
    content_id: dict[Path, tuple[str, bool, int]] = {
        path: (group.file_hash, group.confirmed, group.size) for group in groups for path in group.paths
    }

    dir_files: dict[Path, list[Path]] = defaultdict(list)
    dir_subdirs: dict[Path, set[Path]] = defaultdict(set)
    known_dirs: set[Path] = set()
    roots = set(scan_roots)

    def link(start: Path) -> None:
        current = start
        while current not in known_dirs:
            known_dirs.add(current)
            if current in roots:
                return
            parent = current.parent
            if parent == current:  # reached the filesystem root
                return
            dir_subdirs[parent].add(current)
            current = parent

    for path in all_files:
        parent = path.parent
        dir_files[parent].append(path)
        link(parent)
    for path in skipped:
        parent = path.parent
        dir_files[parent].append(path)
        link(parent)

    # Deepest directories first, so a directory's subdirectories are always
    # already resolved (signature computed, or disqualified) by the time
    # the directory itself is processed.
    ordered = sorted(known_dirs, key=lambda p: len(p.parts), reverse=True)

    signature: dict[Path, Optional[str]] = {}
    confirmed_by_dir: dict[Path, bool] = {}
    size_by_dir: dict[Path, int] = {}
    count_by_dir: dict[Path, int] = {}

    progress = Progress(
        "Analyzing folders", total=len(ordered), enabled=show_progress, on_progress=on_progress
    )

    for d in ordered:
        progress.update()
        entries: list[tuple[str, str, str]] = []
        disqualified = False
        confirmed = True
        total_size = 0
        total_count = 0

        for f in sorted(dir_files.get(d, ()), key=lambda p: p.name):
            entry = content_id.get(f)
            if entry is None:
                disqualified = True
                break
            file_hash, file_confirmed, file_size = entry
            entries.append(("F", f.name, file_hash))
            confirmed = confirmed and file_confirmed
            total_size += file_size
            total_count += 1

        if not disqualified:
            for sub in sorted(dir_subdirs.get(d, ()), key=lambda p: p.name):
                sub_sig = signature.get(sub)
                if sub_sig is None:
                    disqualified = True
                    break
                entries.append(("D", sub.name, sub_sig))
                confirmed = confirmed and confirmed_by_dir[sub]
                total_size += size_by_dir[sub]
                total_count += count_by_dir[sub]

        if disqualified or total_count == 0:
            signature[d] = None
            continue

        signature[d] = hashlib.sha256(repr(entries).encode()).hexdigest()
        confirmed_by_dir[d] = confirmed
        size_by_dir[d] = total_size
        count_by_dir[d] = total_count

    progress.close()

    sig_groups: dict[str, list[Path]] = defaultdict(list)
    for d, sig in signature.items():
        if sig is not None:
            sig_groups[sig].append(d)

    # Shallowest (i.e. largest/outermost) matches first, so once a pair of
    # directories is reported, their matching subdirectories can be skipped.
    candidates = sorted((dirs for dirs in sig_groups.values() if len(dirs) >= 2), key=lambda dirs: len(dirs[0].parts))

    covered: set[Path] = set()

    def mark_covered(start: Path) -> None:
        stack = [start]
        while stack:
            current = stack.pop()
            if current in covered:
                continue
            covered.add(current)
            stack.extend(dir_subdirs.get(current, ()))

    result: list[FolderGroup] = []
    for dirs in candidates:
        if any(d in covered for d in dirs):
            continue
        result.append(
            FolderGroup(
                paths=sorted(dirs),
                file_count=count_by_dir[dirs[0]],
                size=size_by_dir[dirs[0]],
                confirmed=all(confirmed_by_dir[d] for d in dirs),
            )
        )
        for d in dirs:
            mark_covered(d)

    result.sort(key=lambda g: g.size * len(g.paths), reverse=True)
    return result
