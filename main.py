import concurrent.futures
import contextlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Protocol

import httpx
import lxml.html as lh

from config.config import (
    AP_EXPORTS_DIR,
    AP_USERNAME,
    API_BASE_URL,
    EXPORTS_DIR,
    ITEMS_PER_PAGE,
    LIST_PAGE_WORKERS,
    LOG_FILE,
    MAX_EXPORTS,
    MAX_RETRIES,
    MAX_RETRY_AFTER,
    PASSWORD,
    RETRY_DELAY,
    SERIES_LOOKUP_WORKERS,
    USERNAME,
    ensure_env_file,
    setup_logging,
)
from src import term
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
            value = float(raw_value)
            # float() also accepts "inf" and "nan". Neither is a wait anyone
            # can sit through -- time.sleep(nan) raises from inside the retry
            # handler -- so they count as malformed, like any other junk.
            if math.isfinite(value):
                base = max(value, 0.0)
    return base + random.uniform(0.0, RETRY_JITTER)


# Every failure _api_request tries again. A transport error used to be retried
# only if it was a timeout or a failed connect, so a connection the server
# dropped mid-answer -- ReadError, or RemoteProtocolError's "Server
# disconnected without sending a response", the usual end of a pooled
# keep-alive connection the server already closed -- aborted a whole list
# export, or silently lost one series' lookup, on the first occurrence.
# NetworkError covers connect/read/write/close. Deliberately not every
# TransportError: an unsupported protocol or a malformed request of our own
# fails the same way however often it is sent.
_RETRYABLE_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.HTTPStatusError,
)


def _api_request(client: _ClientLike, method: str, url: str, **kwargs) -> httpx.Response:
    """Make an API request with automatic retry on transient errors.

    429 is retried alongside 5xx and transport errors. It was not before:
    only status >= 500 triggered a retry, so a rate-limited response came
    straight back to the caller, whose raise_for_status() then crashed the
    whole run instead of backing off. Both cases honor a Retry-After header
    when the server sends one -- up to MAX_RETRY_AFTER; a server asking for
    longer than that fails the request at once instead of parking the run.
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
        except _RETRYABLE_ERRORS as exc:
            if attempt < MAX_RETRIES:
                delay = _retry_delay(getattr(exc, "response", None))
                # The jitter is ours, not the server's, so it does not count
                # against the limit.
                if delay > MAX_RETRY_AFTER + RETRY_JITTER:
                    log.error(
                        "Request failed: %s – the server asked to wait %.0fs, longer than the %ds limit; not retrying",
                        exc,
                        delay,
                        MAX_RETRY_AFTER,
                    )
                    raise
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


def _verify_page_total(
    items: list[dict], total: int, title: str, pages_fetched: int, site: str = "MangaUpdates"
) -> None:
    """Refuse to hand back a list shorter than the list said it was.

    A short export is the one failure this module must never pass on
    silently: it is saved as though it were complete, and the next run diffs
    against it and reports every item that was missing as removed from the
    account. _fetch_list_page already aborts rather than substitute a default
    for exactly that reason; this applies the same rule to the assembled
    result, which is where a shortfall actually becomes visible. Anime-Planet
    lists go through the same check (`site` only changes the wording).

    The page-limit case stays a warning, not an error -- there the range was
    knowingly clamped and the shortfall is expected.
    """
    if len(items) >= total:
        return
    if pages_fetched >= MAX_LIST_PAGES:
        log.warning("  %s: hit page limit (%d) – list may be incomplete", title, MAX_LIST_PAGES)
        return
    raise ValueError(
        f"{site} returned an incomplete list for '{title}': "
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
    # Control characters (tab, newline, ...) are as invalid in a Windows
    # filename as the punctuation is: a list title containing one reached
    # open() unchanged and the save died with OSError 22 -- after every list
    # had already been fetched.
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
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
    #
    # The manifest's own name starts out taken. A list titled "_manifest" was
    # given _manifest.json, and save_exports then wrote the manifest over it:
    # the list's items were gone from the snapshot, and every later diff read
    # the manifest back as that list's contents.
    used_ci: set[str] = {os.path.splitext(MANIFEST_NAME)[0].lower()}
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


class _ListIdentity(NamedTuple):
    """What a list *is*, as opposed to the key its export is stored under.

    The key is the title, suffixed " (2)" when two lists share one, and that
    suffix is positional: delete the first of two "Reading" lists and the
    second inherits the bare key. Diffing by key then compared the surviving
    list against the deleted one's file and reported every series in both as
    added and removed. The site's own id does not move like that.
    """

    list_id: Any
    title: str


class _ManifestEntry(NamedTuple):
    """One list as an export folder's manifest records it."""

    file: str
    # None when the folder does not know it: a manifest written before ids
    # were recorded, or a site (Anime-Planet) whose lists carry none. Unknown
    # is not the same as different -- such a list is matched by title.
    list_id: Any
    title: str


def _read_manifest(folder: str) -> dict[str, _ManifestEntry] | None:
    """Every list an export folder holds, keyed as it was exported, or None.

    Two shapes are understood. The first manifests mapped key -> filename and
    nothing else; folders written since store an object per list that also
    carries the list's id and real title. Reading both is what keeps every
    existing export comparable after the upgrade.

    None means there is no usable manifest at all -- a folder written before
    manifests existed, or one whose manifest cannot be read -- and the caller
    falls back to the filenames on disk.
    """
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as f:
            stored = json.load(f)
    except (ValueError, OSError):  # ValueError covers bad JSON and bad UTF-8 alike
        return None
    if not isinstance(stored, dict) or not stored:
        return None
    entries: dict[str, _ManifestEntry] = {}
    for key, value in stored.items():
        if isinstance(value, str):
            entries[key] = _ManifestEntry(value, None, key)
        elif isinstance(value, dict) and isinstance(value.get("file"), str):
            title = value.get("title")
            list_id = value.get("list_id")
            # An id is only usable as a matching key if it is a plain value;
            # anything else is treated as unknown rather than trusted.
            if isinstance(list_id, bool) or not isinstance(list_id, (int, str)):
                list_id = None
            entries[key] = _ManifestEntry(value["file"], list_id, title if isinstance(title, str) else key)
        else:
            # Half a manifest is worse than none: the entries it lost would
            # be guessed from filenames while the rest were trusted.
            return None
    return entries


