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


# ── Chapter Routes ─────────────────────────────────────────

def load_routes() -> dict:
    if os.path.exists(ROUTES_FILE):
        try:
            with open(ROUTES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"keywords": {}, "chapter_boundaries": {}}


def save_routes(r: dict):
    os.makedirs(os.path.dirname(ROUTES_FILE), exist_ok=True)
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)


# ── PDF TOC Extraction ─────────────────────────────────────

def extract_toc(pdf_path: str) -> list[dict]:
    """Extract chapter boundaries from PDF. Three-tier strategy:

    Tier 1: PDF outline/bookmarks (most reliable, works even for scanned PDFs)
    Tier 2: Text-based regex scan (for PDFs with text layer)
    Tier 3: Chapter presets (for known books)
    Tier 4: Even-page fallback (last resort)

    Returns: [{chapter: int, title: str, start_page: int, end_page: int}, ...]
    """
    total_pages = 100
    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(pdf_path)
        total_pages = len(reader.pages)
    except Exception:
        pass

    # ── Tier 1: PDF bookmarks ──
    try:
        chapters = _extract_toc_from_outline(pdf_path, total_pages)
        if chapters and len(chapters) >= 3:
            print(f"  TOC via PDF bookmarks: {len(chapters)} chapters")
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
    book_name = os.path.splitext(os.path.basename(pdf_path))[0]
    for preset_key, preset_chapters in CHAPTER_PRESETS.items():
        if preset_key in book_name or any(c in book_name for c in ["加密", "解密", "调试", "逆向"]):
            chapters = _build_preset_chapters(preset_chapters, total_pages)
            if chapters:
                print(f"  TOC via preset ({preset_key}): {len(chapters)} chapters")
                return chapters

    # ── Tier 4: Even-page fallback ──
    print(f"  TOC: all tiers failed, even-page fallback")
    return _fallback_even_split(total_pages)


def _extract_toc_from_outline(pdf_path: str, total_pages: int) -> list[dict]:
    """Extract TOC from PDF bookmarks/outline (Tier 1)."""
    from PyPDF2 import PdfReader

    reader = PdfReader(pdf_path)
    outline = reader.outline
    if not outline:
        return []

    chapters = []
    chapter_num = 0

    def _flatten(items, depth=0):
        nonlocal chapter_num
        for item in items:
            if isinstance(item, list):
                _flatten(item, depth + 1)
                continue
            title = str(item.get("/Title", ""))
            if not title or len(title) < 2 or _is_metadata(title):
                continue
            page_obj = item.get("/Page")
            page_num = _resolve_page_number(reader, page_obj)
            if page_num is None or page_num < 1:
                continue
            has_kids = bool(item.get("/Count"))
            # Capture entries that have children (sub-sections) at any depth
            # These are chapter/section-like entries with content
            if has_kids:
                chapter_num += 1
                chapters.append({
                    "chapter": chapter_num,
                    "title": title[:80],
                    "start_page": page_num,
                })

    _flatten(outline)
    return _finalize_chapters(chapters, total_pages)


def _resolve_page_number(reader, page_obj) -> int | None:
    """Resolve a PDF page object to a 1-based page number."""
    if page_obj is None:
        return None
    try:
        # PyPDF2 way: get PageObject and find its index
        from PyPDF2 import PageObject, IndirectObject
        if isinstance(page_obj, IndirectObject):
            page_num = reader.get_page_number(page_obj)
            return page_num + 1  # 0-based → 1-based
        elif hasattr(page_obj, "get_object"):
            obj = page_obj.get_object()
            page_num = reader.get_page_number(obj)
            return page_num + 1
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
            "title": f"Part {len(chapters) + 1} (pp {start}-{end})",
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


