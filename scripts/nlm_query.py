#!/usr/bin/env python
"""NotebookLM Query Wrapper v4 — chapter routing + auto-retry + keyword learning.

Usage:
  nlm_query.py "question"         Query book content
  nlm_query.py --health           Full health dashboard
  nlm_query.py --health-quick     Fast health (no regional scan)
  nlm_query.py --status           Auth check only
  nlm_query.py --relogin          Force re-login + warmup
  nlm_query.py --warmup           Regional cookie warmup
  nlm_query.py --daemon 120       Background refresh (every 120 min)
  nlm_query.py --cache-stats      Cache statistics
  nlm_query.py --clear-cache      Purge all cached answers
"""

import argparse
import hashlib
import json
import os
import re

import shutil
import site
import subprocess
import sys
import tempfile
import time

# ── Configuration ──────────────────────────────────────────

# Discover notebooklm executable
_NLM_EXE = shutil.which("notebooklm")
if not _NLM_EXE:
    # Fallback: search common Python install locations on Windows
    _candidates = []
    for ver in ("313", "312", "311", "310"):
        _candidates.append(os.path.expanduser(
            rf"~\AppData\Local\Programs\Python\Python{ver}\Scripts\notebooklm.exe"
        ))
    for c in _candidates:
        if os.path.exists(c):
            _NLM_EXE = c
            break

AUTHUSER = os.environ.get("NOTEBOOKLM_AUTHUSER", "0")

BOOK_MAP_FILE = os.path.expanduser(r"~\.notebooklm\book_map.json")
CHAPTER_ROUTES_FILE = os.path.expanduser(r"~\.notebooklm\chapter_routes.json")
STATE_DIR = os.path.expanduser(r"~\.notebooklm\profiles\default")


def _load_book_map() -> dict:
    if os.path.exists(BOOK_MAP_FILE):
        try:
            with open(BOOK_MAP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _resolve_default_notebook() -> str:
    """Determine which notebook to use, in priority order:
    1. book_map.json (authoritative — updated by nlm_add_book.py)
    2. NOTEBOOKLM_DEFAULT_NB environment variable (with staleness warning)
    3. Hardcoded fallback
    """
    # book_map is the source of truth (managed by add_book pipeline)
    book_map = _load_book_map()
    if book_map:
        first = next(iter(book_map.values()))
        map_nb = first.get("notebook_id")
        if map_nb:
            # Warn if env var points to a stale notebook
            env_val = os.environ.get("NOTEBOOKLM_DEFAULT_NB")
            if env_val and env_val != map_nb:
                print(f"[nlm] ⚠ NOTEBOOKLM_DEFAULT_NB={env_val} is stale, "
                      f"using book_map: {map_nb}", file=sys.stderr)
            return map_nb

    # Fallback: env var (may be set manually by user)
    env_val = os.environ.get("NOTEBOOKLM_DEFAULT_NB")
    if env_val:
        return env_val

    return "51429604"


DEFAULT_NOTEBOOK = _resolve_default_notebook()
STORAGE_STATE = os.path.expanduser(r"~\.notebooklm\profiles\default\storage_state.json")
STATE_FILE = os.path.join(STATE_DIR, "nlm_state.json")
CACHE_DIR = os.path.join(STATE_DIR, "cache")

PROACTIVE_REFRESH_MINUTES = 90
CACHE_TTL_DAYS = 7
MAX_CACHE_ENTRIES = 200

REGIONAL_GOOGLE_DOMAINS = [
    "https://accounts.google.com",
    "https://accounts.google.co.jp",
    "https://accounts.google.com.sg",
    "https://accounts.google.com.tw",
    "https://accounts.google.co.kr",
    "https://accounts.google.com.hk",
    "https://accounts.google.co.uk",
]

# Centralized auth-failure patterns (avoids scattered string matching)
AUTH_FAILURE_SUBSTRINGS = [
    "expired", "invalid", "status code 5",
    "Authentication", "redirected", "Not authenticated",
]
AUTH_SUCCESS_SUBSTRING = "Authentication saved"

# ── Module-level caches (invalidate on state changes) ─────

_source_patched: bool | None = None
_auth_cache: tuple[float, bool] | None = None
_AUTH_CACHE_TTL = 30  # seconds


# ── State Management ───────────────────────────────────────

def _load_state() -> dict:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeError):
            pass
    return {
        "last_query_time": 0, "last_auth_time": 0,
        "total_queries": 0, "cache_hits": 0, "cache_misses": 0,
        "version": 3,
    }


