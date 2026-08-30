"""Small v2 authority model: devices, integrations, and the current run."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from orchestra.contracts import TERMINAL_STATES


SERVICE_AUTHORITIES = frozenset(("read", "dispatch", "control", "answer"))
RUN_AUTHORITIES = frozenset(("read", "delegate", "attention", "artifact"))
_PAIRING_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class AuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Identity:
    kind: str
    subject_id: str
    authorities: frozenset[str]
    run_id: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def hashed(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _pairing_code() -> str:
    raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(12))
    return "-".join(raw[index:index + 4] for index in range(0, 12, 4))


def _normalized_pairing_code(value: str) -> str:
    raw = "".join(character for character in str(value or "").upper()
                  if character not in " -")
    raw = raw.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    if len(raw) != 12 or any(character not in _PAIRING_ALPHABET
                             for character in raw):
        return ""
    return raw


def bootstrap_device(con, name: str = "First device") -> tuple[dict, str]:
    """Mint the first operator only; subsequent devices must pair."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
            raise AuthError("an operator device already exists; pair this device")
        device_id, raw, now = str(uuid.uuid4()), _token("od_"), _stamp()
        con.execute(
            "INSERT INTO devices(device_id,name,token_hash,created_at,last_seen_at) "
            "VALUES(?,?,?,?,?)", (device_id, name.strip() or name, hashed(raw), now, now))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return {"device_id": device_id, "name": name, "created_at": now}, raw


def create_pairing(con, *, created_by_device_id: str | None,
                   ttl_seconds: int = 300, commit: bool = True) -> dict:
    ttl_seconds = max(60, min(int(ttl_seconds), 900))
    pairing_id, code = str(uuid.uuid4()), _pairing_code()
    created, expires = _now(), _now() + timedelta(seconds=ttl_seconds)
    def apply():
        con.execute(
            "INSERT INTO pairing_codes(pairing_id,code_hash,created_by_device_id,"
            "created_at,expires_at) VALUES(?,?,?,?,?)",
            (pairing_id, hashed(_normalized_pairing_code(code)), created_by_device_id,
             _stamp(created), _stamp(expires)),
        )
    if commit:
        with con:
            apply()
    else:
        if not con.in_transaction:
            raise RuntimeError("commit=False requires a caller-owned transaction")
        apply()
    return {
        "pairing_id": pairing_id,
        "code": code,
        "expires_at": _stamp(expires),
    }


def redeem_pairing(con, pairing_id: str, code: str, name: str, *,
                   commit: bool = True) -> tuple[dict, str]:
    """Redeem exactly once. The device bearer is returned and never stored raw."""
    name = (name or "").strip()
    if not name:
        raise AuthError("device name is required")
    if commit:
        con.execute("BEGIN IMMEDIATE")
    elif not con.in_transaction:
        raise RuntimeError("commit=False requires a caller-owned transaction")
    try:
        code_hash = hashed(_normalized_pairing_code(code))
        if pairing_id:
            row = con.execute(
                "SELECT * FROM pairing_codes WHERE pairing_id=? AND code_hash=?",
                (pairing_id, code_hash),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM pairing_codes WHERE code_hash=?",
                (code_hash,),
            ).fetchone()
        if row is None or row["used_at"] is not None:
            raise AuthError("pairing code is invalid or already used")
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError) as exc:
            raise AuthError("pairing code is invalid") from exc
        if expires <= _now():
            raise AuthError("pairing code expired")
        now, device_id, raw = _stamp(), str(uuid.uuid4()), _token("od_")
        claimed = con.execute(
            "UPDATE pairing_codes SET used_at=? WHERE pairing_id=? AND used_at IS NULL",
            (now, row["pairing_id"]),
        )
        if claimed.rowcount != 1:
            raise AuthError("pairing code was already used")
        con.execute(
            "INSERT INTO devices(device_id,name,token_hash,created_at,last_seen_at) "
            "VALUES(?,?,?,?,?)", (device_id, name, hashed(raw), now, now))
        if commit:
            con.commit()
    except BaseException:
        if commit:
            con.rollback()
        raise
    return {"device_id": device_id, "name": name, "created_at": now}, raw


