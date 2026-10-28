"""Signed, fixed-destination sender for the reporting API.

Credentials are read from the repo's .env (or the environment; inline comments are not supported) and never
logged, repr'd or placed in exceptions. Only event type, event id, HTTP status
and the server's short error code leave this module.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from signalforge.reports import events as ev
from signalforge.reports.auth import ReportingCredentials, authentication_headers

URL = "https://signalforge-api.example.com/v1/ingest"
TIMEOUT_S = 30
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
VARIABLES = (
    "SIGNALFORGE_ACCESS_CLIENT_ID",
    "SIGNALFORGE_ACCESS_CLIENT_SECRET",
    "SIGNALFORGE_INGEST_SECRET",
)
_ERROR_CODE = re.compile(r"^(?:[a-z][a-z0-9_]{0,39}|Invalid field: [a-z][a-z0-9_.]{0,59})$")

log = logging.getLogger(__name__)


class MissingCredentials(Exception):
    """Raised with the VARIABLE NAME only, never a value."""


@dataclass(frozen=True)
class SendOutcome:
    event_type: str
    event_id: str
    status_code: int
    error_code: str | None
    ok: bool
    duplicate: bool


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name.strip() in VARIABLES:
            values[name.strip()] = value
    return values


def load_credentials(env_path=None, environ=None) -> ReportingCredentials:
    import os

    file_values = _parse_env(ENV_PATH if env_path is None else Path(env_path))
    environment = os.environ if environ is None else environ
    resolved = []
    for name in VARIABLES:
        value = file_values.get(name) or environment.get(name)
        if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
            raise MissingCredentials(f"missing or invalid credential variable: {name}")
        resolved.append(value.strip())
    return ReportingCredentials(*resolved)


def _error_code(response) -> str | None:
    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("error", "code"):
        value = data.get(key)
        if isinstance(value, str) and _ERROR_CODE.match(value):
            return value
    return None


def send(event, session, credentials, clock=time.time) -> SendOutcome:
    """Validate, sign with a fresh timestamp and nonce, POST once, classify."""
    ev.validate_event(event)
    body = ev.serialize(event)
    headers = authentication_headers(
        body, credentials, timestamp=int(clock()), nonce=str(uuid.uuid4()))
    kind, event_id = event["event_type"], event["event_id"]
    try:
        response = session.post(URL, data=body, headers=headers, allow_redirects=False, timeout=TIMEOUT_S)
    except Exception:
        outcome = SendOutcome(kind, event_id, 0, "network_error", False, False)
    else:
        code = _error_code(response)
        status = response.status_code
        duplicate = status == 409 and code == "duplicate_event"
        outcome = SendOutcome(kind, event_id, status, code, status == 202 or duplicate, duplicate)
    log.info("report %s event_id=%s status=%s error=%s",
             outcome.event_type, outcome.event_id, outcome.status_code, outcome.error_code)
    return outcome
