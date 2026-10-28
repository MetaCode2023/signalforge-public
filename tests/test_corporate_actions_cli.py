"""Jump triage: `draft` classifies unrecorded raw jumps, `apply` records approvals.

Fixture warehouses reproduce the four real patterns: the vendor's adjusted
close is smooth while raw drops (GOOGL, a split the vendor never booked in the
raw series), both jump by a non-split factor (AAPL, a real move), both jump by
almost exactly a split and hold (CVX, the vendor adjusted for nothing), and a
jump that comes straight back (a bad print).
"""

import hashlib
import json
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from signalforge.core.types import Bar
from signalforge.corporate_actions_cli import apply_entries, draft_entries, main, parse_args
from signalforge.data.storage import Warehouse

from tests.conftest import business_days

D = Decimal
DAYS = business_days(date(2020, 1, 1), 60)
JUMP = DAYS[30]


@pytest.fixture
def warehouse(tmp_path):
    w = Warehouse(str(tmp_path / "triage.duckdb"))
    yield w
    w.close()


def seed(warehouse, symbol, raw, adj=None, source="tiingo"):
    """`raw`/`adj` map an index to a price; the last given value carries forward."""
    bars, price, adjusted = [], None, None
    for i, day in enumerate(DAYS):
        price = D(raw[i]) if i in raw else price
        if adj is None:
            adjusted = price
        else:
            adjusted = D(adj[i]) if i in adj else adjusted
        bars.append(Bar(symbol=symbol, trade_date=day, open=price, high=price, low=price,
                        close=price, volume=1000, adj_close=adjusted, source=source))
    warehouse.upsert_bars(bars)


def at_jump(entries, symbol="AAA", day=JUMP):
    rows = [e for e in entries if e["symbol"] == symbol and e["date"] == day.isoformat()]
    assert len(rows) == 1, rows
    return rows[0]


def seed_ratios(warehouse, symbol, raw_ratio, adj_ratio, after=None):
    """Prior close 100 for both series; the jump day closes at 100/ratio.

    `after` maps later indices to (raw, adj) so a test can shape what follows.
    """
    q = D("0.000001")
    raw = {0: "100", 30: str((D(100) / D(raw_ratio)).quantize(q))}
    adj = {0: "100", 30: str((D(100) / D(adj_ratio)).quantize(q))}
    for i, (r, a) in (after or {}).items():
        raw[i], adj[i] = str(r), str(a)
    seed(warehouse, symbol, raw, adj)


def falling(raw_ratio):
    """The stock keeps sliding 3% a session after the jump: it never holds."""
    level = (D(100) / D(raw_ratio)).quantize(D("0.000001"))
    out = {}
    for i in range(31, 60):
        level = (level * D("0.97")).quantize(D("0.000001"))
        out[i] = (level, level)
    return out


