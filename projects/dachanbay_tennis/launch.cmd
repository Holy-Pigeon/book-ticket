@echo off
setlocal
cd /d "%~dp0..\.."
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m projects.dachanbay_tennis %*
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
