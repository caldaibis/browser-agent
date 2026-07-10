"""Central configuration for the Stekkies auto-responder."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Persistent Chromium profile (keeps you logged into Stekkies across runs).
USER_DATA_DIR = PROJECT_ROOT / "state" / "chromium-profile"

# Application documents you provide locally (gitignored — they hold personal
# data, so they are never committed). Override the location with DOCS_DIR.
from .settings import settings as _settings
DOCS_DIR = Path(_settings().docs_dir or PROJECT_ROOT / "documents")

LOG_DIR = PROJECT_ROOT / "logs"
SCREENSHOT_DIR = LOG_DIR / "screenshots"

STEKKIES_LOGIN_URL = "https://www.stekkies.com/en/profiles/login/"
STEKKIES_HOME_URL = "https://www.stekkies.com/en/"

# Shared always-on browser exposed over CDP. The browser host (src.browser_host)
# runs ONE persistent Chromium on this port; both the Stekkies extractor and the
# Hermes apply agent attach to it, so all logins (Google SSO + rental sites)
# live in a single profile you sign into once.
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

for _d in (USER_DATA_DIR, LOG_DIR, SCREENSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