class TestDraftCategories:
    def test_a_smooth_adjusted_series_under_a_raw_drop_is_a_missing_split(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "50"}, {0: "50"})
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "missing_split"
        assert entry["proposed"]["action"] == "manual_split"
        assert D(entry["proposed"]["ratio"]) == D(2)
        assert entry["decision"] == "propose" and entry["approved"] is False
        assert (D(entry["raw_ratio"]), D(entry["adj_ratio"])) == (D(2), D(1))
        assert (D(entry["prior_close"]), D(entry["close"])) == (D(100), D(50))

    def test_a_raw_rise_under_a_smooth_adjusted_series_proposes_a_reverse_split(self, warehouse):
        seed(warehouse, "AAA", {0: "50", 30: "100"}, {0: "100"})
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "missing_split"
        assert D(entry["proposed"]["ratio"]) == D("0.5")

    def test_googl_2014_is_a_two_for_one_split(self, warehouse):
        seed_ratios(warehouse, "AAA", "1.986177", "0.996163")
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "missing_split"
        assert entry["proposed"]["action"] == "manual_split"
        assert D(entry["proposed"]["ratio"]) == D(2)

    def test_jci_2012_lands_within_one_percent_of_two_so_it_is_a_split(self, warehouse):
        seed_ratios(warehouse, "AAA", "1.974035", "0.983910")
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["proposed"]["action"] == "manual_split"
        assert D(entry["proposed"]["ratio"]) == D(2)

    @pytest.mark.parametrize(
        "raw,adj",
        [("2.043682", "0.982013"), ("1.799537", "0.987712"), ("1.518138", "0.995633")],
        ids=["ABT-AbbVie", "BAX-Baxalta", "MDLZ-Kraft"],
    )
    def test_spinoffs_get_the_exact_factor_as_a_manual_adjustment(self, warehouse, raw, adj):
        seed_ratios(warehouse, "AAA", raw, adj)
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "missing_split"
        assert entry["proposed"]["action"] == "manual_adjustment"
        assert abs(D(entry["proposed"]["ratio"]) - D(raw) / D(adj)) < D("0.0001")
        assert D(entry["proposed"]["ratio"]).as_tuple().exponent == -6

    def test_a_smooth_adjusted_series_with_an_unclean_ratio_is_an_exact_adjustment(
        self, warehouse
    ):
        seed(warehouse, "AAA", {0: "100", 30: "60"}, {0: "60"})
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "missing_split"
        assert entry["proposed"] == {"action": "manual_adjustment", "ratio": "1.666667"}

    def test_the_adjusted_series_moving_five_percent_or_more_is_not_smooth(self, warehouse):
        seed_ratios(warehouse, "AAA", "2.5", "1.2")
        assert at_jump(draft_entries(warehouse, "tiingo"))["category"] == "review"

    def test_both_series_jumping_by_a_non_split_factor_is_a_real_move(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "48.12"}, {0: "100", 30: "48.12"})
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "real_move"
        assert entry["decision"] == "propose"
        assert entry["proposed"]["action"] == "acknowledge"
        assert entry["proposed"]["reason"].startswith("vendor-adjusted price moved too (adj 2.0")
        assert entry["proposed"]["reason"].endswith("); real move")
        assert entry["approved"] is False

    @pytest.mark.parametrize(
        "raw", ["2.077670", "1.539306", "1.496970", "0.656642"],
        ids=["AAPL-2000", "AAP-2023", "ALGN-2004", "AMD-2016"],
    )
    def test_a_real_move_is_real_whether_or_not_the_stock_holds_the_new_level(
        self, warehouse, raw
    ):
        seed_ratios(warehouse, "AAA", raw, raw, after=falling(raw))
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "real_move"
        assert entry["proposed"]["action"] == "acknowledge"

    @pytest.mark.parametrize(
        "raw,ratio",
        [("2.017094", 2), ("2.030333", 2), ("2.000000", 2), ("3.010000", 3), ("0.500000", "0.5")],
        ids=["CVX-1973", "CVX-1981", "BIO-1980", "three", "half"],
    )
    def test_vendor_agreement_on_a_near_integer_split_that_holds_is_review_with_a_proposal(
        self, warehouse, raw, ratio
    ):
        seed_ratios(warehouse, "AAA", raw, raw)
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "review" and entry["decision"] == "review"
        assert entry["proposed"]["action"] == "manual_split"
        assert D(entry["proposed"]["ratio"]) == D(ratio)

    def test_three_halves_is_never_proposed_as_a_split_on_vendor_agreement(self, warehouse):
        seed_ratios(warehouse, "AAA", "1.5", "1.5")
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "real_move"

    def test_a_near_integer_jump_that_does_not_hold_is_a_real_move(self, warehouse):
        seed_ratios(warehouse, "AAA", "2.017094", "2.017094", after=falling("2.017094"))
        assert at_jump(draft_entries(warehouse, "tiingo"))["category"] == "real_move"

    def test_a_jump_with_fewer_than_twenty_bars_after_it_is_never_proposed_as_a_split(
        self, warehouse
    ):
        seed(warehouse, "AAA", {0: "100", 50: "49.58"}, {0: "100", 50: "49.58"})
        entry = at_jump(draft_entries(warehouse, "tiingo"), day=DAYS[50])
        assert entry["category"] == "review" and entry["decision"] == "review"
        assert entry["proposed"]["action"] == "acknowledge"

    def test_a_real_move_in_the_last_bars_is_review_with_the_acknowledgement_prefilled(
        self, warehouse
    ):
        seed(warehouse, "AAA", {0: "100", 50: "48.12"}, {0: "100", 50: "48.12"})
        entry = at_jump(draft_entries(warehouse, "tiingo"), day=DAYS[50])
        assert entry["category"] == "review" and entry["decision"] == "review"
        assert entry["proposed"]["action"] == "acknowledge"

    def test_a_recovering_bad_print_is_a_real_move_by_the_vendor_rule(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "60", 31: "100"})
        assert at_jump(draft_entries(warehouse, "tiingo"))["category"] == "real_move"

    def test_adjusted_and_raw_jumping_differently_is_review_without_a_proposal(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "45"}, {0: "100", 30: "70"})
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "review" and entry["proposed"] is None

    def test_a_bar_with_no_adj_close_is_review_without_a_proposal(self, warehouse):
        bars = [
            Bar(symbol="AAA", trade_date=day, open=D(100 if i < 30 else 50),
                high=D(100 if i < 30 else 50), low=D(100 if i < 30 else 50),
                close=D(100 if i < 30 else 50), volume=1000, adj_close=None, source="tiingo")
            for i, day in enumerate(DAYS)
        ]
        warehouse.upsert_bars(bars)
        entry = at_jump(draft_entries(warehouse, "tiingo"))
        assert entry["category"] == "review" and entry["proposed"] is None

    def test_moves_inside_the_threshold_are_not_listed(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "75"})
        assert draft_entries(warehouse, "tiingo") == []


