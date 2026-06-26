"""Central configuration for the Stekkies auto-responder."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Persistent Chromium profile (keeps you logged into Stekkies across runs).
USER_DATA_DIR = PROJECT_ROOT / "state" / "chromium-profile"

# Folder containing your application documents (WSL view of C:\Users\colli\Documents\huurdossier_2026).
DOCS_DIR = Path("/mnt/c/Users/colli/Documents/huurdossier_2026")

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

# Set False once you've verified the flow; True = fill but DO NOT submit.
DRY_RUN = True

for _d in (USER_DATA_DIR, LOG_DIR, SCREENSHOT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
