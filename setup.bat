@echo off
REM ============================================================================
REM  Bob master setup (Python kernel, zero PowerShell).
REM  Run ONCE after cloning + install_prereqs.bat. Idempotent (safe to re-run).
REM  Installs engine+proxy -> creates venvs -> fetches models -> wires Continue
REM  and dsh, via `python -m bob.kernel setup`.
REM
REM  Usage:   setup.bat                 (full, includes voice+vision)
REM           setup.bat --skip-models   (skip the multi-GB model downloads)
REM           setup.bat --skip-voice    (skip the STT model / piper voice downloads)
REM           setup.bat --profile 12gb  (smaller models for ~12GB VRAM)
REM           setup.bat --with-aider    (also install aider; opt-in)
REM           setup.bat --with-fabric   (also build fabric; opt-in, needs Go)
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
