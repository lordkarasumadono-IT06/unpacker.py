#!/usr/bin/env python3
"""
unpackerfolder.py  --  batch extractor for archives and EPUB books.

Supports: .zip .cbz .cbr .cb7 .cbt .7z .rar .tar .gz .bz2 .xz .zst .lzma
          .tgz .tbz .tbz2 .txz (+ .tar.gz/.tar.bz2/.tar.xz/.tar.zst, split
          volumes such as .part1.rar / .7z.001) and .epub

Usage
  python unpackerfolder.py                      scan the folder of the script
  python unpackerfolder.py "PATH" ["PATH" ...]  PATH = folder or single item
  python unpackerfolder.py -d "PATH"            delete originals afterwards
  python unpackerfolder.py -c "PATH"            force COPY (overrides UNPACKER_MODE)
  python unpackerfolder.py -o "OUT" "PATH"      extract into OUT
  python unpackerfolder.py -s "PATH"            results go into PATH-parent/[EXTRACTED] for a file,
                                    or FOLDER/[EXTRACTED] for a folder
  python unpackerfolder.py -k book.epub         whole EPUB in "book .epub/"
  python unpackerfolder.py -r "FOLDER"          recursively scan FOLDER and all subfolders

Design
  * COPY mode is ALWAYS the default (double-click included). Originals are
    only ever removed with -d / --delete.
  * ZIP/CBZ/TAR/7z/RAR keep their internal folder structure.
  * Every item is extracted into a hidden temp folder first and renamed to
    its final name only on success, so an interrupted or failed run can
    never leave a half-filled folder that a later run would treat as
    "already extracted".
  * Items are extracted in parallel (thread pool), biggest first.

Configuration (real environment variables or a .env file, searched in the
script folder and then in the current folder; real variables win; when both .env files
contain the same key, the script-folder .env wins)
  UNPACKER_MODE=COPY|DELETE        default mode (default COPY); -d forces DELETE,
                                   -c forces COPY. DELETE still asks for the
                                   safety word unless UNPACKER_CONFIRM_DELETE=0
  EPUB_UNPACKER=IMAGES|STRUCTURE   default EPUB behaviour (default IMAGES)
  CBZ_UNPACKER=AUTO|IMAGES|STRUCTURE
                                   CBZ behaviour (default AUTO): detect supported
                                   web-style CBZs and keep only cover + page images
  UNPACKER_CONFIRM_DELETE=0        disable the "type DELETE" safety prompt
  UNPACKER_SUB_FOLDER=TRUE          same as -s (--no-subfolder overrides it)
  OPEN_UNPACKED_FOLDER=AUTO|NEVER|ALWAYS
                                   opening policy after extraction (default AUTO).
                                   --open / --no-open always override this setting
  EPUB_IMAGE_NAMING=ORIGINAL|SEQUENTIAL   EPUB file names in IMAGES mode
  CBZ_IMAGE_NAMING=ORIGINAL|SEQUENTIAL    CBZ file names in IMAGES mode
  UNPACKER_WORKERS=N               parallel extractions
  UNPACKER_ZIP_ENCODING=cp932      extra encoding for non-UTF-8 zip names
  UNPACKER_ASCII=1 / NO_COLOR=1    plain ASCII / no colours
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import unicodedata
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

if sys.version_info < (3, 9):
    sys.exit("unpackerfolder.py requires Python 3.9 or newer.")

__version__ = "2.4.0"
APP_NAME = "UNPACKER FOLDER"

# ── constants ────────────────────────────────────────────────────────────────

ARCHIVE_EXTS = {".zip", ".cbz", ".cbr", ".cb7", ".cbt", ".7z", ".rar",
                ".tar", ".gz", ".bz2", ".xz", ".zst", ".lzma",
                ".tgz", ".tbz", ".tbz2", ".txz"}
DOUBLE_EXTS  = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
                ".tar.lzma", ".tar.lz")
TAR_SUFFIXES = (".tar", ".tgz", ".tbz", ".tbz2", ".txz", ".cbt") + DOUBLE_EXTS
STREAM_EXTS  = {".gz", ".bz2", ".xz", ".zst", ".lzma"}      # tar or single file
RAR_EXTS     = {".rar", ".cbr"}

IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp",
              ".bmp", ".tif", ".tiff", ".avif", ".jxl", ".heic", ".heif",
              ".svg"}
IMAGE_MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/tiff": ".tif", "image/avif": ".avif", "image/svg+xml": ".svg",
    "image/jxl": ".jxl", "image/heic": ".heic", "image/heif": ".heif",
}

COPY_BUFSIZE = 1024 * 1024                    # 1 MiB (shutil default: 16 KiB)
TEMP_PREFIX  = ".unpacking-"
SUBFOLDER_NAME = "[EXTRACTED]"
DELETE_WORD  = "DELETE"
DEFAULT_WORKERS = min(4, os.cpu_count() or 2)

# EPUB font-obfuscation algorithms are NOT DRM; anything else on a resource
# listed in META-INF/encryption.xml is treated as protected content.
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding",
                    "http://ns.adobe.com/pdf/enc#RC"}

STOP = threading.Event()                      # set on Ctrl+C
_PROCS: set = set()
_PROCS_LOCK = threading.Lock()


class Interrupted(Exception):
    """Raised inside workers when the user pressed Ctrl+C."""


# ── small utilities ──────────────────────────────────────────────────────────

def fmt_size(n: float) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{int(x)} B" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{n} B"


def fmt_time(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_clock(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def dwidth(s: str) -> int:
    """Display width (CJK / fullwidth characters take two cells)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def clip(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if dwidth(s) <= width:
        return s
    out, used = [], 0
    for ch in s:
        w = dwidth(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def load_dotenv() -> None:
    """Minimal .env loader (no dependency). Real environment variables win."""
    seen = set()
    for base in (Path(__file__).resolve().parent, Path.cwd()):
        f = base / ".env"
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        try:
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:]
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].rstrip()
                os.environ.setdefault(key, val)
        except OSError:
            pass


