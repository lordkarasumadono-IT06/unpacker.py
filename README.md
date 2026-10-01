# Unpacker Folder

A safe, dependency-light batch extractor for archives, comic-book files, and EPUBs.

`unpackerfolder.py` can process individual files or folders, scan directory trees recursively, run extractions in parallel, handle multipart archives, extract EPUB images in reading order, and optionally delete sources only after a successful extraction.

> **Version:** 2.4.0  
> **Author:** Lord Karasuma  
> **Python:** 3.9+  
> **Platforms:** Windows, macOS, Linux

## Features

- ZIP, CBZ, CBR, CB7, CBT, 7z, RAR, TAR and common compressed TAR formats.
- EPUB extraction as ordered images or complete internal structure.
- CBZ `AUTO`, image-only, and full-structure modes.
- Folder scanning and strict recursive scanning with `-r` / `--recursive`.
- Parallel extraction with configurable workers.
- Multipart RAR and split 7z/ZIP/RAR detection.
- Safe temporary staging before the final destination is created.
- Existing non-empty destinations are skipped instead of merged or overwritten.
- Optional `[EXTRACTED]` container or custom output directory.
- Optional source deletion only after successful extraction.
- `.env` configuration without third-party Python packages.
- Graceful `Ctrl+C` handling and automatic cleanup of temporary extraction folders.
- No mandatory third-party Python dependencies.

## Requirements

Python **3.9+** is required.

For the broadest archive support, install **7-Zip**. The script automatically looks for `7z`, `7zz`, `7za`, `7zr`, and common Windows 7-Zip installation paths. **UnRAR** and **patool** can also be used as fallbacks.

```powershell
python --version
7z
```

## Quick start

```powershell
# One archive
python .\unpackerfolder.py "C:\Downloads\archive.zip"

# All supported files directly inside a folder
python .\unpackerfolder.py "D:\Comics"

# Folder + all nested subfolders
python .\unpackerfolder.py -r "D:\Comics"

# Recursive scan into [EXTRACTED]
python .\unpackerfolder.py -r -s "D:\Comics"

# Custom output root
python .\unpackerfolder.py -r -o "E:\Extracted" "D:\Comics"

# Preview only
python .\unpackerfolder.py -n -r "D:\Comics"
```

When no path is supplied, the script scans the directory containing `unpackerfolder.py`.

## Command-line reference

```text
python unpackerfolder.py [OPTIONS] [PATH ...]
```

| Option | Purpose |
|---|---|
| `-o DIR`, `--output DIR` | Use `DIR` as the output root. |
| `-s`, `--subfolder` | Put results inside `[EXTRACTED]`. |
| `--no-subfolder` | Disable `[EXTRACTED]` if enabled through `.env`. |
| `-r`, `--recursive` | Recursively scan directory inputs. Only directories are valid with `-r`. |
| `-d`, `--delete` | Delete source files after successful extraction. |
| `-c`, `--copy` | Force COPY mode, overriding a DELETE default from `.env`. |
| `-k`, `--keepepub` | Extract the complete EPUB structure. |
| `--epub-images` | Force EPUB image-only mode. |
| `--cbz-images` | Force CBZ image-only extraction. |
| `--keepcbz` | Force complete CBZ structure extraction. |
| `-j N`, `--jobs N` | Maximum parallel extraction workers. |
| `-n`, `--dry-run` | Show the plan without extracting anything. |
| `-q`, `--quiet` | Show only errors and the final summary. |
| `--open` | Always open the final result/container. |
| `--no-open` | Never open a result automatically. |
| `-V`, `--version` | Print the version. |
| `-h`, `--help` | Show built-in help. |

`-k` and `--epub-images` are mutually exclusive. `--cbz-images` and `--keepcbz` are mutually exclusive.

## Output rules

Without `-r`, a directory input scans only files directly inside that directory.

With `-r`, every nested directory is scanned before extraction starts. The recursive scan ignores `[EXTRACTED]`, temporary `.unpacking-*` directories, directory symlinks, and a custom `-o` output tree when that output is located inside the source tree.

The output rules are:

