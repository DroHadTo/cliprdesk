@echo off
REM ===================================================================
REM  GTA6cliptool setup - plain, readable, safe to re-run.
REM  What it does (nothing hidden, no PowerShell, no admin rights):
REM   1. Python 3.12+ : uses yours, or offers the official winget package.
REM   2. ffmpeg       : uses yours, or offers the official winget package (Gyan.FFmpeg).
REM   3. Python libs  : a private .venv in this folder (never touches your other Python packages),
REM                     pip with pinned versions + SHA-256 hashes (--require-hashes).
REM   4. TwitchDownloaderCLI : official GitHub release, SHA-256 checked, unzipped to .\bin
REM   5. .env         : created from .env.example. Provider connections are optional.
REM   6. Auto-start   : ONLY if you answer Y (a normal Task Scheduler task for your user).
REM ===================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found.
  choice /M "Install Python 3.12 from the official winget package (Python.Python.3.12)"
  if errorlevel 2 ( echo Install Python from https://www.python.org/downloads/ then run setup.bat again. & pause & exit /b 1 )
  winget install -e --id Python.Python.3.12 --accept-package-agreements
  echo Close this window and run setup.bat again so Python is on PATH. & pause & exit /b 0
)

python -c "import sys,sysconfig; sys.exit(sys.version_info < (3,12) or sysconfig.get_platform() != 'win-amd64')"
if errorlevel 1 ( echo Install Windows x64 Python 3.12 or newer and run setup.bat again. & pause & exit /b 1 )

where ffmpeg >nul 2>nul
if errorlevel 1 (
  choice /M "ffmpeg not found. Install it from the official winget package (Gyan.FFmpeg)"
  if errorlevel 2 ( echo FFmpeg is required. & pause & exit /b 1 )
  winget install -e --id Gyan.FFmpeg --accept-package-agreements
  if errorlevel 1 ( echo FFmpeg installation failed. & pause & exit /b 1 )
  echo Close this window and run setup.bat again so FFmpeg is on PATH. & pause & exit /b 0
)

if not exist .venv\Scripts\python.exe python -m venv .venv
if errorlevel 1 ( echo Could not create .venv & pause & exit /b 1 )
echo Installing Python libraries into .venv (pinned + hash-checked)...
.venv\Scripts\python -m pip install --disable-pip-version-check --no-warn-script-location --require-hashes -r requirements.txt
if errorlevel 1 ( echo pip failed. & pause & exit /b 1 )
.venv\Scripts\python -m pip install --disable-pip-version-check --require-hashes --no-deps -r requirements-media.txt
if errorlevel 1 ( echo The media tools could not be installed. & pause & exit /b 1 )
.venv\Scripts\python -m pip check
if errorlevel 1 ( echo A dependency is missing or incompatible. & pause & exit /b 1 )

.venv\Scripts\python setup_helper.py tdcli
if errorlevel 1 ( pause & exit /b 1 )

.venv\Scripts\python setup_helper.py env
if errorlevel 1 ( pause & exit /b 1 )

echo.
choice /M "Start GTA6cliptool automatically when you log in (Task Scheduler, your user only)"
if not errorlevel 2 (
  schtasks /Create /F /SC ONLOGON /TN GTA6cliptool /TR "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0app.py\""
  if errorlevel 1 ( echo Auto-start could not be added. Use start.bat to open the app. ) else ( echo Auto-start added. )
)
echo.
echo Done. Run start.bat to open the clip desk.
pause
