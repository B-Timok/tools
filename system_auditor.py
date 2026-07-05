#!/usr/bin/env python3
"""System auditor — find what's quietly slowing your machine down.

Two lenses, two subcommands:

    startup   Inventory Windows auto-start entries — Run keys, Startup folders,
              logon/boot scheduled tasks, and auto-start services — and flag
              what's NEW since the last run (installers love to add persistence
              you never asked for). Designed to run from WSL/Ubuntu: it shells
              out to Windows via `powershell.exe` interop. Runs on native
              Windows too.

    wsl       Report WSL/Ubuntu disk bloat — apt cache, logs, package-manager
              caches, Docker, and the ext4.vhdx virtual disk that grows but
              never shrinks — with safe cleanup commands you can copy-paste.

Audit-only by design: this tool reports and flags. It never disables a startup
entry or deletes anything itself — it just tells you what to look at and how.

Examples:
    python3 system_auditor.py startup            # audit Windows autostart + diff
    python3 system_auditor.py startup --no-save   # audit without updating baseline
    python3 system_auditor.py startup --json      # machine-readable output
    python3 system_auditor.py wsl                 # WSL disk-bloat report
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Where the startup baseline is stored so we can diff "new since last run".
SNAPSHOT_PATH = Path.home() / ".local" / "state" / "system_auditor" / "startup_snapshot.json"

# Sections of a startup audit, in display order.
SECTIONS = ("run_keys", "startup_folders", "scheduled_tasks", "services")
SECTION_TITLES = {
    "run_keys": "Registry Run keys",
    "startup_folders": "Startup folders",
    "scheduled_tasks": "Scheduled tasks (logon/boot)",
    "services": "Auto-start services",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def human_size(nbytes: int) -> str:
    """Format a byte count as a short human-readable string (e.g. '2.1 GB')."""
    size = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # unreachable, keeps type-checkers happy


def is_wsl() -> bool:
    """True when running inside Windows Subsystem for Linux."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def dir_size(path: Path) -> int:
    """Recursively sum file sizes under `path`, skipping anything unreadable."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Windows interop
# ---------------------------------------------------------------------------

def powershell_exe() -> str | None:
    """Locate PowerShell: powershell.exe via WSL interop, or pwsh if present."""
    return shutil.which("powershell.exe") or shutil.which("pwsh")


def run_powershell(script: str) -> str:
    """Run a PowerShell script and return its stdout.

    The script is passed as a base64 -EncodedCommand to sidestep all shell
    quoting issues between WSL and Windows. Raises RuntimeError with a clear
    message if PowerShell isn't reachable or the script fails.
    """
    exe = powershell_exe()
    if not exe:
        raise RuntimeError(
            "Could not find powershell.exe. The `startup` audit reads the "
            "Windows side and needs PowerShell — run this from WSL (Windows "
            "interop enabled) or directly on Windows."
        )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PowerShell failed: {stderr or 'unknown error'}")
    return stdout


# One PowerShell pass gathers every startup source and emits a single JSON blob.
# @(...) wrappers force arrays so single items don't collapse to objects.
PS_STARTUP = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$runPaths = @(
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
  'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run',
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
)
$runKeys = foreach ($p in $runPaths) {
  if (Test-Path $p) {
    $props = Get-ItemProperty -Path $p
    foreach ($prop in $props.PSObject.Properties) {
      if ($prop.Name -notmatch '^PS') {
        [PSCustomObject]@{ location = $p; name = $prop.Name; command = [string]$prop.Value }
      }
    }
  }
}

$startupFolders = @(
  [Environment]::GetFolderPath('Startup'),
  [Environment]::GetFolderPath('CommonStartup')
)
$startupItems = foreach ($f in $startupFolders) {
  if ($f -and (Test-Path $f)) {
    Get-ChildItem -Path $f -File | ForEach-Object {
      [PSCustomObject]@{ location = $f; name = $_.Name; command = $_.FullName }
    }
  }
}

$tasks = foreach ($t in (Get-ScheduledTask)) {
  if ($t.State -eq 'Disabled') { continue }
  $trig = $t.Triggers | Where-Object { $_.CimClass.CimClassName -match 'Logon|Boot' }
  if ($trig) {
    [PSCustomObject]@{ location = $t.TaskPath; name = $t.TaskName; command = [string]$t.State }
  }
}

$services = Get-CimInstance Win32_Service |
  Where-Object { $_.StartMode -eq 'Auto' } |
  ForEach-Object {
    [PSCustomObject]@{ location = 'Service'; name = $_.Name; command = "$($_.DisplayName) [$($_.State)]" }
  }

[PSCustomObject]@{
  run_keys        = @($runKeys)
  startup_folders = @($startupItems)
  scheduled_tasks = @($tasks)
  services        = @($services)
} | ConvertTo-Json -Depth 5
"""

