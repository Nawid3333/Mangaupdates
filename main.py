import concurrent.futures
import contextlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from config.config import (
    API_BASE_URL,
    EXPORTS_DIR,
    ITEMS_PER_PAGE,
    LIST_PAGE_WORKERS,
    LOG_FILE,
    MAX_EXPORTS,
    MAX_RETRIES,
    PASSWORD,
    RETRY_DELAY,
    SERIES_LOOKUP_WORKERS,
    USERNAME,
    ensure_env_file,
    setup_logging,
)
from src import term
from src.term import cinput as input
from src.term import cprint as print

log = setup_logging()


# Protocol for the small slice of httpx.Client actually exercised in this
# module.  Using a protocol lets tests inject scripted fakes without widening
# every helper to `Any` or pretending `MagicMock` is a real client.
class _ClientLike(Protocol):
    def get(self, *args: Any, **kwargs: Any) -> httpx.Response: ...
    def post(self, *args: Any, **kwargs: Any) -> httpx.Response: ...
    def put(self, *args: Any, **kwargs: Any) -> httpx.Response: ...


# Terminal colours live in src/term.py so all six repos share one vocabulary,
# and with it the NO_COLOR / not-a-tty gate this module never had. Call sites
# name the meaning (term.warn, term.ok, term.title) rather than the colour.


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so string length can be measured.

    The pattern is compiled once at import. It used to be re-imported and
    re-compiled on every call, and this runs for every line of every box.
    Almost no line carries an escape at all, so the scan for one is done
    with a substring test before paying for the regex.
    """
    if "" not in text:
        return text
    return _ANSI_RE.sub("", text)


def _display_width(text: str) -> int:
    """How many terminal columns `text` occupies, ignoring ANSI codes.

    len() counts code points, and an emoji is one code point but two columns
    in every terminal that renders it, so a line containing one came out a
    column short and pushed the box's right edge out of line with the rest.

    Report text is overwhelmingly plain ASCII, and no ASCII character is
    wide, combining or a variation selector, so its width is just its
    length. That fast path matters: the wrapper measures a growing prefix
    once per word and once per character of a hard-broken URL, so the
    per-character loop below otherwise dominates writing a large report.
    """
    if text.isascii() and "" not in text:
        return len(text)
    plain = _strip_ansi(text)
    width = 0
    for index, char in enumerate(plain):
        # Variation selectors and combining marks attach to the previous
        # character rather than occupying a column of their own.
        if char in ("\ufe0f", "\ufe0e") or unicodedata.combining(char):
            continue
        wide = unicodedata.east_asian_width(char) in ("W", "F")
        # U+FE0F asks for emoji presentation, which is two columns even when
        # the base character is narrow on its own (e.g. the warning sign).
        emoji_presentation = plain[index + 1 : index + 2] == "\ufe0f"
        width += 2 if (wide or emoji_presentation) else 1
    return width


def _box(lines: list[str], width: int = 64) -> list[str]:
    """Return a list of box-drawn lines, accounting for ANSI codes.

    The box grows if a line does not fit rather than letting its right edge
    run ragged -- truncating would hide content, which is worse.
    """
    width = max(width, *(_display_width(line) for line in lines)) if lines else width
    out = ["╔" + "═" * width + "╗"]
    for line in lines:
        out.append("║" + line + " " * (width - _display_width(line)) + "║")
    out.append("╚" + "═" * width + "╝")
    return out


# Upper bound on the random spread added to every retry delay.
RETRY_JITTER = 1.0


def _retry_delay(resp: httpx.Response | None) -> float:
    """Seconds to wait before the next attempt, honoring Retry-After if sent.

    A small random spread is added on top. Lookups run across
    SERIES_LOOKUP_WORKERS threads, so without it every worker that was
    rate-limited in the same instant would sleep for exactly the same time
    and retry in the same instant -- rebuilding the burst the server just
    pushed back on.

    The jitter is only ever added, never subtracted: Retry-After is an
    instruction about the earliest acceptable retry, and waiting less than
    the server asked for would be worse than not jittering at all.
    """
    base = RETRY_DELAY
    if resp is not None:
        raw_value = resp.headers.get("Retry-After", "")
        with contextlib.suppress(ValueError):
            base = max(float(raw_value), 0.0)
    return base + random.uniform(0.0, RETRY_JITTER)


def _api_request(client: _ClientLike, method: str, url: str, **kwargs) -> httpx.Response:
    """Make an API request with automatic retry on transient errors.

    429 is retried alongside 5xx and transport errors. It was not before:
    only status >= 500 triggered a retry, so a rate-limited response came
    straight back to the caller, whose raise_for_status() then crashed the
    whole run instead of backing off. Both cases honor a Retry-After header
    when the server sends one.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = getattr(client, method)(url, **kwargs)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise httpx.HTTPStatusError(
                    f"Server error {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            return resp
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.HTTPStatusError,
        ) as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(getattr(exc, "response", None))
                log.warning(
                    "Request failed (attempt %d/%d): %s – retrying in %.0fs...",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                log.error("Request failed after %d attempts: %s", MAX_RETRIES, exc)
                raise

    # Defensive: every attempt must return or raise above.
    raise RuntimeError("Request loop exited without returning or raising")


def login(client: _ClientLike) -> str:
    """Authenticate and return a session token."""
    if not USERNAME or not PASSWORD:
        log.error("MU_USERNAME or MU_PASSWORD not set in .env file")
        raise SystemExit(1)

    log.info("Logging in as '%s'...", USERNAME)
    resp = _api_request(
        client,
        "put",
        f"{API_BASE_URL}/account/login",
        json={
            "username": USERNAME,
            "password": PASSWORD,
        },
    )

    if resp.status_code == 401:
        log.error("Login failed – invalid credentials")
        raise SystemExit(1)
    resp.raise_for_status()

    # `.get("context", {})` only substitutes when the key is absent, so a
    # context present-but-null -- or a body that is not an object at all --
    # crashed here with a bare AttributeError rather than the clean message
    # the missing-token case already produced. Same shape of defect that
    # _extract_series was hardened against.
    data = resp.json()
    context = data.get("context") if isinstance(data, dict) else None
    token = context.get("session_token") if isinstance(context, dict) else None
    if not token:
        log.error(
            "No session token in login response (status: %s)",
            data.get("status", "unknown") if isinstance(data, dict) else f"unexpected {type(data).__name__} body",
        )
        raise SystemExit(1)

    log.info("Login successful")
    return token


def check_site_reachable(client: _ClientLike) -> bool:
    """Confirm the MangaUpdates API is reachable before doing anything else.

    Uses an unauthenticated endpoint (series search) so this is a pure
    connectivity check, answerable even when login itself is about to fail
    for unrelated reasons (bad credentials) -- "is the site up" and "are
    these credentials valid" are different questions.
    """
    try:
        resp = client.post(f"{API_BASE_URL}/series/search", json={"search": "a", "perpage": 1}, timeout=10)
        return resp.status_code < 500
    except httpx.HTTPError:
        return False


def logout(client: _ClientLike) -> None:
    """End the API session."""
    try:
        client.post(f"{API_BASE_URL}/account/logout")
        log.info("Logged out")
    except Exception as exc:
        log.warning("Logout failed: %s", exc)


def fetch_lists(client: _ClientLike) -> list[dict]:
    """Get all user lists (built-in + custom).

    The shape is validated here, at the boundary, because everything
    downstream indexes these dicts directly -- export_all_lists and
    run_finished_check both do -- and a missing key surfaced as a bare
    KeyError naming nothing.

    A malformed entry aborts rather than being skipped. A silently dropped
    list would simply be absent from the export, and the next run would
    report it as removed along with every series in it, which is exactly the
    kind of confident wrong answer this program must not produce.
    """
    log.info("Fetching user lists...")
    resp = _api_request(client, "get", f"{API_BASE_URL}/lists")
    resp.raise_for_status()
    lists = resp.json()

    if not isinstance(lists, list):
        raise ValueError(f"MangaUpdates returned a malformed list index: expected an array, got {type(lists).__name__}")
    for index, entry in enumerate(lists):
        if not isinstance(entry, dict):
            raise ValueError(
                f"MangaUpdates returned a malformed list index: entry {index} is "
                f"{type(entry).__name__}, expected an object"
            )
        if entry.get("list_id") is None:
            raise ValueError(f"MangaUpdates returned a malformed list index: entry {index} has no list_id")
        if not isinstance(entry.get("title"), str):
            raise ValueError(
                f"MangaUpdates returned a malformed list index: entry {index} "
                f"(list_id {entry['list_id']}) has no usable title"
            )

    log.info("Found %d list(s): %s", len(lists), ", ".join(lst["title"] for lst in lists))
    return lists


MAX_LIST_PAGES = 500  # Safety limit to prevent infinite loops


def _fetch_list_page(client: _ClientLike, list_id: int, page: int) -> tuple[list, int]:
    """Fetch one page of one list. Returns (results, total_hits).

    The response shape is checked here instead of being left to fail later.
    A null `results` or `total_hits` used to surface as a bare TypeError from
    inside the paging arithmetic -- "'<=' not supported between instances of
    'NoneType' and 'int'" -- which named neither the list nor the page and
    read like a bug in this program rather than a bad response.

    Deliberately raises rather than substituting a default. An empty
    `results` would end the paging early, and the short list would then be
    saved as if it were complete; the next run would compare against it and
    report every missing series as removed. Stopping is the only safe answer
    for a list export -- the same reason a failed page already aborts -- but
    it should say what happened.
    """
    resp = _api_request(
        client,
        "post",
        f"{API_BASE_URL}/lists/{list_id}/search",
        json={
            "page": page,
            "perpage": ITEMS_PER_PAGE,
        },
    )
    resp.raise_for_status()

    def malformed(detail: str) -> ValueError:
        return ValueError(f"MangaUpdates returned a malformed page for list_id {list_id}, page {page}: {detail}")

    data = resp.json()
    if not isinstance(data, dict):
        raise malformed(f"expected an object, got {type(data).__name__}")

    results = data.get("results", [])
    total = data.get("total_hits", 0)
    if not isinstance(results, list):
        raise malformed(f"'results' was {type(results).__name__}, expected a list")
    if isinstance(total, bool) or not isinstance(total, int):
        raise malformed(f"'total_hits' was {type(total).__name__}, expected an integer")
    return results, total


def _pages_after_first(total: int) -> list[int]:
    """Which page numbers are still outstanding once page 1 has been read.

    Paging used to be discovered by walking -- fetch a page, see whether the
    running total had caught up with total_hits, fetch the next. But page 1
    already reports total_hits, so the whole page range is known after one
    round trip and there is nothing left to discover by going one at a time.
    """
    if total <= ITEMS_PER_PAGE:
        return []
    wanted = min(MAX_LIST_PAGES, -(-total // ITEMS_PER_PAGE))
    return list(range(2, wanted + 1))


def _join_pages(pages: list[list], total: int) -> list[dict]:
    """Concatenate fetched pages, stopping once total_hits items are held.

    The old serial loop also stopped at the first empty page, because there an
    empty page genuinely meant "there is nothing after this" -- it was what
    ended the walk. Here every page has already been fetched before this runs,
    so that rule stopped meaning "no more pages" and started meaning "discard
    the ones I already have": a single blank page in the middle threw away
    every page after it. Only the total_hits stop survives.
    """
    items: list[dict] = []
    for results in pages:
        items.extend(results)
        if len(items) >= total:
            break
    return items


def _verify_page_total(items: list[dict], total: int, title: str, pages_fetched: int) -> None:
    """Refuse to hand back a list shorter than the list said it was.

    A short export is the one failure this module must never pass on
    silently: it is saved as though it were complete, and the next run diffs
    against it and reports every item that was missing as removed from the
    account. _fetch_list_page already aborts rather than substitute a default
    for exactly that reason; this applies the same rule to the assembled
    result, which is where a shortfall actually becomes visible.

    The page-limit case stays a warning, not an error -- there the range was
    knowingly clamped and the shortfall is expected.
    """
    if len(items) >= total:
        return
    if pages_fetched >= MAX_LIST_PAGES:
        log.warning("  %s: hit page limit (%d) – list may be incomplete", title, MAX_LIST_PAGES)
        return
    raise ValueError(
        f"MangaUpdates returned an incomplete list for '{title}': "
        f"{len(items)} of {total} item(s) across {pages_fetched} page(s). "
        "Nothing was saved — saving this would make the next run report the "
        "missing series as removed from your account."
    )


@contextlib.contextmanager
def _worker_pool(max_workers: int):
    """A thread pool that drops queued work when the block is interrupted.

    ThreadPoolExecutor's own context manager always shuts down with
    wait=True and no cancellation, so Ctrl+C during a several-hundred-series
    lookup waited for every task still sitting in the queue before the
    interrupt was allowed through -- many seconds of apparent hang after the
    user had already asked it to stop. Cancelling the queue leaves only the
    requests genuinely in flight to finish.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        yield pool
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)


def _page_pool(job_count: int):
    """A pool sized for the page fetches actually queued, never larger."""
    return _worker_pool(max(1, min(LIST_PAGE_WORKERS, job_count)))


def export_list(client: _ClientLike, list_id: int, title: str) -> list[dict]:
    """Paginate through a single list and return all items."""
    first, total = _fetch_list_page(client, list_id, 1)

    rest = _pages_after_first(total)
    later: list[list] = []
    if rest:
        with _page_pool(len(rest)) as pool:
            later = [results for results, _total in pool.map(lambda p: _fetch_list_page(client, list_id, p), rest)]

    all_items = _join_pages([first, *later], total)

    # The serial loop warned when it had walked every one of the 500 allowed
    # pages and still not reached total_hits. The page range is now computed
    # up front, so the same condition reads as "the range was clamped and the
    # items it produced still fall short". Any *other* shortfall aborts.
    _verify_page_total(all_items, total, title, len(rest) + 1)

    log.info("  %s: %d item(s)", title, len(all_items))
    return all_items


def sanitize_filename(name: str) -> str:
    """Remove characters unsafe for filenames."""
    safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")
    # A single path component is capped at 255 UTF-16 units on NTFS (and
    # similar limits elsewhere). 100 codepoints stays well under that even
    # in the worst case -- a title made entirely of astral-plane characters
    # (most emoji), which are 2 UTF-16 units each -- with headroom left for
    # a "_N" collision suffix and the ".json" extension. An absurdly long
    # custom list title must be shortened, not crash the whole export.
    safe = safe[:100].strip().strip(".")
    return safe if safe else "Unnamed_List"


MANIFEST_NAME = "_manifest.json"


def _write_json(path: str, payload) -> None:
    """Serialise once, write once.

    json.dump streams into the file handle, which for a 300 KB export meant
    hundreds of thousands of individual writes through the text wrapper.
    Building the string first and writing it in one call produces
    byte-identical output roughly four times faster.
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, ensure_ascii=False))


def export_filenames(titles) -> dict[str, str]:
    """Map each list title to the file it is stored in.

    The writer and the reader used to derive this independently: the writer
    deduplicated colliding sanitized names with a "_2" suffix, while the
    reader simply re-ran sanitize_filename() on demand. Two distinct titles
    that sanitize to the same name -- "Sci-Fi/Fantasy" and "Sci-Fi_Fantasy"
    both become "Sci-Fi_Fantasy" -- made the second list silently read the
    *first* list's file, corrupting every added/removed diff computed from
    it. Deriving the mapping in one place used by both sides removes the
    chance for them to disagree.
    """
    mapping: dict[str, str] = {}
    # Tracked lowercased: two names differing only by case (e.g. "Sci-Fi" vs
    # "sci-fi") are the *same* file on a case-insensitive filesystem (NTFS,
    # default macOS). Comparing exact strings here missed that -- the second
    # write would silently land on the first list's file with no warning,
    # the exact corruption this manifest system exists to prevent, just via
    # a different door. Reproduced live before this fix.
    used_ci: set[str] = set()
    for title in titles:
        base = sanitize_filename(title)
        name = base
        counter = 2
        while name.lower() in used_ci:
            name = f"{base}_{counter}"
            counter += 1
        used_ci.add(name.lower())
        mapping[title] = name
    return mapping


def load_manifest(folder: str, titles) -> dict[str, str]:
    """Return the title -> filename mapping an export folder was written with.

    Prefers the manifest stored alongside the export, so a later change to
    the sanitizing rules can never silently repoint an old folder's files at
    the wrong list. Falls back to recomputing for folders written before
    manifests existed.
    """
    path = os.path.join(folder, MANIFEST_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict) and stored:
            return {str(k): str(v) for k, v in stored.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return export_filenames(titles)


def save_exports(exports: dict[str, list[dict]]) -> str:
    """Save each list to a timestamped folder. Returns the folder path."""
    # Two runs inside the same second produce the same folder name, and
    # os.replace onto an existing non-empty directory fails -- on Windows
    # with PermissionError. The run would then die *after* every list had
    # been fetched and written, losing all of it. Step the stamp forward
    # instead of adding a suffix, so the name still parses as a timestamp and
    # keeps working for ordering and rotation.
    stamp = datetime.now()
    folder_name = stamp.strftime(EXPORT_FOLDER_FORMAT)
    folder_path = os.path.join(EXPORTS_DIR, folder_name)
    while os.path.exists(folder_path):
        stamp += timedelta(seconds=1)
        folder_name = stamp.strftime(EXPORT_FOLDER_FORMAT)
        folder_path = os.path.join(EXPORTS_DIR, folder_name)

    # Write into a temporary folder first and only reveal it under its final
    # name once every file has been written successfully. Writing directly
    # into `folder_path` would let a crash/interruption partway through leave
    # behind a partial export folder that later runs could then pick up as
    # "the previous export" (via find_previous_export), producing bogus
    # added/removed diffs from incomplete data.
    tmp_folder_path = folder_path + ".tmp"
    if os.path.isdir(tmp_folder_path):
        shutil.rmtree(tmp_folder_path)
    os.makedirs(tmp_folder_path, exist_ok=True)

    # A run that crashed part-way leaves its *.tmp behind forever -- nothing
    # else ever revisits it. Sweep any leftover here so a crash doesn't
    # permanently clutter exports/ with orphaned partial data.
    #
    # Files as well as directories: save_related_series and
    # save_finished_series build their reports with mkstemp(suffix=".tmp") in
    # this same folder, and only directories were being swept, so a crash
    # mid-report left a stray file that nothing would ever remove.
    if os.path.isdir(EXPORTS_DIR):
        for entry in os.listdir(EXPORTS_DIR):
            entry_path = os.path.join(EXPORTS_DIR, entry)
            if entry_path == tmp_folder_path or not entry.endswith(".tmp"):
                continue
            with contextlib.suppress(OSError):
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path)
                else:
                    os.remove(entry_path)
                log.info("Cleaned up leftover partial data from a previous run: %s", entry)

    filenames = export_filenames(list(exports.keys()))
    for title, items in exports.items():
        unique_title = filenames[title]
        file_path = os.path.join(tmp_folder_path, f"{unique_title}.json")
        _write_json(file_path, items)
        log.info("  Saved %s (%d items)", os.path.join(folder_path, f"{unique_title}.json"), len(items))

    manifest_path = os.path.join(tmp_folder_path, MANIFEST_NAME)
    _write_json(manifest_path, filenames)

    os.replace(tmp_folder_path, folder_path)
    return folder_path


def _extract_series(item) -> dict | None:
    """Pull the `record.series` dict out of one list item, or None if the
    shape doesn't hold up.

    `.get("record", {})` only substitutes the default when the key is
    *missing* -- a key present with a null value (or the item itself being
    null, or not a dict at all) still passed None on through to the next
    `.get()` call and crashed with AttributeError. The API has never sent
    that shape, but a single such item anywhere in a list used to be able to
    take down the entire run (export, compare, related-series, and
    finished-series checks all go through this).
    """
    if not isinstance(item, dict):
        return None
    record = item.get("record") or {}
    if not isinstance(record, dict):
        return None
    series = record.get("series") or {}
    return series if isinstance(series, dict) else None


def get_series_ids(items: list[dict]) -> dict[int, str]:
    """Extract {series_id: title} from a list export."""
    result = {}
    for item in items:
        series = _extract_series(item)
        if series is None:
            continue
        sid = series.get("id")
        if sid is not None:
            result[sid] = series.get("title", "Unknown")
    return result


def get_series_basic(items: list[dict]) -> dict[int, dict]:
    """Extract {series_id: {"title", "url"}} from a list export."""
    result = {}
    for item in items:
        series = _extract_series(item)
        if series is None:
            continue
        sid = series.get("id")
        if sid is not None:
            result[sid] = {"title": series.get("title", "Unknown"), "url": series.get("url", "")}
    return result


def export_all_lists(client: _ClientLike, lists: list[dict]) -> dict[str, list[dict]]:
    """Export every list, guarding against two distinct lists sharing a title.

    Every list's page 1 is fetched at once, then every remaining page of every
    list at once -- two round trips rather than one list's pages after
    another's. Calling export_list per list inside a pool would deadlock the
    moment it tried to fetch its own pages from that same pool, so the two
    phases are driven from here instead.

    Request count is unchanged; only how many are in flight at a time is.
    """
    # Resolve the storage keys first, in list order, so the duplicate-title
    # warnings and the resulting key assignment stay exactly as they were --
    # the dedup counter depends on the order lists are seen in, and that must
    # not become a function of which request happens to finish first.
    plan: list[tuple[str, int, str]] = []
    used_titles = set()
    for lst in lists:
        list_id = lst["list_id"]
        title = lst["title"]
        # Keying `exports` by title alone would let the second list silently
        # overwrite the first one's data if two distinct lists (different
        # list_id) happen to share the same title.
        key = title
        counter = 2
        while key in used_titles:
            key = f"{title} ({counter})"
            counter += 1
        if key != title:
            log.warning(
                "Duplicate list title '%s' (list_id=%s) – storing under '%s' to avoid data loss",
                title,
                list_id,
                key,
            )
        used_titles.add(key)
        plan.append((key, list_id, title))

    if not plan:
        return {}

    # A pool per phase, each sized for the work that phase actually has. One
    # pool sized by len(plan) and reused for both looked tidier, but the
    # phases are not the same size: a single large list is one plan entry and
    # ten pages, so that pool had one thread and fetched every page after the
    # first serially -- precisely what this was meant to stop. The phases are
    # strictly sequential anyway (page 1 is what reveals the rest), so nothing
    # overlaps by splitting them.
    with _page_pool(len(plan)) as pool:
        firsts = list(pool.map(lambda entry: _fetch_list_page(client, entry[1], 1), plan))

    jobs = [(index, page) for index, (_results, total) in enumerate(firsts) for page in _pages_after_first(total)]
    later_results: list[list] = []
    if jobs:

        def fetch_job(job: tuple[int, int]) -> list:
            index, page = job
            return _fetch_list_page(client, plan[index][1], page)[0]

        with _page_pool(len(jobs)) as pool:
            later_results = list(pool.map(fetch_job, jobs))

    # pool.map yields in submission order and `jobs` was built list by list in
    # ascending page order, so each list's pages arrive here already ordered.
    later_by_list: dict[int, list[list]] = {index: [] for index in range(len(plan))}
    for (index, _page), results in zip(jobs, later_results, strict=True):
        later_by_list[index].append(results)

    exports = {}
    for index, (key, _list_id, title) in enumerate(plan):
        first, total = firsts[index]
        items = _join_pages([first, *later_by_list[index]], total)
        _verify_page_total(items, total, title, len(later_by_list[index]) + 1)
        log.info("  %s: %d item(s)", title, len(items))
        exports[key] = items
    return exports


# ==================== Related series ====================
def fetch_series_related(client: _ClientLike, series_id: int) -> list[dict] | None:
    """Fetch the "Related Series" section for one series.

    Returns the raw list of relation objects (title/id/url/relation_type),
    always present -- MangaUpdates represents "no related series" as an
    empty list, not a missing key or null, verified against the live API.
    Returns None if the series could not be looked up at all (deleted from
    the site, or every retry was exhausted), so the caller can skip it
    without mistaking "lookup failed" for "genuinely has none".
    """
    try:
        resp = _api_request(client, "get", f"{API_BASE_URL}/series/{series_id}")
        if resp.status_code == 404:
            log.warning("Series id %s no longer exists on MangaUpdates — skipping", series_id)
            return None
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"unexpected response shape: {type(body).__name__}")
        return body.get("related_series", [])
    except Exception as exc:
        # Deliberately broad: this runs as one task among hundreds inside a
        # thread pool (collect_related_series), and the contract of a single
        # lookup here is "never take the whole batch down". httpx.HTTPError
        # covers transport/status failures and ValueError covers a malformed
        # JSON body, but a response shaped unexpectedly (e.g. a JSON array
        # instead of an object) raised AttributeError from body.get(...)
        # here, which neither of those caught -- reproduced live, this one
        # series then crashed every other series' result along with it since
        # future.result() re-raises on the aggregating thread. The 404 case
        # above already gets the same "skip, don't abort" treatment; this
        # closes the same hole for every other way one lookup can go wrong.
        log.warning("Could not fetch related series for id %s: %s", series_id, exc)
        return None