def load_manifest(folder: str, titles) -> dict[str, str]:
    """Return the title -> filename mapping an export folder was written with.

    Prefers the manifest stored alongside the export, so a later change to
    the sanitizing rules can never silently repoint an old folder's files at
    the wrong list. Falls back to recomputing for folders written before
    manifests existed.
    """
    manifest = _read_manifest(folder)
    if manifest is None:
        return export_filenames(titles)
    return {key: entry.file for key, entry in manifest.items()}


def save_exports(
    exports: dict[str, list[dict]],
    exports_dir: str | None = None,
    identities: dict[str, _ListIdentity] | None = None,
) -> str:
    """Save each list to a timestamped folder. Returns the folder path.

    exports_dir scopes the snapshot tree: option 1 uses the default
    (MangaUpdates) folder and option 4 passes its own, so the two sites'
    exports never diff against each other and rotation stays per site.
    None reads config's EXPORTS_DIR at call time -- a bound default would
    have frozen the import-time path and ignored every test/override.

    identities, keyed like exports, records each list's id and real title in
    the manifest so the next comparison can follow a list across a rename or
    a shifted duplicate-title suffix. A list without one is stored with an
    unknown id and matched by title, exactly as before ids were recorded.
    """
    if exports_dir is None:
        exports_dir = EXPORTS_DIR
    # Two runs inside the same second produce the same folder name, and
    # os.replace onto an existing non-empty directory fails -- on Windows
    # with PermissionError. The run would then die *after* every list had
    # been fetched and written, losing all of it. Step the stamp forward
    # instead of adding a suffix, so the name still parses as a timestamp and
    # keeps working for ordering and rotation.
    stamp = datetime.now()
    folder_name = stamp.strftime(EXPORT_FOLDER_FORMAT)
    folder_path = os.path.join(exports_dir, folder_name)
    while os.path.exists(folder_path):
        stamp += timedelta(seconds=1)
        folder_name = stamp.strftime(EXPORT_FOLDER_FORMAT)
        folder_path = os.path.join(exports_dir, folder_name)

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
    if os.path.isdir(exports_dir):
        for entry in os.listdir(exports_dir):
            entry_path = os.path.join(exports_dir, entry)
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

    identities = identities or {}
    manifest = {}
    for title, filename in filenames.items():
        identity = identities.get(title, _ListIdentity(None, title))
        manifest[title] = {"file": filename, "list_id": identity.list_id, "title": identity.title}
    manifest_path = os.path.join(tmp_folder_path, MANIFEST_NAME)
    _write_json(manifest_path, manifest)

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


def _plan_list_keys(lists: list[dict]) -> list[tuple[str, Any, str]]:
    """Each list's storage key, in list order: [(key, list_id, title), ...].

    Keying `exports` by title alone would let the second list silently
    overwrite the first one's data if two distinct lists (different list_id)
    happen to share the same title, so a repeated title gets " (2)", " (3)".

    Pure and silent, so run_scan_lists can ask for the same keys again to
    record each list's id beside its export; export_all_lists does the
    warning.
    """
    plan: list[tuple[str, Any, str]] = []
    used_titles = set()
    for lst in lists:
        title = lst["title"]
        key = title
        counter = 2
        while key in used_titles:
            key = f"{title} ({counter})"
            counter += 1
        used_titles.add(key)
        plan.append((key, lst["list_id"], title))
    return plan


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
    plan = _plan_list_keys(lists)
    for key, list_id, title in plan:
        if key != title:
            log.warning(
                "Duplicate list title '%s' (list_id=%s) – storing under '%s' to avoid data loss",
                title,
                list_id,
                key,
            )

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


class _Lookups(NamedTuple):
    """What one batch of per-series lookups found, and how many it could not make.

    A lookup that failed used to vanish: the series was skipped, nothing was
    counted, and the report went on to say "0 related series found" or
    "0 finished out of 3 checked" -- reproduced with every lookup rate-limited,
    which overwrote a good report with a confident and wrong one. `unchecked`
    is what lets the report and the log say what was not looked at.
    """

    found: dict[int, dict]
    total: int  # series the batch set out to look up
    unchecked: int  # of those, how many came back with no answer (404 or failure)


def collect_related_series(client: _ClientLike, exports: dict[str, list[dict]]) -> _Lookups:
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

    Returns _Lookups whose `found` is {related_series_id: {"title", "url",
    "sources": [(origin_title, relation_type), ...]}}, already excluding
    anything you already have in any list and deduplicated across every
    series that pointed at it.
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
    unchecked = 0

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
                unchecked += 1
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

    return _Lookups(related, total, unchecked)


RELATED_REPORT_NAME = "related.txt"
FINISHED_REPORT_NAME = "ready_to_read.txt"


def _unchecked_line(unchecked: int) -> str:
    """The report-header line naming lookups that came back with no answer."""
    return f"  {unchecked} series could not be checked (lookup failed — see the log)"


