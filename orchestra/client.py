"""Thin v2 HTTP client and local operator-token storage for the CLI."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from orchestra import config, paths


class ClientError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status, self.payload = status, payload


def _service(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"orchestra-v2-{digest}"


def _fallback_path() -> Path:
    return paths.state_dir() / "client-tokens.json"


def save_token(url: str, token: str) -> None:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", "orchestra",
             "-s", _service(url), "-w", token], capture_output=True, text=True)
        if result.returncode == 0:
            return
    location = _fallback_path()
    values = {}
    if location.is_file():
        try:
            values = json.loads(location.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            values = {}
    values[url] = token
    location.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    if os.name == "posix":
        location.chmod(0o600)


def load_token(url: str) -> str | None:
    # A supervised worker may inherit an operator shell that already had a
    # broader token. The short-lived run credential must always win.
    explicit = os.environ.get("ORCHESTRA_RUN_TOKEN") or os.environ.get(
        "ORCHESTRA_TOKEN")
    if explicit:
        return explicit
    if sys.platform == "darwin":
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "orchestra",
             "-s", _service(url), "-w"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    location = _fallback_path()
    if location.is_file():
        try:
            value = json.loads(location.read_text(encoding="utf-8")).get(url)
            return value if isinstance(value, str) and value else None
        except (OSError, ValueError):
            pass
    return None


class Client:
    def __init__(self, url: str | None = None, token: str | None = None,
                 timeout: float = 30):
        self.url = (url or config.api_url()).rstrip("/")
        self.token = token if token is not None else load_token(self.url)
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, query=None):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {key: value for key, value in query.items() if value is not None})
        raw = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=raw, method=method)
        request.add_header("Accept", "application/json")
        if raw is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not response.length and response.status == 204:
                    return None
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                message = payload.get("error", {}).get("message") or str(exc)
            except (ValueError, AttributeError):
                payload, message = None, str(exc)
            raise ClientError(message, exc.code, payload) from exc
        except urllib.error.URLError as exc:
            raise ClientError(f"cannot reach Orchestra at {self.url}: {exc.reason}") from exc

    def get(self, path: str, **query):
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body=body)

    def patch(self, path: str, body: dict):
        return self.request("PATCH", path, body=body)
