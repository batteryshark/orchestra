"""[settings] lifecycle callbacks: one shell command per event.

Two exist — ``on_run_finished`` (the result and its handoff are durable)
and ``on_run_blocked`` (a run raised an ask, or an agent's request needs a
human decision). Fire and forget: Orchestra learns nothing about the
listener, and a missed callback is covered by the listener's own fallback
poll. These are the only two states where someone external waits on a
change that is invisible until polled; resist adding more.
"""
import os
import subprocess
import sys


def fire(cfg: dict, key: str, env: dict) -> None:
    cmd = ((cfg.get("settings") or {}).get(key) or "").strip()
    if not cmd:
        return
    try:
        subprocess.Popen(cmd, shell=True, env=dict(os.environ, **env),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        print(f"orchestra: {key} could not start: {exc}", file=sys.stderr)
