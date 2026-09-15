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

Each directory is scanned recursively. Output groups files that are exact
duplicates (by content), along with total reclaimable space.
