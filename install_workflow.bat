@echo off
cd /d "%~dp0"
if not exist .github\workflows mkdir .github\workflows
move /Y ci\scrape.yml .github\workflows\scrape.yml
rmdir ci
echo workflow installed at .github\workflows\scrape.yml
pause
