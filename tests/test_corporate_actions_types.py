"""CorporateAction construction and the warehouse loader.

Tiingo publishes split ratios as DECIMAL(18,6), so a true 7:1 split arrives as
7.000007 and a 3:2 as 1.499999. The loader must recover the true fraction or
refuse; it must never book a split with a noisy ratio.
"""

from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from signalforge.core.errors import SignalForgeError, ValidationError
from signalforge.core.types import CorporateAction
from signalforge.data.storage import Warehouse

D = Decimal
DAY = date(2014, 6, 9)


def split(ratio: str) -> CorporateAction:
    return CorporateAction.split("AAPL", DAY, D(ratio), "tiingo")


class TestSplitRatioRecovery:
    @pytest.mark.parametrize(
        "ratio,num,den",
        [
            ("7.000007", 7, 1),
            ("1.499999", 3, 2),
            ("0.333333", 1, 3),
            ("0.100000", 1, 10),
            ("4.000000", 4, 1),
            ("20.000000", 20, 1),
        ],
    )
    def test_noisy_ratios_recover_the_true_fraction(self, ratio, num, den):
        action = split(ratio)
        assert (action.numerator, action.denominator) == (num, den)
        assert action.kind == "split"
        assert isinstance(action.numerator, int) and isinstance(action.denominator, int)

    @pytest.mark.parametrize(
        "ratio",
        ["1.281", "1.046", "1.011", "1.032", "1.061", "0.9535", "1.241", "1.068833",
         "2.132", "1.015", "1.157", "1.214", "1.2345", "3.14159", "1.562525"],
    )
    def test_spinoff_and_special_factors_are_exact_adjustments(self, ratio):
        action = split(ratio)
        assert action.kind == "adjustment"
        assert action.action_type == "split"
        assert Fraction(action.numerator, action.denominator) == Fraction(ratio)

    def test_a_non_positive_ratio_is_refused(self):
        for ratio in ("0", "-2.000000"):
            with pytest.raises(SignalForgeError):
                split(ratio)


class TestConstructionIsValidated:
    def test_a_split_needs_a_positive_numerator_and_denominator(self):
        for num, den in [(0, 1), (1, 0), (-1, 2), (None, None)]:
            with pytest.raises(ValidationError):
                CorporateAction("A", DAY, "split", "tiingo", numerator=num, denominator=den)

    def test_a_dividend_needs_cash(self):
        with pytest.raises(ValidationError):
            CorporateAction("A", DAY, "dividend", "tiingo")

    def test_an_unknown_type_or_kind_is_refused(self):
        with pytest.raises(ValidationError):
            CorporateAction("A", DAY, "spinoff", "tiingo", numerator=2, denominator=1)
        with pytest.raises(ValidationError):
            CorporateAction("A", DAY, "split", "tiingo", numerator=2, denominator=1, kind="other")

    def test_the_kind_defaults_from_the_type(self):
        assert CorporateAction("A", DAY, "split", "t", numerator=2, denominator=1).kind == "split"
        assert CorporateAction.dividend("A", DAY, D("1"), "t").kind == "dividend"


class TestDividend:
    def test_it_carries_cash_per_share_as_decimal(self):
        action = CorporateAction.dividend("AAPL", DAY, D("0.23"), "tiingo")
        assert action.cash_per_share == D("0.23")
        assert action.action_type == "dividend"

    @pytest.mark.parametrize("cash", [None, D("-0.01")])
    def test_missing_or_negative_cash_is_refused(self, cash):
        with pytest.raises(SignalForgeError):
            CorporateAction.dividend("AAPL", DAY, cash, "tiingo")


class TestActionId:
    def test_it_is_deterministic_and_names_the_action(self):
        assert split("7.000007").action_id == "AAPL:2014-06-09:split"
        div = CorporateAction.dividend("MSFT", date(2020, 2, 19), D("0.51"), "tiingo")
        assert div.action_id == "MSFT:2020-02-19:dividend"

    def test_it_does_not_depend_on_the_source(self):
        other = CorporateAction.split("AAPL", DAY, D("7.000007"), "other")
        assert other.action_id == split("7.000007").action_id

    def test_it_is_immutable(self):
        with pytest.raises(AttributeError):
            split("4").symbol = "X"


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "ca.duckdb"))
    yield w
    w.close()


