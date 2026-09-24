"""The monthly live-site check, run against a fake MangaUpdates and Anime-Planet.

The check can only earn trust by being right in both directions: silent on
healthy sites, loud on each kind of change this tool depends on, and never
calling a blocked runner a broken site. Nothing here touches the network.
"""

from __future__ import annotations

import copy
import logging
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as mu  # noqa: E402
from tests import site_check  # noqa: E402
from tests.site_check import FAIL, PASS, SKIPPED, UNREACHABLE  # noqa: E402

USER = "SecretUser"
LIST_TITLE = "My Secret List"
API = "api.mangaupdates.com"
AP = "www.anime-planet.com"

SERIES = {"series_id": 123, "title": "Berserk", "related_series": [], "completed": False, "status": "42 Volumes"}
LIST_ITEM = {"record": {"series": {"id": 123, "title": "Berserk", "url": "https://www.mangaupdates.com/series/x"}}}

PROFILE = f"""<html><body><ul class="statList">
<li class="status1"><a href="/users/{USER}/manga/read">
<span class="slCount">1,204</span><span class="slLabel">Read</span></a></li>
<li class="status2"><a href="/users/{USER}/manga/reading">
<span class="slCount">0</span><span class="slLabel">Reading</span></a></li>
</ul></body></html>"""
AP_LIST = """<html><body><ul class="cardDeck cardGrid">
<li data-type="manga" data-id="14"><a class="tooltip manga14" href="/manga/berserk">
<h3 class="cardName">Berserk</h3></a></li>
</ul></body></html>"""
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"


class FakeSites:
    """MangaUpdates' API and Anime-Planet's pages, keyed by (method, host, path)."""

    def __init__(self, overrides: dict | None = None):
        self.routes: dict[tuple[str, str, str], tuple[int, object]] = {
            ("POST", API, "/v1/series/search"): (200, {"total_hits": 1, "results": [{"record": {"series_id": 123}}]}),
            ("GET", API, "/v1/series/123"): (200, SERIES),
            ("PUT", API, "/v1/account/login"): (200, {"status": "success", "context": {"session_token": "tok"}}),
            ("GET", API, "/v1/lists"): (200, [{"list_id": 0, "title": LIST_TITLE}, {"list_id": 1, "title": "Wish"}]),
            ("POST", API, "/v1/lists/0/search"): (200, {"total_hits": 1, "results": [LIST_ITEM]}),
            ("POST", API, "/v1/lists/1/search"): (200, {"total_hits": 0, "results": []}),
            ("POST", API, "/v1/account/logout"): (200, {}),
            ("GET", AP, f"/users/{USER}"): (200, PROFILE),
            ("GET", AP, f"/users/{USER}/manga/read"): (200, AP_LIST),
        }
        self.routes.update(overrides or {})
        self.requests: list[tuple[str, str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.host, request.url.path)
        self.requests.append(key)
        if key[2].startswith("/v1/lists") and request.headers.get("Authorization") != "Bearer tok":
            return httpx.Response(401, json={"status": "exception"})
        status, body = self.routes.get(key, (404, {"status": "exception", "reason": "not found"}))
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)


def run(sites: FakeSites, *, account: bool = False, anime_planet: bool = False) -> dict[str, site_check.Result]:
    transport = httpx.MockTransport(sites)
    real_client = httpx.Client

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with (
        mock.patch.object(httpx, "Client", RoutedClient),
        mock.patch.object(mu, "USERNAME", USER if account else ""),
        mock.patch.object(mu, "PASSWORD", "pw" if account else ""),
        mock.patch.object(mu, "AP_USERNAME", USER if anime_planet else ""),
        mock.patch.object(mu.time, "sleep"),
    ):
        results = site_check.run_checks()
    return {r.check: r for r in results}


def statuses(results: dict[str, site_check.Result]) -> dict[str, str]:
    return {name: r.status for name, r in results.items()}


def with_series(**changes) -> tuple[int, dict]:
    body = copy.deepcopy(SERIES)
    body.update(changes)
    return 200, body


class HealthySitesTests(unittest.TestCase):
    def test_the_public_checks_pass_and_the_account_ones_are_skipped(self):
        results = run(FakeSites())
        self.assertEqual(
            statuses(results),
            {"API search": PASS, "series details": PASS, "login": SKIPPED, "Anime-Planet profile": SKIPPED},
        )
        self.assertEqual(site_check.exit_code(list(results.values())), 0)

    def test_with_credentials_every_check_passes(self):
        results = run(FakeSites(), account=True, anime_planet=True)
        self.assertEqual(
            statuses(results),
            {
                "API search": PASS,
                "series details": PASS,
                "login": PASS,
                "list index": PASS,
                "list page": PASS,
                "Anime-Planet profile": PASS,
                "Anime-Planet list": PASS,
            },
        )

    def test_the_session_is_logged_out(self):
        sites = FakeSites()
        run(sites, account=True, anime_planet=True)
        self.assertIn(("POST", API, "/v1/account/logout"), sites.requests)

    def test_the_report_never_carries_the_username_or_list_names(self):
        results = run(FakeSites(), account=True, anime_planet=True)
        report = site_check.render(list(results.values()), date(2026, 10, 3))
        self.assertNotIn(USER, report)
        self.assertNotIn(LIST_TITLE, report)

    def test_the_tools_console_log_is_silenced(self):
        # login() logs the account name at INFO to the console handler.
        site_check.silence_console_log()
        console = [h for h in logging.getLogger("mu_export").handlers if type(h) is logging.StreamHandler]
        self.assertTrue(console)
        self.assertTrue(all(h.level > logging.CRITICAL for h in console))