def create_service_token(con, name: str, authorities, *,
                         commit: bool = True) -> tuple[dict, str]:
    requested = frozenset(authorities or ())
    if not requested or not requested <= SERVICE_AUTHORITIES:
        raise AuthError("service authorities must be a non-empty subset of: " +
                        ", ".join(sorted(SERVICE_AUTHORITIES)))
    token_id, raw, now = str(uuid.uuid4()), _token("os_"), _stamp()
    def apply():
        con.execute(
            "INSERT INTO service_tokens(token_id,name,token_hash,authorities_json,"
            "created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (token_id, name.strip() or "integration", hashed(raw),
             json.dumps(sorted(requested)), now, now),
        )
    if commit:
        with con:
            apply()
    else:
        if not con.in_transaction:
            raise RuntimeError("commit=False requires a caller-owned transaction")
        apply()
    return {
        "token_id": token_id,
        "name": name.strip() or "integration",
        "authorities": sorted(requested),
        "created_at": now,
    }, raw


def mint_run(con, run_id: int) -> str:
    raw = _token("or_")
    changed = con.execute(
        "UPDATE runs SET run_token_hash=? WHERE id=? AND status NOT IN "
        f"({','.join('?' for _ in TERMINAL_STATES)})",
        (hashed(raw), run_id, *sorted(TERMINAL_STATES)),
    )
    if changed.rowcount != 1:
        raise AuthError(f"run {run_id} is not active")
    con.commit()
    return raw


def identify(con, raw: str | None) -> Identity | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    digest, now = hashed(raw), _stamp()
    if raw.startswith("od_"):
        row = con.execute(
            "SELECT device_id FROM devices WHERE token_hash=? AND revoked_at IS NULL",
            (digest,),
        ).fetchone()
        if row:
            con.execute("UPDATE devices SET last_seen_at=? WHERE device_id=?",
                        (now, row["device_id"]))
            con.commit()
            return Identity("device", row["device_id"], frozenset(("*",)))
    elif raw.startswith("os_"):
        row = con.execute(
            "SELECT token_id,authorities_json FROM service_tokens "
            "WHERE token_hash=? AND revoked_at IS NULL", (digest,),
        ).fetchone()
        if row:
            try:
                authorities = frozenset(json.loads(row["authorities_json"]))
            except (TypeError, ValueError):
                return None
            if not authorities <= SERVICE_AUTHORITIES:
                return None
            con.execute("UPDATE service_tokens SET last_seen_at=? WHERE token_id=?",
                        (now, row["token_id"]))
            con.commit()
            return Identity("service", row["token_id"], authorities)
    elif raw.startswith("or_"):
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        row = con.execute(
            "SELECT id FROM runs WHERE run_token_hash=? "
            f"AND status NOT IN ({placeholders})", (digest, *sorted(TERMINAL_STATES)),
        ).fetchone()
        if row:
            run_id = int(row["id"])
            return Identity("run", str(run_id), RUN_AUTHORITIES, run_id)
    return None


def authorize(identity: Identity | None, authority: str, *,
              target_run_id: int | None = None) -> None:
    if identity is None:
        raise AuthError("authentication required")
    if "*" in identity.authorities:
        return
    if authority not in identity.authorities:
        raise AuthError(f"credential has no {authority} authority")
    if identity.kind == "run" and target_run_id != identity.run_id:
        raise AuthError(
            f"run {identity.run_id} may act only on itself, not run {target_run_id}")


def revoke_device(con, device_id: str) -> bool:
    with con:
        target = con.execute(
            "SELECT revoked_at FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if target is None or target["revoked_at"] is not None:
            return False
        active = con.execute(
            "SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0]
        if active <= 1:
            raise AuthError("cannot revoke the last operator device")
        changed = con.execute(
            "UPDATE devices SET revoked_at=? WHERE device_id=? AND revoked_at IS NULL",
            (_stamp(), device_id),
        )
    return changed.rowcount == 1


def revoke_service_token(con, token_id: str) -> bool:
    changed = con.execute(
        "UPDATE service_tokens SET revoked_at=? WHERE token_id=? AND revoked_at IS NULL",
        (_stamp(), token_id),
    )
    con.commit()
    return changed.rowcount == 1
