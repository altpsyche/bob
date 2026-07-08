@echo off
REM ============================================================================
REM  Bob master setup (Python kernel, zero PowerShell).
REM  Run ONCE after cloning + install_prereqs.bat. Idempotent (safe to re-run).
REM  Builds engine+proxy -> creates venvs + installs tools -> fetches models ->
REM  wires Continue/aider, via `python -m bob.kernel setup`.
REM
REM  Usage:   setup.bat                 (full, includes voice+vision)
REM           setup.bat --skip-models   (skip the ~38GB model downloads)
REM           setup.bat --skip-voice    (skip whisper/piper/mmproj downloads)
REM           setup.bat --profile 12gb  (smaller models for ~12GB VRAM)
REM           setup.bat --launch        (start the stack when done)
REM ============================================================================
setlocal
REM version-stamp: state which Bob release this blessed entry belongs to.
set "BOBVER=?"
if exist "%~dp0VERSION" set /p BOBVER=<"%~dp0VERSION"
echo [setup] Bob %BOBVER% - setup
set "PYTHONPATH=%~dp0scripts"
where python >nul 2>nul || (
  echo [setup] Python 3.12 is required. Run install_prereqs.bat first.
  exit /b 1
)
python -m bob.kernel setup %*
exit /b %ERRORLEVEL%
