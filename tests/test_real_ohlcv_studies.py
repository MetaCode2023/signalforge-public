"""Two things only real OHLCV can answer, and one structural finding.

The AAPL sample (506 bars, 2015-02-17 to 2017-02-16) is the only series in
the project with genuine adjusted closes and a genuine intraday range.
Everything else is a price index with no range and no dividend, or a four-day
snapshot. So it is the only place these can be measured rather than argued:

**Dividends.** Price return versus total return, and what the gap means for
every NASDAQCOM figure in `STRATEGY_RESULTS.md`. Measured: over the 1.9986
years of the sample AAPL's price return is +5.882816% and its total return is
+10.125484%, so dividends are 4.242668 percentage points, or 2.042766 points
a year. Put through the engine at the production 5% cap, buy-and-hold on the
price series reports +0.297592% where the total-return series reports
+0.502028% -- the price-only figure is 59% of the real one, and the Sharpe
drops from 0.2418 to 0.1455.

NASDAQCOM is a price index, so `series_history` sets `adj_close` equal to the
close on every bar and the two returns are identical by construction. Every
return and Sharpe in the index study is therefore a price return reported as
a return, understated by whatever the Nasdaq Composite's dividend yield was
over each window. That yield is not in any data this project holds, so the
size of the understatement is unknown here; the AAPL measurement establishes
that the mechanism is worth two points a year on one large-cap name, not that
it is worth two points on the index.

**Limit orders.** The limit-fill branches had never run against real highs
and lows. All three now have, on 505 bar transitions, at eleven limit
offsets: fill at the open when the open is already through the limit, fill AT
the limit when only the low (buy) or high (sell) reaches it, and no fill at
all. A limit buy at the previous close fills 409 times of 505 at a mean of
112.9195, against a market order's 505 of 505 at 112.9641.

**The structural finding: no strategy can ask for a limit order.** `Signal`
has no limit-price field, and both drivers -- `BacktestEngine` and
`PaperRunner` -- construct `OrderType.MARKET` unconditionally. The limit
model is reachable only by calling `FillModel.fill` or `PaperBroker.submit`
by hand. It is tested, it works on real data, and no backtest in this project
can ever produce one. That is a gap in the research surface rather than a
bug, and it is pinned below so it cannot be forgotten or quietly changed.
"""

import ast
import inspect
import os
from collections import Counter
from decimal import Decimal

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.execution import FillModel
from signalforge.backtesting.metrics import compute_metrics
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.core.interfaces import Signal
from signalforge.core.types import Bar, Order, OrderType, Side
from signalforge.data.archive import load_archive_history
from signalforge.data.providers import AAPL_FILE, DEFAULT_ARCHIVE_DIR
from signalforge.paper.broker import PaperBroker
from signalforge.pipeline import series_history
from signalforge.strategies.registry import build

from tests.conftest import business_days, make_bar

D = Decimal
MATERIAL_RATIO_STEP = D("0.0005")


def research_config() -> ResearchConfig:
    return ResearchConfig(starting_cash=D("100000"), universe=("AAPL",))


@pytest.fixture(scope="module")
def aapl() -> PriceHistory:
    path = os.path.join(str(DEFAULT_ARCHIVE_DIR), f"{AAPL_FILE}.csv")
    if not os.path.isfile(path):
        pytest.skip(f"{path} is licensed data and is not in the repo")
    return load_archive_history(datasets=[AAPL_FILE])


@pytest.fixture(scope="module")
def aapl_bars(aapl) -> list[Bar]:
    return list(aapl.bars_for("AAPL"))


@pytest.fixture(scope="module")
def model() -> FillModel:
    return FillModel(ExecutionConfig())


def total_return_series(bars: list[Bar]) -> PriceHistory:
    """The same bars rescaled so the CLOSE is the adjusted close.

    Scaling open/high/low by the same ratio keeps each bar internally
    consistent, so the only difference between this history and the raw one
    is that holding it earns the dividend.
    """
    return PriceHistory.from_bars(
        [
            Bar(
                symbol="AAPL_TR", trade_date=b.trade_date,
                open=b.open * (b.adj_close / b.close),
                high=b.high * (b.adj_close / b.close),
                low=b.low * (b.adj_close / b.close),
                close=b.adj_close, volume=b.volume, adj_close=b.adj_close,
                source=b.source, volume_known=b.volume_known,
            )
            for b in bars
        ]
    )


