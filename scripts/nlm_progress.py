#!/usr/bin/env python
"""Learning progress tracker — per-book chapter progress + auto-resume + time tracking.

Data file: ~/.notebooklm/learning_progress.json

CLI:
  nlm_progress.py --show [book]          Show progress for active book
  nlm_progress.py --show --detail        Show per-section detail
  nlm_progress.py --mark "7.2" done      Mark section/chapter complete
  nlm_progress.py --next                  Show what to learn next
  nlm_progress.py --init "book" --order "1,2,3,11,7,8"   Set chapter order
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Optional

PROGRESS_FILE = os.path.expanduser(r"~\.notebooklm\learning_progress.json")
BOOK_MAP_FILE = os.path.expanduser(r"~\.notebooklm\book_map.json")
ACTIVE_BOOK_FILE = os.path.expanduser(r"~\.notebooklm\profiles\default\active_book.json")
CHAPTER_ROUTES_FILE = os.path.expanduser(r"~\.notebooklm\chapter_routes.json")


def _s(spec: str) -> dict:
    """Parse section spec like '7.1-7.3' or '1' into {'7.1': 'pending', ...}."""
    parts = spec.split(",")
    result = {}
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Full section range: "7.1-7.3" (chapter.section-chapter.section)
        m = re.match(r'^(\d+)\.(\d+)-(\d+)\.(\d+)$', p)
        if m:
            ch1, s1, ch2, s2 = m.groups()
            if ch1 == ch2:
                for i in range(int(s1), int(s2) + 1):
                    result[f"{ch1}.{i}"] = "pending"
                continue
            # Chapter numbers differ — fall through to literal
        # Chapter range: "1-3"
        m = re.match(r'^(\d+)-(\d+)$', p)
        if m:
            for i in range(int(m.group(1)), int(m.group(2)) + 1):
                result[str(i)] = "pending"
            continue
        # Single section or chapter: "7.1" or "7"
        result[p] = "pending"
    return result


# ── Learning stages (group chapters by phase) ──────────────

STAGES = [
    {"name": "阶段1 地基",      "chapters": [1, 2, 3],    "desc": "基础工具链"},
    {"name": "阶段2 PE格式",    "chapters": [11],          "desc": "PE文件格式精通"},
    {"name": "阶段3 内核基础",  "chapters": [7, 8, 9],     "desc": "Windows内核入门"},
    {"name": "阶段4 实战技术",  "chapters": [12, 13, 14, 16], "desc": "注入/Hook/漏洞/脱壳"},
    {"name": "阶段5 虚拟化",    "chapters": [10],          "desc": "VT虚拟化技术"},
    {"name": "阶段6 反跟踪",    "chapters": [17],          "desc": "反跟踪技术"},
    {"name": "阶段7 补学用户态","chapters": [4, 5, 6],     "desc": "逆向分析/保护/加密"},
    {"name": "阶段8 进阶保护",  "chapters": [15, 18],      "desc": "软件保护/外壳编写"},
    {"name": "阶段9 高级专题",  "chapters": [19, 20, 21],  "desc": "虚拟化保护/实战"},
    {"name": "阶段10 扩展阅读", "chapters": [22, 23, 24, 25], "desc": "选读内容"},
]


# ── Default chapter orders for known books ─────────────────
# Chapter titles & page ranges verified against book TOC via NotebookLM.
# Section ranges are estimates — use --init to correct if actual TOC differs.

DEFAULT_ORDERS = {
    "加密与解密第四版": {
        "stages": [
            {"name": "阶段1 地基",      "chapters": [1, 2, 3],    "desc": "基础工具链"},
            {"name": "阶段2 PE格式",    "chapters": [11],          "desc": "PE文件格式精通"},
            {"name": "阶段3 内核基础",  "chapters": [7, 8, 9],     "desc": "Windows内核入门"},
            {"name": "阶段4 实战技术",  "chapters": [12, 13, 14, 16], "desc": "注入/Hook/漏洞/脱壳"},
            {"name": "阶段5 虚拟化",    "chapters": [10],          "desc": "VT虚拟化技术"},
            {"name": "阶段6 反跟踪",    "chapters": [17],          "desc": "反跟踪技术"},
            {"name": "阶段7 补学用户态","chapters": [4, 5, 6],     "desc": "逆向分析/保护/加密"},
            {"name": "阶段8 进阶保护",  "chapters": [15, 18],      "desc": "软件保护/外壳编写"},
            {"name": "阶段9 高级专题",  "chapters": [19, 20, 21],  "desc": "虚拟化保护/实战"},
            {"name": "阶段10 扩展阅读", "chapters": [22, 23, 24, 25], "desc": "选读内容"},
        ],
        "chapter_order": [
            {"ch": 1,  "title": "基础知识",              "sections": _s("1.1-1.3"),
             "pages": "1-59"},
            {"ch": 2,  "title": "动态分析技术",           "sections": _s("2.1-2.5"),
             "pages": "60-119"},
            {"ch": 3,  "title": "静态分析技术",           "sections": _s("3.1-3.4"),
             "pages": "120-170"},
            {"ch": 11, "title": "PE文件格式 (提前)",      "sections": _s("11.1-11.12"),
             "pages": "404-460"},
            {"ch": 7,  "title": "Windows内核基础",        "sections": _s("7.1-7.3"),
             "pages": "277-340"},
            {"ch": 8,  "title": "SEH异常处理",            "sections": _s("8.1-8.3"),
             "pages": "341-360"},
            {"ch": 9,  "title": "Win32调试API",          "sections": _s("9.1-9.4"),
             "pages": "361-380"},
            {"ch": 12, "title": "DLL注入技术",            "sections": _s("12.1-12.6"),
             "pages": "461-495"},
            {"ch": 13, "title": "API Hook技术",           "sections": _s("13.1-13.4"),
             "pages": "496-530"},
            {"ch": 14, "title": "二进制漏洞分析",         "sections": _s("14.1-14.10"),
             "pages": "531-599"},
            {"ch": 16, "title": "脱壳技术",               "sections": _s("16.1-16.6"),
             "pages": "661-720"},
            {"ch": 10, "title": "VT虚拟化技术 (高阶)",     "sections": _s("10.1-10.4"),
             "pages": "381-403"},
            {"ch": 17, "title": "反跟踪技术",             "sections": _s("17.1-17.3"),
             "pages": "721-750"},
            {"ch": 4,  "title": "逆向分析技术 (补学)",     "sections": _s("4.1-4.5"),
             "pages": "171-196"},
            {"ch": 5,  "title": "演示版保护技术 (补学)",   "sections": _s("5.1-5.4"),
             "pages": "197-222"},
            {"ch": 6,  "title": "加密算法 (补学)",        "sections": _s("6.1-6.6"),
             "pages": "223-276"},
            {"ch": 15, "title": "软件保护技术",           "sections": _s("15.1-15.7"),
             "pages": "600-660"},
            {"ch": 18, "title": "外壳编写基础",           "sections": _s("18.1-18.4"),
             "pages": "751-780"},
            {"ch": 19, "title": "虚拟化保护技术",         "sections": _s("19.1-19.4"),
             "pages": "781-810"},
            {"ch": 20, "title": "重构与适配",             "sections": _s("20.1-20.4"),
             "pages": "811-850"},
            {"ch": 21, "title": "加密与解密实战",         "sections": _s("21.1-21.4"),
             "pages": "851-880"},
            {"ch": 22, "title": "电子取证技术 (选读)",     "sections": _s("22.1-22.4"),
             "pages": "881-900"},
            {"ch": 23, "title": "移动平台安全 (选读)",     "sections": _s("23.1-23.4"),
             "pages": "901-920"},
            {"ch": 24, "title": "其他",                  "sections": _s("24"),
             "pages": "921-940"},
            {"ch": 25, "title": "附录",                  "sections": _s("25"),
             "pages": "941-948"},
        ],
        "reasoning": (
            "按内核安全路线图重排: 地基(1-3章)→PE(11章)→内核(7-9章)→实战(12-14,16章)→"
            "高阶(10章VT)→对抗(17章)→补学用户态(4-6章)→进阶保护(15,18章)→专题(19-25章)"
        ),
    },
    "软件调试": {
        "chapter_order": [
            {"ch": 1,  "title": "软件调试基础",    "sections": _s("1")},
            {"ch": 4,  "title": "断点和单步执行",  "sections": _s("4")},
            {"ch": 8,  "title": "Windows概要",     "sections": _s("8")},
            {"ch": 9,  "title": "用户态调试模型",  "sections": _s("9")},
            {"ch": 10, "title": "用户态调试过程",  "sections": _s("10")},
            {"ch": 30, "title": "WinDBG用法详解",  "sections": _s("30")},
            {"ch": 18, "title": "内核调试引擎",    "sections": _s("18")},
            {"ch": 23, "title": "堆和堆检查",      "sections": _s("23")},
            {"ch": 22, "title": "栈和函数调用",    "sections": _s("22")},
            {"ch": 2,  "title": "CPU基础",         "sections": _s("2")},
            {"ch": 3,  "title": "中断和异常",      "sections": _s("3")},
            {"ch": 5,  "title": "分支记录和性能监视",  "sections": _s("5")},
            {"ch": 6,  "title": "机器检查架构",    "sections": _s("6")},
            {"ch": 7,  "title": "JTAG调试",        "sections": _s("7")},
            {"ch": 11, "title": "中断和异常管理",  "sections": _s("11")},
            {"ch": 12, "title": "未处理异常和JIT", "sections": _s("12")},
            {"ch": 13, "title": "硬错误和蓝屏",    "sections": _s("13")},
            {"ch": 14, "title": "错误报告",        "sections": _s("14")},
            {"ch": 15, "title": "日志",            "sections": _s("15")},
            {"ch": 16, "title": "事件追踪",        "sections": _s("16")},
            {"ch": 17, "title": "WHEA",            "sections": _s("17")},
            {"ch": 19, "title": "内核调试过程",    "sections": _s("19")},
            {"ch": 20, "title": "远程调试",        "sections": _s("20")},
            {"ch": 21, "title": "运行库和运行期检查", "sections": _s("21")},
            {"ch": 24, "title": "高级调试技术",    "sections": _s("24")},
            {"ch": 25, "title": "调试工具",        "sections": _s("25")},
            {"ch": 26, "title": "调试案例分析",    "sections": _s("26")},
            {"ch": 27, "title": "软件调试未来",    "sections": _s("27")},
            {"ch": 28, "title": "附录A",          "sections": _s("28")},
            {"ch": 29, "title": "附录B",          "sections": _s("29")},
        ],
        "reasoning": "调试工具先行 → 用户态掌握 → 内核态深入 → CPU/中断机理 → 高级主题 → 附录",
    },
}


# ── Core Functions ────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_progress(p: dict):
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)


def get_active_book() -> Optional[str]:
    if os.path.exists(ACTIVE_BOOK_FILE):
        try:
            with open(ACTIVE_BOOK_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("name")
        except Exception:
            pass
    return None


def get_book_progress(book_name: str) -> dict:
    progress = load_progress()
    if book_name not in progress:
        progress[book_name] = _init_book(book_name)
        save_progress(progress)
        _sync_chapter_routes(book_name, progress[book_name])
    elif _migrate_if_needed(book_name, progress):
        save_progress(progress)
    return progress[book_name]


def _init_book(book_name: str) -> dict:
    """Initialize a new book entry with default order if available."""
    entry = {
        "chapter_order": [],
        "created_at": time.strftime("%Y-%m-%d"),
        "last_session": time.strftime("%Y-%m-%d"),
        "total_sessions": 0,
        "version": 2,
    }
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            entry["chapter_order"] = _deep_copy_order(data["chapter_order"])
            entry["order_reasoning"] = data.get("reasoning", "")
            break
    return entry


def _deep_copy_order(order: list) -> list:
    """Deep copy chapter order, converting section dicts safely."""
    import copy
    return copy.deepcopy(order)


def _sync_chapter_routes(book_name: str, bp: dict):
    """Sync chapter page boundaries to namespaced chapter_routes.json (Fix #1)."""
    data = {}
    if os.path.exists(CHAPTER_ROUTES_FILE):
        try:
            with open(CHAPTER_ROUTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if "books" not in data:
        data = {"books": {}}
    if book_name not in data["books"]:
        data["books"][book_name] = {"keywords": {}, "chapter_boundaries": {}}

    boundaries = data["books"][book_name].get("chapter_boundaries", {})
    updated = False

    for ch_entry in bp.get("chapter_order", []):
        ch_str = str(ch_entry["ch"])
        pages = ch_entry.get("pages", "")
        if pages and "-" in pages:
            parts = pages.split("-")
            start, end = int(parts[0]), int(parts[1])
            if ch_str not in boundaries or boundaries[ch_str].get("start") != start:
                boundaries[ch_str] = {
                    "start": start,
                    "end": end,
                    "title": f"第{ch_str}章 {ch_entry['title']}",
                }
                updated = True

    if updated:
        data["books"][book_name]["chapter_boundaries"] = boundaries
        os.makedirs(os.path.dirname(CHAPTER_ROUTES_FILE), exist_ok=True)
        with open(CHAPTER_ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _migrate_if_needed(book_name: str, progress: dict):
    """Migrate old-format progress data to current version.
    Modifies `progress` dict in place. Caller should save_progress() after."""
    bp = progress.get(book_name)
    if not bp:
        return False

    version = bp.get("version", 1)
    if version >= 2:
        return _normalize_sections(book_name, bp)

    # v1 → v2: migrate section values from strings to dicts with timestamps
    for ch_entry in bp.get("chapter_order", []):
        sections = ch_entry.get("sections", {})
        for sec_id, sec_val in list(sections.items()):
            if isinstance(sec_val, str):
                new_val = {"status": sec_val}
                if sec_val in ("done", "completed"):
                    new_val["completed_at"] = bp.get("last_session", "")
                sections[sec_id] = new_val
        ch_status = ch_entry.get("status")
        if ch_status == "done":
            ch_entry["status"] = "completed"

    bp["version"] = 2
    _normalize_sections(book_name, bp)
    return True


def _normalize_sections(book_name: str, bp: dict):
    """Fix section counts to match DEFAULT_ORDERS spec (repair _s() bug damage)."""
    default_order = None
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            default_order = data["chapter_order"]
            break
    if not default_order:
        return

    # Build lookup of correct sections per chapter.
    # If chapter_routes.json has sub_sections (from PDF bookmarks), use those.
    # Otherwise fall back to DEFAULT_ORDERS section specs.
    correct = {}
    cr_boundaries = _load_chapter_boundaries(book_name)
    for ch_entry in default_order:
        ch = ch_entry["ch"]
        sections = set(ch_entry.get("sections", {}).keys())
        # Check for sub_sections from PDF bookmarks
        cr_ch = cr_boundaries.get(str(ch), {})
        cr_subs = cr_ch.get("sub_sections", {})
        if cr_subs:
            # Use actual sub-section IDs from PDF bookmarks
            sections = set(cr_subs.keys())
        correct[ch] = {
            "title": ch_entry.get("title", ""),
            "sections": sections,
        }

    updated = False
    for ch_entry in bp.get("chapter_order", []):
        ch = ch_entry["ch"]
        if ch not in correct:
            continue

        # Fix title if wrong
        expected_title = correct[ch]["title"]
        if ch_entry.get("title") != expected_title and expected_title:
            ch_entry["title"] = expected_title
            updated = True

        # Fix section keys: remove extras, add missing.
        # If chapter is completed, new sections inherit "done" status.
        sections = ch_entry.get("sections", {})
        expected_keys = correct[ch]["sections"]
        actual_keys = set(sections.keys())

        extras = actual_keys - expected_keys
        missing = expected_keys - actual_keys
        ch_status = ch_entry.get("status", "pending")

        # Don't delete sub-sections that were manually expanded
        # (e.g. DEFAULT_ORDERS has "7.2" but progress has "7.2.1-7.2.4").
        # Only remove truly invalid keys (wrong chapter prefix or nonexistent).
        safe_extras = {e for e in extras if not any(
            e.startswith(m + ".") for m in missing
        )}
        truly_extra = extras - safe_extras
        for ek in truly_extra:
            del sections[ek]
            updated = True
        for mk in missing:
            if ch_status in ("completed", "done"):
                sections[mk] = {"status": "done", "completed_at": bp.get("last_session", "")}
            else:
                sections[mk] = "pending"
            updated = True

        # Recalculate chapter status after section changes
        if ch_status not in ("completed", "done"):
            all_done = all(
                (v.get("status") if isinstance(v, dict) else v) in ("done", "completed")
                for v in sections.values()
            )
            if all_done:
                ch_entry["status"] = "completed"
                updated = True

    return updated


# ── Section value helpers (v2: dict with timestamps) ─────

def _sec_status(sec_val) -> str:
    """Extract status string from section value (str or dict)."""
    if isinstance(sec_val, dict):
        return sec_val.get("status", "pending")
    return sec_val if sec_val else "pending"


def _ensure_dict(sec_val):
    """Convert string section value to dict format."""
    if isinstance(sec_val, dict):
        return sec_val
    return {"status": sec_val if sec_val else "pending"}


# ── Marking & Auto-Tracking ──────────────────────────────

def mark_completed(book_name: str, section_id: str, status: str = "done"):
    """Mark a section or chapter as completed with time tracking."""
    progress = load_progress()
    if book_name not in progress:
        progress[book_name] = _init_book(book_name)

    bp = progress[book_name]
    bp["last_session"] = time.strftime("%Y-%m-%d")
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    matched = False
    for ch_entry in bp.get("chapter_order", []):
        ch_str = str(ch_entry["ch"])
        if section_id == ch_str:
            matched = True
            ch_entry["status"] = "completed" if status in ("done", "completed") else status
            for k in ch_entry.get("sections", {}):
                val = _ensure_dict(ch_entry["sections"][k])
                val["status"] = "done" if status in ("done", "completed") else status
                if status in ("done", "completed"):
                    val["completed_at"] = val.get("completed_at") or now_ts
                ch_entry["sections"][k] = val
            break
        elif section_id.startswith(ch_str + "."):
            sections = ch_entry.get("sections", {})
            if section_id in sections:
                matched = True
                val = _ensure_dict(sections[section_id])
                val["status"] = status
                if status in ("done", "completed"):
                    val["completed_at"] = now_ts
                elif status == "learning":
                    val["started_at"] = val.get("started_at") or now_ts
                sections[section_id] = val

                # Update chapter status
                all_done = all(_sec_status(v) in ("done", "completed") for v in sections.values())
                any_learning = any(_sec_status(v) == "learning" for v in sections.values())
                any_done = any(_sec_status(v) in ("done", "completed") for v in sections.values())
                if all_done:
                    ch_entry["status"] = "completed"
                elif any_done or any_learning:
                    ch_entry["status"] = "in_progress"
                else:
                    ch_entry["status"] = "pending"
            else:
                # Section not found in this chapter
                continue

    if not matched:
        print(f"[progress] Warning: '{section_id}' not found in chapter order — nothing marked.",
              file=sys.stderr)
        return

    bp["total_sessions"] = bp.get("total_sessions", 0) + 1
    save_progress(progress)


def auto_track_learning(book_name: str, chapter_num: int, section_num: int = None):
    """Called by nlm_query when a NotebookLM answer matches a chapter.
    Marks the section/chapter as 'learning' if currently pending.
    """
    progress = load_progress()
    if book_name not in progress:
        return

    bp = progress[book_name]
    bp["last_session"] = time.strftime("%Y-%m-%d")
    now_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    updated = False

    ch_str = str(chapter_num)
    sec_str = f"{chapter_num}.{section_num}" if section_num else None

    for ch_entry in bp.get("chapter_order", []):
        if str(ch_entry["ch"]) != ch_str:
            continue

        # Mark chapter as in_progress if pending
        if ch_entry.get("status") in ("pending", None):
            ch_entry["status"] = "in_progress"
            updated = True

        if sec_str:
            sections = ch_entry.get("sections", {})
            if sec_str in sections:
                sec_val = _ensure_dict(sections[sec_str])
                if sec_val.get("status") in ("pending", None):
                    sec_val["status"] = "learning"
                    sec_val["started_at"] = sec_val.get("started_at") or now_ts
                    sections[sec_str] = sec_val
                    updated = True
            # Also look for partial match (e.g., answer cites page within a range)
            # Find which section the answered page falls into
        break

    if updated:
        save_progress(progress)


# ── Next / Resume ────────────────────────────────────────

def get_next(book_name: str) -> Optional[dict]:
    """Return the next chapter/section to learn."""
    bp = get_book_progress(book_name)
    bp["last_session"] = time.strftime("%Y-%m-%d")
    save_progress({**load_progress(), book_name: bp})

    for ch_entry in bp.get("chapter_order", []):
        ch_status = ch_entry.get("status")
        if ch_status in ("pending", None):
            return {"type": "chapter", "ch": ch_entry["ch"], "title": ch_entry["title"]}
        if ch_status in ("completed", "done"):
            continue  # Chapter fully done — skip sections
        sections = ch_entry.get("sections", {})
        # Priority: learning > pending
        learning_sec = None
        for sec_id in sorted(sections.keys()):
            st = _sec_status(sections[sec_id])
            if st == "learning":
                learning_sec = sec_id
                break
        if learning_sec:
            return {"type": "section", "id": learning_sec, "chapter": ch_entry["ch"],
                    "title": f"{ch_entry['title']} - {learning_sec}"}
        for sec_id in sorted(sections.keys()):
            st = _sec_status(sections[sec_id])
            if st == "pending":
                return {"type": "section", "id": sec_id, "chapter": ch_entry["ch"],
                        "title": f"{ch_entry['title']} - {sec_id}"}
    return None


# ── Show Progress (stage-grouped) ─────────────────────────

def _setup_encoding():
    """Ensure stdout can handle UTF-8 on Windows GBK terminals."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _get_stages_for_book(book_name: str) -> list | None:
    """Return stages from DEFAULT_ORDERS, or auto_stages from chapter_routes, or None.

    Priority: DEFAULT_ORDERS preset > PDF-outline auto_stages > even-group auto_stages > None (flat).
    """
    # 1. Check DEFAULT_ORDERS preset
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            return data.get("stages")
    # 2. Check auto_stages from PDF outline or even-group generation
    boundaries = _load_chapter_boundaries(book_name)
    # _load_chapter_boundaries returns chapter_boundaries dict, but auto_stages
    # is stored alongside it. Need to read from the full chapter_routes.json.
    if os.path.exists(CHAPTER_ROUTES_FILE):
        try:
            with open(CHAPTER_ROUTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            books = data.get("books", {})
            for bkey, bdata in books.items():
                if book_name in bkey or bkey in book_name:
                    auto = bdata.get("auto_stages")
                    if auto:
                        return auto
        except (json.JSONDecodeError, OSError):
            pass
    return None


def show_progress(book_name: str, detail: bool = False):
    """Print learning progress for a book, grouped by learning stages."""
    bp = get_book_progress(book_name)
    order = bp.get("chapter_order", [])
    if not order:
        print(f"No chapter order set for '{book_name}'.")
        print(f"Use --init to set up learning order.")
        return

    ch_map = {ch_entry["ch"]: ch_entry for ch_entry in order}

    print(f"Book: {book_name}")
    print(f"Created: {bp.get('created_at', '?')}  |  "
          f"Last session: {bp.get('last_session', '?')}  |  "
          f"Sessions: {bp.get('total_sessions', 0)}")
    print()

    # Overall stats
    completed = sum(1 for c in order if c.get("status") in ("completed", "done"))
    in_progress = sum(1 for c in order if c.get("status") == "in_progress")
    pending = sum(1 for c in order if c.get("status") in ("pending", None))
    total = len(order)

    bar_len = 40
    done_bar = int(bar_len * completed / max(total, 1))
    prog_bar = int(bar_len * (completed + in_progress * 0.5) / max(total, 1))
    bar = "#" * done_bar + "=" * max(0, prog_bar - done_bar) + "-" * (bar_len - prog_bar)
    pct = 100 * completed // max(total, 1)
    print(f"Overall: [{bar}] {completed}/{total} ({pct}%)")
    print()

    # Per-stage display — use book-specific stages if defined, else flat list (Fix #2)
    stages = _get_stages_for_book(book_name)

    def _print_chapter(ch_entry, prefix="  "):
        """Print a single chapter line (used by both staged and flat display)."""
        ch = ch_entry["ch"]
        title = ch_entry["title"]
        status = ch_entry.get("status", "pending") or "pending"
        pages = ch_entry.get("pages", "")
        page_info = f"(pp{pages})" if pages else ""
        icon = "[OK]" if status in ("completed", "done") else ("[>>]" if status == "in_progress" else "[  ]")
        time_info = ""
        if detail:
            sections = ch_entry.get("sections", {})
            total_mins = 0
            for sv in sections.values():
                svd = _ensure_dict(sv)
                if svd.get("completed_at") and svd.get("started_at"):
                    try:
                        start = time.mktime(time.strptime(svd["started_at"][:19], "%Y-%m-%dT%H:%M:%S"))
                        end = time.mktime(time.strptime(svd["completed_at"][:19], "%Y-%m-%dT%H:%M:%S"))
                        total_mins += max(0, (end - start) / 60)
                    except (ValueError, OSError):
                        pass
            if total_mins > 0:
                time_info = f" [{total_mins:.0f}min]"
        title_part = f" {title}" if title else ""
        print(f"{prefix}{icon} 第{ch:2d}章{title_part} {page_info}{time_info}")
        if detail and ch_entry.get("sections"):
            for sec_id in sorted(ch_entry["sections"].keys(),
                                 key=lambda x: (int(x.split(".")[0]), int(x.split(".")[1]) if "." in x and x.split(".")[1].isdigit() else 0)):
                sec_val = _ensure_dict(ch_entry["sections"][sec_id])
                st = sec_val.get("status", "pending")
                s_icon = "  [OK]" if st in ("done", "completed") else ("  [>>]" if st == "learning" else "  [  ]")
                t_str = ""
                started = sec_val.get("started_at", "")
                completed = sec_val.get("completed_at", "")
                if started and completed:
                    try:
                        t1 = time.mktime(time.strptime(started[:19], "%Y-%m-%dT%H:%M:%S"))
                        t2 = time.mktime(time.strptime(completed[:19], "%Y-%m-%dT%H:%M:%S"))
                        mins = max(0, (t2 - t1) / 60)
                        if mins > 0:
                            t_str = f" ({mins:.0f}min)"
                    except (ValueError, OSError):
                        pass
                print(f"       {s_icon} {sec_id}{t_str}")

    if stages:
        for stage in stages:
            stage_chs = [ch_map[ch] for ch in stage["chapters"] if ch in ch_map]
            if not stage_chs:
                continue
            st_done = sum(1 for c in stage_chs if c.get("status") in ("completed", "done"))
            st_prog = sum(1 for c in stage_chs if c.get("status") == "in_progress")
            st_total = len(stage_chs)
            s_bar_len = 30
            s_done_bar = int(s_bar_len * st_done / max(st_total, 1))
            s_prog_bar = int(s_bar_len * (st_done + st_prog * 0.5) / max(st_total, 1))
            s_bar = "#" * s_done_bar + "=" * max(0, s_prog_bar - s_done_bar) + "-" * (s_bar_len - s_prog_bar)
            s_pct = 100 * st_done // max(st_total, 1)
            print(f"  {stage['name']} [{s_bar}] {st_done}/{st_total} ({s_pct}%)  {stage['desc']}")
            for ch_entry in stage_chs:
                _print_chapter(ch_entry, prefix="     ")
            print()
    else:
        # Generic flat display (no stages defined)
        for ch_entry in order:
            _print_chapter(ch_entry, prefix="  ")
        print()

    # Next
    next_item = get_next(book_name)
    if next_item:
        if next_item["type"] == "chapter":
            label = f"第{next_item['ch']}章 {next_item['title']}"
        else:
            label = f"{next_item['id']} ({next_item['title']})"
        print(f"Next: {label}")
    else:
        print("All chapters completed!")


# ── Init / Set Order ─────────────────────────────────────

def set_chapter_order(book_name: str, order_spec: str):
    """Set chapter order from comma-separated spec like '1,2,3,11,7,8'."""
    progress = load_progress()
    if book_name not in progress:
        progress[book_name] = _init_book(book_name)

    ch_nums = [int(x.strip()) for x in order_spec.split(",")]

    # Build title and section lookup from DEFAULT_ORDERS
    title_map = {}
    section_map = {}
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            for entry in data["chapter_order"]:
                title_map[entry["ch"]] = entry["title"]
                section_map[entry["ch"]] = entry.get("sections", {}).copy()
                # Also copy pages if present
                if "pages" in entry:
                    pass  # pages stored per-entry
            break

    # Fix #3: Fallback for books without DEFAULT_ORDERS — try chapter_routes
    # boundaries (namespaced), then fall back to empty title.
    if not title_map:
        boundaries = _load_chapter_boundaries(book_name)
        for ch_num in ch_nums:
            b = boundaries.get(str(ch_num), {})
            ch_title = b.get("title", "")
            # Strip "第X章 " prefix from boundary title if present
            ch_title = re.sub(r'^第\s*\d+\s*章\s*', '', ch_title)
            title_map[ch_num] = ch_title

    new_order = []
    for ch_num in ch_nums:
        title = title_map.get(ch_num, "")
        sections = section_map.get(ch_num, {f"{ch_num}.1": "pending"})
        # Preserve pages from DEFAULT_ORDERS
        pages = ""
        for key, data in DEFAULT_ORDERS.items():
            if key in book_name or book_name in key:
                for entry in data["chapter_order"]:
                    if entry["ch"] == ch_num:
                        pages = entry.get("pages", "")
                        break
                break
        entry = {"ch": ch_num, "title": title, "sections": sections}
        if pages:
            entry["pages"] = pages
        new_order.append(entry)

    progress[book_name]["chapter_order"] = new_order
    progress[book_name]["version"] = 2
    save_progress(progress)
    print(f"Set {len(new_order)} chapters in order: {', '.join(str(c['ch']) for c in new_order)}")


def _load_chapter_boundaries(book_name: str = None) -> dict:
    """Load chapter boundaries from namespaced chapter_routes.json (Fix #1)."""
    if os.path.exists(CHAPTER_ROUTES_FILE):
        try:
            with open(CHAPTER_ROUTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            books = data.get("books", {})
            if book_name and book_name in books:
                return books[book_name].get("chapter_boundaries", {})
            # Fallback: first book's boundaries
            if books:
                return next(iter(books.values())).get("chapter_boundaries", {})
        except Exception:
            pass
    return {}


def touch_session(book_name: str):
    """Update last_session timestamp without incrementing session count."""
    progress = load_progress()
    if book_name in progress:
        progress[book_name]["last_session"] = time.strftime("%Y-%m-%d")
        save_progress(progress)


def _load_book_map() -> dict:
    """Load book_map.json."""
    if os.path.exists(BOOK_MAP_FILE):
        try:
            with open(BOOK_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def list_books():
    """Show all registered books with progress summaries."""
    books = _load_book_map()
    active = get_active_book()
    if not books:
        print("No registered books. Add one with nlm_add_book.py")
        return

    progress = load_progress()
    # Read max_depth from chapter_routes
    cr_data = {}
    if os.path.exists(CHAPTER_ROUTES_FILE):
        try:
            with open(CHAPTER_ROUTES_FILE, "r", encoding="utf-8") as f:
                cr_data = json.load(f)
        except Exception:
            pass
    cr_books = cr_data.get("books", {})

    print(f"\n  {len(books)} book(s) registered:\n")
    for name, info in books.items():
        bp = progress.get(name, {})
        order = bp.get("chapter_order", [])
        done = sum(1 for c in order if c.get("status") in ("completed", "done"))
        total = len(order)
        # Get total sub-sections and max depth
        total_secs = sum(len(c.get("sections", {})) for c in order)
        max_d = 1
        for cr_name, cr_bdata in cr_books.items():
            if name in cr_name or cr_name in name:
                max_d = cr_bdata.get("max_depth", 1)
                break
        depth_label = ["", "章", "节", "小节", "子小节"][min(max_d, 4)] if max_d <= 4 else f"{max_d}级"
        bar_len = 16
        d = int(bar_len * done / max(total, 1)) if total else 0
        bar = "#" * d + "-" * (bar_len - d)
        marker = " <== current" if name == active else ""
        print(f"  [{bar}] {done:2d}/{total:<3d}  {name}  ({total_secs}{depth_label}){marker}")

    print()


def fuzzy_switch(partial: str) -> str | None:
    """Switch active book by fuzzy-matching partial name. Returns matched name or None."""
    books = _load_book_map()
    if not books:
        print("No registered books.")
        return None

    partial_lower = partial.strip().lower()
    matches = []

    for name in books:
        name_lower = name.replace(" ", "").lower()
        partial_clean = partial_lower.replace(" ", "")
        # Exact match first
        if partial_clean == name_lower:
            matches = [name]
            break
        # Substring match
        if partial_clean in name_lower or name_lower[:4] in partial_clean:
            matches.append(name)

    if len(matches) == 1:
        set_active_book(matches[0])
        print(f"Switched to: {matches[0]}")
        return matches[0]
    elif len(matches) > 1:
        print(f"Multiple matches, be more specific:")
        for m in matches:
            print(f"  - {m}")
        return None
    else:
        print(f"No book matching \"{partial}\". Registered books:")
        for name in books:
            print(f"  - {name}")
        return None


def set_active_book(name: str):
    """Write active_book.json."""
    os.makedirs(os.path.dirname(ACTIVE_BOOK_FILE), exist_ok=True)
    with open(ACTIVE_BOOK_FILE, "w", encoding="utf-8") as f:
        json.dump({"name": name, "set_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)


# ── CLI ──────────────────────────────────────────────────

def main():
    _setup_encoding()
    parser = argparse.ArgumentParser(description="Learning Progress Tracker v2")
    parser.add_argument("--show", "-s", nargs="?", const=True, metavar="BOOK",
                       help="Show progress for active book (or specified book)")
    parser.add_argument("--detail", "-d", action="store_true",
                       help="Show per-section detail with timestamps")
    parser.add_argument("--mark", "-m", nargs=2, metavar=("ID", "STATUS"),
                       help="Mark section (e.g. '7.2 done', '7.2 learning')")
    parser.add_argument("--next", "-n", nargs="?", const=True, metavar="BOOK",
                       help="Show next chapter/section to learn")
    parser.add_argument("--init", "-i", nargs=2, metavar=("BOOK", "ORDER"),
                       help="Init learning order (e.g. 'MyBook 1,2,3,11,7')")
    parser.add_argument("--touch", "-t", nargs="?", const=True, metavar="BOOK",
                       help="Update last_session timestamp")
    parser.add_argument("--list", "-l", action="store_true",
                       help="List all registered books with progress")
    parser.add_argument("--switch", "-w", type=str, metavar="PARTIAL_NAME",
                       help="Switch active book by fuzzy name match")
    args = parser.parse_args()

    if args.init:
        book, order = args.init
        set_chapter_order(book, order)
        return 0

    if args.mark:
        section_id, status = args.mark
        book = get_active_book()
        if not book:
            print("No active book. Set one with --init first.")
            return 1
        if status not in ("done", "completed", "learning", "pending"):
            print(f"Invalid status: '{status}'. Use: done, completed, learning, pending")
            return 1
        mark_completed(book, section_id, status)
        print(f"Marked {section_id} -> {status} in '{book}'")
        return 0

    if args.next:
        book = args.next if isinstance(args.next, str) else get_active_book()
        if not book:
            print("No active book.")
            return 1
        next_item = get_next(book)
        if next_item:
            print(f"Next: {next_item['title']}")
        else:
            print("All chapters completed!")
        return 0

    if args.show:
        book = args.show if isinstance(args.show, str) else get_active_book()
        if not book:
            print("No active book. Provide a book name or set active book.")
            return 1
        show_progress(book, detail=args.detail)
        return 0

    if args.touch:
        book = args.touch if isinstance(args.touch, str) else get_active_book()
        if book:
            touch_session(book)
            print(f"Session timestamp updated for '{book}'")
        return 0

    if args.list:
        list_books()
        return 0

    if args.switch:
        fuzzy_switch(args.switch)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
