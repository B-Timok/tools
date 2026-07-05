#!/usr/bin/env python3
"""Downloads organizer.

A small utility to keep your Downloads folder tidy. It works in three stages,
each safer to run than the last:

    preview   Scan and report what *would* happen. Touches nothing.
    organize  Sort files into per-category folders (PDFs/, Images/, ...).
    clean     Apply cleanup rules (drop leftovers, archive old installers).

Nothing is ever moved or deleted unless you ask for it. `preview` is the
default so an accidental run is harmless.

Examples:
    python downloads_organizer.py                      # preview ~/Downloads
    python downloads_organizer.py preview ~/Downloads  # preview a folder
    python downloads_organizer.py organize             # move files into folders
    python downloads_organizer.py clean                # apply cleanup rules
    python downloads_organizer.py clean --dry-run      # show what clean would do
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Map a category name to the file extensions that belong to it. Extensions are
# lowercase and include the leading dot. Anything not listed lands in "Other".
CATEGORIES: dict[str, set[str]] = {
    "PDFs": {".pdf"},
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".heic", ".tiff"},
    "Documents": {".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".pages",
                  ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".key"},
    "Archives": {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar", ".xz"},
    "Installers": {".dmg", ".pkg", ".msi", ".exe", ".deb", ".rpm", ".appimage"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "Video": {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv"},
}

OTHER = "Other"

# Files with these extensions are partial/temporary and safe to delete.
JUNK_EXTENSIONS: set[str] = {".crdownload", ".tmp", ".part", ".download"}

# Files not modified in this many days are considered stale and get moved to
# Archive/ during `clean`. This uses modification time (mtime), which in a
# Downloads folder is effectively the download date, since files here are rarely
# edited after they arrive. (Access time / "last opened" is not used: modern
# filesystems mount with relatime/noatime, so it isn't reliably updated on read.)
STALE_AGE_DAYS = 30
ARCHIVE_DIR = "Archive"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def categorize(path: Path) -> str:
    """Return the category name for a file based on its extension."""
    ext = path.suffix.lower()
    for category, extensions in CATEGORIES.items():
        if ext in extensions:
            return category
    return OTHER


def default_downloads_dir() -> Path:
    """Best guess at the user's Downloads folder."""
    return Path.home() / "Downloads"


def scan(folder: Path) -> list[Path]:
    """Return the top-level files in `folder`, skipping directories and hidden files."""
    files = []
    for entry in sorted(folder.iterdir()):
        if entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue  # leave dotfiles (.DS_Store, etc.) alone
        files.append(entry)
    return files


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def group_by_category(files: list[Path]) -> dict[str, list[Path]]:
    """Group files into {category: [paths]} preserving order."""
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(categorize(f), []).append(f)
    return groups


def human_size(nbytes: int) -> str:
    """Format a byte count as a short human-readable string (e.g. '2.1 GB')."""
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # unreachable, keeps type-checkers happy


def total_size(paths: list[Path]) -> int:
    """Sum the on-disk size of the given files, skipping any that can't be stat'd."""
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def unique_destination(dest: Path) -> Path:
    """Return a non-colliding path, appending ' (1)', ' (2)', ... if needed."""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ---------------------------------------------------------------------------
# Stage 1: preview
# ---------------------------------------------------------------------------

def preview(folder: Path, stale_days: int = STALE_AGE_DAYS) -> None:
    files = scan(folder)
    groups = group_by_category(files)

    print(f"Scanning: {folder}")
    print(f"Found {len(files)} files ({human_size(total_size(files))})\n")

    if not files:
        print("Nothing to organize.")
        return

    # Print counts and sizes in the order categories are defined, then Other.
    order = list(CATEGORIES) + [OTHER]
    for category in order:
        if category in groups:
            members = groups[category]
            print(f"{category}: {len(members)} ({human_size(total_size(members))})")

    print("\nWould move:")
    for category in order:
        for f in groups.get(category, []):
            print(f"  - {f.name} -> {category}/")

    # Highlight stale files (informational only — preview never changes anything).
    now = time.time()
    stale = [f for f in files if _age_days(f, now) >= stale_days]
    if stale:
        print(f"\nStale ({stale_days}+ days, would archive on `clean`): "
              f"{len(stale)} ({human_size(total_size(stale))})")
        for f in stale:
            print(f"  - {f.name} ({int(_age_days(f, now))} days) -> {ARCHIVE_DIR}/{categorize(f)}/")


# ---------------------------------------------------------------------------
# Stage 2: organize
# ---------------------------------------------------------------------------

