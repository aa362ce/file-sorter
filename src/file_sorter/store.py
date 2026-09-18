from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from .dedupe import ScanResult

STORE_DIR = Path.home() / ".file-sorter"
DB_PATH = STORE_DIR / "file_sorter.db"

# Legacy per-feature JSON files, from before both were merged into one
# SQLite database -- imported once (if present) the first time the new
# database is created, so upgrading doesn't lose anyone's history or a
# scan they could still resume.
_LEGACY_RESUME_FILE = STORE_DIR / "resume_states.json"
_LEGACY_HISTORY_FILE = STORE_DIR / "history.json"

MAX_RESUME_STATES = 50
MAX_HISTORY_ENTRIES = 200

_schema_ready = False
_schema_lock = threading.Lock()


@dataclass
class ResumeState:
    """A snapshot of an interrupted scan, saved so it can be picked up again
    without redoing work that was already done.

    Each saved state is keyed by a `run_id` -- the `str(timestamp)` of the
    history record for that cancelled attempt -- so a scan that gets
    cancelled more than once across separate runs keeps each attempt
    independently resumable, and resuming one doesn't affect any other
    stopped run still sitting in history.

    `stage` is the stage that was in progress when the scan was cancelled:
    "scanning" (stage 1, walking directories), "quick_hash" (stage 2,
    partial hashing) or "full_hash" (stage 3, confirming duplicates by
    content). `by_size` / `by_partial` / `by_full` hold whatever results
    that stage (and any earlier stage) had already produced, keyed as
    strings since JSON object keys can't be ints or tuples. `processed`
    lists the files already handled within the interrupted stage -- for a
    "quick_hash" resume this is used precisely, to skip re-hashing those
    files; for a "full_hash" resume, whether a *bucket* (not individual
    file) can be skipped is instead derived from `by_full`, since
    content-comparison confirmation isn't resumable at individual-file
    granularity the way independent hashing was.

    `completed_dirs` is only meaningful for a "scanning" resume: "dev:ino"
    keys (see `dedupe._dir_key`) of directories the walk had already fully
    finished with, so resuming skips re-scanning them entirely instead of
    re-discovering (and, worse, re-counting) everything already found --
    see `dedupe._walk_checkpointed`.

    `root_keys` maps each scan root (a `directories` entry, as a string)
    to its own "dev:ino" identity at checkpoint time -- not the identity
    of anything under it. A "dev:ino" pair isn't a permanent identifier:
    unplugging an external drive and later mounting a *different* one at
    the same path (or drive letter) can reuse it, especially for
    low-numbered inodes. Comparing this against the root's current
    identity before trusting anything else in the state is what stops a
    resumed scan from silently treating a different drive's directories as
    "already covered" (stage "scanning") or hashing/comparing a different
    drive's files under paths that happen to still exist (any stage) --
    see `dedupe._resume_roots_match`.

    A scan also checkpoints incrementally as it runs (see
    `dedupe.find_duplicates`'s `on_checkpoint` and `checkpoint_progress`
    below) so a hard crash or power loss finds recent progress, not just
    whatever was there after a clean Cancel -- `ResumeState` is just the
    shape progress takes once assembled, whether that happened via one
    full write at cancellation or many small incremental ones during the
    scan.
    """

    directories: list[str]
    stage: str
    by_size: dict[str, list[str]] = field(default_factory=dict)
    by_partial: dict[str, list[str]] = field(default_factory=dict)
    by_full: dict[str, list[str]] = field(default_factory=dict)
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    completed_dirs: list[str] = field(default_factory=list)
    root_keys: dict[str, str] = field(default_factory=dict)