# --------------------------------------------------------------------------
# Dividends
# --------------------------------------------------------------------------
class TestTheAdjustmentIsRealDividends:
    def test_the_sample_is_the_shape_the_reports_claim(self, aapl_bars):
        assert len(aapl_bars) == 506
        assert aapl_bars[0].trade_date.isoformat() == "2015-02-17"
        assert aapl_bars[-1].trade_date.isoformat() == "2017-02-16"
        assert all(b.volume_known for b in aapl_bars)
        assert all(b.high > b.low for b in aapl_bars)

    def test_the_adjustment_ratio_spans_the_recorded_range(self, aapl_bars):
        ratios = [b.adj_close / b.close for b in aapl_bars]
        assert min(ratios).quantize(D("0.000001")) == D("0.961474")
        assert max(ratios) == D("1")
        assert ratios[-1] == D("1"), "the last bar must be unadjusted"

    def test_there_are_exactly_eight_material_adjustments_one_a_quarter(self, aapl_bars):
        """Eight steps over two years, one a quarter: AAPL's dividend schedule.

        This is what distinguishes a real adjustment series from a rescaled
        one. The sub-threshold noise is the published adj_close being rounded
        to six decimals, not an event.
        """
        steps, noise = [], 0
        for previous, current in zip(aapl_bars, aapl_bars[1:]):
            before = previous.adj_close / previous.close
            after = current.adj_close / current.close
            change = after / before - D(1)
            if abs(change) > MATERIAL_RATIO_STEP:
                steps.append((current.trade_date, change))
            elif change != 0:
                noise += 1
        assert len(steps) == 8
        assert noise == 491, "the rounding noise is part of the data, not an event"
        months = sorted({(d.year, d.month) for d, _ in steps})
        assert months == [
            (2015, 5), (2015, 8), (2015, 11),
            (2016, 2), (2016, 5), (2016, 8), (2016, 11),
            (2017, 2),
        ]
        assert all(change > 0 for _, change in steps), (
            "a dividend adjustment raises the ratio toward 1 going forward"
        )


class TestPriceReturnVersusTotalReturn:
    def test_total_return_exceeds_price_return_by_the_dividend(self, aapl_bars):
        price = aapl_bars[-1].close / aapl_bars[0].close - D(1)
        total = aapl_bars[-1].adj_close / aapl_bars[0].adj_close - D(1)
        assert price.quantize(D("0.000001")) == D("0.058828")
        assert total.quantize(D("0.000001")) == D("0.101255")
        assert (total - price).quantize(D("0.000001")) == D("0.042427")

    def test_the_annualised_drag_is_two_points_a_year(self, aapl_bars):
        years = D((aapl_bars[-1].trade_date - aapl_bars[0].trade_date).days) / D("365.25")
        assert years.quantize(D("0.0001")) == D("1.9986")

        def annualise(total):
            return (D(1) + total) ** (D(1) / years) - D(1)

        price = annualise(aapl_bars[-1].close / aapl_bars[0].close - D(1))
        total = annualise(aapl_bars[-1].adj_close / aapl_bars[0].adj_close - D(1))
        assert price.quantize(D("0.000001")) == D("0.029014")
        assert total.quantize(D("0.000001")) == D("0.049442")
        assert (total - price).quantize(D("0.000001")) == D("0.020428")

    def test_the_engine_reports_the_price_return_not_the_total_return(self, aapl, aapl_bars):
        """The engine fills and marks at `close`, so its P&L is price return.

        Strategies read `adjusted_closes` for SIGNALS. Nothing in the project
        earns the dividend, which is why a price-index result understates a
        tradeable one even before tracking error and fees.
        """
        research = research_config()
        engine = BacktestEngine(research, RiskConfig())
        reported = {}
        for label, history, symbol in (
            ("price", aapl, "AAPL"),
            ("total_return", total_return_series(aapl_bars), "AAPL_TR"),
        ):
            result = engine.run(
                build("buy_and_hold", symbols=(symbol,), target_weight=D("0.05")),
                history, run_id=f"div:{label}",
            )
            reported[label] = compute_metrics(result, config=research)

        assert reported["price"].total_return.quantize(D("0.000001")) == D("0.002976")
        assert reported["total_return"].total_return.quantize(D("0.000001")) == D("0.005020")
        assert reported["price"].total_return < reported["total_return"].total_return
        assert reported["price"].sharpe < reported["total_return"].sharpe
        # The understatement, stated as a fraction so it does not read as
        # a small absolute number on a 5%-capped book.
        ratio = reported["price"].total_return / reported["total_return"].total_return
        assert D("0.55") < ratio < D("0.65")

    def test_a_trend_strategy_is_understated_the_same_way(self, aapl, aapl_bars):
        research = research_config()
        engine = BacktestEngine(research, RiskConfig())
        out = {}
        for label, history, symbol in (
            ("price", aapl, "AAPL"),
            ("total_return", total_return_series(aapl_bars), "AAPL_TR"),
        ):
            result = engine.run(
                build("ma_trend", fast_window=20, slow_window=60,
                      target_weight=D("0.05"), symbols=(symbol,)),
                history, run_id=f"divma:{label}",
            )
            out[label] = compute_metrics(result, config=research)
        assert out["price"].total_return.quantize(D("0.000001")) == D("0.002471")
        assert out["total_return"].total_return.quantize(D("0.000001")) == D("0.006621")
        assert out["price"].total_return < out["total_return"].total_return