def _may_replace_report(path: str, lookups: _Lookups) -> bool:
    """Whether this run's report may take the place of the one already at `path`.

    The reports live at one fixed path and every run overwrote it, so a run
    whose lookups failed replaced a good report with a worse one and said
    nothing. A complete run still replaces it without asking -- that is what
    the stable path is for -- and so does any run when there is nothing there
    yet. Otherwise:

    - every lookup failed: the new report knows nothing, so the previous one
      is kept and the user is told, without a question to answer;
    - some failed: the user decides. Only an explicit y replaces it; n, end
      of input or a run of unusable answers keeps the previous report.
    """
    if lookups.unchecked == 0 or not os.path.exists(path):
        return True
    if lookups.unchecked >= lookups.total:
        log.warning("Every lookup failed – keeping the previous report at %s unchanged", path)
        return False
    print(
        f"\n  {lookups.unchecked} of {lookups.total} series could not be checked, so this report is incomplete."
        f"\n  A previous report exists at {path}."
    )
    if term.confirm("Replace it with this incomplete report? (y/n): "):
        return True
    log.info("Kept the previous report at %s; this run's results were not saved", path)
    return False


def save_related_series(related: dict[int, dict], unchecked: int = 0) -> str:
    """Write the related-series report to a single, stable path.

    Every run overwrites the same file (exports/related.txt) rather than
    writing a new one into each timestamped export folder, so there is one
    fixed place to check and it always holds the newest data. The write is
    atomic -- built in a temp file, then swapped in with os.replace -- for
    the same reason save_exports uses the same pattern: a crash mid-write
    must never leave a half-written related.txt in place of a good one.

    `unchecked` series are named in the header, and an empty result no longer
    claims "every related series found is already in one of your lists" when
    some series were never looked at.

    The output uses the same box/card layout as save_finished_series so
    the two reports are visually consistent.
    """
    path = os.path.join(EXPORTS_DIR, RELATED_REPORT_NAME)
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    inner_width = FINISHED_REPORT_WIDTH - 4  # "║  ...  ║"

    lines: list[str] = [
        _CARD_TOP,
        "║" + _pad_finished_line(f"  Related Series — {now}") + "║",
        _CARD_MID,
        "║" + _pad_finished_line(f"  {len(related)} related series found") + "║",
    ]
    if unchecked:
        lines.append("║" + _pad_finished_line(_unchecked_line(unchecked)) + "║")
    lines += [_CARD_BOTTOM, ""]

    if not related and unchecked:
        lines.append("  (none found among the series that could be checked)")
    elif not related:
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


def find_finished_wishlist_series(client: _ClientLike, wish_items: list[dict]) -> _Lookups:
    """Check every series in the Wish List and return the ones that are finished.

    Concurrent across SERIES_LOOKUP_WORKERS threads, same pattern and same
    reasoning as collect_related_series: one request per series, the API
    does not charge for it, and _api_request's own backoff is the real
    safety net rather than a fixed pace.

    Returns _Lookups whose `found` is {series_id: {"title", "url", "status"}}.
    A lookup that failed is counted in `unchecked`, never read as "not
    finished".
    """
    basic = get_series_basic(wish_items)
    finished: dict[int, dict] = {}
    total = len(basic)
    done = 0
    unchecked = 0

    with _worker_pool(SERIES_LOOKUP_WORKERS) as pool:
        future_to_series = {
            pool.submit(fetch_series_status, client, series_id): (series_id, info) for series_id, info in basic.items()
        }
        for future in concurrent.futures.as_completed(future_to_series):
            series_id, info = future_to_series[future]
            done += 1
            result = future.result()
            log.info("  [%d/%d] Checked status for %s", done, total, info["title"])
            if result is None:
                unchecked += 1
                continue
            if not result["completed"]:
                continue
            finished[series_id] = {"title": info["title"], "url": info["url"], "status": result["status"]}

    return _Lookups(finished, total, unchecked)


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


def save_finished_series(finished: dict[int, dict], total_checked: int, unchecked: int = 0) -> str:
    """Write the finished-Wish-List report to a single, stable path.

    Same stable-path, atomic-overwrite pattern as save_related_series: one
    fixed file (exports/ready_to_read.txt) that always holds the newest run,
    not one per timestamped export folder. The output uses a box layout with
    one card per series so long status strings are readable.

    `total_checked` counts the series that were actually answered; the
    `unchecked` ones are named separately rather than folded into it.
    """
    path = os.path.join(EXPORTS_DIR, FINISHED_REPORT_NAME)
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    inner_width = FINISHED_REPORT_WIDTH - 4  # "║  ...  ║"

    lines: list[str] = [
        _CARD_TOP,
        "║" + _pad_finished_line(f"  Finished Wish List Series — {now}") + "║",
        _CARD_MID,
        "║" + _pad_finished_line(f"  {len(finished)} series finished out of {total_checked} checked") + "║",
    ]
    if unchecked:
        lines.append("║" + _pad_finished_line(_unchecked_line(unchecked)) + "║")
    lines += [_CARD_BOTTOM, ""]

    if not finished and unchecked:
        lines.append("  None of the Wish List series that could be checked have finished releasing yet.")
    elif not finished:
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


def _export_folders(exports_dir: str | None = None) -> list[str]:
    """Every export snapshot in exports_dir, oldest first.

    exports_dir defaults to config's EXPORTS_DIR, read at call time (a
    default-argument binding would have frozen the import-time value and
    silently ignored every override of EXPORTS_DIR).

    find_previous_export and rotate_exports each used to decide for
    themselves what counted, and both accepted *any* directory. That was
    wrong in two different ways: rotation counted an unrelated folder toward
    MAX_EXPORTS and deleted it first, because an unparseable name sorts as
    datetime.min and therefore looks like the oldest export there is; and a
    comparison would happily diff against it and report every series as new.
    Deciding it once, here, is the only way the two can agree.
    """
    if exports_dir is None:
        exports_dir = EXPORTS_DIR
    if not os.path.isdir(exports_dir):
        return []
    names = [
        name
        for name in os.listdir(exports_dir)
        if _is_export_folder(name) and os.path.isdir(os.path.join(exports_dir, name))
    ]
    return sorted(names, key=_parse_folder_date)