def _save_state(state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _record_query(state: dict):
    """Increment query counter + timestamp (mutates state dict in place)."""
    state["total_queries"] += 1
    state["last_query_time"] = time.time()


def _count_cache_files() -> int:
    try:
        return len([e for e in os.scandir(CACHE_DIR)
                     if e.is_file() and e.name.endswith(".json")])
    except OSError:
        return 0


# ── Helpers ────────────────────────────────────────────────

def _env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["NOTEBOOKLM_AUTHUSER"] = AUTHUSER
    return env


def _run(args: list, timeout: int = 30):
    """Run notebooklm CLI command with explicit argument list (no shell injection)."""
    try:
        r = subprocess.run(
            [_NLM_EXE] + args,
            capture_output=True,
            env=_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT", -1
    except (FileNotFoundError, PermissionError, OSError) as e:
        return "", str(e), -2

    stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
    stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    return stdout, stderr, r.returncode


def _time_ago(ts: float) -> str:
    if ts == 0:
        return "never"
    diff = time.time() - ts
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


def _cache_key(question: str, notebook: str) -> str:
    raw = f"{notebook}:{question.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_auth_error(text: str) -> bool:
    """Check if the given output text indicates an authentication failure."""
    lower = text.lower()
    return any(s.lower() in lower for s in AUTH_FAILURE_SUBSTRINGS)


# ── Source Patching ────────────────────────────────────────

def _find_core_py() -> str | None:
    """Locate notebooklm _core.py across site-packages and sys.path."""
    search_paths = []
    try:
        search_paths.extend(site.getsitepackages())
    except Exception:
        pass
    search_paths.extend(sys.path)

    for sp in search_paths:
        cand = os.path.join(sp, "notebooklm", "_core.py")
        try:
            with open(cand, "r", encoding="utf-8") as f:
                f.read()  # Verify file is readable
            return cand
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return None


def ensure_source_patched() -> bool:
    global _source_patched
    if _source_patched:
        return True

    core_py = _find_core_py()
    if not core_py:
        return False

    with open(core_py, "r", encoding="utf-8") as f:
        content = f.read()

    if '"authuser"' in content:
        _source_patched = True
        return True

    if "import os\n" not in content:
        content = content.replace("import logging\n", "import logging\nimport os\n", 1)

    old = '"rt": "c",\n        }'
    new = '"authuser": os.environ.get("NOTEBOOKLM_AUTHUSER", "0"),\n            "rt": "c",\n        }'
    if old in content:
        content = content.replace(old, new, 1)
        with open(core_py, "w", encoding="utf-8") as f:
            f.write(content)
        _source_patched = True
        return True

    return False


# ── Multi-Region Cookie Warmup ─────────────────────────────

def warmup_regional_cookies() -> tuple[int, int]:
    """Visit regional Google domains. Returns (warmed, total)."""
    if not os.path.exists(STORAGE_STATE):
        return 0, len(REGIONAL_GOOGLE_DOMAINS)

    try:
        import httpx
    except ImportError:
        return 0, len(REGIONAL_GOOGLE_DOMAINS)

    with open(STORAGE_STATE, "r", encoding="utf-8") as f:
        state = json.load(f)

    jar = httpx.Cookies()
    for c in state.get("cookies", []):
        try:
            jar.set(c["name"], c["value"],
                    domain=c.get("domain", "").lstrip("."),
                    path=c.get("path", "/"))
        except Exception:
            pass

    warmed = 0
    for url in REGIONAL_GOOGLE_DOMAINS:
        try:
            resp = httpx.get(url, cookies=jar, follow_redirects=True, timeout=15)
            if resp.status_code < 500:
                warmed += 1
        except Exception:
            pass
    return warmed, len(REGIONAL_GOOGLE_DOMAINS)


# ── Cache ──────────────────────────────────────────────────

def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def cache_get(question: str, notebook: str) -> str | None:
    key = _cache_key(question, notebook)
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None

    age_days = (time.time() - entry.get("time", 0)) / 86400
    if age_days > CACHE_TTL_DAYS:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    return entry.get("answer")


def cache_set(question: str, notebook: str, answer: str):
    key = _cache_key(question, notebook)
    path = _cache_path(key)
    entry = {"question": question, "notebook": notebook, "answer": answer, "time": time.time()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)

    # Evict oldest entries if over limit (uses scandir for fewer syscalls)
    try:
        with os.scandir(CACHE_DIR) as entries:
            files = sorted(
                (e for e in entries if e.is_file() and e.name.endswith(".json")),
                key=lambda e: e.stat().st_mtime,
            )
        while len(files) > MAX_CACHE_ENTRIES:
            oldest = files.pop(0)
            try:
                os.unlink(oldest.path)
            except OSError:
                pass
    except OSError:
        pass


def cache_clear():
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
        os.makedirs(CACHE_DIR, exist_ok=True)


# ── Chapter Routes (auto-learning) ────────────────────────

def _load_chapter_routes() -> dict:
    if os.path.exists(CHAPTER_ROUTES_FILE):
        try:
            with open(CHAPTER_ROUTES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"keywords": {}, "chapter_boundaries": {}}


def _save_chapter_routes(r: dict):
    os.makedirs(os.path.dirname(CHAPTER_ROUTES_FILE), exist_ok=True)
    with open(CHAPTER_ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)


def _route_question(question: str) -> tuple[str | None, str | None]:
    """Match question keywords to chapter/page range. Returns (chapter_hint, pages)."""
    routes = _load_chapter_routes()
    keywords = routes.get("keywords", {})
    boundaries = routes.get("chapter_boundaries", {})

    q_lower = question.lower()

    # Exact match first (longer keyword = higher priority)
    sorted_kws = sorted(keywords.items(), key=lambda x: -len(x[0]))
    for kw, info in sorted_kws:
        if kw.lower() in q_lower:
            ch = info.get("chapter")
            pages = info.get("pages", "")
            if ch and str(ch) in boundaries:
                b = boundaries[str(ch)]
                pages = f"{b['start']}-{b['end']}"
            # Increment hit counter
            info["hits"] = info.get("hits", 0) + 1
            info["last_hit"] = time.strftime("%Y-%m-%d")
            _save_chapter_routes(routes)
            return f"第{ch}章", pages

    # Fallback: check if any chapter number mentioned in question
    ch_match = re.search(r'第\s*(\d+)\s*章', question)
    if ch_match:
        ch_num = ch_match.group(1)
        if ch_num in boundaries:
            b = boundaries[ch_num]
            return f"第{ch_num}章", f"{b['start']}-{b['end']}"

    return None, None


def _learn_from_result(question: str, answer: str):
    """Extract page numbers from answer and auto-register keywords."""
    if not answer or "ERROR" in answer:
        return

    routes = _load_chapter_routes()
    keywords = routes.get("keywords", {})
    boundaries = routes.get("chapter_boundaries", {})

    # Extract page numbers: "第 432 页" or "第432页"
    pages_found = re.findall(r'第\s*(\d{2,4})\s*页', answer)
    if not pages_found:
        return

    # Determine which chapter
    for page_str in pages_found:
        page = int(page_str)
        matched_ch = None
        for ch_num, b in boundaries.items():
            if b["start"] <= page <= b["end"]:
                matched_ch = int(ch_num)
                break

        if matched_ch is None:
            continue

        # Extract potential keywords from the question
        # Chinese terms (2-4 chars) and English abbreviations
        terms = set()
        for m in re.finditer(r'\b([A-Z]{2,6})\b', question):
            terms.add(m.group(1))
        # Chinese nouns: after removing question words
        clean = re.sub(r'是什么|怎么|什么|如何|为什么|的第|在第|书中|原文|定义|请|查询|查找|关于', '', question)
        for m in re.finditer(r'([一-鿿]{2,4})', clean):
            terms.add(m.group(1))

        for term in terms:
            if term and term not in keywords:
                keywords[term] = {
                    "chapter": matched_ch,
                    "pages": f"{boundaries[str(matched_ch)]['start']}-{boundaries[str(matched_ch)]['end']}",
                    "source": "learned",
                    "hits": 1,
                    "last_hit": time.strftime("%Y-%m-%d"),
                }
            elif term in keywords:
                keywords[term]["hits"] = keywords[term].get("hits", 0) + 1
                keywords[term]["last_hit"] = time.strftime("%Y-%m-%d")

    _save_chapter_routes(routes)


# ── Query Optimization ─────────────────────────────────────

FAIL_SIGNALS = [
    "仅涵盖至", "前 166 页", "不在源文件中",
    "仅涵盖至书中的第", "当前提供的源文件", "仅涵盖了前",
    "当前提供的 PDF 源代码片段",
]


def _is_fail_signal(answer: str) -> bool:
    lower = answer.lower()
    return any(s in answer for s in FAIL_SIGNALS)


def optimize_question(raw: str) -> str:
    """Improve a question for better NotebookLM retrieval with chapter routing."""
    q = raw.strip()

    # Step 1: Try chapter routing
    chapter_hint, pages = _route_question(q)
    if chapter_hint and pages:
        return (
            f"在《加密与解密（第4版）》{chapter_hint}（第{pages}页）中，"
            f"{q}。请提供具体的原文引用和页码。"
        )

    # Step 2: Routing miss — tell user, proceed with full-book search
    if len(q) > 3:
        print(f"[nlm] 术语未收录，全文搜索（命中后将自动收录）", file=sys.stderr)

    has_book_context = any(
        kw in q for kw in ["加密与解密", "第4版", "书中", "本书", "源文件"]
    )

    if len(q) < 15 and not has_book_context:
        return (
            f'关于《加密与解密（第4版）》中的"{q}"，请详细解释其定义、工作机制，'
            f"并提供具体的页码引用。"
        )
    if not has_book_context:
        return (
            f"根据《加密与解密（第4版）》，{q}。"
            f"请提供具体的页码引用和书中原文的关键定义。"
        )
    if "页码" not in q and "页" not in q:
        return f"{q}。请提供具体的页码引用。"
    return q


# ── Auth Management ────────────────────────────────────────

def check_auth() -> bool:
    """Check if authenticated. Result cached for 30 seconds to avoid redundant subprocess calls."""
    global _auth_cache
    now = time.time()
    if _auth_cache and (now - _auth_cache[0]) < _AUTH_CACHE_TTL:
        return _auth_cache[1]

    stdout, stderr, rc = _run(["status"])
    combined = stdout + stderr
    ok = not (rc != 0 or _is_auth_error(combined)) and "Notebook ID" in combined

    _auth_cache = (now, ok)
    return ok


def _get_status_output() -> str:
    """Get raw status output (bypasses cache)."""
    stdout, _, _ = _run(["status"])
    return stdout


def relogin() -> bool:
    """Polling-based non-interactive relogin with regional warmup."""
    global _auth_cache
    print("[nlm] Re-authenticating...", file=sys.stderr)

    init_mtime = os.path.getmtime(STORAGE_STATE) if os.path.exists(STORAGE_STATE) else 0

    env = _env()
    # Pass Windows system proxy to Playwright browser
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if proxy_enable:
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            env["HTTP_PROXY"] = f"http://{proxy_server}"
            env["HTTPS_PROXY"] = f"http://{proxy_server}"
        winreg.CloseKey(key)
    except Exception:
        pass

    p = subprocess.Popen(
        [_NLM_EXE, "login", "--browser", "msedge"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    deadline = time.time() + 60
    poll_interval = 0.3
    login_detected = False

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            mtime = os.path.getmtime(STORAGE_STATE)
        except OSError:
            mtime = 0
        if mtime > init_mtime:
            login_detected = True
            break
        if p.poll() is not None:
            break
        poll_interval = min(poll_interval * 1.15, 3.0)

    try:
        out_b, err_b = p.communicate(input=b"\n", timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        return False

    out = out_b.decode("utf-8", errors="replace") if out_b else ""
    err = err_b.decode("utf-8", errors="replace") if err_b else ""

    success = AUTH_SUCCESS_SUBSTRING in (out + err) or (login_detected and p.returncode == 0)
    if success:
        print("[nlm] Login successful.", file=sys.stderr)
        warmup_regional_cookies()
        state = _load_state()
        state["last_auth_time"] = time.time()
        _save_state(state)
        _auth_cache = (time.time(), True)
        return True
    else:
        print(f"[nlm] Login failed: {err[-200:]}", file=sys.stderr)
        return False


def ensure_auth() -> bool:
    if check_auth():
        return True
    print("[nlm] Auth expired, re-logging in...", file=sys.stderr)
    if not relogin():
        return False
    _run(["use", DEFAULT_NOTEBOOK])
    return check_auth()


def proactive_refresh():
    state = _load_state()
    last = state.get("last_query_time", 0)
    if last == 0:
        return

    elapsed_min = (time.time() - last) / 60
    if elapsed_min > PROACTIVE_REFRESH_MINUTES:
        if check_auth():
            print(f"[nlm] Proactive warmup ({elapsed_min:.0f}m since last query)", file=sys.stderr)
            warmup_regional_cookies()
        else:
            print(f"[nlm] Proactive relogin ({elapsed_min:.0f}m since last query)", file=sys.stderr)
            ensure_auth()


# ── Query Core ─────────────────────────────────────────────

def ask(question: str, notebook: str = None) -> tuple[str, bool]:
    """Query NotebookLM. Returns (answer_text, success)."""
    nb = notebook or DEFAULT_NOTEBOOK
    stdout, stderr, rc = _run(["ask", "-n", nb, question], timeout=90)
    combined = stdout + stderr

    if _is_auth_error(combined):
        return combined, False

    if rc != 0 and not combined.strip():
        return stderr, False

    answer = combined
    for marker in ["Answer:", "A:\n"]:
        idx = answer.find(marker)
        if idx != -1:
            answer = answer[idx + len(marker):]
            break
    return answer.strip(), rc == 0


def query(question: str, notebook: str = None) -> str:
    """Full pipeline v2: patch → refresh → route → optimize → cache → auth → ask → retry → learn."""
    nb = notebook or DEFAULT_NOTEBOOK

    ensure_source_patched()
    proactive_refresh()

    optimized = optimize_question(question)
    if optimized != question:
        print("[nlm] Question optimized for better retrieval", file=sys.stderr)

    # Check cache
    state = _load_state()
    cached = cache_get(optimized, nb)
    if cached:
        _record_query(state)
        state["cache_hits"] += 1
        _save_state(state)
        print("[nlm] Cache HIT — answering from cache", file=sys.stderr)
        # Still learn from cached result
        _learn_from_result(question, cached)
        return cached

    state["cache_misses"] += 1

    # Auth + query
    if not ensure_auth():
        return "ERROR: Could not authenticate with NotebookLM. Try: py -3.11 nlm_query.py --relogin"

    answer, ok = ask(optimized, nb)

    # Auto-retry if NotebookLM says "仅涵盖至前166页" etc.
    retry_count = 0
    while not ok or (_is_fail_signal(answer) and retry_count < 3):
        retry_count += 1
        if retry_count == 1:
            # Try with narrower scope — add chapter hint from routes
            ch, pages = _route_question(question)
            if ch:
                print(f"[nlm] Retry {retry_count}: narrowing to {ch} pp{pages}", file=sys.stderr)
                optimized = f"在《加密与解密（第4版）》{ch}（第{pages}页）中，{question}。请提供具体页码引用。"
            else:
                print(f"[nlm] Retry {retry_count}: generic retry", file=sys.stderr)
        elif retry_count == 2:
            # Probe: ask where this term appears
            print(f"[nlm] Retry {retry_count}: probe query", file=sys.stderr)
            optimized = f"在《加密与解密（第4版）》中，术语\"{question[:50]}\"出现在哪一章？请给出章节号和页码。"
        else:
            # Expand to full book search
            print(f"[nlm] Retry {retry_count}: full book search", file=sys.stderr)
            optimized = f"请从《加密与解密（第4版）》全书中查找：{question}。请提供原文引用和具体页码。"

        # Avoid caching retry attempts
        answer, ok = ask(optimized, nb)
        if ok and not _is_fail_signal(answer):
            break

    if ok:
        cache_set(optimized, nb, answer)
        _record_query(state)
        _save_state(state)
        # Auto-learn: extract page numbers and register new keywords
        _learn_from_result(question, answer)
        return answer

    # Final retry after re-login
    print("[nlm] Retrying after re-login...", file=sys.stderr)
    time.sleep(2)
    if relogin():
        _run(["use", nb])
        answer, ok = ask(optimized, nb)
        if ok:
            cache_set(optimized, nb, answer)
            _record_query(state)
            _save_state(state)
            _learn_from_result(question, answer)
            return answer

    if "status code 5" in answer:
        return f"ERROR: NotebookLM RPC failure.\nNotebook: {nb}\nCheck: py -3.11 -m notebooklm list"
    return f"ERROR: Query failed after retry.\n{answer}"


def query_raw(question: str, notebook: str = None, *, use_cache: bool = True, optimize: bool = True) -> str:
    """Simplified query entry point — delegates to ask() with minimal pipeline.

    Used by --no-cache and --no-optimize CLI flags to avoid duplicating pipeline logic.
    """
    nb = notebook or DEFAULT_NOTEBOOK
    ensure_source_patched()

    if not ensure_auth():
        return "ERROR: Could not authenticate with NotebookLM."

    final_q = optimize_question(question) if optimize else question

    if use_cache:
        cached = cache_get(final_q, nb)
        if cached:
            print("[nlm] Cache HIT", file=sys.stderr)
            return cached

    answer, ok = ask(final_q, nb)
    if use_cache and ok:
        cache_set(final_q, nb, answer)
        _learn_from_result(question, answer)

    state = _load_state()
    _record_query(state)
    if use_cache and ok:
        state["cache_misses" if not cached else "cache_hits"] += 1
    _save_state(state)

    return answer


# ── Health Dashboard ───────────────────────────────────────

def health_check() -> dict:
    """Collect all health metrics. Calls _run('status') just once."""
    state = _load_state()

    stdout = _get_status_output()
    auth_ok = not _is_auth_error(stdout) and "Notebook ID" in stdout

    nb_id = DEFAULT_NOTEBOOK if DEFAULT_NOTEBOOK in stdout else "?"
    conv_id = "?"
    uuid_m = re.search(
        r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
        stdout,
    )
    if uuid_m:
        cid = uuid_m.group(1)
        conv_id = cid[:20] + "..." if len(cid) > 20 else cid

    patched = ensure_source_patched()

    return {
        "auth": auth_ok,
        "notebook_id": nb_id,
        "conversation_id": conv_id,
        "source_patched": patched,
        "last_query": state.get("last_query_time", 0),
        "last_auth": state.get("last_auth_time", 0),
        "total_queries": state.get("total_queries", 0),
        "cache_hits": state.get("cache_hits", 0),
        "cache_misses": state.get("cache_misses", 0),
        "cache_files": _count_cache_files(),
        "version": state.get("version", 1),
    }


def print_health(h: dict, include_regional: bool = True):
    sep = "=" * 58
    print(sep)
    print("  NotebookLM Health Dashboard")
    print(sep)
    print(f"  Auth:           {'OK' if h['auth'] else 'EXPIRED'}")
    print(f"  Notebook ID:    {h['notebook_id']}")
    print(f"  Conversation:   {h['conversation_id']}")
    print(f"  Source Patch:    {'OK' if h['source_patched'] else 'NEEDED'}")

    if include_regional:
        warmed, total = warmup_regional_cookies()
        print(f"  Regional:        {warmed}/{total} regions reachable")

    print(f"  Last Query:      {_time_ago(h['last_query'])}")
    print(f"  Last Auth:       {_time_ago(h['last_auth'])}")
    print(f"  Total Queries:   {h['total_queries']}")
    total_q = max(h['total_queries'], 1)
    hit_rate = (h['cache_hits'] / total_q) * 100
    print(f"  Cache:           {h['cache_hits']} hits / {h['cache_misses']} misses ({hit_rate:.0f}% hit)")
    print(f"  Cached Entries:  {h['cache_files']}")
    print(f"  State Version:   v{h['version']}")
    print(sep)


# ── CLI ────────────────────────────────────────────────────

def _dispatch(args: argparse.Namespace) -> int:
    """Route CLI commands. Separated from main() for readability."""
    if args.health or args.health_quick:
        ensure_source_patched()
        h = health_check()
        print_health(h, include_regional=args.health)
        return 0 if h["auth"] else 1

    if args.status:
        ok = check_auth()
        print(f"Auth: {'OK' if ok else 'EXPIRED'}")
        return 0 if ok else 1

    if args.relogin:
        ok = relogin()
        if ok:
            _run(["use", DEFAULT_NOTEBOOK])
        return 0 if ok else 1

    if args.patch:
        ok = ensure_source_patched()
        print(f"Source patch: {'OK' if ok else 'FAILED'}")
        return 0 if ok else 1

    if args.warmup:
        w, t = warmup_regional_cookies()
        print(f"Regional warmup: {w}/{t} regions OK")
        return 0 if w >= 2 else 1

    if args.cache_stats:
        state = _load_state()
        total = state["total_queries"]
        hits = state["cache_hits"]
        rate = (hits / max(total, 1)) * 100
        print(f"Cache entries:  {_count_cache_files()}")
        print(f"Total queries:  {total}")
        print(f"Hits:           {hits}")
        print(f"Misses:         {state['cache_misses']}")
        print(f"Hit rate:       {rate:.1f}%")
        return 0

    if args.clear_cache:
        cache_clear()
        state = _load_state()
        for k in ("cache_hits", "cache_misses", "total_queries"):
            state[k] = 0
        _save_state(state)
        print("Cache cleared.")
        return 0

    if args.daemon:
        interval = max(args.daemon, 30)
        print(f"[nlm] Daemon — refreshing every {interval} min. Ctrl+C to stop.", file=sys.stderr)
        try:
            while True:
                ensure_source_patched()
                if check_auth():
                    print(f"[nlm] {time.strftime('%H:%M:%S')} Auth OK, warming regions...", file=sys.stderr)
                    warmup_regional_cookies()
                else:
                    print(f"[nlm] {time.strftime('%H:%M:%S')} Auth expired, relogging...", file=sys.stderr)
                    relogin()
                    _run(["use", DEFAULT_NOTEBOOK])
                time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("[nlm] Daemon stopped.", file=sys.stderr)
        return 0

    # ── Query path ──
    if args.question:
        question = " ".join(args.question)

        if args.no_optimize or args.no_cache:
            answer = query_raw(
                question, args.notebook,
                use_cache=not args.no_cache,
                optimize=not args.no_optimize,
            )
        else:
            answer = query(question, args.notebook)

        output_path = args.output or os.path.join(tempfile.gettempdir(), "nlm_answer.txt")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(answer)
        print(f"NLM_OUTPUT:{output_path}")

        if args.raw:
            try:
                print(answer)
            except UnicodeEncodeError:
                print(answer.encode("ascii", errors="replace").decode("ascii"))
        return 0

    return -1  # No command matched


def main():
    parser = argparse.ArgumentParser(description="NotebookLM Query Wrapper v3")
    parser.add_argument("question", nargs="*", help="Question to ask NotebookLM")
    parser.add_argument("--notebook", "-n", default=None, help="Notebook ID")
    parser.add_argument("--output", "-o", default=None, help="Output file path")
    parser.add_argument("--raw", action="store_true",
                       help="Also print to stdout (may garble Chinese)")
    parser.add_argument("--health", action="store_true",
                       help="Show health dashboard (includes regional scan)")
    parser.add_argument("--health-quick", action="store_true",
                       help="Health dashboard without regional scan")
    parser.add_argument("--relogin", action="store_true",
                       help="Force re-login + warmup")
    parser.add_argument("--warmup", action="store_true",
                       help="Warm up regional cookies")
    parser.add_argument("--status", action="store_true",
                       help="Quick auth status check")
    parser.add_argument("--patch", action="store_true",
                       help="Ensure source patching")
    parser.add_argument("--daemon", type=int, default=0, metavar="MINUTES",
                       help="Background auth keeper (min interval 30m)")
    parser.add_argument("--cache-stats", action="store_true",
                       help="Show cache statistics")
    parser.add_argument("--clear-cache", action="store_true",
                       help="Clear all cached Q&A entries")
    parser.add_argument("--no-cache", action="store_true",
                       help="Skip cache for this query")
    parser.add_argument("--no-optimize", action="store_true",
                       help="Skip question optimization")
    args = parser.parse_args()

    rc = _dispatch(args)
    if rc == -1:
        parser.print_help()
        return 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