class ApiChangeTests(unittest.TestCase):
    """Each change this tool depends on fails its own check."""

    def assert_only_failure(self, results, check: str) -> None:
        self.assertEqual([name for name, r in results.items() if r.status == FAIL], [check])
        self.assertEqual(site_check.exit_code(list(results.values())), 1)

    def test_a_series_without_the_completed_flag(self):
        body = copy.deepcopy(SERIES)
        del body["completed"]
        results = run(FakeSites({("GET", API, "/v1/series/123"): (200, body)}))
        self.assert_only_failure(results, "series details")
        self.assertIn("finished", results["series details"].detail)

    def test_related_series_that_became_null(self):
        results = run(FakeSites({("GET", API, "/v1/series/123"): with_series(related_series=None)}))
        self.assert_only_failure(results, "series details")

    def test_a_moved_search_endpoint(self):
        results = run(FakeSites({("POST", API, "/v1/series/search"): (404, {})}))
        self.assert_only_failure(results, "API search")

    def test_a_rejected_login(self):
        results = run(FakeSites({("PUT", API, "/v1/account/login"): (401, {})}), account=True)
        self.assert_only_failure(results, "login")

    def test_a_moved_login_endpoint(self):
        results = run(FakeSites({("PUT", API, "/v1/account/login"): (404, {})}), account=True)
        self.assert_only_failure(results, "login")

    def test_a_list_index_entry_without_an_id(self):
        results = run(FakeSites({("GET", API, "/v1/lists"): (200, [{"title": LIST_TITLE}])}), account=True)
        self.assert_only_failure(results, "lists")
        self.assertNotIn(LIST_TITLE, results["lists"].detail)

    def test_list_items_that_lost_record_series(self):
        page = {"total_hits": 1, "results": [{"record": {"title": "Berserk", "id": 123}}]}
        results = run(FakeSites({("POST", API, "/v1/lists/0/search"): (200, page)}), account=True)
        self.assert_only_failure(results, "list page")

    def test_a_profile_without_list_counts(self):
        results = run(
            FakeSites({("GET", AP, f"/users/{USER}"): (200, "<html><body></body></html>")}), anime_planet=True
        )
        self.assert_only_failure(results, "Anime-Planet profile")

    def test_a_list_page_without_cards(self):
        page = "<html><body><div class='entries'></div></body></html>"
        results = run(FakeSites({("GET", AP, f"/users/{USER}/manga/read"): (200, page)}), anime_planet=True)
        self.assert_only_failure(results, "Anime-Planet list")

    def test_cards_that_lost_their_links(self):
        page = AP_LIST.replace('class="tooltip manga14"', 'class="card-link"')
        results = run(FakeSites({("GET", AP, f"/users/{USER}/manga/read"): (200, page)}), anime_planet=True)
        self.assert_only_failure(results, "Anime-Planet list")


class ProbeTests(unittest.TestCase):
    def test_a_search_without_a_series_id_falls_back_to_the_lists(self):
        sites = FakeSites({("POST", API, "/v1/series/search"): (200, {"hits": []})})
        results = run(sites, account=True)
        self.assertEqual(results["API search"].status, PASS)
        self.assertEqual(results["series details"].status, PASS)

    def test_without_any_series_id_the_lookup_is_skipped_not_failed(self):
        results = run(FakeSites({("POST", API, "/v1/series/search"): (200, {"hits": []})}))
        self.assertEqual(results["series details"].status, SKIPPED)


class UnreachableTests(unittest.TestCase):
    def test_an_api_outage_is_unreachable_not_broken(self):
        results = run(FakeSites({("POST", API, "/v1/series/search"): (503, {})}))
        self.assertEqual(results["API search"].status, UNREACHABLE)
        self.assertEqual(site_check.exit_code(list(results.values())), 2)

    def test_a_bot_check_on_anime_planet_is_unreachable(self):
        results = run(FakeSites({("GET", AP, f"/users/{USER}"): (403, CHALLENGE)}), anime_planet=True)
        self.assertEqual(results["Anime-Planet profile"].status, UNREACHABLE)
        self.assertIn("bot check", results["Anime-Planet profile"].detail)

    def test_a_bot_check_on_an_anime_planet_list_is_unreachable(self):
        results = run(FakeSites({("GET", AP, f"/users/{USER}/manga/read"): (403, CHALLENGE)}), anime_planet=True)
        self.assertEqual(results["Anime-Planet list"].status, UNREACHABLE)


class MainTests(unittest.TestCase):
    def test_the_report_file_and_exit_code(self):
        result = site_check.Result("login", FAIL, "a | b")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(site_check, "run_checks", return_value=[result]),
            mock.patch("builtins.print"),
        ):
            report = Path(tmp, "report.md")
            self.assertEqual(site_check.main_cli(["--report", str(report)]), 1)
            self.assertIn("a / b", report.read_text(encoding="utf-8"))

    def test_a_crash_is_reported_as_one_not_as_a_failed_check(self):
        with (
            mock.patch.object(site_check, "run_checks", side_effect=KeyError("boom")),
            mock.patch("builtins.print"),
            mock.patch("traceback.print_exc"),
        ):
            self.assertEqual(site_check.main_cli([]), 3)


if __name__ == "__main__":
    unittest.main()