class TestAnIndexSeriesCarriesNoDividend:
    """Hermetic: why every NASDAQCOM figure is a price return.

    `series_history` sets `adj_close` equal to the close, so price return and
    total return are identical by construction. That is the honest treatment
    of a price index -- it is not inventing a yield -- but it means the index
    study's returns are understated by an amount this project cannot measure.
    """

    def test_every_bar_of_an_index_history_is_unadjusted(self):
        days = business_days(__import__("datetime").date(2020, 1, 1), 300)
        observations = [(day, D(100 + index)) for index, day in enumerate(days)]
        bundle = series_history(observations, symbol="NASDAQCOM")
        bars = bundle.history.bars_for("NASDAQCOM")
        assert bars
        assert all(b.adj_close == b.close for b in bars)
        assert all(not b.volume_known for b in bars)

    def test_the_limitation_says_dividends_are_not_reflected(self):
        days = business_days(__import__("datetime").date(2020, 1, 1), 300)
        bundle = series_history(
            [(day, D(100 + index)) for index, day in enumerate(days)],
            symbol="NASDAQCOM",
        )
        joined = " ".join(bundle.limitations).lower()
        assert "dividend" in joined
        assert "understated" in joined

    def test_price_and_total_return_cannot_differ_on_an_index_history(self):
        days = business_days(__import__("datetime").date(2020, 1, 1), 300)
        bundle = series_history(
            [(day, D(100 + index)) for index, day in enumerate(days)],
            symbol="NASDAQCOM",
        )
        bars = bundle.history.bars_for("NASDAQCOM")
        price = bars[-1].close / bars[0].close - D(1)
        total = bars[-1].adj_close / bars[0].adj_close - D(1)
        assert price == total


# --------------------------------------------------------------------------
# Limit orders
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def driven(aapl, aapl_bars):
    """200 sessions of a limit round trip, driven through the paper broker."""
    broker = PaperBroker(research_config(), RiskConfig())
    submitted, rejected = Counter(), Counter()
    for index in range(1, 200):
        previous = aapl_bars[index - 1]
        day = previous.trade_date
        view = aapl.view(day)
        broker.open_session(day, view)
        broker.settle(day, view)
        held = broker.portfolio().quantity_of("AAPL")
        if held > 0:
            side, factor, quantity = Side.SELL, D("1.005"), held
        else:
            side, factor, quantity = Side.BUY, D("0.995"), 5
        order = broker.create_order(
            symbol="AAPL", side=side, quantity=quantity, signal_date=day,
            strategy_id="limit_e2e", reason="limit round trip",
            order_type=OrderType.LIMIT,
            limit_price=(previous.close * factor).quantize(D("0.000001")),
        )
        result = broker.submit(order)
        submitted[side.value] += 1
        if result.status.value == "REJECTED":
            rejected[broker.risk_events[-1].rule] += 1
        broker.close_session(day, view)
    return broker, submitted, rejected


