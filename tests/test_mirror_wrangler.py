"""wrangler runner: argv, env scrubbing, output parsing, cmd.exe safety. Fake run only."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from signalforge.mirror import wrangler as w

SENTINEL = "sentinel-cf-token-ZZZ999"


class FakeRun:
    def __init__(self, stdout="[]", returncode=0, stderr="", exc=None):
        self.exc = exc
        self.calls = []
        self.result = SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.exc:
            raise self.exc
        return self.result


def ok(rows):
    return json.dumps([{"results": rows, "success": True, "meta": {}}])


def make(run, environ=None):
    env = {"PATH": "p", "CLOUDFLARE_API_TOKEN": SENTINEL, "CLOUDFLARE_API_KEY": SENTINEL,
           "CLOUDFLARE_EMAIL": "a@b.c", "KEEP": "1"} if environ is None else environ
    return w.Wrangler(run=run, environ=env, npx="NPX")


def test_env_scrubs_cloudflare_credentials_and_sets_account():
    run = FakeRun(ok([]))
    before = dict(os.environ)
    make(run).query("SELECT 1")
    env = run.calls[0][1]["env"]
    assert "CLOUDFLARE_API_TOKEN" not in env and "CLOUDFLARE_API_KEY" not in env
    assert "CLOUDFLARE_EMAIL" not in env
    assert env["CLOUDFLARE_ACCOUNT_ID"] == "YOUR_CLOUDFLARE_ACCOUNT_ID"
    assert env["KEEP"] == "1"
    assert dict(os.environ) == before


def test_environ_default_is_os_environ_without_mutation(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", SENTINEL)
    run = FakeRun(ok([]))
    w.Wrangler(run=run, npx="NPX").query("SELECT 1")
    assert "CLOUDFLARE_API_TOKEN" not in run.calls[0][1]["env"]
    assert os.environ["CLOUDFLARE_API_TOKEN"] == SENTINEL


def test_query_argv_exact_and_shell_false():
    run = FakeRun(ok([]))
    make(run).query("SELECT 1")
    argv, kwargs = run.calls[0]
    assert argv == ["NPX", "wrangler", "d1", "execute", "signalforge-market-data",
                    "--remote", "--json", "--command", "SELECT 1"]
    assert kwargs["shell"] is False


def test_query_parses_results():
    rows = [{"a": 1, "b": "x'y"}]
    assert make(FakeRun(ok(rows))).query("SELECT 1") == rows


@pytest.mark.parametrize("bad", list('"%^&|<>!') + ["\r", "\n"])
def test_query_refuses_cmd_unsafe_sql(bad):
    run = FakeRun(ok([]))
    with pytest.raises(w.WranglerError):
        make(run).query(f"SELECT 'a{bad}b'")
    assert run.calls == []


def test_query_refuses_long_sql():
    run = FakeRun(ok([]))
    with pytest.raises(w.WranglerError):
        make(run).query("SELECT '" + "a" * 7000 + "'")
    assert run.calls == []
    make(run).query("SELECT '" + "a" * 6900 + "'")


def test_execute_file_uses_file_and_yes():
    run = FakeRun("")
    make(run).execute_file("data/mirror/x.sql")
    argv, kwargs = run.calls[0]
    assert argv == ["NPX", "wrangler", "d1", "execute", "signalforge-market-data",
                    "--remote", "--file", "data/mirror/x.sql", "--yes"]
    assert kwargs["shell"] is False


def test_nonzero_exit_raises_without_env_values():
    run = FakeRun("", 1, "noise\n" + "boom " * 100 + "\n")
    with pytest.raises(w.WranglerError) as exc:
        make(run).query("SELECT 1")
    msg = str(exc.value)
    assert "1" in msg and SENTINEL not in msg and len(msg) < 400


def test_stderr_containing_a_secret_is_redacted():
    run = FakeRun("", 1, f"auth failed for {SENTINEL}")
    with pytest.raises(w.WranglerError) as exc:
        make(run).query("SELECT 1")
    assert SENTINEL not in str(exc.value)


def test_error_object_on_stdout_is_reported():
    body = json.dumps({"error": {"text": "x", "code": 7500,
                                 "notes": [{"text": "no such table: nope SQLITE_ERROR"}]}})
    run = FakeRun(body, 1, "")
    with pytest.raises(w.WranglerError) as exc:
        make(run).query("SELECT 1")
    assert "7500" in str(exc.value) and "no such table" in str(exc.value)


def test_unparseable_output_raises():
    for out in ("not json at all", "{}", "[]", '[{"success": false}]', '[{"results": 3}]'):
        with pytest.raises(w.WranglerError):
            make(FakeRun(out)).query("SELECT 1")


def test_leading_log_line_is_tolerated():
    out = "some banner\n" + ok([{"n": 1}])
    assert make(FakeRun(out)).query("SELECT 1") == [{"n": 1}]


def test_refuses_other_database_name():
    with pytest.raises(ValueError):
        w.Wrangler(run=FakeRun(), environ={}, npx="NPX", database="other-db")


def test_env_scrubs_every_cf_and_cloudflare_prefixed_key():
    run = FakeRun(ok([]))
    env = {"CF_API_TOKEN": SENTINEL, "cf_api_key": SENTINEL, "CF_EMAIL": "a@b.c",
           "CLOUDFLARE_ACCOUNT_ID": "other", "CLOUDFLARE_WHATEVER": "x", "KEEP": "1", "CFX": "keepme"}
    make(run, env).query("SELECT 1")
    sent = run.calls[0][1]["env"]
    assert set(sent) == {"KEEP", "CFX", "CLOUDFLARE_ACCOUNT_ID"}
    assert sent["CLOUDFLARE_ACCOUNT_ID"] == "YOUR_CLOUDFLARE_ACCOUNT_ID"


def test_redaction_covers_values_of_all_dropped_keys():
    run = FakeRun("", 1, f"bad credential {SENTINEL}")
    with pytest.raises(w.WranglerError) as exc:
        make(run, {"CF_API_TOKEN": SENTINEL}).query("SELECT 1")
    assert SENTINEL not in str(exc.value)


def test_run_kwargs_stdin_devnull_and_timeouts():
    run = FakeRun(ok([]))
    wr = make(run)
    wr.query("SELECT 1")
    wr.execute_file("x.sql")
    q, f = run.calls[0][1], run.calls[1][1]
    assert q["stdin"] is subprocess.DEVNULL and f["stdin"] is subprocess.DEVNULL
    assert q["timeout"] == w.QUERY_TIMEOUT == 180
    assert f["timeout"] == w.FILE_TIMEOUT == 900


def test_timeout_becomes_wrangler_error():
    run = FakeRun(exc=subprocess.TimeoutExpired(cmd="x", timeout=180))
    with pytest.raises(w.WranglerError, match="timed out"):
        make(run).query("SELECT 1")
    with pytest.raises(w.WranglerError, match="timed out"):
        make(run).execute_file("x.sql")
