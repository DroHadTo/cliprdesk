@echo off
REM Runs the tool without a console window (used by the optional auto-start task).
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe exit /b 1
start "" .venv\Scripts\pythonw.exe app.py