@dataclass
class ActionReport:
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    archived: list[tuple[Path, Path]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    freed_bytes: int = 0      # size of deleted junk
    archived_bytes: int = 0   # size of files moved to Archive/


def organize(folder: Path, dry_run: bool = False) -> ActionReport:
    files = scan(folder)
    report = ActionReport()

    print(f"Organizing: {folder}")
    print(f"Found {len(files)} files\n")

    for f in files:
        category = categorize(f)
        dest_dir = folder / category
        dest = unique_destination(dest_dir / f.name)
        try:
            if not dry_run:
                dest_dir.mkdir(exist_ok=True)
                f.rename(dest)
            report.moved.append((f, dest))
            prefix = "Would move" if dry_run else "Moved"
            print(f"  {prefix}: {f.name} -> {category}/{dest.name}")
        except OSError as exc:
            report.errors.append((f, str(exc)))
            print(f"  ERROR moving {f.name}: {exc}", file=sys.stderr)

    verb = "Would move" if dry_run else "Moved"
    print(f"\n{verb} {len(report.moved)} files.")
    if report.errors:
        print(f"{len(report.errors)} error(s).")
    return report


# ---------------------------------------------------------------------------
# Stage 3: clean
# ---------------------------------------------------------------------------

def _age_days(path: Path, now: float) -> float:
    return (now - path.stat().st_mtime) / 86400


def managed_sources(folder: Path) -> list[Path]:
    """Folders `clean` sweeps for stale files: the top level plus any existing
    category folders (PDFs/, Images/, ...) and Other/. Archive/ is excluded so
    already-archived files are never re-archived. Works before or after
    `organize`, since it looks both at loose files and inside category folders."""
    sources = [folder]
    for name in list(CATEGORIES) + [OTHER]:
        sub = folder / name
        if sub.is_dir():
            sources.append(sub)
    return sources


def clean(folder: Path, dry_run: bool = False, stale_days: int = STALE_AGE_DAYS) -> ActionReport:
    report = ActionReport()
    now = time.time()

    print(f"Cleaning: {folder}\n")

    # Rule 1 & 2: delete leftover partial-download / temp files. Check both
    # loose files and an existing Other/ folder, so this works before or after
    # `organize` (which sweeps junk into Other/).
    junk_sources = [folder]
    other_dir = folder / OTHER
    if other_dir.is_dir():
        junk_sources.append(other_dir)

    for source in junk_sources:
        for f in sorted(source.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.suffix.lower() not in JUNK_EXTENSIONS:
                continue
            try:
                size = f.stat().st_size  # capture before deleting
                if not dry_run:
                    f.unlink()
                report.deleted.append(f)
                report.freed_bytes += size
                prefix = "Would delete" if dry_run else "Deleted"
                print(f"  {prefix}: {f.name} ({human_size(size)})")
            except OSError as exc:
                report.errors.append((f, str(exc)))
                print(f"  ERROR deleting {f.name}: {exc}", file=sys.stderr)

    # Rule 3: archive stale files. Any file not modified in `stale_days` days is
    # moved to Archive/ for review — never deleted. We sweep the top level and
    # every category folder (but not Archive/ itself), so this catches stale
    # files whether or not `organize` has run. Leftover junk was already handled
    # above; skip it here so a dry run doesn't double-count it.
    archive_dir = folder / ARCHIVE_DIR
    for source in managed_sources(folder):
        for f in sorted(source.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.suffix.lower() in JUNK_EXTENSIONS:
                continue
            age = _age_days(f, now)
            if age < stale_days:
                continue
            size = f.stat().st_size  # capture before moving
            # Archive into per-category subfolders (Archive/Images/, ...) so the
            # sorting from `organize` isn't flattened away for stale files.
            category = categorize(f)
            dest = unique_destination(archive_dir / category / f.name)
            try:
                if not dry_run:
                    (archive_dir / category).mkdir(parents=True, exist_ok=True)
                    f.rename(dest)
                report.archived.append((f, dest))
                report.archived_bytes += size
                prefix = "Would archive" if dry_run else "Archived"
                print(f"  {prefix}: {f.name} -> {ARCHIVE_DIR}/{category}/ ({int(age)} days old, {human_size(size)})")
            except OSError as exc:
                report.errors.append((f, str(exc)))
                print(f"  ERROR archiving {f.name}: {exc}", file=sys.stderr)

    # Cleanup report.
    freed_verb = "would free" if dry_run else "freed"
    print("\n--- Cleanup report ---")
    print(f"  Deleted:  {len(report.deleted)} file(s) ({human_size(report.freed_bytes)} {freed_verb})")
    print(f"  Archived: {len(report.archived)} stale file(s) older than {stale_days} days "
          f"({human_size(report.archived_bytes)})")
    if report.errors:
        print(f"  Errors:   {len(report.errors)}")
    if dry_run:
        print("  (dry run — nothing was actually changed)")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Organize and tidy your Downloads folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    def add_folder_arg(p):
        p.add_argument(
            "folder",
            nargs="?",
            type=Path,
            default=default_downloads_dir(),
            help="folder to operate on (default: ~/Downloads)",
        )

    p_preview = sub.add_parser("preview", help="scan and report; changes nothing")
    add_folder_arg(p_preview)
    p_preview.add_argument("--stale-days", type=int, default=STALE_AGE_DAYS,
                           help=f"flag files older than this many days (default: {STALE_AGE_DAYS})")

    p_organize = sub.add_parser("organize", help="move files into per-category folders")
    add_folder_arg(p_organize)
    p_organize.add_argument("--dry-run", action="store_true", help="show actions without moving")

    p_clean = sub.add_parser("clean", help="delete leftovers and archive stale files")
    add_folder_arg(p_clean)
    p_clean.add_argument("--dry-run", action="store_true", help="show actions without changing anything")
    p_clean.add_argument("--stale-days", type=int, default=STALE_AGE_DAYS,
                         help=f"archive files older than this many days (default: {STALE_AGE_DAYS})")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Default to the safe preview command when none is given.
    command = args.command or "preview"
    folder = getattr(args, "folder", None) or default_downloads_dir()

    if not folder.exists():
        print(f"Folder not found: {folder}", file=sys.stderr)
        return 1
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 1

    if command == "preview":
        preview(folder, stale_days=args.stale_days)
    elif command == "organize":
        organize(folder, dry_run=args.dry_run)
    elif command == "clean":
        clean(folder, dry_run=args.dry_run, stale_days=args.stale_days)
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
