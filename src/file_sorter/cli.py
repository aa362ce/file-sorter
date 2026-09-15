from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .dedupe import find_duplicates
from .formatting import human_size
from .history import export_history, import_history, load_history, record_run

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="file-sorter",
        description="Find duplicate files across one or more directories.",
    )
    parser.add_argument(
        "directories",
        nargs="*",
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
    parser.add_argument(
        "--history",
        action="store_true",
        help="Show past run history instead of scanning",
    )
    parser.add_argument(
        "--export-history",
        metavar="PATH",
        help="Export run history to a JSON file instead of scanning",
    )
    parser.add_argument(
        "--import-history",
        metavar="PATH",
        help="Import run history from a JSON file instead of scanning (merges with existing)",
    )
    return parser


def _print_history() -> None:
    records = load_history()
    if not records:
        print("No run history yet.")
        return
    for record in records:
        when = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
        status = "cancelled" if record.cancelled else "done"
        print(f"{when}  [{status}]  {', '.join(record.directories)}")
        print(
            f"    {record.groups} duplicate group(s), {human_size(record.reclaimable_bytes)} reclaimable, "
            f"{record.skipped} skipped, {record.duration_seconds:.1f}s"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    log_level = logging.WARNING
    if args.verbose == 1:
        log_level = logging.INFO
    elif args.verbose >= 2:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if args.history:
        _print_history()
        return 0

    if args.export_history:
        path = Path(args.export_history).expanduser().resolve()
        count = export_history(path)
        print(f"Exported {count} run(s) to {path}")
        return 0

    if args.import_history:
        path = Path(args.import_history).expanduser().resolve()
        if not path.is_file():
            print(f"Error: {path} is not a file")
            return 1
        try:
            added = import_history(path)
        except (OSError, ValueError) as exc:
            print(f"Error importing history: {exc}")
            return 1
        print(f"Imported {added} new run(s) from {path}")
        return 0

    if not args.directories:
        build_parser().error("the following arguments are required: directories")

    directories = []
    for raw in args.directories:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            print(f"Error: {path} is not a directory")
            return 1
        directories.append(path)

    cancel_event = threading.Event()

    def handle_sigint(signum, frame):
        if cancel_event.is_set():
            print("\nForce quitting.", file=sys.stderr)
            raise SystemExit(130)
        cancel_event.set()
        print("\nCancelling... (press Ctrl+C again to force quit)", file=sys.stderr)

    previous_handler = signal.signal(signal.SIGINT, handle_sigint)
    start = time.monotonic()
    try:
        result = find_duplicates(directories, show_progress=not args.quiet, cancel_event=cancel_event)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    duration = time.monotonic() - start

    record_run(directories, result, duration)

    groups = result.groups
    if args.min_size:
        groups = [g for g in groups if g.size >= args.min_size]

    if not groups:
        print("No duplicates found." if not result.cancelled else "Scan cancelled before any duplicates were confirmed.")
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

        note = " (scan cancelled -- partial results)" if result.cancelled else ""
        print(f"\n{len(groups)} duplicate group(s), {human_size(total_wasted)} reclaimable{note}.")

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} unreadable file(s) (permission denied or removed).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
