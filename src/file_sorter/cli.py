from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

from .dedupe import LARGE_FILE_THRESHOLD, DuplicateGroup, default_workers, files_equal, find_duplicates
from .folders import FolderGroup
from .formatting import human_size
from .history import export_history, import_history, load_history, record_run
from .resume import clear_resume_state, load_resume_state, save_resume_state

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
        "-j",
        "--threads",
        type=int,
        default=0,
        metavar="N",
        help=f"Number of threads to hash files with (default: {default_workers()}, one per CPU core)",
    )
    parser.add_argument(
        "--large-threshold",
        type=int,
        default=LARGE_FILE_THRESHOLD,
        metavar="BYTES",
        help=(
            "Files at or above this size are reported as probable duplicates "
            "(matched by size + partial hash) without being fully compared during "
            f"the scan -- confirmation is deferred until deletion (default: {LARGE_FILE_THRESHOLD}, "
            "i.e. 500MB; pass 0 to always fully confirm during the scan)"
        ),
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help=(
            "Delete duplicates after scanning -- keeps the first copy in each group and "
            "moves the rest to the Trash (via send2trash, never permanently deleted), same "
            "convention as the GUI. A confirmed duplicate folder is deleted as a single unit "
            "(its files are skipped individually below); a large-file group not verified "
            "during the scan is compared against its kept file right before deletion, and "
            "skipped (with a warning, nothing deleted) if it doesn't actually match. Prompts "
            "for confirmation unless -y/--yes is given."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt before deleting (only meaningful with --delete)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview what --delete would do without deleting anything -- including running "
            "the pre-deletion verification for large-file groups, so the preview shows "
            "exactly which files would be skipped, not just what's planned. Implies --yes "
            "(nothing is deleted, so there's nothing to confirm). Only meaningful with --delete."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the last scan that was cancelled before it finished, instead of starting a new one",
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

    resume_state = None
    if args.resume:
        if args.directories:
            print("Error: --resume picks up the last stopped scan and doesn't take directories")
            return 1
        resume_state = load_resume_state()
        if resume_state is None:
            print("No stopped run to resume.")
            return 1
        directories = [Path(d) for d in resume_state.directories]
        for path in directories:
            if not path.is_dir():
                print(f"Error: cannot resume -- {path} is no longer a directory")
                return 1
    else:
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
        result = find_duplicates(
            directories,
            show_progress=not args.quiet,
            cancel_event=cancel_event,
            resume_state=resume_state,
            workers=args.threads,
            large_file_threshold=args.large_threshold,
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    duration = time.monotonic() - start

    record_run(directories, result, duration)
    if result.cancelled and result.resume_state is not None:
        save_resume_state(result.resume_state)
    else:
        clear_resume_state()

    folder_groups = result.folder_groups
    if args.min_size:
        folder_groups = [g for g in folder_groups if g.size >= args.min_size]

    if folder_groups:
        for fg in folder_groups:
            label = (
                f"({fg.file_count} file(s), {human_size(fg.size)} each)"
                if fg.confirmed
                else f"({fg.file_count} file(s), {human_size(fg.size)} each, NOT VERIFIED -- large file(s))"
            )
            print(f"\nFolder duplicate: {len(fg.paths)} copies {label}:")
            for path in fg.paths:
                print(f"  {path}")
        print(
            f"\n{len(folder_groups)} duplicate folder(s) found -- their files are also listed "
            "individually below. --delete removes a confirmed one as a single unit; an "
            "unverified (large-file) one is handled file by file instead."
        )

    groups = result.groups
    if args.min_size:
        groups = [g for g in groups if g.size >= args.min_size]

    if not groups:
        print("No duplicates found." if not result.cancelled else "Scan cancelled before any duplicates were confirmed.")
    else:
        total_wasted = 0
        deferred_count = 0
        for group in groups:
            wasted = group.size * (len(group.paths) - 1)
            total_wasted += wasted
            if group.confirmed:
                label = f"(sha256 {group.file_hash[:12]}...)"
            else:
                deferred_count += 1
                label = "(NOT VERIFIED -- large file, matched by size + partial hash only)"
            print(f"\n{len(group.paths)} copies, {human_size(group.size)} each {label}:")
            for path in group.paths:
                print(f"  {path}")

        note = " (scan cancelled -- partial results)" if result.cancelled else ""
        print(f"\n{len(groups)} duplicate group(s), {human_size(total_wasted)} reclaimable{note}.")
        if deferred_count:
            print(
                f"{deferred_count} of those group(s) are large files not fully verified -- "
                "they will be confirmed before deletion, and a group could turn out to be a "
                "false match (files that only happen to share a size and partial hash)."
            )

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} unreadable file(s) (permission denied or removed).")

    if result.cancelled and result.resume_state is not None:
        print("\nRun 'file-sorter --resume' to continue this scan where it left off.")

    if args.delete and (groups or result.folder_groups):
        _delete_duplicates(groups, result.folder_groups, skip_confirmation=args.yes, dry_run=args.dry_run)

    return 0


def _delete_duplicates(
    groups: list[DuplicateGroup],
    folder_groups: list[FolderGroup],
    *,
    skip_confirmation: bool,
    dry_run: bool = False,
) -> None:
    # Only a *confirmed* folder group can be deleted as a single unit --
    # every file inside one was already individually confirmed, so trashing
    # the whole directory needs no further verification. An unconfirmed
    # (deferred, large-file) folder group is left alone here entirely; its
    # files fall through to the per-file plan below, which already
    # verifies each one before deleting it.
    folders_to_delete = [(fg, path) for fg in folder_groups if fg.confirmed for path in fg.paths[1:]]
    delete_dirs = [path for _fg, path in folders_to_delete]

    def under_any(path: Path, roots: list[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    # Same convention as the GUI: keep the first file in each group, delete
    # the rest -- but a file's own group.paths[0] pick is independent of
    # which *folder* copy is being kept, so re-derive "keep/delete" among
    # only the paths NOT already covered by a folder-level deletion above,
    # rather than blindly trusting group.paths[0]/[1:]. Otherwise a file
    # whose file-level "kept" copy happens to sit in a folder being
    # bulk-deleted could end up with every copy removed.
    files_to_delete: list[tuple[DuplicateGroup, Path]] = []
    for group in groups:
        remaining = [p for p in group.paths if not under_any(p, delete_dirs)]
        if len(remaining) < 2:
            continue  # already fully handled by a folder-level deletion, or only one copy is left
        files_to_delete.extend((group, path) for path in remaining[1:])

    if not folders_to_delete and not files_to_delete:
        return

    if dry_run:
        print("\nDry run -- nothing will actually be deleted.")
    elif not skip_confirmation:
        parts = []
        if folders_to_delete:
            parts.append(f"{len(folders_to_delete)} folder(s)")
        if files_to_delete:
            parts.append(f"{len(files_to_delete)} file(s)")
        try:
            answer = input(f"\nMove {' and '.join(parts)} to Trash? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("Aborted -- nothing deleted.")
            return

    deleted_folders = 0
    deleted_files = 0
    failures = []

    for _fg, path in folders_to_delete:
        if dry_run:
            print(f"  would delete folder: {path}")
            deleted_folders += 1
            continue
        try:
            send2trash(str(path))
            deleted_folders += 1
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    for group, path in files_to_delete:
        if not group.confirmed:
            try:
                verified = files_equal(group.paths[0], path)
            except OSError as exc:
                failures.append(f"{path}: could not verify against kept file: {exc}")
                continue
            if not verified:
                skip_verb = "would be skipped" if dry_run else "skipped"
                failures.append(
                    f"{path}: not verified as an actual duplicate of the kept file -- "
                    f"{skip_verb} rather than risk deleting a non-duplicate"
                )
                continue
        if dry_run:
            print(f"  would delete: {path}")
            deleted_files += 1
            continue
        try:
            send2trash(str(path))
            deleted_files += 1
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    verb = "Would delete" if dry_run else "Deleted"
    print(f"\n{verb} {deleted_folders} folder(s) and {deleted_files} file(s) to Trash.")
    if failures:
        label = "would not be deleted" if dry_run else "were not deleted"
        print(f"{len(failures)} item(s) {label}:")
        for line in failures:
            print(f"  {line}")


if __name__ == "__main__":
    raise SystemExit(main())
