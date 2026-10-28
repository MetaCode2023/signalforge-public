"""Documented reporting authentication only; no payload schema or sender.

Header dictionaries contain secrets. They must never be logged or persisted.
This module is deliberately outside the socket-free paper execution path.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReportingCredentials:
    access_client_id: str = field(repr=False)
    access_client_secret: str = field(repr=False)
    ingest_secret: str = field(repr=False)

    def __post_init__(self):
        for value in (self.access_client_id, self.access_client_secret, self.ingest_secret):
            if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
                raise ValueError("reporting credentials must be nonempty single-line strings")


def authentication_headers(body: bytes, credentials: ReportingCredentials, *, timestamp=None, nonce=None):
    """Sign exact bytes using REPORTING_SETUP.md; never invent a report body."""
    if not isinstance(body, bytes):
        raise TypeError("sign the exact serialized UTF-8 bytes")
    timestamp = int(time.time()) if timestamp is None else timestamp
    nonce = str(uuid.uuid4()) if nonce is None else nonce
    if type(timestamp) is not int or timestamp < 0:
        raise ValueError("timestamp must be a nonnegative Unix integer")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", nonce):
        raise ValueError("nonce must be a safe nonempty header value")
    prefix = f"v1:{timestamp}:{nonce}:".encode("utf-8")
    signature = hmac.new(credentials.ingest_secret.encode("utf-8"), prefix + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "CF-Access-Client-Id": credentials.access_client_id,
        "CF-Access-Client-Secret": credentials.access_client_secret,
        "x-signalforge-timestamp": str(timestamp),
        "x-signalforge-nonce": nonce,
        "x-signalforge-signature": "v1=" + signature,
    }
