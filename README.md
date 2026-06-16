# unpackerfolder

A zero-dependency Python script that scans a folder, detects archives and EPUBs, and extracts them — all with a live progress bar. Files that were already extracted in a previous run are detected automatically and skipped, so you can safely re-run it on the same folder without duplicating work.

## Supported formats

| Extension | Notes |
|-----------|-------|
| `.zip` `.cbz` | Pure Python |
| `.tar` `.gz` `.bz2` `.xz` `.zst` | Pure Python (falls back to 7-Zip for `.zst`) |
| `.rar` `.cbr` | Requires **7-Zip**, **unrar**, or **patool** |
| `.7z` | Requires **7-Zip** |
| `.epub` | Extracts embedded images only (pure Python) |

## Skipping already-extracted files

Before extracting anything, the script checks whether a destination folder with the same name as the archive (or EPUB) already exists **and** already contains files inside it. If so, that file is treated as already extracted and is skipped entirely — nothing is overwritten, re-extracted, or deleted.

This makes it safe to re-run the script on a folder you've already processed: only new archives/EPUBs are extracted, the rest are left untouched.

- Shown in the **preview** as `[already extracted - will be skipped]` next to the file name.
- Shown during **extraction** as `SKIP folder already contains files (already extracted)`.
- Counted separately as `Skipped` in the final summary.

If the destination folder exists but is empty, it's *not* considered "already extracted" — the script extracts into it normally.

## Usage

### Double-click (Windows / macOS / Linux)

Drop `unpackerfolder.py` into any folder that contains archives, then **double-click** it.

- Starts **immediately in COPY mode** — no menu, no prompts.
- Prints a live progress bar for each file.
- Archives/EPUBs already extracted in a previous run are detected and **skipped automatically**.
- If an error occurs on one file, it is printed and extraction **continues automatically** with the next file.
- The terminal window **closes automatically after 4.2 seconds** when all operations are done (whether or not there were errors).

### From a terminal

```bash
py unpackerfolder.py                 # interactive menu (Copy / Replace / Quit)
py unpackerfolder.py --interactive   # explicit interactive mode
py unpackerfolder.py -i              # short alias
```

You can also force interactive mode by setting an environment variable:

```bash
UNPACKER_INTERACTIVE=1 python unpackerfolder.py
```

### Interactive menu options

| Key | Action |
|-----|--------|
| `C` / Enter | **Copy mode** – originals kept untouched |
| `X` | **Replace mode** – originals deleted after extraction |
| `Q` | Quit |

Replace mode asks you to type `DELETE` (all caps) as a safety confirmation.

> In interactive mode errors are printed inline and execution always continues; a summary (OK / Skipped / Errors count) is shown at the end.

## Requirements

### Python

Python **3.10 or newer** (uses `str | None` union syntax).  
No `pip install` needed for `.zip`, `.tar`, `.epub` — those use the standard library only.

### Optional external tools (for RAR / 7z)

| Tool | Windows | macOS | Linux |
|------|---------|-------|-------|
| **7-Zip** | [7-zip.org](https://www.7-zip.org/) | `brew install sevenzip` | `sudo apt install 7zip` |
| **unrar** | [rarlab.com](https://www.rarlab.com/rar_add.htm) | `brew install unrar` | `sudo apt install unrar` |
| **patool** | `pip install patool` | `pip install patool` | `pip install patool` |

At least one of the three is enough for `.rar`/`.cbr`/`.7z` files.  
For all other formats no external tool is required.

## How double-click detection works

The script checks (in order):

1. `UNPACKER_INTERACTIVE=1` env-var → forces **interactive** mode
2. `--interactive` / `-i` flag → forces **interactive** mode
3. **Windows only**: is the parent process `explorer.exe`? → **double-click**
4. Fallback: `stdin` is not a TTY → **double-click**
