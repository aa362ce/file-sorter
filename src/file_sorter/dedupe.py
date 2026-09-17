from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Optional, TypeVar

from .progress import Progress
from .store import CheckpointDelta, ResumeState

if TYPE_CHECKING:
    from .folders import FolderGroup

ProgressCallback = Callable[[str, int, Optional[int]], None]
CheckpointCallback = Callable[[CheckpointDelta], None]

PARTIAL_CHUNK_SIZE = 8192
FULL_READ_CHUNK_SIZE = 1024 * 1024
LARGE_FILE_THRESHOLD = 500 * 1024 * 1024  # 500MB

# How many files a hash/confirm stage processes between checkpoint saves
# (see `find_duplicates`'s `on_checkpoint`) -- frequent enough that a
# crash or power loss partway through hashing a huge drive loses at most
# this many files' worth of work, infrequent enough that the checkpoint
# write itself (a single-row SQLite UPSERT, see `store.save_resume_state`)
# never becomes the bottleneck.
CHECKPOINT_INTERVAL = 2000

T = TypeVar("T")
K = TypeVar("K")

logger = logging.getLogger(__name__)

_HAS_FILE_DIGEST = hasattr(hashlib, "file_digest")  # Python 3.11+


def default_workers() -> int:
    """Worker count used when the caller doesn't request a specific one."""
    return os.cpu_count() or 1


def _dir_key(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}"


def _walk_checkpointed(
    directories: Iterable[Path],
    *,
    completed_dirs: set[str],
    already_seen: set[Path],
) -> Iterator[tuple[str, object]]:
    """Recursively yield every file under `directories`, resumable at
    directory granularity -- used for stage 1's own checkpointing (see
    `find_duplicates`'s `on_checkpoint`).

    Follows directory symlinks (e.g. an iCloud/Dropbox-synced folder, or a
    dotfiles symlink) but never re-enters a real directory already
    visited, which both prevents symlink-cycle hangs and avoids the same
    file being reported twice through two different symlinked paths. File
    symlinks are skipped: a symlink to a file elsewhere isn't a real
    duplicate on disk, it's the same file, so counting it as a "copy"
    would be misleading.

    Explicit-stack DFS rather than recursion, so a directory's completion
    can be observed as an event (yielded once every entry in it -- files
    and, recursively, subdirectories -- has been fully processed) instead
    of only implicitly by a generator frame returning.

    Yields:
    - `("file", Path)` for each file found, except one already in
      `already_seen` (paths already recorded from a checkpoint before a
      crash) -- skipping those is what keeps a resumed walk from
      double-counting a directory that had only partly been flushed.
    - `("dir_done", key)` once a directory -- identified by `key`, a
      "dev:ino" string stable across a resume even if the path is a
      symlink -- and everything under it is fully processed.

    Any directory whose key is already in `completed_dirs` (persisted by
    an earlier checkpoint) is skipped entirely, without being scanned
    again -- the cost of resuming is bounded by what's still pending, not
    by however much of the tree is already done.
    """
    visited: set[tuple[int, int]] = set()
    stack: list[tuple[Iterator[os.DirEntry], str]] = []

    def try_push(directory: Path, st: os.stat_result) -> None:
        ino_key = (st.st_dev, st.st_ino)
        if ino_key in visited:
            return
        visited.add(ino_key)
        key = _dir_key(st)
        if key in completed_dirs:
            return
        try:
            entries = iter(list(os.scandir(directory)))
        except OSError:
            return
        stack.append((entries, key))

    for root in directories:
        try:
            st = os.stat(root)
        except OSError:
            continue
        try_push(root, st)

    while stack:
        entries, key = stack[-1]
        entry = next(entries, None)
        if entry is None:
            stack.pop()
            yield ("dir_done", key)
            continue
        try:
            is_symlink = entry.is_symlink()
            if is_symlink and not entry.is_dir(follow_symlinks=True):
                continue
            if entry.is_dir(follow_symlinks=True):
                # A real os.stat() call, not entry.stat() -- on Windows,
                # DirEntry.stat() never populates st_dev/st_ino (they're
                # always 0: https://docs.python.org/3/library/os.html#os.DirEntry.stat),
                # so every subdirectory would collide on the same (0, 0)
                # "identity" and try_push() would treat all but the first
                # one as an already-visited cycle, silently pruning nearly
                # the entire tree below the top level.
                try_push(Path(entry.path), os.stat(entry.path))
            elif not is_symlink and entry.is_file(follow_symlinks=False):
                path = Path(entry.path)
                if path not in already_seen:
                    yield ("file", path)
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


