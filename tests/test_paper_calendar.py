"""The calendar and freshness gate in front of an OHLCV paper run."""

from datetime import date, timedelta

from signalforge.paper.calendar import RECENT_SESSIONS, check_freshness

from tests.conftest import business_days

CAL = business_days(date(2024, 1, 1), 30)
LONG = business_days(date(2023, 6, 1), 90)
T = CAL[-1]


def gate(symbol_dates, through=T, window_start=None, calendar=CAL):
    return check_freshness(calendar, symbol_dates, through, window_start)


def problems(*args, **kw):
    return gate(*args, **kw)[0]


class TestFreshnessGate:
    def test_complete_symbols_pass(self):
        assert gate({"AAA": CAL, "BBB": CAL}) == ([], [])

    def test_through_after_the_calendar_end_is_refused(self):
        out = problems({"AAA": CAL}, through=date(2024, 3, 1))
        assert any("after the calendar's last date" in m and str(T) in m for m in out)

    def test_a_symbol_whose_last_bar_is_before_through_is_refused(self):
        out = problems({"AAA": CAL, "BBB": CAL[:-3]})
        assert any(m.startswith("BBB") and "last bar" in m and str(CAL[-4]) in m for m in out)
        assert not any(m.startswith("AAA") for m in out)

    def test_a_holiday_through_compares_against_the_last_session_on_or_before_it(self):
        holiday = CAL[10]
        calendar = [d for d in CAL if d != holiday]
        have = [d for d in calendar]
        assert gate({"AAA": have}, through=holiday, calendar=calendar) == ([], [])

    def test_a_holiday_through_still_refuses_a_symbol_short_of_the_prior_session(self):
        holiday = CAL[10]
        calendar = [d for d in CAL if d != holiday]
        have = [d for d in calendar if d < CAL[9]]
        out = problems({"AAA": have}, through=holiday, calendar=calendar)
        assert any("last bar" in m and str(CAL[9]) in m for m in out)

    def test_a_through_before_the_calendar_starts_is_refused(self):
        out = problems({"AAA": CAL}, through=CAL[0] - timedelta(days=30))
        assert out

    def test_a_recent_hole_is_named_with_its_dates(self):
        have = [d for d in CAL if d not in (CAL[5], CAL[6])]
        joined = " ".join(problems({"AAA": have}))
        assert "AAA" in joined and str(CAL[5]) in joined and str(CAL[6]) in joined

    def test_missing_dates_are_capped_at_ten(self):
        have = [CAL[0]] + CAL[13:]
        missing = [m for m in problems({"AAA": have}) if "missing" in m][0]
        assert str(CAL[1]) in missing and str(CAL[10]) in missing
        assert str(CAL[11]) not in missing
        assert "12" in missing

    def test_a_hole_older_than_the_recent_window_is_a_warning_not_a_refusal(self):
        old = LONG[5]
        assert old < LONG[-RECENT_SESSIONS]
        have = [d for d in LONG if d != old]
        refusals, warnings = gate({"AAA": have}, through=LONG[-1], calendar=LONG)
        assert refusals == []
        assert len(warnings) == 1 and "AAA" in warnings[0] and str(old) in warnings[0]

    def test_a_hole_inside_the_recent_window_is_still_refused(self):
        recent = LONG[-RECENT_SESSIONS]
        have = [d for d in LONG if d != recent]
        refusals, warnings = gate({"AAA": have}, through=LONG[-1], calendar=LONG)
        assert refusals and str(recent) in refusals[0] and warnings == []

    def test_old_warnings_cap_the_dates_shown_and_count_the_rest(self):
        have = [d for d in LONG if d not in LONG[3:18]]
        refusals, warnings = gate({"AAA": have}, through=LONG[-1], calendar=LONG)
        assert refusals == []
        assert str(LONG[3]) in warnings[0] and str(LONG[12]) in warnings[0]
        assert str(LONG[13]) not in warnings[0] and "15" in warnings[0]

    def test_dates_before_a_symbols_first_bar_are_not_missing(self):
        assert gate({"AAA": CAL[10:]}) == ([], [])

    def test_holes_before_the_window_start_are_not_checked(self):
        have = [d for d in CAL if d != CAL[5]]
        assert gate({"AAA": have}, window_start=CAL[10]) == ([], [])
        assert problems({"AAA": have}, window_start=CAL[5]) != []

    def test_a_hole_after_through_is_not_checked(self):
        have = [d for d in CAL if d != CAL[-1]]
        assert gate({"AAA": have}, through=CAL[-2]) == ([], [])

    def test_an_empty_calendar_is_refused(self):
        out = problems({"AAA": CAL}, calendar=[])
        assert out and "calendar" in out[0]

    def test_a_symbol_with_no_bars_is_refused(self):
        out = problems({"AAA": CAL, "BBB": []})
        assert any(m.startswith("BBB") for m in out)
