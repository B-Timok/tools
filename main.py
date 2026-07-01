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
    python main.py                      # preview the default Downloads folder
    python main.py preview ~/Downloads  # preview a specific folder
    python main.py organize             # actually move files into folders
    python main.py clean                # apply cleanup rules
    python main.py clean --dry-run      # show what clean would do
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

# Installers older than this many days get moved to Archive/ during `clean`.
ARCHIVE_AGE_DAYS = 60
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

def preview(folder: Path) -> None:
    files = scan(folder)
    groups = group_by_category(files)

    print(f"Scanning: {folder}")
    print(f"Found {len(files)} files\n")

    if not files:
        print("Nothing to organize.")
        return

    # Print counts in the order categories are defined, then Other.
    order = list(CATEGORIES) + [OTHER]
    for category in order:
        if category in groups:
            print(f"{category}: {len(groups[category])}")

    print("\nWould move:")
    for category in order:
        for f in groups.get(category, []):
            print(f"  - {f.name} -> {category}/")


# ---------------------------------------------------------------------------
# Stage 2: organize
# ---------------------------------------------------------------------------

@dataclass
class ActionReport:
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    archived: list[tuple[Path, Path]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)


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


def clean(folder: Path, dry_run: bool = False, age_days: int = ARCHIVE_AGE_DAYS) -> ActionReport:
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
                if not dry_run:
                    f.unlink()
                report.deleted.append(f)
                prefix = "Would delete" if dry_run else "Deleted"
                print(f"  {prefix}: {f.name}")
            except OSError as exc:
                report.errors.append((f, str(exc)))
                print(f"  ERROR deleting {f.name}: {exc}", file=sys.stderr)

    # Rule 3: archive old installers. Check both loose files and an existing
    # Installers/ folder, so this works before or after `organize`.
    installer_sources = [folder]
    installers_dir = folder / "Installers"
    if installers_dir.is_dir():
        installer_sources.append(installers_dir)

    archive_dir = folder / ARCHIVE_DIR
    for source in installer_sources:
        for f in sorted(source.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            if categorize(f) != "Installers":
                continue
            age = _age_days(f, now)
            if age < age_days:
                continue
            dest = unique_destination(archive_dir / f.name)
            try:
                if not dry_run:
                    archive_dir.mkdir(exist_ok=True)
                    f.rename(dest)
                report.archived.append((f, dest))
                prefix = "Would archive" if dry_run else "Archived"
                print(f"  {prefix}: {f.name} -> {ARCHIVE_DIR}/ ({int(age)} days old)")
            except OSError as exc:
                report.errors.append((f, str(exc)))
                print(f"  ERROR archiving {f.name}: {exc}", file=sys.stderr)

    # Cleanup report.
    print("\n--- Cleanup report ---")
    print(f"  Deleted:  {len(report.deleted)} file(s)")
    print(f"  Archived: {len(report.archived)} installer(s) older than {age_days} days")
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

    p_organize = sub.add_parser("organize", help="move files into per-category folders")
    add_folder_arg(p_organize)
    p_organize.add_argument("--dry-run", action="store_true", help="show actions without moving")

    p_clean = sub.add_parser("clean", help="delete leftovers and archive old installers")
    add_folder_arg(p_clean)
    p_clean.add_argument("--dry-run", action="store_true", help="show actions without changing anything")
    p_clean.add_argument("--age-days", type=int, default=ARCHIVE_AGE_DAYS,
                         help=f"installer archive threshold in days (default: {ARCHIVE_AGE_DAYS})")

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
        preview(folder)
    elif command == "organize":
        organize(folder, dry_run=args.dry_run)
    elif command == "clean":
        clean(folder, dry_run=args.dry_run, age_days=args.age_days)
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
