@echo off
chcp 65001 >nul
echo ============================================================
echo   book-notebooklm Setup (Windows)
echo ============================================================
echo.

REM Find Python 3
where py >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python launcher "py" not found.
    echo Install Python 3.10+ from https://python.org and try again.
    pause
    exit /b 1
)

echo [1/4] Installing Python dependencies...
py -3 -m pip install notebooklm-py httpx PyPDF2 PyMuPDF Pillow --quiet
if %errorlevel% neq 0 (
    echo [WARN] pip install had issues. Try running manually:
    echo   py -3 -m pip install notebooklm-py httpx PyPDF2
)

echo [2/4] Installing Playwright browser...
py -3 -m playwright install chromium 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Playwright install had issues. Run manually:
    echo   py -3 -m playwright install chromium
)

echo [3/4] Authenticating with Google NotebookLM...
echo.
echo A browser window will open. Log in with your Google account.
echo When you see the NotebookLM homepage, press ENTER in this window.
echo.
py -3 -m notebooklm login --browser msedge
if %errorlevel% neq 0 (
    echo [WARN] Login failed. Run manually later:
    echo   py -3 -m notebooklm login --browser msedge
)

echo [4/4] Verifying setup...
py -3 -m notebooklm status 2>nul
if %errorlevel% neq 0 (
    echo [WARN] Could not verify auth. Check with:
    echo   py -3 scripts/nlm_query.py --health
)

echo.
echo ============================================================
echo   Setup complete!
echo.
echo   Next steps:
echo   1. Create a notebook at https://notebooklm.google.com
echo   2. Upload your book PDF as a source
echo   3. Set your notebook ID:
echo      set NOTEBOOKLM_DEFAULT_NB=your_notebook_id
echo   4. Test: py -3 scripts/nlm_query.py --health
echo ============================================================
pause
