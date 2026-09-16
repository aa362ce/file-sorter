from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

RESUME_DIR = Path.home() / ".file-sorter"
RESUME_FILE = RESUME_DIR / "resume_states.json"
MAX_RESUME_STATES = 50


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
    """

    directories: list[str]
    stage: str
    by_size: dict[str, list[str]] = field(default_factory=dict)
    by_partial: dict[str, list[str]] = field(default_factory=dict)
    by_full: dict[str, list[str]] = field(default_factory=dict)
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _load_all() -> dict[str, ResumeState]:
    if not RESUME_FILE.exists():
        return {}
    try:
        raw = json.loads(RESUME_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    states = {}
    for run_id, data in raw.items():
        try:
            states[run_id] = ResumeState(**data)
        except TypeError:
            continue
    return states


def _save_all(states: dict[str, ResumeState]) -> None:
    try:
        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RESUME_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({run_id: asdict(state) for run_id, state in states.items()}, indent=2))
        tmp.replace(RESUME_FILE)
    except OSError:
        pass


def save_resume_state(run_id: str, state: ResumeState) -> None:
    states = _load_all()
    states[run_id] = state
    if len(states) > MAX_RESUME_STATES:
        oldest_first = sorted(states.keys(), key=float)
        for stale_id in oldest_first[: len(states) - MAX_RESUME_STATES]:
            del states[stale_id]
    _save_all(states)


def load_resume_state(run_id: str) -> Optional[ResumeState]:
    return _load_all().get(run_id)


def clear_resume_state(run_id: str) -> None:
    states = _load_all()
    if run_id in states:
        del states[run_id]
        _save_all(states)


def resumable_run_ids() -> set[str]:
    return set(_load_all().keys())


def latest_resume_run_id() -> Optional[str]:
    states = _load_all()
    if not states:
        return None
    return max(states.keys(), key=float)
