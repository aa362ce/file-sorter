from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, TypeVar

from .progress import Progress
from .resume import ResumeState

ProgressCallback = Callable[[str, int, Optional[int]], None]

PARTIAL_CHUNK_SIZE = 8192
FULL_READ_CHUNK_SIZE = 1024 * 1024

T = TypeVar("T")
K = TypeVar("K")

logger = logging.getLogger(__name__)

_HAS_FILE_DIGEST = hasattr(hashlib, "file_digest")  # Python 3.11+


def default_workers() -> int:
    """Worker count used when the caller doesn't request a specific one."""
    return os.cpu_count() or 1


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
    with path.open("rb") as f:
        if _HAS_FILE_DIGEST:
            # C-level loop (readinto + incremental update) -- avoids the
            # per-chunk bytes allocation and Python loop overhead below.
            return hashlib.file_digest(f, "sha256").hexdigest()
        digest = hashlib.sha256()
        buf = bytearray(FULL_READ_CHUNK_SIZE)
        view = memoryview(buf)
        while True:
            n = f.readinto(buf)
            if not n:
                break
            digest.update(view[:n])
    return digest.hexdigest()


def _files_equal(path_a: Path, path_b: Path) -> bool:
    """True if two files -- already known to be the same size -- are
    byte-for-byte identical, comparing chunk by chunk with an early exit at
    the first difference.

    This is how tools like jdupes/rmlint confirm a match instead of hashing
    both files fully and comparing digests: two files that differ early
    (a common case for files that coincidentally share a size and a partial
    hash, e.g. same header/format but different content) are ruled out
    after reading only as far as the difference, rather than always reading
    every byte of both.
    """
    with path_a.open("rb") as fa, path_b.open("rb") as fb:
        while True:
            chunk_a = fa.read(FULL_READ_CHUNK_SIZE)
            chunk_b = fb.read(FULL_READ_CHUNK_SIZE)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def _group_by_content(
    buckets: list[list[Path]],
    *,
    workers: int,
    stage_label: str,
    show_progress: bool,
    on_progress: Optional[ProgressCallback],
    is_cancelled: Callable[[], bool],
) -> tuple[dict[str, list[Path]], list[Path], list[Path], bool]:
    """Confirm which files within each bucket (files that already share a
    size and a partial hash from earlier stages) are true duplicates, by
    comparing their content directly instead of hashing every one of them.

    Within a bucket, one file is picked as the "representative" and every
    other file in the bucket is compared against it (concurrently, across a
    thread pool of `workers` threads); matches join its group, non-matches
    are set aside and the same process repeats among them (picking a new
    representative) until none remain -- so a bucket with more than one
    distinct file (a rare partial-hash coincidence) still ends up correctly
    split into separate groups. A confirmed group's identifying hash is
    computed from its representative only, once -- files that turn out not
    to match anything are never hashed at all.
    """
    result: dict[str, list[Path]] = {}
    processed: list[Path] = []
    skipped: list[Path] = []
    cancelled = False
    total = sum(len(bucket) for bucket in buckets)
    progress = Progress(stage_label, total=total, enabled=show_progress)

    def mark_done(path: Path) -> None:
        processed.append(path)
        progress.update()
        if on_progress:
            on_progress(stage_label, progress.count, total)

    def compare(representative: Path, other: Path) -> tuple[Path, Optional[bool], Optional[OSError]]:
        try:
            return other, _files_equal(representative, other), None
        except OSError as exc:
            return other, None, exc

    def record_group(representative: Path, matched_others: list[Path]) -> None:
        if not matched_others:
            return
        digest = _full_hash(representative)
        result.setdefault(digest, []).extend([representative] + matched_others)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        for bucket in buckets:
            if cancelled:
                break
            remaining = list(bucket)
            while len(remaining) >= 2:
                if is_cancelled():
                    cancelled = True
                    break
                representative, *rest = remaining
                try:
                    with representative.open("rb"):
                        pass
                except OSError as exc:
                    logger.debug("Skipping unreadable file %s: %s", representative, exc)
                    skipped.append(representative)
                    mark_done(representative)
                    remaining = rest
                    continue

                mark_done(representative)
                leftover: list[Path] = []
                matched_others: list[Path] = []
                comparisons = (
                    pool.map(lambda p: compare(representative, p), rest)
                    if pool is not None
                    else (compare(representative, p) for p in rest)
                )
                for other, is_equal, err in comparisons:
                    if err is not None:
                        logger.debug("Skipping unreadable file %s: %s", other, err)
                        skipped.append(other)
                    elif is_equal:
                        matched_others.append(other)
                    else:
                        leftover.append(other)
                    mark_done(other)
                record_group(representative, matched_others)
                remaining = leftover
            if not cancelled and len(remaining) == 1:
                mark_done(remaining[0])
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    progress.close()
    return result, processed, skipped, cancelled


