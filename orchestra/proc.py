"""Process control that works on Unix and Windows.

Unix keeps process groups (``start_new_session`` / ``killpg``). Windows has
neither: a child is launched with ``CREATE_NEW_PROCESS_GROUP`` and the tree
is torn down with ``taskkill /T``. ``os.kill(pid, 0)`` is also not a
liveness probe on Windows, so ``alive`` uses ``GetExitCodeProcess``.
"""
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
_WIN_LAUNCHABLE = (".exe", ".cmd", ".bat", ".com")
_STILL_ACTIVE = 259


def which(name: str) -> str | None:
    """A path ``CreateProcess`` / ``exec`` can actually launch.

    On Windows ``shutil.which('claude')`` can land on the extensionless npm
    shim, which is a Unix shell script. Prefer ``.cmd`` / ``.exe``.
    """
    if not IS_WIN:
        return shutil.which(name)
    suffix = Path(name).suffix.lower()
    if suffix in _WIN_LAUNCHABLE and Path(name).is_file():
        return name
    for ext in (".cmd", ".exe", ".bat"):
        found = shutil.which(name if name.lower().endswith(ext) else name + ext)
        if found:
            return found
    found = shutil.which(name)
    if found and Path(found).suffix.lower() in _WIN_LAUNCHABLE:
        return found
    return found


def resolve_cmd(cmd: list[str]) -> list[str]:
    out = list(cmd)
    found = which(out[0])
    if found:
        out[0] = found
    return out


# The Win32 creation flags, named here because CPython only defines them on
# Windows: reading them off `subprocess` directly makes the Windows branch
# unrunnable from a mac, so its test could only fail. The values are fixed by
# the Win32 API, so naming them costs nothing and buys a testable branch.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def session_kwargs(*, detached: bool = False) -> dict:
    """Popen kwargs that put the child in its own session / process group."""
    if not IS_WIN:
        return {"start_new_session": True}
    flags = CREATE_NEW_PROCESS_GROUP
    if detached:
        flags |= DETACHED_PROCESS | CREATE_NO_WINDOW
    return {"creationflags": flags}


def login_bin_dirs() -> list[str]:
    """Directories a login-session daemon must see to find harnesses and itself."""
    home = Path.home()
    candidates = [
        home / ".local" / "bin",
        Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        / "Programs" / "OpenAI" / "Codex" / "bin",
        Path(r"C:\Program Files\nodejs"),
        Path(r"C:\Program Files\Git\cmd"),
        Path(r"C:\Program Files\Tailscale"),
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]
    out = []
    for path in candidates:
        if path.is_dir() and str(path) not in out:
            out.append(str(path))
    return out


def enrich_path(env: dict[str, str]) -> dict[str, str]:
    """Prepend login-session bin dirs so a scheduled task still finds CLIs."""
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for directory in reversed(login_bin_dirs()):
        if directory not in parts:
            parts.insert(0, directory)
    updated = dict(env)
    updated["PATH"] = os.pathsep.join(parts)
    return updated


def alive(pid: int) -> bool:
    """True only for a process that could still be working."""
    if IS_WIN:
        return _win_alive(int(pid))
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True)
    except OSError:
        return True  # liveness is unproven; recovery must fail closed
    state = (out.stdout or "").strip()
    return not state.startswith("Z")


def process_identity(pid: int) -> str | None:
    """Kernel-backed identity for a process-group leader, or ``None``.

    A PID alone is not durable: after reuse it may name an unrelated process,
    and ``killpg`` would then target that process's group. The launch path
    records this token beside the PID; crash recovery must match both before
    signaling. Unsupported or unreadable platforms fail closed.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if IS_WIN:
        created = _win_creation_time(pid)
        return f"win:{created}" if created is not None else None
    if sys.platform == "darwin":
        info = _darwin_process_info(pid)
        if info is None:
            return None
        pgid, seconds, microseconds = info
        return (f"darwin:{seconds}:{microseconds}"
                if pgid == pid else None)
    if sys.platform.startswith("linux"):
        return _linux_process_identity(pid)
    return None


def _darwin_process_info(pid: int) -> tuple[int, int, int] | None:
    """Return (process group, start seconds, start microseconds) via libproc."""
    import ctypes

    class ProcBSDInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        call = libproc.proc_pidinfo
        call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                         ctypes.c_void_p, ctypes.c_int]
        call.restype = ctypes.c_int
        info = ProcBSDInfo()
        size = ctypes.sizeof(info)
        read = call(pid, 3, 0, ctypes.byref(info), size)  # PROC_PIDTBSDINFO
    except (AttributeError, OSError):
        return None
    if read != size or info.pbi_pid != pid:
        return None
    return (int(info.pbi_pgid), int(info.pbi_start_tvsec),
            int(info.pbi_start_tvusec))


def _linux_process_identity(pid: int) -> str | None:
    """Boot id + kernel start ticks, while proving ``pid`` leads its group."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8").strip()
        closing = raw.rfind(")")
        if closing < 0:
            return None
        fields = raw[closing + 2:].split()
        pgid = int(fields[2])       # proc(5) field 5; fields starts at field 3
        started = fields[19]        # proc(5) field 22
    except (OSError, ValueError, IndexError):
        return None
    if not boot or pgid != pid:
        return None
    return f"linux:{boot}:{started}"


