"""Large tables must be capped without the page lying about the total.

The real dashboard reached 12 MB: a 6,799-symbol coverage table plus each
run's risk events rendered four times over (all / blocked / martingale /
per-rule) across fifteen runs. That is unusable on a phone.

Capping is only honest if the page still states the true total and the
clickable count matches what the reader can actually see. A link saying
"4,812 events" that lands on 250 rows is worse than no link at all.
"""

import re

from signalforge.dashboard.sections import (
    MAX_TABLE_ROWS,
    capped_count_link,
    count_link,
    table_html,
)


def rows(n: int) -> list[str]:
    return [f'<tr class="row"><td>{i}</td></tr>' for i in range(n)]


class TestTableHtmlCapping:
    def test_a_small_table_renders_every_row(self):
        html = table_html("t", ["A"], rows(10), "empty")
        assert html.count('class="row"') == 10

    def test_an_oversized_table_is_capped(self):
        html = table_html("t", ["A"], rows(5000), "empty")
        assert html.count('class="row"') == MAX_TABLE_ROWS

    def test_a_capped_table_states_the_true_total(self):
        html = table_html("t", ["A"], rows(5000), "empty")
        assert "5,000" in html

    def test_a_capped_table_says_how_many_it_is_showing(self):
        html = table_html("t", ["A"], rows(5000), "empty")
        assert format(MAX_TABLE_ROWS, ",") in html
        assert "showing" in html.lower()

    def test_an_uncapped_table_carries_no_truncation_notice(self):
        html = table_html("t", ["A"], rows(10), "empty")
        assert "showing" not in html.lower()

    def test_an_empty_table_still_renders_its_empty_state(self):
        html = table_html("t", ["A"], [], "<p>nothing here</p>")
        assert "nothing here" in html

    def test_the_cap_can_be_raised_per_table(self):
        html = table_html("t", ["A"], rows(300), "empty", max_rows=1000)
        assert html.count('class="row"') == 300

    def test_the_rendered_row_count_is_published_for_the_count_contract(self):
        html = table_html("t", ["A"], rows(5000), "empty")
        match = re.search(r'data-rendered-rows="(\d+)"', html)
        assert match is not None
        assert int(match.group(1)) == MAX_TABLE_ROWS

    def test_the_true_total_is_published_too(self):
        html = table_html("t", ["A"], rows(5000), "empty")
        match = re.search(r'data-total-rows="(\d+)"', html)
        assert match is not None
        assert int(match.group(1)) == 5000


class TestCappedCountLink:
    def test_an_uncapped_count_reads_as_a_plain_count(self):
        html = capped_count_link(10, 10, "event", "events", "target")
        assert "10 events" in html
        assert "of" not in html.replace("target", "")

    def test_a_capped_count_states_both_numbers(self):
        html = capped_count_link(5000, MAX_TABLE_ROWS, "event", "events", "target")
        assert "5,000" in html
        assert format(MAX_TABLE_ROWS, ",") in html

    def test_the_clickable_data_count_matches_the_rows_the_reader_will_see(self):
        # This is the contract: the number you click equals the number of rows
        # at the other end of the link.
        html = capped_count_link(5000, MAX_TABLE_ROWS, "event", "events", "target")
        match = re.search(r'data-count="(\d+)"', html)
        assert int(match.group(1)) == MAX_TABLE_ROWS

    def test_the_singular_is_used_for_one(self):
        assert "1 event" in capped_count_link(1, 1, "event", "events", "t")

    def test_a_plain_count_link_is_unchanged(self):
        html = count_link(7, "event", "events", "target")
        assert "7 events" in html
        assert 'data-count="7"' in html
