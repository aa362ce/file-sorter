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

### Duplicate folders

A whole directory whose contents (recursively) exactly match another
directory's -- same relative files, each pair an exact duplicate -- is
reported as a duplicate folder, e.g. two full copies of a photo library
under different names. This costs no extra work: it's built entirely from
the file-level results above, since a directory can only match another one
if every file inside it already has a match somewhere in the scan. Nested
matches are collapsed -- if two folders match, their matching
subfolders aren't reported separately. A *confirmed* duplicate folder can
be deleted as a single unit with `--delete` (see above); its files are
still listed individually too, but purely for visibility -- deleting them
is handled through the folder entry, not file by file. An *unverified*
(large-file) folder match doesn't support bulk deletion at all -- its
files remain regular, individually deletable duplicate groups.

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

### Excluding directories and temp files

`node_modules`, virtualenvs (`venv`, `.venv`, `env`, `.env`, `virtualenv`),
and common interpreter/tool caches (`__pycache__`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, `.tox`, `.cache`) are skipped by default,
wherever they're encountered (not just at the top level) -- these are
always regenerable and near-guaranteed to bury real results under
enormous numbers of expected, uninteresting duplicates. A directory you
pass directly as a scan target is always scanned regardless of its name.

Individual OS/app marker and temp files are skipped the same way:
`.DS_Store`, `.localized`, `Thumbs.db`, `desktop.ini`, `.gitkeep`, editor
swap/backup files (`.tmp`, `.temp`, `.swp`, `.swo`, `.bak`, trailing `~`).
Some of these are deliberately identical everywhere they appear -- macOS
drops an empty `.localized` into every folder set to use localized
names, so without this a whole-drive scan would report it (and, worse,
every folder containing only it) as duplicates spanning most of the
filesystem, saying nothing real about wasted space.

```bash
file-sorter ~/projects --exclude dist --exclude .next   # add more dirs, on top of the defaults
file-sorter ~/projects --no-default-excludes             # scan everything -- dirs and files, defaults off
```

### Scanning specific file types

Restrict a scan to one or more categories by extension --
`images`, `audio`, `video`, `documents`, `archives`, `programs`, or
`misc` (anything not in one of the other categories, including files
with no extension at all):

```bash
file-sorter ~/Downloads --type images
file-sorter ~/Downloads --type images --type video   # combine categories
```

Omit `--type` entirely to scan every file (the default). Matches by
extension; a macOS `.app` is a directory, not a file, so `programs` can't
currently catch duplicate `.app` bundles.

### Deleting duplicates

By default `file-sorter` only reports what it finds. Add `--delete` to
also delete afterward -- the first file listed in each group is kept, the
rest are moved to the Trash (via `send2trash`, never permanently deleted):

```bash
file-sorter ~/Downloads --delete
file-sorter ~/Downloads --delete --yes       # skip the "are you sure?" prompt
file-sorter ~/Downloads --delete --dry-run   # preview what would be deleted, delete nothing
```

A large-file group that wasn't fully verified during the scan (see
`--large-threshold` above) is compared against its kept file right before
deletion, and skipped -- with a warning, nothing deleted -- if it turns out
not to actually match, rather than risk deleting a file that only
coincidentally shared a size and partial hash.

A confirmed duplicate folder (see below) is deleted as a single unit --
the whole redundant directory moves to the Trash in one operation, rather
than one file at a time -- and its files are correctly left out of the
individual file listing above. An unverified (large-file) folder isn't
bulk-deleted; its files are handled individually instead, same as any
other large-file group.

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

A cancelled scan saves its progress (which files it had already hashed or
confirmed) to `~/.file-sorter/resume_states.json`, keyed to that specific
run in history -- so several stopped scans (e.g. one per drive) can sit
there independently resumable at once; finishing or resuming one never
affects the others. Pick up the most recently stopped one with:

```bash
file-sorter --resume
```

`--history` numbers every run, newest first, and marks cancelled ones
that still have saved progress as `resumable` -- pass that number to
resume a specific past run instead of just the latest:

```bash
file-sorter --history
file-sorter --resume 3
```

Resuming reuses the same directories and skips re-hashing/re-confirming
files that were already handled, so only what hadn't been reached yet
gets (re)processed. `--resume` doesn't take directory arguments. A run's
saved progress is cleared once it's resumed (whether that attempt then
finishes or gets cancelled again, in which case the new attempt gets its
own saved state under its own history entry).

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

Reload and print a past run's full results, using the # index `--history`
shows, without re-scanning anything:

```bash
file-sorter --show 3
```

Only available for a run made after this option was added -- an older run,
or one merged in from `--import-history`, kept only its summary, and
`--show` says so rather than re-scanning.

## GUI

A PySide6 desktop app is available as an optional extra:

```bash
pip install -e ".[gui]"
file-sorter-gui
```

