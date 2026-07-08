@echo off
REM ============================================================================
REM  Bob prerequisite installer (Python kernel, zero PowerShell).
REM  Ensures Python is present, then hands off to `python -m bob.kernel prereqs`,
REM  which installs Node.js, uv, Go, Python 3.12, CUDA Toolkit, cmake, Docker.
REM  Run ONCE on a fresh machine. Idempotent.
REM
REM  Manual prereqs (install before running this):
REM    Git          https://git-scm.com
REM    Python 3.12  winget install Python.Python.3.12
REM    VS2022 C++   winget install Microsoft.VisualStudio.2022.Community
REM                 (then: VS Installer -> Modify -> Desktop development with C++)
REM ============================================================================
setlocal
REM version-stamp: state which Bob release this blessed entry belongs to.
set "BOBVER=?"
if exist "%~dp0VERSION" set /p BOBVER=<"%~dp0VERSION"
echo [install_prereqs] Bob %BOBVER% - prerequisite install
set "PYTHONPATH=%~dp0scripts"
where python >nul 2>nul || (
    echo [install_prereqs] Python 3.12 is required.
    echo Install it with:  winget install Python.Python.3.12   then re-run install_prereqs.bat
    exit /b 1
)
python -m bob.kernel prereqs %*
exit /b %ERRORLEVEL%