def _win_alive(pid: int) -> bool:
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _win_creation_time(pid: int) -> int | None:
    """The process creation FILETIME, stable across PID reuse."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                        ctypes.byref(exited), ctypes.byref(kernel),
                                        ctypes.byref(user)):
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def harvest_children() -> int:
    """Reap dead children. A no-op on Windows: there is no waitpid(-1)."""
    if IS_WIN:
        return 0
    harvested = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if pid == 0:
            break
        harvested += 1
    return harvested


def terminate_group(pid: int, *, force: bool = False) -> None:
    """Stop a worker and anything it spawned. Never raises.

    ``force`` skips the polite signal. It is for the LAST resort — a child
    that already ignored a SIGTERM and outlived its grace period. Without it
    such a worker was merely asked again, politely, forever: SIGTERM succeeds
    against a process that ignores it, so the escalation to SIGKILL below
    (which only fires when SIGTERM RAISES) never ran.
    """
    if IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
        return
    if not force:
        try:
            os.killpg(pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except Exception:
            pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def signal_group(pid: int, sig: int) -> None:
    """``killpg``-shaped: raise ``ProcessLookupError`` when the pid is gone.

    ``sig == 0`` is the liveness probe. Any other signal terminates the tree
    on Windows (there is no SIGTERM delivery).
    """
    if not IS_WIN:
        os.killpg(pid, sig)
        return
    if not alive(pid):
        raise ProcessLookupError(pid)
    if sig == 0:
        return
    terminate_group(pid)


def signal_owned_group(pid: int, expected_identity: str | None,
                       sig: int) -> tuple[str, str]:
    """Signal a process group only while its durable identity still matches.

    Returns ``(outcome, detail)`` where outcome is ``signalled``, ``gone``,
    or ``refused``. A missing or unreadable identity fails closed: a stored
    PID is not ownership proof after it may have been reused.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "refused", f"invalid worker pid {pid}"
    if pid <= 0:
        return "refused", f"invalid worker pid {pid}"
    try:
        signal_group(pid, 0)
    except ProcessLookupError:
        return "gone", "process already gone"
    except (OSError, ValueError):
        return "refused", f"worker group {pid} could not be inspected"
    if not expected_identity:
        return "refused", f"worker group {pid} has no recorded process identity"
    actual_identity = process_identity(pid)
    if actual_identity is None:
        return "refused", f"worker group {pid} identity could not be read"
    if actual_identity != expected_identity:
        return ("refused", f"worker group {pid} identity changed; refusing to "
                "signal a possibly unrelated process")
    try:
        signal_group(pid, sig)
    except ProcessLookupError:
        return "gone", "process already gone"
    except (OSError, ValueError):
        return "refused", f"worker group {pid} could not be signalled"
    return "signalled", f"signalled worker group {pid}"


def chmod(path, mode: int) -> None:
    """Best-effort mode bits. Windows ACLs ignore 0600; do not fail the write."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def fchmod(fd: int, mode: int) -> None:
    fn = getattr(os, "fchmod", None)
    if fn is None:
        return
    try:
        fn(fd, mode)
    except (OSError, NotImplementedError):
        pass


# launchd hands its jobs 256 file descriptors, and every run Orchestra
# starts inherits that number: the daemon, each supervisor, and each worker
# harness. 256 is not enough for a harness — opencode refuses to start with
# "possibly due to low max file descriptors (Current limit: 256)", which is
# how piu-arcade-lift run 40 died in one second, and how run 38 was left
# with no supervisor at all while nine runs were live at once.
FILE_LIMIT = 65536


def raise_file_limit(target: int = FILE_LIMIT) -> int:
    """Give this process and everything it spawns room to work.

    Only the SOFT limit moves, and only upward: a process may always raise
    its soft limit to the hard one, so this needs no privilege and changes
    nothing outside this process tree. Returns the soft limit in force.
    """
    try:
        import resource
    except ImportError:  # Windows has no rlimits
        return 0
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= target:
        return soft
    ceiling = target if hard == resource.RLIM_INFINITY else min(target, hard)
    for want in (ceiling, 10240, 4096):
        if want <= soft:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            return want
        except (ValueError, OSError):
            continue  # the kernel's own per-process cap; try a smaller one
    return soft