@dataclass
class CheckpointDelta:
    """What's new since the previous checkpoint of a running scan (see
    `dedupe.find_duplicates`'s `on_checkpoint`) -- deliberately *not* the
    full accumulated state, so persisting it (via `checkpoint_progress`)
    costs work proportional to this delta, not to how far into a
    million-file scan it fires.

    `new_entries` is (bucket_key, path) pairs newly added since the last
    checkpoint -- into `by_size` for `stage` "scanning", `by_partial` for
    "quick_hash", or `by_full` for "full_hash" (`bucket_key` is the file
    size as a string for "scanning"). `new_completed_dirs` (only used for
    "scanning") is directory keys newly finished since the last checkpoint
    -- see `ResumeState.completed_dirs`. `by_size` and `directories` are
    only set on the very first checkpoint of a run -- neither changes
    again after that, so there's no reason to resend either one every
    time. (`by_size` here means the *stage 2/3* one-time snapshot of the
    stage-1 result -- unrelated to "scanning"'s own incremental
    `new_entries`, which feed the same table by a different, appending
    path; see `checkpoint_progress`.) `root_keys` is also only set on the
    very first checkpoint of a run -- see `ResumeState.root_keys`.
    """

    stage: str
    new_entries: list[tuple[str, str]] = field(default_factory=list)
    new_skipped: list[str] = field(default_factory=list)
    new_completed_dirs: list[str] = field(default_factory=list)
    by_size: Optional[dict[str, list[str]]] = None
    directories: Optional[list[str]] = None
    root_keys: Optional[dict[str, str]] = None


@dataclass
class RunRecord:
    timestamp: float
    directories: list[str] = field(default_factory=list)
    groups: int = 0
    reclaimable_bytes: int = 0
    skipped: int = 0
    cancelled: bool = False
    duration_seconds: float = 0.0


