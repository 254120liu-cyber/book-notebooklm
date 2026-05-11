---
name: book-notebooklm
description: Query Google NotebookLM for book content. Use whenever the user asks questions about any book (加密与解密, textbooks, technical books), wants to verify concepts against source material, or mentions NotebookLM in learning context. Handles auth, encoding, multi-region VPN, and retries automatically.
---

# Book NotebookLM Query

Query Google NotebookLM for authoritative book content. This skill wraps the `notebooklm-py` CLI with auto-auth, multi-region VPN support, encoding safety, and error recovery.

---

## CRITICAL RULES — MUST FOLLOW

These rules override any other behavior. Violating them misleads the user.

### R1: NotebookLM First — NEVER answer book questions from memory

When the user asks a question about any book they're studying:

```
用户问书中问题 → 先查 NotebookLM → 读结果 → 再回答
                     ↑
              绝对不能跳过这一步
```

**Why:** Claude's memory is unreliable for specific book content. Page numbers, section numbers, exact definitions, and structural details are often wrong. NotebookLM has the actual book text. Answering from memory = gaslighting the user with plausible-sounding but incorrect information.

**The only exception:** Tool usage questions (e.g., "how do I set a breakpoint in x32dbg", "what shortcut does IDA use for..."). These are operational, not book-content questions.

### R2: Separate source from expansion

Always make it clear which part comes from NotebookLM (the book) and which part is your own addition:

```
书上说（NotebookLM原文）：
  [核心定义/机制]

我的补充/展开：
  [你的解释、举例、联想]
```

This lets the user know what's authoritative vs what's your interpretation.

### R3: If NotebookLM says content isn't indexed, be honest

If NotebookLM indicates the content is not in its source material:

```
"NotebookLM 中暂无此部分内容。以下是基于我自身知识的回答，可能与书中有出入。[标注：未经验证]"
```

Never silently substitute your own knowledge when the book content is unavailable.

### R4: If NotebookLM is unreachable, say so

If auth fails after all retries, or network is down:

```
"NotebookLM 当前不可用（原因：XXX）。是否要我基于自身知识先回答？注意可能与书中内容不一致。"
```

Wait for the user to confirm before answering from memory.

### R5: Cite page numbers

When NotebookLM provides page numbers, always include them in your response. This is critical for the user to cross-reference.

### R6: Follow up if the answer is insufficient

If NotebookLM's first answer is too vague, too short, or misses the point, query again with a more specific or rephrased question BEFORE responding to the user. Don't waste the user's time with an incomplete answer.

### R7: Conversation continuity — never start fresh

Always resume the same NotebookLM conversation. The wrapper handles this automatically — do NOT pass `--new` or start a fresh conversation. NotebookLM builds context across queries in the same conversation, so later answers are more informed and coherent.

**Why:** Restarting the conversation loses all accumulated context about what the user has been learning. This degrades answer quality and wastes the user's uploaded source material's context window.

### R8: Pre-session warmup

When starting a learning session, proactively verify the pipeline is healthy:

```bash
py -3 scripts/nlm_query.py --status   # Check auth + notebook context
py -3 scripts/nlm_query.py --warmup   # Ensure regional cookies are fresh
```

Do this at the start of each session so the user never hits auth failures mid-learning. If status shows "EXPIRED", run `--relogin` silently before the user asks their first question.

### R9: Chapter coverage awareness

NotebookLM may not index the entire book. Always parse its answer for signals like "外部知识", "不在源文件中", or "非源文件正文内容". When detected:
1. Tell the user clearly that this answer is NOT from the book
2. Mark it as `[外部知识，非书中原文]`
3. Remind them which chapters ARE indexed (check `--health` for notebook info)
4. Suggest uploading the relevant chapter PDF if they need authoritative answers

This is critical — users may assume all answers come from the book unless you explicitly flag otherwise.

### R10: Deduplicate queries

If the user repeats a question that was already answered by NotebookLM within the same session, do NOT query NotebookLM again. Instead, reference the previous answer. This avoids redundant API calls and respects rate limits.

Track which questions have been asked in the current conversation context. If unsure whether a question is a repeat, compare it against recent queries before deciding.

### R11: Diagnose before reporting errors

If NotebookLM returns an abnormal response (empty, timeout, abnormally short), run diagnostics before presenting the result to the user:

