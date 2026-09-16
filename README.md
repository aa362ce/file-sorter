# file-sorter

Finds duplicate files across one or more directories.

Duplicates are found with a staged lookup table rather than comparing every
file to every other file:

1. **Size** — group files by `stat()` size (free). Sizes with only one file
   can't have a duplicate and are dropped.
2. **Partial hash** — for files sharing a size, hash just the first 8KB.
   Cheap, and filters out almost everything that isn't a real duplicate.
3. **Confirm** — files that still match on size *and* partial hash are
   confirmed byte-for-byte identical by comparing their content directly
   (not by hashing each one fully and comparing digests), reading only as
   far as the first difference for files that turn out not to match. A
   confirmed group is identified by a SHA-256 hash of one of its files,
   computed once per group rather than once per candidate.

The partial-hash and confirmation stages process multiple files at once
across a thread pool, defaulting to one thread per CPU core. Override the
count with `-j`/`--threads` (e.g. `--threads 1` to hash/compare
sequentially).

Very large files (500MB+ by default) skip the confirm step during the scan
entirely -- comparing multi-gigabyte files is expensive, and wasted work if
they're never actually deleted. Such a group is reported as unverified
(matched by size + partial hash only) and gets fully confirmed the moment
one of its files is actually about to be deleted, not before -- see
"Deleting" below. Override the cutoff with `--large-threshold BYTES`, or
`--large-threshold 0` to always confirm during the scan regardless of size.

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

### Deleting duplicates

By default `file-sorter` only reports what it finds. Add `--delete` to
also delete afterward -- the first file listed in each group is kept, the
rest are moved to the Trash (via `send2trash`, never permanently deleted):

```bash
file-sorter ~/Downloads --delete
file-sorter ~/Downloads --delete --yes   # skip the "are you sure?" prompt
```

A large-file group that wasn't fully verified during the scan (see
`--large-threshold` above) is compared against its kept file right before
deletion, and skipped -- with a warning, nothing deleted -- if it turns out
not to actually match, rather than risk deleting a file that only
coincidentally shared a size and partial hash.

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

### Resuming a stopped scan

A cancelled scan saves its progress (which files it had already hashed)
to `~/.file-sorter/resume_state.json`. Pick up where it left off with:

```bash
file-sorter --resume
```

This reuses the same directories and skips re-hashing files that were
already confirmed or ruled out, so only the files that hadn't been
reached yet get (re)processed. `--resume` doesn't take directory
arguments -- it always continues the most recently stopped scan. A scan
that finishes normally (not cancelled) clears any saved resume state.

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
thread, so the window stays responsive; "Cancel" stops it early -- "Resume
Last Run" becomes enabled afterward to pick that scan back up), then
check the copies you want gone and "Delete Checked" -- files are moved to
the Trash (via `send2trash`), never permanently deleted. Each duplicate
group defaults to keeping the first copy and checking the rest, and the
app refuses to let every copy in a group be checked at once, so you can't
accidentally wipe out a file entirely. A group of very large files that
wasn't fully verified during the scan is labeled "(unverified -- large
file, checked before deletion)"; deleting a checked file from such a group
compares it against the kept file(s) first, and skips it (with a warning,
nothing deleted) rather than trust an unconfirmed match. "History" shows
past runs from both the GUI and the CLI, with "Export..." / "Import..."
buttons for the same JSON file used by `--export-history` /
`--import-history`.