class TestDraftSkips:
    def setup_jump(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "50"}, {0: "50"})

    def test_a_vendor_split_on_that_date_excuses_it(self, warehouse):
        self.setup_jump(warehouse)
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": JUMP, "action_type": "split",
             "ratio": D("2"), "source": "tiingo"},
        ])
        assert draft_entries(warehouse, "tiingo") == []

    def test_a_recorded_manual_split_excuses_it(self, warehouse):
        self.setup_jump(warehouse)
        warehouse.record_manual_split("AAA", JUMP, D("2"))
        assert draft_entries(warehouse, "tiingo") == []

    def test_a_split_from_another_source_does_not_excuse_it(self, warehouse):
        self.setup_jump(warehouse)
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": JUMP, "action_type": "split",
             "ratio": D("2"), "source": "elsewhere"},
        ])
        assert len(draft_entries(warehouse, "tiingo")) == 1

    def test_the_draft_and_the_runner_agree_on_what_is_excused(self, warehouse):
        from signalforge.config.models import ResearchConfig, RiskConfig
        from signalforge.paper.runner import PaperRunner, RunnerRefusal
        from signalforge.pipeline import ohlcv_history

        self.setup_jump(warehouse)
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": JUMP, "action_type": "split",
             "ratio": D("2"), "source": "elsewhere"},
        ])

        def runner_flags_the_jump() -> bool:
            runner = PaperRunner(
                warehouse=warehouse,
                research=ResearchConfig(starting_cash=D("100000"), universe=("AAA",)),
                risk=RiskConfig(),
                override_strategy=("buy_and_hold", {"symbols": ("AAA",), "target_weight": D("0.05")}),
                override_reason="agreement test",
                raw_basis=True,
                corporate_action_source="tiingo",
                start=DAYS[0],
            )
            history = ohlcv_history(warehouse, ["AAA"], sources=["tiingo"], price_basis="raw").history
            try:
                runner.run(history, through=DAYS[-1])
            except RunnerRefusal as exc:
                return "no split or adjustment recorded" in str(exc)
            return False

        assert len(draft_entries(warehouse, "tiingo")) == 1 and runner_flags_the_jump()
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": JUMP, "action_type": "split",
             "ratio": D("2"), "source": "tiingo"},
        ])
        assert draft_entries(warehouse, "tiingo") == [] and not runner_flags_the_jump()

    def test_an_acknowledged_move_is_skipped(self, warehouse):
        self.setup_jump(warehouse)
        warehouse.acknowledge_price_move("AAA", JUMP, "inspected")
        assert draft_entries(warehouse, "tiingo") == []

    def test_a_dividend_does_not_excuse_a_jump(self, warehouse):
        self.setup_jump(warehouse)
        warehouse.upsert_corporate_actions([
            {"symbol": "AAA", "effective_date": JUMP, "action_type": "dividend",
             "cash_amount": D("1"), "source": "tiingo"},
        ])
        assert len(draft_entries(warehouse, "tiingo")) == 1

    def test_only_the_named_source_is_read(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "50"}, {0: "50"}, source="other")
        assert draft_entries(warehouse, "tiingo") == []

    def test_symbols_restricts_the_scan(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "50"}, {0: "50"})
        seed(warehouse, "BBB", {0: "100", 30: "50"}, {0: "50"})
        entries = draft_entries(warehouse, "tiingo", symbols=["BBB"])
        assert {e["symbol"] for e in entries} == {"BBB"}
        assert {e["symbol"] for e in draft_entries(warehouse, "tiingo")} == {"AAA", "BBB"}


