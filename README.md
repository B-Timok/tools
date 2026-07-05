# tools

A small collection of personal command-line utilities.

## Tools in this repo

| Tool | What it does |
|------|--------------|
| [`downloads_organizer.py`](#downloads-organizer-downloads_organizerpy) | Sorts your Downloads folder by file type, archives stale files, and clears leftovers — preview-first and safe. |
| [`system_auditor.py`](#system-auditor-system_auditorpy) | Audits what's slowing your machine down: Windows auto-start entries (via WSL interop) and WSL disk bloat. |

All tools are single-file Python 3 scripts with no third-party dependencies,
and all default to **reporting before changing anything**.

## Downloads organizer (`downloads_organizer.py`)

Keeps your Downloads folder tidy. It works in three stages, each safer to run
than the last, and **never** moves or deletes anything unless you explicitly
ask it to. Running with no arguments just previews — an accidental run is
harmless.

### Usage

```bash
python3 downloads_organizer.py                      # preview ~/Downloads (default, safe)
python3 downloads_organizer.py preview ~/Downloads  # preview a specific folder
python3 downloads_organizer.py organize             # move files into per-category folders
python3 downloads_organizer.py organize --dry-run   # show what organize would do
python3 downloads_organizer.py clean                # delete leftovers, archive stale files
python3 downloads_organizer.py clean --dry-run      # show what clean would do
python3 downloads_organizer.py clean --stale-days 90 # change the stale-file threshold
```

Recommended workflow the first time: `preview` → `organize` → `clean`.

### What each command does

**`preview`** — scans the folder, groups files by type, prints what *would*
happen, and flags stale files. Touches nothing.

```
Found 42 files (6.7 GB)

PDFs: 8 (120.4 MB)
Images: 12 (340.1 MB)
Archives: 5 (1.2 GB)
Installers: 4 (4.9 GB)
Other: 13 (180.3 MB)

Would move:
  - resume.pdf -> PDFs/
  - photo.png -> Images/
  - node-v22.msi -> Installers/

Stale (30+ days, would archive on `clean`): 3 (2.1 GB)
  - old-installer.msi (74 days) -> Archive/Installers/
  - meeting-notes.pdf (41 days) -> Archive/PDFs/
  - screenshot.png (33 days) -> Archive/Images/
```

Every count comes with a size total, so you can see at a glance where the
space is actually going — the `Stale` and `Installers` lines are usually where
the fat lives.

**`organize`** — creates category folders as needed and moves each file into
the right one. Name collisions are handled automatically (`resume.pdf` becomes
`resume (1).pdf` if one already exists).

**`clean`** — applies cleanup rules:

- Deletes leftover partial downloads (`.crdownload`, `.tmp`, `.part`,
  `.download`).
- Moves **stale files** — anything not modified in 30 days by default
  (configurable with `--stale-days`) — into per-category subfolders under
  `Archive/` (e.g. `Archive/Images/`), so the sorting from `organize` is kept.
  Stale files are *archived, never deleted*, so you can review and remove
  them yourself.
- Prints a summary report.

`clean` sweeps the top level and every category folder (but never `Archive/`
itself), so it works whether or not you've already run `organize`, and
re-running it is safe.

#### How "stale" is measured

Staleness uses each file's **modification time** (`mtime`). In a Downloads
folder that's effectively the download date, since files here are rarely edited
after they arrive — so "stale" means "downloaded a while ago and never
touched." Last-*access* time ("last opened") is deliberately **not** used:
modern filesystems mount with `relatime`/`noatime` and don't reliably update it
on reads, so it would give inconsistent results. The tradeoff is that a file
you re-read often but never edit still counts as stale — which is why stale
files are only archived for review, never deleted.

### Categories

Files are sorted by extension into: **PDFs, Images, Documents, Archives,
Installers, Audio, Video**, and anything unrecognized goes to **Other**. Edit
the `CATEGORIES` dictionary at the top of `downloads_organizer.py` to customize.

### Safety notes

- Dotfiles (e.g. `.DS_Store`) are always left alone.
- The category folders the tool manages are never re-sorted, so re-running is
  safe.
- Use `--dry-run` on `organize` / `clean` any time you want to see the plan
  before committing to it.

## System auditor (`system_auditor.py`)

Finds what's quietly slowing your machine down. Like the organizer, it's
**audit-only**: it reports and flags but never disables a startup entry or
deletes anything itself. Two subcommands:

```bash
python3 system_auditor.py startup            # audit Windows auto-start + flag new
python3 system_auditor.py startup --no-save  # audit without updating the baseline
python3 system_auditor.py startup --json     # machine-readable output
python3 system_auditor.py wsl                # WSL/Ubuntu disk-bloat report
```

### `startup` — Windows auto-start audit

Designed to run from your WSL/Ubuntu terminal: under the hood it shells out to
Windows via `powershell.exe` interop, so it audits the **Windows** side (where
the things that actually slow your boot live) while you keep working the way you
normally do. It also runs on native Windows.

It inventories every auto-start source —

- **Registry Run keys** (`HKCU`/`HKLM`, incl. `WOW6432Node` and `RunOnce`)
- **Startup folders** (user + all-users)
- **Scheduled tasks** triggered at logon or boot
- **Auto-start services**

— then **diffs against the last run** and highlights what's *new* or *gone*
since you last checked. New persistence you didn't add is exactly what
installers (and malware) leave behind, so this catches bloat and is a light
security win too. The baseline is stored at
`~/.local/state/system_auditor/startup_snapshot.json`; the first run just saves
it, and each later run compares against it (use `--no-save` to peek without
updating).

Example output:

```
Windows startup audit — 48 auto-start entries

Registry Run keys: 3
  - Steam: "C:\Program Files (x86)\Steam\steam.exe" -silent
  - OneDrive: "C:\Users\brandon\AppData\Local\Microsoft\OneDrive\OneDrive.exe" /background
  - SketchyUpdater: C:\Users\brandon\AppData\Roaming\upd\updater.exe

Startup folders: 1
  - Spotify.lnk: C:\Users\brandon\AppData\Roaming\Microsoft\Windows\Start Menu\...

Scheduled tasks (logon/boot): 2
  - GoogleUpdateTaskMachineUA: Ready
  - MicrosoftEdgeUpdateTaskMachineCore: Ready

Auto-start services: 42
  - Spooler: Print Spooler [Running]
  - WSearch: Windows Search [Running]
  ...

--- Changes since last check ---
  New:  1 entry
    + [Registry Run keys] SketchyUpdater: C:\Users\brandon\AppData\Roaming\upd\updater.exe
  Gone: 1 entry
    - [Auto-start services] SomeVendorHelper

(Audit only — to disable an entry, use Task Manager > Startup, `msconfig`, or `Disable-ScheduledTask`.)
```

The `Changes since last check` block is the payoff: `SketchyUpdater` appeared
in a Run key since the last audit — exactly the kind of thing an installer adds
silently.

Reading requires no admin; a few service/task details may be blank without an
elevated shell. To actually disable something, use Task Manager > Startup,
`msconfig`, or `Disable-ScheduledTask`.

### `wsl` — disk-bloat report

Runs inside WSL/Ubuntu and reports where disk space is going, with a
copy-paste cleanup command for each:

- apt package cache, system logs (journal), Docker data
- pip / npm / `~/.cache` caches
- the `ext4.vhdx` virtual disk, which **grows but never auto-shrinks** — it
  reports the size (via interop) and the `wsl --manage --set-sparse` /
  `Optimize-VHD` recipe to reclaim it

Example output:

```
WSL / Linux disk report

Filesystem (/): 41.2 GB used of 250.0 GB  (208.8 GB free)

Reclaimable space:
  apt package cache: 512.3 MB
      -> sudo apt clean
  system logs: 88.4 MB
      -> sudo journalctl --vacuum-time=7d
  Docker data: 6.2 GB
      -> docker system prune -a
  pip cache: 340.1 MB
      -> pip cache purge
  npm cache: 117.3 MB
      -> npm cache clean --force
  user cache (~/.cache): 1.1 GB
      -> review and clear stale entries

WSL virtual disk (Ubuntu): 38.7 GB
      path: C:\Users\brandon\AppData\Local\Packages\CanonicalGroupLimited...\LocalState\ext4.vhdx
      Note: the vhdx grows but never auto-shrinks. To reclaim,
      after `wsl --shutdown` run: wsl --manage <distro> --set-sparse true  (or Optimize-VHD).

(Audit only — nothing was deleted. Run the suggested commands yourself to reclaim space.)
```

Nothing is deleted — it prints the commands and leaves running them to you.
