"""Signed sender: exact signature bytes, headers, outcomes, and secret hygiene (fake session only)."""

import hashlib
import hmac
import logging
import re
import uuid
from datetime import datetime, timezone

import pytest

from signalforge.reports import events as ev
from signalforge.reports import sender
from signalforge.reports.auth import ReportingCredentials, authentication_headers

NOW = datetime(2026, 10, 28, 15, 30, 5, tzinfo=timezone.utc)
CLIENT_ID = "sentinel-client-id-AAA"
CLIENT_SECRET = "sentinel-client-secret-BBB"
INGEST = "sentinel-ingest-secret-CCC"
SENTINELS = (CLIENT_ID, CLIENT_SECRET, INGEST)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(ev, "utcnow", lambda: NOW)


def creds():
    return ReportingCredentials(CLIENT_ID, CLIENT_SECRET, INGEST)


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses, raises=None):
        self.responses = list(responses)
        self.calls = []
        self.raises = raises

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.raises:
            raise self.raises
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def event():
    return ev.build_heartbeat(NOW, "e2e-test-2026-10-28")


def ok_response():
    return FakeResponse(202, {"ok": True, "event_id": "x", "mode": "paper", "is_test": True})


def test_signature_matches_independent_hmac_over_exact_bytes():
    session = FakeSession(ok_response())
    e = event()
    sender.send(e, session, creds(), clock=lambda: 1_790_000_000)
    url, kw = session.calls[0]
    body = kw["data"]
    assert body == ev.serialize(e)
    h = kw["headers"]
    expected = hmac.new(
        INGEST.encode(), f"v1:{h['x-signalforge-timestamp']}:{h['x-signalforge-nonce']}:".encode() + body,
        hashlib.sha256).hexdigest()
    assert h["x-signalforge-signature"] == "v1=" + expected


def test_headers_well_formed_and_request_fixed():
    session = FakeSession(ok_response())
    sender.send(event(), session, creds(), clock=lambda: 1_790_000_000)
    url, kw = session.calls[0]
    assert url == "https://signalforge-api.example.com/v1/ingest"
    assert kw["allow_redirects"] is False
    h = kw["headers"]
    assert h["Content-Type"] == "application/json"
    assert h["CF-Access-Client-Id"] == CLIENT_ID and h["CF-Access-Client-Secret"] == CLIENT_SECRET
    assert re.fullmatch(r"\d{10}", h["x-signalforge-timestamp"])
    assert h["x-signalforge-timestamp"] == "1790000000"
    nonce = h["x-signalforge-nonce"]
    assert len(nonce) == 36 and re.fullmatch(r"[a-f0-9-]{36}", nonce) and uuid.UUID(nonce).version == 4
    assert re.fullmatch(r"v1=[0-9a-f]{64}", h["x-signalforge-signature"])


def test_fresh_nonce_each_send_and_timestamp_uses_clock():
    session = FakeSession(ok_response())
    times = iter([1_790_000_000, 1_790_000_001])
    sender.send(event(), session, creds(), clock=lambda: next(times))
    sender.send(event(), session, creds(), clock=lambda: next(times))
    a, b = (c[1]["headers"] for c in session.calls)
    assert a["x-signalforge-nonce"] != b["x-signalforge-nonce"]
    assert a["x-signalforge-timestamp"] != b["x-signalforge-timestamp"]


def test_default_nonce_in_auth_module_is_a_uuid4():
    nonce = authentication_headers(b"{}", creds())["x-signalforge-nonce"]
    assert len(nonce) == 36 and uuid.UUID(nonce).version == 4


def test_202_is_success():
    out = sender.send(event(), FakeSession(ok_response()), creds())
    assert out.ok and not out.duplicate and out.status_code == 202
    assert out.event_type == "heartbeat" and out.event_id == event()["event_id"]


def test_409_duplicate_event_is_success_but_replayed_request_is_failure():
    dup = sender.send(event(), FakeSession(FakeResponse(409, {"ok": False, "error": "duplicate_event"})), creds())
    assert dup.ok and dup.duplicate and dup.error_code == "duplicate_event"
    rep = sender.send(event(), FakeSession(FakeResponse(409, {"ok": False, "error": "replayed_request"})), creds())
    assert not rep.ok and not rep.duplicate and rep.error_code == "replayed_request"


@pytest.mark.parametrize("status", [400, 401, 413, 415, 422, 500, 503])
def test_failure_statuses(status):
    out = sender.send(event(), FakeSession(FakeResponse(status, {"error": "bad_thing"})), creds())
    assert not out.ok and out.status_code == status and out.error_code == "bad_thing"


def test_redirect_and_unparseable_body_are_failures():
    out = sender.send(event(), FakeSession(FakeResponse(302)), creds())
    assert not out.ok and out.status_code == 302 and out.error_code is None


