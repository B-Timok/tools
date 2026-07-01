# tools

A small collection of personal utilities.

## Downloads organizer (`main.py`)

Keeps your Downloads folder tidy. It works in three stages, each safer to run
than the last, and **never** moves or deletes anything unless you explicitly
ask it to. Running with no arguments just previews — an accidental run is
harmless.

### Usage

```bash
python3 main.py                      # preview ~/Downloads (default, safe)
python3 main.py preview ~/Downloads  # preview a specific folder
python3 main.py organize             # move files into per-category folders
python3 main.py organize --dry-run   # show what organize would do
python3 main.py clean                # delete leftovers, archive old installers
python3 main.py clean --dry-run      # show what clean would do
python3 main.py clean --age-days 90  # change the installer-archive threshold
```

Recommended workflow the first time: `preview` → `organize` → `clean`.

### What each command does

**`preview`** — scans the folder, groups files by type, and prints what *would*
happen. Touches nothing.

```
Found 42 files

PDFs: 8
Images: 12
Archives: 5
Installers: 4
Other: 13

Would move:
  - resume.pdf -> PDFs/
  - photo.png -> Images/
  - node-v22.msi -> Installers/
```

**`organize`** — creates category folders as needed and moves each file into
the right one. Name collisions are handled automatically (`resume.pdf` becomes
`resume (1).pdf` if one already exists).

**`clean`** — applies cleanup rules:

- Deletes leftover partial downloads (`.crdownload`, `.tmp`, `.part`,
  `.download`).
- Moves installers older than 60 days (configurable with `--age-days`) into an
  `Archive/` folder.
- Prints a summary report.

`clean` looks in both the top level and the `Other/` / `Installers/` folders,
so it works whether or not you've already run `organize`.

### Categories

Files are sorted by extension into: **PDFs, Images, Documents, Archives,
Installers, Audio, Video**, and anything unrecognized goes to **Other**. Edit
the `CATEGORIES` dictionary at the top of `main.py` to customize.

### Safety notes

- Dotfiles (e.g. `.DS_Store`) are always left alone.
- The category folders the tool manages are never re-sorted, so re-running is
  safe.
- Use `--dry-run` on `organize` / `clean` any time you want to see the plan
  before committing to it.
