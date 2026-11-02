"""The Assay study: honest selection, the hindsight trap, the fair coins, the book.

The two synthetic worlds are the core evidence. With no insider edge, the
honest "smart" picks must look like random picks while the hindsight picks
look brilliant; with a planted edge, the honest picks must find it. If the
first fails the study would flatter strategies; if the second fails it could
not detect a real edge.
"""

from __future__ import annotations

import ast
import json
import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from signalforge.core.errors import ValidationError
from signalforge.insiders.events import Purchase, normalize_ticker, resolve_symbols, select
from signalforge.insiders.study import (
    Params,
    PricePanel,
    arm_cluster,
    arm_smart_hindsight,
    arm_smart_past,
    portfolio,
    run,
    score,
)
from tests.insider_support import business_days, world

PACKAGE = Path(__file__).resolve().parents[1] / "signalforge"


@pytest.fixture(scope="module")
def no_edge():
    purchases, panel, _ = world(skilled_share=0.0, seed=7)
    p = Params(window_start=date(2008, 1, 1), window_end=panel.days[-1], draws=300)
    return run(purchases, panel, p)


@pytest.fixture(scope="module")
def planted():
    purchases, panel, _ = world(skilled_share=0.3, skill=0.06, seed=11)
    p = Params(window_start=date(2008, 1, 1), window_end=panel.days[-1], draws=300)
    return run(purchases, panel, p)


class TestWorlds:
    def test_without_an_edge_honest_picks_look_like_random_picks(self, no_edge):
        assert no_edge.nulls["random_insiders_vs_smart_past"]["p_value"] > 0.05

    def test_without_an_edge_hindsight_picks_still_look_brilliant(self, no_edge):
        hind, past = no_edge.arms["smart_hindsight"][21], no_edge.arms["smart_past"][21]
        assert hind.mean > past.mean + 0.015 and hind.hit_rate > 0.65
        assert no_edge.nulls["random_insiders_vs_smart_hindsight"]["p_value"] < 0.01
        assert no_edge.portfolios["smart_hindsight"].cagr > 2 * no_edge.portfolios["smart_past"].cagr

    def test_with_a_planted_edge_honest_picks_find_it(self, planted):
        assert planted.nulls["random_insiders_vs_smart_past"]["p_value"] < 0.05
        assert planted.arms["smart_past"][21].mean > planted.arms["all"][21].mean

    def test_the_random_stock_coin_measures_the_survivor_drift(self, no_edge):
        null = no_edge.nulls["random_stock_same_days"]
        assert null["median"] > 0  # synthetic stocks drift above SPY, as survivors do
        assert null["p_value"] > 0.05


def _world_small():
    days = business_days(date(2010, 1, 4), 400)
    rng = np.random.default_rng(3)
    adj = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (400, 3)), axis=0))
    return PricePanel(days=tuple(days), symbols=("AAA", "BBB", "SPY"), adj=adj, close=adj.copy()), days


def _buy(acc, owner, day, sym="AAA", panel=None, col=0):
    price = float(panel.close[panel.days.index(day) - 1, col]) if panel else 10.0
    return Purchase(acc, day, day - timedelta(days=1) if day.weekday() else day - timedelta(days=3), "1", sym,
                    owner, True, False, False, 100_000.0, price)


class TestPointInTime:
    def test_entry_is_the_day_after_the_filing(self):
        panel, days = _world_small()
        p = Params(window_start=days[0], window_end=days[-1], horizons=(5, 21), primary=21)
        scored, _ = score([_buy("a", "o", days[50], panel=panel)], panel, p)
        assert scored[0].signal == 50 and scored[0].entry == 51 and scored[0].exits == (56, 72)

    def test_smart_past_never_uses_an_outcome_that_was_not_yet_known(self):
        panel, days = _world_small()
        p = Params(window_start=days[0], window_end=days[-1], horizons=(21,), primary=21, min_history=2, min_hit=0.5)
        buys = [_buy(f"a{i}", "o", days[d], panel=panel) for i, d in enumerate((10, 40, 70, 75))]
        scored, _ = score(buys, panel, p)
        forced = [replace(e, excess=(0.05,)) for e in scored]
        chosen = {e.accession for e in arm_smart_past(forced, panel, p, 0)}
        # a2 (day 70) has two closed winners (exits 32 and 62 < 70); a3 (day 75) too.
        # Neither a0 nor a1 had two closed buys behind them.
        assert chosen == {"a2", "a3"}
        # Changing the outcome of a buy that closes AFTER a2's signal cannot change a2's selection.
        later = [replace(e, excess=(-0.5,)) if e.accession == "a3" else e for e in forced]
        assert "a2" in {e.accession for e in arm_smart_past(later, panel, p, 0)}

    def test_hindsight_selection_uses_outcomes_from_the_future(self):
        panel, days = _world_small()
        p = Params(window_start=days[0], window_end=days[-1], horizons=(21,), primary=21, min_history=2, min_hit=0.5)
        buys = [_buy(f"a{i}", "o", days[d], panel=panel) for i, d in enumerate((10, 200))]
        scored, _ = score(buys, panel, p)
        forced = [replace(e, excess=(0.05,)) for e in scored]
        assert {e.accession for e in arm_smart_hindsight(forced, panel, p, 0)} == {"a0", "a1"}
        assert arm_smart_past(forced, panel, p, 0) == []

    def test_cluster_needs_two_different_insiders_within_the_span(self):
        panel, days = _world_small()
        p = Params(window_start=days[0], window_end=days[-1], horizons=(21,), primary=21)
        buys = [_buy("a", "o1", days[50], panel=panel), _buy("b", "o1", days[52], panel=panel),
                _buy("c", "o2", days[55], panel=panel), _buy("d", "o3", days[60], panel=panel)]
        scored, _ = score(buys, panel, p)
        assert [e.accession for e in arm_cluster(scored, panel, p, 0)] == ["c"]


