#!/usr/bin/env python
"""Learning progress tracker — per-book chapter progress + auto-resume.

Data file: ~/.notebooklm/learning_progress.json

CLI:
  nlm_progress.py --show [book]          Show progress for active book
  nlm_progress.py --show --detail        Show per-section detail
  nlm_progress.py --mark "7.2.1" done    Mark section/chapter complete
  nlm_progress.py --next                  Show what to learn next
  nlm_progress.py --init "book" --order "1,2,3,11,7,8"   Set chapter order
"""

import argparse
import json
import os
import sys
import time
from typing import Optional

PROGRESS_FILE = os.path.expanduser(r"~\.notebooklm\learning_progress.json")
BOOK_MAP_FILE = os.path.expanduser(r"~\.notebooklm\book_map.json")
ACTIVE_BOOK_FILE = os.path.expanduser(r"~\.notebooklm\profiles\default\active_book.json")


def _s(spec: str) -> dict:
    """Parse section spec like '7.1-7.3' or '1' into {'7.1': 'pending', ...}."""
    parts = spec.split(",")
    result = {}
    for p in parts:
        p = p.strip()
        if "-" in p:
            prefix_parts = p.split(".")
            if len(prefix_parts) >= 2:
                prefix = prefix_parts[0]
                rest = prefix_parts[1]
                if "-" in rest:
                    start_s, end_s = rest.split("-")
                    for i in range(int(start_s), int(end_s) + 1):
                        result[f"{prefix}.{i}"] = "pending"
                else:
                    result[p] = "pending"
            else:
                result[p] = "pending"
        else:
            result[p] = "pending"
    return result