def collect_related_series(client: _ClientLike, exports: dict[str, list[dict]]) -> dict[int, dict]:
    """Look up every series in every list and gather what is related but not already tracked.

    One hop only: a related series is found because it relates to a series
    already in one of your lists, not because it relates to a series that
    was itself found this way. Recursing further would blur "this is one
    step away from something you read" into "this is somewhere in the same
    franchise", which is a much noisier and less actionable signal.

    Lookups run concurrently across SERIES_LOOKUP_WORKERS threads -- one
    request per series, and this is often hundreds of series, so doing it
    one at a time was the slowest part of a run for no benefit: the API does
    not charge for this endpoint, and real pushback (429) is already handled
    by _api_request's own backoff regardless of how many threads are asking.

    Returns {related_series_id: {"title", "url", "sources": [(origin_title,
    relation_type), ...]}}, already excluding anything you already have in
    any list and deduplicated across every series that pointed at it.
    """
    # Every series you already track, across every list -- computed once and
    # used both to know which ids to look up and which relations to exclude
    # (a related series is only useful to report if you do not already have
    # it somewhere).
    all_ids: dict[int, str] = {}
    for items in exports.values():
        all_ids.update(get_series_ids(items))
    known_ids = set(all_ids)

    related: dict[int, dict] = {}
    total = len(all_ids)
    done = 0

    with _worker_pool(SERIES_LOOKUP_WORKERS) as pool:
        future_to_series = {
            pool.submit(fetch_series_related, client, series_id): (series_id, origin_title)
            for series_id, origin_title in all_ids.items()
        }
        # Aggregation happens here, on the main thread, as each future
        # completes -- workers only fetch and return; nothing but this loop
        # ever writes to `related`, so no lock is needed.
        for future in concurrent.futures.as_completed(future_to_series):
            series_id, origin_title = future_to_series[future]
            done += 1
            relations = future.result()
            log.info("  [%d/%d] Checked related series for %s", done, total, origin_title)
            if relations is None:
                continue

            for rel in relations:
                rel_id = rel.get("related_series_id")
                rel_title = rel.get("related_series_name")
                if rel_id is None or not rel_title:
                    continue
                if rel_id == series_id or rel_id in known_ids:
                    continue

                entry = related.setdefault(
                    rel_id,
                    {"title": rel_title, "url": rel.get("related_series_url", ""), "sources": []},
                )
                entry["sources"].append((origin_title, rel.get("relation_type", "Related")))

    return related


