# file-sorter

Finds duplicate files across one or more directories.

Duplicates are found with a staged lookup table rather than comparing every
file to every other file:

1. **Size** — group files by `stat()` size (free). Sizes with only one file
   can't have a duplicate and are dropped.
2. **Partial hash** — for files sharing a size, hash just the first 8KB.
   Cheap, and filters out almost everything that isn't a real duplicate.
3. **Full hash** — only files that still match on size *and* partial hash
   get fully hashed (SHA-256) to confirm they're byte-for-byte identical.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

As an installed command:

```bash
file-sorter ~/Downloads ~/Desktop
file-sorter ~/Downloads --min-size 1024
```

Or as a module, without installing:

```bash
python -m file_sorter ~/Downloads ~/Desktop
```

Each directory is scanned recursively, including into symlinked
directories (e.g. an iCloud/Dropbox-synced folder) -- symlink cycles are
detected and safely skipped. Symlinked *files* are not followed, since a
symlink to a file elsewhere isn't a real duplicate on disk. Output groups
files that are exact duplicates (by content), along with total
reclaimable space. Unreadable files (permission-protected, removed
mid-scan) are skipped and reported rather than aborting the scan.

### Progress and logging

A live progress line (files scanned, then quick-hash and full-hash
progress) is shown on stderr by default — pass `-q`/`--quiet` to suppress
it. Use `-v` for stage-level info logs, or `-vv` for per-file debug logs
(e.g. which files were skipped and why):

```bash
file-sorter ~ -v
file-sorter ~ -q
```

Press Ctrl+C to cancel a running scan -- the first press asks the scan to
stop after its current file and reports whatever was already confirmed;
a second press force-quits immediately.

### History

Every run (CLI or GUI) is logged to `~/.file-sorter/history.json`. View it
with:

```bash
file-sorter --history
```

Export it to a file, or import one back in (merges with what's already
there -- re-importing the same file is a no-op, matched on timestamp +
directories):

```bash
file-sorter --export-history backup.json
file-sorter --import-history backup.json
```

## GUI

A PySide6 desktop app is available as an optional extra:

```bash
pip install -e ".[gui]"
file-sorter-gui
```

Add one or more directories, click "Scan for Duplicates" (runs off the UI
thread, so the window stays responsive; "Cancel" stops it early), then
check the copies you want gone and "Delete Checked" -- files are moved to
the Trash (via `send2trash`), never permanently deleted. Each duplicate
group defaults to keeping the first copy and checking the rest, and the
app refuses to let every copy in a group be checked at once, so you can't
accidentally wipe out a file entirely. "History" shows past runs from
both the GUI and the CLI, with "Export..." / "Import..." buttons for the
same JSON file used by `--export-history` / `--import-history`.