def _hash_parallel(
    items: list[T],
    *,
    path_of: Callable[[T], Path],
    hash_fn: Callable[[Path], str],
    key_of: Callable[[T, str], K],
    workers: int,
    stage_label: str,
    show_progress: bool,
    on_progress: Optional[ProgressCallback],
    is_cancelled: Callable[[], bool],
) -> tuple[dict[K, list[Path]], list[Path], list[Path], bool]:
    """Hash `items` into buckets keyed by `key_of`, spreading the reads and
    hashing across a thread pool of `workers` threads.

    Hashing is a mix of file I/O (which releases the GIL while waiting on
    the OS) and CPU work in hashlib's C implementation (which also releases
    the GIL for each chunk), so multiple files can genuinely be read and
    hashed at the same time instead of one at a time -- `workers` should
    typically track the number of CPU cores available.

    Cancellation is checked once per batch of `workers` files rather than
    between every single file, since a whole batch is already in flight
    together by the time it could be checked; whatever a batch finishes is
    kept before stopping.
    """
    result: dict[K, list[Path]] = defaultdict(list)
    processed: list[Path] = []
    skipped: list[Path] = []
    cancelled = False
    total = len(items)
    progress = Progress(stage_label, total=total, enabled=show_progress)

    def record(item: T, digest: Optional[str], error: Optional[OSError]) -> None:
        path = path_of(item)
        if error is not None:
            logger.debug("Skipping unreadable file %s: %s", path, error)
            skipped.append(path)
            processed.append(path)
            return
        assert digest is not None
        result[key_of(item, digest)].append(path)
        processed.append(path)
        progress.update()
        if on_progress:
            on_progress(stage_label, progress.count, total)

    if workers <= 1 or total <= 1:
        for item in items:
            if is_cancelled():
                cancelled = True
                break
            try:
                record(item, hash_fn(path_of(item)), None)
            except OSError as exc:
                record(item, None, exc)
        progress.close()
        return result, processed, skipped, cancelled

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        index = 0
        while index < total:
            if is_cancelled():
                cancelled = True
                break
            batch = items[index : index + workers]
            futures = [(item, pool.submit(hash_fn, path_of(item))) for item in batch]
            for item, future in futures:
                try:
                    record(item, future.result(), None)
                except OSError as exc:
                    record(item, None, exc)
            index += len(batch)
    progress.close()
    return result, processed, skipped, cancelled


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
    workers: Optional[int] = None,
) -> ScanResult:
    """Find duplicate files across directories using a staged lookup table.

    Each stage is a dict keyed by an increasingly expensive signature, and
    only buckets with 2+ files carry forward -- most files drop out after
    the free `stat()` call, so stage 3 only has to look at files that
    already share a size and a few KB of leading bytes.

    Stage 1 (size):          dict[int, list[Path]]
    Stage 2 (size + partial): dict[(int, str), list[Path]]
    Stage 3 (confirm):        dict[str, list[Path]]

    Stage 3 confirms matches by comparing file content directly (see
    `_group_by_content`) rather than hashing every remaining candidate and
    comparing digests -- a pair that differs early is ruled out as soon as
    the difference is found, and a file with no match is never hashed at
    all. Each confirmed group's dict key is still a SHA-256 hash, computed
    once from one file in the group (its "representative"), so the result
    shape and the CLI/GUI's use of it as a display digest are unchanged.

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
    stage are reused. For stage 2, only the files not yet processed at
    cancellation time are (re)hashed. For stage 3, whole buckets (groups of
    files sharing a size and partial hash) that were already fully
    confirmed are skipped, but a bucket that was only partly resolved when
    cancelled is redone from scratch rather than resumed file-by-file --
    representative-based comparison doesn't carry over mid-bucket the way
    independent per-file hashing did, and buckets are small enough that
    this costs little. A `resume_state` with `stage` "scanning" (cancelled
    during the directory walk, which has nothing worth resuming) is
    equivalent to passing none.

    `workers` controls how many files stages 2 and 3 (the actual hashing)
    process concurrently via a thread pool, to take advantage of multiple
    CPU cores/threads instead of hashing one file at a time. Defaults to
    `os.cpu_count()`; pass 1 to hash sequentially.
    """
    directories = list(directories)
    skipped: list[Path] = []
    cancelled = False
    cancelled_stage: Optional[str] = None
    workers = workers if workers and workers > 0 else default_workers()

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

        hashed, processed_stage2, newly_skipped, stage_cancelled = _hash_parallel(
            partial_candidates,
            path_of=lambda item: item[1],
            hash_fn=_partial_hash,
            key_of=lambda item, digest: (item[0], digest),
            workers=workers,
            stage_label="Quick hash",
            show_progress=show_progress,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )
        for key, paths in hashed.items():
            by_partial[key].extend(paths)
        skipped.extend(newly_skipped)
        if stage_cancelled:
            cancelled = True
            cancelled_stage = "quick_hash"

    by_full: dict[str, list[Path]] = defaultdict(list)
    processed_stage3: list[Path] = []
    if not cancelled:
        if resume_stage == "full_hash":
            assert resume_state is not None
            for full_hash, paths in resume_state.by_full.items():
                by_full[full_hash] = [Path(p) for p in paths]
            # A bucket is only skippable if every one of its members is
            # already accounted for in a confirmed group; a bucket that was
            # only partly resolved when the scan was cancelled is redone
            # from scratch (representative comparisons aren't resumable at
            # file granularity the way independent hashing was), so drop
            # whatever partial group it had contributed to avoid double
            # counting those members once it's redone below.
            resolved_paths = {Path(p) for group in resume_state.by_full.values() for p in group}
            buckets = []
            for paths in by_partial.values():
                if len(paths) < 2:
                    continue
                bucket_set = set(paths)
                if bucket_set <= resolved_paths:
                    continue
                for key in [k for k, v in by_full.items() if bucket_set & set(v)]:
                    del by_full[key]
                buckets.append(paths)
        else:
            buckets = [paths for paths in by_partial.values() if len(paths) >= 2]

        full_total = sum(len(b) for b in buckets)
        if resume_stage == "full_hash":
            logger.info("Resuming stage 3/3: %d candidate file(s) remaining", full_total)
        else:
            logger.info("Stage 3/3: confirming %d candidate file(s) by content", full_total)
        hashed, processed_stage3, newly_skipped, stage_cancelled = _group_by_content(
            buckets,
            workers=workers,
            stage_label="Confirm duplicates",
            show_progress=show_progress,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
        )
        for key, paths in hashed.items():
            by_full[key].extend(paths)
        skipped.extend(newly_skipped)
        if stage_cancelled:
            cancelled = True
            cancelled_stage = "full_hash"

    # Every path in by_full first passed through by_partial, keyed by
    # (size, partial_hash) -- reuse that already-known size instead of an
    # extra stat() per group (which could also raise if the file has since
    # been removed).
    size_by_path: dict[Path, int] = {
        path: size for (size, _partial_digest), paths in by_partial.items() for path in paths
    }
    groups = [
        DuplicateGroup(file_hash=file_hash, size=size_by_path[paths[0]], paths=paths)
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
