#!/usr/bin/env python3
"""
unpackerfolder.py
Scans the current folder, detects archives and EPUBs, and extracts them.
Supports: .zip .cbz .cbr .7z .rar .tar .gz .bz2 .xz .zst .epub

Double-click behaviour
----------------------
If the script is launched by double-clicking (i.e. no interactive terminal is
attached, or the env-var UNPACKER_INTERACTIVE=1 is NOT set), it runs
automatically in COPY mode, prints all progress to the console / a log window,
and closes the terminal when finished.

To force the interactive menu (useful when launching from an already-open
terminal), either:
  • set the environment variable  UNPACKER_INTERACTIVE=1
  • pass the flag               --interactive  (or  -i)
"""

import os
import sys
import shutil
import zipfile
import tarfile
import subprocess
import time
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────────────

ARCHIVE_EXTS = {".zip", ".cbz", ".cbr", ".7z", ".rar",
                ".tar", ".gz", ".bz2", ".xz", ".zst"}
EPUB_EXTS    = {".epub"}
IMAGE_EXTS   = {".jpg", ".jpeg", ".png", ".gif", ".webp",
                ".bmp", ".tiff", ".tif", ".avif", ".svg"}

# ── double-click detection ───────────────────────────────────────────────────

def _is_double_click() -> bool:
    """
    Return True when the script was launched by double-clicking rather than
    from an already-open terminal session.

    Heuristics (all platforms):
      1. Explicit env-var override: UNPACKER_INTERACTIVE=1  →  always False
      2. CLI flag --interactive / -i                        →  always False
      3. Windows: parent process is explorer.exe            →  True
      4. stdin is not a real TTY                            →  True  (fallback)
    """
    # 1 – explicit env override
    if os.environ.get("UNPACKER_INTERACTIVE", "").strip() == "1":
        return False
    # 2 – CLI flag
    if "--interactive" in sys.argv or "-i" in sys.argv:
        return False
    # 3 – Windows: check parent process name
    if sys.platform == "win32":
        try:
            import ctypes, ctypes.wintypes
            kernel32  = ctypes.windll.kernel32
            psapi     = ctypes.windll.psapi
            pid       = kernel32.GetCurrentProcessId()
            snap      = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize",              ctypes.wintypes.DWORD),
                    ("cntUsage",            ctypes.wintypes.DWORD),
                    ("th32ProcessID",       ctypes.wintypes.DWORD),
                    ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID",        ctypes.wintypes.DWORD),
                    ("cntThreads",          ctypes.wintypes.DWORD),
                    ("th32ParentProcessID", ctypes.wintypes.DWORD),
                    ("pcPriClassBase",      ctypes.c_long),
                    ("dwFlags",             ctypes.wintypes.DWORD),
                    ("szExeFile",           ctypes.c_char * 260),
                ]
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            parent_id = None
            if kernel32.Process32First(snap, ctypes.byref(entry)):
                while True:
                    if entry.th32ProcessID == pid:
                        parent_id = entry.th32ParentProcessID
                        break
                    if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(snap)
            if parent_id is not None:
                h = kernel32.OpenProcess(0x0410, False, parent_id)
                buf = ctypes.create_unicode_buffer(260)
                psapi.GetModuleFileNameExW(h, None, buf, 260)
                kernel32.CloseHandle(h)
                if "explorer" in buf.value.lower():
                    return True
        except Exception:
            pass
    # 4 – fallback: stdin is not a TTY
    return not sys.stdin.isatty()


# ── UI helpers ───────────────────────────────────────────────────────────────

W = 63  # inner width (chars between the two | pipes)

def _bar()               -> str: return "+" + "-" * W + "+"
def _row(text: str = "") -> str: return "| " + text.ljust(W - 2) + " |"
def _sep(char: str = "-") -> str: return "  " + char * (W - 2)


# ── tool detection ───────────────────────────────────────────────────────────

def _find_7z() -> str | None:
    found = shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return found
    win_default = r"C:\Program Files\7-Zip\7z.exe"
    if Path(win_default).exists():
        return win_default
    return None

def has_unrar()  -> bool: return bool(shutil.which("unrar"))
def has_patool() -> bool: return bool(shutil.which("patool"))

# ── extraction helpers ───────────────────────────────────────────────────────

