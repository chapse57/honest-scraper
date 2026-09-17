@echo off
chcp 65001 > nul
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt || goto :err
python -m playwright install chromium || goto :err
echo.
echo ===== 1) Tests (30, offline) =====
python -m pytest -v || goto :err
echo.
echo ===== 2) Static site: books.toscrape.com (3 pages, 2 injected failures) =====
python scraper.py --pages 3 --inject-failures
echo.
echo ===== 3) JS site: quotes.toscrape.com/js  (API strategy vs headless browser, cross-checked) =====
python js_scraper.py --pages 3 --strategy both
echo.
xcopy /E /I /Y output sample_output > nul
echo Done. See output\report.md and output\report_js.md (copied to sample_output\ for the GitHub repo)
pause
exit /b 0
:err
echo FAILED - see messages above
pause
exit /b 1
