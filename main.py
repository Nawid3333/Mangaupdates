import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime

import httpx

from config.config import (
    API_BASE_URL,
    EXPORTS_DIR,
    ITEMS_PER_PAGE,
    MAX_EXPORTS,
    MAX_RETRIES,
    PASSWORD,
    RETRY_DELAY,
    SERIES_LOOKUP_WORKERS,
    USERNAME,
    setup_logging,
)

log = setup_logging()

# Terminal colors / styles
class _T:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


def _style(text: str, *codes: str) -> str:
    """Wrap text in ANSI style codes."""
    return "".join(codes) + text + _T.RESET


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so string length can be measured.

    The pattern is compiled once at import. It used to be re-imported and
    re-compiled on every call, and this runs for every line of every box.
    """
    return _ANSI_RE.sub("", text)


def _box(lines: list[str], width: int = 64) -> list[str]:
    """Return a list of box-drawn lines, accounting for ANSI codes."""
    out = []
    out.append("╔" + "═" * width + "╗")
    for line in lines:
        visible_len = len(_strip_ansi(line))
        padding = max(0, width - visible_len)
        out.append("║" + line + " " * padding + "║")
    out.append("╚" + "═" * width + "╝")
    return out


def _retry_delay(resp: httpx.Response | None) -> float:
    """Seconds to wait before the next attempt, honoring Retry-After if sent."""
    if resp is not None:
        raw_value = resp.headers.get("Retry-After", "")
        try:
            return max(float(raw_value), 0.0)
        except ValueError:
            pass
    return RETRY_DELAY


def _api_request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
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


def login(client: httpx.Client) -> str:
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

    data = resp.json()
    token = data.get("context", {}).get("session_token")
    if not token:
        log.error(
            "No session token in login response (status: %s)",
            data.get("status", "unknown"),
        )
        raise SystemExit(1)

    log.info("Login successful")
    return token


def check_site_reachable(client: httpx.Client) -> bool:
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


def logout(client: httpx.Client) -> None:
    """End the API session."""
    try:
        client.post(f"{API_BASE_URL}/account/logout")
        log.info("Logged out")
    except Exception as exc:
        log.warning("Logout failed: %s", exc)


def fetch_lists(client: httpx.Client) -> list[dict]:
    """Get all user lists (built-in + custom)."""
    log.info("Fetching user lists...")
    resp = _api_request(client, "get", f"{API_BASE_URL}/lists")
    resp.raise_for_status()
    lists = resp.json()
    log.info("Found %d list(s): %s", len(lists), ", ".join(lst["title"] for lst in lists))
    return lists


def export_list(client: httpx.Client, list_id: int, title: str) -> list[dict]:
    """Paginate through a single list and return all items."""
    all_items = []
    page = 1
    max_pages = 500  # Safety limit to prevent infinite loops

    while page <= max_pages:
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

        data = resp.json()
        results = data.get("results", [])
        total = data.get("total_hits", 0)

        all_items.extend(results)

        if len(all_items) >= total or not results:
            break
        page += 1
    else:
        log.warning("  %s: hit page limit (%d) – list may be incomplete", title, max_pages)

    log.info("  %s: %d item(s)", title, len(all_items))
    return all_items


def sanitize_filename(name: str) -> str:
    """Remove characters unsafe for filenames."""
    safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")
    return safe if safe else "Unnamed_List"


MANIFEST_NAME = "_manifest.json"


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
    used: set[str] = set()
    for title in titles:
        base = sanitize_filename(title)
        name = base
        counter = 2
        while name in used:
            name = f"{base}_{counter}"
            counter += 1
        used.add(name)
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
    folder_name = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
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

    filenames = export_filenames(list(exports.keys()))
    for title, items in exports.items():
        unique_title = filenames[title]
        file_path = os.path.join(tmp_folder_path, f"{unique_title}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        log.info("  Saved %s (%d items)", os.path.join(folder_path, f"{unique_title}.json"), len(items))

    manifest_path = os.path.join(tmp_folder_path, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(filenames, f, indent=2, ensure_ascii=False)

    os.replace(tmp_folder_path, folder_path)
    return folder_path


def get_series_ids(items: list[dict]) -> dict[int, str]:
    """Extract {series_id: title} from a list export."""
    result = {}
    for item in items:
        record = item.get("record", {})
        series = record.get("series", {})
        sid = series.get("id")
        title = series.get("title", "Unknown")
        if sid is not None:
            result[sid] = title
    return result


def get_series_basic(items: list[dict]) -> dict[int, dict]:
    """Extract {series_id: {"title", "url"}} from a list export."""
    result = {}
    for item in items:
        series = item.get("record", {}).get("series", {})
        sid = series.get("id")
        if sid is not None:
            result[sid] = {"title": series.get("title", "Unknown"), "url": series.get("url", "")}
    return result


def export_all_lists(client: httpx.Client, lists: list[dict]) -> dict[str, list[dict]]:
    """Export every list, guarding against two distinct lists sharing a title."""
    exports = {}
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
        exports[key] = export_list(client, list_id, title)
    return exports


# ==================== Related series ====================
def fetch_series_related(client: httpx.Client, series_id: int) -> list[dict] | None:
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
        return resp.json().get("related_series", [])
    except (httpx.HTTPError, ValueError) as exc:
        # httpx.HTTPError covers every transport/status failure _api_request
        # can raise; ValueError catches a malformed JSON body (json.JSONDecodeError
        # is a ValueError subclass). Either way this is one series failing to
        # look up, not a reason to abort the whole related-series pass -- the
        # 404 case above already gets the same treatment, this just closes the
        # same hole for every other way a single lookup can go wrong.
        log.warning("Could not fetch related series for id %s: %s", series_id, exc)
        return None


def collect_related_series(client: httpx.Client, exports: dict[str, list[dict]]) -> dict[int, dict]:
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=SERIES_LOOKUP_WORKERS) as pool:
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

    Always writes, even when nothing was found, so the file's absence never
    has to be read as "did this step run at all".
    """
    path = os.path.join(EXPORTS_DIR, "related.txt")
    lines = [
        f"Related series not already in your lists — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
        f"{len(related)} found",
        "=" * 68,
        "",
    ]

    if not related:
        lines.append("(none — every related series turned up is already in one of your lists)")
    else:
        for _rel_id, entry in sorted(related.items(), key=lambda kv: kv[1]["title"].lower()):
            source_bits = ", ".join(f'{rel_type} of "{origin}"' for origin, rel_type in entry["sources"])
            lines.append(entry["title"])
            lines.append(f"  {source_bits}")
            if entry["url"]:
                lines.append(f"  {entry['url']}")
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
def fetch_series_status(client: httpx.Client, series_id: int) -> dict | None:
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
        return {"completed": bool(data.get("completed", False)), "status": data.get("status", "") or ""}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Could not fetch status for id %s: %s", series_id, exc)
        return None


