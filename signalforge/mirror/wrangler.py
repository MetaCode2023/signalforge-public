"""Thin, injectable runner around `wrangler d1 execute` for the market-data mirror.

Reads go through --command and are refused if cmd.exe could reinterpret them
(npx resolves to npx.cmd on Windows). Writes go only through --file. The
leaked CLOUDFLARE_API_TOKEN (and every CF_ / CLOUDFLARE_ variable) is scrubbed from the child environment so wrangler
falls back to its OAuth login.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

DATABASE = "signalforge-market-data"
ACCOUNT_ID = "YOUR_CLOUDFLARE_ACCOUNT_ID"
SCRUBBED_PREFIXES = ("CF_", "CLOUDFLARE_")
QUERY_TIMEOUT = 180
FILE_TIMEOUT = 900
MAX_COMMAND_CHARS = 7000
CMD_UNSAFE = '"%^&|<>!\r\n'

READ_FLAGS = ["--remote", "--json"]
FILE_FLAGS = ["--remote"]
FILE_TAIL = ["--yes"]


class WranglerError(RuntimeError):
    pass


def _json_from(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    offset = 0
    for line in text.splitlines(True):
        if line.lstrip()[:1] in ("[", "{"):
            try:
                return json.JSONDecoder().raw_decode(text[offset:].lstrip())[0]
            except ValueError:
                break
        offset += len(line)
    raise WranglerError("wrangler output was not JSON")


def _scrubbed(key: str) -> bool:
    return key.upper().startswith(SCRUBBED_PREFIXES)


class Wrangler:
    def __init__(self, run=subprocess.run, environ=None, npx=None, database=DATABASE):
        if database != DATABASE:
            raise ValueError(f"refusing database {database!r}; only {DATABASE!r} is allowed")
        self._run = run
        self._source_env = environ
        self._npx = npx or shutil.which("npx") or "npx"

    def _env(self) -> dict:
        source = os.environ if self._source_env is None else self._source_env
        env = {k: v for k, v in source.items() if not _scrubbed(k)}
        env["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID
        return env

    def _redact(self, text: str) -> str:
        source = os.environ if self._source_env is None else self._source_env
        for key, value in source.items():
            if _scrubbed(key) and value and len(value) >= 6:
                text = text.replace(value, "[redacted]")
        return text

    def _argv(self, *tail: str) -> list[str]:
        return [self._npx, "wrangler", "d1", "execute", DATABASE, *tail]

    def _exec(self, argv: list[str], timeout: int):
        try:
            proc = self._run(
                argv, env=self._env(), capture_output=True, text=True, encoding="utf-8",
                shell=False, stdin=subprocess.DEVNULL, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise WranglerError(f"wrangler timed out after {timeout}s") from None
        if proc.returncode != 0:
            raise WranglerError(self._failure(proc))
        return proc.stdout or ""

    def _failure(self, proc) -> str:
        detail = ""
        try:
            body = _json_from(proc.stdout or "")
            err = body.get("error", {}) if isinstance(body, dict) else {}
            notes = err.get("notes") or [{}]
            detail = f"code {err.get('code')}: {notes[0].get('text') or err.get('text') or ''}"
        except (WranglerError, AttributeError, TypeError):
            lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
            detail = lines[-1].strip() if lines else ""
        detail = self._redact(detail)[:300]
        return f"wrangler exited {proc.returncode}: {detail}"

    def query(self, sql: str) -> list[dict]:
        if len(sql) > MAX_COMMAND_CHARS:
            raise WranglerError(f"refusing a {len(sql)} char --command (limit {MAX_COMMAND_CHARS})")
        bad = [c for c in CMD_UNSAFE if c in sql]
        if bad:
            raise WranglerError(f"refusing --command containing cmd.exe-unsafe characters {bad!r}")
        out = self._exec(self._argv(*READ_FLAGS, "--command", sql), QUERY_TIMEOUT)
        body = _json_from(out)
        if not (isinstance(body, list) and body and isinstance(body[0], dict)):
            raise WranglerError("unexpected wrangler JSON shape")
        first = body[0]
        if first.get("success") is not True or not isinstance(first.get("results"), list):
            raise WranglerError("wrangler reported an unsuccessful query")
        return first["results"]

    def execute_file(self, path) -> None:
        self._exec(self._argv(*FILE_FLAGS, "--file", str(path), *FILE_TAIL), FILE_TIMEOUT)