def find_previous_export(current_folder: str) -> str | None:
    """Find the most recent export folder before current_folder.

    Scopes the search to the current folder's own tree: each site has its
    own export base, and a MangaUpdates run must never be diffed against an
    Anime-Planet snapshot sitting alongside it.
    """
    current_dt = _parse_folder_date(os.path.basename(current_folder))
    exports_dir = os.path.dirname(current_folder)
    folders = [name for name in _export_folders(exports_dir) if _parse_folder_date(name) < current_dt]
    if folders:
        return os.path.join(exports_dir, folders[-1])
    return None


class _PreviousLists(NamedTuple):
    """How the previous export's lists line up with this run's."""

    # current key -> the file that held this same list last time
    files: dict[str, str]
    # current key -> the list's title last time, for a list renamed since
    renamed: dict[str, str]
    # previous key -> entry, for every previous list nothing current matched
    gone: dict[str, _ManifestEntry]


def _match_previous_lists(prev_folder: str, current: dict[str, _ManifestEntry]) -> _PreviousLists:
    """Pair each list of this run with the previous export's file for the same list.

    Lists used to be paired by title -- or rather by key and filename, with a
    sanitize_filename() guess for any title the previous manifest did not
    know. That went wrong three ways, all reproduced: deleting the first of
    two same-titled lists moved the second onto the bare key and diffed it
    against the deleted list's file; a case-only rename ("Sci-fi" ->
    "Sci-Fi") found the old file through NTFS's case-insensitivity and
    reported "No changes" and "LIST REMOVED" for the same list; and removed
    lists were detected by filename, so the survivor of two colliding titles
    could be the one reported gone.

    Now a list is matched by its site id where both sides know it, then by
    key only where at least one side does not -- an older manifest, or a site
    without ids -- and never across two known, different ids. A key the
    previous manifest does not list is a new list, not a filename to guess.
    """
    previous = _read_manifest(prev_folder)
    if previous is None:
        # A folder from before manifests existed: no titles, no ids, only
        # files. It was written with the filenames this run derives from the
        # same titles, so pair on those, as it always was.
        stems = [name[:-5] for name in os.listdir(prev_folder) if name.endswith(".json") and name != MANIFEST_NAME]
        derived = export_filenames(list(current))
        files = {key: derived[key] for key in current if derived[key] in stems}
        paired = set(files.values())
        gone = {stem: _ManifestEntry(stem, None, stem) for stem in sorted(stems) if stem not in paired}
        return _PreviousLists(files, {}, gone)

    files: dict[str, str] = {}
    renamed: dict[str, str] = {}
    claimed: set[str] = set()

    by_id = {entry.list_id: prev_key for prev_key, entry in previous.items() if entry.list_id is not None}
    for key, entry in current.items():
        prev_key = by_id.get(entry.list_id) if entry.list_id is not None else None
        if prev_key is None or prev_key in claimed:
            continue
        files[key] = previous[prev_key].file
        claimed.add(prev_key)
        # Only an id match can tell a rename from a new list. The key cannot:
        # an older manifest recorded no real title, so "Reading (2)" would
        # read as renamed from itself the first time it was compared.
        if previous[prev_key].title != entry.title:
            renamed[key] = previous[prev_key].title

    for key, entry in current.items():
        if key in files or key in claimed or key not in previous:
            continue
        if entry.list_id is not None and previous[key].list_id is not None:
            continue  # same key, two different lists: the old one is gone
        files[key] = previous[key].file
        claimed.add(key)

    gone = {prev_key: entry for prev_key, entry in previous.items() if prev_key not in claimed}
    return _PreviousLists(files, renamed, gone)


