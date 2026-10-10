@echo off
setlocal
cd /d "%~dp0..\.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m projects.dachanbay_tennis %*
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m projects.dachanbay_tennis %*
  ) else (
    python -m projects.dachanbay_tennis %*
  )
)
if errorlevel 1 pause
endlocal