PS_VHDX = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
$out = @()
if (Test-Path $root) {
  foreach ($k in (Get-ChildItem $root)) {
    $vhd = $null  # reset each pass: SilentlyContinue must not leak the previous distro's path
    $props = Get-ItemProperty $k.PSPath
    # Some distros (e.g. docker-desktop) register BasePath with the \\?\
    # extended-length prefix, which Join-Path can't parse — strip it. They may
    # also name their disk via VhdFileName instead of the default ext4.vhdx.
    $bp = ([string]$props.BasePath) -replace '^\\\\\?\\', ''
    if ($bp) {
      $name = if ($props.VhdFileName) { [string]$props.VhdFileName } else { 'ext4.vhdx' }
      $vhd = Join-Path $bp $name
      if ($vhd -and (Test-Path $vhd)) {
        $out += [PSCustomObject]@{ distro = $props.DistributionName; path = $vhd; size = (Get-Item $vhd).Length }
      }
    }
  }
}
$out | ConvertTo-Json -Depth 3
"""


# ---------------------------------------------------------------------------
# Startup audit
# ---------------------------------------------------------------------------

def _as_list(value) -> list:
    """Normalize PowerShell's JSON (which drops empty arrays to null and
    single-element arrays to a bare object) into a plain list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def gather_startup() -> list[dict]:
    """Return a flat list of startup entries: {section, location, name, command}."""
    raw = run_powershell(PS_STARTUP)
    data = json.loads(raw) if raw else {}
    rows: list[dict] = []
    for section in SECTIONS:
        for entry in _as_list(data.get(section)):
            rows.append({
                "section": section,
                "location": str(entry.get("location", "")),
                "name": str(entry.get("name", "")),
                "command": str(entry.get("command", "")),
            })
    return rows


def key_of(row: dict) -> str:
    """Stable identity for an entry, used to diff runs."""
    return f"{row['section']}|{row['location']}|{row['name']}"


def diff_entries(previous: list[dict], current: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (new_entries, removed_entries) comparing current vs previous."""
    prev_keys = {key_of(r) for r in previous}
    curr_keys = {key_of(r) for r in current}
    new = [r for r in current if key_of(r) not in prev_keys]
    removed = [r for r in previous if key_of(r) not in curr_keys]
    return new, removed


def load_snapshot() -> list[dict] | None:
    """Load the previously saved startup baseline, or None if there isn't one."""
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        return data.get("rows", [])
    except (OSError, ValueError):
        return None


def save_snapshot(rows: list[dict]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2))


def _print_entry(row: dict) -> None:
    cmd = row["command"]
    if len(cmd) > 90:
        cmd = cmd[:87] + "..."
    print(f"  - {row['name']}: {cmd}")


