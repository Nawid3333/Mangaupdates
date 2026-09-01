import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

# ==================== PROJECT HOME ====================
# Every path this program reads or writes -- .env, exports/, logs/ -- hangs off
# one directory, so there is a single thing to point somewhere else.
#
# Unset, it resolves to the repo checkout exactly as it always has, so running
# from a clone is byte-for-byte unchanged. MU_HOME overrides it, which is what
# makes an installed copy usable: in a venv this file sits inside
# site-packages, where no user can reasonably find a .env to edit.
_DEFAULT_HOME = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BASE_DIR = os.path.abspath(os.environ.get("MU_HOME") or _DEFAULT_HOME)

# Load environment variables from the project home .env
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)

# Credentials
USERNAME = os.getenv("MU_USERNAME", "")
PASSWORD = os.getenv("MU_PASSWORD", "")

# Paths
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# Ensure directories exist
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# API
API_BASE_URL = "https://api.mangaupdates.com/v1"

# Export settings
MAX_EXPORTS = 3  # Number of dated export folders to keep
ITEMS_PER_PAGE = 100  # Items per API page request

# Retry settings
MAX_RETRIES = 3  # Number of retry attempts for API requests
RETRY_DELAY = 5  # Seconds between retries

# Related-series lookup settings
# One GET /series/{id} request is made per unique series in your lists, run
# concurrently across this many worker threads. There is no fixed per-request
# delay: MangaUpdates does not charge for this endpoint, and _api_request
# already backs off on real pushback (429, honoring Retry-After) rather than
# guessing a safe pace up front. Raise this for a faster run, lower it to be
# gentler on a very large account.
#
# Benchmarked live against the real API on 100 real series ids, each level
# measured 3 times in shuffled order so ordering drift could not fake a
# winner (median req/s): 8 -> 30.3, 10 -> 31.1, 12 -> 40.1, 16 -> 46.2,
# 20 -> 48.1. 16 is where the curve flattens; 20 buys 4% more for 25% more
# concurrent load.
#
# Reliability was the gate, not throughput. Across 1,500 requests the only
# two non-200s were single transient 503s -- one at 20 workers and one at 10,
# the level this was raised *from* -- so they are background noise, not the
# server pushing back at a threshold, and _api_request already retries 5xx.
# An earlier sweep over 150 ids agreed: 32 workers ran clean, 16 ran clean,
# and the single 503 that turned up landed at 24 -- after 32 had already
# passed, which is not how a load threshold behaves.
# Do not raise this further without re-running that measurement.
SERIES_LOOKUP_WORKERS = 16

# List-page settings
# Page 1 of a list reports total_hits, so the whole page range is known after
# one round trip; every list's first page is fetched at once and then every
# remaining page at once (export_all_lists). This caps how many of those page
# requests are in flight.
#
# Kept well below SERIES_LOOKUP_WORKERS on purpose: a page response carries
# up to ITEMS_PER_PAGE full records (~35 KB each here) against ~11 KB for a
# single series, and real accounts need only a handful of pages -- 10 for a
# 635-item, 5-list account, which 8 already clears in two round trips.
LIST_PAGE_WORKERS = 8

# Logging setup
LOG_FILE = os.path.join(LOGS_DIR, "mangaupdates_export.log")


def setup_logging():
    logger = logging.getLogger("mu_export")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    # Rotating file handler (5 MB, 3 backups)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    # Suppress noisy libraries
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger
