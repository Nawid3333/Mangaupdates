"""Unit tests for the Mangaupdates exporter's pure logic.

Run with:  python -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

os.environ.setdefault("MU_USERNAME", "test")
os.environ.setdefault("MU_PASSWORD", "test")

import main as mu  # noqa: E402


def _rec(sid: int, title: str) -> dict:
    return {"record": {"series": {"id": sid, "title": title}}}


class TempExportsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._orig_exports_dir = mu.EXPORTS_DIR
        mu.EXPORTS_DIR = self.dir.name
        self.addCleanup(self._restore_exports_dir)

    def _restore_exports_dir(self) -> None:
        mu.EXPORTS_DIR = self._orig_exports_dir


# ==================== sanitize_filename ====================
class TestSanitizeFilename(unittest.TestCase):
    def test_strips_unsafe_characters(self):
        name = 'a<b>c:d"e/f' + chr(92) + 'g|h?i*j'
        self.assertEqual(mu.sanitize_filename(name), "a_b_c_d_e_f_g_h_i_j")

    def test_empty_input_falls_back(self):
        self.assertEqual(mu.sanitize_filename(""), "Unnamed_List")

    def test_all_dots_falls_back(self):
        # strip(".") on a name of only dots leaves nothing behind.
        self.assertEqual(mu.sanitize_filename("..."), "Unnamed_List")

    def test_unsafe_characters_become_underscores_not_empty(self):
        # Each unsafe char is individually replaced, so this is NOT empty.
        self.assertEqual(mu.sanitize_filename("///"), "___")

    def test_two_distinct_titles_can_collide(self):
        """The premise the manifest fix exists for: this collision is real."""
        a, b = "Sci-Fi/Fantasy", "Sci-Fi_Fantasy"
        self.assertNotEqual(a, b)
        self.assertEqual(mu.sanitize_filename(a), mu.sanitize_filename(b))


# ==================== export_filenames / load_manifest ====================
class TestExportFilenames(unittest.TestCase):
    def test_collision_gets_deduplicated(self):
        mapping = mu.export_filenames(["Sci-Fi/Fantasy", "Sci-Fi_Fantasy"])
        self.assertEqual(len(set(mapping.values())), 2, "both titles must map to distinct files")

    def test_mapping_is_order_stable(self):
        titles = ["A", "B", "C"]
        self.assertEqual(mu.export_filenames(titles), mu.export_filenames(list(titles)))


class TestLoadManifest(TempExportsCase):
    def test_falls_back_to_recompute_when_no_manifest_file(self):
        os.makedirs(os.path.join(self.dir.name, "folder"))
        result = mu.load_manifest(os.path.join(self.dir.name, "folder"), ["Reading"])
        self.assertEqual(result, {"Reading": "Reading"})

    def test_prefers_the_stored_manifest_over_recomputing(self):
        folder = os.path.join(self.dir.name, "folder")
        os.makedirs(folder)
        with open(os.path.join(folder, mu.MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump({"Reading": "Reading_custom"}, f)
        result = mu.load_manifest(folder, ["Reading"])
        self.assertEqual(result, {"Reading": "Reading_custom"})

    def test_corrupt_manifest_falls_back_gracefully(self):
        folder = os.path.join(self.dir.name, "folder")
        os.makedirs(folder)
        with open(os.path.join(folder, mu.MANIFEST_NAME), "w", encoding="utf-8") as f:
            f.write("{not valid json")
        result = mu.load_manifest(folder, ["Reading"])
        self.assertEqual(result, {"Reading": "Reading"})


# ==================== the filename-collision regression ====================
class TestFilenameCollisionRegression(TempExportsCase):
    """Before this fix: two titles sanitizing to the same name made the
    second list's diff silently read the first list's file. Verified against
    the real behavior, not just the mapping in isolation."""

    def test_colliding_titles_each_read_back_their_own_data(self):
        a, b = "Sci-Fi/Fantasy", "Sci-Fi_Fantasy"
        exports = {a: [_rec(1, "AAA")], b: [_rec(2, "BBB")]}
        folder = mu.save_exports(exports)

        loaded = mu._load_prev_exports(folder, list(exports.keys()))
        self.assertEqual(mu.get_series_ids(loaded[a]), {1: "AAA"})
        self.assertEqual(mu.get_series_ids(loaded[b]), {2: "BBB"})

    def test_manifest_is_written_and_not_reported_as_a_removed_list(self):
        mu.save_exports({"Reading": [_rec(1, "X")]})
        time.sleep(1.1)  # folder names have 1-second resolution
        folder2 = mu.save_exports({"Reading": [_rec(1, "X")]})
        changed = mu.compare_exports(folder2, {"Reading": [_rec(1, "X")]})
        self.assertFalse(changed, "identical export must report no changes")

    def test_manifest_less_folder_from_before_this_fix_still_compares(self):
        old_folder = os.path.join(self.dir.name, "01.01.2026_00-00-00")
        os.makedirs(old_folder)
        with open(os.path.join(old_folder, "Reading.json"), "w", encoding="utf-8") as f:
            json.dump([_rec(1, "Old Series")], f)
        time.sleep(1.1)
        new_items = [_rec(1, "Old Series"), _rec(2, "New Series")]
        new_folder = mu.save_exports({"Reading": new_items})
        changed = mu.compare_exports(new_folder, {"Reading": new_items})
        self.assertTrue(changed, "the added series must be detected against a legacy folder")


# ==================== save_exports / rotate_exports ====================
class TestSaveExports(TempExportsCase):
    def test_partial_write_never_produces_a_visible_incomplete_folder(self):
        """save_exports must not leave `<folder>.tmp` visible under the final name."""
        exports = {"Reading": [_rec(1, "X")]}
        folder = mu.save_exports(exports)
        self.assertTrue(os.path.isdir(folder))
        self.assertFalse(os.path.isdir(folder + ".tmp"))

    def test_writes_a_manifest_covering_every_title(self):
        exports = {"A": [_rec(1, "x")], "B": [_rec(2, "y")]}
        folder = mu.save_exports(exports)
        with open(os.path.join(folder, mu.MANIFEST_NAME), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(set(manifest), {"A", "B"})


class TestRotateExports(TempExportsCase):
    def test_keeps_only_the_newest_max_exports(self):
        names = ["01.01.2026_00-00-00", "02.01.2026_00-00-00", "03.01.2026_00-00-00", "04.01.2026_00-00-00"]
        for name in names:
            os.makedirs(os.path.join(self.dir.name, name))
        original_max = mu.MAX_EXPORTS
        mu.MAX_EXPORTS = 2
        try:
            mu.rotate_exports()
        finally:
            mu.MAX_EXPORTS = original_max
        remaining = sorted(os.listdir(self.dir.name))
        self.assertEqual(remaining, names[-2:])


# ==================== find_previous_export ====================
class TestFindPreviousExport(TempExportsCase):
    def test_returns_none_when_no_exports_directory(self):
        empty = os.path.join(self.dir.name, "does-not-exist")
        self.assertIsNone(mu.find_previous_export(os.path.join(empty, "x")))

    def test_picks_the_most_recent_folder_strictly_before_current(self):
        for name in ["01.01.2026_00-00-00", "02.01.2026_00-00-00", "03.01.2026_00-00-00"]:
            os.makedirs(os.path.join(self.dir.name, name))
        current = os.path.join(self.dir.name, "03.01.2026_00-00-00")
        prev = mu.find_previous_export(current)
        self.assertEqual(os.path.basename(prev), "02.01.2026_00-00-00")



# ==================== 429 retry behavior ====================
class TestApiRequestRetriesRateLimit(unittest.TestCase):
    """429 used to slip through uncaught: only status >= 500 retried, so a
    rate-limited response reached the caller's raise_for_status() and crashed
    the run. Both the retry and the Retry-After handling are verified here
    without touching the network."""

    def _fake_response(self, status_code, headers=None):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.headers = headers or {}
        resp.request = MagicMock()
        return resp

    def test_429_is_retried_then_succeeds(self):
        rate_limited = self._fake_response(429, {"Retry-After": "0"})
        ok = self._fake_response(200)
        client = MagicMock()
        client.get.side_effect = [rate_limited, ok]

        with patch.object(mu, "time") as mock_time:
            result = mu._api_request(client, "get", "https://example.invalid")

        self.assertIs(result, ok)
        self.assertEqual(client.get.call_count, 2)
        mock_time.sleep.assert_called_once()

    def test_429_without_retry_after_uses_configured_delay(self):
        rate_limited = self._fake_response(429, {})
        ok = self._fake_response(200)
        client = MagicMock()
        client.get.side_effect = [rate_limited, ok]

        with patch.object(mu, "time") as mock_time:
            mu._api_request(client, "get", "https://example.invalid")

        mock_time.sleep.assert_called_once_with(mu.RETRY_DELAY)

    def test_retry_after_header_overrides_configured_delay(self):
        resp = self._fake_response(429, {"Retry-After": "17"})
        self.assertEqual(mu._retry_delay(resp), 17.0)

    def test_malformed_retry_after_falls_back_to_configured_delay(self):
        resp = self._fake_response(429, {"Retry-After": "not-a-number"})
        self.assertEqual(mu._retry_delay(resp), mu.RETRY_DELAY)

    def test_no_response_falls_back_to_configured_delay(self):
        self.assertEqual(mu._retry_delay(None), mu.RETRY_DELAY)

    def test_exhausting_retries_on_429_raises(self):
        client = MagicMock()
        client.get.return_value = self._fake_response(429, {"Retry-After": "0"})

        with patch.object(mu, "time"), self.assertRaises(httpx.HTTPStatusError):
            mu._api_request(client, "get", "https://example.invalid")

        self.assertEqual(client.get.call_count, mu.MAX_RETRIES)



# ==================== related-series discovery ====================
class _FakeApiResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.headers = {}
        self.request = None

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=self.request, response=self)


class _FakeSeriesClient:
    """Stands in for httpx.Client, keyed by series id -> related_series payload.

    A value of "404" simulates a series that no longer exists on the site.
    """

    def __init__(self, by_id: dict) -> None:
        self.by_id = by_id
        self.calls: list[int] = []

    def get(self, url, **kwargs):  # noqa: ARG002
        sid = int(url.rsplit("/", 1)[-1])
        self.calls.append(sid)
        if self.by_id.get(sid) == "404":
            return _FakeApiResponse(404, {})
        return _FakeApiResponse(200, {"related_series": self.by_id.get(sid, [])})


ONE_PIECE_RELATIONS = [
    {
        "relation_id": 99065,
        "relation_type": "Spin-Off",
        "related_series_id": 49512032547,
        "related_series_name": "One Piece Party",
        "related_series_url": "https://www.mangaupdates.com/series/mqu6otf/one-piece-party",
    },
    {
        "relation_id": 99066,
        "relation_type": "Alternate Version",
        "related_series_id": 14853239448,
        "related_series_name": "Wanted!",
        "related_series_url": "https://www.mangaupdates.com/series/6tn8gwo/wanted",
    },
]


class TestCollectRelatedSeries(unittest.TestCase):
    """Field shapes here (an always-present list, [] when empty) were
    verified against the live MangaUpdates API before this was built."""

    def test_finds_new_related_series_and_excludes_already_tracked(self):
        exports = {
            "Reading": [_rec(1, "One Piece"), _rec(14853239448, "Wanted!")],
            "Wish": [_rec(3, "Deleted Series")],
        }
        client = _FakeSeriesClient({1: ONE_PIECE_RELATIONS, 14853239448: [], 3: "404"})

        related = mu.collect_related_series(client, exports)

        self.assertIn(49512032547, related, "One Piece Party is new and must be found")
        self.assertEqual(related[49512032547]["title"], "One Piece Party")
        self.assertEqual(related[49512032547]["sources"], [("One Piece", "Spin-Off")])
        self.assertNotIn(14853239448, related, "Wanted! is already tracked and must be excluded")

    def test_a_404d_series_is_skipped_not_fatal(self):
        exports = {"Reading": [_rec(1, "One Piece"), _rec(3, "Deleted Series")]}
        client = _FakeSeriesClient({1: [], 3: "404"})

        related = mu.collect_related_series(client, exports)

        self.assertEqual(related, {})
        self.assertEqual(sorted(client.calls), [1, 3], "the 404 must not abort the whole pass")

    def test_a_series_related_to_itself_is_not_reported(self):
        exports = {"Reading": [_rec(1, "Weird Series")]}
        self_relation = [
            {
                "relation_id": 1,
                "relation_type": "Related",
                "related_series_id": 1,
                "related_series_name": "Weird Series",
                "related_series_url": "https://x/1",
            }
        ]
        client = _FakeSeriesClient({1: self_relation})

        related = mu.collect_related_series(client, exports)

        self.assertEqual(related, {})

    def test_same_related_series_found_via_two_sources_is_one_entry(self):
        exports = {"Reading": [_rec(10, "Series A"), _rec(11, "Series B")]}
        shared = {
            "related_series_id": 99,
            "related_series_name": "Shared Universe",
            "related_series_url": "https://x/99",
        }
        client = _FakeSeriesClient(
            {
                10: [{**shared, "relation_id": 1, "relation_type": "Prequel"}],
                11: [{**shared, "relation_id": 2, "relation_type": "Sequel"}],
            }
        )

        related = mu.collect_related_series(client, exports)

        self.assertEqual(len(related), 1, "must be deduplicated into a single entry")
        self.assertEqual(set(related[99]["sources"]), {("Series A", "Prequel"), ("Series B", "Sequel")})

    def test_malformed_relation_entries_are_skipped(self):
        exports = {"Reading": [_rec(1, "One Piece")]}
        client = _FakeSeriesClient(
            {1: [{"relation_id": 1, "relation_type": "Spin-Off"}]}  # missing id and name
        )

        related = mu.collect_related_series(client, exports)

        self.assertEqual(related, {})

    def test_lookups_actually_run_concurrently_not_one_at_a_time(self):
        """Proves overlap happened, not just that the result is correct.

        A fake client whose .get() sleeps briefly and records real
        wall-clock start/end times for each call. If collect_related_series
        were still sequential, no two calls could ever overlap; the thread
        pool this test exercises must produce genuine overlap.
        """
        call_delay = 0.05
        series_count = 6
        exports = {"Reading": [_rec(i, f"Series {i}") for i in range(1, series_count + 1)]}

        lock = threading.Lock()
        intervals: list[tuple[float, float]] = []

        class SlowClient:
            def get(self, url, **kwargs):  # noqa: ARG002
                start = time.perf_counter()
                time.sleep(call_delay)
                end = time.perf_counter()
                with lock:
                    intervals.append((start, end))
                return _FakeApiResponse(200, {"related_series": []})

        with patch.object(mu, "SERIES_LOOKUP_WORKERS", 4):
            wall_start = time.perf_counter()
            mu.collect_related_series(SlowClient(), exports)
            wall_elapsed = time.perf_counter() - wall_start

        self.assertEqual(len(intervals), series_count)

        # If every call were sequential, this would take series_count * call_delay.
        # With real overlap across 4 workers it should take roughly
        # ceil(6/4) * call_delay = 2 * call_delay. Generous margin for
        # scheduling jitter, but tight enough that pure sequential execution
        # -- 6x call_delay -- would fail it.
        fully_sequential = series_count * call_delay
        self.assertLess(wall_elapsed, fully_sequential * 0.8, "no concurrency was observed")

        # Peak overlap: how many calls were simultaneously in-flight.
        events = []
        for start, end in intervals:
            events.append((start, 1))
            events.append((end, -1))
        events.sort()
        peak = running = 0
        for _t, delta in events:
            running += delta
            peak = max(peak, running)
        self.assertGreaterEqual(peak, 2, "expected at least two calls in flight at once")
        self.assertLessEqual(peak, 4, "must never exceed the configured worker count")



class TestSaveRelatedSeries(TempExportsCase):
    """related.txt is a single, stable, always-overwritten file -- not one
    per timestamped export folder -- so there is one fixed place to check
    and it always holds the newest run's data."""

    def test_writes_to_a_stable_path_under_exports_dir(self):
        path = mu.save_related_series({})
        self.assertEqual(path, os.path.join(mu.EXPORTS_DIR, "related.txt"))
        self.assertTrue(os.path.isfile(path))

    def test_a_second_run_replaces_rather_than_appends(self):
        first = {1: {"title": "First Run Series", "url": "https://x/1", "sources": [("A", "Sequel")]}}
        second = {2: {"title": "Second Run Series", "url": "https://x/2", "sources": [("B", "Prequel")]}}

        path1 = mu.save_related_series(first)
        path2 = mu.save_related_series(second)

        self.assertEqual(path1, path2, "must be the same path across runs")
        with open(path2, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Second Run Series", content)
        self.assertNotIn("First Run Series", content, "the previous run's data must not linger")

    def test_no_leftover_temp_file_after_writing(self):
        mu.save_related_series({1: {"title": "X", "url": "", "sources": [("Y", "Sequel")]}})
        leftovers = [f for f in os.listdir(mu.EXPORTS_DIR) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_empty_result_still_produces_a_readable_file(self):
        path = mu.save_related_series({})
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("none", content.lower())

    def test_report_includes_title_source_and_url(self):
        related = {
            42: {
                "title": "One Piece Party",
                "url": "https://www.mangaupdates.com/series/mqu6otf/one-piece-party",
                "sources": [("One Piece", "Spin-Off")],
            }
        }
        path = mu.save_related_series(related)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("One Piece Party", content)
        self.assertIn('Spin-Off of "One Piece"', content)
        self.assertIn("https://www.mangaupdates.com/series/mqu6otf/one-piece-party", content)


# ==================== Wish List completion check ====================
def _rec_url(sid: int, title: str, url: str = "") -> dict:
    return {"record": {"series": {"id": sid, "title": title, "url": url}}}


class _FakeStatusClient:
    """Stands in for httpx.Client, keyed by series id -> {"completed", "status"}.

    A value of "404" simulates a series that no longer exists on the site.
    """

    def __init__(self, by_id: dict) -> None:
        self.by_id = by_id
        self.calls: list[int] = []

    def get(self, url, **kwargs):  # noqa: ARG002
        sid = int(url.rsplit("/", 1)[-1])
        self.calls.append(sid)
        if self.by_id.get(sid) == "404":
            return _FakeApiResponse(404, {})
        return _FakeApiResponse(200, self.by_id.get(sid, {"completed": False, "status": ""}))


class TestFindFinishedWishlistSeries(unittest.TestCase):
    """`completed` is verified live against real MangaUpdates series covering
    every case: a plain Complete, a Cancelled/Discontinued (also `completed:
    true` -- nothing more is coming there either), an Ongoing, a Hiatus, and
    a series where one release format says "(Complete)" in its status text
    while another format is still active and `completed` correctly stays
    false. This suite exercises the same shapes without hitting the network."""

    def test_only_completed_series_are_reported(self):
        wish_items = [
            _rec_url(1, "Finished Manga", "https://x/1"),
            _rec_url(2, "Ongoing Manga", "https://x/2"),
            _rec_url(3, "Hiatus Manga", "https://x/3"),
        ]
        client = _FakeStatusClient(
            {
                1: {"completed": True, "status": "10 Volumes (Complete)"},
                2: {"completed": False, "status": "5 Volumes (Ongoing)"},
                3: {"completed": False, "status": "5 Volumes (Hiatus)"},
            }
        )

        finished = mu.find_finished_wishlist_series(client, wish_items)

        self.assertEqual(set(finished), {1})
        self.assertEqual(finished[1]["title"], "Finished Manga")
        self.assertEqual(finished[1]["url"], "https://x/1")

    def test_cancelled_series_counts_as_finished_too(self):
        """Nothing more is coming for a cancelled series either -- MangaUpdates
        itself marks these `completed: true`, verified live (e.g. a real
        Complete/Discontinued series)."""
        wish_items = [_rec_url(1, "Cancelled Manga")]
        client = _FakeStatusClient({1: {"completed": True, "status": "4 Volumes (Complete/Discontinued)"}})

        finished = mu.find_finished_wishlist_series(client, wish_items)

        self.assertEqual(set(finished), {1})

    def test_mixed_format_series_is_not_falsely_flagged(self):
        """Regression guard for the exact trap a naive '"Complete" in status'
        text search would fall into: one format done, another still active."""
        wish_items = [_rec_url(1, "Mixed Format Manga")]
        client = _FakeStatusClient(
            {1: {"completed": False, "status": "27 Volumes (Complete)\n\n42 Chapters (Webtoon, Ongoing)"}}
        )

        finished = mu.find_finished_wishlist_series(client, wish_items)

        self.assertEqual(finished, {})

    def test_a_404d_series_is_skipped_not_fatal(self):
        wish_items = [_rec_url(1, "Deleted Series")]
        client = _FakeStatusClient({1: "404"})

        finished = mu.find_finished_wishlist_series(client, wish_items)

        self.assertEqual(finished, {})
        self.assertEqual(client.calls, [1], "the 404 must not abort the whole pass")

    def test_empty_wishlist_produces_no_lookups(self):
        finished = mu.find_finished_wishlist_series(_FakeStatusClient({}), [])
        self.assertEqual(finished, {})


class TestSaveFinishedSeries(TempExportsCase):
    """Same stable-path, always-overwritten pattern as related.txt -- one
    fixed file that always holds the newest run's data."""

    def test_writes_to_a_stable_path_under_exports_dir(self):
        path = mu.save_finished_series({}, total_checked=0)
        self.assertEqual(path, os.path.join(mu.EXPORTS_DIR, "ready_to_read.txt"))
        self.assertTrue(os.path.isfile(path))

    def test_a_second_run_replaces_rather_than_appends(self):
        first = {1: {"title": "First Run Series", "url": "https://x/1", "status": "(Complete)"}}
        second = {2: {"title": "Second Run Series", "url": "https://x/2", "status": "(Complete)"}}

        path1 = mu.save_finished_series(first, total_checked=1)
        path2 = mu.save_finished_series(second, total_checked=1)

        self.assertEqual(path1, path2, "must be the same path across runs")
        with open(path2, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Second Run Series", content)
        self.assertNotIn("First Run Series", content, "the previous run's data must not linger")

    def test_no_leftover_temp_file_after_writing(self):
        mu.save_finished_series({1: {"title": "X", "url": "", "status": ""}}, total_checked=1)
        leftovers = [f for f in os.listdir(mu.EXPORTS_DIR) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_empty_result_still_produces_a_readable_file(self):
        path = mu.save_finished_series({}, total_checked=5)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("none", content.lower())

    def test_report_includes_title_status_and_url_and_totals(self):
        finished = {
            42: {
                "title": "Rosario to Vampire",
                "url": "https://www.mangaupdates.com/series/x/rosario-to-vampire",
                "status": "10 Volumes (Complete)",
            }
        }
        path = mu.save_finished_series(finished, total_checked=7)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Rosario to Vampire", content)
        self.assertIn("10 Volumes (Complete)", content)
        self.assertIn("https://www.mangaupdates.com/series/x/rosario-to-vampire", content)
        self.assertIn("1 of 7", content)


if __name__ == "__main__":
    unittest.main()