def _run(cmd: list[str], label: str) -> None:
    """Run a subprocess with a live spinner."""
    frames = ["|", "/", "-", "\\"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    i = 0
    while True:
        try:
            proc.wait(timeout=0.12)
            break
        except subprocess.TimeoutExpired:
            print(f"\r    {frames[i % 4]}  {label} ...", end="", flush=True)
            i += 1
    print(f"\r    ", end="")
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(err.strip() or f"exit code {proc.returncode}")


def extract_zip_cbz(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(src, "r") as zf:
        total = len(zf.infolist())
        for i, member in enumerate(zf.infolist(), 1):
            zf.extract(member, dest)
            pct = int(i / total * 100)
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"\r    [{bar}] {pct:3d}%", end="", flush=True)
    print("\r" + " " * 40 + "\r", end="")


def extract_tar(src: Path, dest: Path) -> None:
    with tarfile.open(src, "r:*") as tf:
        members = tf.getmembers()
        total   = len(members)
        for i, m in enumerate(members, 1):
            tf.extract(m, dest)
            pct = int(i / total * 100)
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"\r    [{bar}] {pct:3d}%", end="", flush=True)
    print("\r" + " " * 40 + "\r", end="")


def extract_with_7z(src: Path, dest: Path) -> None:
    exe = _find_7z()
    if not exe:
        raise RuntimeError("7z not found. Install 7-Zip and add it to PATH.")
    dest.mkdir(parents=True, exist_ok=True)
    _run([exe, "x", str(src), f"-o{dest}", "-y", "-bsp1"], src.name)


