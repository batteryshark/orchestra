"""launchd LaunchAgent for `dromond daemon` (DESIGN §2).

A LaunchAgent in the user session, never a system daemon: the agent CLIs
need the login keychain and the user's project checkouts.

Installing writes the plist and nothing else. Loading is a separate,
explicit `--start`, because writing a file is reversible and starting a
process that dispatches agents is not.
"""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from dromond import paths

# ponytail: one fixed label, so one daemon per login session. DESIGN §2 wants
# a second daemon for a genuinely separate workspace — derive the label from
# DROMOND_HOME when that arrives.
LABEL = "local.dromond.daemon"


def plist_path() -> Path:
    return paths.launch_agents_dir() / f"{LABEL}.plist"


def _program() -> list[str]:
    exe = shutil.which("dromond")
    return [exe, "daemon"] if exe else [sys.executable, "-m", "dromond", "daemon"]


def build_plist() -> dict:
    logs = paths.logs_dir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    # Only forward the overrides that are actually set: launchd gives the job
    # a bare environment, so an unset override must stay unset, not become "".
    # Read through ``paths.env``, so an installer whose shell still exports the
    # old MAESTRO_* name writes the value under the NEW name — the plist is a
    # file that outlives the shim, and it should not carry a deprecated name.
    for name in ("DROMOND_HOME", "DROMOND_CONFIG"):
        value = paths.env(name)
        if value:
            env[name] = value
    return {
        "Label": LABEL,
        "ProgramArguments": _program(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
        "ProcessType": "Background",
    }


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def is_loaded() -> bool:
    return _launchctl("print", _service_target()).returncode == 0


def install(start: bool = False) -> int:
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    with open(p, "wb") as f:
        plistlib.dump(build_plist(), f)
    print(f"dromond service: {'rewrote' if existed else 'wrote'} {p}")
    print(f"  runs: {' '.join(_program())}")
    print(f"  logs: {paths.logs_dir()}/daemon.{{out,err}}.log")
    if not start:
        print("  not loaded (pass --start, or run "
              f"`launchctl bootstrap gui/{os.getuid()} {p}`)")
        return 0
    res = _launchctl("bootstrap", f"gui/{os.getuid()}", str(p))
    if res.returncode != 0:
        print(f"dromond service: launchctl bootstrap failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"  loaded and started as {_service_target()}")
    return 0


def uninstall() -> int:
    p = plist_path()
    if is_loaded():
        res = _launchctl("bootout", _service_target())
        print("dromond service: unloaded " + _service_target() if res.returncode == 0
              else f"dromond service: bootout failed: {(res.stderr or res.stdout).strip()}")
    if p.exists():
        p.unlink()  # Dromond wrote this file; nothing of the user's is touched
        print(f"dromond service: removed {p}")
    else:
        print(f"dromond service: no plist at {p}")
    return 0


def restart() -> int:
    """Restart the daemon so a code change takes effect.

    The dashboard's restart button re-execs the running process; this is the
    same intent from a terminal, and the two differ in what they can do. Under
    launchd, ``kickstart -k`` kills and relaunches, which picks up a changed
    plist as well as changed code. Without launchd there is no supervisor to
    restart anything, so the honest answer is to say what is running and let
    the operator stop it.
    """
    if not is_loaded():
        running = subprocess.run(["pgrep", "-f", "dromond daemon"],
                                 capture_output=True, text=True)
        pids = running.stdout.split()
        if pids:
            print("dromond service: not managed by launchd; a daemon is "
                  f"running as pid {', '.join(pids)}.")
            print("  stop it and start it again, or `dromond service install "
                  "--start` to have launchd own it")
            return 1
        print("dromond service: nothing is running. "
              "`dromond daemon` or `dromond service install --start`")
        return 1
    res = _launchctl("kickstart", "-k", _service_target())
    if res.returncode != 0:
        print(f"dromond service: launchctl kickstart failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"dromond service: restarted {_service_target()}")
    return 0


def status() -> int:
    p = plist_path()
    print(f"dromond service: {LABEL}")
    print(f"  plist:  {p} ({'present' if p.exists() else 'absent'})")
    res = _launchctl("print", _service_target())
    if res.returncode != 0:
        print("  launchd: not loaded")
        return 0
    fields = {"state": None, "pid": None, "last exit code": None}
    for line in res.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in fields:
            fields[key] = value.strip()
    print("  launchd: loaded" + "".join(
        f", {k} {v}" for k, v in fields.items() if v is not None))
    return 0
