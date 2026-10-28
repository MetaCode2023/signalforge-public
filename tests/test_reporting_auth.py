"""Synthetic signing fixtures; these bodies are not a deployed API contract."""

import hashlib
import hmac

import pytest

from signalforge.reports.auth import ReportingCredentials, authentication_headers


def credentials():
    return ReportingCredentials("fixture-client", "fixture-access", "fixture-signing")


def test_documented_authentication_signs_exact_serialized_bytes():
    body = b'{"fixture":true}'
    headers = authentication_headers(body, credentials(), timestamp=123, nonce="fixture-nonce")
    expected = hmac.new(b"fixture-signing", b"v1:123:fixture-nonce:" + body, hashlib.sha256).hexdigest()
    assert headers["x-signalforge-signature"] == "v1=" + expected
    assert headers["CF-Access-Client-Id"] == "fixture-client"
    assert headers["CF-Access-Client-Secret"] == "fixture-access"


def test_whitespace_changes_signature_and_default_nonce_is_fresh():
    one = authentication_headers(b"{}", credentials(), timestamp=123, nonce="same")
    two = authentication_headers(b"{ }", credentials(), timestamp=123, nonce="same")
    assert one["x-signalforge-signature"] != two["x-signalforge-signature"]
    assert authentication_headers(b"{}", credentials())["x-signalforge-nonce"] != authentication_headers(b"{}", credentials())["x-signalforge-nonce"]


def test_credential_representation_never_prints_values():
    representation = repr(credentials())
    assert "fixture-client" not in representation
    assert "fixture-access" not in representation
    assert "fixture-signing" not in representation


@pytest.mark.parametrize("kwargs", [{"timestamp": -1}, {"timestamp": True}, {"nonce": "bad\r\nheader"}])
def test_invalid_auth_metadata_is_refused(kwargs):
    with pytest.raises(ValueError):
        authentication_headers(b"{}", credentials(), **kwargs)


def test_credentials_and_body_are_validated_without_echoing_values():
    with pytest.raises(ValueError) as caught:
        ReportingCredentials("private\nfixture", "x", "y")
    assert "private" not in str(caught.value)
    with pytest.raises(TypeError):
        authentication_headers("{}", credentials())
