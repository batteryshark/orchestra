"""User-session supervisor for `dromond daemon` (DESIGN §2).

macOS: a LaunchAgent. Windows: a per-user Scheduled Task at logon. Never a
system daemon: the agent CLIs need the login keychain / credential store
and the user's project checkouts.

Installing writes the unit and nothing else. Loading is a separate,
explicit `--start`, because writing a file is reversible and starting a
process that dispatches agents is not.
"""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from dromond import paths, proc

# ponytail: one fixed label, so one daemon per login session. DESIGN §2 wants
# a second daemon for a genuinely separate workspace — derive the label from
# DROMOND_HOME when that arrives.
LABEL = "local.dromond.daemon"
WIN_TASK = "DromondDaemon"


def _windows() -> bool:
    return sys.platform == "win32"


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def plist_path() -> Path:
    return paths.launch_agents_dir() / f"{LABEL}.plist"


def _program() -> list[str]:
    exe = proc.which("dromond")
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
    return f"gui/{_uid()}/{LABEL}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def is_loaded() -> bool:
    return _launchctl("print", _service_target()).returncode == 0


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
    print(f"dromond service: {'rewrote' if existed else 'wrote'} {p}")
    print(f"  runs: {' '.join(_program())}")
    print(f"  logs: {paths.logs_dir()}/daemon.{{out,err}}.log")
    if not start:
        print("  not loaded (pass --start, or run "
              f"`launchctl bootstrap gui/{_uid()} {p}`)")
        return 0
    res = _launchctl("bootstrap", f"gui/{_uid()}", str(p))
    if res.returncode != 0:
        print(f"dromond service: launchctl bootstrap failed: "
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
    plist as well as changed code. Without a supervisor there is nothing to
    restart, so the honest answer is to say what is running and let the
    operator stop it.
    """
    if _windows():
        return _win_restart()
    return _darwin_restart()


def _darwin_restart() -> int:
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
    for name in ("DROMOND_HOME", "DROMOND_CONFIG"):
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
        print(f"dromond service: scheduled task failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"dromond service: {'rewrote' if existed else 'wrote'} "
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
        print(f"dromond service: schtasks /Run failed: "
              f"{(run.stderr or run.stdout).strip()}")
        return 1
    print(f"  started scheduled task {WIN_TASK}")
    return 0


def _win_uninstall() -> int:
    if _win_task_running():
        subprocess.run(["schtasks", "/End", "/TN", WIN_TASK],
                       capture_output=True, text=True)
    if _win_task_exists():
        res = subprocess.run(["schtasks", "/Delete", "/TN", WIN_TASK, "/F"],
                             capture_output=True, text=True)
        if res.returncode != 0:
            print(f"dromond service: schtasks /Delete failed: "
                  f"{(res.stderr or res.stdout).strip()}")
            return 1
        print(f"dromond service: removed scheduled task {WIN_TASK}")
    else:
        print(f"dromond service: no scheduled task {WIN_TASK}")
    wrapper = wrapper_path()
    if wrapper.exists():
        wrapper.unlink()
        print(f"dromond service: removed {wrapper}")
    return 0


def _win_restart() -> int:
    if not _win_task_exists():
        print("dromond service: no scheduled task. "
              "`dromond daemon` or `dromond service install --start`")
        return 1
    if _win_task_running():
        subprocess.run(["schtasks", "/End", "/TN", WIN_TASK],
                       capture_output=True, text=True)
    _write_wrapper()
    res = subprocess.run(["schtasks", "/Run", "/TN", WIN_TASK],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"dromond service: schtasks /Run failed: "
              f"{(res.stderr or res.stdout).strip()}")
        return 1
    print(f"dromond service: restarted scheduled task {WIN_TASK}")
    return 0


def _win_status() -> int:
    wrapper = wrapper_path()
    print(f"dromond service: {WIN_TASK}")
    print(f"  wrapper: {wrapper} ({'present' if wrapper.exists() else 'absent'})")
    if not _win_task_exists():
        print("  scheduled task: not installed")
        return 0
    state = "running" if _win_task_running() else "ready"
    print(f"  scheduled task: installed, {state}")
    return 0