def enable_vt() -> bool:
    """Enable ANSI escape processing on Windows 10+ consoles."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


# ── tool detection ───────────────────────────────────────────────────────────

@lru_cache(maxsize=None)
def find_7z() -> str | None:
    for name in ("7z", "7zz", "7za", "7zr", "7z.exe"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"),
                     r"C:\Program Files"):
            if base and (Path(base) / "7-Zip" / "7z.exe").exists():
                return str(Path(base) / "7-Zip" / "7z.exe")
    return None


@lru_cache(maxsize=None)
def find_unrar() -> str | None:
    found = shutil.which("unrar") or shutil.which("UnRAR.exe")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)")):
            if base and (Path(base) / "WinRAR" / "UnRAR.exe").exists():
                return str(Path(base) / "WinRAR" / "UnRAR.exe")
    return None


@lru_cache(maxsize=None)
def find_patool() -> str | None:
    return shutil.which("patool")


# ── subprocess runner ────────────────────────────────────────────────────────

_PCT_RE = re.compile(rb"(\d{1,3})%")


def _tail(text: str, lines: int = 6) -> str:
    rows = [r.rstrip() for r in text.splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def run_tool(cmd: list[str], on_progress=None) -> None:
    """
    Run an external extractor. stdin is closed (no password prompts that
    would hang), stdout is consumed (and parsed for 7z's percentage output),
    stderr is drained on a thread -- so a chatty tool can never deadlock on
    a full pipe buffer.
    """
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    with _PROCS_LOCK:
        _PROCS.add(proc)
    err_chunks: list[bytes] = []

    def drain() -> None:
        try:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                err_chunks.append(chunk)
        except Exception:
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        last = -1
        while True:
            data = proc.stdout.read1(4096)
            if not data:
                break
            if on_progress:
                hits = _PCT_RE.findall(data)
                if hits:
                    pct = min(int(hits[-1]), 100)
                    if pct != last:
                        last = pct
                        on_progress(pct / 100)
        proc.wait()
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(proc)
    t.join(timeout=2)
    if STOP.is_set():
        raise Interrupted()
    if proc.returncode != 0:
        msg = b"".join(err_chunks).decode("utf-8", "replace")
        raise RuntimeError(_tail(msg) or f"exit code {proc.returncode}")


# ── path safety / naming ─────────────────────────────────────────────────────

_WIN_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)


def clean_part(part: str) -> str:
    """Make one path component valid for the current OS."""
    if os.name == "nt":
        part = _WIN_BAD.sub("_", part).rstrip(" .")
        if _WIN_RESERVED.match(part):
            part = "_" + part
    return part


def safe_relpath(name: str, flatten: bool = False) -> Path | None:
    """
    Turn an archive member name into a safe relative path. Drops absolute
    prefixes, drive letters, '.' and '..' components (zip-slip proof: the
    result can never leave the destination) and fixes Windows-invalid names.
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    parts = [c for c in (clean_part(p) for p in parts) if c]
    if not parts:
        return None
    if flatten:
        parts = parts[-1:]
    return Path(*parts)


def unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, sfx = target.stem, target.suffix
    j = 1
    while True:
        cand = target.with_name(f"{stem}_{j}{sfx}")
        if not cand.exists():
            return cand
        j += 1


@lru_cache(maxsize=None)
def zip_encodings() -> tuple:
    extra = os.environ.get("UNPACKER_ZIP_ENCODING", "").strip()
    return ("utf-8", extra) if extra else ("utf-8",)


def zip_name(info: zipfile.ZipInfo) -> str:
    """
    zipfile decodes names as cp437 when the UTF-8 flag is missing, which
    garbles non-ASCII names written by many tools. Re-decode as UTF-8 (and
    optionally UNPACKER_ZIP_ENCODING, e.g. cp932 for Shift-JIS archives).
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in zip_encodings():
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return name


def dir_stats(root: Path) -> tuple[int, int]:
    files = size = 0
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        files += 1
                        try:
                            size += e.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return files, size


def empty_dir(d: Path) -> None:
    for child in d.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def already_extracted(dest: Path) -> bool:
    try:
        return dest.is_dir() and any(dest.iterdir())
    except OSError:
        return False


def fmt_error(exc: BaseException) -> str:
    if isinstance(exc, zipfile.BadZipFile):
        return f"corrupted or invalid ZIP structure ({exc})"
    if isinstance(exc, tarfile.TarError):
        return f"corrupted or invalid TAR archive ({exc})"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror + (f" ({exc.filename})" if exc.filename else "")
    return str(exc).strip() or type(exc).__name__


# ── format detection ─────────────────────────────────────────────────────────

def detect_format(src: Path) -> str:
    """Sniff magic bytes first (a .cbz that is really a RAR is common)."""
    try:
        with open(src, "rb") as f:
            head = f.read(8)
    except OSError:
        head = b""
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    low = src.name.lower()
    if low.endswith(TAR_SUFFIXES) or src.suffix.lower() in STREAM_EXTS:
        return "tar"
    if src.suffix.lower() in (".zip", ".cbz"):
        return "zip"                     # broken zip: let zipfile explain why
    return "other"


# ── extractors ───────────────────────────────────────────────────────────────

def _noop(_f: float) -> None:
    pass


def _copy_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path,
                 cb, done: int, total: int) -> int:
    with zf.open(info) as sf, open(target, "wb") as df:
        while True:
            chunk = sf.read(COPY_BUFSIZE)
            if not chunk:
                break
            if STOP.is_set():
                raise Interrupted()
            df.write(chunk)
            done += len(chunk)
            cb(done / total)
    try:
        ts = time.mktime(tuple(info.date_time) + (0, 0, -1))
        os.utime(target, (ts, ts))
    except (OverflowError, ValueError, OSError):
        pass
    return done


def extract_zip(src: Path, dest: Path, cb=_noop) -> None:
    """Extract ZIP/CBZ/EPUB preserving the internal folder structure."""
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        total = sum(i.file_size for i in infos) or 1
        done = 0
        made: set = set()
        for info in infos:
            if STOP.is_set():
                raise Interrupted()
            name = zip_name(info)
            rel = safe_relpath(name)
            if rel is None:
                continue
            target = dest / rel
            if name.replace("\\", "/").endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.flag_bits & 0x1:
                raise RuntimeError("password-protected archive (encrypted entries)")
            if target.parent not in made:
                target.parent.mkdir(parents=True, exist_ok=True)
                made.add(target.parent)
            done = _copy_member(zf, info, unique_path(target), cb, done, total)
    cb(1.0)


_HAS_TAR_FILTER = hasattr(tarfile, "data_filter")


def extract_tar(src: Path, dest: Path, cb=_noop) -> None:
    with tarfile.open(src, "r:*") as tf:
        members = tf.getmembers()
        total = len(members) or 1
        for i, m in enumerate(members, 1):
            if STOP.is_set():
                raise Interrupted()
            if _HAS_TAR_FILTER:
                tf.extract(m, dest, filter="data")     # hardened extraction
            else:
                rel = safe_relpath(m.name)
                if rel is None or m.issym() or m.islnk() or m.isdev():
                    continue
                m.name = rel.as_posix()
                tf.extract(m, dest)
            cb(i / total)


def extract_external(src: Path, dest: Path, cb=_noop) -> None:
    """7-Zip -> unrar (RAR only) -> patool, first one that works wins."""
    attempts = []
    seven = find_7z()
    if seven:
        attempts.append(("7z", [seven, "x", str(src), f"-o{dest}", "-y", "-aoa",
                                "-bso0", "-bsp1"], cb))
    unrar = find_unrar()
    if unrar and (src.suffix.lower() in RAR_EXTS or ".part" in src.name.lower()
                  or ".rar" in src.name.lower()):
        attempts.append(("unrar", [unrar, "x", "-y", "-o+", "-idq",
                                   str(src), str(dest) + os.sep], None))
    patool = find_patool()
    if patool:
        attempts.append(("patool", [patool, "--outdir", str(dest), "extract", str(src)], None))
    if not attempts:
        raise RuntimeError(f"no tool available to extract {src.suffix.upper() or 'this file'}: "
                           "install 7-Zip (recommended), unrar or patool")
    errors = []
    for label, cmd, progress in attempts:
        try:
            run_tool(cmd, progress)
            cb(1.0)
            return
        except Interrupted:
            raise
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            empty_dir(dest)
    raise RuntimeError("all tools failed:\n" + "\n".join(f"  * {e}" for e in errors))


def _has_external() -> bool:
    return bool(find_7z() or find_patool())


def extract_zip_robust(src: Path, dest: Path, cb=_noop) -> None:
    try:
        extract_zip(src, dest, cb)
    except (zipfile.BadZipFile, NotImplementedError) as exc:
        if not _has_external():
            raise
        empty_dir(dest)
        try:
            extract_external(src, dest, cb)
        except Interrupted:
            raise
        except Exception as exc2:
            raise RuntimeError(f"{fmt_error(exc)}; fallback failed: {exc2}") from None


def extract_archive(src: Path, dest: Path, cb=_noop) -> None:
    fmt = detect_format(src)
    if fmt == "zip":
        extract_zip_robust(src, dest, cb)
    elif fmt == "tar":
        try:
            extract_tar(src, dest, cb)
        except Interrupted:
            raise
        except Exception as exc:
            if not _has_external():
                raise
            empty_dir(dest)
            extract_external(src, dest, cb)
    else:
        extract_external(src, dest, cb)


# ── EPUB ─────────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _ZipIndex:
    """Name lookup (exact, then case-insensitive) with repaired names."""
    def __init__(self, zf: zipfile.ZipFile):
        self.order: list[tuple[zipfile.ZipInfo, str]] = []
        self.exact: dict = {}
        self.lower: dict = {}
        for info in zf.infolist():
            name = zip_name(info).replace("\\", "/")
            if name.endswith("/"):
                continue
            self.order.append((info, name))
            self.exact.setdefault(name, (info, name))
            self.lower.setdefault(name.lower(), (info, name))

    def get(self, path: str):
        path = path.lstrip("/")
        return self.exact.get(path) or self.lower.get(path.lower())


def _resolve(base_dir: str, href: str) -> str | None:
    href = href.split("#", 1)[0].split("?", 1)[0].strip()
    if not href or re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.I):
        return None                                   # data:, http:, ...
    href = unquote(href)
    path = href.lstrip("/") if href.startswith("/") \
        else posixpath.normpath(posixpath.join(base_dir, href))
    return None if path.startswith("..") else path.lstrip("/")


_IMG_TAG_RE = re.compile(r"<(?:img|image)\b([^>]*)>", re.I | re.S)
_REF_ATTR_RE = re.compile(r"""\s(?:xlink:)?(?:src|href)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _html_image_refs(text: str) -> list[str]:
    refs = []
    for m in _IMG_TAG_RE.finditer(text):
        for a in _REF_ATTR_RE.finditer(" " + m.group(1)):
            refs.append(a.group(1) or a.group(2) or "")
    return refs


def _encrypted_paths(zf: zipfile.ZipFile, idx: _ZipIndex) -> set:
    hit = idx.get("META-INF/encryption.xml")
    out: set = set()
    if not hit:
        return out
    try:
        root = ET.fromstring(zf.read(hit[0]))
    except Exception:
        return out
    for ed in root.iter():
        if _local(ed.tag) != "EncryptedData":
            continue
        algo = uri = None
        for el in ed.iter():
            n = _local(el.tag)
            if n == "EncryptionMethod":
                algo = el.get("Algorithm")
            elif n == "CipherReference":
                uri = el.get("URI")
        if uri and algo not in FONT_OBFUSCATION:
            out.add(unquote(uri).lstrip("/").lower())
    return out


def epub_image_list(zf: zipfile.ZipFile) -> tuple[list, list]:
    """
    Return ([(ZipInfo, output_basename), ...] in READING ORDER, warnings).

    The old approach (every member with an image extension, in zip order)
    fails on real-world EPUBs because it ignores how the book is built:
      * zip order is not reading order (page 10 before page 2 ...)
      * images sharing a file name in different folders collide
      * images can have odd/missing extensions (the OPF media-type is the truth)
      * names can be percent-encoded / mixed-case in the XHTML vs the zip
      * DRM-encrypted images were extracted as unreadable garbage
    Here we follow container.xml -> OPF manifest/spine -> each XHTML page's
    <img>/<svg:image> references, then append leftovers, then fall back to a
    plain extension scan if the book structure is broken.
    """
    idx = _ZipIndex(zf)
    warnings: list[str] = []
    protected = _encrypted_paths(zf, idx)
    drm_hits = [0]
    ordered: list = []
    seen: set = set()

    def add(path: str | None, mime: str | None = None) -> None:
        if not path:
            return
        hit = idx.get(path)
        if hit is None:
            return
        info, name = hit
        if info.filename in seen:
            return
        base = posixpath.basename(name)
        stem, ext = posixpath.splitext(base)
        if ext.lower() not in IMAGE_EXTS:
            new_ext = IMAGE_MIME_EXT.get((mime or "").lower())
            if not new_ext:
                return
            base = (stem or base) + new_ext
        if name.lower() in protected:
            drm_hits[0] += 1
            seen.add(info.filename)
            return
        seen.add(info.filename)
        ordered.append((info, base))

    try:
        opf_path = None
        container = idx.get("META-INF/container.xml")
        if container:
            root = ET.fromstring(zf.read(container[0]))
            for el in root.iter():
                if _local(el.tag) == "rootfile" and el.get("full-path"):
                    opf_path = unquote(el.get("full-path"))
                    break
        if not opf_path:
            opf_path = next((n for _, n in idx.order if n.lower().endswith(".opf")), None)
        if not opf_path:
            raise ValueError("no OPF package found")
        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(zf.read(idx.get(opf_path)[0]))

        manifest: dict = {}
        spine: list = []
        cover_ids: set = set()
        for el in opf.iter():
            n = _local(el.tag)
            if n == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = (_resolve(opf_dir, el.get("href")),
                                          el.get("media-type") or "",
                                          el.get("properties") or "")
            elif n == "itemref" and el.get("idref"):
                spine.append(el.get("idref"))
            elif n == "meta" and (el.get("name") or "").lower() == "cover":
                cover_ids.add(el.get("content"))

        # 1) declared cover first
        for iid, (path, mime, props) in manifest.items():
            if mime.startswith("image/") and ("cover-image" in props.split() or iid in cover_ids):
                add(path, mime)
        # 2) images in the order the pages are read
        for iid in spine:
            path, mime, _ = manifest.get(iid, (None, "", ""))
            if not path:
                continue
            if mime.startswith("image/"):
                add(path, mime)
                continue
            hit = idx.get(path)
            if hit is None:
                continue
            text = zf.read(hit[0]).decode("utf-8", "replace")
            page_dir = posixpath.dirname(path)
            for ref in _html_image_refs(text):
                add(_resolve(page_dir, ref))
        # 3) manifest images not referenced by any spine page
        for path, mime, _ in manifest.values():
            if mime.startswith("image/"):
                add(path, mime)
    except Exception as exc:
        warnings.append(f"EPUB structure unreadable ({fmt_error(exc)}); used file scan")

    # 4) safety net: anything with an image extension not seen yet
    for info, name in idx.order:
        if posixpath.splitext(name)[1].lower() in IMAGE_EXTS:
            add(name)

    if drm_hits[0]:
        warnings.append(f"{drm_hits[0]} DRM-protected image(s) skipped (cannot be extracted)")
    return ordered, warnings


def extract_epub_images(src: Path, dest: Path, cb=_noop, naming: str = "ORIGINAL") -> tuple[list, int, int]:
    with zipfile.ZipFile(src) as zf:
        images, warnings = epub_image_list(zf)
        total = sum(i.file_size for i, _ in images) or 1
        width = max(3, len(str(len(images))))
        done = 0
        for n, (info, base) in enumerate(images, 1):
            if STOP.is_set():
                raise Interrupted()
            if naming == "SEQUENTIAL":
                name = f"{n:0{width}d}{posixpath.splitext(base)[1].lower()}"
            else:
                name = clean_part(base) or f"image_{n:0{width}d}"
            done = _copy_member(zf, info, unique_path(dest / name), cb, done, total)
    cb(1.0)
    # We already know exactly which image members were written, so avoid a
    # second filesystem walk just to reconstruct these statistics.
    return warnings, len(images), sum(i.file_size for i, _ in images)



# ── CBZ image-only extraction ────────────────────────────────────────────────

def _is_cbz_cover_name(stem: str, ext: str) -> bool:
    """Return True for semantic CBZ cover names, case-insensitively.

    The extension is deliberately checked against the same IMAGE_EXTS set used
    for CBZ pages, so cover.jpg, cover.jpeg, cover.png, cover.webp, etc. are
    handled identically without tying the detector to one particular format.
    """
    return stem.casefold() in {"cover", "frontcover", "front_cover"} and ext.lower() in IMAGE_EXTS


def _cbz_web_image_members(zf: zipfile.ZipFile) -> tuple[list, list] | None:
    """Detect web-saved CBZ layouts such as *_files/images + *_files/cover.*.

    Returns (covers, pages) only when the pattern is strong enough to be
    considered intentional.  AUTO mode relies on this conservative detector so
    ordinary CBZ files keep their complete internal structure.
    """
    infos = [i for i in zf.infolist() if not i.is_dir()]
    candidates: dict[str, list] = {}
    for info in infos:
        name = info.filename.replace("\\", "/")
        low = name.lower()
        parts = name.split("/")
        if len(parts) < 3 or parts[-2].lower() != "images":
            continue
        parent = "/".join(parts[:-2])
        if not parent.lower().endswith("_files"):
            continue
        if posixpath.splitext(low)[1] in IMAGE_EXTS:
            candidates.setdefault(parent, []).append(info)
    if not candidates:
        return None

    # A real page directory normally contains many files.  Requiring at least
    # two avoids treating an incidental *_files/images asset directory as a book.
    parent, pages = max(candidates.items(), key=lambda kv: len(kv[1]))
    if len(pages) < 2:
        return None
    pages.sort(key=lambda i: natural_key(posixpath.basename(i.filename.replace("\\", "/"))))

    covers = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if posixpath.dirname(name) != parent:
            continue
        stem, ext = posixpath.splitext(posixpath.basename(name))
        if _is_cbz_cover_name(stem, ext):
            covers.append(info)
    covers.sort(key=lambda i: natural_key(posixpath.basename(i.filename.replace("\\", "/"))))
    return covers, pages


def _cbz_all_image_members(zf: zipfile.ZipFile) -> list:
    images = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if posixpath.splitext(name)[1].lower() in IMAGE_EXTS:
            images.append(info)
    images.sort(key=lambda i: natural_key(i.filename.replace("\\", "/")))
    return images


def extract_cbz_images(src: Path, dest: Path, cb=_noop, naming: str = "ORIGINAL",
                       auto: bool = False) -> tuple[bool, list, int, int]:
    """Extract CBZ images without re-encoding them.

    In AUTO mode, return detected=False without writing anything when the
    conservative web-layout detector does not match.  In forced IMAGES mode,
    all image members are flattened into the destination.
    """
    warnings: list[str] = []
    with zipfile.ZipFile(src) as zf:
        detected = _cbz_web_image_members(zf)
        if auto and detected is None:
            return False, [], 0, 0

        if detected is not None:
            covers, pages = detected
            selected = covers + pages
            cover_ids = {id(i) for i in covers}
        else:
            selected = _cbz_all_image_members(zf)
            cover_ids = set()

        total = sum(i.file_size for i in selected) or 1
        width = max(3, len(str(len(selected))))
        done = 0
        written = 0
        out_bytes = 0
        for n, info in enumerate(selected, 1):
            if STOP.is_set():
                raise Interrupted()
            base = clean_part(posixpath.basename(info.filename.replace("\\", "/"))) or f"image_{n:0{width}d}"
            if naming == "SEQUENTIAL" and id(info) not in cover_ids:
                ext = posixpath.splitext(base)[1].lower()
                # Covers retain their descriptive filename; only page images
                # are sequentially renamed.
                page_no = n - len(cover_ids)
                base = f"{page_no:0{width}d}{ext}"
            target = dest / base
            chosen = unique_path(target)
            if chosen != target:
                # Never overwrite a page when two archive members flatten to
                # the same basename. Surface the rename instead of hiding it.
                warnings.append(f"image name collision: {base} -> {chosen.name}")
            done = _copy_member(zf, info, chosen, cb, done, total)
            written += 1
            out_bytes += info.file_size
    cb(1.0)
    return True, warnings, written, out_bytes


def extract_tree_images(source: Path, dest: Path, cb=_noop, naming: str = "ORIGINAL") -> tuple[list, int, int]:
    """Copy image files from an already-extracted tree into *dest*.

    Used for non-ZIP CBZ containers (for example a .cbz that is physically
    RAR/7z) when the user explicitly requests IMAGES mode. Files are copied
    byte-for-byte; no decoding or re-encoding is performed.
    """
    images: list[Path] = []
    for root, dirs, files in os.walk(source):
        dirs.sort(key=natural_key)
        for name in sorted(files, key=natural_key):
            p = Path(root) / name
            if p.suffix.lower() in IMAGE_EXTS:
                images.append(p)
    images.sort(key=lambda p: natural_key(p.relative_to(source).as_posix()))

    total = sum(p.stat().st_size for p in images) or 1
    width = max(3, len(str(len(images))))
    done = 0
    out_bytes = 0
    warnings: list[str] = []
    for n, src_img in enumerate(images, 1):
        if STOP.is_set():
            raise Interrupted()
        base = clean_part(src_img.name) or f"image_{n:0{width}d}{src_img.suffix.lower()}"
        if naming == "SEQUENTIAL":
            base = f"{n:0{width}d}{src_img.suffix.lower()}"
        target = dest / base
        chosen = unique_path(target)
        if chosen != target:
            warnings.append(f"image name collision: {base} -> {chosen.name}")
        shutil.copyfile(src_img, chosen)
        size = src_img.stat().st_size
        done += size
        out_bytes += size
        cb(done / total)
    cb(1.0)
    return warnings, len(images), out_bytes


# ── planning ─────────────────────────────────────────────────────────────────

@dataclass
class Options:
    paths: list
    output: Path | None
    delete: bool
    epub_mode: str              # "images" | "structure"
    cbz_mode: str               # "auto" | "images" | "structure"
    recursive: bool
    subfolder: bool
    workers: int
    dry_run: bool
    quiet: bool
    confirm_delete: bool
    naming: str
    cbz_naming: str
    open_when_done: bool | None     # None = auto (one explicit input path)
    no_args: bool
    warnings: list = field(default_factory=list)


@dataclass
class Spec:
    kind: str                   # "archive" | "epub" | "part"
    stem: str
    parts: list


@dataclass
class Item:
    src: Path
    kind: str
    dest: Path
    parts: list
    rel_parent: Path = Path(".")
    epub_mode: str = "images"
    cbz_mode: str = "structure"
    size: int = 0
    status: str = "pending"     # pending running ok skip empty error aborted
    message: str = ""
    files: int = 0
    out_bytes: int = 0
    elapsed: float = 0.0
    deleted: bool = False
    warnings: list = field(default_factory=list)
    progress: float = 0.0

    def set_progress(self, f: float) -> None:
        self.progress = max(self.progress, min(1.0, f))

    @property
    def finished(self) -> bool:
        return self.status not in ("pending", "running")

    @property
    def label(self) -> str:
        return str(self.rel_parent / self.src.name) if str(self.rel_parent) != "." else self.src.name


_RAR_PART_RE = re.compile(r"\.part0*(\d+)\.rar$", re.I)
_SPLIT_RE = re.compile(r"\.(7z|zip|rar)\.(\d{3})$", re.I)


@lru_cache(maxsize=64)
def _listing(folder: str) -> tuple:
    try:
        return tuple(sorted((e.name for e in os.scandir(folder) if e.is_file()),
                            key=natural_key))
    except OSError:
        return ()


def _siblings(folder: Path, rx) -> list:
    return [folder / n for n in _listing(str(folder)) if rx.search(n)]


def classify(p: Path) -> Spec | None:
    name, low = p.name, p.name.lower()
    if low.endswith(".epub"):
        return Spec("epub", name[:-5], [p])
    m = _RAR_PART_RE.search(name)
    if m:
        if int(m.group(1)) != 1:
            return Spec("part", "", [p])
        prefix = name[:m.start()]
        parts = _siblings(p.parent, re.compile(re.escape(prefix) + r"\.part0*\d+\.rar$", re.I))
        return Spec("archive", prefix, parts or [p])
    m = _SPLIT_RE.search(name)
    if m:
        if m.group(2) != "001":
            return Spec("part", "", [p])
        parts = _siblings(p.parent, re.compile(re.escape(name[:m.start(2)]) + r"\d{3}$", re.I))
        return Spec("archive", name[:m.start()], parts or [p])
    for dbl in DOUBLE_EXTS:
        if low.endswith(dbl):
            return Spec("archive", name[:-len(dbl)], [p])
    ext = p.suffix.lower()
    if ext == ".cbz":
        return Spec("cbz", p.stem, [p])
    if ext in ARCHIVE_EXTS:
        parts = [p]
        if ext == ".rar":                              # old-style .r00 .r01 volumes
            parts += _siblings(p.parent, re.compile(re.escape(p.stem) + r"\.r\d{2,}$", re.I))
        return Spec("archive", p.stem, parts)
    return None


def _same_path(a: Path, b: Path) -> bool:
    """Best-effort same-path comparison that also behaves on non-existent paths."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def iter_dir(root: Path, recursive: bool, skip_dir: Path | None):
    """Yield files under *root* plus their parent path relative to *root*.

    Recursive scans are a snapshot of the source tree taken while the plan is
    built.  Script-owned staging directories, [EXTRACTED], and an explicit -o
    directory are never traversed, preventing old/generated output from being
    treated as new input.  os.walk does not follow directory symlinks.
    """
    if not recursive:
        names = _listing(str(root))
        for n in names:
            yield root / n, Path(".")
        return

    root_abs = root.resolve()
    skip_abs = skip_dir.resolve() if skip_dir else None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        kept = []
        for d in dirnames:
            child = here / d
            if d.startswith(TEMP_PREFIX) or d == SUBFOLDER_NAME:
                continue
            if skip_abs is not None and _same_path(child, skip_abs):
                continue
            kept.append(d)
        dirnames[:] = sorted(kept, key=natural_key)
        try:
            rel = here.resolve().relative_to(root_abs)
        except (OSError, ValueError):
            rel = here.relative_to(root)
        for fn in sorted(filenames, key=natural_key):
            yield here / fn, rel


def build_plan(opts: Options) -> tuple[list, list]:
    notes: list = []
    found: list = []                                   # (file, rel_parent, explicit, root)
    skip_dir = opts.output.resolve() if opts.output else None
    for p in opts.paths:
        if not p.exists():
            notes.append(("error", f"Path not found: {p}"))
        elif p.is_file():
            found.append((p, Path("."), True, None))
        else:
            for f, rel in iter_dir(p, opts.recursive, skip_dir):
                found.append((f, rel, False, p))

    items: list = []
    seen: set = set()
    for f, rel, explicit, root in found:
        key = os.path.normcase(str(f.resolve()))
        if key in seen:
            continue
        seen.add(key)
        spec = classify(f)
        if spec is None:
            if explicit:
                notes.append(("warn", f"Unsupported file type: {f.name}"))
            continue
        if spec.kind == "part":
            if explicit:
                notes.append(("warn", f"{f.name} is a continuation volume; pass the first volume instead"))
            continue
        mode = opts.epub_mode if spec.kind == "epub" else "images"
        cbz_mode = opts.cbz_mode if spec.kind == "cbz" else "structure"
        if opts.output:
            # -o selects the output root; -s, when combined with it, adds
            # the common [EXTRACTED] container inside that root.
            base = (opts.output / SUBFOLDER_NAME / rel) if opts.subfolder else (opts.output / rel)
        elif opts.subfolder:
            # -s: folder input -> FOLDER/[EXTRACTED]; single-file input -> parent/[EXTRACTED]
            base = (root / SUBFOLDER_NAME / rel) if root is not None else (f.parent / SUBFOLDER_NAME)
        else:
            base = f.parent
        # structure mode: "<name> .epub" -- the space keeps it distinct from
        # the "<name>.epub" file that sits next to it.
        name = f"{spec.stem} .epub" if (spec.kind == "epub" and mode == "structure") else spec.stem
        size = 0
        for part in spec.parts:
            try:
                size += part.stat().st_size
            except OSError:
                pass
        items.append(Item(src=f, kind=spec.kind, dest=base / (name or "extracted"),
                          parts=spec.parts, rel_parent=rel, epub_mode=mode,
                          cbz_mode=cbz_mode, size=size))

    # two sources that would land in the same folder (a.zip + a.cbz):
    # later ones keep their extension in the folder name.
    used: set = set()
    for it in items:
        key = os.path.normcase(str(it.dest))
        n = 1
        while key in used:
            n += 1
            suffix = it.src.name if n == 2 else f"{it.src.name} ({n})"
            it.dest = it.dest.with_name(suffix)
            key = os.path.normcase(str(it.dest))
        used.add(key)

    for it in items:
        if it.dest.exists() and not it.dest.is_dir():
            it.status, it.message = "error", "a file with the destination name already exists"
        elif already_extracted(it.dest):
            it.status = "skip"
    return items, notes


# ── worker ───────────────────────────────────────────────────────────────────

def _commit(tmp: Path, dest: Path) -> None:
    if dest.exists():
        try:
            dest.rmdir()                                # empty leftover only
        except OSError:
            raise RuntimeError("destination folder appeared during extraction") from None
    for attempt in range(6):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:                         # antivirus / indexer lock
            if attempt == 5:
                raise
            time.sleep(0.25 * (attempt + 1))


def _delete_originals(item: Item) -> None:
    ok = True
    for p in item.parts:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            ok = False
            item.warnings.append(f"could not delete {p.name}: {fmt_error(exc)}")
    item.deleted = ok


def process_item(item: Item, opts: Options) -> None:
    """Runs in a worker thread. Never raises."""
    t0 = time.monotonic()
    item.status = "running"
    # A unique temp name makes simultaneous instances safe: two processes may
    # legitimately extract the same source at once without deleting or writing
    # into each other's staging directory.
    token = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
    tmp = item.dest.parent / f"{TEMP_PREFIX}{item.dest.name}-{token}"
    known_stats: tuple[int, int] | None = None
    try:
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        cb = item.set_progress
        if item.kind == "epub":
            if item.epub_mode == "structure":
                extract_zip_robust(item.src, tmp, cb)
            else:
                warnings, files, size = extract_epub_images(item.src, tmp, cb, opts.naming)
                item.warnings.extend(warnings)
                known_stats = (files, size)
        elif item.kind == "cbz":
            cbz_format = detect_format(item.src)
            if item.cbz_mode == "structure":
                # CBZ is a naming convention, not a guarantee of ZIP. Honour
                # magic bytes so RAR/7z-backed .cbz files still work.
                extract_archive(item.src, tmp, cb)
            elif cbz_format != "zip":
                if item.cbz_mode == "auto":
                    # The conservative web-style detector is ZIP-specific. For
                    # other physical containers AUTO means preserve everything.
                    extract_archive(item.src, tmp, cb)
                else:  # forced IMAGES on a non-ZIP CBZ
                    raw = tmp.parent / f"{tmp.name}-raw"
                    raw.mkdir()
                    try:
                        extract_archive(item.src, raw, _noop)
                        warnings, files, size = extract_tree_images(
                            raw, tmp, cb, opts.cbz_naming)
                        item.warnings.extend(warnings)
                        known_stats = (files, size)
                    finally:
                        shutil.rmtree(raw, ignore_errors=True)
            elif item.cbz_mode == "images":
                _detected, warnings, files, size = extract_cbz_images(
                    item.src, tmp, cb, opts.cbz_naming, auto=False)
                item.warnings.extend(warnings)
                known_stats = (files, size)
            else:  # AUTO: filter only a confidently recognised web-style CBZ
                detected, warnings, files, size = extract_cbz_images(
                    item.src, tmp, cb, opts.cbz_naming, auto=True)
                item.warnings.extend(warnings)
                if detected:
                    known_stats = (files, size)
                else:
                    # AUTO did not match: preserve the archive exactly as the
                    # historical CBZ extractor did.
                    extract_zip_robust(item.src, tmp, cb)
        else:
            extract_archive(item.src, tmp, cb)

        # External/general archive extraction needs a verification walk because
        # the extractor is authoritative. EPUB-images already reports exact
        # stats while copying, so do not scan those files a second time.
        files, size = known_stats if known_stats is not None else dir_stats(tmp)
        if files == 0:
            item.status = "empty"
            if item.kind == "epub" and item.epub_mode == "images":
                item.message = "no images found inside the EPUB"
            elif item.kind == "cbz" and item.cbz_mode == "images":
                item.message = "no images found inside the CBZ"
            else:
                item.message = "archive contains no files"
            return
        _commit(tmp, item.dest)
        item.files, item.out_bytes = files, size
        item.status = "ok"
        if opts.delete:
            _delete_originals(item)
    except Interrupted:
        item.status = "aborted"
    except Exception as exc:
        item.status, item.message = "error", fmt_error(exc)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        item.elapsed = time.monotonic() - t0
        item.progress = 1.0


# ── console / UI ─────────────────────────────────────────────────────────────

G_UNI = dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│", ok="✔", err="✘",
             warn="⚠", skip="↷", arrow="→", bar_full="█", bar_empty="░",
             spin="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", pointer="▸", sub="↳", dot="·")
G_ASCII = dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", ok="OK", err="XX",
               warn="!!", skip="--", arrow="->", bar_full="#", bar_empty=".",
               spin="|/-\\", pointer=">", sub="`-", dot="|")
_ANSI = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
         "blue": "34", "magenta": "35", "cyan": "36", "gray": "90"}


class Console:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.lock = threading.RLock()
        self.tty = bool(sys.stdout) and sys.stdout.isatty()
        self.vt = self.tty and enable_vt()
        self.color = self.vt and "NO_COLOR" not in os.environ
        enc = (getattr(sys.stdout, "encoding", None) or "").lower()
        uni = "utf" in enc or (os.name == "nt" and self.tty)
        self.g = G_UNI if (uni and not os.environ.get("UNPACKER_ASCII")) else G_ASCII
        self.live_on = False
        self.live_text = ""

    # -- primitives
    def cols(self) -> int:
        return shutil.get_terminal_size((100, 24)).columns

    def c(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return f"\x1b[{';'.join(_ANSI[s] for s in styles)}m{text}\x1b[0m"

    def _clear(self) -> None:
        if self.live_on and self.live_text:
            sys.stdout.write("\r\x1b[2K")

    def _draw(self) -> None:
        if self.live_on and self.live_text:
            sys.stdout.write(self.live_text)
            sys.stdout.flush()

    def out(self, text: str = "", force: bool = False) -> None:
        if self.quiet and not force:
            return
        with self.lock:
            self._clear()
            print(text)
            self._draw()

    def set_status(self, text: str) -> None:
        if not self.live_on:
            return
        with self.lock:
            self._clear()
            self.live_text = text
            self._draw()
            if not text:
                sys.stdout.flush()

    def start_live(self) -> None:
        if self.vt and not self.quiet:
            self.live_on = True
            sys.stdout.write("\x1b[?25l")               # hide cursor

    def stop_live(self) -> None:
        with self.lock:
            if self.live_on:
                self._clear()
                self.live_text = ""
                sys.stdout.write("\x1b[?25h")           # show cursor
                sys.stdout.flush()
                self.live_on = False

    # -- composite pieces
    def rule_width(self) -> int:
        return max(30, min(self.cols() - 2, 78))

    def rule(self, title: str) -> str:
        h = self.g["h"]
        left = f"{h}{h} {title} "
        fill = max(2, self.rule_width() - dwidth(left) - 2)
        return "  " + self.c(h * 2, "gray") + " " + self.c(title, "bold") + " " + self.c(h * fill, "gray")

    def header(self) -> None:
        g = self.g
        inner = 58
        title, ver = f"  {APP_NAME}", "by Lord Karasuma  "
        pad = inner - len(title) - len(ver)
        edge = lambda s: self.c(s, "cyan")
        self.out()
        self.out("  " + edge(g["tl"] + g["h"] * inner + g["tr"]))
        self.out("  " + edge(g["v"]) + self.c(title, "bold") + " " * pad
                 + self.c(ver, "gray") + edge(g["v"]))
        self.out("  " + edge(g["bl"] + g["h"] * inner + g["br"]))

    def kv(self, key: str, value: str) -> None:
        self.out(f"  {self.c(f'{key:<8}', 'gray')} {value}")

    def note(self, level: str, text: str) -> None:
        g = self.g
        icon, style = (g["err"], "red") if level == "error" else (g["warn"], "yellow")
        self.out(f"  {self.c(icon, style, 'bold')} {self.c(text, style)}", force=(level == "error"))

    def item_line(self, it: Item, idx: int | None = None, show_dest: bool = True) -> None:
        g = self.g
        tag = "EPUB" if it.kind == "epub" else ("CBZ " if it.kind == "cbz" else "ARC ")
        extra = ""
        if it.kind == "epub":
            extra = self.c("  [full structure]" if it.epub_mode == "structure" else "  [images]", "gray")
        elif it.kind == "cbz":
            label = {"auto": "auto", "images": "images", "structure": "full structure"}[it.cbz_mode]
            extra = self.c(f"  [{label}]", "gray")
        head = f"  {idx:>3}  " if idx is not None else "  "
        self.out(f"{head}{self.c(tag, 'magenta')}  {self.c(f'{fmt_size(it.size):>9}', 'gray')}  {it.label}{extra}")
        if show_dest:
            self.out(self.c(f"           {g['sub']} {it.dest.name}/", "gray"))

    def plan(self, items: list, opts: Options) -> None:
        g = self.g
        total = sum(i.size for i in items)
        self.out()
        self.out(self.rule(f"PLAN {g['dot']} {len(items)} item(s) {g['dot']} {fmt_size(total)}"))
        self.out()
        many = len(items) > 25
        for n, it in enumerate(items, 1):
            self.item_line(it, n, show_dest=not many and it.status != "skip")
            if it.status == "skip":
                self.out(self.c(f"           {g['skip']} already extracted {g['dot']} will be skipped", "yellow"))
            elif it.status == "error":
                self.out(self.c(f"           {g['err']} {it.message}", "red"), force=True)

    def result(self, it: Item) -> None:
        g = self.g
        if it.status == "ok":
            det = f"{it.files} file(s) {g['dot']} {fmt_size(it.out_bytes)} {g['dot']} {fmt_time(it.elapsed)}"
            if it.deleted:
                det += f" {g['dot']} original deleted"
            self.out(f"  {self.c(g['ok'], 'green', 'bold')} {it.label}")
            self.out(self.c(f"      {g['sub']} {it.dest.name}/   {det}", "gray"))
        elif it.status == "empty":
            self.out(f"  {self.c(g['warn'], 'yellow', 'bold')} {it.label}")
            self.out(self.c(f"      {g['sub']} {it.message}", "yellow"))
        elif it.status == "error":
            self.out(f"  {self.c(g['err'], 'red', 'bold')} {it.label}", force=True)
            for line in it.message.splitlines() or ["unknown error"]:
                self.out(self.c(f"      {line}", "red"), force=True)
        elif it.status == "aborted":
            self.out(f"  {self.c(g['skip'], 'yellow', 'bold')} {it.label} {self.c('(interrupted)', 'gray')}")
        for w in it.warnings:
            self.out(self.c(f"      {g['warn']} {w}", "yellow"))


class Live(threading.Thread):
    """Animated one-line status: overall bar, counters, elapsed, active item."""
    def __init__(self, con: Console, items: list):
        super().__init__(daemon=True)
        self.con, self.items = con, items
        self.stop_ev = threading.Event()
        self.t0 = time.monotonic()
        self.frame = 0

    def render(self) -> str:
        con, g = self.con, self.con.g
        total = len(self.items) or 1
        done = sum(1 for i in self.items if i.finished)
        # Weight progress by input bytes so a 5 MB EPUB no longer counts as much
        # as a 20 GB archive. Zero-size/unknown inputs still get a weight of 1.
        weights = [max(1, i.size) for i in self.items]
        weight_total = sum(weights) or 1
        frac = sum(w * (1.0 if i.finished else i.progress)
                   for i, w in zip(self.items, weights)) / weight_total
        active = [i for i in self.items if i.status == "running"]
        barw = 22
        filled = int(round(frac * barw))
        elapsed = fmt_clock(time.monotonic() - self.t0)
        spin = g["spin"][self.frame % len(g["spin"])]
        counters = f"{done}/{total}"
        head = ("  " + con.c(spin, "cyan") + " "
                + con.c(g["bar_full"] * filled, "cyan")
                + con.c(g["bar_empty"] * (barw - filled), "gray")
                + f" {int(frac * 100):3d}%  {counters}  {elapsed}")
        head_len = 2 + 1 + 1 + barw + 1 + 4 + 2 + len(counters) + 2 + len(elapsed)
        tail = ""
        avail = con.cols() - 1 - head_len - 4
        if active and avail > 8:
            name = active[0].src.name + (f" +{len(active) - 1}" if len(active) > 1 else "")
            tail = "  " + con.c(g["pointer"], "gray") + " " + con.c(clip(name, avail), "gray")
        return head + tail

    def run(self) -> None:
        while not self.stop_ev.wait(0.1):
            self.frame += 1
            self.con.set_status(self.render())

    def finish(self) -> None:
        self.stop_ev.set()
        self.join(timeout=1)


# ── orchestration ────────────────────────────────────────────────────────────

def confirm_delete(con: Console, items: list) -> bool:
    if not (sys.stdin and sys.stdin.isatty()):
        con.note("error", "-d needs interactive confirmation. In non-interactive runs set "
                          "UNPACKER_CONFIRM_DELETE=0 to skip the prompt.")
        return False
    size = sum(i.size for i in items)
    g = con.g
    con.out()
    con.out(con.rule("WARNING"))
    con.out()
    con.out(f"  {con.c(g['warn'], 'red', 'bold')} {con.c('DESTRUCTIVE OPERATION', 'red', 'bold')}")
    con.out(f"    {len(items)} original file(s) ({fmt_size(size)}) will be PERMANENTLY DELETED")
    con.out("    after a successful extraction. This cannot be undone.")
    con.out()
    try:
        answer = input(f"  Type {DELETE_WORD} (all caps) to confirm, Enter to abort: ").strip()
    except EOFError:
        return False
    return answer == DELETE_WORD


def run_all(con: Console, items: list, opts: Options) -> bool:
    """Extract every pending item. Returns True if interrupted."""
    todo = [i for i in items if i.status == "pending"]
    workers = max(1, min(opts.workers, len(todo)))
    con.out()
    con.out(con.rule(f"EXTRACTING {con.g['dot']} {workers} worker(s)"))
    con.out()
    live = Live(con, todo)
    con.start_live()
    live.start()
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="unpack")
    interrupted = False
    try:
        # biggest first: keeps all workers busy until the very end
        futures = {ex.submit(process_item, it, opts): it
                   for it in sorted(todo, key=lambda i: i.size, reverse=True)}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                fut.result()
            except Exception as exc:                    # process_item should never raise
                it.status, it.message = "error", fmt_error(exc)
            con.result(it)
    except KeyboardInterrupt:
        interrupted = True
        STOP.set()
        with _PROCS_LOCK:
            for p in list(_PROCS):
                try:
                    p.terminate()
                except Exception:
                    pass
        ex.shutdown(wait=True, cancel_futures=True)
    finally:
        ex.shutdown(wait=True)
        live.finish()
        con.stop_live()
    return interrupted


def summary(con: Console, items: list, elapsed: float, interrupted: bool) -> None:
    g = con.g
    n = lambda st: sum(1 for i in items if i.status == st)
    ok, skipped, empty, errors = n("ok"), n("skip"), n("empty"), n("error")
    left = n("pending") + n("running") + n("aborted")
    files = sum(i.files for i in items if i.status == "ok")
    size = sum(i.out_bytes for i in items if i.status == "ok")
    deleted = sum(1 for i in items if i.deleted)
    warns = sum(len(i.warnings) for i in items)
    con.out()
    con.out(con.rule("SUMMARY"), force=True)
    con.out(force=True)
    row = lambda icon, style, label, val, extra="": con.out(
        f"  {con.c(icon, style, 'bold')} {label:<11} {con.c(str(val), 'bold')}"
        + (con.c(f"   {extra}", "gray") if extra else ""), force=True)
    row(g["ok"], "green", "Extracted", ok, f"{files} file(s) {g['dot']} {fmt_size(size)}" if ok else "")
    if skipped:
        row(g["skip"], "yellow", "Skipped", skipped)
    if empty:
        row(g["warn"], "yellow", "Empty", empty)
    if warns:
        row(g["warn"], "yellow", "Warnings", warns)
    if errors:
        row(g["err"], "red", "Errors", errors)
    if left:
        row(g["skip"], "yellow", "Not done", left)
    if deleted:
        row(g["ok"], "cyan", "Deleted", deleted, "originals removed")
    row(g["dot"], "gray", "Time", fmt_time(elapsed))
    con.out(force=True)
    if interrupted:
        con.out(f"  {con.c('Interrupted by user.', 'yellow')}", force=True)


def open_in_explorer(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))                      # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def pause(msg: str) -> None:
    if sys.stdin and sys.stdin.isatty():
        try:
            input(msg)
        except (EOFError, KeyboardInterrupt):
            pass


# ── CLI ──────────────────────────────────────────────────────────────────────

EPILOG = """\
environment (or .env next to the script / in the current folder):
  UNPACKER_MODE=COPY|DELETE        default mode (default COPY). -d forces DELETE,
                                   -c forces COPY. DELETE still asks for the
                                   safety word unless UNPACKER_CONFIRM_DELETE=0
  EPUB_UNPACKER=IMAGES|STRUCTURE   IMAGES (default): images only, reading order,
                                   folder "<name>/".  STRUCTURE: whole EPUB with
                                   its sub-folders, folder "<name> .epub/"
                                   (note the space: never clashes with the file).
  CBZ_UNPACKER=AUTO|IMAGES|STRUCTURE
                                   AUTO (default): for recognised web-style CBZs,
                                   keep cover + page images only; otherwise preserve
                                   the complete CBZ structure. IMAGES forces image-only
                                   extraction; STRUCTURE always preserves everything.
  UNPACKER_CONFIRM_DELETE=0        skip the "type DELETE" prompt for -d
  UNPACKER_SUB_FOLDER=TRUE          same as -s (--no-subfolder overrides it)
  OPEN_UNPACKED_FOLDER=AUTO|NEVER|ALWAYS
                                   opening policy after extraction (default AUTO).
                                   --open / --no-open always override this setting
  EPUB_IMAGE_NAMING=SEQUENTIAL     001.jpg, 002.jpg ... instead of original names
  CBZ_IMAGE_NAMING=SEQUENTIAL      sequential names in CBZ IMAGES mode; ORIGINAL default
  UNPACKER_WORKERS=N               parallel extractions (default: min(4, CPUs))
  UNPACKER_ZIP_ENCODING=cp932      extra encoding tried for non-UTF-8 zip names
  UNPACKER_ASCII=1 / NO_COLOR=1    plain ASCII / no colours
"""


def clean_arg(s: str) -> Path:
    # "C:\\dir\\" on Windows reaches us as C:\\dir" -- strip stray quotes.
    return Path(os.path.expandvars(s.strip().strip('"').strip("'"))).expanduser()


def build_options(argv=None) -> Options:
    ap = argparse.ArgumentParser(
        prog="unpackerfolder", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Extract archives and EPUB books. Always COPY mode unless -d is given.",
        epilog=EPILOG)
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help="folder or single file (default: the script's own folder)")
    ap.add_argument("-o", "--output", metavar="DIR", help="output folder for this session")
    mode_group = ap.add_mutually_exclusive_group()
    mode_group.add_argument("-d", "--delete", action="store_true",
                            help="delete originals after a successful extraction (asks for confirmation)")
    mode_group.add_argument("-c", "--copy", action="store_true",
                            help="force COPY mode for this run (overrides UNPACKER_MODE=DELETE)")
    epub_group = ap.add_mutually_exclusive_group()
    epub_group.add_argument("-k", "--keepepub", action="store_true",
                            help='extract EPUBs whole, with structure, into "<name> .epub/"')
    epub_group.add_argument("--epub-images", action="store_true",
                            help="extract only EPUB images for this run (overrides EPUB_UNPACKER)")
    cbz_group = ap.add_mutually_exclusive_group()
    cbz_group.add_argument("--cbz-images", action="store_true",
                           help="extract only CBZ images for this run (overrides CBZ_UNPACKER)")
    cbz_group.add_argument("--keepcbz", action="store_true",
                           help="preserve the complete internal structure of CBZ files for this run")
    subfolder_group = ap.add_mutually_exclusive_group()
    subfolder_group.add_argument("-s", "--subfolder", dest="subfolder", action="store_true",
                                 help='put extracted results into "[EXTRACTED]" (inside a folder input, or next to a single-file input) '
                                      '(with -o, creates [EXTRACTED] inside the selected output folder)')
    subfolder_group.add_argument("--no-subfolder", dest="subfolder", action="store_false",
                                 help="disable [EXTRACTED] (overrides UNPACKER_SUB_FOLDER=TRUE)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="recursively scan directory inputs (not valid for file inputs)")
    ap.add_argument("-j", "--jobs", type=int, metavar="N", help="parallel extractions")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show the plan, extract nothing")
    ap.add_argument("-q", "--quiet", action="store_true", help="only errors and the summary")
    open_group = ap.add_mutually_exclusive_group()
    open_group.add_argument("--open", dest="open_when_done", action="store_true",
                            help="always open the last extracted folder (default: auto-open for one explicit input path)")
    open_group.add_argument("--no-open", dest="open_when_done", action="store_false",
                            help="never open the extracted folder")
    ap.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    ap.set_defaults(open_when_done=None, subfolder=None)
    args = ap.parse_args(argv)

    warnings: list = []
    run_mode = (os.environ.get("UNPACKER_MODE") or "COPY").strip().upper()
    if run_mode not in ("COPY", "DELETE"):
        warnings.append(f"UNPACKER_MODE={run_mode!r} is not valid (COPY or DELETE); using COPY")
        run_mode = "COPY"
    delete = args.delete or (run_mode == "DELETE" and not args.copy)
    env_mode = (os.environ.get("EPUB_UNPACKER") or "IMAGES").strip().upper()
    if env_mode not in ("IMAGES", "STRUCTURE"):
        warnings.append(f"EPUB_UNPACKER={env_mode!r} is not valid (IMAGES or STRUCTURE); using IMAGES")
        env_mode = "IMAGES"
    epub_mode = "structure" if args.keepepub else ("images" if args.epub_images else env_mode.lower())

    env_cbz = (os.environ.get("CBZ_UNPACKER") or "AUTO").strip().upper()
    if env_cbz not in ("AUTO", "IMAGES", "STRUCTURE"):
        warnings.append(f"CBZ_UNPACKER={env_cbz!r} is not valid (AUTO, IMAGES or STRUCTURE); using AUTO")
        env_cbz = "AUTO"
    cbz_mode = "structure" if args.keepcbz else ("images" if args.cbz_images else env_cbz.lower())

    naming = (os.environ.get("EPUB_IMAGE_NAMING") or "ORIGINAL").strip().upper()
    if naming not in ("ORIGINAL", "SEQUENTIAL"):
        warnings.append(f"EPUB_IMAGE_NAMING={naming!r} is not valid; using ORIGINAL")
        naming = "ORIGINAL"

    cbz_naming = (os.environ.get("CBZ_IMAGE_NAMING") or "ORIGINAL").strip().upper()
    if cbz_naming not in ("ORIGINAL", "SEQUENTIAL"):
        warnings.append(f"CBZ_IMAGE_NAMING={cbz_naming!r} is not valid; using ORIGINAL")
        cbz_naming = "ORIGINAL"

    subfolder = env_flag("UNPACKER_SUB_FOLDER", False) if args.subfolder is None else args.subfolder

    workers = args.jobs
    if workers is None:
        raw_workers = os.environ.get("UNPACKER_WORKERS", "").strip()
        try:
            workers = int(raw_workers) if raw_workers else DEFAULT_WORKERS
        except ValueError:
            warnings.append(f"UNPACKER_WORKERS={raw_workers!r} is not valid; using {DEFAULT_WORKERS}")
            workers = DEFAULT_WORKERS
    if workers < 1:
        warnings.append(f"worker count {workers} is not valid; using 1")
        workers = 1

    no_args = not args.paths
    paths = [clean_arg(p) for p in args.paths] or [Path(__file__).resolve().parent]

    # Recursive mode deliberately has one simple contract: every explicit
    # existing input must be a directory.  Mixing a recursive tree scan with
    # explicitly supplied files makes destination/opening semantics needlessly
    # ambiguous and is almost certainly a command-line mistake.  Missing paths
    # are left to build_plan(), which can report them together with other plan
    # diagnostics instead of disguising them as a type error.
    if args.recursive:
        file_inputs = [p for p in paths if p.exists() and p.is_file()]
        if file_inputs:
            shown = ", ".join(str(p) for p in file_inputs)
            ap.error(f"--recursive can only be used with directory inputs; file input: {shown}")

    # Opening policy: command-line flags always have precedence over .env.
    # None is deliberately preserved for AUTO because the final target then
    # depends on whether the user supplied one explicit input path.
    env_open = (os.environ.get("OPEN_UNPACKED_FOLDER") or "AUTO").strip().upper()
    if env_open not in ("AUTO", "NEVER", "ALWAYS"):
        warnings.append(
            f"OPEN_UNPACKED_FOLDER={env_open!r} is not valid "
            "(AUTO, NEVER or ALWAYS); using AUTO"
        )
        env_open = "AUTO"

    if args.open_when_done is not None:
        open_when_done = args.open_when_done             # explicit CLI wins
    elif env_open == "NEVER":
        open_when_done = False
    elif env_open == "ALWAYS":
        open_when_done = True
    else:
        open_when_done = None                            # AUTO

    return Options(
        paths=paths, output=clean_arg(args.output).resolve() if args.output else None,
        delete=delete, epub_mode=epub_mode, cbz_mode=cbz_mode, recursive=args.recursive, subfolder=subfolder,
        workers=workers, dry_run=args.dry_run, quiet=args.quiet,
        confirm_delete=env_flag("UNPACKER_CONFIRM_DELETE", True),
        naming=naming, cbz_naming=cbz_naming, open_when_done=open_when_done, no_args=no_args,
        warnings=warnings)


def main(argv=None) -> int:
    # main() can be called more than once when imported by tests/other tools.
    # A previous interrupted run must not poison the next invocation.
    STOP.clear()
    with _PROCS_LOCK:
        _PROCS.clear()
    _listing.cache_clear()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")         # odd filenames never crash printing
        except Exception:
            pass
    load_dotenv()
    opts = build_options(argv)
    con = Console(opts.quiet)
    interactive = bool(sys.stdin and sys.stdin.isatty() and con.tty)
    pause_on_exit = opts.no_args and interactive

    con.header()
    con.out()
    where = ", ".join(str(p) for p in opts.paths)
    con.kv("Source", where + ("  (recursive)" if opts.recursive else ""))
    if opts.output:
        effective_output = opts.output / SUBFOLDER_NAME if opts.subfolder else opts.output
        con.kv("Output", str(effective_output))
    elif opts.subfolder:
        con.kv("Output", f'"{SUBFOLDER_NAME}" inside folder inputs / next to single-file inputs')
    else:
        con.kv("Output", "next to each source")
    if opts.delete:
        con.kv("Mode", con.c("DELETE", "red", "bold") + " -- originals removed after successful extraction")
    else:
        con.kv("Mode", con.c("COPY", "green", "bold") + " -- originals are never touched")
    if opts.epub_mode == "structure":
        con.kv("EPUB", 'full structure -> folder "<name> .epub/"')
    else:
        con.kv("EPUB", 'images only, reading order -> folder "<name>/"')
    con.kv("Workers", str(opts.workers))
    for w in opts.warnings:
        con.note("warn", w)

    items, notes = build_plan(opts)
    for level, text in notes:
        con.note(level, text)

    if not items:
        con.out()
        con.out("  No archives or EPUB files found. Nothing to do.")
        con.out()
        if pause_on_exit:
            pause("  Press Enter to exit...")
        return 0

    con.plan(items, opts)
    todo = [i for i in items if i.status == "pending"]

    # Capture input type before workers start. DELETE mode may remove an
    # explicitly supplied source file, so querying is_file()/is_dir() after
    # extraction is not reliable.
    explicit_input_kind = None
    if not opts.no_args and len(opts.paths) == 1:
        explicit_input_kind = "dir" if opts.paths[0].is_dir() else "file" if opts.paths[0].is_file() else None

    if opts.dry_run:
        con.out()
        con.out("  Dry run: nothing was extracted.")
        con.out()
        if pause_on_exit:
            pause("  Press Enter to exit...")
        return 0

    if not todo:
        summary(con, items, 0.0, False)
        if pause_on_exit:
            pause("  Press Enter to exit...")
        return 1 if any(i.status == "error" for i in items) else 0

    if opts.delete and opts.confirm_delete and not confirm_delete(con, todo):
        con.out()
        con.out("  Aborted -- nothing was extracted or deleted.")
        con.out()
        if pause_on_exit:
            pause("  Press Enter to exit...")
        return 2

    t0 = time.monotonic()
    interrupted = run_all(con, items, opts)
    summary(con, items, time.monotonic() - t0, interrupted)

    had_errors = any(i.status == "error" for i in items)

    # Auto-open is based on what the user explicitly asked to unpack, not on
    # how many archives happen to be discovered inside that input.  Therefore
    # one explicit folder is still one operation even if it contains 100 files.
    # No-argument/double-click mode keeps the historical single-item behaviour.
    if opts.open_when_done is None:
        want_open = (len(todo) == 1) if opts.no_args else (len(opts.paths) == 1)
    else:
        want_open = opts.open_when_done

    if want_open and not interrupted:
        successful = [it for it in items if it.status == "ok"]
        if successful:
            target = None

            # In automatic mode with exactly one explicit input, open the
            # operation's common result location.  This is deterministic for
            # folder inputs and avoids choosing one arbitrary extracted child.
            if opts.open_when_done is None and not opts.no_args and len(opts.paths) == 1:
                source = opts.paths[0]
                if explicit_input_kind == "dir":
                    if opts.output:
                        target = (opts.output / SUBFOLDER_NAME) if opts.subfolder else opts.output
                    elif opts.subfolder:
                        target = source / SUBFOLDER_NAME
                    else:
                        target = source
                elif explicit_input_kind == "file":
                    if opts.output:
                        target = (opts.output / SUBFOLDER_NAME) if opts.subfolder else successful[0].dest
                    elif opts.subfolder:
                        target = source.parent / SUBFOLDER_NAME
                    else:
                        target = successful[0].dest

            # Explicit --open (including multiple inputs), and the legacy
            # no-argument case, retain the previous "most recently written
            # extracted folder" behaviour.
            if target is None:
                best, best_t = None, -1.0
                for it in successful:
                    try:
                        mt = it.dest.stat().st_mtime
                    except OSError:
                        continue
                    if mt > best_t:
                        best, best_t = it.dest, mt
                target = best

            if target is not None and target.exists():
                open_in_explorer(target)

    if pause_on_exit:
        pause("  Press Enter to exit...")
    return 130 if interrupted else (1 if had_errors else 0)


if __name__ == "__main__":
    if sys.stdout is None:                                # pythonw.exe
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print()
        pause("  Unexpected error -- press Enter to close ...")
        sys.exit(1)
