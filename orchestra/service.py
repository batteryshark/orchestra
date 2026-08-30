"""User-session supervisor for `orchestra daemon` (DESIGN §2).

macOS: a LaunchAgent. Windows: a per-user Scheduled Task at logon. Never a
system daemon: the agent CLIs need the login keychain / credential store
and the user's project checkouts.

Installing writes the unit and nothing else. Loading is a separate,
explicit `--start`, because writing a file is reversible and starting a
process that dispatches agents is not.
"""
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from orchestra import paths, proc

# ponytail: one fixed label, so one daemon per login session. DESIGN §2 wants
# a second daemon for a genuinely separate workspace — derive the label from
# ORCHESTRA_HOME when that arrives.
LABEL = "local.orchestra.daemon"
WIN_TASK = "OrchestraDaemon"


def _windows() -> bool:
    return sys.platform == "win32"


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def plist_path() -> Path:
    return paths.launch_agents_dir() / f"{LABEL}.plist"


def _program() -> list[str]:
    exe = proc.which("orchestra")
    return [exe, "daemon"] if exe else [sys.executable, "-m", "orchestra", "daemon"]


def build_plist() -> dict:
    logs = paths.logs_dir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    # Only forward the overrides that are actually set: launchd gives the job
    # a bare environment, so an unset override must stay unset, not become "".
    for name in ("ORCHESTRA_HOME",):
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
    return f"gui/{_uid()}/{LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def is_loaded() -> bool:
    return _launchctl("print", _service_target()).returncode == 0


def service_pid() -> int | None:
    """The pid launchd currently has for the job, or None if it holds none."""
    res = _launchctl("print", _service_target())
    if res.returncode != 0:
        return None
    m = re.search(r"^\s*pid = (\d+)$", res.stdout, re.M)
    return int(m.group(1)) if m else None


def install(start: bool = False) -> int:
    if _windows():
        return _win_install(start)
    return _darwin_install(start)


def _darwin_install(start: bool = False) -> int:
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    with open(p, "wb") as f:
        plistlib.dump(build_plist(), f)
    print(f"orchestra service: {'rewrote' if existed else 'wrote'} {p}")
    print(f"  runs: {' '.join(_program())}")
    print(f"  logs: {paths.logs_dir()}/daemon.{{out,err}}.log")
    if not start:
        print("  not loaded (pass --start, or run "
              f"`launchctl bootstrap gui/{_uid()} {p}`)")
        return 0
    res = _launchctl("bootstrap", f"gui/{_uid()}", str(p))
    if res.returncode != 0:
        print(f"orchestra service: launchctl bootstrap failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"  loaded and started as {_service_target()}")
    return 0


def uninstall() -> int:
    if _windows():
        return _win_uninstall()
    return _darwin_uninstall()


def _darwin_uninstall() -> int:
    p = plist_path()
    if is_loaded():
        res = _launchctl("bootout", _service_target())
        print("orchestra service: unloaded " + _service_target() if res.returncode == 0
              else f"orchestra service: bootout failed: {(res.stderr or res.stdout).strip()}")
    if p.exists():
        p.unlink()  # Orchestra wrote this file; nothing of the user's is touched
        print(f"orchestra service: removed {p}")
    else:
        print(f"orchestra service: no plist at {p}")
    return 0


def restart() -> int:
    """Restart the daemon so a code change takes effect.

    The dashboard's restart button re-execs the running process; this is the
    same intent from a terminal, and the two differ in what they can do. Under
    launchd, ``kickstart -k`` kills and relaunches, which picks up a changed
    plist as well as changed code. Without a supervisor there is nothing to
    restart, so the honest answer is to say what is running and let the
    operator stop it.
    """
    if _windows():
        return _win_restart()
    return _darwin_restart()


def _darwin_restart() -> int:
    if not is_loaded():
        running = subprocess.run(["pgrep", "-f", "orchestra daemon"],
                                 capture_output=True, text=True)
        pids = running.stdout.split()
        if pids:
            print("orchestra service: not managed by launchd; a daemon is "
                  f"running as pid {', '.join(pids)}.")
            print("  stop it and start it again, or `orchestra service install "
                  "--start` to have launchd own it")
            return 1
        print("orchestra service: nothing is running. "
              "`orchestra daemon` or `orchestra service install --start`")
        return 1
    before = service_pid()
    res = _launchctl("kickstart", "-k", _service_target())
    if res.returncode != 0:
        print(f"orchestra service: launchctl kickstart failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    after = _await_new_pid(before)
    # kickstart -k reports success and leaves the process untouched (seen on
    # macOS 25.5: a daemon ran 15 hours across two "restarts", serving stale
    # code and a wedged runway scraper). A restart that did not restart must
    # never print that it did, so the pid is checked and SIGTERM finishes the
    # job — KeepAlive brings the replacement up.
    if after is not None and after == before:
        _launchctl("kill", "SIGTERM", _service_target())
        after = _await_new_pid(before)
    if after is not None and after == before:
        print(f"orchestra service: {_service_target()} did not restart; "
              f"pid {before} is still running")
        return 1
    print(f"orchestra service: restarted {_service_target()}"
          + (f" (pid {after})" if after else ""))
    return 0


def _await_new_pid(before: int | None, tries: int = 40) -> int | None:
    """The pid once launchd has settled, waiting out the gap where the job is
    down and holds none. Returns ``before`` if it never changed.

    With no pid to compare against there is nothing to wait for, so an
    unmanaged or already-stopped job returns at once."""
    if before is None:
        return None
    seen = before
    for _ in range(tries):
        time.sleep(0.25)
        pid = service_pid()
        if pid is not None:
            seen = pid
            if pid != before:
                return pid
    return seen


def status() -> int:
    if _windows():
        return _win_status()
    return _darwin_status()


def status_line() -> str:
    if _windows():
        return f"scheduled task {WIN_TASK} ({'installed' if _win_task_exists() else 'not installed'})"
    return (f"{plist_path()} "
            f"({'installed' if plist_path().exists() else 'not installed'})")


def _darwin_status() -> int:
    p = plist_path()
    print(f"orchestra service: {LABEL}")
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


def wrapper_path() -> Path:
    return paths.home() / "daemon.cmd"


def _write_wrapper() -> Path:
    program = _program()
    logs = paths.logs_dir()
    path_value = proc.enrich_path(dict(os.environ)).get("PATH", "")
    quoted = subprocess.list2cmdline(program)
    lines = [
        "@echo off",
        f'set "PATH={path_value}"',
        'set "PYTHONIOENCODING=utf-8"',
        'set "PYTHONUTF8=1"',
    ]
    for name in ("ORCHESTRA_HOME", "ORCHESTRA_CONFIG"):
        value = paths.env(name)
        if value:
            lines.append(f'set "{name}={value}"')
    lines += [
        f'cd /d "{Path.home()}"',
        f'{quoted} >> "{logs / "daemon.out.log"}" 2>> "{logs / "daemon.err.log"}"',
        "",
    ]
    dest = wrapper_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True)


def _win_task_exists() -> bool:
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", WIN_TASK],
        capture_output=True, text=True)
    return res.returncode == 0