class TestApply:
    def entries(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "50"}, {0: "50"})
        seed(warehouse, "BBB", {0: "100", 30: "48.12"}, {0: "100", 30: "48.12"})
        return draft_entries(warehouse, "tiingo")

    def test_unapproved_entries_are_never_applied(self, warehouse):
        out = apply_entries(warehouse, self.entries(warehouse))
        assert out == []
        assert warehouse.load_corporate_actions(source="manual") == []
        assert warehouse.price_move_acknowledgements() == set()

    def test_approved_entries_are_applied_by_action(self, warehouse):
        entries = self.entries(warehouse)
        for e in entries:
            e["approved"] = True
        apply_entries(warehouse, entries)
        [split] = warehouse.load_corporate_actions(source="manual")
        assert (split.symbol, split.effective_date, split.numerator, split.denominator) == (
            "AAA", JUMP, 2, 1)
        assert warehouse.price_move_acknowledgements() == {("BBB", JUMP)}

    def test_only_the_approved_subset_is_applied(self, warehouse):
        entries = self.entries(warehouse)
        next(e for e in entries if e["symbol"] == "AAA")["approved"] = True
        apply_entries(warehouse, entries)
        assert len(warehouse.load_corporate_actions(source="manual")) == 1
        assert warehouse.price_move_acknowledgements() == set()

    def test_re_applying_is_a_no_op(self, warehouse):
        entries = self.entries(warehouse)
        for e in entries:
            e["approved"] = True
        first = apply_entries(warehouse, entries)
        second = apply_entries(warehouse, entries)
        assert len(first) == 2 and len(second) == 2
        assert not any("already" in line for line in first)
        assert all("already" in line for line in second)
        assert len(warehouse.load_corporate_actions(source="manual")) == 1
        assert len(warehouse.price_move_acknowledgements()) == 1

    def test_approved_with_no_proposal_is_refused_and_nothing_is_applied(self, warehouse):
        entries = self.entries(warehouse)
        for e in entries:
            e["approved"] = True
        entries.append({"symbol": "CCC", "date": JUMP.isoformat(), "category": "review",
                        "proposed": None, "approved": True})
        with pytest.raises(ValueError, match="CCC"):
            apply_entries(warehouse, entries)
        assert warehouse.load_corporate_actions(source="manual") == []
        assert warehouse.price_move_acknowledgements() == set()

    def test_a_drafted_then_applied_jump_is_not_drafted_again(self, warehouse):
        entries = self.entries(warehouse)
        for e in entries:
            e["approved"] = True
        apply_entries(warehouse, entries)
        assert draft_entries(warehouse, "tiingo") == []

    def test_a_review_entry_with_a_proposal_can_be_approved(self, warehouse):
        seed(warehouse, "AAA", {0: "100", 30: "49.58"}, {0: "100", 30: "49.58"})
        [entry] = draft_entries(warehouse, "tiingo")
        entry["approved"] = True
        apply_entries(warehouse, [entry])
        [split] = warehouse.load_corporate_actions(source="manual")
        assert (split.numerator, split.denominator) == (2, 1)


