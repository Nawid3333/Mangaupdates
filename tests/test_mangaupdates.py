"""Unit tests for the Mangaupdates exporter's pure logic.

Run with:  python -m unittest discover -s tests
"""

import contextlib
import datetime
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

os.environ.setdefault("MU_USERNAME", "test")
os.environ.setdefault("MU_PASSWORD", "test")

import main as mu  # noqa: E402

# Tests must never write to the real logs/ directory. setup_logging attaches a
# RotatingFileHandler to the production log file, and one run of this suite put
# ~52 KB into it -- enough repeated runs rotate genuine run history out of the
# file entirely. The console handler is left in place; tests that assert on log
# output attach their own handler.
for _handler in list(mu.log.handlers):
    if hasattr(_handler, "baseFilename"):
        mu.log.removeHandler(_handler)
        _handler.close()


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


# ==================== console encoding ====================
class TestConfigureConsole(unittest.TestCase):
    def test_a_cp1252_console_can_print_the_status_marks(self):
        """2026-09-11: printing "✗ That option did not finish" on a cp1252
        console raised UnicodeEncodeError from inside the error handler."""
        from config.config import configure_console

        buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with patch.object(sys, "stdout", buf):
            configure_console()
            print("✓ ✗ → ─ ⚠")
            buf.flush()
        self.assertEqual(buf.encoding.lower().replace("-", ""), "utf8")


# ==================== sanitize_filename ====================
class TestSanitizeFilename(unittest.TestCase):
    def test_strips_unsafe_characters(self):
        name = 'a<b>c:d"e/f' + chr(92) + "g|h?i*j"
        self.assertEqual(mu.sanitize_filename(name), "a_b_c_d_e_f_g_h_i_j")

    def test_empty_input_falls_back(self):
        self.assertEqual(mu.sanitize_filename(""), "Unnamed_List")

    def test_all_dots_falls_back(self):
        # strip(".") on a name of only dots leaves nothing behind.
        self.assertEqual(mu.sanitize_filename("..."), "Unnamed_List")

    def test_unsafe_characters_become_underscores_not_empty(self):
        # Each unsafe char is individually replaced, so this is NOT empty.
        self.assertEqual(mu.sanitize_filename("///"), "___")

    def test_control_characters_are_replaced(self):
        """A tab or newline is invalid in a Windows filename; a list title
        carrying one reached open() unchanged and the save died with OSError
        22 after every list had been fetched."""
        self.assertEqual(mu.sanitize_filename("a\tb\nc\x00d"), "a_b_c_d")

    def test_two_distinct_titles_can_collide(self):
        """The premise the manifest fix exists for: this collision is real."""
        a, b = "Sci-Fi/Fantasy", "Sci-Fi_Fantasy"
        self.assertNotEqual(a, b)
        self.assertEqual(mu.sanitize_filename(a), mu.sanitize_filename(b))

    def test_extremely_long_title_is_truncated_not_left_to_crash_later(self):
        """A title long enough to exceed NTFS's ~255 UTF-16-unit path-component
        limit used to reach save_exports unmodified and crash with OSError 22.
        Fuzzed and reproduced live before this fix."""
        name = mu.sanitize_filename("x" * 500)
        self.assertLessEqual(len(name), 100)

    def test_truncation_is_safe_for_worst_case_surrogate_pair_heavy_titles(self):
        """Most emoji are astral-plane characters -- 2 UTF-16 units each on
        Windows -- so the codepoint cap must leave real headroom, not just
        scrape under the byte limit for plain ASCII."""
        name = mu.sanitize_filename("🔥" * 500)
        # 100 codepoints of an astral-plane character is 200 UTF-16 units --
        # comfortably under 255 with room for a "_N" suffix and ".json".
        self.assertLessEqual(len(name), 100)


# ==================== export_filenames / load_manifest ====================
class TestExportFilenames(unittest.TestCase):
    def test_collision_gets_deduplicated(self):
        mapping = mu.export_filenames(["Sci-Fi/Fantasy", "Sci-Fi_Fantasy"])
        self.assertEqual(len(set(mapping.values())), 2, "both titles must map to distinct files")

    def test_mapping_is_order_stable(self):
        titles = ["A", "B", "C"]
        self.assertEqual(mu.export_filenames(titles), mu.export_filenames(list(titles)))

    def test_case_only_difference_is_treated_as_a_collision(self):
        """On a case-insensitive filesystem (Windows NTFS, default macOS),
        "Sci-Fi" and "sci-fi" are the SAME file even though they are
        different strings. Reproduced live before this fix: the second
        write silently landed on the first list's file and its data was
        gone with no warning -- the exact corruption class the manifest
        system exists to prevent, just via case instead of punctuation."""
        mapping = mu.export_filenames(["Sci-Fi", "sci-fi", "SCI-FI"])
        names = list(mapping.values())
        self.assertEqual(len(names), len({n.lower() for n in names}), "must be distinct case-insensitively too")


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

        loaded, unreadable = mu._load_prev_exports(folder, mu.load_manifest(folder, list(exports.keys())))
        self.assertEqual(unreadable, set())
        self.assertEqual(mu.get_series_ids(loaded[a]), {1: "AAA"})
        self.assertEqual(mu.get_series_ids(loaded[b]), {2: "BBB"})

    def test_manifest_is_written_and_not_reported_as_a_removed_list(self):
        mu.save_exports({"Reading": [_rec(1, "X")]})
        time.sleep(1.1)  # folder names have 1-second resolution
        folder2 = mu.save_exports({"Reading": [_rec(1, "X")]})
        changed = mu.compare_exports(folder2, {"Reading": [_rec(1, "X")]})
        self.assertFalse(changed, "identical export must report no changes")

    def test_case_only_colliding_titles_each_read_back_their_own_data(self):
        """Same regression as the punctuation-collision test above, but for
        the case-insensitive-filesystem variant. Uses the real filesystem
        (TempExportsCase), not just export_filenames in isolation, since the
        bug only shows up once actual files are written."""
        a, b = "Sci-Fi", "sci-fi"
        exports = {a: [_rec(1, "AAA")], b: [_rec(2, "BBB")]}
        folder = mu.save_exports(exports)

        loaded, unreadable = mu._load_prev_exports(folder, mu.load_manifest(folder, list(exports.keys())))
        self.assertEqual(unreadable, set())
        self.assertEqual(mu.get_series_ids(loaded[a]), {1: "AAA"})
        self.assertEqual(mu.get_series_ids(loaded[b]), {2: "BBB"})

    def test_a_list_titled_like_the_manifest_keeps_its_own_file(self):
        """A list titled "_manifest" was written to _manifest.json and then
        overwritten by the manifest itself: its items were lost from the
        snapshot."""
        exports = {"_manifest": [_rec(1, "AAA")], "_MANIFEST": [_rec(2, "BBB")]}
        folder = mu.save_exports(exports)

        loaded, unreadable = mu._load_prev_exports(folder, mu.load_manifest(folder, list(exports.keys())))
        self.assertEqual(unreadable, set())
        self.assertEqual(mu.get_series_ids(loaded["_manifest"]), {1: "AAA"})
        self.assertEqual(mu.get_series_ids(loaded["_MANIFEST"]), {2: "BBB"})

    def test_a_title_with_a_control_character_saves(self):
        exports = {"Tab\there": [_rec(1, "AAA")]}
        folder = mu.save_exports(exports)
        loaded, _unreadable = mu._load_prev_exports(folder, mu.load_manifest(folder, list(exports.keys())))
        self.assertEqual(mu.get_series_ids(loaded["Tab\there"]), {1: "AAA"})

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

    def test_leftover_tmp_folder_from_a_crashed_run_is_cleaned_up(self):
        """A run that crashes between creating its *.tmp folder and the final
        os.replace() used to leave that folder behind forever -- nothing
        else ever revisits it. The next successful run must sweep it away."""
        stale = os.path.join(self.dir.name, "01.01.2026_00-00-00.tmp")
        os.makedirs(stale)
        with open(os.path.join(stale, "partial.json"), "w", encoding="utf-8") as f:
            f.write("[]")

        mu.save_exports({"Reading": [_rec(1, "X")]})

        self.assertFalse(os.path.isdir(stale), "stale .tmp folder from a previous crash must be swept up")


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
        self.assertIsNotNone(prev, "the previous export folder was not found")
        assert prev is not None  # narrows for the type checker
        self.assertEqual(os.path.basename(prev), "02.01.2026_00-00-00")


# ==================== get_series_ids / get_series_basic robustness ====================
class TestSeriesExtractionRobustness(unittest.TestCase):
    """`.get("record", {})` only substitutes its default when the key is
    *missing*, not when it's present with a null value. A single item
    anywhere in a list shaped that way -- or not a dict at all -- used to
    crash with AttributeError and take down export, compare, related-series,
    and finished-series checks alike, since they all read through these two
    functions. Fuzzed and reproduced live before this fix."""

    MALFORMED = [
        None,
        {},
        {"record": None},
        {"record": {}},
        {"record": {"series": None}},
        {"record": {"series": {}}},
        {"record": {"series": {"id": None, "title": "no id"}}},
        {"record": "not-a-dict"},
        "not-a-dict-item",
        42,
    ]

    def test_get_series_ids_skips_malformed_items_without_crashing(self):
        good = _rec(1, "Good Series")
        result = mu.get_series_ids([*self.MALFORMED, good])
        self.assertEqual(result, {1: "Good Series"})

    def test_get_series_basic_skips_malformed_items_without_crashing(self):
        good = {"record": {"series": {"id": 1, "title": "Good Series", "url": "https://x/1"}}}
        result = mu.get_series_basic([*self.MALFORMED, good])
        self.assertEqual(result, {1: {"title": "Good Series", "url": "https://x/1"}})


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

    def _assert_delay_in_band(self, actual, base, what):
        """Never sooner than `base`, never more than RETRY_JITTER later.

        The delay carries a random spread so that workers rate-limited in the
        same instant do not all retry in the same instant. Asserting an exact
        value would only pin the implementation; the properties that matter
        are that the server's instruction is never undercut and that the
        spread stays bounded.
        """
        self.assertGreaterEqual(actual, base, f"{what}: retried sooner than instructed")
        self.assertLessEqual(actual, base + mu.RETRY_JITTER, f"{what}: waited longer than the jitter allows")

    def test_429_without_retry_after_uses_configured_delay(self):
        rate_limited = self._fake_response(429, {})
        ok = self._fake_response(200)
        client = MagicMock()
        client.get.side_effect = [rate_limited, ok]

        with patch.object(mu, "time") as mock_time:
            mu._api_request(client, "get", "https://example.invalid")

        mock_time.sleep.assert_called_once()
        self._assert_delay_in_band(mock_time.sleep.call_args[0][0], mu.RETRY_DELAY, "no Retry-After")

    def test_retry_after_header_overrides_configured_delay(self):
        resp = self._fake_response(429, {"Retry-After": "17"})
        self._assert_delay_in_band(mu._retry_delay(resp), 17.0, "Retry-After: 17")

    def test_malformed_retry_after_falls_back_to_configured_delay(self):
        resp = self._fake_response(429, {"Retry-After": "not-a-number"})
        self._assert_delay_in_band(mu._retry_delay(resp), mu.RETRY_DELAY, "malformed Retry-After")

    def test_no_response_falls_back_to_configured_delay(self):
        self._assert_delay_in_band(mu._retry_delay(None), mu.RETRY_DELAY, "no response")

    def test_retry_after_zero_is_never_undercut(self):
        resp = self._fake_response(429, {"Retry-After": "0"})
        self.assertGreaterEqual(mu._retry_delay(resp), 0.0)

    def test_concurrent_retries_do_not_all_wake_together(self):
        """The whole point of the jitter: 16 workers rate-limited at once
        must not rebuild the burst the server just pushed back on."""
        resp = self._fake_response(429, {"Retry-After": "5"})
        delays = {mu._retry_delay(resp) for _ in range(50)}
        self.assertGreater(len(delays), 40, "delays are identical -- no spread at all")

    def test_exhausting_retries_on_429_raises(self):
        client = MagicMock()
        client.get.return_value = self._fake_response(429, {"Retry-After": "0"})

        with patch.object(mu, "time"), self.assertRaises(httpx.HTTPStatusError):
            mu._api_request(client, "get", "https://example.invalid")

        self.assertEqual(client.get.call_count, mu.MAX_RETRIES)

    def test_a_dropped_connection_is_retried(self):
        """Only timeouts and failed connects were retried. A connection the
        server dropped mid-answer -- the usual end of a pooled keep-alive
        connection -- aborted a whole export on the first occurrence."""
        for error in (
            httpx.ReadError("connection reset"),
            httpx.WriteError("broken pipe"),
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
        ):
            with self.subTest(type(error).__name__):
                ok = self._fake_response(200)
                client = MagicMock()
                client.get.side_effect = [error, ok]

                with patch.object(mu, "time"):
                    self.assertIs(mu._api_request(client, "get", "https://example.invalid"), ok)
                self.assertEqual(client.get.call_count, 2)

    def test_a_request_that_cannot_work_is_not_retried(self):
        """Sending it again cannot change the answer, so it is not sent again."""
        client = MagicMock()
        client.get.side_effect = httpx.UnsupportedProtocol("no such scheme")

        with patch.object(mu, "time") as mock_time, self.assertRaises(httpx.UnsupportedProtocol):
            mu._api_request(client, "get", "https://example.invalid")

        self.assertEqual(client.get.call_count, 1)
        mock_time.sleep.assert_not_called()

    def test_a_retry_after_beyond_the_limit_fails_at_once(self):
        """Retry-After was honoured without a limit: 86400 slept every worker
        for a day, and Ctrl+C could not end the program, because the
        interpreter waits for sleeping pool threads before it exits."""
        client = MagicMock()
        client.get.return_value = self._fake_response(429, {"Retry-After": "86400"})

        with patch.object(mu, "time") as mock_time, self.assertRaises(httpx.HTTPStatusError):
            mu._api_request(client, "get", "https://example.invalid")

        self.assertEqual(client.get.call_count, 1)
        mock_time.sleep.assert_not_called()

    def test_a_retry_after_at_the_limit_is_still_honoured(self):
        rate_limited = self._fake_response(429, {"Retry-After": str(mu.MAX_RETRY_AFTER)})
        ok = self._fake_response(200)
        client = MagicMock()
        client.get.side_effect = [rate_limited, ok]

        with patch.object(mu, "time") as mock_time:
            self.assertIs(mu._api_request(client, "get", "https://example.invalid"), ok)

        self._assert_delay_in_band(mock_time.sleep.call_args[0][0], float(mu.MAX_RETRY_AFTER), "at the limit")

    def test_a_non_finite_retry_after_falls_back_to_configured_delay(self):
        """float() accepts "inf" and "nan"; time.sleep(nan) raised from inside
        the retry handler."""
        for raw in ("inf", "nan", "-inf"):
            with self.subTest(raw):
                resp = self._fake_response(429, {"Retry-After": raw})
                self._assert_delay_in_band(mu._retry_delay(resp), mu.RETRY_DELAY, f"Retry-After: {raw}")


# ==================== reproducible output ====================
class TestReportsAreReproducible(TempExportsCase):
    """Everything reaching a report is collected in as_completed order --
    whichever request finished first. Confirmed live before this was fixed:
    two runs of identical code produced two different related.txt files, the
    same length, with three entries' sources reshuffled. A report that
    reorders itself between identical runs makes any comparison against a
    previous copy produce phantom changes.

    Every case here uses titles that sort *against* the order the unfixed
    code produced, because ids that happen to iterate in ascending order make
    a broken implementation look correct.
    """

    def _report_body(self, name):
        text = Path(os.path.join(self.dir.name, name)).read_text(encoding="utf-8")
        # Drop the box header; it is expected to differ between runs. The header
        # now spans 5 lines plus one blank line before the entries.
        return [ln.rstrip() for ln in text.splitlines()[6:] if ln.strip() or ln.startswith("│") or ln.startswith("║")]

    def test_source_order_does_not_depend_on_which_lookup_finished_first(self):
        forwards = [("Alpha", "Sequel"), ("Beta", "Spin-Off"), ("Gamma", "Adaptation")]
        entry = lambda order: {  # noqa: E731
            7: {"title": "Shared Target", "url": "http://x/7", "sources": list(order)}
        }

        mu.save_related_series(entry(forwards))
        first = self._report_body("related.txt")
        mu.save_related_series(entry(reversed(forwards)))
        second = self._report_body("related.txt")

        self.assertEqual(first, second, "source order followed completion order")

    def test_related_entries_that_tie_on_title_keep_a_total_order(self):
        """Sorting on title alone is stable, not total: entries that tie fall
        back to insertion order, which is completion order."""

        def report(ids):
            related = {
                sid: {"title": "Same Title", "url": f"http://x/{sid}", "sources": [("O", "Sequel")]} for sid in ids
            }
            mu.save_related_series(related)
            return [
                ln.rsplit("  ", 1)[-1]
                for ln in self._report_body("related.txt")
                if re.search(r"\[\d/3\]\s+Same Title", ln)
            ]

        forwards = report([3, 1, 2])
        backwards = report([2, 3, 1])
        self.assertEqual(len(forwards), 3, "the urls were not picked up at all")
        self.assertEqual(forwards, backwards)

    def test_finished_entries_that_tie_on_title_keep_a_total_order(self):
        def report(ids):
            finished = {sid: {"title": "Same Title", "url": f"http://x/{sid}", "status": "Complete"} for sid in ids}
            mu.save_finished_series(finished, len(ids))
            return [
                ln.rsplit("  ", 1)[-1]
                for ln in self._report_body("ready_to_read.txt")
                if re.search(r"\[\d/3\]\s+Same Title", ln)
            ]

        forwards = report([3, 1, 2])
        backwards = report([2, 3, 1])
        self.assertEqual(len(forwards), 3, "the urls were not picked up at all")
        self.assertEqual(forwards, backwards)

    # Titles deliberately inverse to the numeric id order, so hash/insertion
    # order and title order cannot coincide.
    INVERSE = {10: "Zulu", 20: "Mike", 30: "Alpha", 40: "Yankee", 50: "November", 60: "Bravo"}

    def _rec_inv(self, sid):
        return _rec(sid, self.INVERSE[sid])

    def test_added_and_removed_are_listed_in_title_order(self):
        """A set of ids iterates in hash order, which for these ids is
        ascending -- and therefore the reverse of the titles."""
        mu.save_exports({"Reading": [self._rec_inv(i) for i in (10, 20, 30)]})
        current = {"Reading": [self._rec_inv(i) for i in (40, 50, 60)]}
        folder = mu.save_exports(current)

        with _LogCapture() as cap:
            mu.compare_exports(folder, current)

        added = [ln.split("Added:")[1].strip() for ln in cap.text.splitlines() if "Added:" in ln]
        removed = [ln.split("Removed:")[1].strip() for ln in cap.text.splitlines() if "Removed:" in ln]
        added = [mu._strip_ansi(name) for name in added]
        removed = [mu._strip_ansi(name) for name in removed]

        self.assertEqual(len(added), 3, f"expected 3 added, got {added}")
        self.assertEqual(len(removed), 3, f"expected 3 removed, got {removed}")
        self.assertEqual(added, sorted(added, key=str.lower), f"added not in title order: {added}")
        self.assertEqual(removed, sorted(removed, key=str.lower), f"removed not in title order: {removed}")

    def test_moved_series_are_listed_in_title_order(self):
        before = {"A": [self._rec_inv(i) for i in (10, 20, 30)], "B": []}
        mu.save_exports(before)
        current = {"A": [], "B": [self._rec_inv(i) for i in (10, 20, 30)]}
        folder = mu.save_exports(current)

        with _LogCapture() as cap:
            mu.compare_exports(folder, current)

        plain = mu._strip_ansi(cap.text)
        positions = [(plain.index(self.INVERSE[sid]), self.INVERSE[sid]) for sid in (10, 20, 30)]
        names_in_order = [name for _pos, name in sorted(positions)]
        self.assertEqual(
            names_in_order,
            sorted(names_in_order, key=str.lower),
            f"moved series not in title order: {names_in_order}",
        )


