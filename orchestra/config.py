"""Tiny non-secret bootstrap; managed configuration lives in SQLite."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from orchestra import paths


DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 8765,
    "callback_command": [],
    "secret_file": str(paths.home() / "v2" / "secrets.json"),
}


class ConfigError(ValueError):
    pass


def _validate(value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError("bootstrap must be a JSON object")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ConfigError("unknown bootstrap field(s): " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **value}
    if not isinstance(result["bind"], str) or not result["bind"].strip():
        raise ConfigError("bootstrap bind must be a non-empty string")
    if isinstance(result["port"], bool) or not isinstance(result["port"], int) \
            or not 0 <= result["port"] <= 65535:
        raise ConfigError("bootstrap port must be an integer from 0 to 65535")
    command = result["callback_command"]
    if not isinstance(command, list) or any(
            not isinstance(part, str) or not part for part in command):
        raise ConfigError("callback_command must be a JSON argv array")
    if not isinstance(result["secret_file"], str) or not result["secret_file"]:
        raise ConfigError("secret_file must be a path")
    return result


def read(path: Path | None = None) -> dict:
    location = path or paths.bootstrap_path()
    if not location.is_file():
        return dict(DEFAULTS)
    try:
        return _validate(json.loads(location.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read bootstrap {location}: {exc}") from exc


def write(value: dict, path: Path | None = None) -> Path:
    location = path or paths.bootstrap_path()
    checked = _validate(value)
    location.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=location.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(checked, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, location)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return location


def ensure() -> tuple[Path, bool]:
    location = paths.bootstrap_path()
    if location.exists():
        read(location)
        return location, False
    return write(DEFAULTS, location), True


def http_config() -> dict:
    value = read()
    return {"bind": value["bind"], "port": value["port"]}


def api_url() -> str:
    explicit = os.environ.get("ORCHESTRA_URL")
    if explicit:
        return explicit.rstrip("/")
    value = http_config()
    host = "127.0.0.1" if value["bind"] in ("0.0.0.0", "::") else value["bind"]
    return f"http://{host}:{value['port']}"


def callback_command() -> list[str]:
    return list(read()["callback_command"])


def secret_environment(path: Path | None = None) -> dict[str, str]:
    """Read an optional owner-only JSON environment file without exposing it."""
    location = path or Path(read()["secret_file"]).expanduser()
    if not location.exists():
        return {}
    if os.name == "posix" and location.stat().st_mode & 0o077:
        raise ConfigError(f"secret file must be mode 0600: {location}")
    try:
        value = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read secret file {location}: {exc}") from exc
    if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value.items()):
        raise ConfigError("secret file must be a JSON object of string values")
    return value


def worker_environment(base=None) -> dict[str, str]:
    return {**dict(os.environ if base is None else base), **secret_environment()}


def apply_profile_env(profile: dict, env: dict[str, str]) -> dict[str, str]:
    """Compatibility seam inside built-in command builders, not config storage."""
    values = profile.get("env") or {}
    if not isinstance(values, dict):
        raise ConfigError("profile env snapshot must be an object")
    result = dict(env)
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError("profile environment keys and values must be strings")
        result[key] = value
    return result
