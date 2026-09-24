"""Check MangaUpdates and Anime-Planet for changes that would break this tool.

Run monthly by .github/workflows/site-check.yml, which opens an issue when a
check fails, comments on it while it stays open, and closes it once a run
passes again. Also runnable by hand from the project root:

    python tests/site_check.py [--report FILE]

Every check runs this tool's own code against today's responses: the list
and page validation in fetch_lists and _fetch_list_page, the fields the
related and finished checks read from a series, and the Anime-Planet profile
and list parsers. So a check fails when the tool itself would, not merely
when a response looks different.

Without credentials only the public API is read: a series search and one
series' details. With MU_USERNAME and MU_PASSWORD it also logs in and reads
the list index and one page of one list; with AP_USERNAME it reads that
public Anime-Planet profile and one of its lists. Nothing is ever changed.

The report carries check names, status codes and counts only -- never a
username, list names or titles -- because the issue it feeds may be public.
The tool's own console log is silenced for the same reason: it names the
account on login.

Exit status: 0 every check passed; 1 a check failed, so a site changed in a
way this tool depends on; 2 nothing failed, but something could not be
checked (site down, or this network blocked); 3 the check itself crashed.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402

PASS, FAIL, UNREACHABLE, SKIPPED = "pass", "fail", "unreachable", "skipped"
_MARK = {PASS: "✅ pass", FAIL: "❌ fail", UNREACHABLE: "⚠️ unreachable", SKIPPED: "➖ skipped"}

# A long-running series to look up. Any search hit will do; only the fields
# of the series it leads to are checked.
PROBE_SEARCH = "Berserk"

# Titles of the interstitial pages bot protection serves instead of the site.
_CHALLENGE_TITLE_RE = re.compile(
    r"<title>\s*(just a moment|attention required|checking your browser|ddos-guard)",
    re.IGNORECASE,
)


@dataclass
class Result:
    check: str
    status: str
    detail: str


class UnreachableError(Exception):
    """The response could not be checked at all: site down, or this network blocked."""


def silence_console_log() -> None:
    """Keep the tool's console log out of the output, which may be public.

    login() logs the account name at INFO, and list titles appear at INFO and
    WARNING. The log file still gets everything.
    """
    for handler in logging.getLogger("mu_export").handlers:
        if type(handler) is logging.StreamHandler:
            handler.setLevel(logging.CRITICAL + 1)


def unreachable_reason(resp: httpx.Response) -> str | None:
    """Why a response says nothing about the site, or None if it does."""
    challenged = resp.headers.get("cf-mitigated", "").lower() == "challenge" or bool(
        _CHALLENGE_TITLE_RE.search(resp.text[:20000])
    )
    if resp.status_code in (403, 407, 429) or resp.status_code >= 500:
        return f"HTTP {resp.status_code}" + (" (bot check)" if challenged else "")
    if challenged:
        return f"HTTP {resp.status_code} bot-check page"
    return None


def checked(send, url: str) -> httpx.Response:
    """Send a request, turning a response that says nothing into UnreachableError."""
    try:
        resp = send()
    except httpx.HTTPError as exc:
        raise UnreachableError(f"{type(exc).__name__} for {urlparse(url).netloc}") from exc
    reason = unreachable_reason(resp)
    if reason:
        raise UnreachableError(f"{reason} for {urlparse(url).netloc}")
    return resp


def series_field_problems(body: object) -> list[str]:
    """What the related and finished checks would misread in a series' details."""
    if not isinstance(body, dict):
        return [f"series details are a {type(body).__name__}, not an object"]
    problems = []
    if not isinstance(body.get("related_series"), list):
        problems.append("related_series is not a list, so no related series would be found")
    if not isinstance(body.get("completed"), bool):
        problems.append("completed is not a boolean, so no series would read as finished")
    if not isinstance(body.get("status"), str):
        problems.append("status is not a string")
    return problems


def http_failure(check: str, exc: httpx.HTTPError) -> Result:
    """A failed request: a moved endpoint fails, a down or blocked one is unreachable."""
    if isinstance(exc, httpx.HTTPStatusError) and not unreachable_reason(exc.response):
        return Result(check, FAIL, f"HTTP {exc.response.status_code}: the endpoint moved or changed")
    return Result(check, UNREACHABLE, type(exc).__name__)


def search_series_id(client: httpx.Client) -> tuple[Result, int | None]:
    url = f"{main.API_BASE_URL}/series/search"
    try:
        resp = checked(lambda: client.post(url, json={"search": PROBE_SEARCH, "perpage": 1}), url)
    except UnreachableError as exc:
        return Result("API search", UNREACHABLE, str(exc)), None
    if resp.status_code >= 400:
        return Result("API search", FAIL, f"HTTP {resp.status_code}: the endpoint the reachability probe uses"), None
    try:
        body = resp.json()
        series_id = body["results"][0]["record"]["series_id"]
    except (ValueError, LookupError, TypeError):
        # The tool reads nothing from search results, so their shape alone
        # is no reason to fail; only the lookup below loses its probe.
        return Result("API search", PASS, "answered, but with no series id to look up"), None
    return Result("API search", PASS, "answered with a series"), series_id