| Command mode | Destination |
|---|---|
| default | next to each source file |
| `-s` | `<source root>\[EXTRACTED]\...` |
| `-o OUT` | `OUT\...` |
| `-o OUT -s` | `OUT\[EXTRACTED]\...` |

For recursive runs, relative subdirectory structure is preserved whenever an output root is used.

Example source tree:

```text
D:\Library\
├── root.cbz
├── MAGAZINE\issue.epub
└── WEB\chapter.zip
```

`-r` extracts beside each source:

```text
D:\Library\root\
D:\Library\MAGAZINE\issue\
D:\Library\WEB\chapter\
```

`-r -s` produces:

```text
D:\Library\[EXTRACTED]\root\
D:\Library\[EXTRACTED]\MAGAZINE\issue\
D:\Library\[EXTRACTED]\WEB\chapter\
```

`-r -o "E:\Output"` produces:

```text
E:\Output\root\
E:\Output\MAGAZINE\issue\
E:\Output\WEB\chapter\
```

A non-empty destination is treated as already extracted and skipped. If multiple planned sources would otherwise use the same destination, the planner assigns distinct destination names.

## EPUB handling

### Images mode — default

EPUBs are normally extracted as images only. The script reconstructs reading order from the EPUB package where possible:

```text
container.xml → OPF manifest/spine → XHTML/SVG references → images
```

It then uses manifest and extension-based fallbacks for remaining images. Original image bytes are preserved; no re-encoding is performed.

Supported image types include JPEG, PNG, GIF, WebP, BMP, TIFF, AVIF, JXL, HEIC/HEIF, and SVG.

Default naming is `ORIGINAL`; `SEQUENTIAL` naming can be enabled through `.env`.

### Complete structure

```powershell
python .\unpackerfolder.py -k "book.epub"
```

The complete EPUB is extracted to:

```text
book .epub\
```

The intentional space prevents the output directory from colliding with the source `.epub` file.

Encrypted/DRM-protected image resources are not decrypted. If EPUB package parsing fails, the script warns and falls back to scanning recognized image resources.

## CBZ handling

Default CBZ mode is `AUTO`.

- **AUTO** — preserves a normal CBZ completely, but can flatten confidently detected web-style image packages.
- **`--cbz-images`** — extracts image resources only and flattens them into the result directory.
- **`--keepcbz`** — preserves the complete internal archive structure.

Images are copied byte-for-byte. Duplicate flattened filenames are renamed safely instead of overwritten.

The script also checks archive signatures, so a `.cbz` that is not physically ZIP-based can be routed through another available extractor.

## Multipart archives

Supported layouts include:

```text
archive.part1.rar + archive.part2.rar + ...
archive.7z.001    + archive.7z.002    + ...
archive.zip.001   + archive.zip.002   + ...
archive.rar       + archive.r00       + ...
```

Start from the first volume (`part1.rar`, `.001`, or the main `.rar`). Related volumes are grouped into one extraction item. Actual extraction normally requires 7-Zip or UnRAR.

## Safe extraction and deletion

Every item is extracted into a unique hidden staging directory first. Only after files have been successfully produced is that directory committed to the final destination.

DELETE mode:

```powershell
python .\unpackerfolder.py -d "archive.zip"
```

Interactive runs require typing `DELETE` by default. Sources are removed only after extraction and finalization succeed. Multipart deletion applies to all recognized volumes.

For unattended trusted workflows, confirmation can be disabled with `UNPACKER_CONFIRM_DELETE=0`.

`Ctrl+C` stops the run, terminates tracked external extractors, cleans temporary directories where possible, and exits with code `130`.

## Parallel extraction

The default worker count is:

```text
min(4, CPU count)
```

Override it with:

```powershell
python .\unpackerfolder.py -j 2 "D:\Archives"
```

Pending items are ordered by source size so larger jobs start first. More workers are not always faster; storage speed and decompression cost often matter more than CPU count.

## `.env` configuration

The script checks for `.env` beside `unpackerfolder.py` and in the current working directory. Real environment variables take precedence.