# ── Default chapter orders for known books ─────────────────
# These are generated based on content dependency analysis.
DEFAULT_ORDERS = {
    "加密与解密第四版": {
        "chapter_order": [
            {"ch": 1,  "title": "基础知识",              "sections": _s("1.1-1.3")},
            {"ch": 2,  "title": "动态分析技术",           "sections": _s("2.1-2.5")},
            {"ch": 3,  "title": "静态分析技术",           "sections": _s("3.1-3.4")},
            {"ch": 11, "title": "PE文件格式 (提前)",      "sections": _s("11.1-11.12")},
            {"ch": 7,  "title": "Windows内核基础",        "sections": _s("7.1-7.3")},
            {"ch": 8,  "title": "结构化异常处理SEH",      "sections": _s("8.1-8.3")},
            {"ch": 9,  "title": "Win32调试API",          "sections": _s("9.1-9.7")},
            {"ch": 12, "title": "DLL注入技术",            "sections": _s("12.1-12.5")},
            {"ch": 13, "title": "API Hook技术",           "sections": _s("13.1-13.4")},
            {"ch": 10, "title": "VT虚拟化技术",           "sections": _s("10.1-10.4")},
            {"ch": 14, "title": "二进制漏洞分析",         "sections": _s("14.1-14.4")},
            {"ch": 16, "title": "脱壳技术",               "sections": _s("16.1-16.6")},
            {"ch": 18, "title": "反跟踪技术",             "sections": _s("18.1-18.3")},
            {"ch": 4,  "title": "逆向分析技术 (补学)",     "sections": _s("4.1-4.5")},
            {"ch": 5,  "title": "演示版保护技术 (补学)",   "sections": _s("5.1-5.4")},
            {"ch": 6,  "title": "加密算法 (补学)",        "sections": _s("6.1-6.6")},
            {"ch": 15, "title": "软件保护技术",           "sections": _s("15.1-15.4")},
            {"ch": 17, "title": "外壳编写基础",           "sections": _s("17.1-17.4")},
            {"ch": 19, "title": "虚拟化保护技术",         "sections": _s("19.1-19.4")},
            {"ch": 20, "title": "VMProtect深度分析",      "sections": _s("20.1-20.4")},
            {"ch": 21, "title": "加密与解密实战",         "sections": _s("21.1-21.4")},
            {"ch": 22, "title": "电子取证技术 (选读)",     "sections": _s("22.1-22.4")},
            {"ch": 23, "title": "移动平台安全 (选读)",     "sections": _s("23.1-23.4")},
            {"ch": 24, "title": "其他",                  "sections": _s("24")},
            {"ch": 25, "title": "附录",                  "sections": _s("25")},
        ],
        "reasoning": (
            "按内核安全路线图重排: 地基(1-3章)→PE(11章)→内核(7-9章)→实战(12-14,16章)→"
            "高阶(10章VT)→对抗(18章)→补学用户态逆向(4-6章)→进阶保护与脱壳→专题"
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
    return progress[book_name]


def _init_book(book_name: str) -> dict:
    """Initialize a new book entry with default order if available."""
    entry = {
        "chapter_order": [],
        "created_at": time.strftime("%Y-%m-%d"),
        "last_session": time.strftime("%Y-%m-%d"),
        "total_sessions": 0,
    }
    # Try default order
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            entry["chapter_order"] = data["chapter_order"]
            entry["order_reasoning"] = data.get("reasoning", "")
            break
    return entry


def mark_completed(book_name: str, section_id: str, status: str = "done"):
    """Mark a section or chapter as completed."""
    progress = load_progress()
    if book_name not in progress:
        progress[book_name] = _init_book(book_name)

    bp = progress[book_name]
    bp["last_session"] = time.strftime("%Y-%m-%d")

    # section_id can be "7" (chapter), "7.1" (section), or "7.1.1" (sub-section)
    for ch_entry in bp.get("chapter_order", []):
        ch_str = str(ch_entry["ch"])
        if section_id == ch_str:
            # Mark entire chapter
            ch_entry["status"] = status
            for k in ch_entry.get("sections", {}):
                ch_entry["sections"][k] = status
            break
        elif section_id.startswith(ch_str + "."):
            # Mark a section within a chapter
            sections = ch_entry.get("sections", {})
            if section_id in sections:
                sections[section_id] = status
            # If all sections done, mark chapter done
            if all(v in ("done", "completed") for v in sections.values()):
                ch_entry["status"] = "completed"
            elif any(v == "done" for v in sections.values()):
                ch_entry["status"] = "in_progress"

    # Update session count
    bp["total_sessions"] = bp.get("total_sessions", 0) + 1

    save_progress(progress)


def get_next(book_name: str) -> Optional[dict]:
    """Return the next chapter/section to learn."""
    bp = get_book_progress(book_name)
    bp["last_session"] = time.strftime("%Y-%m-%d")
    save_progress({k: v for k, v in load_progress().items()})
    progress = load_progress()
    if book_name in progress:
        progress[book_name] = bp
    save_progress(progress)

    for ch_entry in bp.get("chapter_order", []):
        if ch_entry.get("status") in ("pending", None):
            return {"type": "chapter", "ch": ch_entry["ch"], "title": ch_entry["title"]}
        sections = ch_entry.get("sections", {})
        for sec_id, sec_status in sections.items():
            if sec_status == "pending":
                return {"type": "section", "id": sec_id, "chapter": ch_entry["ch"],
                        "title": f"{ch_entry['title']} - {sec_id}"}
    return None


def show_progress(book_name: str, detail: bool = False):
    """Print learning progress for a book."""
    bp = get_book_progress(book_name)
    order = bp.get("chapter_order", [])
    if not order:
        print(f"No chapter order set for '{book_name}'.")
        print(f"Use --init to set up learning order.")
        return

    print(f"Book: {book_name}")
    print(f"Created: {bp.get('created_at', '?')}  |  "
          f"Last session: {bp.get('last_session', '?')}  |  "
          f"Sessions: {bp.get('total_sessions', 0)}")
    print(f"Reasoning: {bp.get('order_reasoning', '(none)')}")
    print()

    completed = sum(1 for c in order if c.get("status") in ("completed", "done"))
    in_progress = sum(1 for c in order if c.get("status") == "in_progress")
    pending = sum(1 for c in order if c.get("status") in ("pending", None))
    total = len(order)

    bar_len = 30
    done_bar = int(bar_len * completed / max(total, 1))
    prog_bar = int(bar_len * (completed + in_progress) / max(total, 1))
    bar = "#" * done_bar + "=" * (prog_bar - done_bar) + "-" * (bar_len - prog_bar)
    print(f"Overall: [{bar}] {completed}/{total} ({100*completed//max(total,1)}%)")
    print()

    for ch_entry in order:
        ch = ch_entry["ch"]
        title = ch_entry["title"]
        status = ch_entry.get("status", "pending") or "pending"
        icon = "[OK]" if status in ("completed", "done") else ("[>>]" if status == "in_progress" else "[  ]")
        print(f"  {icon} 第{ch:2d}章 {title}")

        if detail and ch_entry.get("sections"):
            for sec_id, sec_status in sorted(ch_entry["sections"].items()):
                s_icon = "  [OK]" if sec_status in ("done", "completed") else "  [  ]"
                print(f"       {s_icon} {sec_id}")

    print()
    next_item = get_next(book_name)
    if next_item:
        if next_item["type"] == "chapter":
            print(f"Next: 第{next_item['ch']}章 {next_item['title']}")
        else:
            print(f"Next: {next_item['id']} ({next_item['title']})")
    else:
        print("All chapters completed!")


def set_chapter_order(book_name: str, order_spec: str):
    """Set chapter order from comma-separated spec like '1,2,3,11,7,8'."""
    progress = load_progress()
    if book_name not in progress:
        progress[book_name] = _init_book(book_name)

    ch_nums = [int(x.strip()) for x in order_spec.split(",")]

    # Build title lookup: prefer DEFAULT_ORDERS, then chapter_routes
    title_map = {}
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            for entry in data["chapter_order"]:
                title_map[entry["ch"]] = entry["title"]
            break
    if not title_map:
        chapters = _load_chapter_boundaries()
        for k, v in chapters.items():
            title_map[int(k)] = v.get("title", f"Chapter {k}")

    # Build section map from default order
    default_sections = {}
    for key, data in DEFAULT_ORDERS.items():
        if key in book_name or book_name in key:
            for entry in data["chapter_order"]:
                default_sections[entry["ch"]] = entry.get("sections", {}).copy()
            break

    new_order = []
    for ch_num in ch_nums:
        title = title_map.get(ch_num, f"Chapter {ch_num}")
        sections = default_sections.get(ch_num, {f"{ch_num}.1": "pending"})
        new_order.append({"ch": ch_num, "title": title, "sections": sections})

    progress[book_name]["chapter_order"] = new_order
    save_progress(progress)
    print(f"Set {len(new_order)} chapters in order: {', '.join(str(c['ch']) for c in new_order)}")


def _load_chapter_boundaries() -> dict:
    routes_file = os.path.expanduser(r"~\.notebooklm\chapter_routes.json")
    if os.path.exists(routes_file):
        try:
            with open(routes_file, "r", encoding="utf-8") as f:
                return json.load(f).get("chapter_boundaries", {})
        except Exception:
            pass
    return {}


def touch_session(book_name: str):
    """Update last_session timestamp without incrementing session count."""
    progress = load_progress()
    if book_name in progress:
        progress[book_name]["last_session"] = time.strftime("%Y-%m-%d")
        save_progress(progress)


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Learning Progress Tracker")
    parser.add_argument("--show", "-s", nargs="?", const=True, metavar="BOOK",
                       help="Show progress for active book (or specified book)")
    parser.add_argument("--detail", "-d", action="store_true", help="Show per-section detail")
    parser.add_argument("--mark", "-m", nargs=2, metavar=("ID", "STATUS"),
                       help="Mark section (e.g. '7.2.1 done')")
    parser.add_argument("--next", "-n", nargs="?", const=True, metavar="BOOK",
                       help="Show next chapter/section to learn")
    parser.add_argument("--init", "-i", nargs=2, metavar=("BOOK", "ORDER"),
                       help="Init learning order (e.g. 'MyBook 1,2,3,11,7')")
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
        mark_completed(book, section_id, status)
        print(f"Marked {section_id} → {status} in '{book}'")
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
