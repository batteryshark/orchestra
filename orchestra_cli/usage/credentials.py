"""Server-side credential discovery for the usage collectors.

Only the COLLECTORS in `providers.py` ever touch `Credential.value`; the value
must never appear in any `to_dict()` output or JSON response. Errors are explicit
so the UI can distinguish "missing" from "read but unparseable".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAX_CREDENTIAL_FILE_BYTES = 1_048_576

DEFAULT_OPENCODE_AUTH = Path("~/.local/share/opencode/auth.json").expanduser()


class CredentialError(RuntimeError):
    pass


class CredentialMissing(CredentialError):
    pass


@dataclass(frozen=True, slots=True)
class Credential:
    value: str
    source: str


def _read_json(path: Path) -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise CredentialMissing(f"Credential file not found: {path}") from exc
    if stat.st_size > MAX_CREDENTIAL_FILE_BYTES:
        raise CredentialError(f"Credential file is unexpectedly large: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialError(f"Could not read credential file: {path}") from exc
    if not isinstance(value, dict):
        raise CredentialError(f"Credential file has an invalid shape: {path}")
    return value


def _environment_credential(names: Iterable[str]) -> Credential | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return Credential(value=value, source=f"environment ({name})")
    return None


def opencode_api_key(
    provider_ids: Iterable[str],
    env_names: Iterable[str],
    *,
    auth_path: Path | None = None,
) -> Credential:
    from_environment = _environment_credential(env_names)
    if from_environment:
        return from_environment

    path = auth_path or DEFAULT_OPENCODE_AUTH
    payload = _read_json(path)
    for provider_id in provider_ids:
        entry = payload.get(provider_id)
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if isinstance(key, str) and key.strip():
            return Credential(key.strip(), f"OpenCode ({provider_id})")
    raise CredentialMissing("No matching API key is configured in OpenCode")