def _connect() -> sqlite3.Connection:
    global _schema_ready
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # WAL keeps a frequent small checkpoint write (every couple thousand
    # files during a long hash stage) cheap -- an UPSERT of one row, not a
    # full-database fsync -- which is the whole point of moving off a
    # JSON file that had to be re-serialized and rewritten in full on
    # every single save.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if not _schema_ready:
        with _schema_lock:
            _init_schema(conn)
            _schema_ready = True
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    is_new = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone() is None
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            directories TEXT NOT NULL,
            groups INTEGER NOT NULL,
            reclaimable_bytes INTEGER NOT NULL,
            skipped INTEGER NOT NULL,
            cancelled INTEGER NOT NULL,
            duration_seconds REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resume_runs (
            run_id TEXT PRIMARY KEY,
            directories TEXT NOT NULL,
            stage TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resume_progress (
            run_id TEXT NOT NULL,
            list_name TEXT NOT NULL,
            key TEXT NOT NULL,
            path TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_resume_progress_run ON resume_progress(run_id);
        CREATE TABLE IF NOT EXISTS run_groups (
            run_id TEXT NOT NULL,
            group_idx INTEGER NOT NULL,
            kind TEXT NOT NULL,
            file_hash TEXT,
            file_count INTEGER,
            size INTEGER NOT NULL,
            confirmed INTEGER NOT NULL,
            PRIMARY KEY (run_id, kind, group_idx)
        );
        CREATE TABLE IF NOT EXISTS run_group_paths (
            run_id TEXT NOT NULL,
            group_idx INTEGER NOT NULL,
            kind TEXT NOT NULL,
            seq INTEGER NOT NULL,
            path TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_group_paths_run ON run_group_paths(run_id);
        CREATE TABLE IF NOT EXISTS scan_manifests (
            dir_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            saved_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scan_manifest_files (
            dir_key TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            PRIMARY KEY (dir_key, path)
        );
        CREATE INDEX IF NOT EXISTS idx_scan_manifest_files_dir ON scan_manifest_files(dir_key);
        """
    )
    conn.commit()
    if is_new:
        _migrate_legacy_json(conn)


def _migrate_legacy_json(conn: sqlite3.Connection) -> None:
    if _LEGACY_HISTORY_FILE.exists():
        try:
            raw = json.loads(_LEGACY_HISTORY_FILE.read_text())
            if isinstance(raw, list):
                for item in raw:
                    try:
                        record = RunRecord(**item)
                    except TypeError:
                        continue
                    _upsert_run(conn, str(record.timestamp), record)
            conn.commit()
            _LEGACY_HISTORY_FILE.rename(_LEGACY_HISTORY_FILE.with_suffix(".json.migrated"))
        except OSError:
            pass

    if _LEGACY_RESUME_FILE.exists():
        try:
            raw = json.loads(_LEGACY_RESUME_FILE.read_text())
            if isinstance(raw, dict):
                for run_id, data in raw.items():
                    try:
                        state = ResumeState(**data)
                    except TypeError:
                        continue
                    _replace_resume_state(conn, run_id, state)
            conn.commit()
            _LEGACY_RESUME_FILE.rename(_LEGACY_RESUME_FILE.with_suffix(".json.migrated"))
        except OSError:
            pass


# -- resume state -----------------------------------------------------
#
# `resume_runs` holds one small row per run (its directories and current
# stage). `resume_progress` holds every path making up that run's
# by_size/by_partial/by_full/skipped, one row each, tagged by `list_name`.
# Splitting it this way is what makes `checkpoint_progress` cheap: adding
# rows for what's newly confirmed since the last checkpoint is a plain
# INSERT, not a read-modify-write of an ever-growing JSON blob -- its cost
# never depends on how much of the run has already been persisted.


def _touch_resume_run(conn: sqlite3.Connection, run_id: str, *, stage: str, directories: Optional[list[str]]) -> None:
    conn.execute(
        """
        INSERT INTO resume_runs (run_id, directories, stage, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            stage=excluded.stage,
            updated_at=excluded.updated_at
            """
        + (", directories=excluded.directories" if directories is not None else ""),
        (run_id, json.dumps(directories if directories is not None else []), stage, time.time()),
    )


def _evict_old_resume_runs(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) FROM resume_runs").fetchone()
    if row[0] <= MAX_RESUME_STATES:
        return
    stale_ids = [
        r[0]
        for r in conn.execute(
            "SELECT run_id FROM resume_runs ORDER BY updated_at ASC LIMIT ?",
            (row[0] - MAX_RESUME_STATES,),
        ).fetchall()
    ]
    conn.executemany("DELETE FROM resume_runs WHERE run_id = ?", [(rid,) for rid in stale_ids])
    conn.executemany("DELETE FROM resume_progress WHERE run_id = ?", [(rid,) for rid in stale_ids])


def _replace_resume_state(conn: sqlite3.Connection, run_id: str, state: ResumeState) -> None:
    """Write the full state, replacing anything already stored for this
    run_id -- an O(size of state) operation, appropriate for the one-off
    authoritative save on cancellation (or a legacy-JSON import), but not
    for frequent mid-scan checkpointing (see `checkpoint_progress`).
    """
    _touch_resume_run(conn, run_id, stage=state.stage, directories=state.directories)
    conn.execute("DELETE FROM resume_progress WHERE run_id = ?", (run_id,))
    rows = [(run_id, "by_size", key, path) for key, paths in state.by_size.items() for path in paths]
    rows += [(run_id, "by_partial", key, path) for key, paths in state.by_partial.items() for path in paths]
    rows += [(run_id, "by_full", key, path) for key, paths in state.by_full.items() for path in paths]
    rows += [(run_id, "skipped", "", path) for path in state.skipped]
    rows += [(run_id, "completed_dirs", "", dir_key) for dir_key in state.completed_dirs]
    rows += [(run_id, "root_keys", directory, key) for directory, key in state.root_keys.items()]
    if rows:
        conn.executemany("INSERT INTO resume_progress (run_id, list_name, key, path) VALUES (?, ?, ?, ?)", rows)
    _evict_old_resume_runs(conn)


def save_resume_state(run_id: str, state: ResumeState) -> None:
    """Persist the full `state` for `run_id`, replacing anything already
    saved for the same run -- used for the one authoritative save when a
    scan is actually cancelled. For frequent mid-scan progress saves, see
    `checkpoint_progress` instead, which only ever writes what's new.
    """
    try:
        conn = _connect()
    except OSError:
        return
    with conn:
        _replace_resume_state(conn, run_id, state)
    conn.close()


def checkpoint_progress(run_id: str, delta: CheckpointDelta) -> None:
    """Persist `delta` -- what's new since the previous checkpoint of a
    running scan -- without touching anything already saved for `run_id`.

    Every call is an append of a handful of rows plus one tiny metadata
    update, regardless of how much of the run is already stored, which is
    what keeps checkpointing viable on a scan with millions of files: the
    cost of the 500th checkpoint is the same as the cost of the 5th.
    """
    try:
        conn = _connect()
    except OSError:
        return
    with conn:
        _touch_resume_run(conn, run_id, stage=delta.stage, directories=delta.directories)
        if delta.by_size is not None:
            # by_size doesn't change once a run starts hashing -- sent (and
            # stored) exactly once, so replace rather than append, in case
            # a retry ever sends it twice.
            conn.execute("DELETE FROM resume_progress WHERE run_id = ? AND list_name = 'by_size'", (run_id,))
            rows = [(run_id, "by_size", key, path) for key, paths in delta.by_size.items() for path in paths]
            if rows:
                conn.executemany("INSERT INTO resume_progress (run_id, list_name, key, path) VALUES (?, ?, ?, ?)", rows)
        if delta.root_keys is not None:
            # Same one-time replace pattern as by_size above -- root_keys
            # never changes after the first checkpoint of a run.
            conn.execute("DELETE FROM resume_progress WHERE run_id = ? AND list_name = 'root_keys'", (run_id,))
            rows = [(run_id, "root_keys", directory, key) for directory, key in delta.root_keys.items()]
            if rows:
                conn.executemany("INSERT INTO resume_progress (run_id, list_name, key, path) VALUES (?, ?, ?, ?)", rows)
        if delta.new_entries:
            # "scanning"'s own new_entries feed by_size too, incrementally,
            # by appending rather than the replace-on-first-checkpoint path
            # above -- unlike stage 2/3, stage 1 has no single point where
            # the whole thing is known at once, so it can only ever grow
            # row by row as the walk finds more files.
            list_name = {"scanning": "by_size", "quick_hash": "by_partial", "full_hash": "by_full"}[delta.stage]
            conn.executemany(
                "INSERT INTO resume_progress (run_id, list_name, key, path) VALUES (?, ?, ?, ?)",
                [(run_id, list_name, key, path) for key, path in delta.new_entries],
            )
        if delta.new_skipped:
            conn.executemany(
                "INSERT INTO resume_progress (run_id, list_name, key, path) VALUES (?, 'skipped', '', ?)",
                [(run_id, path) for path in delta.new_skipped],
            )
        if delta.new_completed_dirs:
            conn.executemany(
                "INSERT INTO resume_progress (run_id, list_name, key, path) VALUES (?, 'completed_dirs', '', ?)",
                [(run_id, dir_key) for dir_key in delta.new_completed_dirs],
            )
        _evict_old_resume_runs(conn)
    conn.close()


def load_resume_state(run_id: str) -> Optional[ResumeState]:
    conn = _connect()
    try:
        meta = conn.execute("SELECT directories, stage FROM resume_runs WHERE run_id = ?", (run_id,)).fetchone()
        if meta is None:
            return None
        rows = conn.execute(
            "SELECT list_name, key, path FROM resume_progress WHERE run_id = ?", (run_id,)
        ).fetchall()
    finally:
        conn.close()

    by_size: dict[str, list[str]] = defaultdict(list)
    by_partial: dict[str, list[str]] = defaultdict(list)
    by_full: dict[str, list[str]] = defaultdict(list)
    skipped: list[str] = []
    completed_dirs: list[str] = []
    root_keys: dict[str, str] = {}
    for list_name, key, path in rows:
        if list_name == "by_size":
            by_size[key].append(path)
        elif list_name == "by_partial":
            by_partial[key].append(path)
        elif list_name == "by_full":
            by_full[key].append(path)
        elif list_name == "skipped":
            skipped.append(path)
        elif list_name == "completed_dirs":
            completed_dirs.append(path)
        elif list_name == "root_keys":
            root_keys[key] = path

    # `processed` isn't stored directly -- every path that ended up in
    # by_partial/by_full or skipped was, by construction, also counted as
    # processed (see dedupe._hash_parallel/_group_by_content), so this
    # union reconstructs it exactly for whichever stage was in progress.
    processed = sorted(
        {p for paths in by_partial.values() for p in paths} | {p for paths in by_full.values() for p in paths}
        | set(skipped)
    )
    return ResumeState(
        directories=json.loads(meta[0]),
        stage=meta[1],
        by_size=dict(by_size),
        by_partial=dict(by_partial),
        by_full=dict(by_full),
        processed=processed,
        skipped=skipped,
        completed_dirs=completed_dirs,
        root_keys=root_keys,
    )


def clear_resume_state(run_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM resume_runs WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM resume_progress WHERE run_id = ?", (run_id,))
    conn.close()


def resumable_run_ids() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT run_id FROM resume_runs").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def latest_resume_run_id() -> Optional[str]:
    conn = _connect()
    try:
        row = conn.execute("SELECT run_id FROM resume_runs ORDER BY updated_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


# -- run history --------------------------------------------------------


def _upsert_run(conn: sqlite3.Connection, run_id: str, record: RunRecord) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, timestamp, directories, groups, reclaimable_bytes, skipped, cancelled, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            timestamp=excluded.timestamp,
            directories=excluded.directories,
            groups=excluded.groups,
            reclaimable_bytes=excluded.reclaimable_bytes,
            skipped=excluded.skipped,
            cancelled=excluded.cancelled,
            duration_seconds=excluded.duration_seconds
        """,
        (
            run_id,
            record.timestamp,
            json.dumps(record.directories),
            record.groups,
            record.reclaimable_bytes,
            record.skipped,
            int(record.cancelled),
            record.duration_seconds,
        ),
    )
    row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    if row[0] > MAX_HISTORY_ENTRIES:
        stale_ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM runs ORDER BY timestamp ASC LIMIT ?",
                (row[0] - MAX_HISTORY_ENTRIES,),
            ).fetchall()
        ]
        conn.executemany("DELETE FROM runs WHERE run_id = ?", [(rid,) for rid in stale_ids])
        conn.executemany("DELETE FROM run_groups WHERE run_id = ?", [(rid,) for rid in stale_ids])
        conn.executemany("DELETE FROM run_group_paths WHERE run_id = ?", [(rid,) for rid in stale_ids])
        _delete_manifests_for_runs(conn, stale_ids)


def _row_to_run_record(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        timestamp=row["timestamp"],
        directories=json.loads(row["directories"]),
        groups=row["groups"],
        reclaimable_bytes=row["reclaimable_bytes"],
        skipped=row["skipped"],
        cancelled=bool(row["cancelled"]),
        duration_seconds=row["duration_seconds"],
    )


def record_run(
    directories: Iterable[Path],
    result: "ScanResult",
    duration_seconds: float,
    *,
    run_id: Optional[str] = None,
) -> RunRecord:
    """Record a finished (or cancelled) run in history.

    `run_id`, if given, is used as both the row's primary key and its
    timestamp (as `float(run_id)`) -- callers that already generated a
    run_id at scan start (to checkpoint resume state under while the scan
    was running) pass it back in here so the history entry lines up with
    whatever was checkpointed, instead of minting a second, different id
    for the same run.
    """
    reclaimable = sum(g.size * (len(g.paths) - 1) for g in result.groups)
    timestamp = float(run_id) if run_id is not None else time.time()
    record = RunRecord(
        timestamp=timestamp,
        directories=[str(d) for d in directories],
        groups=len(result.groups),
        reclaimable_bytes=reclaimable,
        skipped=len(result.skipped),
        cancelled=result.cancelled,
        duration_seconds=duration_seconds,
    )
    conn = _connect()
    with conn:
        _upsert_run(conn, run_id if run_id is not None else str(timestamp), record)
    conn.close()
    return record


def save_run_groups(run_id: str, result: "ScanResult") -> None:
    """Persist the full duplicate groups/folder groups of a finished (or
    cancelled) run, so `load_run_groups` can later reload the same result
    for display without re-scanning. Companion to `record_run`, which only
    stores the summary shown in history -- callers save both for the same
    run_id.
    """
    group_rows = []
    path_rows = []
    for idx, group in enumerate(result.groups):
        group_rows.append((run_id, idx, "file", group.file_hash, None, group.size, int(group.confirmed)))
        path_rows.extend((run_id, idx, "file", seq, str(path)) for seq, path in enumerate(group.paths))
    for idx, folder_group in enumerate(result.folder_groups):
        group_rows.append(
            (run_id, idx, "folder", None, folder_group.file_count, folder_group.size, int(folder_group.confirmed))
        )
        path_rows.extend(
            (run_id, idx, "folder", seq, str(path)) for seq, path in enumerate(folder_group.paths)
        )

    conn = _connect()
    with conn:
        conn.execute("DELETE FROM run_groups WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_group_paths WHERE run_id = ?", (run_id,))
        if group_rows:
            conn.executemany(
                "INSERT INTO run_groups (run_id, group_idx, kind, file_hash, file_count, size, confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                group_rows,
            )
        if path_rows:
            conn.executemany(
                "INSERT INTO run_group_paths (run_id, group_idx, kind, seq, path) VALUES (?, ?, ?, ?, ?)",
                path_rows,
            )
    conn.close()


def load_run_groups(run_id: str) -> Optional[tuple[list, list]]:
    """Reload the duplicate groups/folder groups saved by `save_run_groups`
    for `run_id`, as (groups, folder_groups) -- or None if nothing was
    saved for it (e.g. a run from before this existed, or one imported
    from a history export, which only ever carries the summary).
    """
    from .dedupe import DuplicateGroup
    from .folders import FolderGroup

    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        group_rows = conn.execute(
            "SELECT * FROM run_groups WHERE run_id = ? ORDER BY kind, group_idx", (run_id,)
        ).fetchall()
        if not group_rows:
            return None
        path_rows = conn.execute(
            "SELECT group_idx, kind, path FROM run_group_paths WHERE run_id = ? ORDER BY kind, group_idx, seq",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()

    paths_by_key: dict[tuple[str, int], list[Path]] = defaultdict(list)
    for row in path_rows:
        paths_by_key[(row["kind"], row["group_idx"])].append(Path(row["path"]))

    groups: list[DuplicateGroup] = []
    folder_groups: list[FolderGroup] = []
    for row in group_rows:
        paths = paths_by_key.get((row["kind"], row["group_idx"]), [])
        if row["kind"] == "file":
            groups.append(
                DuplicateGroup(file_hash=row["file_hash"], size=row["size"], paths=paths, confirmed=bool(row["confirmed"]))
            )
        else:
            folder_groups.append(
                FolderGroup(paths=paths, file_count=row["file_count"], size=row["size"], confirmed=bool(row["confirmed"]))
            )
    return groups, folder_groups


def load_history() -> list[RunRecord]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM runs ORDER BY timestamp ASC").fetchall()
    finally:
        conn.close()
    return [_row_to_run_record(row) for row in rows]


def export_history(path: Path, run_ids: Optional[Iterable[str]] = None) -> int:
    """Write history to `path` as JSON. Returns the count written.

    With `run_ids` omitted, writes the full history. Otherwise writes only
    the records whose `str(timestamp)` (i.e. run id) is in `run_ids`, so a
    single selected run can be backed up without dragging along the rest
    of the history.
    """
    records = load_history()
    if run_ids is not None:
        wanted = set(run_ids)
        records = [r for r in records if str(r.timestamp) in wanted]
    path.write_text(json.dumps([asdict(r) for r in records], indent=2))
    return len(records)


def import_history(path: Path) -> int:
    """Merge run records from `path` into the existing history.

    Records already present (matched by timestamp + directories) are
    skipped, so importing the same export twice is a no-op the second
    time. Returns the count of newly added records.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("history file must contain a JSON list of run records")
    incoming = []
    for item in raw:
        try:
            incoming.append(RunRecord(**item))
        except TypeError:
            continue

    existing = load_history()
    seen = {(r.timestamp, tuple(r.directories)) for r in existing}

    added = 0
    conn = _connect()
    with conn:
        for record in incoming:
            key = (record.timestamp, tuple(record.directories))
            if key in seen:
                continue
            seen.add(key)
            _upsert_run(conn, str(record.timestamp), record)
            added += 1
    conn.close()
    return added


def delete_runs(run_ids: Iterable[str]) -> int:
    """Permanently remove the given run_ids from history, along with any
    saved detailed results and resume state for them. Returns the count of
    history entries actually removed.
    """
    ids = list(dict.fromkeys(run_ids))
    if not ids:
        return 0
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(ids))
        existing = conn.execute(f"SELECT COUNT(*) FROM runs WHERE run_id IN ({placeholders})", ids).fetchone()[0]
        with conn:
            conn.executemany("DELETE FROM runs WHERE run_id = ?", [(rid,) for rid in ids])
            conn.executemany("DELETE FROM run_groups WHERE run_id = ?", [(rid,) for rid in ids])
            conn.executemany("DELETE FROM run_group_paths WHERE run_id = ?", [(rid,) for rid in ids])
            conn.executemany("DELETE FROM resume_runs WHERE run_id = ?", [(rid,) for rid in ids])
            conn.executemany("DELETE FROM resume_progress WHERE run_id = ?", [(rid,) for rid in ids])
            _delete_manifests_for_runs(conn, ids)
    finally:
        conn.close()
    return existing


def clear_history() -> int:
    """Remove all run history, saved detailed results, and resume state.
    Returns the count of history entries removed.
    """
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        with conn:
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM run_groups")
            conn.execute("DELETE FROM run_group_paths")
            conn.execute("DELETE FROM resume_runs")
            conn.execute("DELETE FROM resume_progress")
            conn.execute("DELETE FROM scan_manifests")
            conn.execute("DELETE FROM scan_manifest_files")
    finally:
        conn.close()
    return count


# -- scan manifests -------------------------------------------------------
#
# A manifest is a {path: (size, mtime)} snapshot of every file under one
# particular set of scanned directories, saved by `dedupe.scan_or_reuse`
# after a completed scan. The *next* scan of that same directory set can
# be checked against it cheaply (a stat-only walk, no hashing) -- if
# nothing differs, that earlier scan's saved duplicate groups are replayed
# instead of redoing all the hashing. Keyed by `dir_key` (the directory
# set, order-independent) rather than by run_id, so each directory set
# only ever has one manifest -- the most recent -- rather than one per
# run piling up forever.


def _dir_key(directories: Iterable[str]) -> str:
    return json.dumps(sorted(str(d) for d in directories))


def _delete_manifests_for_runs(conn: sqlite3.Connection, run_ids: list[str]) -> None:
    """Remove any manifest whose `run_id` is one of these -- called when
    those runs themselves are being deleted (individually or via history
    eviction), so a manifest never points at a run_id whose saved groups
    no longer exist. Must be called inside the same transaction as the
    run deletion it accompanies.
    """
    if not run_ids:
        return
    placeholders = ",".join("?" * len(run_ids))
    stale_dir_keys = [
        row[0] for row in conn.execute(f"SELECT dir_key FROM scan_manifests WHERE run_id IN ({placeholders})", run_ids).fetchall()
    ]
    conn.executemany("DELETE FROM scan_manifests WHERE run_id = ?", [(rid,) for rid in run_ids])
    conn.executemany("DELETE FROM scan_manifest_files WHERE dir_key = ?", [(k,) for k in stale_dir_keys])


def save_scan_manifest(directories: Iterable[str], run_id: str, manifest: dict[str, tuple[int, float]]) -> None:
    """Replace whatever manifest was saved for this directory set (if any)
    with `manifest`, attributed to `run_id`. See `find_reusable_run` for
    how it's later checked against.
    """
    dir_key = _dir_key(directories)
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM scan_manifest_files WHERE dir_key = ?", (dir_key,))
        conn.execute(
            """
            INSERT INTO scan_manifests (dir_key, run_id, saved_at) VALUES (?, ?, ?)
            ON CONFLICT(dir_key) DO UPDATE SET run_id=excluded.run_id, saved_at=excluded.saved_at
            """,
            (dir_key, run_id, time.time()),
        )
        if manifest:
            conn.executemany(
                "INSERT INTO scan_manifest_files (dir_key, path, size, mtime) VALUES (?, ?, ?, ?)",
                [(dir_key, path, size, mtime) for path, (size, mtime) in manifest.items()],
            )
    conn.close()


def find_reusable_run(directories: Iterable[str], manifest: dict[str, tuple[int, float]]) -> Optional[str]:
    """If `manifest` (a fresh `{path: (size, mtime)}` snapshot -- see
    `dedupe.quick_scan_manifest`) exactly matches what was saved for this
    same directory set by a previous scan -- same files present, same
    sizes, same modification times, nothing added or removed -- returns
    that scan's run_id, so its saved duplicate groups (see
    `load_run_groups`) can be reused instead of rehashing everything.

    Returns None on any mismatch, including when no manifest was ever
    saved for this directory set -- either way the caller should fall
    back to a real scan.
    """
    dir_key = _dir_key(directories)
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        meta = conn.execute("SELECT run_id FROM scan_manifests WHERE dir_key = ?", (dir_key,)).fetchone()
        if meta is None:
            return None
        rows = conn.execute(
            "SELECT path, size, mtime FROM scan_manifest_files WHERE dir_key = ?", (dir_key,)
        ).fetchall()
    finally:
        conn.close()

    if len(rows) != len(manifest):
        return None
    for row in rows:
        current = manifest.get(row["path"])
        if current is None or current[0] != row["size"] or current[1] != row["mtime"]:
            return None
    return meta["run_id"]
