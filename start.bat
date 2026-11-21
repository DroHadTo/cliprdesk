@echo off
REM Starts the clipper and opens its private desk on this PC.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe ( echo Run setup.bat first. & pause & exit /b 1 )
start "" .venv\Scripts\pythonw.exe app.py --open