1. Run `--status` to check if auth is the issue
2. If auth is OK but query failed, wait 3 seconds and retry once
3. If the content genuinely doesn't exist, inform the user honestly
4. If it's a network issue, tell the user and suggest checking VPN

Don't just pass an unexplained error to the user.

### R12: Multi-book readiness

When the user starts studying a new book:
1. Help them create a new NotebookLM notebook
2. Upload the book PDF as a source
3. Record the notebook ID in this SKILL.md's Notebook IDs table
4. Different books may have overlapping questions — always use the correct notebook for each book

The user's current books and their notebook IDs are maintained in the Notebook IDs table below.

---

## When This Skill Triggers

**Must trigger (R1 applies):**
- User asks about content from any book they're studying
- User says "书上怎么说的", "查一下", "NotebookLM", "书里关于..."
- User wants to verify a concept against the book

**Should NOT trigger (R1 does NOT apply):**
- Tool usage questions: "怎么用x32dbg", "IDA快捷键是什么"
- Questions about code the user wrote themselves
- Learning plan / scheduling questions
- Pure reasoning: "为什么这段代码会crash"

---

## Setup (one-click)

**Windows:** Double-click `setup.bat` in the skill directory.
**macOS/Linux:** Run `bash setup.sh` in the skill directory.

The setup script installs all dependencies, authenticates with Google, and verifies everything works.

**Manual setup** (if the script fails):

1. Install Python deps: `pip install notebooklm-py httpx PyPDF2`
2. Install browser: `playwright install chromium`
3. Login: `notebooklm login --browser msedge`
4. Create a notebook at https://notebooklm.google.com and upload your book PDF
5. Set notebook ID: `export NOTEBOOKLM_DEFAULT_NB="your_id"` (or `set` on Windows)

**Verify:** `py -3 scripts/nlm_query.py --health`

---

## Commands

Run from the skill directory. Claude Code resolves `scripts/` relative to where this skill is installed.

| Command | Purpose |
|---------|---------|
| `py -3 scripts/nlm_query.py "question"` | Query book content |
| `nlm_query.py --status` | Check auth status |
| `nlm_query.py --relogin` | Force re-login |
| `nlm_query.py --warmup` | Warm up regional cookies |
| `nlm_query.py --daemon 120` | Background auth refresh (every 120 min) |
| `nlm_query.py --notebook <id> "q"` | Query specific notebook |

Query path:
```
py -3 scripts/nlm_query.py "你的问题"
```

Output is written to `NLM_OUTPUT:<path>` — read it with the Read tool.

---

## Multi-Region VPN Stability

After each login, `warmup_regional_cookies()` visits 7 Google regional domains:
- `.google.com` (US), `.google.co.jp` (Japan), `.google.com.sg` (Singapore)
- `.google.com.tw` (Taiwan), `.google.co.kr` (Korea), `.google.com.hk` (Hong Kong)
- `.google.co.uk` (UK/Europe)

This ensures valid session cookies exist regardless of which VPN node is active.

## Auto-Login

`relogin()` launches Playwright Edge, polls `storage_state.json` for modifications (instead of fixed sleep), and auto-closes when Google OAuth completes. The user doesn't need to do anything.

## Source Patching

`ensure_source_patched()` adds `authuser` parameter to notebooklm-py's RPC calls. Idempotent, auto-called before every query. Required for multi-region Google account routing.

---

## Notebook IDs

Configure via `NOTEBOOKLM_DEFAULT_NB` environment variable, or pass `--notebook <id>` per query.

| Book | Notebook ID | Status |
|------|-------------|--------|
| *(your book here)* | `export NOTEBOOKLM_DEFAULT_NB="..."` | — |

Use short prefix IDs (6+ chars), not the full UUID with dashes.

To add a new book:
1. Create notebook: `py -3.11 -m notebooklm create "Book Name"`
2. Add PDF source via NotebookLM web or CLI
3. Set `NOTEBOOKLM_DEFAULT_NB` or record the ID here

## Limitations

- Large scanned PDFs may have partial OCR coverage in NotebookLM. Use `nlm_pdf_splitter.py` to split into chapter-sized chunks.
- Chrome/Edge v127+ cookie encryption (v20) blocks direct disk extraction. Playwright login is the fallback.
- Google session cookies expire after hours. The wrapper handles this automatically.
- Some regions (e.g., Hong Kong) are blocked by Google for NotebookLM access.
