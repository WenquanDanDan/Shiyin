@echo off
rem vtw debug launcher: console window with live logs
rem normal use: double-click run.pyw (no console)

rem Global keyboard hooks may not receive keys in a non-elevated process
rem (UIPI + security software), so relaunch elevated when needed.
rem NOTE: use goto instead of a parenthesised if-block, because %~f0 contains
rem parentheses and would terminate the block early.
net session >nul 2>&1
if "%errorlevel%"=="0" goto run

powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:run
cd /d "%~dp0"
".venv\Scripts\python.exe" -m vtw -v %*
pause