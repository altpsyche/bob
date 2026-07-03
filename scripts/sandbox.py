"""O5 — OS-level sandbox for exec surfaces (shell_run today; any future exec tool).

`run_sandboxed(argv, cwd, timeout, limits)` runs a command under an OS-native confinement backend,
selected via NB3's `osenv`. Read-only tools stay in-process — there is no benefit to sandboxing a pure
read, and the wrapping cost/limits only make sense around an exec surface.

Backends (deny-by-default filesystem where the OS allows, resource limits, optional no-network):
  Linux  : bubblewrap (`bwrap`) > `nsjail` > `unshare` + rlimit fallback (rlimits + pid/net ns only)
  Windows: restricted token + Job Object (win32) ; Job-Object-only if a restricted token can't be built
  macOS  : deferred with the rest of macOS (`sandbox-exec`) — reports UNAVAILABLE for now

Policy (config, all under `runtime.agent.*`):
  ``sandbox`` = ``'off'`` (default) | ``'on'``
    off -> callers do NOT wrap (they run in-process); byte-identical to pre-O5 behavior.
    on  -> `run_sandboxed` wraps the command. If no usable backend exists it **fails closed**
           (raises ``SandboxUnavailable``) so the caller refuses rather than silently running
           unconfined — a loud *unsandboxed* fallback is only ever chosen when ``sandbox='off'``.
  ``sandboxLimits`` = ``{ cpuSeconds, memoryMB, allowRoots: [paths], network: bool }``

Defense in depth, not a replacement: the N9 secrets denylist + web_fetch SSRF guard still apply at the
tool layer. On Linux the deny-by-default bind set never mounts ``$HOME`` (so ``~/.ssh``/secrets are
absent from the sandbox namespace even with a filesystem view); on Windows a restricted token filters
the user's own SIDs. Both are documented in docs/SECURITY.md with the per-OS guarantee matrix.
"""
import os
import shutil
import subprocess
from pathlib import Path

import osenv

SANDBOX_OFF = "off"
SANDBOX_ON = "on"

# Linux base bind set (deny-by-default): only these system dirs are read-only-mounted; $HOME is
# deliberately absent, so secrets under it don't exist inside the sandbox. allowRoots are rw-bound.
_LINUX_RO_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32", "/etc", "/opt")


class SandboxUnavailable(RuntimeError):
    """Raised by run_sandboxed when sandbox='on' but no usable backend exists on this host. The
    caller fails closed (refuses to run) — it must NOT downgrade to an unsandboxed run under 'on'."""


# --- policy resolvers ----------------------------------------------------------------------------

def sandbox_mode(config: dict) -> str:
    """'off' (default) | 'on', from agent.sandbox. Anything unrecognized is treated as 'off'."""
    mode = ((config or {}).get("agent", {}) or {}).get("sandbox", SANDBOX_OFF)
    return SANDBOX_ON if str(mode).lower() == SANDBOX_ON else SANDBOX_OFF


def sandbox_limits(config: dict) -> dict:
    """Normalize agent.sandboxLimits into the internal shape used by the backends. allowRoots
    defaults to the repo root (the workspace) so a confined shell can still operate on the project
    but nothing else; an empty explicit list means 'no rw root' (only a tmpfs /tmp)."""
    raw = ((config or {}).get("agent", {}) or {}).get("sandboxLimits", {}) or {}
    roots = raw.get("allowRoots")
    if roots is None:
        roots = [str(osenv.REPO)]
    elif isinstance(roots, str):
        roots = [roots]
    return {
        "cpu_seconds": int(raw.get("cpuSeconds", 30)),
        "memory_mb": int(raw.get("memoryMB", 2048)),
        # Expand a leading ~ but otherwise leave the string as given (don't OS-normalize separators —
        # the backends resolve() each root at build time). Keeps allowRoots stable across platforms.
        "allow_roots": [_expand_root(r) for r in roots if r],
        "network": bool(raw.get("network", False)),
    }


def _expand_root(p) -> str:
    s = str(p)
    return str(Path(s).expanduser()) if s.startswith("~") else s


# --- backend probe -------------------------------------------------------------------------------

def backend_name() -> str | None:
    """The confinement backend available on this host, or None. Windows reports 'windows' when the
    win32 job API is importable; Linux prefers bwrap > nsjail > unshare; macOS is deferred (None)."""
    if osenv.is_windows():
        try:
            import win32job  # noqa: F401  (pywin32)
            return "windows"
        except Exception:
            return None
    if shutil.which("bwrap"):
        return "bwrap"
    if shutil.which("nsjail"):
        return "nsjail"
    if shutil.which("unshare"):
        return "unshare"
    return None


def available() -> bool:
    return backend_name() is not None


# --- Linux argv builders (pure — unit-tested without executing) ----------------------------------