class TestChecks:
    def test_outcomes_past_the_window_are_unknown_not_guessed(self):
        panel, days = _world_small()
        p = Params(window_start=days[0], window_end=days[100], horizons=(5, 21), primary=21)
        scored, _ = score([_buy("a", "o", days[90], panel=panel)], panel, p)
        assert math.isfinite(scored[0].excess[0]) and math.isnan(scored[0].excess[1])

    def test_a_filed_price_far_from_the_market_is_dropped(self):
        panel, days = _world_small()
        p = Params(window_start=days[0], window_end=days[-1], horizons=(21,), primary=21)
        bad = replace(_buy("a", "o", days[50], panel=panel), price=1000.0)
        scored, dropped = score([bad], panel, p)
        assert scored == [] and dropped == {"filed price far from the market close": 1}

    def test_an_unexplained_jump_is_dropped_unless_acknowledged(self):
        panel, days = _world_small()
        adj = panel.adj.copy()
        adj[60:, 0] *= 2.0
        jumpy = PricePanel(days=panel.days, symbols=panel.symbols, adj=adj, close=adj.copy())
        p = Params(window_start=days[0], window_end=days[-1], horizons=(21,), primary=21)
        buy = _buy("a", "o", days[50], panel=jumpy)
        assert score([buy], jumpy, p)[1] == {"unexplained price jump while held": 1}
        assert len(score([buy], jumpy, p, acknowledged={("AAA", days[60])})[0]) == 1

    def test_costs_are_charged_both_ways(self):
        panel, days = _world_small()
        p0 = Params(window_start=days[0], window_end=days[-1], horizons=(21,), primary=21, cost_bps=0)
        p5 = replace(p0, cost_bps=5)
        buy = _buy("a", "o", days[50], panel=panel)
        assert score([buy], panel, p0)[0][0].excess[0] - score([buy], panel, p5)[0][0].excess[0] == pytest.approx(0.001)


def test_portfolio_matches_a_hand_computed_book():
    days = business_days(date(2010, 1, 4), 10)
    adj = np.array([[100, 100], [110, 101], [121, 102], [121, 103], [121, 104],
                    [121, 105], [121, 106], [121, 107], [121, 108], [121, 109]], dtype=float)
    panel = PricePanel(days=tuple(days), symbols=("AAA", "SPY"), adj=adj, close=adj.copy())
    p = Params(window_start=days[0], window_end=days[-1], horizons=(2,), primary=2, cost_bps=0)
    scored, _ = score([Purchase("a", days[0], days[0], "1", "AAA", "o", True, False, False, 1e5, 100.0)], panel, p)
    book = portfolio(scored, panel, p, 0, points=100)
    # entry at close of day 1 (110), held days 2..3: +10% then 0%, cash after
    assert book.equity[0] == pytest.approx(100_000.0)
    assert book.equity[1] == pytest.approx(110_000.0)
    assert book.total_return == pytest.approx(0.10)
    assert book.invested_share == pytest.approx(2 / 9)