def _load_prev_exports(prev_folder: str, files: dict[str, str]) -> tuple[dict[str, list[dict]], set[str]]:
    """Load previous exports. Returns ({list_key: raw items}, unreadable keys).

    `files` maps each list to the file that held it in prev_folder -- what
    _match_previous_lists worked out, or load_manifest for a folder read
    back on its own. Returns the raw items rather than an already-reduced id
    map, so compare_exports can do both the movement scan and the per-list
    diff from one read of each file instead of two.

    A file that exists but cannot be parsed used to be substituted with an
    empty list, which made a corrupted export indistinguishable from a list
    that genuinely had nothing in it: every series in it came back reported
    as newly Added. That is the worst kind of wrong answer here, because it
    looks exactly like a real account change. Those keys are named
    separately now so the caller can say it does not know, instead of
    guessing -- and so is a file the manifest names but that is missing.
    """
    result: dict[str, list[dict]] = {}
    unreadable: set[str] = set()
    for key, filename in files.items():
        prev_file = os.path.join(prev_folder, f"{filename}.json")
        try:
            with open(prev_file, encoding="utf-8") as f:
                result[key] = json.load(f)
        except (ValueError, OSError) as exc:  # ValueError covers bad JSON and bad UTF-8 alike
            log.warning("Could not read '%s' from the previous export: %s", key, exc)
            unreadable.add(key)
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

    # Which previous list is which current list. Read from the manifest
    # save_exports already wrote for this run -- one source of truth for the
    # ids and files this folder actually uses.
    current_manifest = _read_manifest(current_folder) or {}
    current = {key: current_manifest.get(key, _ManifestEntry("", None, key)) for key in exports}
    match = _match_previous_lists(prev_folder, current)

    # Load every previous list once; both the movement scan and the
    # per-list diff below read from this instead of the file a second time.
    # Lists that are gone are loaded too: a series that moved out of a list
    # which no longer exists was reported as newly Added, because only the
    # lists still present were ever read.
    prev_by_list, unreadable = _load_prev_exports(prev_folder, match.files)
    gone_by_list, _gone_unreadable = _load_prev_exports(prev_folder, {k: e.file for k, e in match.gone.items()})
    prev_ids_by_list = {title: get_series_ids(items) for title, items in prev_by_list.items()}
    gone_ids_by_list = {title: get_series_ids(items) for title, items in gone_by_list.items()}
    # A gone list's key can equal a current one -- the survivor of two
    # same-titled lists inherits the bare title -- so where both exist the
    # gone one is named by its id, in moves and in the removal line alike.
    gone_label = {
        key: f"{key} (list_id {entry.list_id})" if key in exports and entry.list_id is not None else key
        for key, entry in match.gone.items()
    }

    # get_series_ids(items) used to be called separately for the movement
    # scan, again per moved series (re-scanning every list from scratch to
    # find its name), and again in the diff loop below -- up to 3x over the
    # same list. Computed once here and reused everywhere.
    cur_ids_by_list = {title: get_series_ids(items) for title, items in exports.items()}

    # Where each series was last time: (list name, whether that list is gone).
    # The flag, not the name, says a gone list was left: its name can equal a
    # current list's, and a name comparison alone would then miss the move.
    prev_sid_to_list: dict[int, tuple[str, bool]] = {}
    for list_title, ids in gone_ids_by_list.items():
        for sid in ids:
            prev_sid_to_list[sid] = (gone_label[list_title], True)
    for list_title, ids in prev_ids_by_list.items():
        for sid in ids:
            prev_sid_to_list[sid] = (list_title, False)

    cur_sid_to_list: dict[int, str] = {}
    cur_sid_to_name: dict[int, str] = {}
    for list_title, ids in cur_ids_by_list.items():
        for sid, name in ids.items():
            cur_sid_to_list[sid] = list_title
            cur_sid_to_name[sid] = name

    # Detect movements (series that changed lists)
    moved: dict[int, tuple[str, str, str]] = {}  # sid -> (title, old_list, new_list)
    for sid, new_list in cur_sid_to_list.items():
        old = prev_sid_to_list.get(sid)
        if old is None:
            continue
        old_list, old_list_gone = old
        if old_list_gone or old_list != new_list:
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
        if title in match.renamed:
            has_changes = True
            log.info(
                "  %s  %s",
                term.accent("✎"),
                term.accent(f"[{title}] renamed (was '{match.renamed[title]}')"),
            )

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

    # Lists that existed before and matched nothing this time. The series
    # they held are listed, less any that moved to a list still present --
    # those were reported as moves above.
    for prev_title in match.gone:
        has_changes = True
        label = gone_label[prev_title]
        gone_ids = gone_ids_by_list.get(prev_title)
        if gone_ids is None:
            log.info("  %s  %s", term.warn("✗"), term.warn(f"[{label}] LIST REMOVED (no longer exists)"))
            continue
        left = {sid: name for sid, name in gone_ids.items() if sid not in moved_sids}
        log.info(
            "  %s  %s",
            term.warn("✗"),
            term.warn(f"[{label}] LIST REMOVED (no longer exists) – {len(gone_ids)} item(s)"),
        )
        for sid in sorted(left, key=lambda s: (left[s].lower(), s)):
            log.info("     %s Removed: %s", term.warn("-"), left[sid])

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


def rotate_exports(exports_dir: str | None = None) -> None:
    """Keep only the newest MAX_EXPORTS folders, delete the rest.

    None reads config's EXPORTS_DIR at call time, matching _export_folders
    and save_exports -- a bound default would freeze the import-time path.
    """
    if exports_dir is None:
        exports_dir = EXPORTS_DIR
    if not os.path.isdir(exports_dir):
        return

    # Only this program's own snapshots. Anything else in exports_dir -- a
    # folder the user put there, one from another tool -- is not ours to
    # count or delete.
    folders = _export_folders(exports_dir)

    while len(folders) > MAX_EXPORTS:
        oldest = folders.pop(0)
        path = os.path.join(exports_dir, oldest)
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
    print("  4. Scan my Anime-Planet lists (export + compare with last run)")
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
    # The same keys export_all_lists stored the lists under, with each one's
    # id, so the next run can tell a list apart from another of the same name.
    identities = {key: _ListIdentity(list_id, title) for key, list_id, title in _plan_list_keys(lists)}
    folder = save_exports(exports, identities=identities)
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
    lookups = collect_related_series(client, exports)
    _report_unchecked(lookups)
    path = os.path.join(EXPORTS_DIR, RELATED_REPORT_NAME)
    if not _may_replace_report(path, lookups):
        return
    related_path = save_related_series(lookups.found, lookups.unchecked)
    log.info("Related series report saved to: %s (%d found)", related_path, len(lookups.found))


def _report_unchecked(lookups: _Lookups) -> None:
    """Say in the log how many lookups came back with no answer, if any did.

    Each failure is already logged as it happens, but one line per series
    scrolls away in a run of hundreds; this is the count that stays visible.
    """
    if lookups.unchecked:
        log.warning(
            "%d of %d series could not be checked (lookup failed — see the warnings above)",
            lookups.unchecked,
            lookups.total,
        )


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
    lookups = find_finished_wishlist_series(client, items)
    _report_unchecked(lookups)
    path = os.path.join(EXPORTS_DIR, FINISHED_REPORT_NAME)
    if not _may_replace_report(path, lookups):
        return
    # "Out of N checked" counts the series that were answered. It used to be
    # len(items), which counted a failed lookup as checked and not finished.
    checked = lookups.total - lookups.unchecked
    path = save_finished_series(lookups.found, checked, lookups.unchecked)
    log.info("Ready-to-read report saved to: %s (%d of %d found)", path, len(lookups.found), checked)


# ==================== Anime-Planet ====================