def _win_task_running() -> bool:
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", WIN_TASK, "/FO", "LIST"],
        capture_output=True, text=True)
    if res.returncode != 0:
        return False
    return any(line.strip().lower() == "status: running"
               for line in res.stdout.splitlines())


def _win_install(start: bool = False) -> int:
    wrapper = _write_wrapper()
    existed = _win_task_exists()
    # PT0S = no execution time limit (schtasks defaults to 72 hours).
    script = (
        f'$action = New-ScheduledTaskAction -Execute "cmd.exe" '
        f'-Argument "/c `"{wrapper}`"" -WorkingDirectory "{Path.home()}"; '
        f'$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; '
        f'$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries '
        f'-DontStopIfGoingOnBatteries -RestartCount 3 '
        f'-RestartInterval (New-TimeSpan -Minutes 1) '
        f'-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew; '
        f'$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME '
        f'-LogonType Interactive -RunLevel Limited; '
        f'Register-ScheduledTask -TaskName "{WIN_TASK}" -Action $action '
        f'-Trigger $trigger -Settings $settings -Principal $principal -Force '
        f'| Out-Null'
    )
    res = _ps(script)
    if res.returncode != 0:
        print(f"orchestra service: scheduled task failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"orchestra service: {'rewrote' if existed else 'wrote'} "
          f"scheduled task {WIN_TASK}")
    print(f"  runs: {wrapper}")
    print(f"  logs: {paths.logs_dir()}/daemon.{{out,err}}.log")
    if not start:
        print("  not started (pass --start, or run "
              f"`schtasks /Run /TN {WIN_TASK}`)")
        return 0
    run = subprocess.run(["schtasks", "/Run", "/TN", WIN_TASK],
                         capture_output=True, text=True)
    if run.returncode != 0:
        print(f"orchestra service: schtasks /Run failed: "
              f"{(run.stderr or run.stdout).strip()}")
        return 1
    print(f"  started scheduled task {WIN_TASK}")
    return 0


def _win_uninstall() -> int:
    _win_kill_daemons()
    if _win_task_exists():
        res = subprocess.run(["schtasks", "/Delete", "/TN", WIN_TASK, "/F"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            print(f"orchestra service: schtasks /Delete failed: "
                  f"{(res.stderr or res.stdout).strip()}")
            return 1
        print(f"orchestra service: removed scheduled task {WIN_TASK}")
    else:
        print(f"orchestra service: no scheduled task {WIN_TASK}")
    wrapper = wrapper_path()
    if wrapper.exists():
        wrapper.unlink()
        print(f"orchestra service: removed {wrapper}")
    return 0


def _win_daemon_pids() -> list[int]:
    """PIDs whose command line is the daemon, never this CLI process."""
    me = os.getpid()
    res = subprocess.run(
        ["wmic", "process", "where",
         "CommandLine like '%orchestra%daemon%'",
         "get", "ProcessId"],
        capture_output=True, text=True)
    pids = []
    for token in (res.stdout or "").split():
        if token.isdigit() and int(token) != me:
            pids.append(int(token))
    return pids


def _win_kill_daemons() -> None:
    """schtasks /End only stops the cmd wrapper; the python daemon stays up."""
    subprocess.run(["schtasks", "/End", "/TN", WIN_TASK],
                   capture_output=True, text=True)
    for pid in _win_daemon_pids():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True)


def _win_restart() -> int:
    if not _win_task_exists():
        print("orchestra service: no scheduled task. "
              "`orchestra daemon` or `orchestra service install --start`")
        return 1
    _win_kill_daemons()
    _write_wrapper()
    res = subprocess.run(["schtasks", "/Run", "/TN", WIN_TASK],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"orchestra service: schtasks /Run failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"orchestra service: restarted scheduled task {WIN_TASK}")
    return 0


def _win_status() -> int:
    wrapper = wrapper_path()
    print(f"orchestra service: {WIN_TASK}")
    print(f"  wrapper: {wrapper} ({'present' if wrapper.exists() else 'absent'})")
    if not _win_task_exists():
        print("  scheduled task: not installed")
        return 0
    state = "running" if _win_task_running() else "ready"
    print(f"  scheduled task: installed, {state}")
    return 0
