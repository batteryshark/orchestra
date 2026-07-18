"""Persistent opencode server host for ensemble runs.

Ensemble teammates live inside the lead's opencode process; a one-shot
`opencode run` kills the whole team when the lead's turn ends. Orchestra
therefore hosts ensemble sessions on a long-lived `opencode serve` and
dispatches leads with `--attach`, so teams survive client exits and the
plugin's async wake-ups (teammate -> lead promptAsync) keep working.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 4763

STATE_DIR = Path("~/.local/state/orchestra").expanduser()
STATE_FILE = STATE_DIR / "host.json"
LOG_FILE = STATE_DIR / "host.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _healthy(url: str) -> bool:
    try:
        urllib.request.urlopen(url + "/app", timeout=2).read()
        return True
    except Exception:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return True
        except Exception:
            return False


def state() -> dict | None:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            return None
    return None


def url() -> str | None:
    s = state()
    if s and _alive(s.get("pid", -1)) and _healthy(s["url"]):
        return s["url"]
    return None


def ensure(port: int = DEFAULT_PORT) -> str:
    """Return the host URL, starting the server if needed."""
    u = url()
    if u:
        return u
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    try:
        from orchestra_cli import config as _config
        _config.apply_env_passthrough(_config.load(None), env)
    except Exception:
        pass
    # server-side sessions (teammates, async lead wake-ups) have no client to
    # answer permission asks — a blocked ask hangs the team forever. This server
    # exists only to run orchestra workers, so allow everything on it.
    # OPENCODE_CONFIG_CONTENT merges over global/project config (docs: config).
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {"permission": {"*": "allow", "external_directory": "allow"}})
    with open(LOG_FILE, "ab") as log:
        proc = subprocess.Popen(
            [shutil.which("opencode") or "opencode", "serve", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, start_new_session=True)
    u = f"http://127.0.0.1:{port}"
    for _ in range(40):
        if _healthy(u):
            STATE_FILE.write_text(json.dumps({"pid": proc.pid, "url": u, "port": port}))
            return u
        if proc.poll() is not None:
            raise SystemExit(f"orchestra: opencode serve exited at startup; see {LOG_FILE}")
        time.sleep(0.5)
    raise SystemExit(f"orchestra: opencode serve on port {port} not healthy; see {LOG_FILE}")


def stop() -> bool:
    s = state()
    if s and _alive(s.get("pid", -1)):
        try:
            os.killpg(s["pid"], 15)
        except OSError:
            os.kill(s["pid"], 15)
        STATE_FILE.unlink(missing_ok=True)
        return True
    STATE_FILE.unlink(missing_ok=True)
    return False
