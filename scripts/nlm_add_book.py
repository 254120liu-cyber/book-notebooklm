#!/usr/bin/env python
"""One-command book setup: compress → create notebook → upload → ready.

Usage:
  py -3 scripts/nlm_add_book.py "path/to/book.pdf"
  py -3 scripts/nlm_add_book.py "path/to/book.pdf" --name "My Book Name"
  py -3 scripts/nlm_add_book.py --list              # Show all registered books
  py -3 scripts/nlm_add_book.py --switch "Book"     # Switch active book

The script:
  1. Compresses the PDF if > 50MB (NotebookLM's practical limit)
  2. Creates a new NotebookLM notebook
  3. Uploads the PDF (with resumable upload for large files)
  4. Waits for OCR indexing to complete
  5. Saves the mapping so the skill auto-uses the right notebook
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Config ─────────────────────────────────────────────────

BOOK_MAP_FILE = os.path.expanduser(r"~\.notebooklm\book_map.json")
STATE_DIR = os.path.expanduser(r"~\.notebooklm\profiles\default")
MAX_FILE_SIZE_MB = 200  # NotebookLM's file size limit
COMPRESS_ABOVE_MB = 200   # Start compression if above this


# ── Book Map (persistent notebook ID storage) ──────────────

def load_book_map() -> dict:
    """Returns {book_name: {notebook_id, pdf_path, added_at}}."""
    if os.path.exists(BOOK_MAP_FILE):
        try:
            with open(BOOK_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_book_map(m: dict):
    os.makedirs(os.path.dirname(BOOK_MAP_FILE), exist_ok=True)
    with open(BOOK_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def set_active_book(name: str):
    """Set the active notebook via env var hint and file."""
    state_file = os.path.join(STATE_DIR, "active_book.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"name": name, "set_at": time.time()}, f)
    print(f"Active book set to: {name}")


# ── PDF Compression ────────────────────────────────────────

def compress_pdf(input_path: str, max_mb: int = COMPRESS_ABOVE_MB) -> str | None:
    """Compress PDF via Ghostscript /ebook (150 DPI). ~4 min for 500MB.

    Ghostscript is the only engine. It's fast (30-year C codebase), reliable,
    and /ebook preset gets virtually any book under 200MB with near-lossless quality.
    """
    size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if size_mb <= max_mb:
        print(f"  File size {size_mb:.0f}MB — under {max_mb}MB limit")
        return None

    print(f"  File is {size_mb:.0f}MB — compressing with Ghostscript /ebook...")
    compressed = os.path.join(tempfile.gettempdir(), os.path.basename(input_path))

    if _compress_ghostscript(input_path, compressed, max_mb):
        return compressed

    gs = _find_ghostscript()
    if not gs:
        raise RuntimeError(
            f"PDF is {size_mb:.0f}MB (over {max_mb}MB limit) but Ghostscript is not installed.\n"
            "Install Ghostscript for fast, high-quality compression:\n"
            "  https://ghostscript.com/releases/gsdnld.html\n"
            "Download and install the Windows 64-bit version, then restart your terminal."
        )

    if os.path.exists(compressed):
        os.unlink(compressed)
    raise RuntimeError(
        f"Ghostscript /ebook could not compress {size_mb:.0f}MB under {max_mb}MB.\n"
        f"Try splitting the PDF:\n"
        f"  py -3 scripts/nlm_pdf_splitter.py \"{input_path}\" --ranges \"1-200,201-400\""
    )


def _compress_ghostscript(src: str, dst: str, max_mb: int) -> bool:
    """Ghostscript two-pass: /ebook first (150 DPI, high quality), then /screen
    if still over limit (72 DPI, more aggressive).

    /ebook: 478MB → 171MB (64% reduction), near-lossless, ~4 min
    /screen: 478MB → 59MB (88% reduction), lower quality, ~7 min
    """
    gs = _find_ghostscript()
    if not gs:
        return False

    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"

    presets = [
        ("/ebook", "150 DPI, high quality"),
        ("/screen", "72 DPI, more compression"),
    ]

    for preset, desc in presets:
        print(f"  Ghostscript {preset} ({desc})...")
        try:
            subprocess.run(
                [gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                 f"-dPDFSETTINGS={preset}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                 f"-sOutputFile={dst}", src],
                timeout=600, check=True, env=env,
            )
        except subprocess.TimeoutExpired:
            continue
        except (subprocess.CalledProcessError, FileNotFoundError):
            break

        if _check_and_report(dst, max_mb, f"Ghostscript {preset}"):
            return True
        if os.path.exists(dst):
            os.unlink(dst)

    return False


def _find_ghostscript() -> str | None:
    """Find Ghostscript executable. Checks PATH first, then registry."""
    for name in ["gswin64c", "gswin64", "gswin32c", "gswin32", "gs"]:
        gs = shutil.which(name)
        if gs:
            return gs

    # Registry search (Windows)
    try:
        import winreg
        for root_key, base_path in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GPL Ghostscript"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Artifex\GPL Ghostscript"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\GPL Ghostscript"),
        ]:
            try:
                key = winreg.OpenKey(root_key, base_path)
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        subkey = winreg.OpenKey(key, subkey_name)
                        try:
                            dll_path, _ = winreg.QueryValueEx(subkey, "GS_DLL")
                            gs_dir = os.path.dirname(dll_path)
                            for exe in ["gswin64c.exe", "gswin64.exe", "gswin32c.exe"]:
                                candidate = os.path.join(gs_dir, exe)
                                if os.path.exists(candidate):
                                    return candidate
                        except Exception:
                            pass
                        winreg.CloseKey(subkey)
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                pass
    except ImportError:
        pass

    return None


def _check_and_report(path: str, max_mb: int, engine_name: str) -> bool:
    """Check if compressed file fits. Prints result."""
    new_mb = os.path.getsize(path) / (1024 * 1024)
    pct = (1 - new_mb / max(max_mb, 1)) * 100
    if new_mb <= max_mb:
        print(f"  OK {engine_name}: {new_mb:.0f}MB — fits under {max_mb}MB limit")
        return True
    else:
        print(f"  FAIL {engine_name}: {new_mb:.0f}MB — still over {max_mb}MB limit, trying next engine...")
        try:
            os.unlink(path)
        except OSError:
            pass
        return False


# ── NotebookLM Operations ──────────────────────────────────

async def create_and_upload(
    pdf_path: str, book_name: str, wait_for_ocr: bool = True
) -> tuple[str, str]:
    """Create notebook + upload PDF. Returns (notebook_id, notebook_title)."""
    from notebooklm import NotebookLMClient

    print(f"\n[1/3] Creating notebook \"{book_name}\"...")
    async with await NotebookLMClient.from_storage() as client:
        notebook = await client.notebooks.create(book_name)
        nb_id = notebook.id
        print(f"  Created: {nb_id}")

        print(f"[2/3] Uploading PDF: {os.path.basename(pdf_path)}...")
        source = await client.sources.add_file(nb_id, pdf_path, wait=False)
        print(f"  Uploaded: {source.id}")

        if wait_for_ocr:
            print(f"[3/3] Waiting for OCR indexing...")
            try:
                source = await client.sources.wait_until_ready(
                    nb_id, source.id, timeout=300
                )
                print(f"  Ready: {source.title}")
            except Exception as e:
                print(f"  [WARN] OCR wait timed out: {e}")
                print(f"  Notebook is still usable; indexing continues in background.")

    return nb_id.split("-")[0], notebook.title  # Return short ID + full title


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add a book to NotebookLM — one command from PDF to ready"
    )
    parser.add_argument("pdf", nargs="?", help="Path to the book PDF file")
    parser.add_argument("--name", "-n", default=None,
                       help="Book name (defaults to PDF filename)")
    parser.add_argument("--no-compress", action="store_true",
                       help="Skip PDF compression even if file is large")
    parser.add_argument("--no-wait", action="store_true",
                       help="Don't wait for OCR indexing to complete")
    parser.add_argument("--list", action="store_true",
                       help="List all registered books")
    parser.add_argument("--switch", "-s", default=None, metavar="NAME",
                       help="Switch active book by name")
    args = parser.parse_args()

    # ── List books ──
    if args.list:
        books = load_book_map()
        if not books:
            print("No books registered yet.")
            print("Add one: py -3 scripts/nlm_add_book.py path/to/book.pdf")
        else:
            print("Registered books:")
            for name, info in sorted(books.items()):
                nb_id = info.get("notebook_id", "?")
                added = info.get("added_at", "?")
                print(f"  {name}")
                print(f"    Notebook: {nb_id}  |  Added: {added}")
        return 0

    # ── Switch book ──
    if args.switch:
        books = load_book_map()
        name = args.switch
        # Fuzzy match
        match = None
        for k in books:
            if name.lower() in k.lower():
                match = k
                break
        if not match:
            print(f"Book \"{name}\" not found. Registered books:")
            for k in sorted(books):
                print(f"  - {k}")
            return 1
        set_active_book(match)
        print(f"Notebook ID: {books[match]['notebook_id']}")
        print(f'Run: set NOTEBOOKLM_DEFAULT_NB={books[match]["notebook_id"]}')
        print(f'Or the skill will auto-detect from ~/.notebooklm/book_map.json')
        return 0

    # ── Add book ──
    if not args.pdf:
        parser.print_help()
        return 0

    pdf_path = os.path.abspath(args.pdf)
    if not os.path.exists(pdf_path):
        print(f"ERROR: PDF not found: {pdf_path}")
        return 1

    book_name = args.name or os.path.splitext(os.path.basename(pdf_path))[0]

    print("=" * 60)
    print(f"  Adding book: {book_name}")
    print(f"  PDF: {pdf_path}")
    print("=" * 60)

    # Step 0: Compress if needed
    upload_path = pdf_path
    compressed = None
    if not args.no_compress:
        compressed = compress_pdf(pdf_path)
        if compressed:
            upload_path = compressed

    # Step 1-3: Create notebook + upload + wait
    try:
        nb_id, title = asyncio.run(create_and_upload(
            upload_path, book_name, wait_for_ocr=not args.no_wait
        ))
    except Exception as e:
        msg = str(e)
        print(f"\nERROR: {msg}")
        print("\nTroubleshooting:")
        if "connection" in msg.lower() or "all connection attempts" in msg.lower():
            print("  → VPN may be disconnected. NotebookLM requires VPN access.")
            print("  → Turn on your VPN and try again.")
        print("  1. Check auth: py -3 scripts/nlm_query.py --status")
        print("  2. Re-login if needed: py -3 scripts/nlm_query.py --relogin")
        print("  3. Ensure the PDF is valid and readable")
        return 1
    finally:
        # Clean up temp compressed file
        if compressed and os.path.exists(compressed):
            try:
                os.unlink(compressed)
            except OSError:
                pass

    # Step 4: Save mapping
    books = load_book_map()
    books[book_name] = {
        "notebook_id": nb_id,
        "title": title,
        "pdf_path": pdf_path,
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    save_book_map(books)
    set_active_book(book_name)

    print(f"\n{'=' * 60}")
    print(f"  [OK] Book \"{book_name}\" is ready!")
    print(f"  Notebook ID: {nb_id}")
    print(f"  Set as active book")
    print(f"")
    print(f"  You can now ask Claude any question about this book.")
    print(f"  Example: what is chapter 3 about?")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