AP_BASE_URL = "https://www.anime-planet.com"
# Manga and anime each get their own status vocabulary on the site, and for
# ten of these twelve lists that vocabulary is already distinct ("Read" vs
# "Watched", "Won't Read" vs "Won't Watch", ...). "Stalled" and "Dropped" are
# the two exceptions -- the site itself uses the identical word for both
# content types. _ap_export_all_lists keys its `exports` dict by this label,
# so leaving them identical here meant a non-empty manga/stalled and a
# non-empty anime/stalled silently collapsed into one entry, discarding
# whichever list was read first -- a live data-loss bug, not a hypothetical
# one. Prefixed here so the two stay distinct the same way every other pair
# already is.
AP_LIST_TYPES = {
    "manga/read": "Read",
    "manga/reading": "Reading",
    "manga/wanttoread": "Want to Read",
    "manga/stalled": "Manga Stalled",
    "manga/dropped": "Manga Dropped",
    "manga/wontread": "Won't Read",
    "anime/watched": "Watched",
    "anime/watching": "Watching",
    "anime/wanttowatch": "Want to Watch",
    "anime/stalled": "Anime Stalled",
    "anime/dropped": "Anime Dropped",
    "anime/wontwatch": "Won't Watch",
}


class _AnimePlanetClient:
    """Minimal httpx wrapper for anonymous Anime-Planet requests."""

    _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

    def __init__(self) -> None:
        # Deliberately NOT the shared main() client: that one carries the
        # MangaUpdates session, and httpx merges client-level headers into
        # every request, so the "Authorization: Bearer ..." set after login
        # went to anime-planet.com too -- which answered 401 Unauthorized.
        # (Setting a header to None in per-request headers, httpx's documented
        # way to *remove* one, is rejected by this version with
        # "Header value must be str or bytes".) A dedicated client has no
        # MangaUpdates state to leak, and its cookie jar stays Anime-Planet's.
        self.client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": self._UA},
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{AP_BASE_URL}{path}" if path.startswith("/") else f"{AP_BASE_URL}/{path}"
        # Through the same retry as every MangaUpdates request. This went out
        # bare, so one 429 from the site -- or a dropped connection, with up
        # to LIST_PAGE_WORKERS pages in flight -- ended option 4 at once.
        return _api_request(self.client, "get", url, params=params)

    def close(self) -> None:
        self.client.close()


def _ap_user_profile_path(username: str) -> str:
    if not username:
        raise ValueError("Anime-Planet username is not configured")
    return f"/users/{username}"


def _ap_list_path(username: str, list_type: str) -> str:
    return f"/users/{username}/{list_type}"


def _ap_profile_error(position: int, detail: str) -> ValueError:
    """A profile list entry that cannot be read, said in a way that names it."""
    return ValueError(
        f"Anime-Planet profile list entry {position} {detail}. Nothing was saved — exporting "
        "without that list would make the next run report everything in it as removed."
    )


def _ap_parse_profile_list_counts(page_html: str) -> list[tuple[str, str, str, int]]:
    """Parse the profile statLists and return every list they count.

    The profile carries one statList per section (manga, anime); reading only
    the first would have silently dropped every anime list of an account
    with both. Returns tuples of (list_type, relative_url, label, count).

    Empty lists are returned too, with a count of 0. They used to be left
    out, so a list emptied since the last run -- the last manga in Reading
    finished and moved to Read -- vanished from the export: the diff called
    it "LIST REMOVED (no longer exists)" and reported the move as an Add.

    A list entry whose link or count cannot be read stops the parse with a
    ValueError instead of being skipped. Skipping left that list out of the
    export with nothing on screen, and the next comparison reported it, and
    every title in it, as removed -- the same reason fetch_lists refuses a
    malformed MangaUpdates list index. The label span is not required: the
    label comes from AP_LIST_TYPES or the URL, never from the page.
    """
    results: list[tuple[str, str, str, int]] = []
    if not page_html or not page_html.strip():
        return results
    doc = lh.fromstring(page_html)
    items = [
        li
        for stat_list in doc.xpath('//ul[contains(@class, "statList")]')
        for li in stat_list.xpath('.//li[contains(@class, "status")]')
    ]
    for position, item in enumerate(items, 1):
        link = item.xpath("./a")
        href = link[0].get("href", "") if link else ""
        if not href:
            raise _ap_profile_error(position, "has no link to its list")
        count_text = link[0].xpath('.//span[@class="slCount"]/text()')
        if not count_text:
            raise _ap_profile_error(position, f"({href}) shows no item count")
        # Anime-Planet formats counts in the thousands with a comma
        # ("1,234"); int() rejects that and would silently drop the list.
        try:
            count = int(count_text[0].strip().replace(",", ""))
        except ValueError:
            raise _ap_profile_error(
                position, f"({href}) has an unreadable item count {count_text[0].strip()!r}"
            ) from None
        if count < 0:
            raise _ap_profile_error(position, f"({href}) has a negative item count ({count})")
        # Map the URL tail back to the canonical list type key.
        key = href.lstrip("/")
        if key.startswith("users/"):
            key = key.split("/", 2)[-1]
        if key not in AP_LIST_TYPES:
            # Unknown list URL: keep it so a future site change is visible,
            # but derive a readable label from the URL if needed.
            derived_label = key.replace("/", " ").replace("-", " ").title()
            results.append((key, href, derived_label, count))
        else:
            results.append((key, href, AP_LIST_TYPES[key], count))
    return results