def find_finished_wishlist_series(client: httpx.Client, wish_items: list[dict]) -> dict[int, dict]:
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=SERIES_LOOKUP_WORKERS) as pool:
        future_to_series = {
            pool.submit(fetch_series_status, client, series_id): (series_id, info)
            for series_id, info in basic.items()
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


def save_finished_series(finished: dict[int, dict], total_checked: int) -> str:
    """Write the finished-Wish-List report to a single, stable path.

    Same stable-path, atomic-overwrite pattern as save_related_series: one
    fixed file (exports/ready_to_read.txt) that always holds the newest run,
    not one per timestamped export folder.
    """
    path = os.path.join(EXPORTS_DIR, "ready_to_read.txt")
    lines = [
        f"Wish List series that have finished releasing — {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
        f"{len(finished)} of {total_checked} found",
        "=" * 68,
        "",
    ]

    if not finished:
        lines.append("(none — nothing on your Wish List has finished releasing yet)")
    else:
        for _sid, entry in sorted(finished.items(), key=lambda kv: kv[1]["title"].lower()):
            lines.append(entry["title"])
            status_text = " / ".join(s.strip() for s in entry["status"].splitlines() if s.strip())
            if status_text:
                lines.append(f"  {status_text}")
            if entry["url"]:
                lines.append(f"  {entry['url']}")
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


def _parse_folder_date(name: str) -> datetime:
    """Parse a folder name into a datetime for sorting."""
    try:
        return datetime.strptime(name, "%d.%m.%Y_%H-%M-%S")
    except ValueError:
        return datetime.min


def find_previous_export(current_folder: str) -> str | None:
    """Find the most recent export folder before current_folder."""
    if not os.path.isdir(EXPORTS_DIR):
        return None

    current_name = os.path.basename(current_folder)
    current_dt = _parse_folder_date(current_name)
    folders = sorted(
        (
            d
            for d in os.listdir(EXPORTS_DIR)
            if os.path.isdir(os.path.join(EXPORTS_DIR, d)) and _parse_folder_date(d) < current_dt
        ),
        key=_parse_folder_date,
    )
    if folders:
        return os.path.join(EXPORTS_DIR, folders[-1])
    return None


def _load_prev_exports(prev_folder: str, titles: list[str]) -> dict[str, list[dict]]:
    """Load previous exports and return {list_title: raw items}.

    Reads the manifest once and returns the raw items rather than an
    already-reduced id map, so compare_exports can do both the movement scan
    and the per-list diff from one read of each file instead of two.
    """
    result: dict[str, list[dict]] = {}
    filenames = load_manifest(prev_folder, titles)
    for title in titles:
        prev_file = os.path.join(prev_folder, f"{filenames.get(title, sanitize_filename(title))}.json")
        if os.path.isfile(prev_file):
            try:
                with open(prev_file, encoding="utf-8") as f:
                    result[title] = json.load(f)
            except (json.JSONDecodeError, OSError):
                result[title] = []
    return result


def compare_exports(current_folder: str, exports: dict[str, list[dict]]) -> bool:
    """Compare current export with the previous one and print changes.

    Returns True if any changes were detected, False otherwise.
    """
    prev_folder = find_previous_export(current_folder)
    if not prev_folder:
        log.info("")
        for line in _box([
            _style("ℹ  No previous export found", _T.BOLD, _T.CYAN),
            _style("   Skipping comparison", _T.DIM),
        ]):
            log.info(line)
        return False

    prev_name = os.path.basename(prev_folder)
    log.info("")
    for line in _box([
        _style("  📋  Changes since last export (" + prev_name + ")", _T.BOLD, _T.BLUE),
    ]):
        log.info(line)

    # Load every previous list once; both the movement scan and the
    # per-list diff below read from this instead of the file a second time.
    all_titles = list(exports.keys())
    prev_by_list = _load_prev_exports(prev_folder, all_titles)
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
        log.info("  %s", _style("↔ Moved series", _T.BOLD, _T.YELLOW))
        for _sid, (name, old_list, new_list) in moved.items():
            log.info(
                "     %s %s  %s → %s",
                _style("↪", _T.YELLOW),
                name,
                _style(old_list, _T.DIM),
                _style(new_list, _T.CYAN),
            )
        log.info("")

    moved_sids = set(moved.keys())

    for title, current_items in exports.items():
        if title not in prev_by_list:
            log.info(
                "  %s  %s",
                _style("✱", _T.CYAN),
                _style(f"[{title}] NEW LIST (not in previous export) – {len(current_items)} item(s)", _T.CYAN),
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
                    _style("✓", _T.GREEN),
                    _style(f"[{title}] No changes", _T.GREEN),
                    _style(f"({len(current_items)} items)", _T.DIM),
                )
            else:
                has_changes = True
                sign = "+" if count_diff >= 0 else ""
                log.info(
                    "  %s  %s %d → %d (%s%d) %s",
                    _style("~", _T.YELLOW),
                    _style(f"[{title}]", _T.BOLD),
                    len(prev_items),
                    len(current_items),
                    _style(sign, _T.YELLOW),
                    count_diff,
                    _style("(movements only)", _T.DIM),
                )
            continue

        has_changes = True
        sign = "+" if count_diff >= 0 else ""
        log.info(
            "  %s  %s %d → %d (%s%d)",
            _style("✎", _T.YELLOW),
            _style(f"[{title}]", _T.BOLD),
            len(prev_items),
            len(current_items),
            _style(sign, _T.YELLOW),
            count_diff,
        )

        for sid in added_ids:
            log.info("     %s Added:   %s", _style("+", _T.GREEN), current_ids[sid])
        for sid in removed_ids:
            log.info("     %s Removed: %s", _style("-", _T.YELLOW), prev_ids[sid])

    # Check for lists that existed before but are now gone. Read the
    # previous folder's own manifest for this -- comparing against the
    # *current* titles' filenames would use the current run's dedup order,
    # which does not necessarily match the one the previous folder was
    # written with if list ordering changed between runs.
    prev_manifest = load_manifest(prev_folder, [])
    if not prev_manifest:
        prev_manifest = {
            f[:-5]: f[:-5] for f in os.listdir(prev_folder) if f.endswith(".json") and f != MANIFEST_NAME
        }
    # Read the manifest save_exports already wrote for this run, rather than
    # recomputing it -- one source of truth for what filenames this folder
    # actually uses.
    current_filenames = set(load_manifest(current_folder, list(exports.keys())).values())
    for prev_title, prev_filename in prev_manifest.items():
        if prev_filename not in current_filenames:
            has_changes = True
            log.info(
                "  %s  %s",
                _style("✗", _T.YELLOW),
                _style(f"[{prev_title}] LIST REMOVED (no longer exists)", _T.YELLOW),
            )

    if not has_changes:
        log.info("")
        for line in _box([
            _style("  ✅ NO CHANGES", _T.BOLD, _T.GREEN),
            _style("     All lists are identical to the previous export", _T.DIM),
        ]):
            log.info(line)
    else:
        log.info("")
        for line in _box([
            _style("  ⚠️  CHANGES DETECTED", _T.BOLD, _T.YELLOW),
            _style("     Review the details above", _T.DIM),
        ]):
            log.info(line)

    return has_changes


def rotate_exports() -> None:
    """Keep only the newest MAX_EXPORTS folders, delete the rest."""
    if not os.path.isdir(EXPORTS_DIR):
        return

    folders = sorted(
        [d for d in os.listdir(EXPORTS_DIR) if os.path.isdir(os.path.join(EXPORTS_DIR, d))],
        key=_parse_folder_date,
    )

    while len(folders) > MAX_EXPORTS:
        oldest = folders.pop(0)
        path = os.path.join(EXPORTS_DIR, oldest)
        try:
            shutil.rmtree(path)
            log.info("Deleted old export: %s", oldest)
        except OSError as exc:
            log.warning("Could not delete %s: %s", oldest, exc)


def print_header() -> None:
    log.info("=" * 60)
    log.info("  MANGAUPDATES LIST EXPORTER & TRACKER")
    log.info("=" * 60)


def show_menu() -> None:
    print("\nOptions:")
    print("  1. Scan my lists (export + compare with last run)")
    print("  2. Check related series not already in your lists")
    print("  3. Check Wish List for finished/cancelled series (ready to read)")
    print("  0. Exit\n")


def run_scan_lists(client: httpx.Client) -> None:
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

    has_changes = compare_exports(folder, exports)
    rotate_exports()

    if not has_changes:
        log.info("Run ended with no changes since previous export.")

    elapsed = time.time() - start_time
    total_items = sum(len(items) for items in exports.values())
    log.info("")
    for line in _box([
        _style(
            f"  📊 Summary: {len(exports)} list(s), {total_items} item(s), in {elapsed:.1f}s",
            _T.BOLD,
            _T.BLUE,
        ),
    ]):
        log.info(line)


def run_related_check(client: httpx.Client) -> None:
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


def run_finished_check(client: httpx.Client) -> None:
    """Option 3: find Wish List series that have finished releasing."""
    lists = fetch_lists(client)
    wish_list = next((lst for lst in lists if lst["title"] == "Wish List"), None)
    if wish_list is None:
        log.warning("No 'Wish List' found on this account — nothing to check")
        return

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

        try:
            while True:
                show_menu()
                choice = input("Enter your choice (0-3): ").strip()

                if choice == "1":
                    run_scan_lists(client)
                elif choice == "2":
                    run_related_check(client)
                elif choice == "3":
                    run_finished_check(client)
                elif choice == "0":
                    log.info("Goodbye!")
                    break
                else:
                    print("✗ Invalid choice. Please enter a number between 0 and 3.")
        finally:
            logout(client)

    log.info("Done!")


if __name__ == "__main__":
    main()
