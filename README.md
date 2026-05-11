# book-notebooklm

A Claude Code skill for reliable, hands-free querying of book content via Google NotebookLM. Designed for accelerated AI-assisted learning — ask questions about any book and get answers backed by the actual source material.

## Why

Claude's memory is unreliable for specific book content. Page numbers, definitions, section details — it gets these wrong often enough to mislead. NotebookLM has the actual book text. This skill bridges them: Claude queries NotebookLM as the authoritative source, then explains and expands on the verified answer.

## Features

- **Zero-touch auth** — Auto-detects expired sessions and re-logs in silently
- **Multi-VPN stability** — Warms up cookies across 7 Google regional domains (US/JP/SG/TW/KR/HK/UK). Any VPN node works
- **Encoding-safe** — UTF-8 file output bypasses Windows GBK terminal corruption
- **Smart caching** — Cross-session Q&A cache with 7-day TTL, avoids redundant queries
- **Question optimizer** — Rewrites vague questions for better NotebookLM retrieval
- **Health dashboard** — One command shows auth, notebook, cache, and connectivity status
- **PDF splitter** — Break large books into chapter groups for full NotebookLM coverage
- **Background daemon** — Keeps auth fresh between learning sessions

## Prerequisites

- Python 3.10+
- A Google account with NotebookLM access
- A VPN (for users in regions where Google is blocked)

## Quick Install

```bash
# 1. Clone into Claude Code skills directory
git clone https://github.com/YOUR_USERNAME/book-notebooklm.git ~/.claude/skills/book-notebooklm

# 2. One-click setup (installs all dependencies + guides you through auth)
#    Windows: double-click setup.bat in the skill folder
#    macOS/Linux: bash ~/.claude/skills/book-notebooklm/setup.sh

# 3. Create a notebook at https://notebooklm.google.com and upload your book PDF

# 4. Set your notebook ID
#    Windows: set NOTEBOOKLM_DEFAULT_NB=abc123
#    macOS/Linux: export NOTEBOOKLM_DEFAULT_NB=abc123

# 5. Done! Ask Claude anything about your book.
```

Or install via Claude Code skill registry: `npx skills add <github-user>/book-notebooklm`

## Commands

| Command | Purpose |
|---------|---------|
| `nlm_query.py "question"` | Query book content (auto-optimized) |
| `nlm_query.py --health` | Full health dashboard |
| `nlm_query.py --health-quick` | Fast health check (no regional scan) |
| `nlm_query.py --status` | Auth status only |
| `nlm_query.py --relogin` | Force re-login + regional warmup |
| `nlm_query.py --warmup` | Warm up regional cookies |
| `nlm_query.py --daemon 120` | Background refresh every 120 min |
| `nlm_query.py --cache-stats` | Cache hit/miss statistics |
| `nlm_query.py --clear-cache` | Purge cached answers |
| `nlm_query.py --no-cache "q"` | Query without cache |
| `nlm_query.py --no-optimize "q"` | Query without question optimization |

### PDF Splitter

```bash
# Auto-split into ~100-page chunks
py -3 nlm_pdf_splitter.py book.pdf

# Use chapter preset
py -3 nlm_pdf_splitter.py book.pdf --preset "加密与解密第四版"

# Custom page ranges
py -3 nlm_pdf_splitter.py book.pdf --ranges "1-59,60-119,120-200"

# List available presets
py -3 nlm_pdf_splitter.py --list-presets
```

## How It Works

```
User Question
    │
    ▼
[Question Optimizer]  "PE是什么" → "根据《加密与解密》第11章，PE文件的结构定义..."
    │
    ▼
[Cache Check]  Have we answered this before? → Return cached answer (instant)
    │
    ▼
[Auth Check]  Session valid? → No → [Auto Relogin] → [Regional Warmup: 7 domains]
    │
    ▼
[Query NotebookLM]  Send optimized question via notebooklm-py CLI
    │
    ▼
[Cache Result]  Store answer for future reuse (7-day TTL)
    │
    ▼
[Output]  Write UTF-8 answer to file → Claude reads with Read tool
```

### Regional Cookie Warmup

When you switch VPN nodes, Google routes requests through different regional domains. Without cookies for each region, auth fails. The warmup step visits all 7 major Google regional domains after each login, establishing session cookies everywhere:

```
accounts.google.com      (US/Global)
accounts.google.co.jp    (Japan)
accounts.google.com.sg   (Singapore)
accounts.google.com.tw   (Taiwan)
accounts.google.co.kr    (Korea)
accounts.google.com.hk   (Hong Kong)
accounts.google.co.uk    (UK/Europe)
```

## Verified VPN Nodes

| Region | Status | Notes |
|--------|--------|-------|
| 🇺🇸 US | ✅ | Full support |
| 🇯🇵 Japan | ✅ | Full support |
| 🇸🇬 Singapore | ✅ | Full support |
| 🇹🇼 Taiwan | ✅ | Full support |
| 🇰🇷 Korea | ✅ | Full support (untested, high confidence) |
| 🇬🇧 UK | ✅ | Full support (untested, high confidence) |
| 🇭🇰 Hong Kong | ❌ | Google blocks NotebookLM (`location=unsupported`) |

## Skill Rules (for Claude)

This skill enforces 12 rules when the user asks book-related questions:

1. **NotebookLM First** — Never answer book questions from memory
2. **Separate Source** — Distinguish "what the book says" from "my explanation"
3. **Honesty** — Flag when content isn't indexed in NotebookLM
4. **Transparency** — When NotebookLM is unreachable, ask before using own knowledge
5. **Citations** — Always include page numbers from NotebookLM
6. **Follow-up** — If answer is insufficient, query again before responding
7. **Conversation Continuity** — Never start fresh conversations
8. **Pre-session Warmup** — Verify auth health before learning sessions
9. **Chapter Awareness** — Know which chapters are indexed (1-4) vs not
10. **Deduplication** — Don't re-query NotebookLM for the same question
11. **Diagnose First** — Before reporting errors, check auth and retry
12. **Multi-book Support** — Track notebook IDs for multiple books

## Directory Structure

```
book-notebooklm/
├── SKILL.md                # Skill instructions for Claude
├── README.md               # This file
├── LICENSE                 # MIT
└── scripts/
    ├── nlm_query.py        # Core query wrapper
    └── nlm_pdf_splitter.py # PDF chapter splitter
```

## Limitations

- **Chrome v127+ encryption (v20)**: Direct cookie extraction from browser disk is blocked. Playwright-based login is the fallback (opens Edge briefly, auto-closes in 3-5 seconds).
- **NotebookLM OCR coverage**: Large PDFs (>200 pages) may not be fully indexed. Use the PDF splitter to break into chapter-sized chunks.
- **Google session expiry**: Cookies expire after hours. The auto-relogin and daemon mode handle this transparently.
- **Region blocking**: Some regions (e.g., Hong Kong) are blocked by Google for NotebookLM access.

## License

MIT — see [LICENSE](LICENSE)
