"""config/universes/*.yaml: the explicit, committed list a broad run may trade."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import yaml

from signalforge.config.models import SplitConfig, load_universe
from signalforge.core.errors import ConfigError

BASE = {
    "name": "broad",
    "source": "tiingo",
    "benchmark": "SPY",
    "symbols": ["AAA", "BBB", "CCC"],
    "excluded": {"ZZZ": "unverified split"},
    "load_start": "1992-01-02",
    "train_start": "1993-02-01",
    "eligibility": {
        "min_history_sessions": 252,
        "min_median_dollar_volume": "500000",
        "dollar_volume_window": 63,
    },
    "max_names": 20,
}


def write(tmp_path, **overrides):
    payload = {**BASE, **overrides}
    for key in [k for k, v in payload.items() if v is None]:
        del payload[key]
    path = tmp_path / "u.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def splits():
    return SplitConfig(
        train_start=date(1971, 2, 5), train_end=date(2004, 12, 31),
        validation_start=date(2005, 1, 1), validation_end=date(2014, 12, 31),
        test_start=date(2015, 1, 1), test_end=date(2026, 10, 23),
    )


def test_universe_loads_a_valid_file(tmp_path):
    u = load_universe(write(tmp_path))
    assert u.symbols == ("AAA", "BBB", "CCC")
    assert u.eligibility.min_median_dollar_volume == Decimal("500000")
    assert u.max_names == 20 and u.load_start == date(1992, 1, 2)


def test_universe_rejects_excluded_symbol_in_symbols(tmp_path):
    with pytest.raises(ConfigError, match="excluded"):
        load_universe(write(tmp_path, excluded={"BBB": "ETF"}))


def test_universe_rejects_duplicates(tmp_path):
    with pytest.raises(ConfigError, match="duplicate"):
        load_universe(write(tmp_path, symbols=["AAA", "BBB", "AAA"]))


def test_universe_rejects_benchmark_in_symbols(tmp_path):
    with pytest.raises(ConfigError, match="benchmark"):
        load_universe(write(tmp_path, symbols=["AAA", "SPY"]))


@pytest.mark.parametrize("key", ["test_start", "test_end", "validation_end", "train_end"])
def test_universe_rejects_unknown_keys_eg_test_start(tmp_path, key):
    with pytest.raises(ConfigError):
        load_universe(write(tmp_path, **{key: "2015-01-01"}))


def test_universe_train_start_must_fall_inside_train_window(tmp_path):
    u = load_universe(write(tmp_path))
    effective = u.effective_splits(splits())
    assert effective.train_start == date(1993, 2, 1)
    assert effective.train_end == date(2004, 12, 31)
    assert effective.validation_end == date(2014, 12, 31)
    assert effective.test_start == date(2015, 1, 1)
    for bad in ("1970-01-01", "2005-06-01"):
        outside = load_universe(write(tmp_path, train_start=bad, load_start="1960-01-01"))
        with pytest.raises(ConfigError, match="train_start"):
            outside.effective_splits(splits())


def test_universe_load_start_must_not_follow_train_start(tmp_path):
    with pytest.raises(ConfigError, match="load_start"):
        load_universe(write(tmp_path, load_start="1993-03-01"))


@pytest.mark.parametrize("name", ["Broad Universe", "has/slash", "", "-lead", "x" * 41])
def test_universe_name_must_be_slug(tmp_path, name):
    with pytest.raises(ConfigError, match="name"):
        load_universe(write(tmp_path, name=name))


def test_universe_rejects_bad_eligibility_and_max_names(tmp_path):
    with pytest.raises(ConfigError):
        load_universe(write(tmp_path, max_names=0))
    with pytest.raises(ConfigError):
        load_universe(write(tmp_path, eligibility={**BASE["eligibility"], "dollar_volume_window": 0}))
    with pytest.raises(ConfigError):
        load_universe(write(tmp_path, eligibility={**BASE["eligibility"], "min_median_dollar_volume": "-1"}))


def test_committed_broad_universe_loads_and_fits_the_research_splits():
    from pathlib import Path

    from signalforge.config.models import CONFIG_DIR, load_research

    u = load_universe(CONFIG_DIR / "universes" / "broad.yaml")
    assert u.name == "broad" and u.benchmark == "SPY" and u.max_names == 20
    assert len(u.symbols) > 400 and "AAPL" in u.symbols
    assert "ON" in u.symbols and "TRUE" not in u.symbols
    assert {"AES", "USB", "QQQ", "TLT"} <= set(u.excluded) and not set(u.excluded) & set(u.symbols)
    effective = u.effective_splits(load_research().splits)
    assert effective.train_start == date(1993, 2, 1)
    assert effective.validation_end == date(2014, 12, 31)
    assert u.eligibility.min_median_dollar_volume == Decimal("500000")


def test_universe_refuses_a_bare_yaml_boolean_symbol(tmp_path):
    lines = [
        "name: broad", "source: tiingo", "benchmark: SPY", "load_start: 1992-01-02",
        "train_start: 1993-02-01", "max_names: 20",
        "eligibility: {min_history_sessions: 252, min_median_dollar_volume: '500000', "
        "dollar_volume_window: 63}",
        "symbols:", "  - AAA", "  - ON",
    ]
    path = tmp_path / "u.yaml"
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    with pytest.raises(ConfigError, match="quote"):
        load_universe(path)


@pytest.mark.parametrize("field,value", [
    ("symbols", ["AAA", True]),
    ("symbols", ["AAA", None]),
    ("symbols", ["AAA", 12]),
    ("excluded", {True: "x"}),
    ("benchmark", False),
])
def test_universe_refuses_non_string_symbols_keys_and_benchmark(tmp_path, field, value):
    with pytest.raises(ConfigError, match="quote YAML 1.1 words like ON/YES/NO/NULL"):
        load_universe(write(tmp_path, **{field: value}))


def test_committed_broad_universe_symbols_all_have_tiingo_bars():
    import duckdb
    from signalforge.config.models import CONFIG_DIR

    db = CONFIG_DIR.parent / "data" / "warehouse" / "signalforge.duckdb"
    if not db.exists():
        pytest.skip("no local warehouse")
    u = load_universe(CONFIG_DIR / "universes" / "broad.yaml")
    con = duckdb.connect(str(db), read_only=True)
    try:
        have = {r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM bars WHERE source = 'tiingo'").fetchall()}
    finally:
        con.close()
    assert set(u.symbols) <= have
    assert u.benchmark in have


def test_universe_normalises_excluded_keys_before_the_overlap_check(tmp_path):
    with pytest.raises(ConfigError, match="excluded"):
        load_universe(write(tmp_path, excluded={"bbb ": "ETF"}))


def test_universe_normalises_benchmark_before_the_overlap_check(tmp_path):
    with pytest.raises(ConfigError, match="benchmark"):
        load_universe(write(tmp_path, benchmark="spy", symbols=["AAA", "SPY"]))


@pytest.mark.parametrize("value", [[], "AAPL"])
def test_universe_symbols_must_be_a_non_empty_list(tmp_path, value):
    with pytest.raises(ConfigError, match="symbols"):
        load_universe(write(tmp_path, symbols=value))