def test_hostile_error_text_is_not_carried_into_the_outcome():
    out = sender.send(event(), FakeSession(FakeResponse(400, {"error": "x" * 5 + " " + INGEST})), creds())
    assert INGEST not in repr(out) and out.error_code is None


def test_network_exception_text_never_surfaces():
    session = FakeSession(raises=RuntimeError(f"boom {INGEST} {CLIENT_SECRET}"))
    out = sender.send(event(), session, creds())
    assert not out.ok and out.status_code == 0 and out.error_code == "network_error"
    assert not any(s in repr(out) for s in SENTINELS)


def test_invalid_event_is_never_sent():
    session = FakeSession(ok_response())
    bad = event()
    bad["is_test"] = True
    with pytest.raises(ev.EventInvalid):
        sender.send(bad, session, creds())
    assert session.calls == []


def test_sending_logs_only_safe_metadata(caplog):
    caplog.set_level(logging.DEBUG)
    sender.send(event(), FakeSession(FakeResponse(401, {"error": "unauthorized"})), creds())
    text = caplog.text
    assert "heartbeat" in text and "401" in text
    assert not any(s in text for s in SENTINELS)


# ------------------------------------------------------------------ credentials

def write_env(path, **values):
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")


def full_env(**over):
    base = {
        "SIGNALFORGE_ACCESS_CLIENT_ID": CLIENT_ID,
        "SIGNALFORGE_ACCESS_CLIENT_SECRET": CLIENT_SECRET,
        "SIGNALFORGE_INGEST_SECRET": INGEST,
    }
    base.update(over)
    return base


def test_load_credentials_parses_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nOTHER=1\n"
        f'SIGNALFORGE_ACCESS_CLIENT_ID="{CLIENT_ID}"\n'
        f"SIGNALFORGE_ACCESS_CLIENT_SECRET = {CLIENT_SECRET}\n"
        f"SIGNALFORGE_INGEST_SECRET={INGEST}\n", encoding="utf-8")
    c = sender.load_credentials(path, environ={})
    assert (c.access_client_id, c.access_client_secret, c.ingest_secret) == SENTINELS


@pytest.mark.parametrize("missing", [
    "SIGNALFORGE_ACCESS_CLIENT_ID", "SIGNALFORGE_ACCESS_CLIENT_SECRET", "SIGNALFORGE_INGEST_SECRET"])
def test_missing_variable_fails_closed_naming_only_the_variable(tmp_path, missing):
    values = full_env()
    del values[missing]
    path = tmp_path / ".env"
    write_env(path, **values)
    with pytest.raises(sender.MissingCredentials) as caught:
        sender.load_credentials(path, environ={})
    assert missing in str(caught.value)
    assert not any(s in str(caught.value) for s in SENTINELS)


def test_blank_value_and_missing_file_fail_closed(tmp_path):
    path = tmp_path / ".env"
    write_env(path, **full_env(SIGNALFORGE_INGEST_SECRET=""))
    with pytest.raises(sender.MissingCredentials) as caught:
        sender.load_credentials(path, environ={})
    assert "SIGNALFORGE_INGEST_SECRET" in str(caught.value)
    with pytest.raises(sender.MissingCredentials):
        sender.load_credentials(tmp_path / "absent.env", environ={})


def test_environment_fills_what_the_file_lacks(tmp_path):
    path = tmp_path / ".env"
    write_env(path, SIGNALFORGE_ACCESS_CLIENT_ID=CLIENT_ID, SIGNALFORGE_ACCESS_CLIENT_SECRET=CLIENT_SECRET)
    c = sender.load_credentials(path, environ={"SIGNALFORGE_INGEST_SECRET": INGEST})
    assert c.ingest_secret == INGEST


def test_env_file_with_bom_loads(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(f"{k}={v}" for k, v in full_env().items()) + "\n", encoding="utf-8-sig")
    c = sender.load_credentials(path, environ={})
    assert c.ingest_secret == INGEST and c.access_client_id == CLIENT_ID


def test_parser_retains_only_the_three_variables(tmp_path):
    path = tmp_path / ".env"
    write_env(path, **full_env(UNRELATED_TOKEN="other-secret-value"))
    parsed = sender._parse_env(path)
    assert set(parsed) == set(sender.VARIABLES)
    assert "other-secret-value" not in repr(parsed)


def test_credentials_repr_has_no_secret():
    assert not any(s in repr(creds()) for s in SENTINELS)


def test_post_uses_timeout_30_and_never_disables_verification():
    session = FakeSession(ok_response())
    sender.send(event(), session, creds())
    kw = session.calls[0][1]
    assert kw["timeout"] == 30
    assert "verify" not in kw
