"""The engine at cross-sectional width, and the evidence gate at four days.

Two separate claims, both measured on the real MarketParquet snapshot
(26,018 bars, 6,799 symbols, 2026-10-20 to 2026-10-23) and then pinned here:

1. **It completes at that width.** Before the `cash_cap` fix in gauntlet
   round 6 it did not, in any useful sense: a four-day run over the full
   cross-section took 461.231 seconds, 99.8% of it inside one quantity cap.
   The same run now takes 0.930 seconds and produces byte-identical results
   (85 orders, 36 fills, 41 risk events, final equity 99,061.157008).

2. **A 126-day lookback over four days produces nothing, and says so.** The
   interesting failure here would be a strategy that ranks on whatever
   history happens to exist and reports a number: a 126-day momentum figure
   computed from three returns looks exactly like a real one. It does not
   happen. The warmup gate withholds every signal, and the metrics layer
   returns INSUFFICIENT_EVIDENCE naming both shortfalls rather than a
   verdict.

The width tests are synthetic so the suite stays hermetic and fast. The
real-data test is skipped when the licensed archive file is absent, which is
the case in any clone, because `data/archive/**` is gitignored.
"""

import os
from datetime import date
from decimal import Decimal
from time import perf_counter

import pytest

from signalforge.backtesting.engine import BacktestEngine
from signalforge.backtesting.metrics import EvidenceLevel, compute_metrics
from signalforge.config.models import ExecutionConfig, ResearchConfig, RiskConfig
from signalforge.core.history import PriceHistory
from signalforge.data.archive import load_archive_history
from signalforge.data.providers import MARKETPARQUET_FILE
from signalforge.strategies.registry import build

from tests.conftest import business_days, make_bar

D = Decimal

WIDE_SYMBOL_COUNT = 1200
DAYS = business_days(date(2026, 10, 20), 4)

# The real parameters from `pipeline.baseline_specs`, unchanged.
HONEST_MOMENTUM = {
    "lookback_days": 126,
    "skip_days": 21,
    "top_n": 1,
    "target_weight_total": D("0.05"),
    "min_history_days": 150,
}
# A lookback four sessions can actually satisfy, used only to drive real
# order flow across the full width.
SHORT_MOMENTUM = {
    "lookback_days": 1,
    "skip_days": 0,
    "top_n": 20,
    "target_weight_total": D("0.05"),
    "min_history_days": 2,
}


def engine(universe) -> BacktestEngine:
    return BacktestEngine(
        ResearchConfig(
            starting_cash=D("100000"),
            universe=tuple(universe),
            execution=ExecutionConfig(),
        ),
        RiskConfig(),
    )


@pytest.fixture(scope="module")
def wide_history() -> PriceHistory:
    """1,200 symbols x 4 sessions, with the real snapshot's price spread.

    Prices deliberately span 0.000001 to 813,595, the measured minimum and
    maximum close of the real cross-section, because the sub-penny tail is
    what made the original run unusable.
    """
    prices = ["0.000001", "0.006", "0.01", "0.37", "12.7", "184.5", "813595"]
    bars = []
    for i in range(WIDE_SYMBOL_COUNT):
        base = D(prices[i % len(prices)])
        for j, day in enumerate(DAYS):
            bars.append(
                make_bar(
                    f"S{i:05d}",
                    day,
                    base * (D(1) + D(j) / D(100)),
                    volume=1_000_000 + i,
                    source="wide_fixture",
                )
            )
    return PriceHistory.from_bars(bars)


@pytest.fixture(scope="module")
def real_cross_section() -> PriceHistory:
    from signalforge.data.providers import DEFAULT_ARCHIVE_DIR

    path = os.path.join(str(DEFAULT_ARCHIVE_DIR), f"{MARKETPARQUET_FILE}.csv")
    if not os.path.isfile(path):
        pytest.skip(f"{path} is licensed data and is not in the repo")
    return load_archive_history(datasets=[MARKETPARQUET_FILE], as_of=date(2026, 10, 26))