# ==================== process exit codes ====================
class TestExitCodes(unittest.TestCase):
    """Every way the program can end must produce a sensible exit code and a
    message, never a traceback."""

    def _run(self, main_side_effect):
        with (
            patch.object(mu, "main", side_effect=main_side_effect),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return mu._run_cli()

    def test_a_clean_run_exits_zero(self):
        self.assertEqual(self._run(lambda: None), 0)

    def test_ctrl_c_exits_130(self):
        """130 is the conventional shell code for a process ended by SIGINT."""
        self.assertEqual(self._run(KeyboardInterrupt), 130)

    def test_a_deliberate_systemexit_keeps_its_code(self):
        self.assertEqual(self._run(SystemExit(1)), 1)
        self.assertEqual(self._run(SystemExit(0)), 0)

    def test_a_bare_systemexit_is_success(self):
        self.assertEqual(self._run(SystemExit()), 0)

    def test_a_non_integer_systemexit_code_becomes_one(self):
        self.assertEqual(self._run(SystemExit("something went wrong")), 1)

    def test_an_unexpected_error_exits_one_and_is_logged_with_its_traceback(self):
        with _LogCapture() as cap:
            code = self._run(RuntimeError("kaboom"))
        self.assertEqual(code, 1)
        self.assertIn("kaboom", cap.text)
        self.assertIn("RuntimeError", cap.text, "the traceback must reach the log")

    def test_an_unexpected_error_tells_the_user_where_to_look(self):
        buf = io.StringIO()
        with (
            patch.object(mu, "main", side_effect=RuntimeError("kaboom")),
            contextlib.redirect_stdout(buf),
        ):
            mu._run_cli()
        self.assertIn("Unexpected error", buf.getvalue())
        self.assertIn(mu.LOG_FILE, buf.getvalue())


# ==================== diff reporting branches ====================
class TestCompareExportsReporting(TempExportsCase):
    def _previous(self, exports):
        folder = mu.save_exports(exports)
        return folder

    def test_no_previous_export_is_reported_and_is_not_a_change(self):
        exports = {"Reading": [_rec(1, "A")]}
        folder = mu.save_exports(exports)
        with _LogCapture() as cap:
            self.assertFalse(mu.compare_exports(folder, exports))
        self.assertIn("No previous export found", cap.text)

    def test_a_series_that_moved_lists_is_reported_as_moved_not_added_and_removed(self):
        self._previous({"Reading": [_rec(1, "A")], "Complete": []})
        now = {"Reading": [], "Complete": [_rec(1, "A")]}
        folder = mu.save_exports(now)
        with _LogCapture() as cap:
            self.assertTrue(mu.compare_exports(folder, now))
        self.assertIn("Moved series", cap.text)
        self.assertNotIn("Added:", cap.text)
        self.assertNotIn("Removed:", cap.text)

    def test_a_brand_new_list_is_reported_as_new(self):
        self._previous({"Reading": [_rec(1, "A")]})
        now = {"Reading": [_rec(1, "A")], "Brand New": [_rec(2, "B")]}
        folder = mu.save_exports(now)
        with _LogCapture() as cap:
            self.assertTrue(mu.compare_exports(folder, now))
        self.assertIn("NEW LIST", cap.text)

    def test_a_list_that_disappeared_is_reported_as_removed(self):
        self._previous({"Reading": [_rec(1, "A")], "Gone": [_rec(2, "B")]})
        now = {"Reading": [_rec(1, "A")]}
        folder = mu.save_exports(now)
        with _LogCapture() as cap:
            self.assertTrue(mu.compare_exports(folder, now))
        self.assertIn("LIST REMOVED", cap.text)

    def test_a_removed_series_is_listed(self):
        self._previous({"Reading": [_rec(1, "A"), _rec(2, "B")]})
        now = {"Reading": [_rec(1, "A")]}
        folder = mu.save_exports(now)
        with _LogCapture() as cap:
            self.assertTrue(mu.compare_exports(folder, now))
        self.assertIn("Removed:", cap.text)

    def test_identical_exports_report_no_changes(self):
        exports = {"Reading": [_rec(1, "A")]}
        self._previous(exports)
        folder = mu.save_exports(exports)
        with _LogCapture() as cap:
            self.assertFalse(mu.compare_exports(folder, exports))
        self.assertIn("NO CHANGES", cap.text)

    def test_a_series_that_left_a_removed_list_is_reported_as_moved(self):
        """Only the lists still present were read from the previous export,
        so a series that moved out of a list that is now gone came back as
        newly Added."""
        self._previous({"Gone": [_rec(1, "A")], "Read": [_rec(2, "B")]})
        now = {"Read": [_rec(1, "A"), _rec(2, "B")]}
        folder = mu.save_exports(now)
        with _LogCapture() as cap:
            mu.compare_exports(folder, now)
        text = mu._strip_ansi(cap.text)
        self.assertIn("Moved series", text)
        self.assertRegex(text, r"A\s+Gone → Read")
        self.assertNotIn("Added:", text)

    def test_a_removed_list_names_the_series_that_went_with_it(self):
        self._previous({"Gone": [_rec(1, "Moved On"), _rec(3, "Dropped")], "Read": []})
        now = {"Read": [_rec(1, "Moved On")]}
        folder = mu.save_exports(now)
        with _LogCapture() as cap:
            mu.compare_exports(folder, now)
        text = mu._strip_ansi(cap.text)
        self.assertIn("[Gone] LIST REMOVED (no longer exists) – 2 item(s)", text)
        self.assertIn("Removed: Dropped", text)
        self.assertNotIn("Removed: Moved On", text, "a moved series is a move, not a removal")


class TestListIdentity(TempExportsCase):
    """Lists are followed by their MangaUpdates list_id, not their key."""

    def _scan(self, lists):
        client = _MenuApi([{"list_id": lid, "title": t} for lid, t, _ in lists], {lid: it for lid, _, it in lists})
        with contextlib.redirect_stdout(io.StringIO()), _LogCapture() as cap:
            mu.run_scan_lists(client)
        return mu._strip_ansi(cap.text)

    def test_deleting_one_of_two_same_titled_lists_does_not_scramble_the_other(self):
        """Reproduced before this fix: list 20 inherited the bare key
        "Reading" and was diffed against deleted list 10's file -- "Added: C,
        D / Removed: A, B" for a list that had not changed at all."""
        self._scan([(10, "Reading", [_rec(1, "A"), _rec(2, "B")]), (20, "Reading", [_rec(3, "C"), _rec(4, "D")])])
        text = self._scan([(20, "Reading", [_rec(3, "C"), _rec(4, "D")])])

        self.assertNotIn("Added:", text)
        self.assertIn("[Reading] No changes", text)
        self.assertIn("[Reading (list_id 10)] LIST REMOVED", text)
        self.assertIn("Removed: A", text)
        self.assertIn("Removed: B", text)

    def test_a_case_only_rename_is_one_renamed_list(self):
        """Reproduced before this fix: NTFS found the old file under the new
        name, so the same list was reported "No changes" and "LIST REMOVED"."""
        self._scan([(10, "Sci-fi", [_rec(1, "A")]), (20, "Read", [_rec(2, "B")])])
        text = self._scan([(10, "Sci-Fi", [_rec(1, "A")]), (20, "Read", [_rec(2, "B")])])

        self.assertIn("[Sci-Fi] renamed (was 'Sci-fi')", text)
        self.assertNotIn("LIST REMOVED", text)
        self.assertNotIn("NEW LIST", text)

    def test_a_move_out_of_a_deleted_same_titled_list_is_a_named_move(self):
        """The removed list and the survivor share the key "Reading", so a
        comparison by name saw no move at all; it is named by its id."""
        self._scan([(10, "Reading", [_rec(1, "A")]), (20, "Reading", [_rec(2, "B")])])
        text = self._scan([(20, "Reading", [_rec(1, "A"), _rec(2, "B")])])

        self.assertIn("Moved series", text)
        self.assertRegex(text, r"A\s+Reading \(list_id 10\) → Reading")
        self.assertNotIn("Added:", text)

    def test_a_shifted_duplicate_suffix_is_not_a_rename(self):
        self._scan([(10, "Reading", [_rec(1, "A")]), (20, "Reading", [_rec(2, "B")])])
        text = self._scan([(20, "Reading", [_rec(2, "B")])])
        self.assertNotIn("renamed", text)

    def test_the_manifest_records_each_lists_id_and_title(self):
        self._scan([(10, "Reading", [_rec(1, "A")]), (20, "Reading", [_rec(2, "B")])])
        folder = os.path.join(self.dir.name, next(d for d in os.listdir(self.dir.name) if mu._is_export_folder(d)))
        manifest = mu._read_manifest(folder)
        assert manifest is not None
        self.assertEqual(manifest["Reading"].list_id, 10)
        self.assertEqual(manifest["Reading (2)"], mu._ManifestEntry("Reading (2)", 20, "Reading"))

    def _write_old_style_previous(self, manifest: dict, files: dict) -> None:
        prev = os.path.join(self.dir.name, "01.01.2026_00-00-00")
        os.makedirs(prev)
        with open(os.path.join(prev, mu.MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        for name, items in files.items():
            with open(os.path.join(prev, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(items, f)

    def test_an_export_from_before_ids_were_recorded_still_diffs_by_title(self):
        """Existing exports carry a title -> filename manifest with no ids;
        an unknown id is matched by title, not treated as a different list."""
        self._write_old_style_previous({"Reading": "Reading"}, {"Reading": [_rec(1, "A")]})
        text = self._scan([(10, "Reading", [_rec(1, "A"), _rec(2, "B")])])

        self.assertIn("Added:   B", text)
        self.assertNotIn("NEW LIST", text)
        self.assertNotIn("LIST REMOVED", text)

    def test_without_ids_a_title_missing_from_the_manifest_is_a_new_list_not_a_guessed_file(self):
        """A title the previous manifest does not list used to be looked up by
        sanitize_filename() -- which on NTFS found a different list's file."""
        self._write_old_style_previous({"Sci-fi": "Sci-fi"}, {"Sci-fi": [_rec(1, "A")]})
        text = self._scan([(10, "Sci-Fi", [_rec(1, "A")])])

        self.assertIn("[Sci-Fi] NEW LIST", text)
        self.assertIn("[Sci-fi] LIST REMOVED", text)
        self.assertRegex(text, r"A\s+Sci-fi → Sci-Fi")
        self.assertNotIn("No changes", text)

    def test_two_different_ids_under_one_key_are_two_lists(self):
        self._scan([(10, "Reading", [_rec(1, "A")])])
        text = self._scan([(20, "Reading", [_rec(2, "B")])])
        self.assertIn("[Reading] NEW LIST", text)
        self.assertIn("[Reading (list_id 10)] LIST REMOVED", text)

    def test_a_manifest_entry_of_an_unknown_shape_falls_back_to_the_files(self):
        folder = os.path.join(self.dir.name, "folder")
        os.makedirs(folder)
        with open(os.path.join(folder, mu.MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump({"Reading": 5}, f)
        self.assertIsNone(mu._read_manifest(folder))
        self.assertEqual(mu.load_manifest(folder, ["Reading"]), {"Reading": "Reading"})


# ==================== error paths ====================
class TestErrorPaths(TempExportsCase):
    def test_a_report_write_failure_leaves_no_temp_file_behind(self):
        with patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            mu.save_related_series({1: {"title": "A", "url": "", "sources": []}})
        leftovers = [f for f in os.listdir(self.dir.name) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [], "a failed report write left a temp file behind")

    def test_a_finished_report_write_failure_also_cleans_up(self):
        with patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            mu.save_finished_series({1: {"title": "A", "url": "", "status": ""}}, 1)
        leftovers = [f for f in os.listdir(self.dir.name) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_rotation_reports_a_folder_it_cannot_delete_instead_of_crashing(self):
        for day in range(1, 6):
            os.makedirs(os.path.join(self.dir.name, f"0{day}.01.2026_00-00-00"))
        with patch.object(mu.shutil, "rmtree", side_effect=OSError("in use")), _LogCapture() as cap:
            mu.rotate_exports()
        self.assertIn("Could not delete", cap.text)

    def test_export_folders_is_empty_when_the_directory_does_not_exist(self):
        with patch.object(mu, "EXPORTS_DIR", os.path.join(self.dir.name, "nope")):
            self.assertEqual(mu._export_folders(), [])
            self.assertIsNone(mu.find_previous_export("01.01.2026_00-00-00"))

    def test_hitting_the_page_limit_is_warned_about(self):
        """A list too large for MAX_LIST_PAGES must say the export is short
        rather than quietly returning a truncated list."""
        huge = mu.ITEMS_PER_PAGE * (mu.MAX_LIST_PAGES + 50)
        api = _PagingApi({1: (_items(1, 3), huge)})
        with patch.object(mu, "MAX_LIST_PAGES", 3), _LogCapture() as cap:
            mu.export_list(api, 1, "Huge")
        self.assertIn("hit page limit", cap.text)

    def test_hitting_the_page_limit_is_warned_about_for_every_list(self):
        huge = mu.ITEMS_PER_PAGE * 900
        api = _PagingApi({1: (_items(1, 3), huge)})
        with patch.object(mu, "MAX_LIST_PAGES", 3), _LogCapture() as cap:
            mu.export_all_lists(api, [{"list_id": 1, "title": "Huge"}])
        self.assertIn("hit page limit", cap.text)

    def test_a_leftover_tmp_folder_with_the_same_name_is_replaced(self):
        """save_exports reuses its own tmp path; a stale one from a crashed
        run at the same second must be cleared, not merged into."""
        fixed = datetime.datetime(2026, 1, 1, 12, 0, 0)

        class Frozen(datetime.datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return fixed

        stale = os.path.join(self.dir.name, "01.01.2026_12-00-00.tmp")
        os.makedirs(stale)
        with open(os.path.join(stale, "junk.json"), "w", encoding="utf-8") as f:
            f.write("stale")

        with patch.object(mu, "datetime", Frozen):
            folder = mu.save_exports({"L": [_rec(1, "A")]})

        self.assertNotIn("junk.json", os.listdir(folder), "stale partial data survived into the export")

    def test_the_defensive_retry_guard_is_unreachable_but_correct(self):
        """_api_request's loop must return or raise on every path; the trailing
        raise exists so a future edit that breaks that fails loudly."""
        client = MagicMock()
        client.get.return_value = MagicMock(status_code=200)
        with patch.object(mu, "MAX_RETRIES", 0), self.assertRaises(RuntimeError):
            mu._api_request(client, "get", "https://example.invalid")


class TestStartupAndMenuGuards(unittest.TestCase):
    def test_an_unreachable_api_stops_before_attempting_to_log_in(self):
        """Reporting "unreachable" and stopping is the whole reason the probe
        runs before login rather than after it."""
        with (
            patch.object(mu, "check_site_reachable", return_value=False),
            patch.object(mu, "login") as login,
            patch("httpx.Client", return_value=MagicMock()),
            patch("builtins.input", side_effect=["0"]),
            contextlib.redirect_stdout(io.StringIO()),
            _LogCapture() as cap,
        ):
            mu.main()
        login.assert_not_called()
        self.assertIn("Cannot reach the MangaUpdates API", cap.text)

    def test_an_invalid_choice_reprompts_instead_of_ending_the_session(self):
        ran = []
        with (
            patch.object(mu, "check_site_reachable", return_value=True),
            patch.object(mu, "login", return_value="tok"),
            patch.object(mu, "logout"),
            patch.object(mu, "run_scan_lists", lambda _c: ran.append("1")),
            patch("httpx.Client", return_value=MagicMock()),
            patch("builtins.input", side_effect=["", "9", "abc", "1", "0"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mu.main()
        self.assertEqual(ran, ["1"], "an invalid choice must not end the session or run anything")


class TestRotateWithoutExportsDir(unittest.TestCase):
    def test_rotation_is_a_no_op_when_the_directory_does_not_exist(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(mu, "EXPORTS_DIR", os.path.join(tmp, "missing")),
        ):
            mu.rotate_exports()  # must not raise


# ==================== auth path ====================
class _AuthClient:
    """Scriptable stand-in for httpx.Client covering login/lists/logout."""

    def __init__(self, login_body=None, login_status=200, lists_body=None, post_error=None):
        self.login_body = login_body if login_body is not None else {"context": {"session_token": "tok"}}
        self.login_status = login_status
        self.lists_body = lists_body if lists_body is not None else []
        self.post_error = post_error
        self.posts = []

    def put(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(self.login_status, self.login_body)

    def get(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, self.lists_body)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.post_error is not None:
            raise self.post_error
        return _FakeApiResponse(200, {"results": [], "total_hits": 0})


class TestLogin(unittest.TestCase):
    def test_returns_the_session_token(self):
        """Credentials are patched in: without this the test reads whatever is
        in the developer's own .env, and fails anywhere that file is absent --
        every CI run, and every fresh clone."""
        with patch.object(mu, "USERNAME", "user"), patch.object(mu, "PASSWORD", "pass"):
            self.assertEqual(mu.login(_AuthClient()), "tok")

    def test_missing_credentials_exit_before_any_request(self):
        client = _AuthClient()
        with (
            patch.object(mu, "USERNAME", ""),
            patch.object(mu, "PASSWORD", ""),
            self.assertRaises(SystemExit),
        ):
            mu.login(client)
        self.assertEqual(client.posts, [], "no request should be made without credentials")

    def test_401_exits_rather_than_raising_for_status(self):
        with self.assertRaises(SystemExit):
            mu.login(_AuthClient(login_status=401))

    def test_a_null_context_exits_cleanly_instead_of_attributeerror(self):
        """`.get("context", {})` only substitutes when the key is absent, so a
        context present-but-null crashed with a bare AttributeError."""
        with self.assertRaises(SystemExit):
            mu.login(_AuthClient(login_body={"context": None}))

    def test_a_non_object_body_exits_cleanly(self):
        for body in ([1, 2, 3], "nope", 7):
            with self.subTest(body=body), self.assertRaises(SystemExit):
                mu.login(_AuthClient(login_body=body))

    def test_a_context_that_is_not_an_object_exits_cleanly(self):
        with self.assertRaises(SystemExit):
            mu.login(_AuthClient(login_body={"context": "x"}))

    def test_a_missing_token_exits_cleanly(self):
        with self.assertRaises(SystemExit):
            mu.login(_AuthClient(login_body={"context": {}}))


class TestCheckSiteReachable(unittest.TestCase):
    def test_a_healthy_response_is_reachable(self):
        self.assertTrue(mu.check_site_reachable(_AuthClient()))

    def test_a_transport_error_is_unreachable(self):
        self.assertFalse(mu.check_site_reachable(_AuthClient(post_error=httpx.ConnectError("down"))))

    def test_a_timeout_is_unreachable(self):
        self.assertFalse(mu.check_site_reachable(_AuthClient(post_error=httpx.ReadTimeout("slow"))))

    def test_a_server_error_is_unreachable(self):
        class Failing(_AuthClient):
            def post(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(503, {})

        self.assertFalse(mu.check_site_reachable(Failing()))

    def test_a_4xx_still_counts_as_reachable(self):
        """The question is whether the API answers, not whether that one
        endpoint liked the request."""

        class NotFound(_AuthClient):
            def post(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(404, {})

        self.assertTrue(mu.check_site_reachable(NotFound()))


class TestLogout(unittest.TestCase):
    def test_it_posts_to_the_logout_endpoint(self):
        client = _AuthClient()
        mu.logout(client)
        self.assertTrue(any("logout" in url for url, _ in client.posts))

    def test_a_failure_is_swallowed_so_it_cannot_mask_the_real_outcome(self):
        """logout runs in a finally; raising there would replace whatever
        actually went wrong with a logout error."""
        mu.logout(_AuthClient(post_error=httpx.ConnectError("gone")))


class TestFetchLists(unittest.TestCase):
    def test_returns_the_lists(self):
        body = [{"list_id": 1, "title": "A"}, {"list_id": 2, "title": "B"}]
        self.assertEqual(mu.fetch_lists(_AuthClient(lists_body=body)), body)

    def test_an_empty_account_is_not_an_error(self):
        self.assertEqual(mu.fetch_lists(_AuthClient(lists_body=[])), [])

    def _assert_rejected(self, body, needle):
        with self.assertRaises(ValueError) as caught:
            mu.fetch_lists(_AuthClient(lists_body=body))
        self.assertIn(needle, str(caught.exception))

    def test_a_non_array_body_is_named(self):
        self._assert_rejected({"lists": []}, "expected an array")

    def test_a_null_entry_is_named(self):
        self._assert_rejected([None], "entry 0")

    def test_an_entry_without_list_id_is_named(self):
        self._assert_rejected([{"title": "A"}], "no list_id")

    def test_an_entry_without_a_title_is_named(self):
        self._assert_rejected([{"list_id": 5}], "no usable title")

    def test_a_null_title_is_named(self):
        self._assert_rejected([{"list_id": 5, "title": None}], "no usable title")

    def test_validation_happens_before_anything_downstream_indexes_the_dicts(self):
        """export_all_lists and run_finished_check both index these dicts
        directly; the boundary check is what keeps them safe, so a malformed
        index must never get past fetch_lists."""
        with self.assertRaises(ValueError):
            mu.fetch_lists(_AuthClient(lists_body=[{"list_id": 1}]))


# ==================== option orchestration ====================
class TestRunScanLists(TempExportsCase):
    def _client(self, items):
        return _MenuApi([{"list_id": 1, "title": "Reading"}], {1: items})

    def test_it_exports_saves_and_rotates(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mu.run_scan_lists(self._client([_rec(1, "A")]))
        folders = [d for d in os.listdir(self.dir.name) if mu._is_export_folder(d)]
        self.assertEqual(len(folders), 1)

    def test_an_account_with_no_lists_stops_early(self):
        client = _MenuApi([], {})
        with contextlib.redirect_stdout(io.StringIO()), _LogCapture() as cap:
            mu.run_scan_lists(client)
        self.assertIn("No lists found", cap.text)
        self.assertEqual([d for d in os.listdir(self.dir.name) if mu._is_export_folder(d)], [])

    def test_rotation_still_runs_when_the_diff_fails(self):
        """The export is already on disk by then. Skipping rotation would let
        exports/ grow past MAX_EXPORTS without bound across repeated failures."""
        with (
            patch.object(mu, "compare_exports", side_effect=RuntimeError("diff blew up")),
            patch.object(mu, "rotate_exports") as rotate,
        ):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                mu.run_scan_lists(self._client([_rec(1, "A")]))
            rotate.assert_called_once()

    def test_a_second_run_reports_an_added_series(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mu.run_scan_lists(self._client([_rec(1, "A")]))
        with contextlib.redirect_stdout(io.StringIO()), _LogCapture() as cap:
            mu.run_scan_lists(self._client([_rec(1, "A"), _rec(2, "B")]))
        self.assertIn("Added:", cap.text)


class TestRunRelatedCheck(TempExportsCase):
    def test_it_writes_the_report(self):
        client = _MenuApi([{"list_id": 1, "title": "Reading"}], {1: [_rec(1, "A")]})
        with (
            patch.object(mu, "fetch_series_related", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mu.run_related_check(client)
        self.assertTrue(os.path.isfile(os.path.join(self.dir.name, "related.txt")))

    def test_an_account_with_no_lists_stops_early(self):
        with contextlib.redirect_stdout(io.StringIO()), _LogCapture() as cap:
            mu.run_related_check(_MenuApi([], {}))
        self.assertIn("No lists found", cap.text)
        self.assertFalse(os.path.isfile(os.path.join(self.dir.name, "related.txt")))


class TestRunFinishedCheck(TempExportsCase):
    def test_it_writes_the_report(self):
        client = _MenuApi([{"list_id": 1, "title": "Wish List"}], {1: [_rec(1, "A")]})
        with (
            patch.object(mu, "fetch_series_status", return_value={"completed": True, "status": "Complete"}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mu.run_finished_check(client)
        self.assertTrue(os.path.isfile(os.path.join(self.dir.name, "ready_to_read.txt")))

    def test_no_wish_list_stops_early(self):
        with contextlib.redirect_stdout(io.StringIO()), _LogCapture() as cap:
            mu.run_finished_check(_MenuApi([{"list_id": 1, "title": "Reading"}], {1: []}))
        self.assertIn("No 'Wish List' found", cap.text)

    def test_an_empty_wish_list_stops_early(self):
        with contextlib.redirect_stdout(io.StringIO()), _LogCapture() as cap:
            mu.run_finished_check(_MenuApi([{"list_id": 1, "title": "Wish List"}], {1: []}))
        self.assertIn("empty", cap.text)


# ==================== graceful shutdown ====================
class TestInterruptibleWorkerPool(unittest.TestCase):
    """ThreadPoolExecutor's own context manager shuts down with wait=True and
    no cancellation, so Ctrl+C during a several-hundred-series lookup waited
    for every queued task before the interrupt got through."""

    def test_queued_work_is_cancelled_on_interrupt(self):
        release = threading.Event()
        started = []
        lock = threading.Lock()

        def slow(n):
            with lock:
                started.append(n)
            release.wait(5)

        futures = []
        began = time.perf_counter()
        with self.assertRaises(KeyboardInterrupt), mu._worker_pool(2) as pool:
            futures = [pool.submit(slow, i) for i in range(40)]
            while len(started) < 2:  # let the pool actually pick work up
                time.sleep(0.005)
            raise KeyboardInterrupt
        elapsed = time.perf_counter() - began
        release.set()

        cancelled = sum(1 for f in futures if f.cancelled())
        self.assertGreater(cancelled, 30, "queued work was not cancelled")
        self.assertLess(elapsed, 3, "shutdown waited for the queue instead of dropping it")

    def test_normal_completion_still_waits_for_every_task(self):
        done = []
        with mu._worker_pool(4) as pool:
            for i in range(20):
                pool.submit(lambda n: done.append(n), i)
        self.assertEqual(len(done), 20, "a clean exit must not drop work")

    def test_an_ordinary_exception_also_drops_the_queue(self):
        with self.assertRaises(RuntimeError), mu._worker_pool(2) as pool:
            pool.submit(lambda: None)
            raise RuntimeError("boom")


class TestMenuInterrupt(unittest.TestCase):
    def test_ctrl_c_during_an_option_returns_to_the_menu(self):
        """Interrupt the operation, not the session -- the same thing Ctrl+C
        does at any other interactive prompt."""
        calls = []

        def interrupted(_client):
            calls.append("ran")
            raise KeyboardInterrupt

        with (
            patch.object(mu, "check_site_reachable", return_value=True),
            patch.object(mu, "login", return_value="tok"),
            patch.object(mu, "logout") as bye,
            patch.object(mu, "run_scan_lists", interrupted),
            patch("httpx.Client", return_value=MagicMock()),
            patch("builtins.input", side_effect=["1", "0"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mu.main()

        self.assertEqual(calls, ["ran"])
        bye.assert_called_once()


# ==================== presentation ====================
class TestDisplayWidth(unittest.TestCase):
    def test_ascii_is_one_column_per_character(self):
        self.assertEqual(mu._display_width("hello"), 5)

    def test_ansi_codes_do_not_count(self):
        self.assertEqual(mu._display_width(mu.term.success("hello")), 5)

    def test_emoji_count_as_two_columns(self):
        for emoji in ("✅", "📊", "📋"):
            with self.subTest(emoji=emoji):
                self.assertEqual(mu._display_width(emoji), 2)

    def test_a_variation_selector_does_not_add_a_column(self):
        """U+26A0 is narrow alone but renders as a two-column emoji with
        U+FE0F, and the selector itself occupies nothing."""
        self.assertEqual(mu._display_width("⚠️"), 2)

    def test_box_edges_line_up_with_mixed_content(self):
        lines = [
            mu.term.success("  ✅ NO CHANGES"),
            mu.term.dim("     plain ascii"),
            mu.term.bold("  📊 Summary: 5 list(s)"),
            mu.term.bold("  ⚠️  CHANGES DETECTED"),
        ]
        box = mu._box(lines)
        widths = {mu._display_width(line) for line in box}
        self.assertEqual(len(widths), 1, f"box edges are ragged: {sorted(widths)}")

    def test_a_line_longer_than_the_box_widens_it_instead_of_breaking_it(self):
        long_line = "x" * 100
        box = mu._box([long_line, "short"], width=64)
        widths = {mu._display_width(line) for line in box}
        self.assertEqual(len(widths), 1)
        self.assertGreaterEqual(next(iter(widths)), 102)


class TestWriteJson(TempExportsCase):
    def test_output_matches_what_json_dump_would_have_written(self):
        payload = [_rec(i, f"Series {i} — ünïcode") for i in range(50)]
        streamed = os.path.join(self.dir.name, "streamed.json")
        with open(streamed, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        once = os.path.join(self.dir.name, "once.json")
        mu._write_json(once, payload)

        with open(streamed, "rb") as a, open(once, "rb") as b:
            self.assertEqual(a.read(), b.read(), "the faster write must be byte-identical")


# ==================== malformed pages and menu resilience ====================
class _ShapeApi:
    """Returns one list page with a chosen field replaced by junk."""

    def __init__(self, body):
        self._body = body

    def get(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def put(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def post(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, self._body)


class TestMalformedPageIsExplained(unittest.TestCase):
    """A null field used to surface as a bare TypeError from inside the
    paging arithmetic -- "'<=' not supported between instances of 'NoneType'
    and 'int'" -- naming neither the list nor the page, and reading like a
    bug in this program rather than a bad response."""

    def _assert_explained(self, body, needle):
        api = _ShapeApi(body)
        with self.assertRaises(ValueError) as caught:
            mu.export_list(api, 4242, "Some List")
        message = str(caught.exception)
        self.assertIn(needle, message)
        self.assertIn("4242", message, "the failing list must be identifiable")
        self.assertIn("page 1", message, "the failing page must be identifiable")

    def test_null_results_is_named_not_a_typeerror(self):
        self._assert_explained({"results": None, "total_hits": 5}, "'results'")

    def test_null_total_hits_is_named_not_a_typeerror(self):
        self._assert_explained({"results": [], "total_hits": None}, "'total_hits'")

    def test_a_json_array_instead_of_an_object_is_named(self):
        self._assert_explained([1, 2, 3], "expected an object")

    def test_a_string_total_hits_is_named(self):
        self._assert_explained({"results": [], "total_hits": "12"}, "'total_hits'")

    def test_a_malformed_page_never_yields_a_short_list(self):
        """The whole reason this raises instead of defaulting: a truncated
        list saved as complete would make the next run report every missing
        series as removed."""
        api = _ShapeApi({"results": None, "total_hits": 500})
        with self.assertRaises(ValueError):
            mu.export_list(api, 1, "L")

    def test_well_formed_pages_are_unaffected(self):
        api = _PagingApi({1: (_items(1, 365), 365)})
        self.assertEqual(len(mu.export_list(api, 1, "L")), 365)


class TestMenuSurvivesAFailingOption(unittest.TestCase):
    """A failure inside one option used to propagate out of the menu loop and
    end the run, so a single bad response dropped the user back to the shell
    with a traceback and a session they would have to log in again to replace."""

    def _run_menu(self, keys, failing=("1",)):
        seen = []

        def make(name):
            def action(_client):
                seen.append(name)
                if name in failing:
                    raise RuntimeError("boom in " + name)

            return action

        with (
            patch.object(mu, "check_site_reachable", return_value=True),
            patch.object(mu, "login", return_value="tok"),
            patch.object(mu, "logout"),
            patch.object(mu, "run_scan_lists", make("1")),
            patch.object(mu, "run_related_check", make("2")),
            patch.object(mu, "run_finished_check", make("3")),
            patch("httpx.Client", return_value=MagicMock()),
            patch("builtins.input", side_effect=keys),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mu.main()
        return seen

    def test_a_failing_option_returns_to_the_menu(self):
        seen = self._run_menu(["1", "2", "0"])
        self.assertEqual(seen, ["1", "2"], "the session ended instead of offering the menu again")

    def test_several_failures_in_a_row_do_not_end_the_session(self):
        seen = self._run_menu(["1", "1", "1", "0"], failing=("1",))
        self.assertEqual(seen, ["1", "1", "1"])

    def test_the_failure_is_logged_with_its_traceback(self):
        with _LogCapture() as cap:
            self._run_menu(["1", "0"])
        self.assertIn("boom in 1", cap.text)
        self.assertIn("RuntimeError", cap.text, "the traceback must reach the log")

    def test_ctrl_c_at_the_prompt_exits_cleanly(self):
        seen = self._run_menu([KeyboardInterrupt()], failing=())
        self.assertEqual(seen, [])

    def test_closed_stdin_exits_cleanly_instead_of_raising(self):
        seen = self._run_menu([EOFError()], failing=())
        self.assertEqual(seen, [])

    def test_logout_still_happens_after_a_failing_option(self):
        with patch.object(mu, "logout") as bye:
            with (
                patch.object(mu, "check_site_reachable", return_value=True),
                patch.object(mu, "login", return_value="tok"),
                patch.object(mu, "run_scan_lists", side_effect=RuntimeError("boom")),
                patch("httpx.Client", return_value=MagicMock()),
                patch("builtins.input", side_effect=["1", "0"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                mu.main()
            bye.assert_called_once()


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.mangaupdates.com/v1/lists")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request))


class TestExpiredSession(unittest.TestCase):
    """The token from login() was used for the whole session, so once it
    lapsed every option failed -- each with "You are still logged in" under
    it -- until the program was restarted."""

    def _run_menu(self, action, keys=("1", "0")):
        out = io.StringIO()
        with (
            patch.object(mu, "check_site_reachable", return_value=True),
            patch.object(mu, "login", side_effect=["tok", "fresh", "fresher"]) as login,
            patch.object(mu, "logout"),
            patch.object(mu, "run_scan_lists", action),
            patch("httpx.Client", return_value=MagicMock()),
            patch("builtins.input", side_effect=list(keys)),
            contextlib.redirect_stdout(out),
            _LogCapture(),
        ):
            mu.main()
        return login.call_count, out.getvalue()

    def test_a_401_logs_in_again_and_runs_the_option_once_more(self):
        calls = []

        def expires_once(_client):
            calls.append(1)
            if len(calls) == 1:
                raise _status_error(401)

        logins, out = self._run_menu(expires_once)
        self.assertEqual(len(calls), 2)
        self.assertEqual(logins, 2, "one login at start, one after the 401")
        self.assertNotIn("did not finish", out)

    def test_a_second_401_is_reported_and_not_retried_again(self):
        calls = []

        def always_rejected(_client):
            calls.append(1)
            raise _status_error(401)

        logins, out = self._run_menu(always_rejected)
        self.assertEqual((len(calls), logins), (2, 2))
        self.assertIn("rejected the session even after logging in again", out)
        self.assertNotIn("You are still logged in", out)

    def test_other_failures_are_not_retried(self):
        calls = []

        def server_error(_client):
            calls.append(1)
            raise _status_error(500)

        logins, out = self._run_menu(server_error)
        self.assertEqual((len(calls), logins), (1, 1))
        self.assertIn("You are still logged in", out)


class TestMenuPrompt(unittest.TestCase):
    def test_a_run_of_unusable_answers_quits_instead_of_asking_forever(self):
        """The menu asked again on every unusable answer without limit, so an
        unattended feed of junk kept it asking forever. After
        MAX_UNRECOGNIZED it gives the answer that changes nothing: 0."""
        ran = []
        with (
            patch.object(mu, "check_site_reachable", return_value=True),
            patch.object(mu, "login", return_value="tok"),
            patch.object(mu, "logout") as bye,
            patch.object(mu, "run_scan_lists", lambda _c: ran.append("1")),
            patch("httpx.Client", return_value=MagicMock()),
            patch("builtins.input", side_effect=["x"] * mu.term.MAX_UNRECOGNIZED),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            mu.main()
        self.assertEqual(ran, [])
        bye.assert_called_once()
        self.assertIn("type a number between 0 and 4", out.getvalue())


# ==================== export folder handling ====================
class _MenuApi:
    """Minimal stand-in for the API client used by the run_* entry points."""

    def __init__(self, lists, items_by_list=None):
        self._lists = lists
        self._items = items_by_list or {}

    def get(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, self._lists)

    def put(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def post(self, url, **kwargs):
        list_id = int(url.rstrip("/search").rsplit("/", 1)[-1])
        page = kwargs["json"]["page"]
        items = self._items.get(list_id, [])
        return _FakeApiResponse(200, {"results": items if page == 1 else [], "total_hits": len(items)})


class _LogCapture:
    """Collect everything the module logger emits during a block."""

    def __enter__(self):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setLevel(logging.DEBUG)
        mu.log.addHandler(self.handler)
        self._level = mu.log.level
        mu.log.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        mu.log.removeHandler(self.handler)
        mu.log.setLevel(self._level)
        return False

    @property
    def text(self):
        return self.buf.getvalue()


class TestUnreadablePreviousExport(TempExportsCase):
    """A corrupted previous export used to be substituted with an empty list,
    so every series in it came back reported as newly Added -- a wrong answer
    that looks exactly like a real account change."""

    def _write_corrupt_previous(self):
        prev = os.path.join(self.dir.name, "01.01.2026_00-00-00")
        os.makedirs(prev)
        with open(os.path.join(prev, mu.MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump({"Reading List": "Reading List"}, f)
        with open(os.path.join(prev, "Reading List.json"), "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        return prev

    def test_load_prev_exports_reports_it_as_unreadable_not_as_empty(self):
        prev = self._write_corrupt_previous()
        loaded, unreadable = mu._load_prev_exports(prev, mu.load_manifest(prev, ["Reading List"]))
        self.assertEqual(unreadable, {"Reading List"})
        self.assertNotIn("Reading List", loaded, "an unreadable file must not masquerade as an empty list")

    def test_series_are_not_reported_as_added_when_the_previous_file_is_corrupt(self):
        self._write_corrupt_previous()
        exports = {"Reading List": [_rec(i, f"S{i}") for i in range(1, 6)]}
        folder = mu.save_exports(exports)
        with _LogCapture() as cap:
            changed = mu.compare_exports(folder, exports)
        self.assertNotIn("Added:", cap.text, "a corrupt previous export must not look like 5 new series")
        self.assertIn("could not be read", cap.text)
        self.assertTrue(changed, "an uncomparable list must not be reported as 'no changes'")


class TestSameSecondSave(TempExportsCase):
    """os.replace onto an existing directory fails, so two runs inside one
    second used to kill the second run after all its work was done."""

    def test_two_saves_in_the_same_second_both_succeed(self):
        fixed = datetime.datetime(2026, 1, 1, 12, 0, 0)

        class Frozen(datetime.datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return fixed

        with patch.object(mu, "datetime", Frozen):
            first = mu.save_exports({"L": [_rec(1, "A")]})
            second = mu.save_exports({"L": [_rec(2, "B")]})

        self.assertNotEqual(first, second, "the second save must not reuse the first folder")
        with open(os.path.join(second, "L.json"), encoding="utf-8") as f:
            self.assertEqual(mu.get_series_ids(json.load(f)), {2: "B"})
        with open(os.path.join(first, "L.json"), encoding="utf-8") as f:
            self.assertEqual(mu.get_series_ids(json.load(f)), {1: "A"}, "the first save must survive intact")

    def test_the_bumped_name_is_still_a_valid_export_folder(self):
        """It must stay sortable and rotatable, which a '_2' suffix would break."""
        fixed = datetime.datetime(2026, 1, 1, 12, 0, 0)

        class Frozen(datetime.datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ARG003
                return fixed

        with patch.object(mu, "datetime", Frozen):
            mu.save_exports({"L": [_rec(1, "A")]})
            second = mu.save_exports({"L": [_rec(2, "B")]})

        name = os.path.basename(second)
        self.assertTrue(mu._is_export_folder(name), f"{name} is not recognised as an export folder")
        self.assertEqual(mu.find_previous_export(second), os.path.join(self.dir.name, "01.01.2026_12-00-00"))


class TestStrayTempFiles(TempExportsCase):
    def test_a_leftover_tmp_file_is_swept_not_just_tmp_directories(self):
        """save_related_series builds its report with mkstemp(suffix='.tmp')
        in this same folder; only directories were being swept, so a crash
        mid-report left a file nothing would ever clean up."""
        stray_file = os.path.join(self.dir.name, "leftover.tmp")
        with open(stray_file, "w", encoding="utf-8") as f:
            f.write("half-written report")
        stray_dir = os.path.join(self.dir.name, "leftover_dir.tmp")
        os.makedirs(stray_dir)

        mu.save_exports({"L": [_rec(1, "A")]})

        self.assertFalse(os.path.exists(stray_file), "stray .tmp file was left behind")
        self.assertFalse(os.path.exists(stray_dir), "stray .tmp directory was left behind")


class TestOnlyOurOwnFoldersAreTouched(TempExportsCase):
    """Both rotation and the previous-export lookup used to accept any
    directory at all. An unparseable name sorts as datetime.min, i.e. as the
    oldest export there could be, so rotation deleted it first."""

    def test_rotation_never_deletes_an_unrelated_folder(self):
        notes = os.path.join(self.dir.name, "my_notes")
        os.makedirs(notes)
        with open(os.path.join(notes, "note.txt"), "w", encoding="utf-8") as f:
            f.write("important")
        for day in range(1, 5):
            os.makedirs(os.path.join(self.dir.name, f"0{day}.01.2026_00-00-00"))

        mu.rotate_exports()

        self.assertTrue(os.path.exists(notes), "an unrelated folder was deleted by rotation")

    def test_an_unrelated_folder_does_not_count_toward_max_exports(self):
        os.makedirs(os.path.join(self.dir.name, "my_notes"))
        for day in range(1, 4):
            os.makedirs(os.path.join(self.dir.name, f"0{day}.01.2026_00-00-00"))

        with patch.object(mu, "MAX_EXPORTS", 3):
            mu.rotate_exports()

        remaining = sorted(d for d in os.listdir(self.dir.name) if mu._is_export_folder(d))
        self.assertEqual(len(remaining), 3, "a real export was rotated out to make room for a stray folder")

    def test_a_folder_that_is_not_a_timestamp_is_never_used_as_the_previous_export(self):
        os.makedirs(os.path.join(self.dir.name, "not-a-date"))
        current = os.path.join(self.dir.name, "05.01.2026_00-00-00")
        os.makedirs(current)
        self.assertIsNone(mu.find_previous_export(current))

    def test_a_real_previous_export_is_still_found_alongside_strays(self):
        os.makedirs(os.path.join(self.dir.name, "not-a-date"))
        real = os.path.join(self.dir.name, "02.01.2026_00-00-00")
        os.makedirs(real)
        current = os.path.join(self.dir.name, "05.01.2026_00-00-00")
        os.makedirs(current)
        self.assertEqual(mu.find_previous_export(current), real)


class TestDuplicateWishList(TempExportsCase):
    def test_a_second_list_with_the_same_title_is_reported_not_ignored(self):
        """export_all_lists warns and keeps both when two lists share a title.
        This path silently checked the first and ignored the rest, so the same
        account state was reported two different ways."""
        client = _MenuApi(
            [{"list_id": 7, "title": "Wish List"}, {"list_id": 9, "title": "Wish List"}],
            {7: [_rec(70, "Seven")], 9: [_rec(90, "Nine")]},
        )
        with _LogCapture() as cap, patch.object(mu, "fetch_series_status", return_value=None):
            mu.run_finished_check(client)
        self.assertIn("Found 2 lists titled 'Wish List'", cap.text)

    def test_a_single_wish_list_produces_no_such_warning(self):
        client = _MenuApi([{"list_id": 7, "title": "Wish List"}], {7: [_rec(70, "Seven")]})
        with _LogCapture() as cap, patch.object(mu, "fetch_series_status", return_value=None):
            mu.run_finished_check(client)
        self.assertNotIn("lists titled", cap.text)


# ==================== list pagination ====================
class _PagingApi:
    """A fake lists API with fully controlled paging behaviour.

    `spec` maps list_id -> (items, reported_total_hits). Reporting a total
    that disagrees with the items is deliberate: that disagreement is the
    only thing the loop's two stop rules exist for.

    `mode` shapes the pathological cases:
      "hole"     -- page 2 comes back empty while page 3 still has items
      "overfull" -- page 1 returns more rows than perpage asked for
    """

    def __init__(self, spec, mode="normal", delay=0.0):
        self.spec = spec
        self.mode = mode
        self.delay = delay
        self.calls = []
        self.intervals = []
        self._lock = threading.Lock()

    def get(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def put(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def post(self, url, **kwargs):
        list_id = int(url.rstrip("/search").rsplit("/", 1)[-1])
        page = kwargs["json"]["page"]
        start = time.perf_counter()
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append((list_id, page))
            self.intervals.append((start, time.perf_counter()))

        items, reported_total = self.spec[list_id]
        per_page = mu.ITEMS_PER_PAGE
        if self.mode == "hole" and page == 2:
            results = []
        elif self.mode == "overfull" and page == 1:
            results = items[: per_page * 3]
        else:
            begin = (page - 1) * per_page
            results = items[begin : begin + per_page]
        return _FakeApiResponse(200, {"results": results, "total_hits": reported_total})


def _items(list_id: int, count: int) -> list[dict]:
    return [_rec(list_id * 100000 + i, f"L{list_id} Series {i}") for i in range(count)]


def _peak_overlap(intervals) -> int:
    events = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    peak = running = 0
    for _t, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


class TestExportListPaging(unittest.TestCase):
    def test_single_short_page(self):
        api = _PagingApi({1: (_items(1, 7), 7)})
        self.assertEqual(len(mu.export_list(api, 1, "L")), 7)
        self.assertEqual(api.calls, [(1, 1)])

    def test_empty_list_costs_one_request(self):
        api = _PagingApi({1: ([], 0)})
        self.assertEqual(mu.export_list(api, 1, "L"), [])
        self.assertEqual(api.calls, [(1, 1)])

    def test_exactly_one_full_page_does_not_ask_for_a_second(self):
        api = _PagingApi({1: (_items(1, mu.ITEMS_PER_PAGE), mu.ITEMS_PER_PAGE)})
        self.assertEqual(len(mu.export_list(api, 1, "L")), mu.ITEMS_PER_PAGE)
        self.assertEqual(api.calls, [(1, 1)])

    def test_multi_page_returns_every_item_in_order(self):
        expected = _items(1, 365)
        api = _PagingApi({1: (expected, 365)})
        got = mu.export_list(api, 1, "L")
        self.assertEqual(got, expected, "items must come back in page order, not completion order")

    def test_page_count_comes_from_total_hits_not_from_walking(self):
        api = _PagingApi({1: (_items(1, 365), 365)})
        mu.export_list(api, 1, "L")
        self.assertEqual(sorted(api.calls), [(1, 1), (1, 2), (1, 3), (1, 4)])

    def test_a_hole_in_the_paging_aborts_instead_of_truncating(self):
        """A blank page mid-list must abort, not quietly shorten the export.

        The serial walk ended at the first empty page because that was how it
        discovered the end. Every page is fetched up front now, so an empty
        one in the middle says nothing about where the list ends -- it only
        means one response came back blank. Ending there both discarded pages
        already in hand and, far worse, saved the remainder as if it were the
        whole list, which the next run reads as "these series were removed".
        """
        api = _PagingApi({1: (_items(1, 365), 365)}, mode="hole")
        with self.assertRaises(ValueError) as caught:
            mu.export_list(api, 1, "L")
        self.assertIn("incomplete", str(caught.exception).lower())
        self.assertIn("265 of 365", str(caught.exception))

    def test_pages_after_a_blank_one_are_not_discarded(self):
        """The pages fetched after the hole must still be counted.

        The old rule dropped them on the floor; the shortfall reported in the
        abort message is the proof they were kept -- 265, not 100.
        """
        pages = [_items(1, 100), [], _items(101, 100), _items(201, 65)]
        self.assertEqual(len(mu._join_pages(pages, 365)), 265)

    def test_stops_once_total_hits_is_reached(self):
        """A page returning more rows than asked for must still stop on total_hits.

        Page 1 comes back with 300 rows against a reported total of 250, so
        the very first check already satisfies "we hold total_hits items" and
        pages 2 and 3 -- which the computed range did ask for -- contribute
        nothing, exactly as the serial walk behaved.
        """
        api = _PagingApi({1: (_items(1, 365), 250)}, mode="overfull")
        got = mu.export_list(api, 1, "L")
        self.assertEqual(len(got), mu.ITEMS_PER_PAGE * 3)

    def test_understated_total_hits_yields_the_pages_it_asked_for(self):
        api = _PagingApi({1: (_items(1, 350), 120)})
        self.assertEqual(len(mu.export_list(api, 1, "L")), 200)

    def test_a_failing_page_is_not_swallowed(self):
        """A lost page must abort the export, never yield a short list.

        A partial list read as complete is the worst outcome available here:
        the diff against the previous export would report every missing
        series as removed.
        """

        class BrokenPage(_PagingApi):
            def post(self, url, **kwargs):
                if kwargs["json"]["page"] == 3:
                    raise httpx.ConnectError("boom")
                return super().post(url, **kwargs)

        api = BrokenPage({1: (_items(1, 365), 365)})
        with patch.object(mu, "RETRY_DELAY", 0), self.assertRaises(httpx.ConnectError):
            mu.export_list(api, 1, "L")

    def test_pages_are_fetched_concurrently(self):
        api = _PagingApi({1: (_items(1, 500), 500)}, delay=0.05)
        with patch.object(mu, "LIST_PAGE_WORKERS", 4):
            mu.export_list(api, 1, "L")
        # Page 1 must be alone (its total_hits is what reveals the rest);
        # pages 2-5 then go together.
        self.assertGreaterEqual(_peak_overlap(api.intervals), 2, "pages were fetched one at a time")

    def test_concurrency_never_exceeds_the_configured_limit(self):
        api = _PagingApi({1: (_items(1, 2000), 2000)}, delay=0.05)
        with patch.object(mu, "LIST_PAGE_WORKERS", 3):
            mu.export_list(api, 1, "L")
        self.assertLessEqual(_peak_overlap(api.intervals), 3)


class TestExportAllListsPaging(unittest.TestCase):
    SPEC = {
        1: (_items(1, 365), 365),
        2: (_items(2, 268), 268),
        3: (_items(3, 2), 2),
        4: ([], 0),
    }
    LISTS = [{"list_id": i, "title": f"List {i}"} for i in (1, 2, 3, 4)]

    def test_every_list_comes_back_whole_and_in_order(self):
        api = _PagingApi(self.SPEC)
        out = mu.export_all_lists(api, self.LISTS)
        self.assertEqual(list(out), ["List 1", "List 2", "List 3", "List 4"])
        for list_id in self.SPEC:
            self.assertEqual(out[f"List {list_id}"], self.SPEC[list_id][0])

    def test_request_count_is_unchanged_by_going_concurrent(self):
        api = _PagingApi(self.SPEC)
        mu.export_all_lists(api, self.LISTS)
        # 4 + 3 + 1 + 1 pages -- exactly what a serial walk would have asked for.
        self.assertEqual(len(api.calls), 9)
        self.assertEqual(len(set(api.calls)), 9, "no page fetched twice")

    def test_no_lists_makes_no_requests(self):
        api = _PagingApi(self.SPEC)
        self.assertEqual(mu.export_all_lists(api, []), {})
        self.assertEqual(api.calls, [])

    def test_duplicate_titles_are_keyed_by_list_order_not_completion_order(self):
        """The dedup suffix must not depend on which request finishes first.

        Whichever list finishes first, the list_id=1 list is the one that
        keeps the bare title, because that is the order they were given in.
        """
        spec = {1: (_items(1, 400), 400), 2: (_items(2, 1), 1), 3: (_items(3, 250), 250)}
        lists = [{"list_id": i, "title": "Same"} for i in (1, 2, 3)]
        for _ in range(10):
            out = mu.export_all_lists(_PagingApi(spec, delay=0.002), lists)
            self.assertEqual(list(out), ["Same", "Same (2)", "Same (3)"])
            self.assertEqual(out["Same"], spec[1][0])
            self.assertEqual(out["Same (2)"], spec[2][0])
            self.assertEqual(out["Same (3)"], spec[3][0])

    def test_lists_are_fetched_concurrently_with_each_other(self):
        api = _PagingApi(self.SPEC, delay=0.05)
        with patch.object(mu, "LIST_PAGE_WORKERS", 4):
            mu.export_all_lists(api, self.LISTS)
        self.assertGreaterEqual(_peak_overlap(api.intervals), 2, "lists were exported one after another")

    def test_concurrency_never_exceeds_the_configured_limit(self):
        api = _PagingApi(self.SPEC, delay=0.05)
        with patch.object(mu, "LIST_PAGE_WORKERS", 2):
            mu.export_all_lists(api, self.LISTS)
        self.assertLessEqual(_peak_overlap(api.intervals), 2)

    def test_a_single_large_list_still_parallelises_its_pages(self):
        """One list must not collapse the page fetches back to serial.

        The pool was originally sized by the number of lists and reused for
        the page fetches too. An account with one big list is a single plan
        entry, so that pool had one thread and every page after the first
        went out one at a time -- the exact behaviour this replaced, hidden
        because the account it was developed against has five lists.
        """
        spec = {1: (_items(1, 1000), 1000)}
        api = _PagingApi(spec, delay=0.02)
        with patch.object(mu, "LIST_PAGE_WORKERS", 8):
            out = mu.export_all_lists(api, [{"list_id": 1, "title": "Big"}])
        self.assertEqual(out["Big"], spec[1][0])
        self.assertGreater(
            _peak_overlap(api.intervals),
            1,
            "pages 2-10 of a single list were fetched one at a time",
        )

    def test_a_failing_page_aborts_rather_than_exporting_a_short_list(self):
        class BrokenPage(_PagingApi):
            def post(self, url, **kwargs):
                if (int(url.rstrip("/search").rsplit("/", 1)[-1]), kwargs["json"]["page"]) == (2, 2):
                    raise httpx.ConnectError("boom")
                return super().post(url, **kwargs)

        with patch.object(mu, "RETRY_DELAY", 0), self.assertRaises(httpx.ConnectError):
            mu.export_all_lists(BrokenPage(self.SPEC), self.LISTS)


class TestPageRange(unittest.TestCase):
    def test_first_page_alone_covers_a_list_that_fits(self):
        self.assertEqual(mu._pages_after_first(0), [])
        self.assertEqual(mu._pages_after_first(1), [])
        self.assertEqual(mu._pages_after_first(mu.ITEMS_PER_PAGE), [])

    def test_one_item_over_a_page_boundary_adds_exactly_one_page(self):
        self.assertEqual(mu._pages_after_first(mu.ITEMS_PER_PAGE + 1), [2])

    def test_range_is_clamped_to_the_safety_limit(self):
        pages = mu._pages_after_first(mu.ITEMS_PER_PAGE * 10_000)
        self.assertEqual(pages[-1], mu.MAX_LIST_PAGES)
        self.assertEqual(len(pages), mu.MAX_LIST_PAGES - 1)


# ==================== Anime-Planet parsing ====================
# The fixtures below mirror the markup the live site sends (verified against
# www.anime-planet.com in September 2026): one statList per profile section
# with slCount/slLabel spans inside the anchor, and cardDeck cards whose
# tooltip anchor carries only "tooltip manga<N>" as its class.

AP_PROFILE_HTML = """
<html><body>
<ul class="statList">
  <li class="status1">
    <a href="/users/nawid3333/manga/read"><span class="slCount">0</span><span class="slLabel">read</span></a>
  </li>
  <li class="status2">
    <a href="/users/nawid3333/manga/reading"><span class="slCount">2</span><span class="slLabel">reading</span></a>
  </li>
  <li class="status4">
    <a href="/users/nawid3333/manga/wanttoread">
      <span class="slCount">1,234</span><span class="slLabel">want to read</span>
    </a>
  </li>
  <li class="status5">
    <a href="/users/nawid3333/manga/stalled"><span class="slCount">3</span><span class="slLabel">stalled</span></a>
  </li>
</ul>
<ul class="statList">
  <li class="status1">
    <a href="/users/nawid3333/anime/watched"><span class="slCount">7</span><span class="slLabel">watched</span></a>
  </li>
</ul>
</body></html>
"""


class TestAnimePlanetProfileParsing(unittest.TestCase):
    def test_every_list_is_returned_including_empty_ones(self):
        """Empty lists used to be dropped here, so a list emptied since the
        last run vanished from the export and was reported as removed."""
        infos = mu._ap_parse_profile_list_counts(AP_PROFILE_HTML)
        keys = [key for key, _href, _label, _count in infos]
        self.assertEqual(
            keys,
            ["manga/read", "manga/reading", "manga/wanttoread", "manga/stalled", "anime/watched"],
            "the count-0 list must be kept, and both sections must be read",
        )
        counts = {key: count for key, _href, _label, count in infos}
        self.assertEqual(counts["manga/read"], 0)

    def test_counts_are_returned_as_integers(self):
        infos = mu._ap_parse_profile_list_counts(AP_PROFILE_HTML)
        counts = {key: count for key, _href, _label, count in infos}
        self.assertEqual(counts["manga/reading"], 2)
        self.assertEqual(counts["anime/watched"], 7)

    def test_comma_formatted_counts_are_understood(self):
        """The site formats thousands with a comma; int('1,234') raises."""
        infos = mu._ap_parse_profile_list_counts(AP_PROFILE_HTML)
        counts = {key: count for key, _href, _label, count in infos}
        self.assertEqual(counts["manga/wanttoread"], 1234)

    def test_labels_are_mapped_through_ap_list_types(self):
        infos = mu._ap_parse_profile_list_counts(AP_PROFILE_HTML)
        labels = {key: label for key, _href, label, _count in infos}
        self.assertEqual(labels["manga/wanttoread"], "Want to Read")
        self.assertEqual(labels["anime/watched"], "Watched")

    def test_every_list_section_of_the_profile_is_read(self):
        """The profile carries one statList per section; only the first used
        to be parsed, so every anime list of an account with both was
        silently dropped."""
        infos = mu._ap_parse_profile_list_counts(AP_PROFILE_HTML)
        self.assertTrue(any(key.startswith("anime/") for key, _h, _l, _c in infos))

    def test_an_unknown_list_url_keeps_a_derived_label(self):
        html = """
        <ul class="statList">
          <li class="status9">
            <a href="/users/nawid3333/manga/new-list-kind">
              <span class="slCount">4</span><span class="slLabel">new kind</span>
            </a>
          </li>
        </ul>
        """
        infos = mu._ap_parse_profile_list_counts(html)
        self.assertEqual(len(infos), 1)
        key, href, label, count = infos[0]
        self.assertEqual(key, "manga/new-list-kind")
        self.assertEqual(href, "/users/nawid3333/manga/new-list-kind")
        self.assertEqual(label, "Manga New List Kind")
        self.assertEqual(count, 4)

    def test_an_unreadable_list_entry_stops_the_parse_instead_of_being_skipped(self):
        """Each of these used to be skipped without a word, so that list was
        left out of the export and the next comparison reported it, and every
        title in it, as removed. It must stop the run instead -- the same rule
        fetch_lists applies to a malformed MangaUpdates list index."""
        good = (
            '<li class="status4"><a href="/users/nawid3333/manga/dropped">'
            '<span class="slCount">1</span><span class="slLabel">dropped</span></a></li>'
        )
        broken = {
            "unreadable count": '<li class="status1"><a href="/users/nawid3333/manga/read">'
            '<span class="slCount">nope</span><span class="slLabel">read</span></a></li>',
            "no count": '<li class="status2"><a href="/users/nawid3333/manga/reading">'
            '<span class="slLabel">no count</span></a></li>',
            "no link": '<li class="status3"><span class="slCount">9</span></li>',
            "empty link": '<li class="status3"><a href=""><span class="slCount">9</span></a></li>',
        }
        for name, item in broken.items():
            with self.subTest(name), self.assertRaises(ValueError) as caught:
                mu._ap_parse_profile_list_counts(f'<ul class="statList">{item}{good}</ul>')
            self.assertIn("Nothing was saved", str(caught.exception))

    def test_a_missing_label_span_does_not_matter(self):
        """The label comes from AP_LIST_TYPES or the URL, never the page, so a
        list whose label span is gone is still read rather than dropped."""
        html = """
        <ul class="statList">
          <li class="status1"><a href="/users/nawid3333/manga/read"><span class="slCount">4</span></a></li>
        </ul>
        """
        self.assertEqual(
            mu._ap_parse_profile_list_counts(html),
            [("manga/read", "/users/nawid3333/manga/read", "Read", 4)],
        )

    def test_an_item_with_a_zero_count_is_kept_as_an_empty_list(self):
        html = """
        <ul class="statList">
          <li class="status1">
            <a href="/users/nawid3333/manga/read"><span class="slCount">0</span><span class="slLabel">read</span></a>
          </li>
        </ul>
        """
        self.assertEqual(
            mu._ap_parse_profile_list_counts(html),
            [("manga/read", "/users/nawid3333/manga/read", "Read", 0)],
        )

    def test_empty_or_unrecognisable_pages_yield_no_lists(self):
        self.assertEqual(mu._ap_parse_profile_list_counts(""), [])
        self.assertEqual(mu._ap_parse_profile_list_counts("   "), [])
        self.assertEqual(mu._ap_parse_profile_list_counts("<p>no lists here</p>"), [])


def _ap_card(entry_id: int, title: str, href: str = "/manga/example", cls="tooltip mangaX", data_type="manga") -> str:
    return f"""
    <li data-type="{data_type}" data-id="{entry_id}" class="card">
      <a class="{cls}" href="{href}">
        <h3 class='cardName'>{title}</h3>
      </a>
    </li>
    """


class TestAnimePlanetListEntryParsing(unittest.TestCase):
    def _entries(self, *cards: str) -> list[dict]:
        return mu._ap_parse_list_entries(f'<ul class="cardDeck cardGrid">{"".join(cards)}</ul>')

    def test_entries_are_shaped_like_mangaupdates_items(self):
        """The whole point of the shape: every helper built for option 1 --
        get_series_ids, compare_exports, save_exports -- works on these
        entries unchanged."""
        entries = self._entries(_ap_card(91756, "Some Manga", "/manga/some-manga"))
        self.assertEqual(mu.get_series_ids(entries), {91756: "Some Manga"})

    def test_relative_urls_get_the_site_prefix(self):
        entries = self._entries(_ap_card(1, "Some Manga", "/manga/some-manga"))
        self.assertEqual(entries[0]["record"]["series"]["url"], "https://www.anime-planet.com/manga/some-manga")

    def test_absolute_urls_are_kept_as_they_came(self):
        entries = self._entries(_ap_card(1, "Some Manga", "https://www.anime-planet.com/manga/x"))
        self.assertEqual(entries[0]["record"]["series"]["url"], "https://www.anime-planet.com/manga/x")

    def test_the_tooltip_class_is_matched_even_without_a_pl0_class(self):
        """The live cards carry only 'tooltip manga<N>'; demanding 'pl0' too
        used to miss every link and export every URL as an empty string."""
        entries = self._entries(_ap_card(2, "Real Class", "/manga/real-class"))
        self.assertEqual(entries[0]["record"]["series"]["url"], "https://www.anime-planet.com/manga/real-class")

    def test_titles_are_unescaped_and_whitespace_collapsed_by_the_parser(self):
        entries = self._entries(_ap_card(3, "Attack &amp;   Titans", "/manga/a"))
        self.assertEqual(entries[0]["record"]["series"]["title"], "Attack &   Titans")

    def test_cards_without_a_usable_title_are_skipped(self):
        entry = '<li data-type="manga" data-id="4" class="card"><a class="tooltip" href="/manga/x"></a></li>'
        self.assertEqual(self._entries(entry), [])

    def test_cards_with_a_non_numeric_id_are_skipped(self):
        entry = (
            '<li data-type="manga" data-id="abc" class="card">'
            '<a class="tooltip" href="/manga/x"><h3 class=\'cardName\'>T</h3></a></li>'
        )
        self.assertEqual(self._entries(entry), [])

    def test_cards_are_read_from_every_carddeck_on_the_page(self):
        html = f"""
        <ul class="cardDeck cardGrid">{_ap_card(1, "A", "/manga/a")}</ul>
        <ul class="cardDeck cardGrid">{_ap_card(2, "B", "/manga/b")}</ul>
        """
        self.assertEqual(len(mu._ap_parse_list_entries(html)), 2)

    def test_cards_without_data_attributes_are_skipped(self):
        entry = '<li class="card"><a class="tooltip" href="/manga/x"><h3 class=\'cardName\'>T</h3></a></li>'
        self.assertEqual(self._entries(entry), [])

    def test_a_page_without_cards_yields_no_entries(self):
        self.assertEqual(mu._ap_parse_list_entries("<p>nothing here</p>"), [])
        self.assertEqual(mu._ap_parse_list_entries(""), [])

    def test_manga_and_anime_cards_sharing_a_raw_id_do_not_collide(self):
        """Anime-Planet numbers manga and anime from separate sequences --
        confirmed live: manga "Berserk" is id 14, anime "Berserk" is id 61.
        Without disambiguation, an unrelated manga and anime that happen to
        share a raw id would merge into one entry everywhere ids are used as
        dict keys (get_series_ids, compare_exports' movement scan), reading
        as a title that impossibly "moved" from a manga list to an anime
        one."""
        manga_entries = self._entries(_ap_card(500, "Some Manga", "/manga/x", data_type="manga"))
        anime_entries = self._entries(_ap_card(500, "Some Anime", "/anime/y", data_type="anime"))

        manga_id = manga_entries[0]["record"]["series"]["id"]
        anime_id = anime_entries[0]["record"]["series"]["id"]

        self.assertNotEqual(manga_id, anime_id, "same raw id from the two content types must not collide")
        self.assertEqual(manga_id, 500, "a manga id must still export as the real site id")

    def test_anime_card_url_and_title_are_unaffected_by_the_id_offset(self):
        entries = self._entries(_ap_card(7, "Some Anime", "/anime/some-anime", data_type="anime"))
        series = entries[0]["record"]["series"]
        self.assertEqual(series["title"], "Some Anime")
        self.assertEqual(series["url"], "https://www.anime-planet.com/anime/some-anime")


class _FakeApApiResponse:
    """Stands in for httpx.Response with a .text body and status checking."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"error {self.status_code}",
                request=httpx.Request("GET", "https://www.anime-planet.com/"),
                response=httpx.Response(self.status_code),
            )
        return self


class _ApFakeClient:
    """Stands in for _AnimePlanetClient, scripted per (list_type, page).

    `spec` maps list_type -> {page: html}. A page missing from the spec
    returns a 404, exercising the raise_for_status path.
    """

    def __init__(self, spec: dict[str, dict[int, str]]) -> None:
        self.spec = spec
        self.calls: list[tuple[str, int]] = []

    def get(self, path, params=None):  # noqa: ARG002
        parts = path.split("/")
        # /users/<name>/<list_type...>
        list_type = "/".join(parts[3:])
        page = (params or {}).get("page", 1)
        self.calls.append((list_type, page))
        pages = self.spec.get(list_type, {})
        status = 200 if page in pages else 404
        return _FakeApApiResponse(status, pages.get(page, ""))

    def close(self) -> None:
        pass


def _ap_page(*cards: str) -> str:
    return f'<ul class="cardDeck cardGrid">{"".join(cards)}</ul>'


def _ap_cards(first_id: int, count: int, data_type: str = "manga") -> list[str]:
    return [
        _ap_card(i, f"Title {i}", f"/{data_type}/t{i}", data_type=data_type) for i in range(first_id, first_id + count)
    ]


def _small_ap_pages():
    """Let pages of a card or two stand for a real page in the paging tests.

    _ap_fetch_all_list_entries only trusts page 1's length as the site's page
    size when it is at least the site's own default (35); below that it reads
    as a broken page. Lowering that floor keeps the paging tests small.
    """
    return patch.object(mu, "_AP_SITE_PER_PAGE", 1)


def _as_ap_client(fake) -> mu._AnimePlanetClient:
    """Hand a scripted fake to code annotated with the real client class."""
    return cast(mu._AnimePlanetClient, fake)


class TestAnimePlanetFetchAllListEntries(unittest.TestCase):
    def test_all_entries_of_a_single_page_list_come_back_in_card_order(self):
        html = f'<ul class="cardDeck cardGrid">{_ap_card(3, "C", "/manga/c")}{_ap_card(1, "A", "/manga/a")}</ul>'
        client = _ApFakeClient({"manga/reading": {1: html}})

        entries = mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 2)

        self.assertEqual(mu.get_series_ids(entries), {3: "C", 1: "A"})

    def test_pages_are_joined_in_page_order_not_completion_order(self):
        """The export's item order used to depend on which request finished
        first, so a large list's diff read as every item moved. Pages must
        be concatenated 1..N whatever the completion order."""
        spec = {
            "manga/reading": {
                1: f'<ul class="cardDeck cardGrid">{_ap_card(1, "P1A")}{_ap_card(2, "P1B")}</ul>',
                2: f'<ul class="cardDeck cardGrid">{_ap_card(3, "P2A")}{_ap_card(4, "P2B")}</ul>',
            }
        }
        client = _ApFakeClient(spec)

        # Four reported items at two per page span two pages.
        with _small_ap_pages():
            entries = mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 4)

        self.assertEqual([e["record"]["series"]["id"] for e in entries], [1, 2, 3, 4])

    def test_every_page_is_fetched_when_the_count_spans_pages(self):
        spec = {
            "manga/reading": {
                1: _ap_page(*_ap_cards(1, 2)),
                2: _ap_page(*_ap_cards(3, 2)),
                3: _ap_page(*_ap_cards(5, 1)),
            }
        }
        client = _ApFakeClient(spec)

        with _small_ap_pages():
            entries = mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 5)

        self.assertEqual(len(entries), 5)
        self.assertEqual(sorted(client.calls), [("manga/reading", 1), ("manga/reading", 2), ("manga/reading", 3)])

    def test_pages_are_sized_from_what_the_site_actually_serves(self):
        """The page count used to come from the 560 asked for. A site serving
        its own default of 35 left everything past page 1 out, and the short
        list was saved -- the next diff reported the rest as removed."""
        cards = _ap_cards(1, 80)
        spec = {"manga/reading": {1: _ap_page(*cards[:35]), 2: _ap_page(*cards[35:70]), 3: _ap_page(*cards[70:])}}
        client = _ApFakeClient(spec)

        entries = mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 80)

        self.assertEqual(len(entries), 80)
        self.assertEqual(sorted(page for _lt, page in client.calls), [1, 2, 3])

    def test_a_short_list_is_never_returned_as_complete(self):
        """The profile says 3, the page holds 1: this used to log a warning
        and save the short list, and the next run reported two removals."""
        client = _ApFakeClient({"manga/reading": {1: _ap_page(*_ap_cards(1, 1))}})

        with self.assertRaises(ValueError) as caught:
            mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 3, "Reading")

        message = str(caught.exception)
        self.assertIn("incomplete", message.lower())
        self.assertIn("'Reading'", message)
        self.assertIn("1 of 3", message)

    def test_a_page_that_parses_to_nothing_is_not_paged_through(self):
        """A bot-check or private-list page answers 200 with no cards. It is
        not a page size to plan from, so nothing more is fetched before the
        run stops."""
        client = _ApFakeClient({"manga/reading": {1: "<html><body>Just a moment...</body></html>"}})

        with self.assertRaises(ValueError):
            mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 1201)

        self.assertEqual(client.calls, [("manga/reading", 1)])

    def test_a_repeated_page_does_not_count_twice(self):
        """A page past the real end can come back as a copy of page 1; its
        cards must not make a short list add up to the profile's count."""
        page = _ap_page(*_ap_cards(1, 2))
        client = _ApFakeClient({"manga/reading": {1: page, 2: page}})

        with _small_ap_pages(), self.assertRaises(ValueError) as caught:
            mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 4)

        self.assertIn("2 of 4", str(caught.exception))

    def test_pages_are_fetched_in_parallel_not_one_after_another(self):
        """The two-page fetch must actually use its worker pool; with the
        pages scripted to sleep briefly, a serial fetch takes 2x the sleep
        and this assertion (Option 1's overlap probe, reused) catches it."""
        import time as _time

        intervals = []
        _lock = threading.Lock()

        class TimedPage(_ApFakeClient):
            def get(self, path, params=None):  # noqa: ARG002
                start = _time.perf_counter()
                _time.sleep(0.05)
                with _lock:
                    intervals.append((start, _time.perf_counter()))
                return super().get(path, params)

        # Page 1 goes alone -- its length is the page size -- so the overlap
        # to look for is among the pages after it.
        spec = {"manga/reading": {page: _ap_page(*_ap_cards(page, 1)) for page in (1, 2, 3)}}

        with patch.object(mu, "LIST_PAGE_WORKERS", 4), _small_ap_pages():
            mu._ap_fetch_all_list_entries(_as_ap_client(TimedPage(spec)), "u", "manga/reading", 3)

        self.assertGreaterEqual(_peak_overlap(intervals), 2, "pages 2 and 3 were fetched one after another")

    def test_a_failing_page_aborts_rather_than_exporting_a_short_list(self):
        class BrokenPage(_ApFakeClient):
            def get(self, path, params=None):  # noqa: ARG002
                resp = super().get(path, params)
                resp.raise_for_status()
                return resp

        client = BrokenPage({"manga/reading": {1: _ap_page(*_ap_cards(1, 2))}})  # pages 2 and 3 -> 404

        with _small_ap_pages(), self.assertRaises(httpx.HTTPStatusError):
            mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 6)

    def test_zero_and_negative_counts_fetch_nothing(self):
        client = _ApFakeClient({})
        self.assertEqual(mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", 0), [])
        self.assertEqual(mu._ap_fetch_all_list_entries(_as_ap_client(client), "u", "manga/reading", -5), [])
        self.assertEqual(client.calls, [])


class TestAnimePlanetExportAllLists(unittest.TestCase):
    def test_manga_and_anime_stalled_lists_do_not_overwrite_each_other(self):
        """manga/stalled and anime/stalled used to share the label "Stalled",
        so exports[label] = items let whichever list was read second silently
        overwrite the first with no warning -- a live data-loss bug, not a
        hypothetical one. AP_LIST_TYPES now gives them distinct labels; this
        pins that down end to end through _ap_export_all_lists rather than
        just at the label-mapping layer."""
        profile_html = """
        <ul class="statList">
          <li class="status1">
            <a href="/users/u/manga/stalled"><span class="slCount">1</span><span class="slLabel">stalled</span></a>
          </li>
        </ul>
        <ul class="statList">
          <li class="status1">
            <a href="/users/u/anime/stalled"><span class="slCount">1</span><span class="slLabel">stalled</span></a>
          </li>
        </ul>
        """
        spec = {
            "": {1: profile_html},
            "manga/stalled": {1: f'<ul class="cardDeck cardGrid">{_ap_card(1, "Manga Series")}</ul>'},
            "anime/stalled": {
                1: f'<ul class="cardDeck cardGrid">{_ap_card(2, "Anime Series", "/anime/x", data_type="anime")}</ul>'
            },
        }
        client = _ApFakeClient(spec)

        exports = mu._ap_export_all_lists(_as_ap_client(client), "u")

        self.assertEqual(set(exports.keys()), {"Manga Stalled", "Anime Stalled"})
        self.assertEqual(mu.get_series_ids(exports["Manga Stalled"]), {1: "Manga Series"})
        self.assertEqual(mu.get_series_ids(exports["Anime Stalled"]), {2 + mu._AP_ANIME_ID_OFFSET: "Anime Series"})

    def test_a_residual_label_collision_is_kept_apart_not_overwritten(self):
        """Belt-and-suspenders for the same failure mode: even if two
        list_types ever again produced the same label (a future site change,
        or two unrecognised URLs deriving the same text), the dedup guard
        must keep both rather than silently dropping one -- the same
        protection export_all_lists already has for MangaUpdates' own
        duplicate-title case."""
        profile_html = """
        <ul class="statList">
          <li class="status1">
            <a href="/users/u/manga/one"><span class="slCount">1</span><span class="slLabel">dup</span></a>
          </li>
          <li class="status2">
            <a href="/users/u/manga/two"><span class="slCount">1</span><span class="slLabel">dup</span></a>
          </li>
        </ul>
        """
        spec = {
            "": {1: profile_html},
            "manga/one": {1: f'<ul class="cardDeck cardGrid">{_ap_card(1, "First")}</ul>'},
            "manga/two": {1: f'<ul class="cardDeck cardGrid">{_ap_card(2, "Second")}</ul>'},
        }
        client = _ApFakeClient(spec)

        exports = mu._ap_export_all_lists(_as_ap_client(client), "u")

        self.assertEqual(len(exports), 2, f"a duplicate label silently overwrote a list: {exports.keys()}")
        all_ids = {sid for items in exports.values() for sid in mu.get_series_ids(items)}
        self.assertEqual(all_ids, {1, 2})

    def test_ap_list_types_has_no_duplicate_labels(self):
        """If this ever fails, a new collision has crept into the label map --
        the exact failure mode manga/stalled vs anime/stalled hit before they
        were named apart. New entries should get their own distinct label
        rather than relying on the dedup guard to paper over it."""
        labels = list(mu.AP_LIST_TYPES.values())
        self.assertEqual(len(labels), len(set(labels)), f"duplicate labels in AP_LIST_TYPES: {labels}")


# Option 4 is handed the MangaUpdates client like every option, and never
# touches it.
_NO_MU_CLIENT = cast(mu._ClientLike, None)


def _ap_profile(**counts: int) -> str:
    """A profile statList with one entry per list type, e.g. manga_reading=2."""
    items = "".join(
        f'<li class="status"><a href="/users/u/{key.replace("_", "/", 1)}">'
        f'<span class="slCount">{count}</span><span class="slLabel">x</span></a></li>'
        for key, count in counts.items()
    )
    return f'<ul class="statList">{items}</ul>'


class TestAnimePlanetScan(TempExportsCase):
    """Option 4 end to end, against a scripted Anime-Planet and a temp tree."""

    def setUp(self) -> None:
        super().setUp()
        self.ap_dir = os.path.join(self.dir.name, "anime-planet")
        for name, value in (("AP_EXPORTS_DIR", self.ap_dir), ("AP_USERNAME", "u")):
            patcher = patch.object(mu, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _scan(self, spec):
        with patch.object(mu, "_AnimePlanetClient", lambda: _ApFakeClient(spec)), _LogCapture() as cap:
            mu.run_anime_planet_scan(_NO_MU_CLIENT)
        return mu._strip_ansi(cap.text)

    def _folders(self):
        return sorted(d for d in os.listdir(self.ap_dir) if mu._is_export_folder(d))

    def test_a_short_list_stops_the_run_and_keeps_the_previous_export(self):
        """Reproduced before this fix: the profile said 3, the list page held
        1, and the run saved it with a warning -- then reported "Removed: B"
        and "Removed: C" for titles still on the account."""
        self._scan({"": {1: _ap_profile(manga_reading=3)}, "manga/reading": {1: _ap_page(*_ap_cards(1, 3))}})
        before = self._folders()
        with open(os.path.join(self.ap_dir, before[0], "Reading.json"), encoding="utf-8") as f:
            saved = f.read()

        with self.assertRaises(ValueError) as caught:
            self._scan({"": {1: _ap_profile(manga_reading=3)}, "manga/reading": {1: _ap_page(*_ap_cards(1, 1))}})

        self.assertIn("1 of 3", str(caught.exception))
        self.assertEqual(self._folders(), before, "a short export was saved")
        with open(os.path.join(self.ap_dir, before[0], "Reading.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), saved, "the previous export was touched")

    def test_a_list_emptied_since_the_last_run_reads_as_a_move(self):
        """Reproduced before this fix: Alpha finished, Reading went to 0, and
        the diff said "[Read] + Added: Alpha" and "[Reading] LIST REMOVED (no
        longer exists)" -- the list exists, and nothing was added."""
        self._scan(
            {
                "": {1: _ap_profile(manga_reading=1, manga_read=1)},
                "manga/reading": {1: _ap_page(_ap_card(1, "Alpha"))},
                "manga/read": {1: _ap_page(_ap_card(2, "Beta"))},
            }
        )
        text = self._scan(
            {
                "": {1: _ap_profile(manga_reading=0, manga_read=2)},
                "manga/read": {1: _ap_page(_ap_card(1, "Alpha"), _ap_card(2, "Beta"))},
            }
        )

        self.assertIn("Moved series", text)
        self.assertRegex(text, r"Alpha\s+Reading → Read")
        self.assertNotIn("Added:", text)
        self.assertNotIn("LIST REMOVED", text)

    def test_a_profile_with_every_list_at_zero_saves_nothing(self):
        """All zeros is far likelier a page that did not show the counts than
        an account emptied overnight; saving it would report every title as
        removed."""
        text = self._scan({"": {1: _ap_profile(manga_reading=0, manga_read=0)}})
        self.assertIn("No non-empty Anime-Planet lists", text)
        self.assertFalse(os.path.isdir(self.ap_dir) and self._folders())

    def test_an_unknown_user_is_named_not_reported_as_unreachable(self):
        with self.assertRaises(ValueError) as caught:
            self._scan({})  # the profile answers 404
        self.assertIn("AP_USERNAME", str(caught.exception))

    def test_a_refused_request_is_not_called_unreachable(self):
        class Forbidden(_ApFakeClient):
            def get(self, path, params=None):  # noqa: ARG002
                return _FakeApApiResponse(403, "")

        with patch.object(mu, "_AnimePlanetClient", lambda: Forbidden({})), _LogCapture() as cap:
            mu.run_anime_planet_scan(_NO_MU_CLIENT)
        self.assertIn("answered HTTP 403", cap.text)
        self.assertNotIn("Could not reach", cap.text)


class TestAnimePlanetClientRetries(unittest.TestCase):
    """Anime-Planet requests went out bare: one 429, or one dropped
    connection among up to LIST_PAGE_WORKERS pages in flight, ended option 4."""

    def _client(self, handler) -> mu._AnimePlanetClient:
        ap = mu._AnimePlanetClient()
        ap.client.close()
        ap.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        self.addCleanup(ap.close)
        return ap

    def test_a_rate_limited_page_is_retried(self):
        answers = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, text="ok")]
        sent = []

        def handler(request):
            sent.append(request.url.path)
            return answers.pop(0)

        with patch.object(mu, "time"):
            resp = self._client(handler).get("/users/u/manga/read")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sent, ["/users/u/manga/read", "/users/u/manga/read"])

    def test_a_dropped_connection_is_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(200, text="ok")

        with patch.object(mu, "time"):
            self.assertEqual(self._client(handler).get("/users/u").status_code, 200)
        self.assertEqual(len(calls), 2)


class TestAnimePlanetPagesNeeded(unittest.TestCase):
    def test_counts_at_or_below_one_page_need_one_page(self):
        self.assertEqual(mu._ap_pages_needed(0, 560), 0)
        self.assertEqual(mu._ap_pages_needed(1, 560), 1)
        self.assertEqual(mu._ap_pages_needed(560, 560), 1)

    def test_one_item_over_a_page_boundary_adds_exactly_one_page(self):
        self.assertEqual(mu._ap_pages_needed(561, 560), 2)

    def test_a_non_positive_count_needs_no_pages(self):
        self.assertEqual(mu._ap_pages_needed(-1, 560), 0)


# ==================== related-series discovery ====================
class _FakeApiResponse(httpx.Response):
    def __init__(self, status_code, body):
        super().__init__(status_code, content=b"", request=httpx.Request("GET", "https://example.com/"))
        self._body = body
        self.headers = {}

    def json(self, **kwargs):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=self.request, response=self)
        return self


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

    def post(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def put(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})


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

        related = mu.collect_related_series(client, exports).found

        self.assertIn(49512032547, related, "One Piece Party is new and must be found")
        self.assertEqual(related[49512032547]["title"], "One Piece Party")
        self.assertEqual(related[49512032547]["sources"], [("One Piece", "Spin-Off")])
        self.assertNotIn(14853239448, related, "Wanted! is already tracked and must be excluded")

    def test_a_404d_series_is_skipped_not_fatal(self):
        exports = {"Reading": [_rec(1, "One Piece"), _rec(3, "Deleted Series")]}
        client = _FakeSeriesClient({1: [], 3: "404"})

        related = mu.collect_related_series(client, exports).found

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

        related = mu.collect_related_series(client, exports).found

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

        related = mu.collect_related_series(client, exports).found

        self.assertEqual(len(related), 1, "must be deduplicated into a single entry")
        self.assertEqual(set(related[99]["sources"]), {("Series A", "Prequel"), ("Series B", "Sequel")})

    def test_malformed_relation_entries_are_skipped(self):
        exports = {"Reading": [_rec(1, "One Piece")]}
        client = _FakeSeriesClient(
            {1: [{"relation_id": 1, "relation_type": "Spin-Off"}]}  # missing id and name
        )

        related = mu.collect_related_series(client, exports).found

        self.assertEqual(related, {})

    def test_an_unexpectedly_shaped_response_for_one_series_does_not_crash_the_batch(self):
        """A response body that's valid JSON but not an object (e.g. a bare
        array) used to raise AttributeError from body.get(...), uncaught,
        which crashed every OTHER series' result along with it since
        future.result() re-raises on the aggregating thread. Reproduced live
        before this fix, including under real concurrent thread-pool load."""

        class ShapeShiftingClient:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                sid = int(url.rsplit("/", 1)[-1])
                self.calls.append(sid)
                if sid == 999:
                    return _FakeApiResponse(200, ["unexpected", "array", "body"])
                return _FakeApiResponse(200, {"related_series": []})

            def post(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(200, {})

            def put(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(200, {})

        exports = {
            "Reading": [
                _rec(1, "Good Series 1"),
                _rec(999, "Bad Shape Series"),
                _rec(2, "Good Series 2"),
            ]
        }
        client = ShapeShiftingClient()

        related = mu.collect_related_series(client, exports).found  # must not raise

        self.assertEqual(related, {})
        self.assertEqual(sorted(client.calls), [1, 2, 999], "the bad-shape series must not abort the others")

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

            def post(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(200, {})

            def put(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(200, {})

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

    def post(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})

    def put(self, url, **kwargs):  # noqa: ARG002
        return _FakeApiResponse(200, {})


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

        finished = mu.find_finished_wishlist_series(client, wish_items).found

        self.assertEqual(set(finished), {1})
        self.assertEqual(finished[1]["title"], "Finished Manga")
        self.assertEqual(finished[1]["url"], "https://x/1")

    def test_cancelled_series_counts_as_finished_too(self):
        """Nothing more is coming for a cancelled series either -- MangaUpdates
        itself marks these `completed: true`, verified live (e.g. a real
        Complete/Discontinued series)."""
        wish_items = [_rec_url(1, "Cancelled Manga")]
        client = _FakeStatusClient({1: {"completed": True, "status": "4 Volumes (Complete/Discontinued)"}})

        finished = mu.find_finished_wishlist_series(client, wish_items).found

        self.assertEqual(set(finished), {1})

    def test_an_unexpectedly_shaped_response_for_one_series_does_not_crash_the_batch(self):
        """Same regression as collect_related_series's equivalent test: a
        non-dict body for one series must not take the rest down with it."""

        class ShapeShiftingClient:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                sid = int(url.rsplit("/", 1)[-1])
                self.calls.append(sid)
                if sid == 999:
                    return _FakeApiResponse(200, ["unexpected", "array", "body"])
                return _FakeApiResponse(200, {"completed": True, "status": "(Complete)"})

            def post(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(200, {})

            def put(self, url, **kwargs):  # noqa: ARG002
                return _FakeApiResponse(200, {})

        wish_items = [_rec_url(1, "Good 1"), _rec_url(999, "Bad Shape"), _rec_url(2, "Good 2")]
        client = ShapeShiftingClient()

        finished = mu.find_finished_wishlist_series(client, wish_items).found  # must not raise

        self.assertEqual(set(finished), {1, 2})
        self.assertEqual(sorted(client.calls), [1, 2, 999])

    def test_mixed_format_series_is_not_falsely_flagged(self):
        """Regression guard for the exact trap a naive '"Complete" in status'
        text search would fall into: one format done, another still active."""
        wish_items = [_rec_url(1, "Mixed Format Manga")]
        client = _FakeStatusClient(
            {1: {"completed": False, "status": "27 Volumes (Complete)\n\n42 Chapters (Webtoon, Ongoing)"}}
        )

        finished = mu.find_finished_wishlist_series(client, wish_items).found

        self.assertEqual(finished, {})

    def test_a_404d_series_is_skipped_not_fatal(self):
        wish_items = [_rec_url(1, "Deleted Series")]
        client = _FakeStatusClient({1: "404"})

        finished = mu.find_finished_wishlist_series(client, wish_items).found

        self.assertEqual(finished, {})
        self.assertEqual(client.calls, [1], "the 404 must not abort the whole pass")

    def test_empty_wishlist_produces_no_lookups(self):
        finished = mu.find_finished_wishlist_series(_FakeStatusClient({}), []).found
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
        self.assertIn("0 series finished out of 5 checked", content)
        self.assertIn("finished releasing yet", content.lower())

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
        self.assertIn("1 series finished out of 7 checked", content)
        self.assertIn("[1/1]", content)


# ==================== lookups that came back with no answer ====================
class _Script:
    """Hand out *answers* one per prompt and record every prompt shown.

    Running out is a failure, not an end of input: a prompt that asks more
    often than the test expects is exactly what these tests are here to see.
    An answer that is an exception instance is raised instead (EOFError).
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt=""):
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"asked again after the scripted answers ran out: {prompt!r}")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class TestFailedLookupsAreNotHidden(TempExportsCase):
    """Reproduced before this fix: with every /series lookup rate-limited,
    ready_to_read.txt said "0 series finished out of 3 checked -- No Wish
    List series have finished releasing yet" and related.txt said "every
    related series found is already in one of your lists", each replacing a
    good report from an earlier run."""

    GOOD = "a good report from an earlier run\n"

    def _previous(self, name):
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.GOOD)
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _wish_list(self, count=3):
        return _MenuApi([{"list_id": 1, "title": "Wish List"}], {1: [_rec(i, f"S{i}") for i in range(1, count + 1)]})

    def _finished_check(self, status, *answers):
        """Run option 3 with `status(series_id)` standing in for each lookup."""
        script = _Script(*answers)
        with (
            patch.object(mu, "fetch_series_status", lambda _client, sid: status(sid)),
            patch("builtins.input", script),
            contextlib.redirect_stdout(io.StringIO()) as out,
            _LogCapture() as cap,
        ):
            mu.run_finished_check(self._wish_list())
        return script, mu._strip_ansi(cap.text + out.getvalue())

    @staticmethod
    def _second_fails(sid):
        return None if sid == 2 else {"completed": sid == 1, "status": "Complete"}

    def test_failed_lookups_are_counted_not_dropped(self):
        # 2 no longer exists; 3 answers with a body that is not an object.
        client = _FakeStatusClient({1: {"completed": True, "status": "(Complete)"}, 2: "404", 3: ["junk"]})
        lookups = mu.find_finished_wishlist_series(client, [_rec(1, "Done"), _rec(2, "Gone"), _rec(3, "Failed")])
        self.assertEqual(set(lookups.found), {1})
        self.assertEqual((lookups.total, lookups.unchecked), (3, 2), "a 404 and a failure are both unchecked")

    def test_related_lookups_count_failures_too(self):
        client = _FakeSeriesClient({1: [], 2: "404"})
        lookups = mu.collect_related_series(client, {"Reading": [_rec(1, "A"), _rec(2, "B")]})
        self.assertEqual((lookups.total, lookups.unchecked), (2, 1))

    def test_the_reports_name_what_could_not_be_checked(self):
        related = self._read(mu.save_related_series({}, unchecked=2))
        self.assertIn("2 series could not be checked", related)
        self.assertNotIn("already in one of your lists", related, "that claim needs every series checked")

        finished = self._read(mu.save_finished_series({}, 1, unchecked=2))
        self.assertIn("0 series finished out of 1 checked", finished)
        self.assertIn("2 series could not be checked", finished)

    def test_every_lookup_failing_keeps_the_previous_report_without_asking(self):
        path = self._previous("related.txt")
        client = _MenuApi([{"list_id": 1, "title": "Reading"}], {1: [_rec(1, "A"), _rec(2, "B")]})
        with (
            patch.object(mu, "fetch_series_related", return_value=None),
            patch("builtins.input", _Script()),
            _LogCapture() as cap,
        ):
            mu.run_related_check(client)
        self.assertEqual(self._read(path), self.GOOD)
        self.assertIn("2 of 2 series could not be checked", cap.text)
        self.assertIn("keeping the previous report", cap.text)

    def test_some_lookups_failing_asks_and_n_keeps_the_previous_report(self):
        path = self._previous("ready_to_read.txt")
        script, text = self._finished_check(self._second_fails, "n")
        self.assertEqual(len(script.asked), 1)
        self.assertEqual(self._read(path), self.GOOD)
        self.assertIn("1 of 3 series could not be checked", text)

    def test_y_replaces_it_with_the_incomplete_report(self):
        path = self._previous("ready_to_read.txt")
        self._finished_check(self._second_fails, "Y")
        report = self._read(path)
        self.assertIn("1 series finished out of 2 checked", report)
        self.assertIn("1 series could not be checked", report)

    def test_only_y_or_n_answer_and_end_of_input_keeps_the_previous_report(self):
        path = self._previous("ready_to_read.txt")
        script, text = self._finished_check(self._second_fails, "", "yes", "j", EOFError())
        self.assertEqual(len(script.asked), 4, "Enter and other answers must be asked again")
        self.assertIn("type y or n", text)
        self.assertEqual(self._read(path), self.GOOD)

    def test_a_run_of_unusable_answers_keeps_the_previous_report(self):
        path = self._previous("ready_to_read.txt")
        script, _text = self._finished_check(self._second_fails, *(["x"] * mu.term.MAX_UNRECOGNIZED))
        self.assertEqual(len(script.asked), mu.term.MAX_UNRECOGNIZED)
        self.assertEqual(self._read(path), self.GOOD)

    def test_with_no_previous_report_the_incomplete_one_is_written_without_asking(self):
        script, _text = self._finished_check(self._second_fails)
        self.assertEqual(script.asked, [])
        self.assertIn("1 series could not be checked", self._read(os.path.join(self.dir.name, "ready_to_read.txt")))

    def test_a_complete_run_still_replaces_the_report_without_asking(self):
        path = self._previous("ready_to_read.txt")
        script, _text = self._finished_check(lambda sid: {"completed": sid == 1, "status": "Complete"})
        self.assertEqual(script.asked, [])
        self.assertIn("1 series finished out of 3 checked", self._read(path))


if __name__ == "__main__":
    unittest.main()