class TestApplyIsAtomic:
    def good(self, symbol="AAA"):
        return {"symbol": symbol, "date": JUMP.isoformat(), "category": "missing_split",
                "proposed": {"action": "manual_split", "ratio": "2.000000"}, "approved": True}

    def nothing_recorded(self, warehouse):
        return (
            warehouse.load_corporate_actions(source="manual") == []
            and warehouse.price_move_acknowledgements() == set()
        )

    @pytest.mark.parametrize(
        "bad",
        [
            {"action": "manual_split", "ratio": 2},
            {"action": "manual_split", "ratio": 2.0},
            {"action": "manual_split", "ratio": D("2")},
            {"action": "manual_split", "ratio": "-1"},
            {"action": "manual_split", "ratio": "abc"},
            {"action": "manual_split"},
            {"action": "manual_adjustment", "ratio": "0"},
            {"action": "acknowledge", "reason": "  "},
            {"action": "acknowledge"},
            {"action": "delete_everything"},
        ],
        ids=["int", "float", "decimal", "negative", "garbage", "no-ratio", "zero",
             "blank-reason", "no-reason", "unknown-action"],
    )
    def test_one_invalid_approved_entry_stops_the_whole_batch(self, warehouse, bad):
        entries = [self.good("AAA"), {**self.good("BBB"), "proposed": bad}]
        with pytest.raises(ValueError, match="nothing was recorded"):
            apply_entries(warehouse, entries)
        assert self.nothing_recorded(warehouse)

    def test_a_bad_date_stops_the_whole_batch(self, warehouse):
        entries = [self.good("AAA"), {**self.good("BBB"), "date": "not-a-date"}]
        with pytest.raises(ValueError, match="nothing was recorded"):
            apply_entries(warehouse, entries)
        assert self.nothing_recorded(warehouse)

    def test_a_failure_during_the_writes_rolls_the_earlier_writes_back(self, warehouse):
        warehouse.record_manual_split("BBB", JUMP, D("3"))
        entries = [self.good("AAA"), self.good("BBB")]
        with pytest.raises(ValueError, match="nothing was recorded"):
            apply_entries(warehouse, entries)
        assert [a.symbol for a in warehouse.load_corporate_actions(source="manual")] == ["BBB"]

    def test_unapproved_invalid_entries_are_ignored(self, warehouse):
        bad = {**self.good("BBB"), "approved": False, "proposed": {"action": "nope"}}
        assert len(apply_entries(warehouse, [self.good("AAA"), bad])) == 1

    def test_a_manual_adjustment_is_recorded_with_its_exact_ratio(self, warehouse):
        entry = {**self.good("AAA"), "proposed": {"action": "manual_adjustment", "ratio": "2.081110"}}
        apply_entries(warehouse, [entry])
        [action] = warehouse.load_corporate_actions(source="manual")
        assert action.kind == "adjustment"
        assert D(action.numerator) / D(action.denominator) == D("2.08111")