# Anime-Planet numbers manga and anime entries from two separate sequences,
# not one shared id space -- confirmed live: Berserk the manga is id 14,
# Berserk the anime is id 61, unrelated numbers for the same franchise. Every
# id merge downstream of this module (compare_exports' cross-list movement
# scan, get_series_ids/get_series_basic) assumes one global id space, because
# that is what MangaUpdates -- the only other source feeding the same code --
# genuinely has. Left alone, a manga id that happens to equal some unrelated
# anime id would make compare_exports think a title "moved" from a manga list
# to an anime list, and could mask the real add/remove on each side behind
# that bogus move. Offsetting the anime id space keeps the "one global id
# space" assumption true for Anime-Planet too. The exported "id" is no longer
# the raw site id for anime entries because of this, but "url" is untouched,
# so the real page is always one click away.
_AP_ANIME_ID_OFFSET = 1_000_000_000


def _ap_parse_list_entries(page_html: str) -> list[dict]:
    """Extract entries from an Anime-Planet list page HTML.

    Each entry is shaped exactly like a MangaUpdates list item
    ("record.series") so save_exports, compare_exports, and every
    id/title/url extraction built for option 1 work on it unchanged.
    """
    entries: list[dict] = []
    if not page_html or not page_html.strip():
        return entries
    doc = lh.fromstring(page_html)
    cards = [
        card
        for deck in doc.xpath('//ul[contains(@class, "cardDeck") and contains(@class, "cardGrid")]')
        for card in deck.xpath("./li[@data-type and @data-id]")
    ]
    for card in cards:
        entry_id = card.get("data-id", "")
        title_el = card.xpath(".//h3[@class='cardName']")
        title = title_el[0].text_content().strip() if title_el else ""
        if not entry_id or not title:
            continue
        try:
            numeric_id = int(entry_id)
        except ValueError:
            continue
        if card.get("data-type") == "anime":
            numeric_id += _AP_ANIME_ID_OFFSET
        # 'pl0' used to be demanded alongside 'tooltip', but the live cards
        # carry only "tooltip manga<N>" -- so every link lookup missed and
        # every exported URL came back empty. Match the class the site
        # actually sends.
        link = card.xpath(".//a[contains(@class, 'tooltip')]")
        path = link[0].get("href", "") if link else ""
        # A relative href gets the site prefix; an absolute one is kept as
        # it came instead of being welded onto AP_BASE_URL.
        url = f"{AP_BASE_URL}{path}" if path.startswith("/") else path
        entries.append({"record": {"series": {"id": numeric_id, "title": title, "url": url}}})
    return entries


# The page size asked for, and the one the site serves when it ignores
# per_page -- its own default, which is also why per_page is left off the URL
# when it equals this.
_AP_PER_PAGE = 560
_AP_SITE_PER_PAGE = 35


def _ap_fetch_list_page(
    ap_client: _AnimePlanetClient, username: str, list_type: str, page: int, per_page: int
) -> list[dict]:
    """Fetch a single page of an Anime-Planet list."""
    params = {}
    if page > 1:
        params["page"] = page
    if per_page != _AP_SITE_PER_PAGE:
        params["per_page"] = per_page
    resp = ap_client.get(_ap_list_path(username, list_type), params=params)
    resp.raise_for_status()
    return _ap_parse_list_entries(resp.text)