def files_equal(path_a: Path, path_b: Path) -> bool:
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
    on_checkpoint: Optional[Callable[[list[tuple[str, Path]], list[Path]], None]] = None,
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

    `on_checkpoint(new_entries, new_skipped)`, if given, is called every
    `CHECKPOINT_INTERVAL` files with only what's new *since the last call*
    (as (digest, path) pairs newly confirmed into `result`, and paths newly
    added to `skipped`) -- not the full accumulated state. That keeps each
    checkpoint's cost proportional to `CHECKPOINT_INTERVAL` regardless of
    how far into a huge scan it fires, instead of proportional to total
    progress so far (which would make checkpointing itself the bottleneck
    on a scan with millions of files).
    """
    result: dict[str, list[Path]] = {}
    processed: list[Path] = []
    skipped: list[Path] = []
    cancelled = False
    total = sum(len(bucket) for bucket in buckets)
    progress = Progress(stage_label, total=total, enabled=show_progress, on_progress=on_progress)

    # Append-only logs mirroring `result`/`skipped`, used only to compute
    # cheap since-last-checkpoint deltas below -- see `on_checkpoint` above.
    checkpoint_log: list[tuple[str, Path]] = []
    flushed_entries = 0
    flushed_skipped = 0

    def flush_checkpoint(*, force: bool = False) -> None:
        nonlocal flushed_entries, flushed_skipped
        if on_checkpoint is None:
            return
        if not force and len(processed) % CHECKPOINT_INTERVAL != 0:
            return
        new_entries = checkpoint_log[flushed_entries:]
        new_skipped = skipped[flushed_skipped:]
        if new_entries or new_skipped:
            on_checkpoint(new_entries, new_skipped)
        flushed_entries = len(checkpoint_log)
        flushed_skipped = len(skipped)

    def mark_done(path: Path) -> None:
        processed.append(path)
        progress.update()
        flush_checkpoint()

    def compare(representative: Path, other: Path) -> tuple[Path, Optional[bool], Optional[OSError]]:
        try:
            return other, files_equal(representative, other), None
        except OSError as exc:
            return other, None, exc

    def record_group(representative: Path, matched_others: list[Path]) -> None:
        if not matched_others:
            return
        digest = _full_hash(representative)
        group_paths = [representative] + matched_others
        result.setdefault(digest, []).extend(group_paths)
        if on_checkpoint is not None:
            # checkpoint_log duplicates what's already in `result`, purely
            # to support the delta slicing in flush_checkpoint below -- not
            # worth the extra memory when there's no checkpoint consumer.
            checkpoint_log.extend((digest, p) for p in group_paths)

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
                        mark_done(other)
                    elif is_equal:
                        matched_others.append(other)
                        mark_done(other)
                    else:
                        # Not done yet -- it goes back into `remaining` for
                        # another round against a new representative (a
                        # bucket can hold more than one distinct file if its
                        # members only coincidentally share a size and
                        # partial hash). Marking it done here too, on every
                        # round it takes to finally resolve, is what let
                        # progress.count exceed `total` and flooded
                        # on_progress with far more signal emissions than
                        # there are actual files -- exactly what could
                        # overwhelm the GUI's cross-thread queue and make it
                        # unresponsive even mid-scan, not just afterward.
                        leftover.append(other)
                record_group(representative, matched_others)
                remaining = leftover
            if not cancelled and len(remaining) == 1:
                mark_done(remaining[0])
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    # Unconditional final flush -- without it, whatever's landed in `result`
    # since the last periodic checkpoint (including all of it, if this
    # stage had fewer than CHECKPOINT_INTERVAL files total) would never
    # reach `on_checkpoint`, leaving a resumed stage 3 with an incomplete
    # `by_full` even though this stage otherwise finished cleanly.
    flush_checkpoint(force=True)
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
    on_checkpoint: Optional[Callable[[list[tuple[K, Path]], list[Path]], None]] = None,
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

    `on_checkpoint(new_entries, new_skipped)`, if given, is called every
    `CHECKPOINT_INTERVAL` files with only what's new since the last call --
    see `_group_by_content`'s identical contract for why (checkpoint cost
    must stay proportional to `CHECKPOINT_INTERVAL`, not to total progress).
    """
    result: dict[K, list[Path]] = defaultdict(list)
    processed: list[Path] = []
    skipped: list[Path] = []
    cancelled = False
    total = len(items)
    progress = Progress(stage_label, total=total, enabled=show_progress, on_progress=on_progress)

    checkpoint_log: list[tuple[K, Path]] = []
    flushed_entries = 0
    flushed_skipped = 0

    def flush_checkpoint(*, force: bool = False) -> None:
        nonlocal flushed_entries, flushed_skipped
        if on_checkpoint is None:
            return
        if not force and len(processed) % CHECKPOINT_INTERVAL != 0:
            return
        new_entries = checkpoint_log[flushed_entries:]
        new_skipped = skipped[flushed_skipped:]
        if new_entries or new_skipped:
            on_checkpoint(new_entries, new_skipped)
        flushed_entries = len(checkpoint_log)
        flushed_skipped = len(skipped)

    def record(item: T, digest: Optional[str], error: Optional[OSError]) -> None:
        path = path_of(item)
        if error is not None:
            logger.debug("Skipping unreadable file %s: %s", path, error)
            skipped.append(path)
            processed.append(path)
        else:
            key = key_of(item, digest)
            result[key].append(path)
            if on_checkpoint is not None:
                # See _group_by_content's identical guard: checkpoint_log
                # duplicates `result`, so skip it entirely with no
                # checkpoint consumer to report the delta to.
                checkpoint_log.append((key, path))
            processed.append(path)
            progress.update()
        flush_checkpoint()

    if workers <= 1 or total <= 1:
        for item in items:
            if is_cancelled():
                cancelled = True
                break
            try:
                record(item, hash_fn(path_of(item)), None)
            except OSError as exc:
                record(item, None, exc)
        flush_checkpoint(force=True)
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
    flush_checkpoint(force=True)
    progress.close()
    return result, processed, skipped, cancelled