def check_series(client: httpx.Client, series_id: int) -> Result:
    url = f"{main.API_BASE_URL}/series/{series_id}"
    try:
        resp = checked(lambda: client.get(url), url)
    except UnreachableError as exc:
        return Result("series details", UNREACHABLE, str(exc))
    if resp.status_code >= 400:
        return Result("series details", FAIL, f"HTTP {resp.status_code} for a series id the API just gave out")
    try:
        body = resp.json()
    except ValueError:
        return Result("series details", FAIL, "response is not JSON")
    problems = series_field_problems(body)
    if problems:
        return Result("series details", FAIL, "; ".join(problems))
    return Result("series details", PASS, "related_series, completed and status present")


def check_account(client: httpx.Client) -> tuple[list[Result], int | None]:
    """Log in, read the list index and one page of one list, log out."""
    try:
        token = main.login(client)
    except SystemExit:
        return [Result("login", FAIL, "rejected: check the credential secrets, then the login endpoint")], None
    except httpx.HTTPError as exc:
        return [http_failure("login", exc)], None
    client.headers["Authorization"] = f"Bearer {token}"
    results = [Result("login", PASS, "session token received")]
    series_id = None
    try:
        lists = main.fetch_lists(client)
        results.append(Result("list index", PASS if lists else FAIL, f"{len(lists)} list(s)"))
        for entry in lists:
            items, total = main._fetch_list_page(client, entry["list_id"], 1)
            if not items:
                continue
            parsed = [s for s in map(main._extract_series, items) if s and s.get("id") is not None and s.get("title")]
            if parsed:
                series_id = parsed[0]["id"]
                detail = f"{len(parsed)} of {len(items)} item(s) on page 1 read (list holds {total})"
                results.append(Result("list page", PASS, detail))
            else:
                results.append(
                    Result("list page", FAIL, f"none of {len(items)} item(s) has record.series id and title")
                )
            break
        else:
            results.append(Result("list page", SKIPPED, "every list is empty"))
    except ValueError as exc:
        # fetch_lists and _fetch_list_page raise ValueError on a shape they
        # cannot trust; the message names list ids and fields, not titles.
        results.append(Result("lists", FAIL, str(exc)))
    except httpx.HTTPError as exc:
        results.append(http_failure("lists", exc))
    finally:
        main.logout(client)
    return results, series_id


def check_anime_planet(username: str) -> list[Result]:
    ap = main._AnimePlanetClient()
    try:
        url = main.AP_BASE_URL + main._ap_user_profile_path(username)
        try:
            profile = checked(lambda: ap.get(main._ap_user_profile_path(username)), url)
        except UnreachableError as exc:
            return [Result("Anime-Planet profile", UNREACHABLE, str(exc))]
        if profile.status_code >= 400:
            return [Result("Anime-Planet profile", FAIL, f"HTTP {profile.status_code} for the profile page")]
        lists = main._ap_parse_profile_list_counts(profile.text)
        if not lists:
            return [Result("Anime-Planet profile", FAIL, "no list counts found, so option 4 would export nothing")]
        results = [Result("Anime-Planet profile", PASS, f"{len(lists)} non-empty list(s)")]
        list_type = lists[0][0]
        try:
            entries = main._ap_fetch_list_page(ap, username, list_type, 1, 35)
        except httpx.HTTPError as exc:
            return [*results, http_failure("Anime-Planet list", exc)]
        if not entries:
            results.append(Result("Anime-Planet list", FAIL, "no entries parsed from a list the profile counts"))
        elif not any(e["record"]["series"]["url"] for e in entries):
            results.append(Result("Anime-Planet list", FAIL, f"{len(entries)} entries but none has a link"))
        else:
            results.append(Result("Anime-Planet list", PASS, f"{len(entries)} entries on page 1"))
        return results
    finally:
        ap.close()


def run_checks() -> list[Result]:
    results: list[Result] = []
    with httpx.Client(timeout=30) as client:
        search, series_id = search_series_id(client)
        results.append(search)
        if main.USERNAME and main.PASSWORD:
            account, list_series_id = check_account(client)
            client.headers.pop("Authorization", None)
            series_id = series_id or list_series_id
        else:
            account = [Result("login", SKIPPED, "set MU_USERNAME and MU_PASSWORD to check the lists")]
        if series_id is not None:
            results.append(check_series(client, series_id))
        elif search.status == PASS:
            results.append(Result("series details", SKIPPED, "no series id to look up"))
        results.extend(account)
    if main.AP_USERNAME:
        results.extend(check_anime_planet(main.AP_USERNAME))
    else:
        results.append(Result("Anime-Planet profile", SKIPPED, "set AP_USERNAME to check the Anime-Planet export"))
    return results


def exit_code(results: list[Result]) -> int:
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return 1
    if UNREACHABLE in statuses:
        return 2
    return 0


def render(results: list[Result], today: date) -> str:
    lines = [
        f"### Site check: {today.isoformat()}",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.check} | {_MARK[r.status]} | {r.detail.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check MangaUpdates and Anime-Planet for breaking changes.")
    ap.add_argument("--report", help="also write the markdown report to this file")
    args = ap.parse_args(argv)
    silence_console_log()
    try:
        results = run_checks()
        report, code = render(results, date.today()), exit_code(results)
    except Exception:  # noqa: BLE001 -- reported as a crash, not mistaken for a failed check
        traceback.print_exc()
        report, code = "### Site check crashed\n\nSee the workflow log for the traceback.\n", 3
    print(report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main_cli())
