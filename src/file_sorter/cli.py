from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from send2trash import send2trash

from .dedupe import (
    DEFAULT_EXCLUDED_DIR_NAMES,
    LARGE_FILE_THRESHOLD,
    VALID_FILE_TYPES,
    DuplicateGroup,
    default_workers,
    files_equal,
    scan_or_reuse,
)
from .folders import FolderGroup
from .formatting import human_size
from .store import (
    checkpoint_progress,
    clear_resume_state,
    export_history,
    import_history,
    latest_resume_run_id,
    load_history,
    load_resume_state,
    load_run_groups,
    record_run,
    resumable_run_ids,
    save_resume_state,
    save_run_groups,
)

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
        "--exclude",
        action="append",
        default=[],
        metavar="NAME",
        dest="exclude",
        help=(
            "Directory name to skip entirely wherever it's encountered (not just at the top "
            "level) -- repeatable. Added on top of the built-in defaults "
            f"({', '.join(sorted(DEFAULT_EXCLUDED_DIR_NAMES))}) unless --no-default-excludes is "
            "also given. Matched case-insensitively; a directory you pass directly as a scan "
            "target is always scanned regardless of its name."
        ),
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help=(
            "Don't skip the built-in default directories (node_modules, virtualenvs, "
            "interpreter/tool caches) or files (.DS_Store, Thumbs.db, editor swap/backup "
            "files, etc.) -- scan everything. Any --exclude names are still applied."
        ),
    )
    parser.add_argument(
        "--type",
        action="append",
        default=[],
        dest="file_types",
        choices=sorted(VALID_FILE_TYPES),
        metavar="CATEGORY",
        help=(
            "Only scan files of this type -- repeatable to combine categories. One of: "
            f"{', '.join(sorted(VALID_FILE_TYPES))}. Omit entirely to scan every file "
            "(the default). Matches by extension; a macOS .app is a directory, not a file, "
            "so 'programs' can't currently catch duplicate .app bundles."
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
        "--move-to",
        metavar="DIR",
        help=(
            "Move duplicates into DIR instead of deleting them -- keeps the first copy in "
            "each group in place (same convention as --delete) and moves the rest, "
            "mirroring each moved file's/folder's original absolute path underneath DIR "
            "(e.g. a file from D:\\Photos\\a.jpg lands at DIR\\D\\Photos\\a.jpg) so "
            "duplicates from different source folders never collide by name and stay easy "
            "to trace back. DIR is created if it doesn't exist. Mutually exclusive with "
            "--delete."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt before deleting/moving (only meaningful with --delete/--move-to)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview what --delete/--move-to would do without touching anything -- including "
            "running the pre-deletion verification for large-file groups, so the preview shows "
            "exactly which files would be skipped, not just what's planned. Implies --yes "
            "(nothing happens, so there's nothing to confirm). Only meaningful with "
            "--delete/--move-to."
        ),
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="LAST",
        default=None,
        metavar="N",
        help=(
            "Resume a scan that was cancelled before it finished, instead of starting a new one. "
            "With no value, resumes the most recently stopped run; pass the # index shown by "
            "--history (e.g. --resume 3) to resume a specific past stopped run instead."
        ),
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Show past run history instead of scanning",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Reload and print the full results of a past run (the # index shown by --history) "
            "instead of scanning -- no directories are re-read. Only available for a run made "
            "after this option was added; older runs kept only their summary."
        ),
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
    records = list(reversed(load_history()))
    if not records:
        print("No run history yet.")
        return
    resumable = resumable_run_ids()
    for index, record in enumerate(records, start=1):
        when = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
        if not record.cancelled:
            status = "done"
        elif str(record.timestamp) in resumable:
            status = "cancelled, resumable"
        else:
            status = "cancelled"
        print(f"#{index}  {when}  [{status}]  {', '.join(record.directories)}")
        print(
            f"    {record.groups} duplicate group(s), {human_size(record.reclaimable_bytes)} reclaimable, "
            f"{record.skipped} skipped, {record.duration_seconds:.1f}s"
        )


def _resolve_resume_run_id(resume_arg: str) -> tuple[Optional[str], Optional[str]]:
    """Turn `--resume`'s value into a resume_states.json run_id.

    `resume_arg` is "LAST" for a bare `--resume`, or the #index string from
    `--history`'s output for `--resume N`. Returns (run_id, error_message)
    -- exactly one of the two is not None.
    """
    if resume_arg == "LAST":
        run_id = latest_resume_run_id()
        if run_id is None:
            return None, "No stopped run to resume."
        return run_id, None

    try:
        index = int(resume_arg)
    except ValueError:
        return None, f"--resume expects the # index shown by --history, got {resume_arg!r}"

    records = list(reversed(load_history()))
    if index < 1 or index > len(records):
        return None, f"No run #{index} in history -- run --history to see valid indexes."

    record = records[index - 1]
    run_id = str(record.timestamp)
    if not record.cancelled or run_id not in resumable_run_ids():
        return None, f"Run #{index} has no saved progress to resume."
    return run_id, None


def _print_scan_results(
    groups: list[DuplicateGroup],
    folder_groups: list[FolderGroup],
    skipped_count: int,
    *,
    cancelled: bool,
) -> None:
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

    if not groups:
        print("No duplicates found." if not cancelled else "Scan cancelled before any duplicates were confirmed.")
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

        note = " (scan cancelled -- partial results)" if cancelled else ""
        print(f"\n{len(groups)} duplicate group(s), {human_size(total_wasted)} reclaimable{note}.")
        if deferred_count:
            print(
                f"{deferred_count} of those group(s) are large files not fully verified -- "
                "they will be confirmed before deletion, and a group could turn out to be a "
                "false match (files that only happen to share a size and partial hash)."
            )

    if skipped_count:
        print(f"\nSkipped {skipped_count} unreadable file(s) (permission denied or removed).")


def _show_past_run(index: int) -> int:
    records = list(reversed(load_history()))
    if index < 1 or index > len(records):
        print(f"No run #{index} in history -- run --history to see valid indexes.")
        return 1

    record = records[index - 1]
    run_id = str(record.timestamp)
    loaded = load_run_groups(run_id)
    if loaded is None:
        print(f"Run #{index} has no saved detailed results to show (only its summary is kept).")
        return 1

    groups, folder_groups = loaded
    when = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d %H:%M")
    print(f"Run #{index} -- {when} -- {', '.join(record.directories)}")
    _print_scan_results(groups, folder_groups, record.skipped, cancelled=record.cancelled)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.delete and args.move_to:
        print("Error: --delete and --move-to are mutually exclusive")
        return 1

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

    if args.show is not None:
        return _show_past_run(args.show)

    resume_state = None
    resume_run_id = None
    if args.resume is not None:
        if args.directories:
            print("Error: --resume picks up a stopped scan and doesn't take directories")
            return 1
        resume_run_id, error = _resolve_resume_run_id(args.resume)
        if error is not None:
            print(error)
            return 1
        resume_state = load_resume_state(resume_run_id)
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

    # Reused from --resume when picking up a stopped run, or freshly minted
    # otherwise, so mid-scan checkpoints (see on_checkpoint below) have
    # somewhere to save to from the very start -- not just once the scan
    # is actually cancelled -- and a hard crash or killed process loses at
    # most a checkpoint interval's worth of work on a large scan.
    run_id = resume_run_id if resume_run_id is not None else str(time.time())

    exclude_dirs = set(args.exclude) if args.no_default_excludes else DEFAULT_EXCLUDED_DIR_NAMES | set(args.exclude)
    exclude_temp_files = not args.no_default_excludes
    # args.file_types defaults to [] (argparse append), but find_duplicates
    # treats an empty collection as "match zero categories" (finds
    # nothing) rather than "no filter" -- None is what means "no filter"
    # there, so an empty list here must become None, not pass through.
    file_types = set(args.file_types) if args.file_types else None

    previous_handler = signal.signal(signal.SIGINT, handle_sigint)
    start = time.monotonic()
    try:
        # scan_or_reuse first checks (cheaply, via a stat-only walk) whether
        # these exact directories are unchanged since a previous scan; if
        # so, it replays that scan's saved results instead of rehashing
        # everything -- see there.
        result = scan_or_reuse(
            directories,
            run_id=run_id,
            show_progress=not args.quiet,
            cancel_event=cancel_event,
            resume_state=resume_state,
            workers=args.threads,
            large_file_threshold=args.large_threshold,
            on_checkpoint=lambda delta: checkpoint_progress(run_id, delta),
            exclude_dirs=exclude_dirs,
            exclude_temp_files=exclude_temp_files,
            file_types=file_types,
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    duration = time.monotonic() - start
    if result.reused_run_id is not None and not args.quiet:
        print("Nothing changed since the last scan of these directories -- reused those results.", file=sys.stderr)

    record_run(directories, result, duration, run_id=run_id)
    # Saved separately from the summary above so `--show` can later reload
    # and reprint this exact result without re-scanning -- see
    # `_show_past_run`. Uses the unfiltered result (not the --min-size
    # filtered lists below), so a run made with --min-size doesn't lose
    # the smaller groups it chose not to print this time.
    save_run_groups(run_id, result)
    # Whatever checkpoint(s) this run saved along the way (or, if resuming,
    # the older stopped run it consumed) are done with now -- either the
    # scan finished, or it's cancelled again and a fresh resume state is
    # saved right below under this same id.
    clear_resume_state(run_id)
    if result.cancelled and result.resume_state is not None:
        save_resume_state(run_id, result.resume_state)

    folder_groups = result.folder_groups
    if args.min_size:
        folder_groups = [g for g in folder_groups if g.size >= args.min_size]

    groups = result.groups
    if args.min_size:
        groups = [g for g in groups if g.size >= args.min_size]

    _print_scan_results(groups, folder_groups, len(result.skipped), cancelled=result.cancelled)

    if result.cancelled and result.resume_state is not None:
        print("\nRun 'file-sorter --resume' to continue this scan where it left off.")

    if args.delete and (groups or result.folder_groups):
        _delete_duplicates(groups, result.folder_groups, skip_confirmation=args.yes, dry_run=args.dry_run)

    if args.move_to and (groups or result.folder_groups):
        dest_root = Path(args.move_to).expanduser().resolve()
        _move_duplicates(
            groups,
            result.folder_groups,
            dest_root,
            skip_confirmation=args.yes,
            dry_run=args.dry_run,
        )

    return 0


def _plan_duplicates_to_remove(
    groups: list[DuplicateGroup],
    folder_groups: list[FolderGroup],
) -> tuple[list[tuple[FolderGroup, Path]], list[tuple[DuplicateGroup, Path]]]:
    """Pick which folder/file copies --delete and --move-to both act on.

    Only a *confirmed* folder group can be handled as a single unit -- every
    file inside one was already individually confirmed, so acting on the
    whole directory needs no further verification. An unconfirmed
    (deferred, large-file) folder group is left alone here entirely; its
    files fall through to the per-file plan below, which already verifies
    each one before it's touched.
    """
    folders_to_remove = [(fg, path) for fg in folder_groups if fg.confirmed for path in fg.paths[1:]]
    remove_dirs = [path for _fg, path in folders_to_remove]

    def under_any(path: Path, roots: list[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    # Same convention as the GUI: keep the first file in each group, act on
    # the rest -- but a file's own group.paths[0] pick is independent of
    # which *folder* copy is being kept, so re-derive "keep/remove" among
    # only the paths NOT already covered by a folder-level removal above,
    # rather than blindly trusting group.paths[0]/[1:]. Otherwise a file
    # whose file-level "kept" copy happens to sit in a folder being
    # bulk-removed could end up with every copy gone.
    files_to_remove: list[tuple[DuplicateGroup, Path]] = []
    for group in groups:
        remaining = [p for p in group.paths if not under_any(p, remove_dirs)]
        if len(remaining) < 2:
            continue  # already fully handled by a folder-level removal, or only one copy is left
        files_to_remove.extend((group, path) for path in remaining[1:])

    return folders_to_remove, files_to_remove


def _mirrored_path(path: Path, dest_root: Path) -> Path:
    """Where `path` lands under `dest_root`, preserving its full source hierarchy.

    Mirrors the drive/anchor too (e.g. D:\\Photos\\a.jpg -> dest_root/D/Photos/a.jpg,
    /home/user/a.jpg -> dest_root/home/user/a.jpg) so duplicates that happen to
    share a relative path under different scanned directories -- or different
    drives entirely -- never collide at the destination.
    """
    drive = path.drive.rstrip(":")
    tail = path.relative_to(path.anchor)
    return dest_root / drive / tail if drive else dest_root / tail


def _delete_duplicates(
    groups: list[DuplicateGroup],
    folder_groups: list[FolderGroup],
    *,
    skip_confirmation: bool,
    dry_run: bool = False,
) -> None:
    folders_to_delete, files_to_delete = _plan_duplicates_to_remove(groups, folder_groups)

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


def _move_duplicates(
    groups: list[DuplicateGroup],
    folder_groups: list[FolderGroup],
    dest_root: Path,
    *,
    skip_confirmation: bool,
    dry_run: bool = False,
) -> None:
    folders_to_move, files_to_move = _plan_duplicates_to_remove(groups, folder_groups)

    if not folders_to_move and not files_to_move:
        return

    if dry_run:
        print(f"\nDry run -- nothing will actually be moved to {dest_root}.")
    elif not skip_confirmation:
        parts = []
        if folders_to_move:
            parts.append(f"{len(folders_to_move)} folder(s)")
        if files_to_move:
            parts.append(f"{len(files_to_move)} file(s)")
        try:
            answer = input(f"\nMove {' and '.join(parts)} to {dest_root}? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("Aborted -- nothing moved.")
            return

    if not dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    moved_folders = 0
    moved_files = 0
    failures = []

    for _fg, path in folders_to_move:
        dest = _mirrored_path(path, dest_root)
        if dest.exists():
            failures.append(f"{path}: destination {dest} already exists -- skipped")
            continue
        if dry_run:
            print(f"  would move folder: {path} -> {dest}")
            moved_folders += 1
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved_folders += 1
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    for group, path in files_to_move:
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
                    f"{skip_verb} rather than risk moving a non-duplicate"
                )
                continue
        dest = _mirrored_path(path, dest_root)
        if dest.exists():
            failures.append(f"{path}: destination {dest} already exists -- skipped")
            continue
        if dry_run:
            print(f"  would move: {path} -> {dest}")
            moved_files += 1
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            moved_files += 1
        except OSError as exc:
            failures.append(f"{path}: {exc}")

    verb = "Would move" if dry_run else "Moved"
    print(f"\n{verb} {moved_folders} folder(s) and {moved_files} file(s) to {dest_root}.")
    if failures:
        label = "would not be moved" if dry_run else "were not moved"
        print(f"{len(failures)} item(s) {label}:")
        for line in failures:
            print(f"  {line}")


if __name__ == "__main__":
    raise SystemExit(main())