def _ap_pages_needed(count: int, per_page: int) -> int:
    if count <= 0:
        return 0
    return max(1, (count + per_page - 1) // per_page)


def _ap_unique_entries(pages: list[list[dict]]) -> list[dict]:
    """Concatenate pages in order, keeping the first copy of each id.

    A page asked for past the real end can come back as a repeat of an
    earlier one rather than empty. Counted twice, those repeats could make a
    short list look complete; a list holds each title once, so a repeated id
    is never a second entry.
    """
    seen: set[Any] = set()
    entries: list[dict] = []
    for page in pages:
        for entry in page:
            series = _extract_series(entry)
            sid = series.get("id") if series else None
            if sid in seen:
                continue
            seen.add(sid)
            entries.append(entry)
    return entries


def _ap_fetch_all_list_entries(
    ap_client: _AnimePlanetClient, username: str, list_type: str, count: int, label: str | None = None
) -> list[dict]:
    """Fetch every page of an Anime-Planet list and return the entries.

    Page 1 is fetched alone, because its length is the page size the site
    actually serves. The page count used to be worked out from the 560 that
    was asked for, so a site serving fewer per page left every entry past
    page 1's worth out of the export -- with only a warning, while the short
    list was saved as complete and the next diff reported the rest removed.

    A page 1 shorter than the site's own default page while the list holds
    more cannot be a page-size cap -- the page is broken (a bot-check or
    private-list page, or cards that no longer parse) -- so nothing more is
    fetched. Either way, a list still short of the profile's count raises
    through _verify_page_total, the same rule MangaUpdates lists follow.
    """
    if count <= 0:
        return []
    first = _ap_fetch_list_page(ap_client, username, list_type, 1, _AP_PER_PAGE)
    page_size = len(first)
    pages = 1
    later: list[list[dict]] = []
    if _AP_SITE_PER_PAGE <= page_size < count:
        pages = min(MAX_LIST_PAGES, _ap_pages_needed(count, page_size))
        jobs = list(range(2, pages + 1))
        with _page_pool(len(jobs)) as pool:
            # pool.map keeps page order, the way export_list assembles
            # MangaUpdates pages; the earlier as_completed loop appended in
            # completion order, so a large list's items landed in the export
            # in a different order every run and the diffs read as huge
            # spurious changes.
            later = list(
                pool.map(lambda page: _ap_fetch_list_page(ap_client, username, list_type, page, _AP_PER_PAGE), jobs)
            )
    entries = _ap_unique_entries([first, *later])
    _verify_page_total(entries, count, label or list_type, pages, site="Anime-Planet")
    return entries


def _ap_export_all_lists(ap_client: _AnimePlanetClient, username: str) -> dict[str, list[dict]]:
    """Export every Anime-Planet list the user's profile counts, empty ones included."""
    log.info("Fetching Anime-Planet profile for '%s'...", username)
    profile_resp = ap_client.get(_ap_user_profile_path(username))
    if profile_resp.status_code == 404:
        # Said plainly: this used to surface as "Could not reach Anime-Planet",
        # which sent the user looking at their connection, not at a typo.
        raise ValueError(f"Anime-Planet has no user named '{username}' — check AP_USERNAME in your .env")
    profile_resp.raise_for_status()
    list_infos = _ap_parse_profile_list_counts(profile_resp.text)
    # Every list empty is treated as nothing to export, as it was before empty
    # lists were kept: a profile page reading all zeros is far more likely a
    # page that did not show the counts than an account emptied overnight,
    # and saving it would report every title as removed.
    if not any(count for _key, _href, _label, count in list_infos):
        log.warning("No non-empty Anime-Planet lists found for '%s'", username)
        return {}

    # _ap_parse_profile_list_counts walks the profile's statList in document
    # order, so this summary reads top-to-bottom in the order the site itself
    # shows the lists -- the same "Found N list(s): titles" shape as
    # fetch_lists for MangaUpdates lists.
    log.info(
        "Found %d non-empty list(s): %s",
        sum(1 for _key, _href, _label, count in list_infos if count),
        ", ".join(label for _key, _href, label, count in list_infos if count),
    )

    log.info("Exporting lists...")
    exports: dict[str, list[dict]] = {}
    used_labels: set[str] = set()
    for list_type, _href, label, count in list_infos:
        # Keying `exports` by label alone would let a second list silently
        # overwrite the first one's data if two distinct list_types (e.g. a
        # manga and an anime list) ever produce the same label -- exactly
        # what "Stalled"/"Dropped" used to do before AP_LIST_TYPES gave the
        # manga and anime versions distinct names. Guarded the same way
        # export_all_lists guards MangaUpdates' own duplicate-title case, in
        # case a future site change (or an unrecognised-URL derived label)
        # reintroduces a collision another way.
        key = label
        counter = 2
        while key in used_labels:
            key = f"{label} ({counter})"
            counter += 1
        if key != label:
            log.warning(
                "Duplicate Anime-Planet list label '%s' (list_type=%s) – storing under '%s' to avoid data loss",
                label,
                list_type,
                key,
            )
        used_labels.add(key)

        # A shortfall raises inside, and the whole run stops before anything
        # is saved -- the previous export stays the one the next run compares
        # against. An empty list costs no request and is kept as [].
        items = _ap_fetch_all_list_entries(ap_client, username, list_type, count, label)
        log.info("  %s: %d item(s)", label, len(items))
        exports[key] = items
    return exports


def run_anime_planet_scan(client: _ClientLike) -> None:
    """Option 4: export Anime-Planet lists and diff against previous run."""
    if not AP_USERNAME:
        log.error("AP_USERNAME is not set in .env — configure it to use Option 4")
        return

    start_time = time.time()
    ap_client = _AnimePlanetClient()
    try:
        exports = _ap_export_all_lists(ap_client, AP_USERNAME)
    except httpx.HTTPStatusError as exc:
        # The site answered, so "could not reach" would point the wrong way.
        log.error("Anime-Planet answered HTTP %d for %s", exc.response.status_code, exc.request.url)
        return
    except httpx.HTTPError as exc:
        log.error("Could not reach Anime-Planet: %s", exc)
        return
    finally:
        ap_client.close()

    if not exports:
        log.warning("No Anime-Planet lists to export")
        return

    log.info("Saving exports...")
    folder = save_exports(exports, AP_EXPORTS_DIR)
    log.info("Exports saved to: %s", folder)

    try:
        has_changes = compare_exports(folder, exports)
    finally:
        rotate_exports(AP_EXPORTS_DIR)

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


def _session_rejected(exc: BaseException) -> bool:
    """Whether MangaUpdates answered 401: it no longer accepts the session token."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401


def _run_option(client: httpx.Client, action: Callable[[httpx.Client], None]) -> None:
    """Run one menu option, logging in again once if the session was rejected.

    The token from login() was set once and used for the whole session, so a
    menu left open long enough for it to lapse failed every option after
    that -- each with "You are still logged in" printed under it. A 401 now
    gets one fresh login and one more attempt. Every option only reads from
    MangaUpdates and saves atomically, so running it again is safe.
    """
    try:
        action(client)
    except httpx.HTTPStatusError as exc:
        if not _session_rejected(exc):
            raise
        log.warning("MangaUpdates no longer accepts this session (HTTP 401) – logging in again and retrying once")
        # The stale token must not ride along on the login request itself.
        client.headers.pop("Authorization", None)
        client.headers["Authorization"] = f"Bearer {login(client)}"
        action(client)


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

        actions = {
            "1": run_scan_lists,
            "2": run_related_check,
            "3": run_finished_check,
            "4": run_anime_planet_scan,
        }
        try:
            while True:
                show_menu()
                try:
                    # Only the offered numbers are answers; anything else is
                    # asked again. A closed stdin, or a run of unusable
                    # answers, gives "0" -- the choice that changes nothing.
                    choice = term.ask(
                        "Enter your choice (0-4): ",
                        ("0", *actions),
                        safe="0",
                        hint="type a number between 0 and 4",
                    )
                except KeyboardInterrupt:
                    # Ctrl+C at the prompt is a way of saying "done", not a
                    # crash worth a traceback.
                    print()
                    log.info("Goodbye!")
                    break

                if choice == "0":
                    log.info("Goodbye!")
                    break

                try:
                    _run_option(client, actions[choice])
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
                    if _session_rejected(exc):
                        # _run_option already logged in again once; a second
                        # rejection is not something another try will fix.
                        print("  MangaUpdates rejected the session even after logging in again.")
                        print("  Enter 0 to quit, then start the program again.")
                    else:
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
