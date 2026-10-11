"""All environment configuration for the unified service, in one place."""
import os


def _hosts(name, default=""):
    return {h.strip().lower() for h in os.environ.get(name, default).split(",") if h.strip()}


# ---- server ----------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8080))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")
API_KEY = os.environ.get("API_KEY", "").strip()          # sent as header X-API-Key
SCRAPE_REQUIRES_KEY = os.environ.get("SCRAPE_REQUIRES_KEY", "0") == "1"
# /getlink is open by default (like the old standalone service); set GETLINK_REQUIRES_KEY=1 to protect it
GETLINK_REQUIRES_KEY = os.environ.get("GETLINK_REQUIRES_KEY", "0") == "1"

# ---- shared browser ----------------------------------------------------------
# Max browser jobs (getlink + driveseed) running at the same time.
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))

# ---- /scrape (modpro episode pages) -----------------------------------------
SCRAPE_ALLOWED_HOSTS = _hosts("SCRAPE_ALLOWED_HOSTS")   # empty = any public host

# ---- /getlink (linkpilot shortener) -------------------------------------------
PROXY_URL = os.environ.get("PROXY_URL", "").strip()     # applies to /getlink only
GETLINK_ALLOWED_HOSTS = _hosts("GETLINK_ALLOWED_HOSTS")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 150))
DEBUG_DIR = os.environ.get("DEBUG_DIR", "").strip()
CACHE_TTL = int(os.environ.get("CACHE_TTL", 900))       # 0 = off
BLOCK_THIRD_PARTY = os.environ.get("BLOCK_THIRD_PARTY", "1") != "0"
ENABLED_VARIANTS = {v.strip() for v in os.environ.get("VARIANTS", "").split(",") if v.strip()}
TIME_SPEEDUP = max(1, int(os.environ.get("TIME_SPEEDUP", 25)))

# ---- /api/link (driveseed -> google drive) ------------------------------------
DRIVESEED_ALLOWED_HOSTS = _hosts("DRIVESEED_ALLOWED_HOSTS", "driveseed.org")
DRIVESEED_CACHE_TTL = int(os.environ.get("DRIVESEED_CACHE_TTL", 600))
QUEUE_WAIT = int(os.environ.get("QUEUE_WAIT", 60))
SESSION_DIR = os.environ.get("SESSION_DIR", "/tmp/driveseed")
