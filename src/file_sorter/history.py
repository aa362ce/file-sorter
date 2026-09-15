from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .dedupe import ScanResult

HISTORY_DIR = Path.home() / ".file-sorter"
HISTORY_FILE = HISTORY_DIR / "history.json"
MAX_HISTORY_ENTRIES = 200


@dataclass
class RunRecord:
    timestamp: float
    directories: list[str] = field(default_factory=list)
    groups: int = 0
    reclaimable_bytes: int = 0
    skipped: int = 0
    cancelled: bool = False
    duration_seconds: float = 0.0


def record_run(directories: Iterable[Path], result: ScanResult, duration_seconds: float) -> RunRecord:
    reclaimable = sum(g.size * (len(g.paths) - 1) for g in result.groups)
    record = RunRecord(
        timestamp=time.time(),
        directories=[str(d) for d in directories],
        groups=len(result.groups),
        reclaimable_bytes=reclaimable,
        skipped=len(result.skipped),
        cancelled=result.cancelled,
        duration_seconds=duration_seconds,
    )
    _append(record)
    return record


def _append(record: RunRecord) -> None:
    records = load_history()
    records.append(record)
    records = records[-MAX_HISTORY_ENTRIES:]
    _save(records)


def _save(records: list[RunRecord]) -> None:
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in records], indent=2))
        tmp.replace(HISTORY_FILE)
    except OSError:
        pass


def load_history() -> list[RunRecord]:
    if not HISTORY_FILE.exists():
        return []
    try:
        raw = json.loads(HISTORY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return _parse_records(raw)


def _parse_records(raw: object) -> list[RunRecord]:
    if not isinstance(raw, list):
        raise ValueError("history file must contain a JSON list of run records")
    records = []
    for item in raw:
        try:
            records.append(RunRecord(**item))
        except TypeError:
            continue
    return records


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
    incoming = _parse_records(raw)

    existing = load_history()
    seen = {(r.timestamp, tuple(r.directories)) for r in existing}

    merged = list(existing)
    added = 0
    for record in incoming:
        key = (record.timestamp, tuple(record.directories))
        if key in seen:
            continue
        seen.add(key)
        merged.append(record)
        added += 1

    merged.sort(key=lambda r: r.timestamp)
    merged = merged[-MAX_HISTORY_ENTRIES:]
    _save(merged)
    return added
