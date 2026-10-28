"""Golden values frozen before the Phase 2 corporate-action work.

Corporate actions must not change anything for a series with none (NASDAQCOM
is an index with no splits or dividends). These pin the current paper path and
research fingerprint so any drift introduced by Phase 2 fails here first.
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ResearchConfig, RiskConfig, load_all
from signalforge.core.history import PriceHistory
from signalforge.data.archive import load_benchmark_series
from signalforge.data.providers import DEFAULT_ARCHIVE_DIR, SERIES_FILES
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import PaperRunner
from signalforge.pipeline import series_history

from tests.conftest import business_days, make_bar

D = Decimal


def _runner(warehouse, symbol, spec, history):
    return PaperRunner(
        warehouse=warehouse,
        research=ResearchConfig(starting_cash=D("100000"), universe=(symbol,)),
        risk=RiskConfig(),
        override_strategy=spec,
        override_reason="golden",
        start=history.trading_days[0],
    )


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "golden.duckdb"))
    yield w
    w.close()


@pytest.fixture(scope="module")
def nasdaq_history() -> PriceHistory:
    path = os.path.join(str(DEFAULT_ARCHIVE_DIR), f"{SERIES_FILES[0]}.csv")
    if not os.path.isfile(path):
        pytest.skip(f"{path} is licensed data and is not in the repo")
    return series_history(load_benchmark_series("NASDAQCOM"), symbol="NASDAQCOM").history


class TestNasdaqcomPaperPathIsFrozen:
    SPEC = ("ma_trend", {"fast_window": 20, "slow_window": 100,
                         "target_weight": D("0.05"), "symbols": ("NASDAQCOM",)})

    def test_final_equity_ledger_head_and_fills(self, warehouse, nasdaq_history):
        r = _runner(warehouse, "NASDAQCOM", self.SPEC, nasdaq_history)
        result = r.run(nasdaq_history, through=date(1988, 12, 31))
        portfolio = r.broker.portfolio()
        head = r.broker.ledger.head()
        statement = r.broker.statements[-1]
        assert result.sessions == 4524
        assert result.last_session == date(1988, 12, 30)
        assert head.length == 11047
        assert head.event_hash == "de0554f081b502b142500777a7ea3cef3f87f224631bc1114be3318702715fc7"
        assert len(portfolio.fills) == 36
        assert statement.equity == D("102417.974340")
        assert statement.cash == D("102417.974340")
        assert portfolio.total_fees == D("36.00")
        assert r.broker.ledger.verify() is True

    def test_the_train_window_fingerprint(self, nasdaq_history):
        cfg = load_all()
        fingerprint = BacktestEngine(cfg.research, cfg.risk).fingerprint(
            nasdaq_history, cfg.research.splits.train_start, cfg.research.splits.train_end
        )
        assert fingerprint == (
            "eda78f442b957376ff2b74367443fcff70872d7349a989ffdb1c134a219cc559"
        )


class TestFixturePaperPathIsFrozen:
    DAYS = business_days(date(2020, 1, 1), 60)
    SPEC = ("ma_trend", {"fast_window": 5, "slow_window": 15,
                         "target_weight": D("0.05"), "symbols": ("AAA",)})

    def test_equity_and_ledger_head(self, warehouse):
        history = PriceHistory.from_bars(
            [make_bar("AAA", d, D(100 + i), volume=10_000_000) for i, d in enumerate(self.DAYS)]
        )
        r = _runner(warehouse, "AAA", self.SPEC, history)
        r.run(history, through=self.DAYS[40])
        head = r.broker.ledger.head()
        statement = r.broker.statements[-1]
        assert (head.length, head.event_hash) == GOLDEN_AAA_HEAD
        assert statement.equity == GOLDEN_AAA_EQUITY
        assert len(r.broker.portfolio().fills) == GOLDEN_AAA_FILLS


GOLDEN_AAA_HEAD = (86, "e2b836518ece4420f1d53c7c704a89adbd76190ba5685dfec46ba54966b86754")
GOLDEN_AAA_EQUITY = D("101071.527500")
GOLDEN_AAA_FILLS = 1
