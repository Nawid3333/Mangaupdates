"""Tests for the finished-Wish-List report's layout helpers.

The report is drawn as a fixed-width box, so every line has to be measured in
terminal columns rather than code points: an emoji is one code point and two
columns, and an ANSI colour code is several code points and zero. A helper
that gets that wrong does not crash, it just pushes the box's right edge out
of line, which is why these are pinned by width rather than by eyeball.

Run with:  python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("MU_USERNAME", "test")
os.environ.setdefault("MU_PASSWORD", "test")

import main as mu  # noqa: E402


class TestPadFinishedLine(unittest.TestCase):
    def test_a_short_line_is_padded_to_the_report_width(self):
        padded = mu._pad_finished_line("abc")
        self.assertEqual(mu._display_width(padded), mu.FINISHED_REPORT_WIDTH)

    def test_an_exact_width_line_is_left_alone(self):
        text = "x" * mu.FINISHED_REPORT_WIDTH
        self.assertEqual(mu._pad_finished_line(text), text)

    def test_an_overlong_line_is_never_truncated(self):
        """Losing characters silently would be worse than a ragged edge."""
        text = "x" * (mu.FINISHED_REPORT_WIDTH + 5)
        self.assertEqual(mu._pad_finished_line(text), text)

    def test_padding_measures_columns_not_code_points(self):
        padded = mu._pad_finished_line("⚠️ warning")
        self.assertEqual(mu._display_width(padded), mu.FINISHED_REPORT_WIDTH)

    def test_ansi_codes_do_not_eat_into_the_padding(self):
        padded = mu._pad_finished_line(mu._style("hello", mu._T.BOLD))
        self.assertEqual(mu._display_width(padded), mu.FINISHED_REPORT_WIDTH)


class TestWrapFinishedLine(unittest.TestCase):
    def _widths(self, lines):
        return [mu._display_width(mu._strip_ansi(line)) for line in lines]

    def test_a_line_that_already_fits_is_returned_unchanged(self):
        self.assertEqual(mu._wrap_finished_line("  Status: Complete", 40), ["  Status: Complete"])

    def test_a_line_exactly_at_the_limit_is_not_wrapped(self):
        text = "x" * 20
        self.assertEqual(mu._wrap_finished_line(text, 20), [text])

    def test_a_long_line_is_split_across_several_lines(self):
        text = "  Status: " + " ".join(["word"] * 30)
        lines = mu._wrap_finished_line(text, 40)
        self.assertGreater(len(lines), 1)

    def test_no_produced_line_exceeds_the_width(self):
        text = "  Status: " + " ".join(["word"] * 30)
        for width in self._widths(mu._wrap_finished_line(text, 40)):
            self.assertLessEqual(width, 40)

    def test_no_word_is_lost_in_the_wrapping(self):
        words = [f"w{n}" for n in range(40)]
        lines = mu._wrap_finished_line("  " + " ".join(words), 30)
        self.assertEqual(" ".join(line.strip() for line in lines).split(), words)

    def test_continuation_lines_keep_the_first_line_s_indent(self):
        text = "    " + " ".join(["word"] * 30)
        lines = mu._wrap_finished_line(text, 40)
        self.assertGreater(len(lines), 1)
        for line in lines[1:]:
            self.assertTrue(line.startswith("    "), f"lost the indent: {line!r}")

    def test_an_unindented_line_gets_no_indent_on_continuation(self):
        lines = mu._wrap_finished_line(" ".join(["word"] * 30), 40)
        self.assertGreater(len(lines), 1)
        self.assertFalse(lines[1].startswith(" "))

    def test_a_single_token_wider_than_the_line_is_hard_broken(self):
        """Typically a URL: it must not sit alone on an overflowing line."""
        url = "https://www.mangaupdates.com/series/" + "a" * 120
        lines = mu._wrap_finished_line(f"  Link: {url}", 40)
        self.assertGreater(len(lines), 1)
        for width in self._widths(lines):
            self.assertLessEqual(width, 40)

    def test_a_hard_broken_token_loses_no_characters(self):
        token = "b" * 100
        lines = mu._wrap_finished_line(token, 30)
        self.assertEqual("".join(line.strip() for line in lines), token)

    def test_ansi_codes_do_not_count_toward_the_width(self):
        """A coloured line must wrap at the same point as a plain one."""
        words = " ".join(["word"] * 20)
        plain = mu._wrap_finished_line(words, 40)
        coloured = mu._wrap_finished_line(mu._style(words, mu._T.GREEN), 40)
        self.assertEqual(len(coloured), len(plain))

    def test_wide_characters_count_as_two_columns(self):
        text = "見" * 40
        for width in self._widths(mu._wrap_finished_line(text, 20)):
            self.assertLessEqual(width, 20)

    def test_an_indent_that_fills_the_line_still_makes_progress(self):
        """The forced-one-character branch; without it this would not terminate."""
        lines = mu._wrap_finished_line(" " * 10 + "abcdef", 10)
        self.assertTrue(lines)
        self.assertEqual("".join(line.strip() for line in lines), "abcdef")


class TestCompletionMarkerDetection(unittest.TestCase):
    """The marker regex on its own.

    It previously ended in `(?!\\))`, which required the marker *not* to be
    closed -- so the three forms MangaUpdates actually writes never matched,
    and only a malformed unclosed "(Complete" did. That went unnoticed because
    _split_finished_status' fallback gives the same answer whenever the marker
    sits in the first fragment.
    """

    def _matches(self, text):
        return bool(mu._FINISHED_STATUS_SPLIT_RE.search(text))

    def test_the_ordinary_closed_forms_are_detected(self):
        for marker in ("(Complete)", "(Completed)", "(Cancelled)", "(Discontinued)"):
            self.assertTrue(self._matches(f"12 Volumes {marker}"), marker)

    def test_detection_is_case_insensitive(self):
        self.assertTrue(self._matches("12 Volumes (complete)"))

    def test_a_doubled_parenthesis_is_not_a_marker(self):
        """The (?<!\\() guard: "((Complete)" is malformed, not a status."""
        self.assertFalse(self._matches("((Complete)"))

    def test_a_word_merely_containing_a_marker_is_not_one(self):
        self.assertFalse(self._matches("(Incomplete)"))

    def test_a_bare_word_without_parentheses_is_not_a_marker(self):
        self.assertFalse(self._matches("Complete"))

    def test_an_unrelated_status_is_not_a_marker(self):
        self.assertFalse(self._matches("12 Volumes (Ongoing)"))


class TestSplitFinishedStatus(unittest.TestCase):
    def test_an_empty_status_yields_nothing(self):
        self.assertEqual(mu._split_finished_status(""), ("", []))

    def test_a_status_of_only_separators_yields_nothing(self):
        self.assertEqual(mu._split_finished_status("  /  /  "), ("", []))

    def test_a_single_fragment_is_the_headline(self):
        self.assertEqual(mu._split_finished_status("Complete"), ("Complete", []))

    def test_a_completion_marker_in_the_first_fragment_splits_off_the_rest(self):
        headline, details = mu._split_finished_status("12 Volumes (Complete) / S1: 6 Volumes / S2: 6 Volumes")
        self.assertEqual(headline, "12 Volumes (Complete)")
        self.assertEqual(details, ["S1: 6 Volumes", "S2: 6 Volumes"])

    def test_the_marker_match_is_case_insensitive(self):
        headline, details = mu._split_finished_status("12 Volumes (complete) / S1: 6 Volumes")
        self.assertEqual(headline, "12 Volumes (complete)")
        self.assertEqual(details, ["S1: 6 Volumes"])

    def test_cancelled_and_discontinued_also_count_as_markers(self):
        for marker in ("Cancelled", "Discontinued"):
            headline, details = mu._split_finished_status(f"3 Volumes ({marker}) / S1: 3 Volumes")
            self.assertEqual(headline, f"3 Volumes ({marker})")
            self.assertEqual(details, ["S1: 3 Volumes"])

    def test_a_marker_only_in_a_later_fragment_keeps_the_whole_status(self):
        """Promoting an arbitrary later fragment would misreport the headline."""
        status = "S1: 6 Volumes / 12 Volumes (Complete)"
        self.assertEqual(mu._split_finished_status(status), (status, []))

    def test_no_marker_anywhere_falls_back_to_first_plus_rest(self):
        headline, details = mu._split_finished_status("Ongoing / S1: 6 Volumes / S2: 6 Volumes")
        self.assertEqual(headline, "Ongoing")
        self.assertEqual(details, ["S1: 6 Volumes", "S2: 6 Volumes"])

    def test_surrounding_whitespace_is_stripped_from_every_fragment(self):
        headline, details = mu._split_finished_status("  12 Volumes (Complete)  /   S1: 6 Volumes  ")
        self.assertEqual(headline, "12 Volumes (Complete)")
        self.assertEqual(details, ["S1: 6 Volumes"])

    def test_nothing_from_the_status_is_dropped(self):
        status = "12 Volumes (Complete) / S1: 6 Volumes / S2: 6 Volumes"
        headline, details = mu._split_finished_status(status)
        for fragment in (p.strip() for p in status.split("/")):
            self.assertTrue(
                fragment in headline or fragment in details,
                f"{fragment!r} was dropped",
            )


class TestReportBoxIntegrity(unittest.TestCase):
    """The rendered box, end to end.

    Two defects motivated these. The cards were opened with a light rule
    ("|--|") but closed and sided with double ones, so the strokes did not
    join; and the card header was the one line not passed through
    _wrap_finished_line, so a long title pushed that single row past the
    right border while the rest of the card stayed at the fixed width.
    """

    LONG_TITLE = "A Very Long Light Novel Title That Explains The Entire Premise In One Breath And Keeps Going"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = unittest.mock.patch.object(mu, "EXPORTS_DIR", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _finished(self, entries, total_checked=None):
        path = mu.save_finished_series(entries, total_checked or len(entries))
        return Path(path).read_text(encoding="utf-8")

    def _related(self, entries):
        path = mu.save_related_series(entries)
        return Path(path).read_text(encoding="utf-8")

    def _widths(self, text):
        return {mu._display_width(line) for line in text.splitlines() if line.strip()}

    def _entry(self, title, status="3 Volumes (Complete)"):
        return {"title": title, "url": "https://www.mangaupdates.com/series/x/a", "status": status}

    def _related_entry(self, title):
        return {
            "title": title,
            "url": "https://www.mangaupdates.com/series/y/b",
            "sources": [("Origin", "Sequel")],
        }

    def test_every_finished_line_is_the_same_width(self):
        text = self._finished({1: self._entry("Short")})
        self.assertEqual(self._widths(text), {mu.FINISHED_REPORT_WIDTH + 2})

    def test_a_long_title_does_not_break_the_border(self):
        text = self._finished({1: self._entry(self.LONG_TITLE)})
        self.assertEqual(self._widths(text), {mu.FINISHED_REPORT_WIDTH + 2})

    def test_a_long_title_is_still_printed_in_full(self):
        """Wrapping may split the title across rows, but must not drop any of it."""
        text = self._finished({1: self._entry(self.LONG_TITLE)})
        inner = " ".join(line.strip("║").strip() for line in text.splitlines())
        self.assertIn(" ".join(self.LONG_TITLE.split()), " ".join(inner.split()))

    def test_a_long_url_does_not_break_the_border(self):
        entry = self._entry("Short")
        entry["url"] = "https://www.mangaupdates.com/series/x/" + "a" * 200
        self.assertEqual(self._widths(self._finished({1: entry})), {mu.FINISHED_REPORT_WIDTH + 2})

    def test_a_long_status_does_not_break_the_border(self):
        seasons = " / ".join(f"S{n}: 6 Volumes" for n in range(12))
        entry = self._entry("Short", status=f"99 Volumes (Complete) / {seasons}")
        self.assertEqual(self._widths(self._finished({1: entry})), {mu.FINISHED_REPORT_WIDTH + 2})

    def test_an_empty_finished_report_keeps_its_header_box(self):
        text = self._finished({}, total_checked=5)
        self.assertIn("0 series finished out of 5 checked", text)

    def test_every_related_line_is_the_same_width(self):
        related = {1: self._related_entry("Some Series")}
        self.assertEqual(self._widths(self._related(related)), {mu.FINISHED_REPORT_WIDTH + 2})

    def test_a_long_related_title_does_not_break_the_border(self):
        related = {1: self._related_entry(self.LONG_TITLE)}
        self.assertEqual(self._widths(self._related(related)), {mu.FINISHED_REPORT_WIDTH + 2})

    def test_the_card_borders_join(self):
        """Opening, dividing and closing rules must be one box style."""
        text = self._finished({1: self._entry("Short")})
        opens = [ln for ln in text.splitlines() if ln.startswith("\u2554")]
        divides = [ln for ln in text.splitlines() if ln.startswith("\u2560")]
        closes = [ln for ln in text.splitlines() if ln.startswith("\u255a")]
        self.assertTrue(opens and divides and closes)
        for line in opens + divides + closes:
            self.assertNotIn("\u2500", line, "a light rule is mixed into a double-ruled box")

    def test_no_light_box_characters_survive_anywhere(self):
        text = self._finished({1: self._entry("Short")}) + self._related({1: self._related_entry("T")})
        for ch in ("\u250c", "\u2510", "\u2514", "\u2518", "\u251c", "\u2524", "\u2502"):
            self.assertNotIn(ch, text, f"stray light box character {ch!r}")


if __name__ == "__main__":
    unittest.main()
