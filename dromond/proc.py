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


def session_kwargs(*, detached: bool = False) -> dict:
    """Popen kwargs that put the child in its own session / process group."""
    if not IS_WIN:
        return {"start_new_session": True}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if detached:
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
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
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                         capture_output=True, text=True)
    state = (out.stdout or "").strip()
    return not state.startswith("Z")


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


def terminate_group(pid: int) -> None:
    """Stop a worker and anything it spawned. Never raises."""
    if IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
        return
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