def limit_branch(order: Order, bar: Bar) -> str:
    limit = order.limit_price
    if order.side is Side.BUY:
        if bar.open <= limit:
            return "fill_at_open"
        return "fill_at_limit" if bar.low < limit else "no_fill"
    if bar.open >= limit:
        return "fill_at_open"
    return "fill_at_limit" if bar.high > limit else "no_fill"


def sweep(bars, side, offset_bps, model) -> tuple[Counter, list[Decimal]]:
    branches, prices = Counter(), []
    for previous, bar in zip(bars, bars[1:]):
        limit = (previous.close * (D(1) + D(offset_bps) / D(10000))).quantize(D("0.000001"))
        order = Order(
            order_id=f"l{bar.trade_date}", symbol="AAPL", side=side, quantity=10,
            order_type=OrderType.LIMIT, signal_date=previous.trade_date,
            strategy_id="limit_study", reason="limit sweep", limit_price=limit,
        )
        branches[limit_branch(order, bar)] += 1
        fill = model.fill(order, bar, previous_close=previous.close)
        if fill is not None:
            prices.append(fill.price)
            if side is Side.BUY:
                assert fill.price <= limit
            else:
                assert fill.price >= limit
    return branches, prices


class TestLimitFillsOnRealIntradayRange:
    def test_all_three_branches_occur_on_real_bars(self, aapl_bars, model):
        branches, prices = sweep(aapl_bars, Side.BUY, 0, model)
        assert branches["fill_at_open"] == 251
        assert branches["fill_at_limit"] == 158
        assert branches["no_fill"] == 96
        assert sum(branches.values()) == 505
        assert len(prices) == 409

    def test_the_sell_side_branches_too(self, aapl_bars, model):
        branches, prices = sweep(aapl_bars, Side.SELL, 0, model)
        assert branches["fill_at_open"] == 262
        assert branches["fill_at_limit"] == 167
        assert branches["no_fill"] == 76
        assert len(prices) == 429

    @pytest.mark.parametrize("offset_bps", [-500, -200, -100, -50, -20, 0, 20, 50, 100, 200, 500])
    @pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
    def test_a_limit_fill_never_trades_through_its_limit(self, aapl_bars, model, side, offset_bps):
        """The sweep asserts it per fill; this is the whole 11x2 grid.

        Slippage is applied after the limit is resolved, so without the clamp
        in `_fill_price` a 5 bps adverse move would push a fill past the price
        the order was protecting.
        """
        branches, prices = sweep(aapl_bars, side, offset_bps, model)
        assert sum(branches.values()) == 505
        assert len(prices) == branches["fill_at_open"] + branches["fill_at_limit"]

    def test_a_far_limit_almost_always_fills_and_a_distant_one_almost_never(
        self, aapl_bars, model
    ):
        deep_buy, deep_prices = sweep(aapl_bars, Side.BUY, -500, model)
        high_buy, high_prices = sweep(aapl_bars, Side.BUY, 500, model)
        assert len(deep_prices) == 6
        assert len(high_prices) == 504
        assert deep_buy["no_fill"] == 499
        assert high_buy["no_fill"] == 1

    def test_a_limit_buy_pays_less_but_misses_bars(self, aapl_bars, model):
        """The measured adverse-selection trade-off on real prices."""
        _, limit_prices = sweep(aapl_bars, Side.BUY, 0, model)
        market_prices = []
        for previous, bar in zip(aapl_bars, aapl_bars[1:]):
            fill = model.fill(
                Order(order_id=f"m{bar.trade_date}", symbol="AAPL", side=Side.BUY,
                      quantity=10, order_type=OrderType.MARKET,
                      signal_date=previous.trade_date, strategy_id="limit_study",
                      reason="market"),
                bar, previous_close=previous.close)
            assert fill is not None
            market_prices.append(fill.price)
        assert len(market_prices) == 505
        assert len(limit_prices) == 409
        limit_mean = sum(limit_prices) / len(limit_prices)
        market_mean = sum(market_prices) / len(market_prices)
        assert limit_mean < market_mean
        assert limit_mean.quantize(D("0.0001")) == D("112.9195")
        assert market_mean.quantize(D("0.0001")) == D("112.9641")


