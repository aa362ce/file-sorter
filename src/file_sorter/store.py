from __future__ import annotations

import json
import sqlite3
import threading
import time
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

    A state saved via `save_resume_state` mid-scan (a "checkpoint", see
    `dedupe.find_duplicates`'s `on_checkpoint`) has exactly the same shape
    as one saved when a scan is actually cancelled -- there's nothing
    special about the final save, so a hard crash or power loss finds
    whatever the most recent checkpoint left behind, not just a clean
    Cancel.
    """

    directories: list[str]
    stage: str
    by_size: dict[str, list[str]] = field(default_factory=dict)
    by_partial: dict[str, list[str]] = field(default_factory=dict)
    by_full: dict[str, list[str]] = field(default_factory=dict)
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


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
        CREATE TABLE IF NOT EXISTS resume_states (
            run_id TEXT PRIMARY KEY,
            directories TEXT NOT NULL,
            stage TEXT NOT NULL,
            by_size TEXT NOT NULL,
            by_partial TEXT NOT NULL,
            by_full TEXT NOT NULL,
            processed TEXT NOT NULL,
            skipped TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
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
                    _upsert_resume_state(conn, run_id, state)
            conn.commit()
            _LEGACY_RESUME_FILE.rename(_LEGACY_RESUME_FILE.with_suffix(".json.migrated"))
        except OSError:
            pass


# -- resume state -----------------------------------------------------


def _upsert_resume_state(conn: sqlite3.Connection, run_id: str, state: ResumeState) -> None:
    conn.execute(
        """
        INSERT INTO resume_states
            (run_id, directories, stage, by_size, by_partial, by_full, processed, skipped, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            directories=excluded.directories,
            stage=excluded.stage,
            by_size=excluded.by_size,
            by_partial=excluded.by_partial,
            by_full=excluded.by_full,
            processed=excluded.processed,
            skipped=excluded.skipped,
            updated_at=excluded.updated_at
        """,
        (
            run_id,
            json.dumps(state.directories),
            state.stage,
            json.dumps(state.by_size),
            json.dumps(state.by_partial),
            json.dumps(state.by_full),
            json.dumps(state.processed),
            json.dumps(state.skipped),
            time.time(),
        ),
    )
    row = conn.execute("SELECT COUNT(*) FROM resume_states").fetchone()
    if row[0] > MAX_RESUME_STATES:
        conn.execute(
            """
            DELETE FROM resume_states WHERE run_id IN (
                SELECT run_id FROM resume_states ORDER BY updated_at ASC LIMIT ?
            )
            """,
            (row[0] - MAX_RESUME_STATES,),
        )


def _row_to_resume_state(row: sqlite3.Row) -> ResumeState:
    return ResumeState(
        directories=json.loads(row["directories"]),
        stage=row["stage"],
        by_size=json.loads(row["by_size"]),
        by_partial=json.loads(row["by_partial"]),
        by_full=json.loads(row["by_full"]),
        processed=json.loads(row["processed"]),
        skipped=json.loads(row["skipped"]),
    )


def save_resume_state(run_id: str, state: ResumeState) -> None:
    """Persist `state` for `run_id`, replacing any previously saved state
    for the same run.

    This is a single-row UPSERT, not a rewrite of every saved run's state
    the way the old JSON store's `_save_all` was -- cheap enough to call
    every couple thousand files during a long hash stage (see
    `dedupe.find_duplicates`'s `on_checkpoint`) as well as once when a
    scan is cancelled, so progress on a large scan survives a crash or
    power loss, not just a clean Cancel.
    """
    try:
        conn = _connect()
    except OSError:
        return
    with conn:
        _upsert_resume_state(conn, run_id, state)
    conn.close()


def load_resume_state(run_id: str) -> Optional[ResumeState]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM resume_states WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_resume_state(row) if row is not None else None


def clear_resume_state(run_id: str) -> None:
    conn = _connect()
    with conn:
        conn.execute("DELETE FROM resume_states WHERE run_id = ?", (run_id,))
    conn.close()


def resumable_run_ids() -> set[str]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT run_id FROM resume_states").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def latest_resume_run_id() -> Optional[str]:
    conn = _connect()
    try:
        row = conn.execute("SELECT run_id FROM resume_states ORDER BY updated_at DESC LIMIT 1").fetchone()
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
        conn.execute(
            """
            DELETE FROM runs WHERE run_id IN (
                SELECT run_id FROM runs ORDER BY timestamp ASC LIMIT ?
            )
            """,
            (row[0] - MAX_HISTORY_ENTRIES,),
        )


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


def load_history() -> list[RunRecord]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM runs ORDER BY timestamp ASC").fetchall()
    finally:
        conn.close()
    return [_row_to_run_record(row) for row in rows]


def export_history(path: Path) -> int:
    """Write the full current history to `path` as JSON. Returns the count written."""
    records = load_history()
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
