@echo off
setlocal
cd /d "%~dp0..\.."
set "BOOK_TICKET_ARGS=%*"
if "%~1"=="" set "BOOK_TICKET_ARGS=watch"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m projects.pkuszh %BOOK_TICKET_ARGS%
) else (
  py -3 -m projects.pkuszh %BOOK_TICKET_ARGS%
)
if errorlevel 1 pause
endlocal
