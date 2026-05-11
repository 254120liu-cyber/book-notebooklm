#!/usr/bin/env bash
set -e

echo "============================================================"
echo "  book-notebooklm Setup (macOS / Linux)"
echo "============================================================"
echo ""

# Find Python 3
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &> /dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3.10+ not found. Install from https://python.org"
    exit 1
fi
echo "Using: $PYTHON ($($PYTHON --version))"

echo "[1/4] Installing Python dependencies..."
$PYTHON -m pip install notebooklm-py httpx PyPDF2 --quiet || \
    echo "[WARN] pip install had issues"

echo "[2/4] Installing Playwright browser..."
$PYTHON -m playwright install chromium 2>/dev/null || \
    echo "[WARN] Playwright install had issues"

echo "[3/4] Authenticating with Google NotebookLM..."
echo ""
echo "A browser window will open. Log in with your Google account."
echo "When you see the NotebookLM homepage, press ENTER in this terminal."
echo ""
$PYTHON -m notebooklm login --browser msedge 2>/dev/null || \
    $PYTHON -m notebooklm login 2>/dev/null || \
    echo "[WARN] Login failed. Run: notebooklm login"

echo "[4/4] Verifying setup..."
$PYTHON -m notebooklm status 2>/dev/null || true

echo ""
echo "============================================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Create a notebook at https://notebooklm.google.com"
echo "  2. Upload your book PDF as a source"
echo "  3. Set your notebook ID:"
echo "     export NOTEBOOKLM_DEFAULT_NB=your_notebook_id"
echo "  4. Test: python3 scripts/nlm_query.py --health"
echo "============================================================"
