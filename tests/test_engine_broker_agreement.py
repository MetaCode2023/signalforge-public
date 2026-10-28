"""The backtest engine and the paper broker must agree.

They are two independent execution paths over the same rules: the engine runs
its own event loop, and the paper runner drives `PaperBroker` session by
session. Both size from a target weight, both route through the same risk
engine and fill model, and both reconcile at fill time.

If they disagree on the same data and strategy, one of them is wrong, and
nothing else in the project would tell you which. This is the cheapest
high-value check available, because each path was built and tested on its
own and neither was ever compared with the other.

Noticed because a real paper run through 2005 returned +12.7% where the
engine's train window through 2004 returned +13.0% — close enough to be
worth checking properly, and close enough that a quiet divergence would have
been invisible.
"""

from datetime import date
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.data.storage import Warehouse
from signalforge.paper.runner import PaperRunner
from signalforge.strategies.registry import build

from tests.conftest import business_days, make_bar

D = Decimal
DAYS = business_days(date(2020, 1, 1), 260)

SPECS = [
    (
        "ma_trend",
        {"fast_window": 10, "slow_window": 30, "target_weight": D("0.05"),
         "symbols": ("AAA",)},
    ),
    (
        "buy_and_hold",
        {"symbols": ("AAA",), "target_weight": D("0.05")},
    ),
    (
        "zscore_mean_reversion",
        {"window": 20, "entry_z": D("2"), "exit_z": D("0"),
         "target_weight": D("0.05"), "max_holding_days": 20, "symbols": ("AAA",)},
    ),
]
IDS = [s[0] for s in SPECS]


def research() -> ResearchConfig:
    return ResearchConfig(
        starting_cash=D("100000"),
        universe=("AAA",),
        execution=ExecutionConfig(),
    )


@pytest.fixture(params=["trending", "choppy", "declining"])
def history(request) -> PriceHistory:
    shape = request.param
    bars = []
    for i, day in enumerate(DAYS):
        if shape == "trending":
            close = D(100) + D(i) * D("0.4")
        elif shape == "declining":
            close = D(300) - D(i) * D("0.5")
        else:
            close = D(150) + D(((i * 29) % 41) - 20)
        bars.append(make_bar("AAA", day, close, volume=10_000_000))
    return PriceHistory.from_bars(bars)


def paper_equity(history, strategy_id, params, tmp_path):
    wh = Warehouse(str(tmp_path / f"{strategy_id}.duckdb"))
    try:
        runner = PaperRunner(
            warehouse=wh,
            research=research(),
            risk=RiskConfig(),
            override_strategy=(strategy_id, params),
            override_reason="engine/broker agreement test",
        )
        runner.run(history, through=DAYS[-1])
        statements = runner.broker.statements
        return statements[-1].equity, runner.broker.portfolio()
    finally:
        wh.close()


def engine_equity(history, strategy_id, params):
    strategy = build(strategy_id, **params)
    result = BacktestEngine(research(), RiskConfig()).run(strategy, history)
    return result.final_equity, result


@pytest.mark.parametrize("strategy_id,params", SPECS, ids=IDS)
class TestTheTwoPathsAgree:
    def test_final_equity_matches_exactly(self, strategy_id, params, history, tmp_path):
        paper, _ = paper_equity(history, strategy_id, params, tmp_path)
        engine, _ = engine_equity(history, strategy_id, params)
        assert paper == engine, (
            f"{strategy_id}: paper broker ended at {paper}, backtest engine at "
            f"{engine}. Two implementations of the same rules disagree, so one is "
            "wrong and nothing else would say which"
        )

    def test_the_same_number_of_fills(self, strategy_id, params, history, tmp_path):
        _, portfolio = paper_equity(history, strategy_id, params, tmp_path)
        _, result = engine_equity(history, strategy_id, params)
        assert len(portfolio.fills) == len(result.fills)

    def test_the_same_fill_prices_and_quantities(
        self, strategy_id, params, history, tmp_path
    ):
        _, portfolio = paper_equity(history, strategy_id, params, tmp_path)
        _, result = engine_equity(history, strategy_id, params)
        assert [(f.fill_date, f.side, f.quantity, f.price) for f in portfolio.fills] == [
            (f.fill_date, f.side, f.quantity, f.price) for f in result.fills
        ]

    def test_the_same_final_position(self, strategy_id, params, history, tmp_path):
        _, portfolio = paper_equity(history, strategy_id, params, tmp_path)
        _, result = engine_equity(history, strategy_id, params)
        engine_qty = {s: p.quantity for s, p in result_positions(result).items()}
        paper_qty = {s: p.quantity for s, p in portfolio.positions.items()}
        assert paper_qty == engine_qty

    def test_the_same_total_costs(self, strategy_id, params, history, tmp_path):
        _, portfolio = paper_equity(history, strategy_id, params, tmp_path)
        _, result = engine_equity(history, strategy_id, params)
        assert portfolio.total_fees == sum(f.fees for f in result.fills)


def result_positions(result):
    """Rebuild the engine's final book from its fills.

    BacktestResult does not carry the portfolio, so the comparison is made
    against a book replayed from the recorded fills — which also checks that
    the fill ledger is sufficient to reconstruct the position, as the
    accounting invariants claim.
    """
    from signalforge.core.types import Portfolio

    book = Portfolio(cash=Decimal("100000000"))
    for fill in result.fills:
        book.apply_fill(fill)
    return book.positions