Add one or more directories. "Skip node_modules, virtualenvs, caches &
temp files" (checked by default) and "Only scan:" (a set of type
checkboxes, none checked by default -- meaning every file) both narrow
what a scan even looks at, the same as the CLI's
`--exclude`/`--no-default-excludes` and `--type`. Then click "Scan for
Duplicates" (runs off the UI thread, so
the window stays responsive; "Cancel" stops it early -- "Resume Last
Run" becomes enabled afterward to pick that scan back up). Duplicate
groups appear in the results tree as they're confirmed, not just once the
whole scan (including the folder-duplicate analysis that runs after --
see above) finishes -- so there's something to look at during a long scan
instead of a blank tree the entire time. That's a live preview only:
folder-covered files can't be correctly distinguished until folder
analysis completes, so once the scan finishes the tree is cleared and
rebuilt from the authoritative final result, which is when a duplicate
folder row appears and its files switch from individually-checkable to
informational. Then check the copies you want gone and "Delete Checked" --
files are moved to
the Trash (via `send2trash`), never permanently deleted. Each duplicate
group defaults to keeping the first copy and checking the rest, and the
app refuses to let every copy in a group be checked at once, so you can't
accidentally wipe out a file entirely. A group of very large files that
wasn't fully verified during the scan is labeled "(unverified -- large
file, checked before deletion)"; deleting a checked file from such a group
compares it against the kept file(s) first, and skips it (with a warning,
nothing deleted) rather than trust an unconfirmed match. A duplicate folder
(see above) shows up as its own "📁 Folder duplicate" row, listed above the
regular groups; a *confirmed* one is checkable the same way ("keep first,
check the rest") and checking a copy deletes the whole directory in one
action -- its files are shown underneath for visibility but aren't
separately checkable, so there's never a conflict between "keep this
folder" and "delete this one file inside it." An *unverified* (large-file)
folder's row is informational only, and its files remain regular,
individually checkable duplicate groups instead.

A scan can easily turn up thousands of groups -- more than anyone would
review by hand, and not free to render either -- so folder and file-level
groups are ranked together by how much space deleting all but one copy
would actually reclaim, and only the top 10 are ever shown (the status
line always states the true total group/folder count and total
reclaimable space regardless, so nothing's hidden from view, just not
rendered as individual rows past the top 10).

Once a scan finishes, "Filter results:" (another row of type checkboxes,
disabled until there's a finished result to filter) narrows which of
*that* scan's groups are eligible for that top-10 ranking -- unlike "Only
scan:" above, this never re-scans: it's a pure re-render of the same
result, so you can freely switch it back and forth to browse a completed
scan by type. Folder rows are never *filtered* by it, since a directory
doesn't have a single type the way a file does, but they still compete
for a top-10 slot on equal footing with file groups.

"History" shows past
runs from both the GUI and the CLI; a cancelled run with saved progress
is labeled "Cancelled (resumable)" and selecting it enables "Resume
Selected", which repopulates the directory list and continues that scan
-- independently of "Resume Last Run" on the main window, which always
targets the most recently stopped run. Selecting any run (done or
cancelled) enables "Load Selected", which reloads that run's full
results straight into the main window's results tree -- same top-10
ranking, filtering, and deletion as a fresh scan -- without scanning
anything; a run made before this existed, or one brought in via
"Import...", only has its summary, so loading it says so instead.
"Export..." / "Import..." use the same JSON file as `--export-history` /
`--import-history` -- and only ever carry that summary, not the
loadable detail, so a run round-tripped through export/import loses its
"Load Selected" data even though it keeps its history-list row.

## Releasing prebuilt executables

`.github/workflows/release-build.yml` builds standalone CLI and GUI
executables for both Windows and macOS with PyInstaller and attaches them
to a GitHub Release -- no Python install needed to run them.

To cut a release: push a tag matching `v*` (e.g. `git tag v0.2.0 && git
push origin v0.2.0`). This builds on both platforms and creates a GitHub
Release for that tag with all four files attached. To build without
releasing (e.g. to sanity-check a change), run the workflow manually from
the Actions tab (`workflow_dispatch`) -- it still uploads the executables
as workflow artifacts, just without creating a release.

Artifacts per platform:

- **Windows**: `file-sorter.exe` (CLI), `file-sorter-gui.exe` (GUI)
- **macOS**: `file-sorter-macos` (CLI), `file-sorter-gui-macos.zip`
  (GUI `.app` bundle, zipped since GitHub release assets can't be
  directories)

Neither build is code-signed or notarized (that needs a paid Apple
Developer account for macOS, and a code-signing certificate for Windows),
so:

- **macOS** will refuse to open `file-sorter-gui.app` with an
  "unidentified developer" warning. After unzipping, either right-click
  the app and choose "Open" (prompts once, then remembers), or run `xattr
  -cr file-sorter-gui.app` first to strip the quarantine flag that
  triggers the warning.
- **Windows** SmartScreen may show a similar "Windows protected your PC"
  prompt the first time; "More info" -> "Run anyway" bypasses it.

The macOS build only targets the runner's native architecture (Apple
Silicon/arm64, since GitHub's `macos-latest` runners moved off Intel) --
it isn't a universal2 binary. It should still run on an Intel Mac via
Rosetta 2, but hasn't been verified there.