def _fallback_chapter_split(pdf_path: str) -> list:
    """When TOC extraction fails, try chapter preset then even-page split."""
    # Try to match a known book preset from the filename
    book_name = os.path.splitext(os.path.basename(pdf_path))[0]
    matched_preset = None
    for preset_key, preset_chapters in CHAPTER_PRESETS.items():
        if preset_key in book_name or any(c in book_name for c in ["加密", "解密"]):
            matched_preset = preset_chapters
            print(f"  Using chapter preset for: {preset_key}")
            break

    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(pdf_path)
        total_pages = len(reader.pages)
    except Exception:
        total_pages = 948

    if matched_preset:
        chapters = []
        for ch_num in sorted(matched_preset.keys()):
            entry = matched_preset[ch_num]
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

    # Even-page fallback
    chapters = []
    for start in range(1, total_pages + 1, MAX_PAGES_PER_CHUNK):
        end = min(start + MAX_PAGES_PER_CHUNK - 1, total_pages)
        chapters.append({
            "chapter": len(chapters) + 1,
            "title": f"Part {len(chapters) + 1} (pp {start}-{end})",
            "start_page": start,
            "end_page": end,
        })
    return chapters


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

    # Also record in chapter_routes.json for future routing
    routes = load_routes()
    for ch in chapters:
        routes["chapter_boundaries"][str(ch["chapter"])] = {
            "start": ch["start_page"],
            "end": ch["end_page"],
            "title": ch["title"],
        }
    # Auto-seed keywords from chapter titles and preset keywords
    for ch in chapters:
        title = ch["title"]
        # Use preset keywords if available, otherwise extract from title
        if ch.get("keywords"):
            terms = ch["keywords"].split("|")
        else:
            terms = _extract_terms(title)
        for term in terms:
            term = term.strip()
            if term and len(term) >= 2:
                if term not in routes["keywords"]:
                    routes["keywords"][term] = {
                        "chapter": ch["chapter"],
                        "pages": f"{ch['start_page']}-{ch['end_page']}",
                        "source": "preset" if ch.get("keywords") else "toc_seed",
                        "hits": 0,
                        "last_hit": "",
                    }
    save_routes(routes)

    return chunks


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

async def create_and_upload_all(
    split_pdfs: list[str], book_name: str, wait_for_ocr: bool = True,
    ocr_timeout: int = 600,
) -> tuple[str, str, list, bool]:
    """Create notebook + upload all split PDFs. Returns (nb_id, title, sources, all_ready).

    CRITICAL: ALL sources must pass OCR before all_ready=True.
    If any source fails OCR after retries, the notebook is NOT marked ready
    and the caller should NOT activate this notebook.
    """
    from notebooklm import NotebookLMClient

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
                print(f"  ✓ Uploaded: {source.id}")
            except Exception as e:
                print(f"  ✗ Upload failed: {e}")
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
                        print(f"    ✗ OCR failed after 3 attempts: {str(e)[:120]}")

            if ok:
                print(f"    ✓ OCR ready")
                ocr_ready.append(fname)
            else:
                ocr_failed.append(fname)

        all_ready = len(ocr_failed) == 0

        if not all_ready:
            print(f"\n  ✗ OCR INCOMPLETE: {len(ocr_ready)}/{len(sources)} ready, "
                  f"{len(ocr_failed)} failed")
            for fname in ocr_failed:
                print(f"      FIX: try re-uploading '{fname}' as smaller chunks")
            if upload_errors:
                print(f"  Upload errors: {len(upload_errors)}")
            print(f"  Notebook {nb_id.split('-')[0]} NOT marked as ready.")
        else:
            print(f"\n  ✓ ALL {len(ocr_ready)}/{len(sources)} sources OCR-ready")

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
            print(f"    ✓ Deleted from NotebookLM")
            deleted_ids.add(old_id)
        else:
            print(f"    ⚠ Could not delete from API (may already be gone)")
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
        chunks = compute_chunks(chapters, MAX_PAGES_PER_CHUNK)
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
        nb_id, title, sources, all_ready = asyncio.run(create_and_upload_all(
            split_pdfs, book_name, wait_for_ocr=not args.no_wait
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
        print(f"  ✗ Book \"{book_name}\" setup incomplete.")
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