def seed(w):
    w.upsert_corporate_actions([
        {"symbol": "AAPL", "effective_date": date(2014, 6, 9), "action_type": "split",
         "ratio": "7.000007", "source": "tiingo"},
        {"symbol": "AAPL", "effective_date": date(2014, 5, 8), "action_type": "dividend",
         "cash_amount": "0.4700", "source": "tiingo"},
        {"symbol": "NVDA", "effective_date": date(2007, 9, 11), "action_type": "split",
         "ratio": "1.499999", "source": "tiingo"},
        {"symbol": "AAPL", "effective_date": date(2014, 6, 9), "action_type": "dividend",
         "cash_amount": "0.10", "source": "other"},
    ])


class TestLoadCorporateActions:
    def test_an_empty_warehouse_returns_an_empty_list(self, warehouse):
        assert warehouse.load_corporate_actions(["AAPL"]) == []

    def test_it_returns_sorted_typed_actions(self, warehouse):
        seed(warehouse)
        got = warehouse.load_corporate_actions(["AAPL", "NVDA"], source="tiingo")
        assert [a.action_id for a in got] == [
            "NVDA:2007-09-11:split",
            "AAPL:2014-05-08:dividend",
            "AAPL:2014-06-09:split",
        ]
        assert got[0].denominator == 2 and got[0].numerator == 3
        assert got[1].cash_per_share == D("0.47")
        assert (got[2].numerator, got[2].denominator) == (7, 1)

    def test_it_filters_by_symbol_window_and_source(self, warehouse):
        seed(warehouse)
        got = warehouse.load_corporate_actions(
            ["AAPL"], start=date(2014, 6, 1), end=date(2014, 6, 30), source="tiingo"
        )
        assert [a.action_id for a in got] == ["AAPL:2014-06-09:split"]
        assert warehouse.load_corporate_actions([]) == []

    def test_a_stored_split_with_no_ratio_is_refused(self, warehouse):
        warehouse.upsert_corporate_actions([
            {"symbol": "X", "effective_date": date(2020, 1, 2), "action_type": "split",
             "source": "tiingo"},
        ])
        with pytest.raises(ValidationError):
            warehouse.load_corporate_actions(["X"])

    def test_an_unknown_action_type_is_refused(self, warehouse):
        warehouse.upsert_corporate_actions([
            {"symbol": "X", "effective_date": date(2020, 1, 2), "action_type": "spinoff",
             "ratio": "1", "source": "tiingo"},
        ])
        with pytest.raises(ValidationError):
            warehouse.load_corporate_actions(["X"])

    def test_sources_agreeing_on_an_action_collapse_to_one(self, warehouse):
        for src in ("tiingo", "other"):
            warehouse.upsert_corporate_actions([
                {"symbol": "AAPL", "effective_date": DAY, "action_type": "split",
                 "ratio": "7.000007", "source": src},
            ])
        got = warehouse.load_corporate_actions(["AAPL"])
        assert [a.action_id for a in got] == ["AAPL:2014-06-09:split"]

    def test_sources_disagreeing_on_an_action_are_refused(self, warehouse):
        for src, ratio in (("tiingo", "7.000007"), ("other", "2.000000")):
            warehouse.upsert_corporate_actions([
                {"symbol": "AAPL", "effective_date": DAY, "action_type": "split",
                 "ratio": ratio, "source": src},
            ])
        with pytest.raises(ValidationError):
            warehouse.load_corporate_actions(["AAPL"])

    def test_disagreeing_dividends_are_refused(self, warehouse):
        for src, cash in (("tiingo", "0.47"), ("other", "0.48")):
            warehouse.upsert_corporate_actions([
                {"symbol": "AAPL", "effective_date": DAY, "action_type": "dividend",
                 "cash_amount": cash, "source": src},
            ])
        with pytest.raises(ValidationError):
            warehouse.load_corporate_actions(["AAPL"])

    def test_a_spinoff_factor_loads_instead_of_breaking_the_load(self, warehouse):
        warehouse.upsert_corporate_actions([
            {"symbol": "HON", "effective_date": date(2026, 6, 29), "action_type": "split",
             "ratio": "0.9535", "source": "tiingo"},
        ])
        (a,) = warehouse.load_corporate_actions(["HON"])
        assert (a.kind, a.numerator, a.denominator) == ("adjustment", 1907, 2000)