def audit_startup(save: bool = True, as_json: bool = False) -> int:
    try:
        current = gather_startup()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    previous = load_snapshot()
    new, removed = diff_entries(previous or [], current)

    if as_json:
        print(json.dumps({
            "entries": current,
            "new_since_last_run": new,
            "gone_since_last_run": removed,
            "had_baseline": previous is not None,
        }, indent=2))
    else:
        print(f"Windows startup audit — {len(current)} auto-start entries\n")
        for section in SECTIONS:
            rows = [r for r in current if r["section"] == section]
            if rows:
                print(f"{SECTION_TITLES[section]}: {len(rows)}")
                for r in rows:
                    _print_entry(r)
                print()

        if previous is None:
            print("No previous baseline — saved one now. Re-run later to see "
                  "what's new since this check.")
        else:
            print("--- Changes since last check ---")
            print(f"  New:  {len(new)} entr{'y' if len(new) == 1 else 'ies'}")
            for r in new:
                print(f"    + [{SECTION_TITLES[r['section']]}] {r['name']}: {r['command'][:80]}")
            print(f"  Gone: {len(removed)} entr{'y' if len(removed) == 1 else 'ies'}")
            for r in removed:
                print(f"    - [{SECTION_TITLES[r['section']]}] {r['name']}")
            if not new and not removed:
                print("  Nothing changed.")
        print("\n(Audit only — to disable an entry, use Task Manager > Startup, "
              "`msconfig`, or `Disable-ScheduledTask`.)")

    if save:
        save_snapshot(current)
    return 0


# ---------------------------------------------------------------------------
# WSL disk-bloat audit
# ---------------------------------------------------------------------------

def audit_wsl() -> int:
    if not is_wsl():
        print("Note: this doesn't look like a WSL environment; reporting local "
              "disk usage anyway.\n", file=sys.stderr)

    usage = shutil.disk_usage("/")
    print("WSL / Linux disk report\n")
    print(f"Filesystem (/): {human_size(usage.used)} used of "
          f"{human_size(usage.total)}  ({human_size(usage.free)} free)\n")

    home = Path.home()
    # (label, path, cleanup suggestion) — checked only if the path exists.
    targets = [
        ("apt package cache", Path("/var/cache/apt/archives"), "sudo apt clean"),
        ("system logs", Path("/var/log"), "sudo journalctl --vacuum-time=7d"),
        ("Docker data", Path("/var/lib/docker"), "docker system prune -a"),
        ("pip cache", home / ".cache" / "pip", "pip cache purge"),
        ("npm cache", home / ".npm", "npm cache clean --force"),
        ("user cache (~/.cache)", home / ".cache", "review and clear stale entries"),
    ]

    print("Reclaimable space:")
    found_any = False
    for label, path, suggestion in targets:
        if not path.exists():
            continue
        found_any = True
        size = dir_size(path)
        print(f"  {label}: {human_size(size)}")
        print(f"      -> {suggestion}")
    if not found_any:
        print("  (none of the usual cache locations were found)")

    # The ext4.vhdx lives on the Windows side and never shrinks on its own.
    if powershell_exe():
        try:
            raw = run_powershell(PS_VHDX)
            for vhd in _as_list(json.loads(raw)) if raw else []:
                print(f"\nWSL virtual disk ({vhd.get('distro', '?')}): "
                      f"{human_size(int(vhd.get('size', 0)))}")
                print(f"      path: {vhd.get('path', '?')}")
            print("      Note: the vhdx grows but never auto-shrinks. To reclaim safely:")
            print("        1. in WSL:      sudo fstrim -a")
            print("        2. in Windows:  wsl --shutdown")
            print("        3. in admin PowerShell, compact with diskpart:")
            print("             select vdisk file=<path above>")
            print("             attach vdisk readonly / compact vdisk / detach vdisk")
            print("      (Don't use --set-sparse --allow-unsafe: WSL disabled sparse "
                  "VHDs by default over data-corruption risk.)")
        except (RuntimeError, ValueError):
            pass  # interop not available — skip the vhdx section quietly

    print("\n(Audit only — nothing was deleted. Run the suggested commands "
          "yourself to reclaim space.)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit what's slowing your machine down (Windows startup + WSL disk bloat).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_startup = sub.add_parser("startup", help="audit Windows auto-start entries and flag new ones")
    p_startup.add_argument("--no-save", action="store_true",
                           help="don't update the saved baseline this run")
    p_startup.add_argument("--json", action="store_true", help="output machine-readable JSON")

    sub.add_parser("wsl", help="report WSL/Ubuntu disk bloat with cleanup suggestions")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "startup":
        return audit_startup(save=not args.no_save, as_json=args.json)
    if args.command == "wsl":
        return audit_wsl()

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
