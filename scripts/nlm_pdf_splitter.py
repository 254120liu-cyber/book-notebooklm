#!/usr/bin/env python
"""Split a PDF into chapter-based segments for NotebookLM upload.

NotebookLM OCR is limited — large PDFs only get partial indexing. Splitting
the book into chapter groups ensures full coverage across all chapters.

Usage:
  py -3.11 nlm_pdf_splitter.py <pdf_path>                    # Auto-split into ~100-page chunks
  py -3.11 nlm_pdf_splitter.py <pdf_path> --preset book_name # Use preset chapter ranges
  py -3.11 nlm_pdf_splitter.py <pdf_path> --ranges "1-50,51-100,101-200"  # Custom ranges
  py -3.11 nlm_pdf_splitter.py <pdf_path> --list-presets     # Show available chapter presets

Output: <pdf_dir>/split/ directory with individual PDFs ready for NotebookLM upload.
"""

import argparse
import os
import sys

# ── Chapter presets ────────────────────────────────────────

# Pages are 1-indexed (as they appear in the book)
# Each entry maps the book's logical chapter ranges
PRESETS = {
    "加密与解密第四版": {
        "description": "Encryption & Decryption 4th Ed — chapter groups for NotebookLM",
        "groups": [
            # Each group ≤ ~100 pages for reliable OCR indexing
            {
                "name": "01-第1章_基础知识",
                "pages": (1, 59),  # Chapter 1: pages 1-59
                "description": "虚拟内存、WOW64、Windows API、消息机制",
            },
            {
                "name": "02-第2章_动态分析技术",
                "pages": (60, 119),  # Chapter 2: pages 60-119 (approximate)
                "description": "OllyDbg、x64dbg、WinDbg、断点技术、跟踪分析",
            },
            {
                "name": "03-第3章_静态分析技术",
                "pages": (60, 94),  # Chapter 3 (overlaps ch2 — needs actual page refs)
                "description": "IDA Pro、反汇编引擎、十六进制工具、静态分析实战",
            },
            {
                "name": "04-第4章_逆向分析技术",
                "pages": (95, 170),  # Chapter 4
                "description": "函数识别、控制语句、数据结构、调用约定",
            },
            # Chapter ranges below need verification against the actual book
            {
                "name": "05-第5-6章_加密算法",
                "pages": (171, 230),
                "description": "加密算法识别与特征 (待验证页码)",
            },
            {
                "name": "06-第7-8章_内核与SEH",
                "pages": (231, 310),
                "description": "Windows内核基础、结构化异常处理 (待验证页码)",
            },
            {
                "name": "07-第11章_PE文件格式",
                "pages": (404, 460),
                "description": "PE结构、导入表、导出表、重定位、绑定导入 (待验证页码)",
            },
            {
                "name": "08-第12-13章_注入与Hook",
                "pages": (461, 530),
                "description": "用户态/内核态注入、IAT Hook、Inline Hook (待验证页码)",
            },
        ],
        "note": "Chapter 3-4 range above is approximate. Adjust --ranges after checking actual book pages."
    }
}


def split_pdf(input_path: str, groups: list, output_dir: str):
    """Split PDF into groups and save as separate PDFs."""
    try:
        from PyPDF2 import PdfReader, PdfWriter
    except ImportError:
        print("ERROR: PyPDF2 is required. Install: py -3.11 -m pip install PyPDF2", file=sys.stderr)
        return False

    os.makedirs(output_dir, exist_ok=True)

    reader = PdfReader(input_path)
    total_pages = len(reader.pages)
    print(f"PDF: {input_path}")
    print(f"Total pages: {total_pages}")
    print(f"Output: {output_dir}/")
    print()

    created = []
    for group in groups:
        start = max(1, group["pages"][0])
        end = min(total_pages, group["pages"][1])

        if start > total_pages:
            print(f"  SKIP {group['name']}: start page {start} > {total_pages} total pages")
            continue

        writer = PdfWriter()
        for i in range(start - 1, end):  # 0-indexed
            writer.add_page(reader.pages[i])

        filename = f"{group['name']}.pdf"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "wb") as f:
            writer.write(f)

        size_kb = os.path.getsize(filepath) / 1024
        created.append((filepath, size_kb, group.get("description", "")))
        print(f"  OK {filename}  (pages {start}-{end}, {size_kb:.0f} KB)")
        if group.get("description"):
            print(f"     {group['description']}")

    print(f"\nCreated {len(created)} PDF files.")
    print(f"\nNext steps:")
    print(f"  1. Upload each PDF to NotebookLM as a source")
    print(f"  2. Label each source with the chapter name")
    print(f"  3. After upload, verify OCR by asking chapter-specific questions")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Split PDF into chapter groups for NotebookLM"
    )
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--preset", "-p", default=None,
                       help=f"Use a chapter preset ({', '.join(PRESETS.keys())})")
    parser.add_argument("--ranges", "-r", default=None,
                       help="Custom page ranges: '1-50,51-100,101-200'")
    parser.add_argument("--output-dir", "-o", default=None,
                       help="Output directory (default: <pdf_dir>/split/)")
    parser.add_argument("--list-presets", action="store_true",
                       help="Show available presets and exit")
    args = parser.parse_args()

    if args.list_presets:
        for name, preset in PRESETS.items():
            print(f"\n{name}:")
            print(f"  {preset['description']}")
            for g in preset["groups"]:
                print(f"    {g['name']}: pages {g['pages'][0]}-{g['pages'][1]}")
            if preset.get("note"):
                print(f"  NOTE: {preset['note']}")
        return 0

    if not os.path.exists(args.pdf):
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    # Determine output directory
    pdf_dir = os.path.dirname(os.path.abspath(args.pdf))
    output_dir = args.output_dir or os.path.join(pdf_dir, "split")

    # Build groups
    groups = []

    if args.ranges:
        names = [f"pages_{r.replace('-', '_')}" for r in args.ranges.split(",")]
        for i, (name, rng) in enumerate(zip(names, args.ranges.split(","))):
            parts = rng.split("-")
            groups.append({
                "name": name,
                "pages": (int(parts[0]), int(parts[1])),
                "description": "",
            })

    elif args.preset:
        preset = PRESETS.get(args.preset)
        if not preset:
            print(f"ERROR: Unknown preset '{args.preset}'. Available: {', '.join(PRESETS.keys())}", file=sys.stderr)
            return 1
        groups = preset["groups"]

    else:
        # Auto-split: ~100 pages per group
        from PyPDF2 import PdfReader
        reader = PdfReader(args.pdf)
        total = len(reader.pages)
        chunk_size = 100
        for i, start in enumerate(range(1, total + 1, chunk_size)):
            end = min(start + chunk_size - 1, total)
            groups.append({
                "name": f"part_{i+1:02d}_pages_{start}-{end}",
                "pages": (start, end),
                "description": "",
            })

    if not groups:
        print("ERROR: No groups defined. Use --ranges, --preset, or auto-split.", file=sys.stderr)
        return 1

    ok = split_pdf(args.pdf, groups, output_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