@dataclass
class DuplicateGroup:
    """`confirmed` is False for a very-large-file group whose members are
    only known to share a size and partial hash -- full confirmation (a
    direct content comparison) was deferred rather than paying its cost
    during the scan, and happens instead right before a file from the
    group is actually deleted. `file_hash` for such a group is a
    "size:partial_hash" string (see `_partial_key`), not a SHA-256 digest.
    """

    file_hash: str
    size: int
    paths: list[Path] = field(default_factory=list)
    confirmed: bool = True


@dataclass
class ScanResult:
    groups: list[DuplicateGroup]
    skipped: list[Path]
    cancelled: bool = False
    resume_state: Optional[ResumeState] = None
    folder_groups: list["FolderGroup"] = field(default_factory=list)


def _partial_key(size: int, partial_hash: str) -> str:
    return f"{size}:{partial_hash}"


def _resume_roots_match(resume_state: ResumeState, directories: list[Path]) -> bool:
    """True if every one of `resume_state`'s recorded root identities
    (`ResumeState.root_keys`) still matches -- i.e. nothing in `directories`
    has quietly become a different filesystem since the state was saved.

    A root missing from `root_keys` (e.g. a `ResumeState` saved before this
    check existed) is treated as a pass for that root -- there's nothing to
    compare against, so this can't newly break resuming an older state.
    """
    for directory in directories:
        recorded = resume_state.root_keys.get(str(directory))
        if recorded is None:
            continue
        try:
            current = _dir_key(os.stat(directory))
        except OSError:
            return False
        if current != recorded:
            return False
    return True


