#!/usr/bin/env python
"""One-command book setup v2: extract TOC → split by chapters → upload all → cleanup old.

Usage:
  py -3 scripts/nlm_add_book.py "path/to/book.pdf"
  py -3 scripts/nlm_add_book.py "path/to/book.pdf" --name "My Book Name"
  py -3 scripts/nlm_add_book.py --list
  py -3 scripts/nlm_add_book.py --switch "Book"

Auto-split: PDF ≤100 pages → direct upload. PDF >100 pages → extract TOC →
split by chapter boundaries (≤100 pages/chunk) → upload all chunks as sources
to a single notebook.

Old notebooks for the same book are automatically cleaned up after successful upload.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Config ─────────────────────────────────────────────────

BOOK_MAP_FILE = os.path.expanduser(r"~\.notebooklm\book_map.json")
ROUTES_FILE = os.path.expanduser(r"~\.notebooklm\chapter_routes.json")
STATE_DIR = os.path.expanduser(r"~\.notebooklm\profiles\default")
SPLIT_DIR = os.path.expanduser(r"~\.notebooklm\split")
MAX_FILE_SIZE_MB = 200
COMPRESS_ABOVE_MB = 200
MAX_PAGES_PER_CHUNK = 100


def _clean_book_name(raw: str) -> str:
    """Clean up a PDF filename into a readable book name.

    Removes: Z-Library suffixes, edition info in brackets, author lists,
    and trailing junk like '(Z-Library)' or '【无封面】'.
    """
    name = raw
    # Remove parenthesized author lists and source markers
    name = re.sub(r'\s*[（(][^)）]*?(Z-Library|佚名|著|译|编|出版社)[^)）]*?[)）]', '', name)
    # Remove bracketed annotations like 【无封面、版权页】
    name = re.sub(r'\s*【[^】]*】', '', name)
    # Remove trailing parenthesized content
    name = re.sub(r'\s*\([^)]*\)$', '', name)
    name = re.sub(r'\s*（[^）]*）$', '', name)
    # Remove stray " - " suffixes
    name = re.sub(r'\s*-\s*$', '', name)
    # Normalize whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name if len(name) >= 2 else raw


# ── Book Map ──────────────────────────────────────────────

def load_book_map() -> dict:
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
    state_file = os.path.join(STATE_DIR, "active_book.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"name": name, "set_at": time.time()}, f)


async def _query_full_toc_from_nb(client, nb_id: str, book_name: str) -> dict[str, dict]:
    """Query NotebookLM for the complete book TOC, parse into sub-section tree.

    Returns: {ch_str: {section_id: {title, start_page}, ...}, ...}
    Only adds sub_sections that the chapter_routes don't already have.
    """
    prompt = (
        f"请列出《{book_name}》的完整目录结构，包括每一章下的所有小节编号和标题。"
        f"请严格按照\"X.Y 标题\"或\"X.Y.Z 标题\"的格式输出，每个小节一行。"
        f"按章节顺序排列，不需要页码，只需要编号和标题。"
    )
    try:
        answer = await client.ask(nb_id, prompt)
        if not answer:
            return {}
    except Exception:
        return {}

    # Parse: "7.1 标题" or "7.1.1 标题" pattern
    tree: dict[str, dict] = {}
    current_ch = None
    for line in answer.split("\n"):
        line = line.strip()
        # Skip markdown markers and non-content lines
        line = re.sub(r'^\*+\s*|^[\-\•]\s*|^\d+[\.\)]\s*', '', line)
        # Match section IDs: "7.1 标题", "7.1.1 标题", "7.1.1.1 标题" (任意深度)
        m = re.match(r'(\d+)\.(\d+(?:\.\d+)*)\s+(.+)', line)
        if m:
            ch_num = m.group(1)
            sec_id = f"{ch_num}.{m.group(2)}"
            sec_title = m.group(3).strip()[:80]
            if ch_num not in tree:
                tree[ch_num] = {}
            tree[ch_num][sec_id] = {"title": sec_title, "start_page": 0}
            continue
        # Match chapter-only: "第7章 标题"
        m = re.match(r'第\s*(\d+)\s*章\s+(.+)', line)
        if m:
            current_ch = m.group(1)
            continue

    # Filter: only keep sections with at least 2 sub-sections per chapter
    result = {}
    for ch_str, subs in tree.items():
        if len(subs) >= 2:
            result[ch_str] = subs

    return result


async def _ensure_full_toc(book_name: str, nb_id: str):
    """After OCR, query NotebookLM for full TOC to get lowest-level sections.

    Only queries if chapter_routes.json doesn't already have sub_sections for this book.
    Updates chapter_routes.json in place.
    """
    routes = load_routes()
    books = routes.get("books", {})
    book_data = books.get(book_name, {})
    boundaries = book_data.get("chapter_boundaries", {})

    # Check if any chapter already has sub_sections (from PDF bookmarks)
    has_subs = any(b.get("sub_sections") for b in boundaries.values())
    if has_subs:
        return  # Already have sub-sections from PDF bookmarks

    # Only query if we have >3 chapters to avoid wasting queries on tiny books
    if len(boundaries) < 3:
        return

    print(f"  Auto-detecting sub-section structure via NotebookLM...")
    try:
        from notebooklm import NotebookLMClient
        async with await NotebookLMClient.from_storage() as client:
            toc_tree = await _query_full_toc_from_nb(client, nb_id, book_name)
    except Exception as e:
        print(f"  [WARN] TOC query failed: {e}")
        return

    if not toc_tree:
        print(f"  [WARN] Could not parse TOC from NotebookLM response")
        return

    # Merge into chapter_routes
    updated = 0
    for ch_str, subs in toc_tree.items():
        if ch_str in boundaries and len(subs) >= 2:
            boundaries[ch_str]["sub_sections"] = subs
            updated += 1

    if updated:
        books[book_name] = book_data
        routes["books"] = books
        os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
        with open(ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(routes, f, ensure_ascii=False, indent=2)
        total_subs = sum(len(s) for s in toc_tree.values())
        # Compute max depth
        max_d = 1
        for subs in toc_tree.values():
            for sec_id in subs:
                d = sec_id.count(".") + 1
                if d > max_d:
                    max_d = d
        book_data["max_depth"] = max_d
        print(f"  Sub-section tree saved: {updated} chapters, {total_subs} sections (max depth={max_d})")
    else:
        print(f"  No sub-sections found in TOC response")


# ── Chapter Routes ─────────────────────────────────────────

def load_routes(book_name: str = None) -> dict:
    """Load chapter routes. If book_name, return that book's data only.

    Auto-migrates old flat format to new namespaced format:
      OLD: {"keywords": {...}, "chapter_boundaries": {...}}
      NEW: {"books": {"BookName": {"keywords": {...}, "chapter_boundaries": {...}}}}
    """
    data = {"books": {}}
    if os.path.exists(ROUTES_FILE):
        try:
            with open(ROUTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Migration: old flat format → namespaced
    if "books" not in data:
        old = data
        data = {"books": {}}
        if old.get("keywords") or old.get("chapter_boundaries"):
            # Try to guess the book from book_map or active_book
            legacy_name = _guess_book_for_routes()
            data["books"][legacy_name] = old

    if book_name:
        return data["books"].get(book_name, {"keywords": {}, "chapter_boundaries": {}})
    return data


def _guess_book_for_routes() -> str:
    """Guess which book legacy routes data belongs to."""
    # Check active book first
    active_file = os.path.join(STATE_DIR, "active_book.json")
    if os.path.exists(active_file):
        try:
            with open(active_file, "r", encoding="utf-8") as f:
                name = json.load(f).get("name")
                if name:
                    return name
        except Exception:
            pass
    # Check book_map
    books = load_book_map()
    if len(books) == 1:
        return list(books.keys())[0]
    return "_legacy"


def save_routes(book_name: str, book_routes: dict):
    """Save chapter routes for a specific book (namespaced)."""
    data = load_routes()  # returns full {"books": {...}} when no arg
    data["books"][book_name] = book_routes
    os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _store_auto_stages(book_name: str, parts: list[dict], chapters: list[dict]):
    """Store auto-detected stages from PDF outline hierarchy.

    Saved to chapter_routes.json under the book's namespace as 'auto_stages'.
    nlm_progress.py reads this back for stage-grouped display (Fix stage auto-generation).
    """
    if not parts:
        return
    stages = []
    for p in parts:
        ch_nums = [c for c in p["chapter_indices"] if 1 <= c <= len(chapters)]
        if len(ch_nums) >= 2:
            stages.append({"name": p["name"], "chapters": ch_nums, "desc": ""})
    if not stages:
        return
    data = load_routes()
    if "books" not in data:
        data = {"books": {}}
    if book_name not in data["books"]:
        data["books"][book_name] = {"keywords": {}, "chapter_boundaries": {}}
    data["books"][book_name]["auto_stages"] = stages
    os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── PDF TOC Extraction ─────────────────────────────────────

def extract_toc(pdf_path: str) -> list[dict]:
    """Extract chapter boundaries from PDF. Four-tier strategy:

    Tier 1: PDF outline/bookmarks (handles nested Parts → auto-stages)
    Tier 2: Text-based regex scan (for PDFs with text layer)
    Tier 3: Chapter presets (for known books)
    Tier 4: Even-page fallback (last resort)

    Returns: [{chapter: int, title: str, start_page: int, end_page: int}, ...]
    """
    total_pages = 100
    book_name = os.path.splitext(os.path.basename(pdf_path))[0]
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(pdf_path)
        total_pages = len(reader.pages)
    except Exception:
        pass

    # ── Tier 1: PDF bookmarks ──
    try:
        chapters, parts = _extract_toc_from_outline(pdf_path, total_pages)
        if chapters and len(chapters) >= 3:
            parts_info = f", {len(parts)} parts" if parts else ""
            print(f"  TOC via PDF bookmarks: {len(chapters)} chapters{parts_info}")
            # Store parts for stage generation
            if parts:
                _store_auto_stages(book_name, parts, chapters)
            return chapters
    except Exception as e:
        pass

    # ── Tier 2: Text regex scan ──
    try:
        chapters = _extract_toc_from_text(pdf_path, total_pages)
        if chapters and len(chapters) >= 3:
            print(f"  TOC via text extraction: {len(chapters)} chapters")
            return chapters
    except Exception as e:
        pass

    # ── Tier 3: Known book preset ──
    for preset_key, preset_chapters in CHAPTER_PRESETS.items():
        if preset_key in book_name:
            chapters = _build_preset_chapters(preset_chapters, total_pages)
            if chapters:
                print(f"  TOC via preset ({preset_key}): {len(chapters)} chapters")
                return chapters

    # ── Tier 4: Even-page fallback ──
    print(f"  TOC: all tiers failed, even-page fallback")
    return _fallback_even_split(total_pages)


def _extract_toc_from_outline(pdf_path: str, total_pages: int) -> tuple[list[dict], list[dict] | None]:
    """Extract TOC from PDF bookmarks/outline (Tier 1).

    Handles:
      - Multi-level outlines: parent entries become Parts (for stage generation),
        child entries become Chapters.
      - Both leaf and non-leaf chapter entries.
      - Fallback page resolution: if IndirectObject fails, try text search.
      - Page number extraction from title (e.g. "... 121" → page 121).

    Returns: (chapters, parts_or_none)
      chapters: [{chapter: int, title: str, start_page: int}, ...]
      parts: [{name: str, chapters: [int, ...]}, ...] or None
    """
    from PyPDF2 import PdfReader

    reader = PdfReader(pdf_path)
    outline = reader.outline
    if not outline:
        return [], None

    # First pass: build a page-number lookup by searching PDF text
    # for bookmark titles (used as fallback when indirect refs fail)
    _page_search_cache = {}  # title_prefix → page_num

    def _search_title_in_pdf(title: str) -> int | None:
        """Search for a title in the PDF text to find its page."""
        clean = title.strip()[:30]
        if clean in _page_search_cache:
            return _page_search_cache[clean]
        # Search first page of each 20-page block (efficient scan)
        for pg in range(0, min(total_pages, 200), 15):
            try:
                text = reader.pages[pg].extract_text()
                if text and clean[:6] in text:
                    _page_search_cache[clean] = pg + 1
                    return pg + 1
            except Exception:
                continue
        _page_search_cache[clean] = None
        return None

    chapters = []
    parts = []
    chapter_num = 0
    # Track sub-section accumulation: when a chapter entry has children, the NEXT
    # list element in the outline is a sub-list containing its sub-sections.
    # Due to PyPDF2's outline structure, children are siblings, not /Kids.
    _pending_chapter_subs = None  # dict to fill, or None

    def _is_chapter_like(title: str) -> bool:
        """Check if a title looks like a chapter heading (not a sub-section)."""
        if re.match(r'第\s*\d+\s*章\b', title):
            return True
        if re.match(r'Chapter\s+\d+\b', title, re.IGNORECASE):
            return True
        return False

    def _flatten(items, depth=0, parent_chapters=None):
        nonlocal chapter_num, _pending_chapter_subs
        for item in items:
            if isinstance(item, list):
                # If we're expecting sub-sections for the previous chapter,
                # this sub-list IS the sub-sections. Capture them.
                if _pending_chapter_subs is not None:
                    _capture_subsections(item, _pending_chapter_subs)
                    _pending_chapter_subs = None
                else:
                    _flatten(item, depth + 1, parent_chapters)
                continue
            title = str(item.get("/Title", ""))
            if not title or len(title) < 2 or _is_metadata(title):
                continue

            page_obj = item.get("/Page")
            page_num = _resolve_page_number(reader, page_obj)
            if page_num is None:
                page_num = _search_title_in_pdf(title)
            if page_num is None:
                m = re.search(r'(\d{1,4})\s*$', title)
                if m:
                    p = int(m.group(1))
                    if 1 <= p <= total_pages + 50:
                        page_num = min(p, total_pages)
            if page_num is None or page_num < 1:
                continue

            has_kids = bool(item.get("/Count"))
            is_chapter_like = _is_chapter_like(title)
            is_part = has_kids and depth == 0 and not is_chapter_like

            if is_part:
                part_name = title[:60]
                part_chapters = []
                parts.append({"name": part_name, "chapter_indices": part_chapters})
                _pending_chapter_subs = None
                _flatten([], depth + 1, parent_chapters=part_chapters)
            elif is_chapter_like:
                chapter_num += 1
                ch_entry = {
                    "chapter": chapter_num,
                    "title": title[:80],
                    "start_page": page_num,
                    "sub_sections": {},
                }
                if has_kids:
                    # The next list element in outline is the sub-section list
                    _pending_chapter_subs = ch_entry["sub_sections"]
                chapters.append(ch_entry)
                if parent_chapters is not None:
                    parent_chapters.append(chapter_num)

    def _capture_subsections(kids_list, ss_dict: dict):
        """Capture sub-sections from a list of outline items."""
        sub_idx = 0
        for kid in kids_list:
            if isinstance(kid, list):
                _capture_subsections(kid, ss_dict)
                continue
            k_title = str(kid.get("/Title", ""))
            if not k_title or len(k_title) < 2 or _is_metadata(k_title):
                continue
            k_page_obj = kid.get("/Page")
            k_page = _resolve_page_number(reader, k_page_obj)
            if k_page is None:
                k_page = _search_title_in_pdf(k_title)
            if k_page is None:
                continue
            sub_idx += 1
            # Extract section number from title (e.g., "1.1 标题" or "1.2.3.4 标题")
            sec_m = re.match(r'(\d+(?:\.\d+)+)', k_title)
            if sec_m:
                ss_id = sec_m.group(1)
            else:
                ss_id = f"{sub_idx}"  # fallback: sequential
            ss_dict[ss_id] = {
                "title": k_title[:80],
                "start_page": k_page,
            }

    _flatten(outline)

    if len(chapters) < 3:
        return _finalize_chapters(chapters, total_pages), None

    chapters = _finalize_chapters(chapters, total_pages)

    # Convert part chapter_indices to actual chapter numbers
    valid_parts = []
    for p in parts:
        actual_chs = [c for c in p["chapter_indices"] if 1 <= c <= len(chapters)]
        if len(actual_chs) >= 2:
            valid_parts.append({"name": p["name"], "chapters": actual_chs})

    return chapters, (valid_parts if valid_parts else None)


def _resolve_page_number(reader, page_obj) -> int | None:
    """Resolve a PDF page object to a 1-based page number. Tries multiple methods."""
    if page_obj is None:
        return None
    try:
        # Import IndirectObject: location depends on PyPDF2 version
        try:
            from PyPDF2 import IndirectObject
        except ImportError:
            from PyPDF2.generic import IndirectObject
        try:
            from PyPDF2.generic import NumberObject
        except ImportError:
            NumberObject = int  # fallback

        # Method 1: IndirectObject via get_page_number
        if isinstance(page_obj, IndirectObject):
            pn = reader.get_page_number(page_obj)
            return pn + 1 if pn is not None else None
        # Method 2: has get_object attribute
        if hasattr(page_obj, "get_object"):
            obj = page_obj.get_object()
            pn = reader.get_page_number(obj)
            return pn + 1 if pn is not None else None
        # Method 3: direct integer
        if isinstance(page_obj, (int, NumberObject)):
            pn = int(page_obj)
            return pn + 1 if 0 <= pn < 10000 else pn
        # Method 4: try get_page_number directly
        pn = reader.get_page_number(page_obj)
        return pn + 1 if pn is not None else None
    except Exception:
        pass
    return None


def _is_metadata(title: str) -> bool:
    """Filter out metadata entries from bookmarks (covers, copyright, etc.)."""
    meta_patterns = [
        "封面", "扉页", "版权", "前言", "序言", "目录", "致谢", "样章",
        ".pdf", "RJTS", "FLb", "文前", "正文",
        "preface", "cover", "toc", "acknowledgment",
    ]
    tl = title.lower()
    return any(p.lower() in tl for p in meta_patterns)


def _extract_toc_from_text(pdf_path: str, total_pages: int) -> list[dict]:
    """Extract TOC by scanning text of first pages (Tier 2)."""
    from PyPDF2 import PdfReader
    reader = PdfReader(pdf_path)
    chapters = []
    seen = set()

    for page_num in range(min(35, total_pages)):
        try:
            text = reader.pages[page_num].extract_text()
            if not text:
                continue
        except Exception:
            continue

        # Match: "第X章" patterns
        # The first 35 pages may contain TOC. The page_hint from TOC
        # entries is the actual chapter page — validate it against total_pages.
        for m in re.finditer(r'第\s*(\d{1,2})\s*章\s*(.+?)(?:\s*\.{3,}|\s*\d{2,4}\s*$|\s*$)', text):
            ch_num = int(m.group(1))
            ch_title = m.group(2).strip().rstrip('.')[:80]
            if ch_num not in seen and 1 <= ch_num <= 50:
                seen.add(ch_num)
                page_hint = _extract_page_after_match(text, m.end())
                # Validate: page_hint from TOC is usually the real chapter page
                if page_hint and 1 <= page_hint <= total_pages:
                    start = page_hint
                else:
                    start = page_num + 1
                chapters.append({
                    "chapter": ch_num,
                    "title": f"第{ch_num}章 {ch_title}",
                    "start_page": start,
                })
        # English "Chapter N" patterns
        for m in re.finditer(r'Chapter\s+(\d{1,2})\s*[:\-]?\s*(.+?)(?:\s*\.{3,}|\s*\d{2,4}\s*$|\s*$)', text, re.IGNORECASE):
            ch_num = int(m.group(1))
            ch_title = m.group(2).strip()[:80]
            if ch_num not in seen and 1 <= ch_num <= 30:
                seen.add(ch_num)
                chapters.append({
                    "chapter": ch_num,
                    "title": f"Chapter {ch_num}: {ch_title}",
                    "start_page": page_num + 1,
                })

    if len(chapters) < 3:
        return []
    chapters.sort(key=lambda c: c["chapter"])
    return _finalize_chapters(chapters, total_pages)


def _build_preset_chapters(preset: dict, total_pages: int) -> list[dict]:
    """Build chapter list from known preset (Tier 3)."""
    chapters = []
    for ch_num in sorted(preset.keys()):
        entry = preset[ch_num]
        start, end, title = entry[0], entry[1], entry[2]
        keywords_str = entry[3] if len(entry) > 3 else ""
        chapters.append({
            "chapter": ch_num,
            "title": f"第{ch_num}章 {title}",
            "start_page": start,
            "end_page": min(end, total_pages),
            "keywords": keywords_str,
        })
    return chapters


def _fallback_even_split(total_pages: int) -> list[dict]:
    """Even-page split when all extraction methods fail (Tier 4)."""
    chapters = []
    for start in range(1, total_pages + 1, MAX_PAGES_PER_CHUNK):
        end = min(start + MAX_PAGES_PER_CHUNK - 1, total_pages)
        chapters.append({
            "chapter": len(chapters) + 1,
            "title": f"第{len(chapters) + 1}部分 (第{start}-{end}页)",
            "start_page": start,
            "end_page": end,
        })
    return chapters


def _finalize_chapters(chapters: list, total_pages: int) -> list:
    """Sort chapters and fill in end_page boundaries."""
    if not chapters:
        return []
    chapters.sort(key=lambda c: c["chapter"])
    for i in range(len(chapters)):
        if "end_page" not in chapters[i]:
            if i + 1 < len(chapters):
                chapters[i]["end_page"] = max(chapters[i + 1]["start_page"] - 1, chapters[i]["start_page"])
            else:
                chapters[i]["end_page"] = total_pages
        # Clean up temporary fields
        for field in ["page_hint", "keywords_str"]:
            chapters[i].pop(field, None)
    # Remove duplicate chapter numbers (keep first)
    seen = set()
    deduped = []
    for c in chapters:
        if c["chapter"] not in seen:
            seen.add(c["chapter"])
            deduped.append(c)
    return deduped


def _extract_page_after_match(text: str, pos: int) -> int | None:
    """Try to find a page number near the match position."""
    # Look for patterns like "...  290" or "  290"
    tail = text[pos:pos + 200]
    m = re.search(r'\.{2,}\s*(\d{1,4})', tail)
    if m:
        return int(m.group(1))
    m = re.search(r'\b(\d{2,4})\b', tail)
    if m:
        val = int(m.group(1))
        if 10 < val < 1000:
            return val
    return None


def _refine_page_numbers(chapters: list, reader, total_pages: int) -> list:
    """Use PDF structure hints to refine chapter start pages."""
    for c in chapters:
        hint = c.get("page_hint")
        if hint and 1 <= hint <= total_pages:
            c["start_page"] = hint
        elif c["chapter"] == 1:
            c["start_page"] = 1
        else:
            # Estimate based on previous chapter
            idx = chapters.index(c)
            if idx > 0 and chapters[idx - 1].get("start_page"):
                c["start_page"] = chapters[idx - 1]["start_page"] + 50  # rough guess

    # Fill in end pages: next chapter's start - 1
    for i in range(len(chapters)):
        if i + 1 < len(chapters):
            chapters[i]["end_page"] = max(chapters[i + 1]["start_page"] - 1, chapters[i]["start_page"])
        else:
            chapters[i]["end_page"] = total_pages

    # Clean up
    for c in chapters:
        if "page_hint" in c:
            del c["page_hint"]

    return chapters


# Known book chapter presets (verified via NotebookLM page citations)
CHAPTER_PRESETS = {
    "加密与解密": {
        1:  (1, 59,   "基础知识",
             "虚拟内存|WOW64|Win32 API|消息机制|字节序|ASCII|Unicode|小端|大端|保护模式"),
        2:  (60, 119, "动态分析技术",
             "断点|x32dbg|x64dbg|OllyDbg|单步|硬件断点|内存断点|条件断点|步过|步进|寄存器|栈回溯"),
        3:  (120, 170, "静态分析技术",
             "IDA|反汇编|F5|交叉引用|字符串搜索|十六进制|静态分析|函数识别|流程图"),
        4:  (171, 196, "逆向分析技术",
             "调用约定|cdecl|stdcall|fastcall|thiscall|if-else|switch|循环|虚函数|vtable|数组|结构体"),
        5:  (197, 222, "演示版保护技术",
             "序列号|KeyFile|Nag|警告窗口|注册表|网络验证|光盘检测|反跟踪|反调试"),
        6:  (223, 276, "加密算法",
             "MD5|SHA|AES|DES|RSA|RC4|SM4|Base64|S-Box|加密|解密|对称|非对称|摘要"),
        7:  (277, 340, "Windows内核基础",
             "内核|Ring0|Ring3|KPCR|TEB|PEB|SSDT|EPROCESS|驱动|DriverEntry|sysenter|syscall|"
             "DeviceIoControl|内核对象|IRP|HAL|对象管理器|内存管理器|I/O管理器"),
        8:  (341, 360, "SEH异常处理",
             "SEH|VEH|异常处理|异常分发|安全SEH|异常链|try-except|栈展开"),
        9:  (361, 380, "Win32调试API",
             "DebugActiveProcess|WaitForDebugEvent|DEBUG_EVENT|ContinueDebugEvent|调试API"),
        10: (381, 403, "VT虚拟化技术",
             "VT|VMX|VMCS|EPT|VPID|Hypervisor|VMM|虚拟化"),
        11: (404, 460, "PE文件格式",
             "PE|IAT|导入表|导出表|重定位|TLS|延迟导入|资源|绑定导入|输入表|"
             "IMAGE_DOS_HEADER|IMAGE_NT_HEADERS|IMAGE_SECTION_HEADER|.text|.data|.rsrc"),
        12: (461, 495, "DLL注入技术",
             "注入|远程线程|APC注入|DLL注入|SetWindowsHookEx|CreateRemoteThread"),
        13: (496, 530, "API Hook技术",
             "Hook|Inline|IAT Hook|VirtualProtect|跳转|detours|SSDT Hook|IRP Hook"),
        14: (531, 599, "二进制漏洞分析",
             "shellcode|缓冲区溢出|栈溢出|UAF|类型混淆|ROP|DEP|ASLR|GS|CFG"),
        15: (600, 660, "软件保护技术",
             "保护|VMProtect|虚拟化保护|混淆|花指令|反调试|反dump|完整性校验"),
        16: (661, 720, "脱壳技术",
             "脱壳|OEP|UPX|ASPack|ASProtect|壳|IAT重建|ESP定律|单步跟踪|内存镜像"),
        17: (721, 750, "反跟踪技术",
             "反跟踪|TLS回调|时间检测|RDTSC|int 2d|int 3|硬件断点检测"),
        18: (751, 780, "外壳编写基础",
             "外壳|加壳|压缩引擎|导入表加密|重定位处理|AntiDump"),
        19: (781, 810, "虚拟化保护技术",
             "虚拟机保护|Handler|VMContext|VMP堆栈|x86指令模拟"),
        20: (811, 850, "重构与适配",
             "重构|x64适配|移植|WOW64|天堂之门"),
        21: (851, 880, "加密与解密实战",
             "CrackMe|KeyGen|注册机|逆向实战|练习"),
        22: (881, 900, "电子取证技术",
             "取证|电子证据|日志分析|文件恢复|内存取证"),
        23: (901, 920, "移动平台安全",
             "Android|iOS|Mach-O|ELF|DEX|Smali|越狱"),
        24: (921, 940, "其他"),
        25: (941, 948, "附录",
             "参考文献|附录|索引"),
    }
}


def _detect_book_depth(chapters: list[dict]) -> int:
    """Detect the maximum section depth across all chapters.

    Depth 1 = only chapters (no sub-sections)
    Depth 2 = X.Y level (e.g. 1.1, 2.3)
    Depth 3 = X.Y.Z level (e.g. 1.1.1, 2.3.4)
    Depth 4+ = deeper nesting

    Returns the max depth found, min 1.
    """
    max_depth = 1
    for ch in chapters:
        subs = ch.get("sub_sections", {})
        for sec_id in subs:
            depth = sec_id.count(".") + 1
            if depth > max_depth:
                max_depth = depth
    return max_depth


def _detect_depth_from_routes(routes_data: dict) -> int:
    """Detect max depth from routes_data structure."""
    max_depth = 1
    boundaries = routes_data.get("chapter_boundaries", {})
    for ch_str, b in boundaries.items():
        subs = b.get("sub_sections", {})
        for sec_id in subs:
            depth = sec_id.count(".") + 1
            if depth > max_depth:
                max_depth = depth
    return max_depth


def _auto_generate_stages_if_needed(book_name: str, chapters: list[dict]):
    """If no auto_stages exist yet, generate them by evenly grouping chapters.

    Only for books with 9+ chapters — groups them into ~5 stages of roughly equal size.
    """
    if not chapters or len(chapters) < 9:
        return
    # Check if stages already exist from PDF outline
    routes = load_routes()
    books = routes.get("books", {})
    book_data = books.get(book_name, {})
    if book_data.get("auto_stages"):
        return  # Already have stages from PDF outline

    total = len(chapters)
    num_stages = min(5, max(2, total // 3))  # Aim for ~3 chapters per stage
    if num_stages < 2:
        return

    stage_size = (total + num_stages - 1) // num_stages  # Ceiling division
    stages = []
    for i in range(num_stages):
        start_idx = i * stage_size
        end_idx = min(start_idx + stage_size, total)
        stage_chs = list(range(start_idx + 1, end_idx + 1))
        if len(stage_chs) >= 2:
            stages.append({
                "name": f"第{start_idx + 1}-{end_idx}章",
                "chapters": stage_chs,
                "desc": "",
            })

    if not stages:
        return

    if book_name not in books:
        books[book_name] = {"keywords": {}, "chapter_boundaries": {}}
    books[book_name]["auto_stages"] = stages
    routes["books"] = books
    os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False, indent=2)

    print(f"  Auto-generated {len(stages)} stages ({num_stages} groups of ~{stage_size} chapters)")


# ── PDF Splitting ──────────────────────────────────────────

def compute_chunks(chapters: list, max_pages: int = MAX_PAGES_PER_CHUNK) -> list[dict]:
    """Group chapters into chunks ≤ max_pages each.

    Strategy:
    - Single chapter > max_pages → split internally (by sub-section boundaries if possible)
    - Accumulate chapters until adding the next one exceeds max_pages
    - Each chunk gets a name like "第1章_基础知识" or "第5-6章_加密算法"
    """
    chunks = []
    current_start = None
    current_end = None
    current_name_parts = []

    for ch in chapters:
        ch_pages = ch["end_page"] - ch["start_page"] + 1

        # Single chapter too large → split internally
        if ch_pages > max_pages:
            # Flush current accumulation
            if current_start is not None:
                chunks.append(_make_chunk(current_name_parts, current_start, current_end))
                current_start = None

            # Split this chapter into sub-chunks
            for sub_start in range(ch["start_page"], ch["end_page"] + 1, max_pages):
                sub_end = min(sub_start + max_pages - 1, ch["end_page"])
                sub_idx = len([c for c in chunks if c["name"].startswith(ch["title"])]) + 1
                chunks.append({
                    "name": f"{ch['title']}({sub_idx})",
                    "start": sub_start,
                    "end": sub_end,
                })
            current_name_parts = []
            continue

        # Start new accumulation
        if current_start is None:
            current_start = ch["start_page"]
            current_end = ch["end_page"]
            current_name_parts = [ch["title"]]
        else:
            # Would adding this chapter exceed max_pages?
            if (ch["end_page"] - current_start + 1) > max_pages:
                # Flush current and start new
                chunks.append(_make_chunk(current_name_parts, current_start, current_end))
                current_start = ch["start_page"]
                current_end = ch["end_page"]
                current_name_parts = [ch["title"]]
            else:
                current_end = ch["end_page"]
                current_name_parts.append(ch["title"])

    # Flush remaining
    if current_start is not None:
        chunks.append(_make_chunk(current_name_parts, current_start, current_end))

    # Build chapter routes dict (saved later only after OCR success — Fix #6)
    routes_data = {"keywords": {}, "chapter_boundaries": {}, "max_depth": _detect_book_depth(chapters)}
    for ch in chapters:
        entry = {
            "start": ch["start_page"],
            "end": ch["end_page"],
            "title": ch["title"],
        }
        # Store sub-section structure from PDF bookmarks
        if ch.get("sub_sections"):
            entry["sub_sections"] = ch["sub_sections"]
        routes_data["chapter_boundaries"][str(ch["chapter"])] = entry
    for ch in chapters:
        title = ch["title"]
        if ch.get("keywords"):
            terms = ch["keywords"].split("|")
        else:
            terms = _extract_terms(title)
        for term in terms:
            term = term.strip()
            if term and len(term) >= 2:
                if term not in routes_data["keywords"]:
                    routes_data["keywords"][term] = {
                        "chapter": ch["chapter"],
                        "pages": f"{ch['start_page']}-{ch['end_page']}",
                        "source": "preset" if ch.get("keywords") else "toc_seed",
                        "hits": 0,
                        "last_hit": "",
                    }

    return chunks, routes_data


def _make_chunk(name_parts: list, start: int, end: int) -> dict:
    """Create a chunk descriptor with a human-readable name."""
    if len(name_parts) == 1:
        name = name_parts[0]
    else:
        # "第7章 ..." + "第8章 ..." → "第7-8章_..."
        first_num = re.search(r'第(\d+)章', name_parts[0])
        last_num = re.search(r'第(\d+)章', name_parts[-1])
        if first_num and last_num:
            name = f"第{first_num.group(1)}-{last_num.group(1)}章"
        else:
            name = name_parts[0]
    return {"name": name, "start": start, "end": end}


def _extract_terms(title: str) -> list[str]:
    """Extract searchable terms from a chapter/section title."""
    terms = []

    # Extract abbreviations from parentheses BEFORE stripping
    for m in re.finditer(r'[（(]\s*([A-Za-z0-9]+(?:\s+[A-Za-z0-9]+)*)\s*[)）]', title):
        abbr = m.group(1).replace(' ', '').upper()
        if 2 <= len(abbr) <= 10:
            terms.append(abbr)

    # Extract space-separated uppercase letters as abbreviation
    space_abbr = re.findall(r'\b([A-Z](?:\s+[A-Z]){1,6})\b', title)
    for sa in space_abbr:
        abbr = sa.replace(' ', '')
        if abbr not in terms:
            terms.append(abbr)

    # Remove chapter numbers and parentheses content
    clean = re.sub(r'第\d+章|第[一二三四五六七八九十]+章|\d+\.\d+(\.\d+)?|\s+', ' ', title)
    clean = re.sub(r'[（(][^)）]*[)）]', ' ', clean)

    # English terms
    for m in re.finditer(r'\b([A-Z]{2,7}|[A-Z][a-z]+[A-Z][a-zA-Z]*)\b', clean):
        t = m.group(1)
        if t not in ('Windows',):  # too generic alone
            terms.append(t)

    # Chinese terms: split by delimiters
    cn_parts = re.split(r'[、，,。/.\s]+', clean)
    for part in cn_parts:
        part = part.strip()
        if not part:
            continue
        if 2 <= len(part) <= 10:
            terms.append(part)
        # Sliding window for sub-terms in long phrases
        if len(part) > 4:
            for i in range(0, len(part) - 1):
                sub = part[i:i + 2]
                if sub not in terms and not re.match(r'^[\d\s]+$', sub):
                    terms.append(sub)

    # Single Chinese char terms (e.g. "堆", "栈") as fallback
    single_chars = set()
    for ch in re.findall(r'([一-鿿])', clean):
        if ch not in ('和', '与', '及', '的', '在', '是', '不', '了', '有', '人', '这', '中', '大'):
            single_chars.add(ch)
    for ch in single_chars:
        if ch not in terms:
            terms.append(ch)

    return terms


def split_pdf(pdf_path: str, chunks: list[dict], output_dir: str) -> list[str]:
    """Split PDF into chunk files using Ghostscript. Returns list of output paths."""
    gs = _find_ghostscript()
    if not gs:
        raise RuntimeError("Ghostscript not found. Install from https://ghostscript.com/releases/gsdnld.html")

    os.makedirs(output_dir, exist_ok=True)
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"

    outputs = []
    for i, chunk in enumerate(chunks):
        safe_name = re.sub(r'[<>:"/\\|?*\s]', '_', chunk["name"])[:60]
        out_path = os.path.join(output_dir, f"{i+1:02d}-{safe_name}.pdf")
        start, end = chunk["start"], chunk["end"]

        print(f"  Splitting {chunk['name']} (pp {start}-{end})...")
        try:
            subprocess.run(
                [gs, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                 f"-dFirstPage={start}", f"-dLastPage={end}",
                 f"-sOutputFile={out_path}", pdf_path],
                timeout=300, check=True, env=env,
            )
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(f"    → {size_mb:.0f}MB")
            outputs.append(out_path)
        except subprocess.TimeoutExpired:
            print(f"    [WARN] Timeout splitting {chunk['name']} — retrying with smaller range")
            # Fallback: split as a single-page range
            for sub_start in range(start, end + 1, 50):
                sub_end = min(sub_start + 49, end)
                sub_path = os.path.join(output_dir, f"{i+1:02d}-{safe_name}_p{sub_start}-{sub_end}.pdf")
                try:
                    subprocess.run(
                        [gs, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                         f"-dFirstPage={sub_start}", f"-dLastPage={sub_end}",
                         f"-sOutputFile={sub_path}", pdf_path],
                        timeout=120, check=True, env=env,
                    )
                    outputs.append(sub_path)
                except Exception:
                    print(f"      [ERR] Failed pp {sub_start}-{sub_end}, skipping")
        except Exception as e:
            print(f"    [ERR] {e}")

    return outputs


# ── PDF Compression ────────────────────────────────────────

def compress_pdf(input_path: str, max_mb: int = COMPRESS_ABOVE_MB) -> str | None:
    """Compress PDF via Ghostscript /ebook if > max_mb."""
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
            f"PDF is {size_mb:.0f}MB (over {max_mb}MB) but Ghostscript is not installed.\n"
            "Install: https://ghostscript.com/releases/gsdnld.html"
        )
    if os.path.exists(compressed):
        os.unlink(compressed)
    raise RuntimeError(
        f"Ghostscript /ebook could not compress {size_mb:.0f}MB under {max_mb}MB.\n"
        f"Try splitting: py -3 scripts/nlm_pdf_splitter.py \"{input_path}\""
    )


def _compress_ghostscript(src: str, dst: str, max_mb: int) -> bool:
    gs = _find_ghostscript()
    if not gs:
        return False
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    for preset, desc in [("/ebook", "150 DPI"), ("/screen", "72 DPI")]:
        print(f"  Ghostscript {preset} ({desc})...")
        try:
            subprocess.run(
                [gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                 f"-dPDFSETTINGS={preset}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                 f"-sOutputFile={dst}", src],
                timeout=600, check=True, env=env,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            continue
        if _check_and_report(dst, max_mb, f"Ghostscript {preset}"):
            return True
        if os.path.exists(dst):
            os.unlink(dst)
    return False


def _find_ghostscript() -> str | None:
    for name in ["gswin64c", "gswin64", "gswin32c", "gswin32", "gs"]:
        gs = shutil.which(name)
        if gs:
            return gs
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
    new_mb = os.path.getsize(path) / (1024 * 1024)
    if new_mb <= max_mb:
        print(f"  OK {engine_name}: {new_mb:.0f}MB")
        return True
    print(f"  FAIL {engine_name}: {new_mb:.0f}MB — still over {max_mb}MB")
    try:
        os.unlink(path)
    except OSError:
        pass
    return False


# ── NotebookLM Operations ──────────────────────────────────

def _refresh_auth_before_upload():
    """Refresh NotebookLM auth before the long upload+OCR phase (Fix #5)."""
    try:
        import subprocess
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        relogin_script = os.path.join(scripts_dir, "nlm_query.py")
        if os.path.exists(relogin_script):
            r = subprocess.run(
                [sys.executable, relogin_script, "--relogin"],
                capture_output=True, timeout=60,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
            if r.returncode == 0:
                print("  Auth refreshed before upload")
            else:
                print(f"  [WARN] Auth refresh skipped (may already be fresh)")
    except Exception:
        pass  # Non-critical


async def create_and_upload_all(
    split_pdfs: list[str], book_name: str, routes_data: dict = None,
    wait_for_ocr: bool = True, ocr_timeout: int = 600,
) -> tuple[str, str, list, bool]:
    """Create notebook + upload all split PDFs. Returns (nb_id, title, sources, all_ready).

    routes_data: if provided, saved to chapter_routes.json ONLY after OCR success (Fix #6).
    """
    from notebooklm import NotebookLMClient

    # Fix #5: Refresh auth before a potentially long upload+OCR phase
    _refresh_auth_before_upload()

    print(f"\n[1/3] Creating notebook \"{book_name}\"...")
    async with await NotebookLMClient.from_storage() as client:
        notebook = await client.notebooks.create(book_name)
        nb_id = notebook.id
        full_nb_id = nb_id  # keep full ID for source operations
        print(f"  Created: {nb_id}")

        # ── Upload phase ──
        sources = []
        upload_errors = []
        for i, pdf_path in enumerate(split_pdfs):
            fname = os.path.basename(pdf_path)
            size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            print(f"[2/3] Uploading [{i+1}/{len(split_pdfs)}] {fname} ({size_mb:.0f}MB)...")
            try:
                source = await client.sources.add_file(full_nb_id, pdf_path, wait=False)
                sources.append(source)
                print(f"  [OK] Uploaded: {source.id}")
            except Exception as e:
                print(f"  [FAIL] Upload failed: {e}")
                upload_errors.append(fname)

        if not sources:
            return nb_id.split("-")[0], notebook.title, [], False

        if not wait_for_ocr:
            return nb_id.split("-")[0], notebook.title, sources, False

        # ── OCR phase: ALL must succeed ──
        print(f"\n[3/3] OCR indexing {len(sources)} sources "
              f"(timeout={ocr_timeout}s, max 3 retries each)...")
        ocr_ready = []
        ocr_failed = []

        for i, source in enumerate(sources):
            fname = source.title or f"source {i+1}"
            print(f"  [{i+1}/{len(sources)}] {fname[:50]}...")

            ok = False
            for attempt in range(3):
                try:
                    src = await client.sources.wait_until_ready(
                        full_nb_id, source.id, timeout=ocr_timeout
                    )
                    ok = True
                    break
                except Exception as e:
                    wait_s = 5 * (attempt + 1)
                    if attempt < 2:
                        print(f"    Retry {attempt+1} in {wait_s}s: {str(e)[:80]}")
                        await asyncio.sleep(wait_s)
                    else:
                        print(f"    [FAIL] OCR failed after 3 attempts: {str(e)[:120]}")

            if ok:
                print(f"    [OK] OCR ready")
                ocr_ready.append(fname)
            else:
                ocr_failed.append(fname)

        all_ready = len(ocr_failed) == 0

        if not all_ready:
            print(f"\n  [FAIL] OCR INCOMPLETE: {len(ocr_ready)}/{len(sources)} ready, "
                  f"{len(ocr_failed)} failed")
            for fname in ocr_failed:
                print(f"      FIX: try re-uploading '{fname}' as smaller chunks")
            if upload_errors:
                print(f"  Upload errors: {len(upload_errors)}")
            print(f"  Notebook {nb_id.split('-')[0]} NOT marked as ready.")
        else:
            print(f"\n  [OK] ALL {len(ocr_ready)}/{len(sources)} sources OCR-ready")
            # Fix #6: Only save chapter routes AFTER successful OCR
            if routes_data:
                save_routes(book_name, routes_data)
                print(f"  Chapter routes saved for \"{book_name}\"")
                # Auto-detect sub-section tree from NotebookLM
                try:
                    await _ensure_full_toc(book_name, nb_id)
                except Exception:
                    pass

    return nb_id.split("-")[0], notebook.title, sources, all_ready


async def delete_notebook_completely(nb_id: str) -> bool:
    """Delete a notebook and all its sources from NotebookLM."""
    try:
        from notebooklm import NotebookLMClient
        async with await NotebookLMClient.from_storage() as client:
            # Find full notebook ID
            notebooks = await client.notebooks.list()
            full_id = None
            for n in notebooks:
                if n.id.startswith(nb_id):
                    full_id = n.id
                    break
            if not full_id:
                return False
            # Delete all sources first
            sources = await client.sources.list(full_id)
            for s in sources:
                try:
                    await client.sources.delete(full_id, s.id)
                except Exception:
                    pass
            # Then delete the notebook itself
            await client.notebooks.delete(full_id)
            return True
    except Exception:
        return False


# ── Cleanup ────────────────────────────────────────────────

def cleanup_old_notebooks(book_name: str, new_nb_id: str):
    """Delete old notebooks ONLY for the SAME book from both NotebookLM API and local book_map.

    Must be called after new notebook is fully verified (OCR ready).
    Never touches notebooks belonging to other books.
    """
    books = load_book_map()
    to_delete = []

    # Normalize names for fuzzy comparison
    def _same_book(a: str, b: str) -> bool:
        """Check if two book names refer to the same book."""
        a_norm = a.replace(" ", "").replace("-", "").replace("_", "").lower()
        b_norm = b.replace(" ", "").replace("-", "").replace("_", "").lower()
        # One contains the other
        if a_norm in b_norm or b_norm in a_norm:
            return True
        # Share significant keyword overlap
        a_key = a_norm[:4]
        b_key = b_norm[:4]
        return a_key == b_key and len(a_key) >= 2

    for k, v in list(books.items()):
        if not _same_book(k, book_name):
            continue  # ← Skip other books entirely
        if v.get("notebook_id") == new_nb_id:
            continue  # ← Skip the new notebook itself
        to_delete.append((k, v.get("notebook_id")))

    deleted_ids = set()
    for k, old_id in to_delete:
        if old_id in deleted_ids:
            del books[k]
            continue
        print(f"  Deleting old notebook: {k} ({old_id})")
        if asyncio.run(delete_notebook_completely(old_id)):
            print(f"    [OK] Deleted from NotebookLM")
            deleted_ids.add(old_id)
        else:
            print(f"    [WARN] Could not delete from API (may already be gone)")
        del books[k]

    save_book_map(books)
    if deleted_ids:
        print(f"  Cleaned up {len(deleted_ids)} old notebook(s).")


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add a book to NotebookLM v2 — auto-split + multi-source upload"
    )
    parser.add_argument("pdf", nargs="?", help="Path to the book PDF file")
    parser.add_argument("--name", "-n", default=None, help="Book name (defaults to PDF filename)")
    parser.add_argument("--no-compress", action="store_true", help="Skip PDF compression")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for OCR indexing")
    parser.add_argument("--list", action="store_true", help="List all registered books")
    parser.add_argument("--switch", "-s", default=None, metavar="NAME", help="Switch active book by name")
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
                print(f"  {name}")
                print(f"    Notebook: {info.get('notebook_id', '?')}  |  Added: {info.get('added_at', '?')}")
        return 0

    # ── Switch book ──
    if args.switch:
        books = load_book_map()
        name = args.switch
        match = None
        for k in books:
            if name.lower() in k.lower():
                match = k
                break
        if not match:
            print(f"Book \"{name}\" not found.")
            for k in sorted(books):
                print(f"  - {k}")
            return 1
        set_active_book(match)
        print(f"Active book set to: {match}")
        print(f"Notebook ID: {books[match]['notebook_id']}")
        return 0

    # ── Add book ──
    if not args.pdf:
        parser.print_help()
        return 0

    pdf_path = os.path.abspath(args.pdf)
    if not os.path.exists(pdf_path):
        print(f"ERROR: PDF not found: {pdf_path}")
        return 1

    book_name = args.name or _clean_book_name(os.path.splitext(os.path.basename(pdf_path))[0])

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

    # Step 1: Check page count and split if needed
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(upload_path)
        total_pages = len(reader.pages)
        reader = None  # free memory
    except Exception as e:
        print(f"  [WARN] Cannot read page count: {e}. Assuming large PDF.")
        total_pages = 999

    print(f"  Total pages: {total_pages}")

    if total_pages <= MAX_PAGES_PER_CHUNK + 10:  # Allow 10-page grace margin
        print(f"  ≤{MAX_PAGES_PER_CHUNK + 10} pages — direct upload, no splitting needed.")
        split_pdfs = [upload_path]
    else:
        print(f"  >{MAX_PAGES_PER_CHUNK} pages — extracting TOC and splitting by chapters...")
        chapters = extract_toc(upload_path)
        # Auto-generate stages if PDF outline had no Parts (even-group fallback)
        _auto_generate_stages_if_needed(book_name, chapters)
        chunks, routes_data = compute_chunks(chapters, MAX_PAGES_PER_CHUNK)
        print(f"  → {len(chunks)} chunks (max {MAX_PAGES_PER_CHUNK} pages each):")
        for c in chunks:
            print(f"      {c['name']}: pp {c['start']}-{c['end']} ({c['end']-c['start']+1} pages)")

        output_dir = os.path.join(SPLIT_DIR, re.sub(r'[<>:"/\\|?*]', '_', book_name))
        split_pdfs = split_pdf(upload_path, chunks, output_dir)
        print(f"  → Created {len(split_pdfs)} split PDFs in {output_dir}")

        if not split_pdfs:
            print("  [ERR] Splitting failed. Falling back to direct upload.")
            split_pdfs = [upload_path]

    # Step 2-3: Create notebook + upload all
    all_ready = False
    try:
        routes_arg = routes_data if total_pages > MAX_PAGES_PER_CHUNK + 10 else None
        nb_id, title, sources, all_ready = asyncio.run(create_and_upload_all(
            split_pdfs, book_name, routes_data=routes_arg, wait_for_ocr=not args.no_wait
        ))
    except Exception as e:
        msg = str(e)
        print(f"\nERROR: {msg}")
        if "connection" in msg.lower():
            print("  → VPN may be disconnected. Turn on VPN and try again.")
        print("  1. Check auth: py -3 scripts/nlm_query.py --status")
        print("  2. Re-login: py -3 scripts/nlm_query.py --relogin")
        return 1
    finally:
        if compressed and os.path.exists(compressed):
            try:
                os.unlink(compressed)
            except OSError:
                pass

    if not all_ready:
        print(f"\n{'=' * 60}")
        print(f"  [FAIL] Book \"{book_name}\" setup incomplete.")
        print(f"  Notebook {nb_id} exists but some sources failed OCR.")
        print(f"  Check the errors above and fix before retrying.")
        print(f"  Old notebooks NOT cleaned (new one not fully ready).")
        print(f"{'=' * 60}")
        return 1

    # Step 4: Only when ALL sources OCR-ready → clean old + activate
    cleanup_old_notebooks(book_name, nb_id)

    # Step 5: Save mapping
    books = load_book_map()
    books[book_name] = {
        "notebook_id": nb_id,
        "title": title,
        "pdf_path": pdf_path,
        "split_sources": len(split_pdfs) if isinstance(split_pdfs, list) else 1,
        "total_pages": total_pages,
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    save_book_map(books)
    set_active_book(book_name)

    print(f"\n{'=' * 60}")
    print(f"  [OK] Book \"{book_name}\" is ready!")
    print(f"  Notebook ID: {nb_id}")
    print(f"  Sources: {len(split_pdfs) if isinstance(split_pdfs, list) else 1}")
    print(f"  Chapter routes auto-seeded to {ROUTES_FILE}")
    print(f"  You can now ask Claude any question about this book.")
    print(f"{'=' * 60}")

    # Step 6: Auto-init learning progress (drop-and-go)
    _auto_init_progress(book_name, book_name)

    return 0


def _auto_init_progress(book_name: str, progress_book_name: str = None):
    """Auto-initialize learning progress for a newly added book.

    Extracts chapter count from chapter_routes.json boundaries,
    then calls nlm_progress.py --init with the discovered order.
    If the book already has progress data, skips silently.
    """
    target = progress_book_name or book_name
    # Check if already initialized
    progress_file = os.path.expanduser(r"~\.notebooklm\learning_progress.json")
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if target in existing:
                # Check fuzzy match
                for k in existing:
                    if target in k or k in target:
                        return  # Already initialized
        except Exception:
            pass

    # Get chapter count from routes
    routes = load_routes()
    books = routes.get("books", {})
    book_routes = books.get(book_name, {})
    boundaries = book_routes.get("chapter_boundaries", {})
    if not boundaries:
        return

    ch_nums = sorted(int(k) for k in boundaries.keys())
    if not ch_nums:
        return

    order_spec = ",".join(str(c) for c in ch_nums)
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    progress_script = os.path.join(scripts_dir, "nlm_progress.py")
    try:
        subprocess.run(
            [sys.executable, progress_script, "--init", target, order_spec],
            capture_output=True, timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        print(f"  Learning progress auto-initialized ({len(ch_nums)} chapters)")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
