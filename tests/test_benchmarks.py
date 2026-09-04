"""Timing benchmarks for the paths whose cost scales with account size.

Skipped unless ``--benchmark`` is passed. See ``tests/bench.py`` for the
harness, the tolerance, and how to re-record the baseline.

What belongs here
-----------------
Work that grows with the number of series in the account and would regress
invisibly: assembling pages into a list, reducing a list export to id maps,
and the report writers that sort every row and wrap it into a card.

What does not: anything that makes a request, and anything dominated by
sleeps -- the retry backoff is deliberately slow and timing it measures the
sleep, not the code.

Every benchmark builds its input once, outside the timed callable.
"""

from __future__ import annotations

import pytest

import main as mu


def _items(count: int, *, start: int = 0) -> list[dict]:
    """A list export of ``count`` series, shaped like the real API response."""
    return [
        {
            "record": {
                "series": {
                    "id": start + n,
                    "title": f"Series {start + n:05d}",
                    "url": f"https://www.mangaupdates.com/series/{start + n}",
                }
            }
        }
        for n in range(count)
    ]


def _pages(total: int) -> list[list]:
    per = mu.ITEMS_PER_PAGE
    return [_items(min(per, total - offset), start=offset) for offset in range(0, total, per)]


@pytest.mark.benchmark
def test_joining_pages_of_a_large_list(bench):
    """Runs once per list; a big account is thousands of items across many pages."""
    pages = _pages(5000)
    bench("join_pages/5000_items", lambda: mu._join_pages(pages, 5000))


@pytest.mark.benchmark
def test_reducing_an_export_to_series_ids(bench):
    """get_series_ids runs over every list, repeatedly, during a comparison."""
    items = _items(5000)
    bench("get_series_ids/5000_items", lambda: mu.get_series_ids(items))


@pytest.mark.benchmark
def test_reducing_an_export_to_basic_records(bench):
    items = _items(5000)
    bench("get_series_basic/5000_items", lambda: mu.get_series_basic(items))


@pytest.mark.benchmark
def test_export_filename_mapping(bench):
    """Quadratic if the collision loop is ever changed carelessly."""
    titles = [f"List {n:04d}" for n in range(500)]
    bench("export_filenames/500_lists", lambda: mu.export_filenames(titles))


@pytest.mark.benchmark
def test_writing_the_related_series_report(bench, tmp_path, monkeypatch):
    """Sorts every entry and every source, then wraps a bordered card for each.

    The card layout emits about seven lines per entry where the old aligned
    table emitted one, so this timing is not comparable to the one recorded
    before that change: roughly 7x the output at a third the cost per line.
    What it still guards is the shape of the cost -- the wrapper measures a
    growing prefix once per word and once per character of a hard-broken URL,
    so losing the ASCII fast path in _display_width shows up here as an order
    of magnitude rather than as drift.
    """
    monkeypatch.setattr(mu, "EXPORTS_DIR", str(tmp_path))
    related = {
        n: {
            "title": f"Related {n:05d}",
            "url": f"https://www.mangaupdates.com/series/{n}",
            "sources": [(f"Origin {n % 50}", "Sequel"), (f"Origin {(n + 1) % 50}", "Prequel")],
        }
        for n in range(2000)
    }
    bench("save_related_series/2000_entries", lambda: mu.save_related_series(related), repeats=3)


@pytest.mark.benchmark
def test_writing_the_finished_series_report(bench, tmp_path, monkeypatch):
    monkeypatch.setattr(mu, "EXPORTS_DIR", str(tmp_path))
    finished = {
        n: {"title": f"Done {n:05d}", "url": f"https://www.mangaupdates.com/series/{n}", "status": "Complete"}
        for n in range(2000)
    }
    bench("save_finished_series/2000_entries", lambda: mu.save_finished_series(finished, 2000), repeats=3)


@pytest.mark.benchmark
def test_display_width_of_a_box_line(bench):
    """Runs per character of every boxed line; an emoji-heavy line is worst case."""
    line = "  📊 Summary: 12 list(s), 3456 item(s), in 12.3s  ⚠️  漢字テスト" * 4
    bench("display_width/long_mixed_line", lambda: mu._display_width(line))