def save_related_series(related: dict[int, dict]) -> str:
    """Write the related-series report to a single, stable path.

    Every run overwrites the same file (exports/related.txt) rather than
    writing a new one into each timestamped export folder, so there is one
    fixed place to check and it always holds the newest data. The write is
    atomic -- built in a temp file, then swapped in with os.replace -- for
    the same reason save_exports uses the same pattern: a crash mid-write
    must never leave a half-written related.txt in place of a good one.

    The output uses the same box/card layout as save_finished_series so
    the two reports are visually consistent.
    """
    path = os.path.join(EXPORTS_DIR, "related.txt")
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    inner_width = FINISHED_REPORT_WIDTH - 4  # "║  ...  ║"

    lines: list[str] = [
        _CARD_TOP,
        "║" + _pad_finished_line(f"  Related Series — {now}") + "║",
        _CARD_MID,
        "║" + _pad_finished_line(f"  {len(related)} related series found") + "║",
        _CARD_BOTTOM,
        "",
    ]

    if not related:
        lines.append("  (none — every related series found is already in one of your lists)")
    else:
        # Everything here was collected in as_completed order -- whichever
        # lookup happened to finish first -- so both the entries and their
        # sources have to be ordered explicitly or the same data prints
        # differently every run. Confirmed live before this: two runs of
        # identical code produced two different reports. The id breaks ties
        # so the ordering is total, not merely stable.
        entries = [entry for _rel_id, entry in sorted(related.items(), key=lambda kv: (kv[1]["title"].lower(), kv[0]))]
        total = len(entries)
        idx_width = len(str(total))

        for i, entry in enumerate(entries, 1):
            source_bits = ", ".join(
                f'{rel_type} of "{origin}"'
                for origin, rel_type in sorted(entry["sources"], key=lambda src: (src[0].lower(), src[1].lower()))
            )

            lines.append(_CARD_TOP)
            # Wrapped like every other line in the card: _pad_finished_line
            # only pads, so an over-long title used to push this one row past
            # the border while the rest of the card stayed at the fixed width.
            header = f"  [{i:>{idx_width}}/{total}]  {entry['title']}"
            for line in _wrap_finished_line(header, inner_width):
                lines.append("║" + _pad_finished_line(line) + "║")
            lines.append(_CARD_MID)

            for line in _wrap_finished_line(f"  Relation: {source_bits}", inner_width):
                lines.append("║" + _pad_finished_line(line) + "║")

            for line in _wrap_finished_line(f"  Link: {entry['url']}", inner_width):
                lines.append("║" + _pad_finished_line(line) + "║")

            lines.append(_CARD_BOTTOM)
            if i < total:
                lines.append("")

    body = "\n".join(lines).rstrip("\n") + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=EXPORTS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
    return path