def _bwrap_argv(argv: list, limits: dict, cwd: str | None) -> list:
    """bubblewrap: deny-by-default namespace. RO-bind the system dirs, RW-bind allowRoots, tmpfs
    /tmp+/run, private proc/dev; $HOME is never bound (secrets absent). No network unless allowed."""
    a = ["bwrap", "--die-with-parent", "--new-session",
         "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
    if not limits.get("network"):
        a += ["--unshare-net"]
    for d in _LINUX_RO_DIRS:
        if os.path.exists(d):
            a += ["--ro-bind", d, d]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run"]
    roots = limits.get("allow_roots", [])
    for root in roots:
        rp = str(Path(root).resolve())
        if os.path.exists(rp):
            a += ["--bind", rp, rp]
    workdir = str(Path(cwd).resolve()) if cwd else (roots[0] if roots else "/tmp")
    a += ["--chdir", workdir, "--"]
    return a + list(argv)


def _nsjail_argv(argv: list, limits: dict, cwd: str | None) -> list:
    """nsjail equivalent of the bwrap policy: exec mode, mount ns, RO system dirs, RW allowRoots,
    tmpfs /tmp, rlimits as flags, optional network. Command follows the `--` separator."""
    roots = limits.get("allow_roots", [])
    workdir = str(Path(cwd).resolve()) if cwd else (roots[0] if roots else "/tmp")
    a = ["nsjail", "--quiet", "--mode", "o",  # 'o' = run once (execve)
         "--cwd", workdir,
         "--rlimit_cpu", str(limits.get("cpu_seconds", 30)),
         "--rlimit_as", str(limits.get("memory_mb", 2048)),
         "--tmpfsmount", "/tmp"]
    if not limits.get("network"):
        a += ["--disable_clone_newnet=false"]  # keep a fresh (empty) net ns => no external network
    for d in _LINUX_RO_DIRS:
        if os.path.exists(d):
            a += ["--bindmount_ro", f"{d}:{d}"]
    for root in roots:
        rp = str(Path(root).resolve())
        if os.path.exists(rp):
            a += ["--bindmount", f"{rp}:{rp}"]
    a += ["--"]
    return a + list(argv)


def _unshare_argv(argv: list, limits: dict) -> list:
    """Weakest Linux tier: pid (+ optional net) namespace, no filesystem confinement. Resource
    limits come from the rlimit preexec, not from unshare. Documented as rlimits-only — fs jailing
    needs bwrap/nsjail (the integration test for write-denial skips on this tier)."""
    a = ["unshare", "--fork", "--pid", "--mount-proc"]
    if not limits.get("network"):
        a += ["--net"]
    return a + list(argv)


def _rlimit_preexec(limits: dict):
    """POSIX preexec: cap CPU seconds (RLIMIT_CPU) and address space (RLIMIT_AS) in the child before
    exec. None on platforms without `resource` (Windows uses the Job Object instead)."""
    try:
        import resource
    except ImportError:  # pragma: no cover — Windows
        return None

    def _apply():
        cpu = limits.get("cpu_seconds")
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        mem_mb = limits.get("memory_mb")
        if mem_mb:
            b = int(mem_mb) * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (b, b))
            except (ValueError, OSError):
                pass  # some kernels reject RLIMIT_AS for the shell; CPU cap still applies

    return _apply


# --- runners -------------------------------------------------------------------------------------

def _run_linux(argv, cwd, timeout, limits, backend):
    if backend == "bwrap":
        wrapped = _bwrap_argv(argv, limits, cwd)
    elif backend == "nsjail":
        wrapped = _nsjail_argv(argv, limits, cwd)
    else:  # unshare (rlimits-only)
        wrapped = _unshare_argv(argv, limits)
    return subprocess.run(wrapped, capture_output=True, text=True, timeout=timeout,
                          preexec_fn=_rlimit_preexec(limits))


def _run_windows(argv, cwd, timeout, limits):  # pragma: no cover — exercised only on Windows
    """A Job Object enforcing a per-process memory cap, an active-process cap, and kill-on-close,
    applied to the child (and any grandchildren it spawns). Captures output via a piped Popen.

    NOTE (docs/SECURITY.md): this delivers the *resource* guarantee (memory/process caps + reliable
    tree teardown). Full deny-by-default *filesystem* confinement on Windows needs a restricted token
    with restricting SIDs (Chromium-style) or an AppContainer — that is a tracked Windows hardening
    follow-up, because it must be validated live on Windows before it can be trusted. Until then the
    N9 secrets denylist is the filesystem floor for file_* tools; a sandboxed shell_run on Windows is
    resource-confined but not FS-jailed."""
    import win32con
    import win32job

    mem_bytes = int(limits.get("memory_mb", 2048)) * 1024 * 1024
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    info["ProcessMemoryLimit"] = mem_bytes
    info["BasicLimitInformation"]["ActiveProcessLimit"] = 64
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)

    p = subprocess.Popen(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, creationflags=win32con.CREATE_NO_WINDOW)
    try:
        # Assign immediately after spawn. The child runs a few instructions before assignment (a
        # negligible race for resource caps); KILL_ON_JOB_CLOSE then owns the whole process tree.
        win32job.AssignProcessToJobObject(job, int(p._handle))
    except Exception:
        pass  # assignment failed — still run (best-effort); the caller logs the degraded mode
    try:
        out, err = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, p.returncode, stdout=out, stderr=err)
    except subprocess.TimeoutExpired:
        win32job.TerminateJobObject(job, 1)
        p.kill()
        raise
    finally:
        win32job.TerminateJobObject(job, 1)


def run_sandboxed(argv, cwd: str | None = None, timeout: int = 30,
                  limits: dict | None = None) -> subprocess.CompletedProcess:
    """Run `argv` (a full command vector, e.g. osenv.default_shell() + [cmd]) confined by the host's
    backend. Returns a subprocess.CompletedProcess; raises subprocess.TimeoutExpired on timeout and
    SandboxUnavailable when no backend exists (the caller must then refuse under sandbox='on')."""
    limits = limits or {}
    backend = backend_name()
    if backend is None:
        raise SandboxUnavailable(
            "no sandbox backend on this host "
            "(Linux: install bubblewrap/nsjail; Windows: install pywin32)")
    if backend == "windows":
        return _run_windows(argv, cwd, timeout, limits)
    return _run_linux(argv, cwd, timeout, limits, backend)