```dotenv
UNPACKER_MODE=COPY
EPUB_UNPACKER=IMAGES
CBZ_UNPACKER=AUTO
UNPACKER_CONFIRM_DELETE=1
UNPACKER_SUB_FOLDER=FALSE
OPEN_UNPACKED_FOLDER=AUTO
EPUB_IMAGE_NAMING=ORIGINAL
CBZ_IMAGE_NAMING=ORIGINAL
UNPACKER_WORKERS=4
UNPACKER_ZIP_ENCODING=cp932
```

| Variable | Values / purpose |
|---|---|
| `UNPACKER_MODE` | `COPY` or `DELETE` |
| `EPUB_UNPACKER` | `IMAGES` or `STRUCTURE` |
| `CBZ_UNPACKER` | `AUTO`, `IMAGES`, or `STRUCTURE` |
| `UNPACKER_CONFIRM_DELETE` | Require DELETE confirmation |
| `UNPACKER_SUB_FOLDER` | Enable `[EXTRACTED]` by default |
| `OPEN_UNPACKED_FOLDER` | `AUTO`, `NEVER`, or `ALWAYS` |
| `EPUB_IMAGE_NAMING` | `ORIGINAL` or `SEQUENTIAL` |
| `CBZ_IMAGE_NAMING` | `ORIGINAL` or `SEQUENTIAL` |
| `UNPACKER_WORKERS` | Default worker count |
| `UNPACKER_ZIP_ENCODING` | Legacy ZIP filename encoding, e.g. `cp932` |
| `UNPACKER_ASCII` | Force ASCII console graphics |
| `NO_COLOR` | Disable ANSI colors |

CLI flags override corresponding `.env` defaults.

## Useful examples

```powershell
# EPUB as images
unpacker "D:\Books\book.epub"

# EPUB complete structure
unpacker -k "D:\Books\book.epub"

# Force CBZ images only
unpacker --cbz-images "D:\Comics\comic.cbz"

# Recursive library scan
unpacker -r "D:\Library"

# Recursive scan + [EXTRACTED]
unpacker -r -s "D:\Library"

# Recursive scan to another drive
unpacker -r -o "E:\Extracted" "D:\Library"

# Recursive EPUB structure extraction with six workers
unpacker -r -k -j 6 "D:\Books"

# Preview a destructive recursive run
unpacker -n -r -d "D:\Archives"

# Extract and delete successful sources
unpacker -r -d "D:\Archives"

# Disable automatic Explorer/Finder opening
unpacker --no-open "D:\Archives"
```

`unpacker` in these examples assumes you have created a PowerShell alias/function for the script. Otherwise use `python .\unpackerfolder.py`.

## Troubleshooting

**`no tool available to extract ...`**  
Install 7-Zip and ensure `7z` is available on `PATH` or installed in its standard Windows location.

**Multipart archive does not extract**  
Pass the first volume and keep all volumes in the same directory.

**`already extracted` / item skipped**  
The destination already exists and is non-empty. The script intentionally does not merge into it.

**Garbled Japanese ZIP filenames**  
Try `UNPACKER_ZIP_ENCODING=cp932`.

**EPUB images are missing**  
The EPUB may contain malformed package metadata, unsupported resources, or encrypted content. Use `-k` to inspect the complete internal structure.

**`-d` refuses to run unattended**  
DELETE confirmation is enabled. For a trusted automated workflow, set `UNPACKER_CONFIRM_DELETE=0`.

**More workers are slower**  
Try `-j 2` or `-j 4`; extraction performance depends heavily on storage and compression format.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Success, or nothing needed extraction |
| `1` | Extraction/planning error |
| `2` | DELETE confirmation declined/aborted |
| `130` | Interrupted with `Ctrl+C` |

Warnings do not necessarily produce a non-zero exit code.

## Notes

- This is an extractor, not a DRM-removal tool.
- Password entry is intentionally non-interactive.
- External-format support depends on installed extraction tools.
- Existing non-empty outputs are skipped rather than merged.
- Source deletion is best-effort and only occurs after successful extraction.
- The script favors predictable, non-destructive behavior by default.

## Author

**Lord Karasuma**

The terminal header displays `by Lord Karasuma`; the technical release number is available with `--version`.

## License

Add an explicit `LICENSE` file before describing the repository as open source. Common choices for a utility like this include MIT, Apache-2.0, and GPL-3.0.
