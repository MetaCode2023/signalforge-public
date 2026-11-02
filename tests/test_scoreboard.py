"""The Scoreboard tab: verdicts come from the numbers, never from the page.

Every row compares an idea with a bar measured on the same data. These tests
pin the rules: which rows count, which bar each is held to, and that the
page stays static, escaped and honest when a source is missing.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from signalforge.dashboard import scoreboard_page as sb
from signalforge.forecast_cli import with_backtest
from signalforge.insiders.report import record
from signalforge.insiders.study import Params, run
from signalforge.learning.registry import _COLUMNS
from tests.insider_support import world

ROOT = Path(__file__).resolve().parents[1]
CRUCIBLE_RECORD = ROOT / "reports" / "forecast_trials" / "trial-random_walk+block_bootstrap-2005-01-01-2014-12-31.json"
NOW = datetime(2026, 11, 1, 19, 30)
UNIVERSE = "2952113 OHLCV bars for 20 symbols from tiingo, 1972-01-25 to 2026-10-27, adjusted price basis"
INDEX = "NASDAQCOM daily close from fred, 14048 observations 1971-02-05 to 2026-10-23"


def experiment(name, window, fingerprint, sharpe, *, description=UNIVERSE, evidence="SUFFICIENT",
               params=None, created=datetime(2026, 10, 28, 12)):
    row = dict.fromkeys(_COLUMNS)
    row.update(
        experiment_id=f"{name}-{window}-{fingerprint}-{created.isoformat()}", strategy_name=name,
        parameters=json.dumps(params or {"symbols": ["SPY"]} if name == "buy_and_hold" else params or {}),
        dataset_fingerprint=fingerprint, dataset_description=description, evaluated_window=window,
        validation_start=date(2005, 1, 1), validation_end=date(2014, 12, 31),
        test_start=date(2015, 1, 1), test_end=date(2026, 10, 23),
        metrics=json.dumps({"sharpe": None if sharpe is None else str(sharpe)}),
        evidence_level=evidence, created_at=created,
    )
    return tuple(row[c] for c in _COLUMNS)


def registry(*rows):
    return {"columns": list(_COLUMNS), "rows": list(rows)}


@pytest.fixture
def classic():
    return registry(
        experiment("buy_and_hold", "validation", "u1", "0.50"),
        experiment("ma_trend", "validation", "u1", "0.30", params={"fast_window": 50, "slow_window": 200}),
        experiment("xs_momentum", "validation", "u1", "0.08", params={"lookback_days": 126, "skip_days": 21}),
        experiment("zscore_mean_reversion", "validation", "u1", "-0.32", params={"entry_z": "2"}),
        experiment("ma_trend_nobreaker", "validation", "u1", "0.90"),
        experiment("ma_trend", "train", "u0", "0.99"),
        experiment("buy_and_hold", "test", "i1", "0.68", description=INDEX, params={"symbols": ["NASDAQCOM"]}),
        experiment("xs_momentum", "test", "i1", "0.68", description=INDEX, evidence="INSUFFICIENT_EVIDENCE"),
        experiment("ma_trend", "test", "i1", "0.32", description=INDEX),
    )


def crucible():
    return [(CRUCIBLE_RECORD.name, json.loads(CRUCIBLE_RECORD.read_text(encoding="utf-8")))]


def verdicts(groups, family=None):
    return {r.idea: r.verdict for g in groups if family in (None, g.family) for r in g.rows}


class TestClassic:
    def test_each_strategy_is_held_to_the_benchmark_on_its_own_data(self, classic):
        groups = sb.classic_groups(classic)
        assert [g.title for g in groups] == [
            "Classic strategies, sealed exam, evaluated once", "Classic strategies, practice years",
        ]
        test, validation = groups
        assert test.bar.startswith("bar = 0.68 Sharpe") and "NASDAQCOM" in test.bar
        assert validation.bar == "bar = 0.50 Sharpe, buy and hold SPY"
        assert verdicts([validation]) == {
            "Trend 50/200": "LOST", "Momentum 126/21": "LOST", "Mean reversion z=2": "LOST",
        }
        assert verdicts([test])["Momentum"] == "TIED"

    def test_train_rows_folds_and_breaker_free_variants_stay_off_the_board(self, classic):
        rows = [r for g in sb.classic_groups(classic) for r in g.rows]
        assert all(r.score != "0.90 / 0.50" and r.score != "0.99 / 0.50" for r in rows)
        fold = registry(
            experiment("buy_and_hold", "validation", "f1", "0.4", description="SPY 2005..2006, 500 bars; limitations: none recorded"),
            experiment("ma_trend", "validation", "f1", "0.9", description="SPY 2005..2006, 500 bars; limitations: none recorded"),
        )
        assert sb.classic_groups(fold) == []

    def test_a_group_without_a_benchmark_is_not_scored(self):
        assert sb.classic_groups(registry(experiment("ma_trend", "validation", "x", "2.0"))) == []

    def test_a_rebuilt_warehouse_replaces_the_older_result(self, classic):
        newer = UNIVERSE.replace("2952113", "2953000").replace("2026-10-27", "2026-11-01")
        later = datetime(2026, 11, 1, 9)
        rows = classic["rows"] + [
            experiment("buy_and_hold", "validation", "u2", "0.40", description=newer, created=later),
            experiment("ma_trend", "validation", "u2", "0.45", description=newer, created=later,
                       params={"fast_window": 50, "slow_window": 200}),
        ]
        practice = [g for g in sb.classic_groups(registry(*rows)) if "practice" in g.title]
        assert len(practice) == 1 and practice[0].bar.startswith("bar = 0.40")
        assert verdicts(practice) == {"Trend 50/200": "BEAT"}

    def test_ahead_on_too_few_trades_is_not_a_win(self):
        board = registry(
            experiment("buy_and_hold", "validation", "u", "0.5"),
            experiment("ma_trend", "validation", "u", "0.8", evidence="INSUFFICIENT_EVIDENCE"),
        )
        groups = sb.classic_groups(board)
        assert verdicts(groups) == {"Trend": "AHEAD_THIN"}
        assert sb.tally(groups)["beat"] == 0

    def test_the_verdict_follows_the_benchmark(self):
        def verdict(bar):
            return verdicts(sb.classic_groups(registry(
                experiment("buy_and_hold", "validation", "u", bar), experiment("ma_trend", "validation", "u", "0.30"))))
        assert verdict("0.50") == {"Trend": "LOST"}
        assert verdict("0.20") == {"Trend": "BEAT"}
        assert verdict("0.304") == {"Trend": "TIED"}


class TestCrucible:
    def test_the_committed_trial_loses_to_always_up_and_to_holding(self):
        groups = sb.crucible_groups(crucible())
        calling, trading = groups
        assert calling.bar == "bar = always guess UP, right 61.2% of the time"
        assert verdicts([calling]) == {"Coin-flip forecaster": "LOST", "Copycat cone forecaster": "LOST"}
        assert trading.bar == "bar = 0.86 Sharpe, buy and hold the same stocks"
        rows = {r.idea: r for r in trading.rows}
        assert rows["Cone rule, copycat forecaster"].verdict == "LOST"
        assert "$110,689" in rows["Cone rule, copycat forecaster"].detail and "$260,447" in rows["Cone rule, copycat forecaster"].detail

    def test_a_forecaster_beats_the_bar_only_when_its_whole_interval_clears_it(self):
        name, rec = crucible()[0]
        kronos = json.loads(json.dumps(rec["forecasters"][1]))
        kronos["forecaster"] = "kronos"
        rec = {**rec, "forecasters": [kronos], "backtest": {}}
        kronos["summary"].update(hit_rate=0.66, hit_interval=[0.63, 0.69])
        assert verdicts(sb.crucible_groups([(name, rec)])) == {"Kronos, the AI model": "BEAT"}
        kronos["summary"].update(hit_rate=0.64, hit_interval=[0.60, 0.68])
        assert verdicts(sb.crucible_groups([(name, rec)])) == {"Kronos, the AI model": "UNCLEAR"}
        kronos["summary"].update(n=12)
        assert verdicts(sb.crucible_groups([(name, rec)])) == {"Kronos, the AI model": "THIN"}

    def test_kronos_stays_queued_until_a_record_has_it(self):
        waiting = sb.queued_group(crucible(), [])
        assert "Kronos, the AI model" in verdicts([waiting])
        name, rec = crucible()[0]
        ran = {**rec, "forecasters": [{**rec["forecasters"][0], "forecaster": "kronos"}]}
        assert "Kronos, the AI model" not in verdicts([sb.queued_group([(name, ran)], [])])

    def test_the_saved_record_carries_the_engine_run_without_its_curve(self):
        saved = with_backtest({"kind": "forecast_trial"}, {
            "buy_and_hold": {"equity": [1.0, 2.0], "final": 2.0, "sharpe": 0.9},
            "cone_kronos": {"equity": [1.0, 1.1], "final": 1.1, "sharpe": None},
        })
        assert saved["backtest"] == {"buy_and_hold": {"final": 2.0, "sharpe": 0.9},
                                     "cone_kronos": {"final": 1.1, "sharpe": None}}
        assert "equity" not in json.dumps(saved)


@pytest.fixture(scope="module")
def assay():
    purchases, panel, _ = world(n_symbols=30, n_owners=160, seed=4)
    result = run(purchases, panel, Params(window_start=date(2008, 1, 1), window_end=panel.days[-1], draws=80))
    return [("assay-2008-01-01-2015-03-17.json", record(result))]


class TestAssay:
    def test_honest_picks_face_random_insiders_and_hindsight_is_never_scored(self, assay):
        (group,) = sb.assay_groups(assay)
        v = verdicts([group])
        assert v["Smart insiders, picked with hindsight"] == "FUTURE"
        assert set(v) == {"Copy smart insiders", "Copy insider clusters", "Copy every insider buy",
                          "Smart insiders, picked with hindsight"}
        p = assay[0][1]["nulls"]["random_insiders_vs_smart_past"]["p_value"]
        assert v["Copy smart insiders"] == ("BEAT" if p < 0.05 else "NO_EDGE")
        assert sb.queued_group([], assay) is not None and "Copy smart insiders" not in verdicts([sb.queued_group([], assay)])


class TestPage:
    def test_an_empty_board_says_so_and_queues_the_untested(self):
        html = sb.render_scoreboard_page(now=NOW)
        assert "No results yet." in html and "DATA UNAVAILABLE" in html
        assert html.count(">QUEUED<") == 3
        assert "<script" not in html.lower() and not re.search(r'(src|href)="https?://', html)

    def test_full_board_headline_counts_the_scored_rows(self, classic):
        html = sb.render_scoreboard_page(experiments=classic, crucible=crucible(), now=NOW)
        assert "Not yet." in html and "None cleared the bar" in html
        assert ">BEAT THE BAR<" not in html and "DATA UNAVAILABLE" not in html

    def test_a_win_changes_the_headline_and_says_it_is_not_a_promotion(self):
        board = registry(experiment("buy_and_hold", "validation", "u", "0.2"), experiment("ma_trend", "validation", "u", "0.6"))
        html = sb.render_scoreboard_page(experiments=board, now=NOW)
        assert "1 cleared it." in html and "not a promotion" in html and ">BEAT THE BAR<" in html

    def test_hostile_text_is_escaped(self):
        board = registry(
            experiment("buy_and_hold", "validation", "u", "0.5", description='<script>alert(1)</script>'),
            experiment("<img src=x>", "validation", "u", "0.1", description='<script>alert(1)</script>'),
        )
        html = sb.render_scoreboard_page(experiments=board, notes=["<b>x</b>"], now=NOW)
        assert "<script>alert" not in html and "<img src=x>" not in html
        assert "&lt;script&gt;" in html and "&lt;b&gt;" in html

    def test_motion_stops_for_reduced_motion(self):
        html = sb.render_scoreboard_page(now=NOW)
        block = html[html.index("@media (prefers-reduced-motion: reduce){.forge"):]
        assert "animation:none" in block.split("}")[0]

    def test_unreadable_records_are_reported_not_skipped_quietly(self, tmp_path):
        folder = tmp_path / "forecast_trials"
        folder.mkdir()
        (folder / "broken.json").write_text("{not json", encoding="utf-8")
        (folder / "other.json").write_text(json.dumps({"kind": "something_else"}), encoding="utf-8")
        found, problems = sb.load_records(folder, "forecast_trial")
        assert found == [] and problems == ["broken.json could not be read: JSONDecodeError"]

    def test_the_dashboard_build_writes_the_scoreboard(self, tmp_path):
        from signalforge.dashboard.__main__ import main

        reports = tmp_path / "reports" / "forecast_trials"
        reports.mkdir(parents=True)
        (reports / CRUCIBLE_RECORD.name).write_bytes(CRUCIBLE_RECORD.read_bytes())
        out = tmp_path / "build" / "index.html"
        assert main(["--db", str(tmp_path / "missing.duckdb"), "--out", str(out), "--reports-dir", str(tmp_path / "reports")]) == 0
        html = (out.parent / "scoreboard.html").read_text(encoding="utf-8")
        assert "Crucible, calling direction" in html and "no warehouse file found" in html
        for page in ("index.html", "forecast.html", "assay.html"):
            assert 'href="scoreboard.html"' in (out.parent / page).read_text(encoding="utf-8")

    def test_refresh_never_raises(self, tmp_path):
        board = sb.refresh_scoreboard(tmp_path / "scoreboard.html", tmp_path / "missing.duckdb", tmp_path)
        assert board is not None and "No warehouse at" in board.read_text(encoding="utf-8")