# ==================== Wish List completion check ====================
def fetch_series_status(client: _ClientLike, series_id: int) -> dict | None:
    """Fetch one series' completion state.

    `completed` is MangaUpdates' own boolean for "nothing more is ever
    coming" -- it is true for both a normal Complete and a Cancelled/
    Discontinued series, and stays false for Hiatus, Ongoing, and Upcoming.
    It also correctly stays false for a series where only one release format
    (e.g. print volumes) is complete but another (e.g. a webtoon re-release)
    is still ongoing, which a plain "Complete" text search on `status` would
    have wrongly flagged as finished. Verified against known real series of
    each kind before this was built.

    Returns None on 404 or any lookup failure, same convention as
    fetch_series_related, so the caller can skip it instead of misreading a
    failed lookup as "not finished".
    """
    try:
        resp = _api_request(client, "get", f"{API_BASE_URL}/series/{series_id}")
        if resp.status_code == 404:
            log.warning("Series id %s no longer exists on MangaUpdates — skipping", series_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected response shape: {type(data).__name__}")
        return {"completed": bool(data.get("completed", False)), "status": data.get("status", "") or ""}
    except Exception as exc:
        # Deliberately broad -- same reasoning as fetch_series_related: one
        # task among many in a thread pool, must never take the whole batch
        # down over a single unexpectedly-shaped response.
        log.warning("Could not fetch status for id %s: %s", series_id, exc)
        return None


def find_finished_wishlist_series(client: _ClientLike, wish_items: list[dict]) -> dict[int, dict]:
    """Check every series in the Wish List and return the ones that are finished.

    Concurrent across SERIES_LOOKUP_WORKERS threads, same pattern and same
    reasoning as collect_related_series: one request per series, the API
    does not charge for it, and _api_request's own backoff is the real
    safety net rather than a fixed pace.

    Returns {series_id: {"title", "url", "status"}}.
    """
    basic = get_series_basic(wish_items)
    finished: dict[int, dict] = {}
    total = len(basic)
    done = 0

    with _worker_pool(SERIES_LOOKUP_WORKERS) as pool:
        future_to_series = {
            pool.submit(fetch_series_status, client, series_id): (series_id, info) for series_id, info in basic.items()
        }
        for future in concurrent.futures.as_completed(future_to_series):
            series_id, info = future_to_series[future]
            done += 1
            result = future.result()
            log.info("  [%d/%d] Checked status for %s", done, total, info["title"])
            if result is None or not result["completed"]:
                continue
            finished[series_id] = {"title": info["title"], "url": info["url"], "status": result["status"]}

    return finished


# Width chosen so each card fits comfortably in an 80-column terminal.
FINISHED_REPORT_WIDTH = 82

# Every card draws the same three borders, and a large report draws thousands
# of them, so they are built once rather than re-joined per card.
_CARD_TOP = "╔" + "═" * FINISHED_REPORT_WIDTH + "╗"
_CARD_MID = "╠" + "═" * FINISHED_REPORT_WIDTH + "╣"
_CARD_BOTTOM = "╚" + "═" * FINISHED_REPORT_WIDTH + "╝"


# A completion marker as MangaUpdates writes it: "(Complete)", "(Cancelled)".
# The trailing boundary is \b, not (?!\)) -- the negative lookahead this used
# to carry required the marker *not* to be closed, so the three ordinary forms
# "(Complete)", "(Cancelled)" and "(Discontinued)" never matched and only the
# malformed "(Complete" did. Nothing looked wrong because _split_finished_status
# falls back to "first fragment plus the rest", which is the same answer
# whenever the marker is in the first fragment; the branch that keeps a whole
# out-of-order status intact simply never ran. The (?<!\() guard is kept: it is
# what stops a doubled "((Complete" from reading as a marker.
_FINISHED_STATUS_SPLIT_RE = re.compile(
    r"(?<!\()\((Complete|Completed|Cancelled|Discontinued)\b",
    re.IGNORECASE,
)


def _pad_finished_line(text: str) -> str:
    """Pad `text` to the report width using display columns, not codepoints."""
    return text + " " * max(0, FINISHED_REPORT_WIDTH - _display_width(text))


def _wrap_finished_line(text: str, inner_width: int) -> list[str]:
    """Wrap `text` to `inner_width` display columns, preserving leading whitespace.

    The first line's leading spaces are measured and then reapplied to every
    continuation line so multi-line status and detail blocks stay aligned.
    Long unbroken tokens (typically URLs) are hard-broken so they do not
    force a single word onto its own line.
    """
    if _display_width(text) <= inner_width:
        return [text]

    leading_match = re.match(r"^(\s+)", text)
    leading = leading_match.group(1) if leading_match else ""
    continuation = " " * _display_width(leading)
    content = text[len(leading) :]
    words = content.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if _display_width(leading + candidate) <= inner_width:
            current = candidate
            continue

        if current:
            lines.append(leading + current)
            leading = continuation
            current = ""

        # A single word that is wider than the line must be hard-broken.
        while _display_width(leading + word) > inner_width:
            # Take as much of the word as will fit.
            take = ""
            for char in word:
                test = take + char
                if _display_width(leading + test) > inner_width:
                    break
                take = test
            if not take:
                # The leading indent alone fills the line; force at least one char.
                take = word[0]
            lines.append(leading + take)
            leading = continuation
            word = word[len(take) :]
        current = word

    if current:
        lines.append(leading + current)
    return lines


def _split_finished_status(status_text: str) -> tuple[str, list[str]]:
    """Split a MangaUpdates status into a headline and detail bullets.

    Slash-separated fragments such as:
        "12 Volumes (Complete) / S1: 6 Volumes / S2: 6 Volumes"
    are separated so the overall completion phrase stays on the Status line
    and the per-season / per-format notes become indented bullets. When no
    fragment carries a completion marker, the whole string becomes the
    headline so nothing is silently dropped.
    """
    parts = [p.strip() for p in status_text.split("/") if p.strip()]
    if not parts:
        return "", []
    if len(parts) == 1:
        return parts[0], []

    first = parts[0]
    if _FINISHED_STATUS_SPLIT_RE.search(first):
        return first, parts[1:]

    # No clear completion marker in the first fragment. If any later fragment
    # carries one, keep the whole original status as the headline rather than
    # elevating an arbitrary later fragment. Otherwise fall back to the first
    # fragment plus the rest as details.
    if any(_FINISHED_STATUS_SPLIT_RE.search(p) for p in parts[1:]):
        return status_text, []
    return first, parts[1:]


def save_finished_series(finished: dict[int, dict], total_checked: int) -> str:
    """Write the finished-Wish-List report to a single, stable path.

    Same stable-path, atomic-overwrite pattern as save_related_series: one
    fixed file (exports/ready_to_read.txt) that always holds the newest run,
    not one per timestamped export folder. The output uses a box layout with
    one card per series so long status strings are readable.
    """
    path = os.path.join(EXPORTS_DIR, "ready_to_read.txt")
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    inner_width = FINISHED_REPORT_WIDTH - 4  # "║  ...  ║"

    lines: list[str] = [
        _CARD_TOP,
        "║" + _pad_finished_line(f"  Finished Wish List Series — {now}") + "║",
        _CARD_MID,
        "║" + _pad_finished_line(f"  {len(finished)} series finished out of {total_checked} checked") + "║",
        _CARD_BOTTOM,
        "",
    ]

    if not finished:
        lines.append("  No Wish List series have finished releasing yet.")
    else:
        # Sort by title; use the series id to break ties into a total order.
        entries = [entry for _sid, entry in sorted(finished.items(), key=lambda kv: (kv[1]["title"].lower(), kv[0]))]
        total = len(entries)
        idx_width = len(str(total))

        for i, entry in enumerate(entries, 1):
            status_text = " / ".join(s.strip() for s in entry["status"].splitlines() if s.strip())
            summary, details = _split_finished_status(status_text)

            lines.append(_CARD_TOP)
            # Wrapped like every other line in the card: _pad_finished_line
            # only pads, so an over-long title used to push this one row past
            # the border while the rest of the card stayed at the fixed width.
            header = f"  [{i:>{idx_width}}/{total}]  {entry['title']}"
            for line in _wrap_finished_line(header, inner_width):
                lines.append("║" + _pad_finished_line(line) + "║")
            lines.append(_CARD_MID)

            for line in _wrap_finished_line(f"  Status: {summary}", inner_width):
                lines.append("║" + _pad_finished_line(line) + "║")

            for detail in details:
                for line in _wrap_finished_line(f"  ▸ {detail}", inner_width):
                    lines.append("║" + _pad_finished_line(line) + "║")

            for line in _wrap_finished_line(f"  Link: {entry['url']}", inner_width):
                lines.append("║" + _pad_finished_line(line) + "║")

            lines.append(_CARD_BOTTOM)
            if i < total:
                lines.append("")

    body = "\n".join(lines).rstrip("\n") + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=EXPORTS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
    return path


EXPORT_FOLDER_FORMAT = "%d.%m.%Y_%H-%M-%S"


def _parse_folder_date(name: str) -> datetime:
    """Parse a folder name into a datetime for sorting."""
    try:
        return datetime.strptime(name, EXPORT_FOLDER_FORMAT)
    except ValueError:
        return datetime.min


def _is_export_folder(name: str) -> bool:
    """Whether this directory name is one this program created as an export."""
    try:
        datetime.strptime(name, EXPORT_FOLDER_FORMAT)
    except ValueError:
        return False
    return True


def _export_folders() -> list[str]:
    """Every export snapshot in EXPORTS_DIR, oldest first.

    find_previous_export and rotate_exports each used to decide for
    themselves what counted, and both accepted *any* directory. That was
    wrong in two different ways: rotation counted an unrelated folder toward
    MAX_EXPORTS and deleted it first, because an unparseable name sorts as
    datetime.min and therefore looks like the oldest export there is; and a
    comparison would happily diff against it and report every series as new.
    Deciding it once, here, is the only way the two can agree.
    """
    if not os.path.isdir(EXPORTS_DIR):
        return []
    names = [
        name
        for name in os.listdir(EXPORTS_DIR)
        if _is_export_folder(name) and os.path.isdir(os.path.join(EXPORTS_DIR, name))
    ]
    return sorted(names, key=_parse_folder_date)


def find_previous_export(current_folder: str) -> str | None:
    """Find the most recent export folder before current_folder."""
    current_dt = _parse_folder_date(os.path.basename(current_folder))
    folders = [name for name in _export_folders() if _parse_folder_date(name) < current_dt]
    if folders:
        return os.path.join(EXPORTS_DIR, folders[-1])
    return None


def _load_prev_exports(prev_folder: str, titles: list[str]) -> tuple[dict[str, list[dict]], set[str]]:
    """Load previous exports. Returns ({list_title: raw items}, unreadable titles).

    Reads the manifest once and returns the raw items rather than an
    already-reduced id map, so compare_exports can do both the movement scan
    and the per-list diff from one read of each file instead of two.

    A file that exists but cannot be parsed used to be substituted with an
    empty list, which made a corrupted export indistinguishable from a list
    that genuinely had nothing in it: every series in it came back reported
    as newly Added. That is the worst kind of wrong answer here, because it
    looks exactly like a real account change. Those titles are named
    separately now so the caller can say it does not know, instead of
    guessing.
    """
    result: dict[str, list[dict]] = {}
    unreadable: set[str] = set()
    filenames = load_manifest(prev_folder, titles)
    for title in titles:
        prev_file = os.path.join(prev_folder, f"{filenames.get(title, sanitize_filename(title))}.json")
        if os.path.isfile(prev_file):
            try:
                with open(prev_file, encoding="utf-8") as f:
                    result[title] = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read '%s' from the previous export: %s", title, exc)
                unreadable.add(title)
    return result, unreadable


def compare_exports(current_folder: str, exports: dict[str, list[dict]]) -> bool:
    """Compare current export with the previous one and print changes.

    Returns True if any changes were detected, False otherwise.
    """
    prev_folder = find_previous_export(current_folder)
    if not prev_folder:
        log.info("")
        for line in _box(
            [
                term.step("ℹ  No previous export found"),
                term.dim("   Skipping comparison"),
            ]
        ):
            log.info(line)
        return False

    prev_name = os.path.basename(prev_folder)
    log.info("")
    for line in _box(
        [
            term.title("  📋  Changes since last export (" + prev_name + ")"),
        ]
    ):
        log.info(line)

    # Load every previous list once; both the movement scan and the
    # per-list diff below read from this instead of the file a second time.
    all_titles = list(exports.keys())
    prev_by_list, unreadable = _load_prev_exports(prev_folder, all_titles)
    prev_ids_by_list = {title: get_series_ids(items) for title, items in prev_by_list.items()}

    # get_series_ids(items) used to be called separately for the movement
    # scan, again per moved series (re-scanning every list from scratch to
    # find its name), and again in the diff loop below -- up to 3x over the
    # same list. Computed once here and reused everywhere.
    cur_ids_by_list = {title: get_series_ids(items) for title, items in exports.items()}

    prev_sid_to_list: dict[int, str] = {}
    for list_title, ids in prev_ids_by_list.items():
        for sid in ids:
            prev_sid_to_list[sid] = list_title

    cur_sid_to_list: dict[int, str] = {}
    cur_sid_to_name: dict[int, str] = {}
    for list_title, ids in cur_ids_by_list.items():
        for sid, name in ids.items():
            cur_sid_to_list[sid] = list_title
            cur_sid_to_name[sid] = name

    # Detect movements (series that changed lists)
    moved: dict[int, tuple[str, str, str]] = {}  # sid -> (title, old_list, new_list)
    for sid, new_list in cur_sid_to_list.items():
        old_list = prev_sid_to_list.get(sid)
        if old_list and old_list != new_list:
            moved[sid] = (cur_sid_to_name[sid], old_list, new_list)

    has_changes = False

    # Log movements first
    if moved:
        has_changes = True
        log.info("")
        log.info("  %s", term.alert("↔ Moved series"))
        for _sid, (name, old_list, new_list) in sorted(moved.items(), key=lambda kv: (kv[1][0].lower(), kv[0])):
            log.info(
                "     %s %s  %s → %s",
                term.warn("↪"),
                name,
                term.dim(old_list),
                term.accent(new_list),
            )
        log.info("")

    moved_sids = set(moved.keys())

    for title, current_items in exports.items():
        if title in unreadable:
            # Not reported as added/removed: we do not know what was there.
            has_changes = True
            log.info(
                "  %s  %s",
                term.warn("✗"),
                term.warn(
                    f"[{title}] previous export could not be read – cannot compare "
                    f"(currently {len(current_items)} item(s))"
                ),
            )
            continue

        if title not in prev_by_list:
            log.info(
                "  %s  %s",
                term.accent("✱"),
                term.accent(f"[{title}] NEW LIST (not in previous export) – {len(current_items)} item(s)"),
            )
            has_changes = True
            continue

        prev_items = prev_by_list[title]
        current_ids = cur_ids_by_list[title]
        prev_ids = prev_ids_by_list[title]

        # Exclude moved series from simple added/removed
        added_ids = set(current_ids) - set(prev_ids) - moved_sids
        removed_ids = set(prev_ids) - set(current_ids) - moved_sids
        count_diff = len(current_items) - len(prev_items)

        if not added_ids and not removed_ids:
            if count_diff == 0:
                log.info(
                    "  %s  %s — %s",
                    term.ok("✓"),
                    term.ok(f"[{title}] No changes"),
                    term.dim(f"({len(current_items)} items)"),
                )
            else:
                has_changes = True
                sign = "+" if count_diff >= 0 else ""
                log.info(
                    "  %s  %s %d → %d (%s%d) %s",
                    term.warn("~"),
                    term.bold(f"[{title}]"),
                    len(prev_items),
                    len(current_items),
                    term.warn(sign),
                    count_diff,
                    term.dim("(movements only)"),
                )
            continue

        has_changes = True
        sign = "+" if count_diff >= 0 else ""
        log.info(
            "  %s  %s %d → %d (%s%d)",
            term.warn("✎"),
            term.bold(f"[{title}]"),
            len(prev_items),
            len(current_items),
            term.warn(sign),
            count_diff,
        )

        # Sets of ids iterate in hash order, which reads as arbitrary and
        # makes two printings of the same change set hard to compare.
        for sid in sorted(added_ids, key=lambda s: (current_ids[s].lower(), s)):
            log.info("     %s Added:   %s", term.ok("+"), current_ids[sid])
        for sid in sorted(removed_ids, key=lambda s: (prev_ids[s].lower(), s)):
            log.info("     %s Removed: %s", term.warn("-"), prev_ids[sid])

    # Check for lists that existed before but are now gone. Read the
    # previous folder's own manifest for this -- comparing against the
    # *current* titles' filenames would use the current run's dedup order,
    # which does not necessarily match the one the previous folder was
    # written with if list ordering changed between runs.
    prev_manifest = load_manifest(prev_folder, [])
    if not prev_manifest:
        prev_manifest = {f[:-5]: f[:-5] for f in os.listdir(prev_folder) if f.endswith(".json") and f != MANIFEST_NAME}
    # Read the manifest save_exports already wrote for this run, rather than
    # recomputing it -- one source of truth for what filenames this folder
    # actually uses.
    current_filenames = set(load_manifest(current_folder, list(exports.keys())).values())
    for prev_title, prev_filename in prev_manifest.items():
        if prev_filename not in current_filenames:
            has_changes = True
            log.info(
                "  %s  %s",
                term.warn("✗"),
                term.warn(f"[{prev_title}] LIST REMOVED (no longer exists)"),
            )

    if not has_changes:
        log.info("")
        for line in _box(
            [
                term.success("  ✅ NO CHANGES"),
                term.dim("     All lists are identical to the previous export"),
            ]
        ):
            log.info(line)
    else:
        log.info("")
        for line in _box(
            [
                term.alert("  ⚠️  CHANGES DETECTED"),
                term.dim("     Review the details above"),
            ]
        ):
            log.info(line)

    return has_changes


def rotate_exports() -> None:
    """Keep only the newest MAX_EXPORTS folders, delete the rest."""
    if not os.path.isdir(EXPORTS_DIR):
        return

    # Only this program's own snapshots. Anything else in exports/ -- a
    # folder the user put there, one from another tool -- is not ours to
    # count or delete.
    folders = _export_folders()

    while len(folders) > MAX_EXPORTS:
        oldest = folders.pop(0)
        path = os.path.join(EXPORTS_DIR, oldest)
        try:
            shutil.rmtree(path)
            log.info("Deleted old export: %s", oldest)
        except OSError as exc:
            log.warning("Could not delete %s: %s", oldest, exc)


def print_header() -> None:
    log.info("%s", term.accent("=" * 60))
    log.info("%s", term.step("  MANGAUPDATES LIST EXPORTER & TRACKER"))
    log.info("%s", term.accent("=" * 60))


def show_menu() -> None:
    print("\n" + term.step("Options:"))
    print("  1. Scan my lists (export + compare with last run)")
    print("  2. Check related series not already in your lists")
    print("  3. Check Wish List for finished/cancelled series (ready to read)")
    print("  0. Exit\n")


def run_scan_lists(client: _ClientLike) -> None:
    """Option 1: export every list, save it, and diff it against the previous run."""
    start_time = time.time()

    lists = fetch_lists(client)
    if not lists:
        log.warning("No lists found for this account")
        return

    log.info("Exporting lists...")
    exports = export_all_lists(client, lists)

    log.info("Saving exports...")
    folder = save_exports(exports)
    log.info("Exports saved to: %s", folder)

    try:
        has_changes = compare_exports(folder, exports)
    finally:
        # The export is already on disk by this point. If the diff fails,
        # rotation must still run or exports/ grows past MAX_EXPORTS without
        # bound across repeated failures.
        rotate_exports()

    if not has_changes:
        log.info("Run ended with no changes since previous export.")

    elapsed = time.time() - start_time
    total_items = sum(len(items) for items in exports.values())
    log.info("")
    for line in _box(
        [
            term.title(f"  📊 Summary: {len(exports)} list(s), {total_items} item(s), in {elapsed:.1f}s"),
        ]
    ):
        log.info(line)


def run_related_check(client: _ClientLike) -> None:
    """Option 2: look up every tracked series' related series."""
    lists = fetch_lists(client)
    if not lists:
        log.warning("No lists found for this account")
        return

    log.info("Exporting lists...")
    exports = export_all_lists(client, lists)

    log.info("Checking related series...")
    related = collect_related_series(client, exports)
    related_path = save_related_series(related)
    log.info("Related series report saved to: %s (%d found)", related_path, len(related))


def run_finished_check(client: _ClientLike) -> None:
    """Option 3: find Wish List series that have finished releasing."""
    lists = fetch_lists(client)
    wish_lists = [lst for lst in lists if lst["title"] == "Wish List"]
    if not wish_lists:
        log.warning("No 'Wish List' found on this account — nothing to check")
        return
    if len(wish_lists) > 1:
        # export_all_lists already warns and keeps both when two lists share
        # a title; this path silently checked the first and ignored the rest,
        # so the same account state was reported two different ways.
        log.warning(
            "Found %d lists titled 'Wish List' (ids %s) — checking only the first (id %s)",
            len(wish_lists),
            ", ".join(str(lst["list_id"]) for lst in wish_lists),
            wish_lists[0]["list_id"],
        )
    wish_list = wish_lists[0]

    items = export_list(client, wish_list["list_id"], wish_list["title"])
    if not items:
        log.info("Wish List is empty — nothing to check")
        return

    log.info("Checking which Wish List series have finished releasing...")
    finished = find_finished_wishlist_series(client, items)
    path = save_finished_series(finished, len(items))
    log.info("Ready-to-read report saved to: %s (%d of %d found)", path, len(finished), len(items))


def main():
    print_header()

    with httpx.Client(timeout=30) as client:
        log.info("")
        log.info("Checking MangaUpdates API availability...")
        reachable = check_site_reachable(client)
        log.info(
            "  %s  api.mangaupdates.com — %s",
            "✓" if reachable else "✗",
            "reachable" if reachable else "UNREACHABLE",
        )
        if not reachable:
            log.error("Cannot reach the MangaUpdates API. Check your internet connection and try again.")
            return

        token = login(client)
        client.headers["Authorization"] = f"Bearer {token}"
        log.info("Logged in as: %s", USERNAME)

        actions = {"1": run_scan_lists, "2": run_related_check, "3": run_finished_check}
        try:
            while True:
                show_menu()
                try:
                    choice = input("Enter your choice (0-3): ").strip()
                except (EOFError, KeyboardInterrupt):
                    # Ctrl+C or a closed stdin at the prompt is a way of
                    # saying "done", not a crash worth a traceback.
                    print()
                    log.info("Goodbye!")
                    break

                if choice == "0":
                    log.info("Goodbye!")
                    break

                action = actions.get(choice)
                if action is None:
                    print("✗ Invalid choice. Please enter a number between 0 and 3.")
                    continue

                try:
                    action(client)
                except KeyboardInterrupt:
                    # Interrupt the operation, not the session -- the same
                    # thing Ctrl+C does at any other interactive prompt.
                    # Ctrl+C at the menu itself still exits.
                    print()
                    log.warning("Option %s interrupted by the user", choice)
                    print("  Stopped. Any partly written data has been discarded.")
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    # A failure inside one option used to propagate out of
                    # this loop and end the run, so a single bad response
                    # dropped the user back to the shell with a traceback and
                    # a session they would have to log in again to replace.
                    # The full traceback still goes to the log file.
                    log.error("Option %s failed: %s", choice, exc, exc_info=True)
                    print(f"\n✗ That option did not finish: {exc}")
                    print("  You are still logged in — pick another option, or 0 to quit.")
                    print(f"  Full detail is in {LOG_FILE}")
        finally:
            logout(client)

    log.info("Done!")


def _run_cli() -> int:
    """Run main() and turn every way it can end into a process exit code.

    Separate from main() so that this -- the part whose entire job is to
    behave well when something goes wrong -- is reachable from the tests.
    Inside `if __name__ == "__main__"` it was the one piece of the program
    that no test could execute.
    """
    # A fresh install has no .env anywhere, so write the template out rather than
    # leaving the user a filename to hunt for. Deliberately non-fatal: the
    # credential check further in reports what still needs filling in.
    created = ensure_env_file()
    if created:
        print("")
        print(term.danger("Created a credentials file at:"))
        print(f"    {created}")
        print("Fill in your details there, then run this again.")
        print("")
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl+C somewhere the menu loop could not catch it: during login, or
        # while shutting down. 130 is the conventional exit code for SIGINT.
        print()
        log.info("Interrupted.")
        return 130
    except SystemExit as exc:
        # login() and the credential check raise this deliberately and have
        # already explained themselves; keep whatever code they chose.
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Nothing should reach here -- the menu loop handles per-option
        # failures -- so if something does, say so plainly instead of ending
        # on a traceback, and keep the traceback in the log where it is useful.
        log.critical("Unexpected error: %s", exc, exc_info=True)
        print("\n" + term.danger(f"\u2717 Unexpected error: {exc}"))
        print(term.err(f"  This is a bug. Full detail is in {LOG_FILE}"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
