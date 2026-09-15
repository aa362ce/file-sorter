from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .dedupe import find_duplicates
from .formatting import human_size

logger = logging.getLogger(__name__)


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
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v for stage info, -vv for per-file debug logs)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress the live progress display",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose >= 2:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    directories = []
    for raw in args.directories:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            print(f"Error: {path} is not a directory")
            return 1
        directories.append(path)

    result = find_duplicates(directories, show_progress=not args.quiet)
    groups = result.groups
    if args.min_size:
        groups = [g for g in groups if g.size >= args.min_size]

    if not groups:
        print("No duplicates found.")
    else:
        total_wasted = 0
        for group in groups:
            wasted = group.size * (len(group.paths) - 1)
            total_wasted += wasted
            print(
                f"\n{len(group.paths)} copies, {human_size(group.size)} each "
                f"(sha256 {group.file_hash[:12]}...):"
            )
            for path in group.paths:
                print(f"  {path}")

        print(f"\n{len(groups)} duplicate group(s), {human_size(total_wasted)} reclaimable.")

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} unreadable file(s) (permission denied or removed).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