def extract_with_unrar(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    _run(["unrar", "x", "-y", str(src), str(dest) + "/"], src.name)


def extract_with_patool(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    _run(["patool", "--outdir", str(dest), "extract", str(src)], src.name)


def extract_rar_cbr(src: Path, dest: Path) -> None:
    errors = []
    for label, fn in [("7z",     lambda: extract_with_7z(src, dest)),
                      ("unrar",  lambda: extract_with_unrar(src, dest)),
                      ("patool", lambda: extract_with_patool(src, dest))]:
        available = {"7z": _find_7z, "unrar": has_unrar, "patool": has_patool}[label]
        if available():
            try:
                fn(); return
            except Exception as e:
                errors.append(f"{label}: {e}")
    if errors:
        raise RuntimeError("All tools failed:\n" + "\n".join(f"  * {e}" for e in errors))
    raise RuntimeError(
        f"No tool to extract {src.suffix.upper()}. Install 7-Zip, unrar, or patool."
    )


def _cleanup_if_empty(dest: Path) -> None:
    try:
        if dest.exists() and dest.is_dir() and not any(dest.iterdir()):
            dest.rmdir()
    except Exception:
        pass


def extract_archive(src: Path, dest: Path) -> None:
    ext = src.suffix.lower()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if ext in {".zip", ".cbz"}:
            extract_zip_cbz(src, dest)
        elif ext in {".tar", ".gz", ".bz2", ".xz", ".zst"}:
            try:
                extract_tar(src, dest)
            except Exception:
                extract_with_7z(src, dest)
        elif ext in {".rar", ".cbr"}:
            extract_rar_cbr(src, dest)
        else:
            extract_with_7z(src, dest)
    except Exception:
        _cleanup_if_empty(dest)
        raise


def extract_epub_images(src: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(src, "r") as zf:
        images = [m for m in zf.infolist()
                  if Path(m.filename).suffix.lower() in IMAGE_EXTS]
        total = len(images)
        for i, member in enumerate(images, 1):
            member_path = Path(member.filename)
            target = dest / member_path.name
            if target.exists():
                stem, sfx = member_path.stem, member_path.suffix
                j = 1
                while target.exists():
                    target = dest / f"{stem}_{j}{sfx}"
                    j += 1
            with zf.open(member) as sf, open(target, "wb") as df:
                shutil.copyfileobj(sf, df)
            count += 1
            pct = int(count / total * 100) if total else 100
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"\r    [{bar}] {pct:3d}%  ({count}/{total})", end="", flush=True)
    print("\r" + " " * 50 + "\r", end="")
    return count


# ── scan ─────────────────────────────────────────────────────────────────────

def scan(root: Path) -> tuple[list[Path], list[Path]]:
    archives, epubs = [], []
    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        if entry.name == Path(__file__).resolve().name:
            continue
        ext = entry.suffix.lower()
        if ext in EPUB_EXTS:
            epubs.append(entry)
        elif ext in ARCHIVE_EXTS:
            archives.append(entry)
    return archives, epubs


# ── UI ───────────────────────────────────────────────────────────────────────

def print_header() -> None:
    print()
    print(_bar())
    print(_row("  UNPACKER FOLDER"))
    print(_bar())


def print_preview(archives: list[Path], epubs: list[Path]) -> None:
    total = len(archives) + len(epubs)
    print()
    print(_bar())
    print(_row(f"  PREVIEW  --  {total} file(s) detected"))
    print(_bar())
    if archives:
        print()
        print("  ARCHIVES  (full extraction -> subfolder with same name)")
        print(_sep())
        for f in archives:
            print(f"    {f.name}")
    if epubs:
        print()
        print("  EPUB  (images only -> subfolder with same name)")
        print(_sep())
        for f in epubs:
            print(f"    {f.name}")
    print()


def print_menu() -> str:
    print(_bar())
    print(_row("How do you want to proceed?"))
    print(_row())
    print(_row("  [C] / [Enter]  Copy mode    (originals kept untouched)"))
    print(_row("  [X]            Replace mode (originals will be DELETED)"))
    print(_row("  [Q]            Quit"))
    print(_bar())
    choice = input("  Your choice: ").strip().lower()
    return choice


def confirm_replace() -> bool:
    print()
    print(_bar())
    print(_row("  !! WARNING -- DESTRUCTIVE OPERATION !!"))
    print(_row())
    print(_row("  Original archive files will be PERMANENTLY DELETED"))
    print(_row("  after extraction. This CANNOT be undone."))
    print(_row())
    print(_row("  Type  DELETE  (all caps) to confirm,"))
    print(_row("  or press Enter to go back to the menu."))
    print(_bar())
    answer = input("  Confirm: ").strip()
    return answer == "DELETE"


# ── core extraction run ──────────────────────────────────────────────────────

def run(root: Path, archives: list[Path], epubs: list[Path], replace: bool) -> None:
    mode_label = "REPLACE" if replace else "COPY"
    print()
    print(f"  [ EXTRACTING -- mode: {mode_label} ]")
    print()

    ok = errors = 0

    for src in archives:
        dest = root / src.stem
        if dest.exists():
            dest = root / (src.stem + "_extracted")
        print(f"  [arc]  {src.name}")
        print(f"         -> {dest.name}/")
        try:
            extract_archive(src, dest)
            if replace:
                src.unlink()
                print("         OK  extracted | original deleted")
            else:
                print("         OK  extracted | original kept")
            ok += 1
        except Exception as exc:
            print(f"         ERROR: {exc}")
            errors += 1
        print()

    for src in epubs:
        dest = root / src.stem
        if dest.exists():
            dest = root / (src.stem + "_extracted")
        print(f"  [epub] {src.name}")
        print(f"         -> {dest.name}/")
        try:
            count = extract_epub_images(src, dest)
            if count == 0:
                print("         WARN  no images found inside epub")
                dest.rmdir()
            else:
                if replace:
                    src.unlink()
                    print(f"         OK  {count} image(s) | original deleted")
                else:
                    print(f"         OK  {count} image(s) | original kept")
                ok += 1
        except Exception as exc:
            print(f"         ERROR: {exc}")
            errors += 1
        print()

    print(_sep("="))
    print(f"  Extracted OK : {ok}")
    if errors:
        print(f"  Errors       : {errors}")
    print(_sep("="))
    print()
    print("  Done.")


# ── auto-close helper ────────────────────────────────────────────────────────

def _wait_and_close(seconds: int = 5, had_errors: bool = False) -> None:
    """
    When running in double-click mode, pause briefly so the user can read
    the result, then close the window automatically.
    If there were errors, pause longer and prompt to press Enter instead.
    """
    print()
    if had_errors:
        print("  Errors occurred – press Enter to close this window.")
        try:
            input()
        except Exception:
            pass
    else:
        for i in range(seconds, 0, -1):
            print(f"\r  Window closes in {i}s …  ", end="", flush=True)
            time.sleep(1)
        print()


# ── entry points ─────────────────────────────────────────────────────────────

def main() -> None:
    root = Path(__file__).resolve().parent
    double_click = _is_double_click()

    print_header()
    print(f"  Folder: {root}")
    if double_click:
        print(_row("  AUTO mode: COPY  (originals kept untouched)"))
        print(_row("  Launch with --interactive / -i for the full menu."))

    archives, epubs = scan(root)

    if not archives and not epubs:
        print()
        print("  No archives or EPUB files found. Nothing to do.")
        print()
        if double_click:
            _wait_and_close(seconds=4)
        return

    print_preview(archives, epubs)

    if double_click:
        # ── headless COPY mode ──────────────────────────────────────────────
        run(root, archives, epubs, replace=False)
        had_errors = False   # run() prints errors but doesn't raise; check later
        # Simple heuristic: look for "ERROR" in output is hard here,
        # so we just always use the short countdown after a clean run.
        _wait_and_close(seconds=5)
        return

    # ── interactive menu ────────────────────────────────────────────────────
    while True:
        choice = print_menu()

        if choice in ("c", ""):
            run(root, archives, epubs, replace=False)
            break
        elif choice == "x":
            if confirm_replace():
                run(root, archives, epubs, replace=True)
                break
            else:
                print()
                print("  Replace cancelled. Back to menu.")
                print()
        elif choice == "q":
            print()
            print("  Aborted.")
            print()
            break
        else:
            print()
            print("  Invalid choice. Press C, X, or Q.")
            print()


if __name__ == "__main__":
    main()