class TestLimitOrdersEndToEndThroughThePaperBroker:
    """A round-trip loop, because repeated entries are averaging down.

    Submitting the same buy limit every session had 53 of 59 orders refused
    by `martingale_averaging_down`, which proved the control works rather
    than that the limit path does. Buying 50 bps under the previous close and
    selling 50 bps over it once held exercises both sides.
    """

    def test_limit_orders_fill_and_expire_on_real_bars(self, driven):
        broker, submitted, _ = driven
        book = broker.portfolio()
        assert sum(submitted.values()) == 199
        assert submitted == Counter({"BUY": 119, "SELL": 80})
        assert len(book.fills) == 105
        assert sum(1 for f in book.fills if f.side is Side.BUY) == 53
        assert sum(1 for f in book.fills if f.side is Side.SELL) == 52
        kinds = Counter(e.event_type.value for e in broker.ledger.events)
        assert kinds["FILL"] == 105
        assert kinds["ORDER_EXPIRED"] == 60, "a limit that was never reached"
        assert kinds["ORDER_REJECTED"] == 33

    def test_the_only_refusal_is_the_downtrend_control(self, driven):
        _, _, rejected = driven
        assert rejected == Counter({"sustained_decline": 33})

    def test_the_naive_limit_round_trip_loses_money_on_real_data(self, driven):
        """Not a cost result: the sells average BELOW the buys.

        Buying 50 bps under the close fills on days the price fell and
        selling 50 bps over it fills on days it rose, so the side that gets
        the "better" price is systematically the wrong one. 52 round trips,
        realised -167.934215, mean buy 123.7146 against mean sell 123.5731.
        """
        broker, _, _ = driven
        book = broker.portfolio()
        buys = [f for f in book.fills if f.side is Side.BUY]
        sells = [f for f in book.fills if f.side is Side.SELL]
        mean_buy = sum(f.price for f in buys) / len(buys)
        mean_sell = sum(f.price for f in sells) / len(sells)
        assert mean_buy.quantize(D("0.0001")) == D("123.7146")
        assert mean_sell.quantize(D("0.0001")) == D("123.5731")
        assert mean_sell < mean_buy
        assert book.realized_pnl.quantize(D("0.000001")) == D("-167.934215")

    def test_the_broker_stays_paper_throughout(self, driven):
        broker, _, _ = driven
        assert broker.is_paper is True
        assert len(broker.statements) == 199


class TestNoStrategyCanAskForALimitOrder:
    """The structural gap, pinned so it cannot be forgotten.

    If a limit-order research path is ever wanted, these three assertions are
    the list of what has to change.
    """

    def test_a_signal_has_no_limit_price_field(self):
        assert "limit_price" not in Signal.__dataclass_fields__
        assert "limit" not in " ".join(Signal.__dataclass_fields__)

    def test_the_backtest_engine_only_ever_builds_market_orders(self):
        source = inspect.getsource(BacktestEngine)
        tree = ast.parse(source.lstrip())
        order_types = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "OrderType"
        }
        assert order_types == {"MARKET"}, order_types

    def test_the_paper_runner_only_ever_builds_market_orders(self):
        from signalforge.paper import runner

        tree = ast.parse(inspect.getsource(runner))
        order_types = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "OrderType"
        }
        assert order_types == {"MARKET"}, order_types

    def test_the_paper_broker_is_the_one_door_that_accepts_a_limit(self):
        signature = inspect.signature(PaperBroker.create_order)
        assert "limit_price" in signature.parameters
        assert signature.parameters["order_type"].default is OrderType.MARKET