class TestEvents:
    def _p(self, acc, cik, sym, when, **kw):
        base = dict(accession=acc, filing_date=when, trans_date=when, issuer_cik=cik, symbol=sym, owner_cik="o",
                    is_director=True, is_officer=False, is_ten_pct=False, value=50_000.0, price=10.0)
        return Purchase(**{**base, **kw})

    def test_a_renamed_company_keeps_its_history(self):
        ps = [self._p("1", "9", "FB", date(2012, 6, 1)), self._p("2", "9", "META", date(2023, 1, 5))]
        kept, _ = resolve_symbols(ps, ["META", "SPY"])
        assert [k.symbol for k in kept] == ["META", "META"]

    def test_a_reused_ticker_belongs_to_its_latest_user(self):
        ps = [self._p("1", "5", "XYZ", date(2007, 1, 2)), self._p("2", "6", "XYZ", date(2020, 1, 2))]
        kept, dropped = resolve_symbols(ps, ["XYZ"])
        assert [k.accession for k in kept] == ["2"] and dropped == {"ticker now belongs to another company": 1}

    def test_class_share_spellings_match(self):
        assert normalize_ticker("brk.b") == normalize_ticker("BRK/B") == "BRK-B"

    def test_selection_keeps_officers_and_directors_over_the_floor(self):
        ps = [self._p("1", "1", "A", date(2010, 1, 4)),
              self._p("2", "1", "A", date(2010, 1, 4), value=10_000.0),
              self._p("3", "1", "A", date(2010, 1, 4), is_director=False, is_ten_pct=True)]
        kept, dropped = select(ps)
        assert [k.accession for k in kept] == ["1"]
        assert dropped == {"under $25,000": 1, "not an officer or director": 1}


def test_params_refuse_nonsense():
    with pytest.raises(ValidationError):
        Params(window_start=date(2010, 1, 1), window_end=date(2009, 1, 1))
    with pytest.raises(ValidationError):
        Params(window_start=date(2008, 1, 1), window_end=date(2009, 1, 1), horizons=(5,), primary=21)


NETWORK = {"socket", "requests", "urllib", "urllib3", "http", "httpx", "aiohttp"}


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_insiders_package_imports_no_network_library():
    offenders = {p.name: sorted(_imports(p) & NETWORK) for p in (PACKAGE / "insiders").rglob("*.py") if _imports(p) & NETWORK}
    assert offenders == {}


def test_paper_strategy_and_risk_layers_do_not_import_insiders():
    for root in ("paper", "strategies", "risk"):
        for p in (PACKAGE / root).rglob("*.py"):
            assert "signalforge.insiders" not in p.read_text(encoding="utf-8"), p


def test_the_cli_refuses_the_held_out_window(capsys):
    from signalforge.insider_cli import main

    assert main(["trial", "--window", "test"]) == 2
    assert "run-holdout" in capsys.readouterr().err


def test_cli_trial_runs_end_to_end_on_a_real_warehouse(tmp_path, monkeypatch):
    """Synthetic purchases in the build's parquet layout, a real DuckDB warehouse, the real CLI."""
    import polars as pl

    from signalforge.data.storage import Warehouse
    from signalforge.insider_cli import main
    from tests.conftest import make_bar
    from decimal import Decimal

    purchases, panel, _ = world(n_days=2400, n_symbols=25, n_owners=150, seed=5, start=date(2006, 1, 2))
    db = tmp_path / "wh.duckdb"
    w = Warehouse(str(db))
    bars = []
    for j, sym in enumerate(panel.symbols):
        for i, d in enumerate(panel.days):
            px = Decimal(str(round(float(panel.adj[i, j]), 4)))
            bars.append(make_bar(sym, d, px, source="tiingo"))
    w.upsert_bars(bars)
    w.close()
    out_dir = tmp_path / "data" / "insiders"
    out_dir.mkdir(parents=True)
    pl.DataFrame([{k: getattr(x, k) for k in Purchase.__slots__} for x in purchases]).write_parquet(out_dir / "purchases.parquet")
    (out_dir / "manifest.json").write_text(json.dumps({"transaction_rows": 9999, "purchases": len(purchases)}))
    monkeypatch.chdir(tmp_path)
    code = main(["trial", "--window", "validation", "--db", str(db), "--source", "tiingo",
                 "--config-dir", str(PACKAGE.parent / "config"), "--draws", "50",
                 "--purchases", str(out_dir / "purchases.parquet")])
    assert code == 0
    rec = json.loads(next((tmp_path / "reports" / "insider_trials").glob("*.json")).read_text())
    assert rec["kind"] == "insider_assay" and rec["sizes"]["all"] > 100
    assert rec["funnel"][0] == {"step": "Form 4 transaction rows read", "count": 9999}
    assert rec["params"]["window_end"] <= "2014-12-31"
    page = (tmp_path / "dashboard" / "build" / "assay.html").read_text()
    assert "ASSAY" in page and "<script" not in page
