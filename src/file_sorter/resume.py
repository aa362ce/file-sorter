from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

RESUME_DIR = Path.home() / ".file-sorter"
RESUME_FILE = RESUME_DIR / "resume_state.json"


@dataclass
class ResumeState:
    """A snapshot of an interrupted scan, saved so it can be picked up again
    without redoing work that was already done.

    `stage` is the stage that was in progress when the scan was cancelled:
    "scanning" (stage 1, walking directories), "quick_hash" (stage 2,
    partial hashing) or "full_hash" (stage 3, full hashing). `by_size` /
    `by_partial` / `by_full` hold whatever results that stage (and any
    earlier stage) had already produced, keyed as strings since JSON object
    keys can't be ints or tuples. `processed` lists the files already
    handled within the interrupted stage, so a resume only re-does the
    remainder of that stage's candidates.
    """

    directories: list[str]
    stage: str
    by_size: dict[str, list[str]] = field(default_factory=dict)
    by_partial: dict[str, list[str]] = field(default_factory=dict)
    by_full: dict[str, list[str]] = field(default_factory=dict)
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def save_resume_state(state: ResumeState) -> None:
    try:
        RESUME_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RESUME_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2))
        tmp.replace(RESUME_FILE)
    except OSError:
        pass


def load_resume_state() -> Optional[ResumeState]:
    if not RESUME_FILE.exists():
        return None
    try:
        raw = json.loads(RESUME_FILE.read_text())
        return ResumeState(**raw)
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def has_resume_state() -> bool:
    return RESUME_FILE.exists()


def clear_resume_state() -> None:
    try:
        RESUME_FILE.unlink()
    except OSError:
        pass