def find_duplicates(
    directories: Iterable[Path],
    *,
    show_progress: bool = False,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    resume_state: Optional[ResumeState] = None,
    workers: Optional[int] = None,
    large_file_threshold: Optional[int] = LARGE_FILE_THRESHOLD,
    on_checkpoint: Optional[CheckpointCallback] = None,
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

    `on_progress(stage_label, count, total)`, if given, is throttled to the
    same interval as `Progress`'s own terminal output (see there), not
    called on every single file -- across a fast stage with many files,
    calling it unthrottled could invoke it far more often than any UI
    could usefully redraw for, and for the GUI specifically (a queued
    cross-thread Qt signal) could flood its event queue faster than the
    main thread can drain it, making the window appear frozen even though
    the scan itself is proceeding normally. `total` is None for stage 1
    (unknown until the walk finishes).

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
    partway through stage 1, 2 or 3. For a `stage` "scanning" resume (the
    directory walk itself was interrupted), any directory already fully
    walked (see `_walk_checkpointed`) is skipped entirely rather than
    re-scanned, and files already recorded are never re-added -- so
    resuming costs work proportional to what's still unwalked, not to
    however much of a huge tree was already covered. For a `stage`
    "quick_hash" or "full_hash" resume, the results already computed for
    the interrupted stage and any earlier stage are reused: for stage 2,
    only the files not yet processed at cancellation time are (re)hashed;
    for stage 3, whole buckets (groups of files sharing a size and partial
    hash) that were already fully confirmed are skipped, but a bucket that
    was only partly resolved when cancelled is redone from scratch rather
    than resumed file-by-file -- representative-based comparison doesn't
    carry over mid-bucket the way independent per-file hashing did, and
    buckets are small enough that this costs little.

    Before trusting any of that, each directory in `directories` is
    checked against `resume_state.root_keys` (see `_resume_roots_match`):
    if one no longer matches the identity recorded when the state was
    saved -- e.g. a different drive is now mounted at the same path --
    `resume_state` is ignored entirely and the scan starts fresh, rather
    than risk silently skipping a now-different directory's files or
    comparing/hashing the wrong drive's content under paths that happen to
    still exist.

    `workers` controls how many files stages 2 and 3 (the actual hashing)
    process concurrently via a thread pool, to take advantage of multiple
    CPU cores/threads instead of hashing one file at a time. Defaults to
    `os.cpu_count()`; pass 1 to hash sequentially.

    `large_file_threshold` (bytes) controls stage 3's cutoff for deferring
    confirmation: a bucket of candidates at or above this size is reported
    as a single unconfirmed `DuplicateGroup` (`confirmed=False`) without
    ever being compared, instead of paying the I/O cost of comparing
    potentially huge files during the scan itself -- especially wasteful
    for files the user might not even choose to delete. The caller is
    expected to confirm such a group itself (e.g. with `files_equal`)
    right before actually deleting one of its files. Pass 0/None to
    disable deferral and always confirm during the scan, regardless of
    size. Defaults to `LARGE_FILE_THRESHOLD` (500MB).

    `on_checkpoint(delta)`, if given, is called periodically (every
    `CHECKPOINT_INTERVAL` files, plus once whenever a directory finishes
    during stage 1, and once more with whatever's left when a stage
    finishes) throughout all three stages with a `CheckpointDelta` -- only
    what's newly confirmed *since the previous call*, not the full state,
    so a caller persisting it (via `store.checkpoint_progress`) pays a
    cost proportional to `CHECKPOINT_INTERVAL`, not to how far into a scan
    with millions of files it fires. This is how progress on a large scan
    -- including the directory walk itself, for a drive large enough that
    walking it is a substantial fraction of the whole scan -- survives a
    crash or power loss, not just a clean Cancel.
    """
    directories = list(directories)
    skipped: list[Path] = []
    cancelled = False
    cancelled_stage: Optional[str] = None
    workers = workers if workers and workers > 0 else default_workers()

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # `directories` never changes once a scan starts, and `by_size` is
    # invariant from the moment stage 2 begins -- both are only worth
    # sending once each, whichever checkpoint (stage 1, stage 2, or stage
    # 3 if resuming straight into it) happens to fire first.
    directories_sent = False

    def _directories_once() -> Optional[list[str]]:
        nonlocal directories_sent
        if directories_sent:
            return None
        directories_sent = True
        return [str(d) for d in directories]

    by_size_sent = False

    def _by_size_once() -> Optional[dict[str, list[str]]]:
        nonlocal by_size_sent
        if by_size_sent:
            return None
        by_size_sent = True
        return {str(size): [str(p) for p in paths] for size, paths in by_size.items()}

    root_keys_sent = False

    def _root_keys_once() -> Optional[dict[str, str]]:
        nonlocal root_keys_sent
        if root_keys_sent:
            return None
        root_keys_sent = True
        keys: dict[str, str] = {}
        for directory in directories:
            try:
                keys[str(directory)] = _dir_key(os.stat(directory))
            except OSError:
                continue
        return keys

    if resume_state is not None and not _resume_roots_match(resume_state, directories):
        # "dev:ino" isn't a permanent identity -- unplugging an external
        # drive and later mounting a *different* one at the same path can
        # reuse it. Trusting completed_dirs here would mean silently
        # skipping a directory that only looks already-covered; trusting
        # by_partial/by_full would mean hashing/comparing an unrelated
        # drive's files under paths that happen to still exist. Either way
        # a fresh scan is the only safe fallback.
        logger.warning(
            "Ignoring saved progress for %s -- a directory's identity has changed "
            "since it was last checkpointed (e.g. a different drive now mounted at "
            "the same path); starting a fresh scan instead of risking wrong results.",
            ", ".join(str(d) for d in directories),
        )
        resume_state = None

    resume_stage = resume_state.stage if resume_state is not None else None

    by_size: dict[int, list[Path]] = defaultdict(list)
    completed_dirs: set[str] = set()
    if resume_stage in ("quick_hash", "full_hash"):
        assert resume_state is not None
        for size_str, paths in resume_state.by_size.items():
            by_size[int(size_str)] = [Path(p) for p in paths]
        skipped.extend(Path(p) for p in resume_state.skipped)
        logger.info("Resuming: stage 1/3 already done (%d distinct sizes)", len(by_size))
    else:
        already_seen: set[Path] = set()
        if resume_stage == "scanning":
            assert resume_state is not None
            for size_str, paths in resume_state.by_size.items():
                by_size[int(size_str)] = [Path(p) for p in paths]
            already_seen = {Path(p) for paths in by_size.values() for p in paths}
            completed_dirs = set(resume_state.completed_dirs)
            skipped.extend(Path(p) for p in resume_state.skipped)
            already_seen |= set(skipped)
            logger.info(
                "Resuming stage 1/3: %d file(s) and %d completed director(y/ies) already known",
                len(already_seen),
                len(completed_dirs),
            )
        else:
            logger.info("Stage 1/3: scanning directories")

        # Delta-tracking for stage 1's own checkpoint, mirroring stage 2/3's
        # pattern in _hash_parallel/_group_by_content -- see CheckpointDelta.
        new_size_entries: list[tuple[str, str]] = []
        new_completed: list[str] = []
        new_skipped_stage1: list[Path] = []

        def flush_stage1_checkpoint(*, force: bool = False) -> None:
            nonlocal new_size_entries, new_completed, new_skipped_stage1
            # A directory-heavy, file-sparse tree (many small/empty
            # directories) could otherwise advance `progress.count` -- the
            # only interval this used to check -- so slowly that a long
            # run of completed directories never gets flushed at all.
            due = force or progress.count % CHECKPOINT_INTERVAL == 0 or len(new_completed) >= CHECKPOINT_INTERVAL
            if not due:
                return
            # Clear unconditionally once due, whether or not there's a
            # caller to report to -- these lists otherwise duplicate
            # everything already in by_size for the rest of the walk when
            # on_checkpoint isn't given (e.g. find_duplicates called
            # directly without checkpointing), roughly doubling stage 1's
            # memory footprint on a large scan for no reason.
            entries, completed, stage1_skipped = new_size_entries, new_completed, new_skipped_stage1
            new_size_entries, new_completed, new_skipped_stage1 = [], [], []
            if on_checkpoint is not None and (entries or completed or stage1_skipped):
                on_checkpoint(
                    CheckpointDelta(
                        stage="scanning",
                        new_entries=entries,
                        new_skipped=[str(p) for p in stage1_skipped],
                        new_completed_dirs=completed,
                        directories=_directories_once(),
                        root_keys=_root_keys_once(),
                    )
                )

        progress = Progress("Scanning", enabled=show_progress, on_progress=on_progress)
        for kind, value in _walk_checkpointed(directories, completed_dirs=completed_dirs, already_seen=already_seen):
            if is_cancelled():
                cancelled = True
                cancelled_stage = "scanning"
                break
            if kind == "dir_done":
                dir_key = value
                completed_dirs.add(dir_key)
                new_completed.append(dir_key)
                flush_stage1_checkpoint()
                continue
            path = value
            try:
                size = path.stat().st_size
            except OSError as exc:
                logger.debug("Skipping unreadable file %s: %s", path, exc)
                skipped.append(path)
                new_skipped_stage1.append(path)
                flush_stage1_checkpoint()
                continue
            by_size[size].append(path)
            new_size_entries.append((str(size), str(path)))
            progress.update()
            flush_stage1_checkpoint()
        flush_stage1_checkpoint(force=True)
        progress.close()
        logger.info("Stage 1/3 done: %d file(s) found this attempt, %d distinct sizes", progress.count, len(by_size))

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

        def _checkpoint_stage2(new_entries: list[tuple[tuple[int, str], Path]], new_skipped: list[Path]) -> None:
            if on_checkpoint is None:
                return
            on_checkpoint(
                CheckpointDelta(
                    stage="quick_hash",
                    new_entries=[(_partial_key(*key), str(p)) for key, p in new_entries],
                    new_skipped=[str(p) for p in new_skipped],
                    by_size=_by_size_once(),
                    directories=_directories_once(),
                    root_keys=_root_keys_once(),
                )
            )

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
            on_checkpoint=_checkpoint_stage2,
        )
        for key, paths in hashed.items():
            by_partial[key].extend(paths)
        skipped.extend(newly_skipped)
        if stage_cancelled:
            cancelled = True
            cancelled_stage = "quick_hash"

    by_full: dict[str, list[Path]] = defaultdict(list)
    processed_stage3: list[Path] = []
    deferred_groups: list[DuplicateGroup] = []
    if not cancelled:
        resolved_paths: set[Path] = set()
        full_key_by_path: dict[Path, str] = {}
        if resume_stage == "full_hash":
            assert resume_state is not None
            for full_hash, paths in resume_state.by_full.items():
                loaded = [Path(p) for p in paths]
                by_full[full_hash] = loaded
                for p in loaded:
                    full_key_by_path[p] = full_hash
            resolved_paths = set(full_key_by_path)

        # Split each (size, partial_hash) bucket into buckets to actually
        # compare now versus very-large-file buckets whose confirmation is
        # deferred until deletion time (see `large_file_threshold` above).
        buckets: list[list[Path]] = []
        for (size, partial_digest), paths in by_partial.items():
            if len(paths) < 2:
                continue
            bucket_set = set(paths)
            if resume_stage == "full_hash":
                # A bucket is only skippable if every one of its members is
                # already accounted for in a confirmed group; a bucket that
                # was only partly resolved when the scan was cancelled is
                # redone from scratch (representative comparisons aren't
                # resumable at file granularity the way independent hashing
                # was), so drop whatever partial group it had contributed
                # to avoid double counting those members once it's redone.
                if bucket_set <= resolved_paths:
                    continue
                # A reverse-index lookup (built once, above) rather than
                # rescanning every already-confirmed group for each bucket
                # here -- with tens of thousands of buckets and confirmed
                # groups both, that rescan is O(buckets x confirmed groups),
                # which is exactly what turned a large resume's setup into a
                # multi-minute (or worse) stall with zero visible progress.
                stale_keys = {full_key_by_path[p] for p in bucket_set if p in full_key_by_path}
                for key in stale_keys:
                    del by_full[key]
            if large_file_threshold and size >= large_file_threshold:
                deferred_groups.append(
                    DuplicateGroup(
                        file_hash=_partial_key(size, partial_digest),
                        size=size,
                        paths=list(paths),
                        confirmed=False,
                    )
                )
            else:
                buckets.append(paths)

        full_total = sum(len(b) for b in buckets)
        if resume_stage == "full_hash":
            logger.info("Resuming stage 3/3: %d candidate file(s) remaining", full_total)
        elif deferred_groups:
            logger.info(
                "Stage 3/3: confirming %d candidate file(s) by content "
                "(%d file(s) in %d large-file group(s) deferred until deletion)",
                full_total,
                sum(len(g.paths) for g in deferred_groups),
                len(deferred_groups),
            )
        else:
            logger.info("Stage 3/3: confirming %d candidate file(s) by content", full_total)

        def _checkpoint_stage3(new_entries: list[tuple[str, Path]], new_skipped: list[Path]) -> None:
            if on_checkpoint is None:
                return
            on_checkpoint(
                CheckpointDelta(
                    stage="full_hash",
                    new_entries=[(digest, str(p)) for digest, p in new_entries],
                    new_skipped=[str(p) for p in new_skipped],
                    by_size=_by_size_once(),
                    directories=_directories_once(),
                    root_keys=_root_keys_once(),
                )
            )

        hashed, processed_stage3, newly_skipped, stage_cancelled = _group_by_content(
            buckets,
            workers=workers,
            stage_label="Confirm duplicates",
            show_progress=show_progress,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            on_checkpoint=_checkpoint_stage3,
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
    ] + deferred_groups
    groups.sort(key=lambda g: g.size * len(g.paths), reverse=True)

    new_resume_state: Optional[ResumeState] = None
    if cancelled:
        logger.info("Cancelled: %d duplicate group(s) confirmed so far, %d file(s) skipped", len(groups), len(skipped))
        final_root_keys: dict[str, str] = {}
        for directory in directories:
            try:
                final_root_keys[str(directory)] = _dir_key(os.stat(directory))
            except OSError:
                continue
        if cancelled_stage in ("quick_hash", "full_hash"):
            new_resume_state = ResumeState(
                directories=[str(d) for d in directories],
                stage=cancelled_stage,
                by_size={str(size): [str(p) for p in paths] for size, paths in by_size.items()},
                by_partial={_partial_key(*key): [str(p) for p in paths] for key, paths in by_partial.items()},
                by_full={h: [str(p) for p in paths] for h, paths in by_full.items()},
                processed=[str(p) for p in (processed_stage2 if cancelled_stage == "quick_hash" else processed_stage3)],
                skipped=[str(p) for p in skipped],
                root_keys=final_root_keys,
            )
        else:
            new_resume_state = ResumeState(
                directories=[str(d) for d in directories],
                stage="scanning",
                by_size={str(size): [str(p) for p in paths] for size, paths in by_size.items()},
                completed_dirs=sorted(completed_dirs),
                skipped=[str(p) for p in skipped],
                root_keys=final_root_keys,
            )
    else:
        logger.info("Done: %d duplicate group(s), %d file(s) skipped", len(groups), len(skipped))

    folder_groups: list["FolderGroup"] = []
    if not cancelled:
        # Only meaningful for a complete scan -- a cancelled one hasn't
        # finished confirming every candidate, so a directory could look
        # "fully accounted for" purely because its remaining files weren't
        # reached yet, not because they're actually unique.
        from .folders import find_duplicate_folders

        all_files = [p for paths in by_size.values() for p in paths]
        folder_groups = find_duplicate_folders(all_files, skipped, groups, directories)
        if folder_groups:
            logger.info("Found %d duplicate folder(s)", len(folder_groups))

    return ScanResult(
        groups=groups,
        skipped=skipped,
        cancelled=cancelled,
        resume_state=new_resume_state,
        folder_groups=folder_groups,
    )
