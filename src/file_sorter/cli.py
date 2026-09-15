from __future__ import annotations

import argparse
from pathlib import Path

from .dedupe import find_duplicates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-sorter",
        description="Find duplicate files across one or more directories.",
    )
    parser.add_argument(
        "directories",
        nargs="+",
        help="Directories to scan for duplicates (searched recursively)",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=0,
        metavar="BYTES",
        help="Ignore files smaller than this many bytes (default: 0)",
    )
    return parser


def _human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    directories = []
    for raw in args.directories:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            print(f"Error: {path} is not a directory")
            return 1
        directories.append(path)

    groups = find_duplicates(directories)
    if args.min_size:
        groups = [g for g in groups if g.size >= args.min_size]

    if not groups:
        print("No duplicates found.")
        return 0

    total_wasted = 0
    for group in groups:
        wasted = group.size * (len(group.paths) - 1)
        total_wasted += wasted
        print(
            f"\n{len(group.paths)} copies, {_human_size(group.size)} each "
            f"(sha256 {group.file_hash[:12]}...):"
        )
        for path in group.paths:
            print(f"  {path}")

    print(f"\n{len(groups)} duplicate group(s), {_human_size(total_wasted)} reclaimable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