class TestCli:
    def seeded_db(self, tmp_path):
        path = tmp_path / "cli.duckdb"
        w = Warehouse(str(path))
        seed(w, "AAA", {0: "100", 30: "50"}, {0: "50"})
        seed(w, "BBB", {0: "100", 30: "48.12"}, {0: "100", 30: "48.12"})
        w.close()
        return path

    def test_draft_writes_the_json_and_prints_a_summary(self, tmp_path, capsys):
        db, out = self.seeded_db(tmp_path), tmp_path / "drafts.json"
        assert main(["draft", "--db", str(db), "--source", "tiingo", "--out", str(out)]) == 0
        data = json.loads(out.read_text())
        assert {e["category"] for e in data} == {"missing_split", "real_move"}
        assert set(data[0]) >= {"symbol", "date", "raw_ratio", "adj_ratio", "prior_close",
                                "close", "category", "proposed", "approved"}
        text = capsys.readouterr().out
        assert "missing_split" in text and "real_move" in text and "review" in text

    def test_draft_does_not_touch_the_warehouse_file(self, tmp_path):
        db = self.seeded_db(tmp_path)
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        main(["draft", "--db", str(db), "--out", str(tmp_path / "d.json")])
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before

    def test_draft_refuses_a_missing_warehouse_without_creating_one(self, tmp_path):
        db = tmp_path / "nope.duckdb"
        assert main(["draft", "--db", str(db)]) == 2
        assert not db.exists()

    def test_apply_round_trip_through_the_file(self, tmp_path):
        db, out = self.seeded_db(tmp_path), tmp_path / "drafts.json"
        main(["draft", "--db", str(db), "--out", str(out)])
        data = json.loads(out.read_text())
        for e in data:
            e["approved"] = e["symbol"] == "AAA"
        out.write_text(json.dumps(data))
        assert main(["apply", "--db", str(db), "--file", str(out)]) == 0
        assert main(["apply", "--db", str(db), "--file", str(out)]) == 0
        w = Warehouse(str(db))
        try:
            assert len(w.load_corporate_actions(source="manual")) == 1
            assert w.price_move_acknowledgements() == set()
        finally:
            w.close()

    def test_apply_exit_2_for_approved_without_a_proposal(self, tmp_path, capsys):
        db, out = self.seeded_db(tmp_path), tmp_path / "bad.json"
        out.write_text(json.dumps([{"symbol": "AAA", "date": JUMP.isoformat(),
                                    "category": "review", "proposed": None, "approved": True}]))
        assert main(["apply", "--db", str(db), "--file", str(out)]) == 2
        assert "AAA" in capsys.readouterr().out

    def test_a_numeric_ratio_in_the_file_is_refused_with_exit_2(self, tmp_path, capsys):
        db, out = self.seeded_db(tmp_path), tmp_path / "num.json"
        out.write_text('[{"symbol": "AAA", "date": "%s", "category": "missing_split", '
                       '"proposed": {"action": "manual_split", "ratio": 2.0}, "approved": true}]'
                       % JUMP.isoformat())
        assert main(["apply", "--db", str(db), "--file", str(out)]) == 2
        text = capsys.readouterr().out
        assert "string" in text and "nothing was recorded" in text

    def test_draft_will_not_overwrite_an_existing_file(self, tmp_path, capsys):
        db, out = self.seeded_db(tmp_path), tmp_path / "keep.json"
        out.write_text("[]")
        assert main(["draft", "--db", str(db), "--out", str(out)]) == 2
        assert out.read_text() == "[]"
        assert "--force" in capsys.readouterr().out

    def test_force_overwrites(self, tmp_path):
        db, out = self.seeded_db(tmp_path), tmp_path / "keep.json"
        out.write_text("[]")
        assert main(["draft", "--db", str(db), "--out", str(out), "--force"]) == 0
        assert len(json.loads(out.read_text())) == 2

    def test_a_warehouse_missing_phase_2_tables_gives_a_clean_message(self, tmp_path, capsys):
        path = tmp_path / "old.duckdb"
        con = duckdb.connect(str(path))
        con.execute("CREATE TABLE bars (symbol VARCHAR, source VARCHAR)")
        con.close()
        assert main(["draft", "--db", str(path), "--out", str(tmp_path / "o.json")]) == 2
        out = capsys.readouterr().out
        assert "missing" in out and "Traceback" not in out
        assert not (tmp_path / "o.json").exists()

    def test_symbols_and_universe_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            parse_args(["draft", "--symbols", "AAA", "--universe"])

    def test_symbols_are_parsed_upper_case(self):
        assert parse_args(["draft", "--symbols", "aaa, bbb"]).symbols == ["AAA", "BBB"]


class TestReadOnlyWarehouse:
    def test_a_read_only_warehouse_reads_and_refuses_writes(self, tmp_path):
        path = tmp_path / "ro.duckdb"
        w = Warehouse(str(path))
        seed(w, "AAA", {0: "100"})
        w.close()
        ro = Warehouse(str(path), read_only=True)
        try:
            assert len(ro.load_bars(symbols=["AAA"])) == len(DAYS)
            with pytest.raises(duckdb.Error):
                ro.acknowledge_price_move("AAA", JUMP, "no")
        finally:
            ro.close()