class TestWidthDoesNotStallTheEngine:
    def test_a_wide_cross_section_completes_in_seconds(self, wide_history):
        assert len(wide_history.symbols) == WIDE_SYMBOL_COUNT
        universe = wide_history.symbols
        strategy = build("xs_momentum", **SHORT_MOMENTUM)
        start = perf_counter()
        result = engine(universe).run(strategy, wide_history)
        elapsed = perf_counter() - start
        assert len(result.equity_curve) == len(DAYS)
        # The real 6,799-symbol run took 0.930s after the fix and 461.231s
        # before it. 30s over a fifth of that width is a bound only the
        # defect could breach.
        assert elapsed < 30.0, f"{WIDE_SYMBOL_COUNT} symbols took {elapsed:.1f}s"

    def test_the_sub_penny_tail_is_what_used_to_stall_it(self, wide_history):
        """Name the condition, so a reader knows why the fixture has it."""
        view = wide_history.view(DAYS[-1])
        closes = sorted(
            view.latest(symbol).close for symbol in wide_history.symbols
        )
        assert closes[0] < D("0.01")
        assert sum(1 for c in closes if c < D("0.01")) > WIDE_SYMBOL_COUNT // 10

    def test_width_does_not_break_the_accounting_identity(self, wide_history):
        result = engine(wide_history.symbols).run(
            build("xs_momentum", **SHORT_MOMENTUM), wide_history
        )
        final = result.equity_curve[-1]
        assert final.equity == final.cash + final.gross_exposure
        assert result.final_equity > D("0")

    def test_width_leaves_no_committed_cash_behind(self, wide_history):
        residual = engine(wide_history.symbols).run_and_report_residual(
            build("xs_momentum", **SHORT_MOMENTUM), wide_history
        )
        assert residual == D("0")


class TestAFourDayWindowRefusesA126DayLookback:
    def test_no_signal_survives_the_warmup_gate(self, wide_history):
        strategy = build("xs_momentum", **HONEST_MOMENTUM)
        assert strategy.warmup_days == 150
        result = engine(wide_history.symbols).run(strategy, wide_history)
        assert result.orders == []
        assert result.fills == []
        assert result.risk_events == []
        assert result.final_equity == D("100000")

    def test_it_does_not_rank_on_the_history_that_happens_to_exist(self, wide_history):
        """The dangerous outcome would be a ranking, not an empty result."""
        strategy = build("xs_momentum", **HONEST_MOMENTUM)
        for day in DAYS:
            assert strategy.generate_signals(day, wide_history.view(day)) == []

    def test_the_metrics_report_insufficient_evidence_not_a_verdict(self, wide_history):
        runner = engine(wide_history.symbols)
        result = runner.run(build("xs_momentum", **HONEST_MOMENTUM), wide_history)
        metrics = compute_metrics(result, config=runner.research)
        assert metrics.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
        assert metrics.trade_count == 0
        assert metrics.observations == len(DAYS)

    def test_the_evidence_note_names_both_shortfalls_with_their_sizes(self, wide_history):
        runner = engine(wide_history.symbols)
        result = runner.run(build("xs_momentum", **HONEST_MOMENTUM), wide_history)
        note = compute_metrics(result, config=runner.research).evidence_note
        assert "closed trade count 0" in note
        assert "short by 30" in note
        assert "observations 4" in note
        assert "short by 248" in note

    def test_sharpe_is_none_rather_than_a_number_from_four_returns(self, wide_history):
        runner = engine(wide_history.symbols)
        result = runner.run(build("xs_momentum", **HONEST_MOMENTUM), wide_history)
        metrics = compute_metrics(result, config=runner.research)
        assert metrics.sharpe is None


class TestTheRealCrossSection:
    """Skipped in any clone: the source file is licensed and gitignored."""

    def test_the_snapshot_is_the_shape_the_reports_claim(self, real_cross_section):
        assert len(real_cross_section) == 26_018
        assert len(real_cross_section.symbols) == 6_799
        assert len(real_cross_section.trading_days) == 4
        assert real_cross_section.trading_days[0] == date(2026, 10, 20)
        assert real_cross_section.trading_days[-1] == date(2026, 10, 23)

    def test_the_honest_lookback_produces_nothing_on_the_real_snapshot(
        self, real_cross_section
    ):
        runner = engine(real_cross_section.symbols)
        start = perf_counter()
        result = runner.run(build("xs_momentum", **HONEST_MOMENTUM), real_cross_section)
        elapsed = perf_counter() - start
        assert result.orders == []
        assert result.fills == []
        assert result.final_equity == D("100000")
        metrics = compute_metrics(result, config=runner.research)
        assert metrics.evidence is EvidenceLevel.INSUFFICIENT_EVIDENCE
        assert elapsed < 30.0, f"the real cross-section took {elapsed:.1f}s"

    def test_the_full_width_run_completes_and_trades(self, real_cross_section):
        runner = engine(real_cross_section.symbols)
        start = perf_counter()
        result = runner.run(build("xs_momentum", **SHORT_MOMENTUM), real_cross_section)
        elapsed = perf_counter() - start
        assert result.fills
        final = result.equity_curve[-1]
        assert final.equity == final.cash + final.gross_exposure
        # 0.930s measured. 60s is 65x the fix and 7.7x under the defect's
        # 461.231s, so it fails loudly if the cap ever starts searching again.
        assert elapsed < 60.0, f"the real cross-section took {elapsed:.1f}s"
