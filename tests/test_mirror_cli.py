"""mirror_cli: subcommands, exit codes, and that no environment value reaches the output."""

from pathlib import Path

import pytest

from signalforge import mirror_cli
from signalforge.mirror.wrangler import Wrangler
from tests.mirror_support import FakeD1, make_warehouse

SENTINEL = "sentinel-cf-token-QQQ777"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "w.duckdb"
    make_warehouse(path).close()
    return str(path)


@pytest.fixture
def out(tmp_path):
    return tmp_path / "mirror"


def cli(args, d1, out, **kw):
    return mirror_cli.main(args, d1=d1, out_dir=out, **kw)


def test_init_creates_schema_and_is_idempotent(out):
    d1 = FakeD1(init=False)
    assert cli(["init"], d1, out) == 0
    assert cli(["init"], d1, out) == 0
    assert d1.one("SELECT count(*) FROM mirror_state") == 2
    assert d1.one("SELECT count(*) FROM sqlite_master WHERE name='bars'") == 1


def test_init_refuses_version_mismatch(out, capsys):
    d1 = FakeD1()
    d1.con.execute("UPDATE mirror_state SET value='9' WHERE key='schema_version'")
    assert cli(["init"], d1, out) == 2
    assert "schema_version" in capsys.readouterr().err
    assert d1.files == []


def test_sync_dry_run_no_writes(db, out, capsys):
    d1 = FakeD1()
    assert cli(["sync", "--db", db, "--dry-run"], d1, out) == 0
    assert d1.files == [] and d1.one("SELECT count(*) FROM bars") == 0
    assert "dry run" in capsys.readouterr().out


def test_load_is_sync(db, out):
    d1 = FakeD1()
    assert cli(["load", "--db", db, "--rows-per-file", "30"], d1, out) == 0
    assert d1.one("SELECT count(*) FROM bars") == 36
    assert len(d1.files) > 2


def test_sync_max_files_pilot_writes_exactly_one_file_and_exits_3(db, out, capsys):
    d1 = FakeD1()
    assert cli(["sync", "--db", db, "--max-files", "1", "--rows-per-file", "10"], d1, out) == 3
    assert len(d1.files) == 1
    assert "incomplete: rerun sync to continue" in capsys.readouterr().out
    assert cli(["sync", "--db", db, "--rows-per-file", "10"], d1, out) == 0


def test_dry_run_exits_0_even_though_nothing_was_written(db, out):
    assert cli(["sync", "--db", db, "--dry-run", "--max-files", "1"], FakeD1(), out) == 0


@pytest.mark.parametrize("bad", ["0", "-1", "x"])
def test_max_files_below_one_rejected(db, out, bad):
    with pytest.raises(SystemExit):
        cli(["sync", "--db", db, "--max-files", bad], FakeD1(), out)


def test_sync_guard_refusal_exits_2(db, out, capsys):
    d1 = FakeD1()
    assert cli(["sync", "--db", db], d1, out) == 0
    import duckdb

    con = duckdb.connect(db)
    con.execute("DELETE FROM corporate_actions WHERE effective_date < DATE '2020-01-05'")
    con.close()
    assert cli(["sync", "--db", db, "--max-deletes", "1"], d1, out) == 2
    assert "max-deletes" in capsys.readouterr().err


def test_verify_exit_codes_0_and_1(db, out, capsys):
    d1 = FakeD1()
    cli(["sync", "--db", db], d1, out)
    assert cli(["verify", "--db", db, "--sample", "10"], d1, out) == 0
    d1.con.execute("UPDATE bars SET close='999.000000' WHERE symbol='AAA' AND trade_date='2024-01-03'")
    capsys.readouterr()
    assert cli(["verify", "--db", db, "--sample", "10"], d1, out) == 1
    assert "mismatch" in capsys.readouterr().out


def test_postcondition_failure_exits_1(db, out):
    d1 = FakeD1()
    d1.drop_writes = True
    assert cli(["sync", "--db", db], d1, out) == 1


def test_missing_warehouse_returns_2(tmp_path, out, capsys):
    assert cli(["sync", "--db", str(tmp_path / "nope.duckdb")], FakeD1(), out) == 2
    assert "not found" in capsys.readouterr().err


def test_locked_warehouse_returns_2(db, out, capsys):
    import duckdb

    holder = duckdb.connect(db)  # a writer holds the lock
    try:
        assert cli(["sync", "--db", db], FakeD1(), out) == 2
    finally:
        holder.close()
    assert "could not open" in capsys.readouterr().err


def test_output_never_contains_env_sentinel(db, out, capsys):
    d1 = FakeD1()
    calls = []
    environ = {"PATH": "p", "CLOUDFLARE_API_TOKEN": SENTINEL, "OTHER_SECRET": SENTINEL}
    runner = d1.as_runner(calls)
    for args in (["init"], ["sync", "--db", db], ["verify", "--db", db]):
        assert mirror_cli.main(args, runner=runner, environ=environ, out_dir=out) == 0
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert calls and all("CLOUDFLARE_API_TOKEN" not in kw["env"] for _, kw in calls)
    assert all(kw["shell"] is False for _, kw in calls)


def test_default_d1_is_wrangler(monkeypatch):
    assert isinstance(mirror_cli.build_d1(None, None), Wrangler)


def test_gitignore_covers_data_mirror():
    text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "data/mirror/" in text.splitlines()


@pytest.mark.parametrize("flag,bad", [("--rows-per-file", "0"), ("--rows-per-file", "-5"),
                                      ("--max-deletes", "-1")])
def test_bad_sizes_are_rejected_by_the_parser(db, out, flag, bad):
    with pytest.raises(SystemExit):
        cli(["sync", "--db", db, flag, bad], FakeD1(), out)
