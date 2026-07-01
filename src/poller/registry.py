"""Per-site watch registry — one SiteConfig per source site.

Tiers were assigned by an initial discovery pass (`just discover` + manual
probing on 2026-07-01):

  - **tier 2, JSON-LD** (`parse_jsonld`): site server-renders schema.org
    listings. Works today with plain httpx.
  - **tier 2, anchor** (`make_anchor_parser`): site server-renders listing
    HTML with detail-page links but no JSON-LD; we scrape the links (URL only,
    price/size come later from the apply/judge stage).
  - **tier 3** (rendered tab): site is Cloudflare/DataDome/TLS-fingerprint
    protected or login-walled, so httpx gets a 403/challenge. The watcher opens
    the page in the real shared Chromium (over CDP, under the browser lock) and
    parses the rendered HTML. These need the browser host running.
  - **AWAITING TIER-1** (enabled=False): SPA whose list is drawn from a JSON API
    we have not captured yet. Fetching the HTML yields 0 listings. Capture the
    API call (DevTools → Network → Copy as cURL) and turn it into a tier-1 entry
    (endpoint/params/parse). Left disabled so it doesn't poll fruitlessly.

Re-run `python -m src.poller.discover` any time to re-check.
"""
from __future__ import annotations

from .models import SiteConfig
from .parsers import make_anchor_parser, parse_jsonld


def _jsonld(name: str, list_url: str, **kw) -> SiteConfig:
    return SiteConfig(name=name, tier=2, list_url=list_url, parse=parse_jsonld, **kw)


def _anchor(name: str, list_url: str, pattern: str, **kw) -> SiteConfig:
    return SiteConfig(name=name, tier=2, list_url=list_url,
                      parse=make_anchor_parser(pattern), **kw)


# Tier-3 opens a real tab under the browser lock, so it competes with live
# submissions. Kept OFF by default (validate with the browser host up first, and
# give each a real rendered-page parser), and on a slow cadence to limit
# contention. Flip on with POLL_ENABLE_TIER3=1 once verified.
import os as _os
_TIER3_ON = _os.environ.get("POLL_ENABLE_TIER3", "0") == "1"


def _tier3(name: str, list_url: str, *, parse=None, cadence_s: int = 180,
           **kw) -> SiteConfig:
    kw.setdefault("enabled", _TIER3_ON)
    return SiteConfig(name=name, tier=3, list_url=list_url,
                      parse=parse or parse_jsonld, cadence_s=cadence_s, **kw)


def _pending(name: str, list_url: str, **kw) -> SiteConfig:
    """SPA awaiting a tier-1 API cURL. Kept for reference; disabled until then."""
    return SiteConfig(name=name, tier=2, list_url=list_url,
                      parse=parse_jsonld, enabled=False, **kw)


REGISTRY: list[SiteConfig] = [
    # ---- working now: tier-2 JSON-LD --------------------------------------
    _jsonld("huurportaal.nl", "https://huurportaal.nl/huurwoningen/utrecht"),
    _jsonld("huurportaal.nl", "https://huurportaal.nl/huurwoningen/amsterdam"),

    # ---- working now: tier-2 anchor (detail links in server HTML) ----------
    _anchor("huurexpert.nl", "https://www.huurexpert.nl/huurwoningen/Utrecht",
            r"/huurwoning/[^\"']+/\d+/"),
    _anchor("huurexpert.nl", "https://www.huurexpert.nl/huurwoningen/Amsterdam",
            r"/huurwoning/[^\"']+/\d+/"),
    _anchor("livresidential.nl", "https://livresidential.nl/huurwoningen/utrecht",
            r"/huurwoningen/[a-z-]+/[a-z-]+/[a-z0-9-]+"),
    _anchor("livresidential.nl", "https://livresidential.nl/huurwoningen/amsterdam",
            r"/huurwoningen/[a-z-]+/[a-z-]+/[a-z0-9-]+"),
    _anchor("ikwilhuren.nu", "https://ikwilhuren.nu/aanbod/utrecht",
            r"/object/[a-z0-9-]+/"),
    _anchor("ikwilhuren.nu", "https://ikwilhuren.nu/aanbod/amsterdam",
            r"/object/[a-z0-9-]+/"),

    # ---- tier-3: Cloudflare / DataDome / TLS-fingerprint / login-walled ----
    # (need the shared Chromium host running; httpx alone gets 403/challenge.)
    _tier3("pararius.nl", "https://www.pararius.nl/huurwoningen/utrecht"),
    _tier3("pararius.nl", "https://www.pararius.nl/huurwoningen/amsterdam"),
    _tier3("huurwoningen.nl", "https://www.huurwoningen.nl/in/utrecht/"),
    _tier3("huurwoningen.nl", "https://www.huurwoningen.nl/in/amsterdam/"),
    _tier3("mijndak.nl", "https://www.mijndak.nl/woningaanbod/", needs_login=True),
    _tier3("woningnetregioutrecht.nl", "https://utrecht.mijndak.nl/",
           needs_login=True),
    _tier3("kamernet.nl", "https://kamernet.nl/en/for-rent/properties-utrecht",
           needs_login=True, cadence_s=120),

    # ---- awaiting tier-1 API cURL (SPA; HTML has no listings) --------------
    _pending("vesteda.com", "https://www.vesteda.com/nl/woningen"),
    _pending("funda.nl", "https://www.funda.nl/zoeken/huur"),
    _pending("plaza.newnewnew.space", "https://plaza.newnewnew.space/", needs_login=True),
    _pending("househunting.nl", "https://www.househunting.nl/aanbod/"),
    _pending("your-house.nl", "https://your-house.nl/woningaanbod/"),
    _pending("stienstra.nl", "https://www.stienstra.nl/ik-zoek-een-woning"),
    _pending("hurenindemix.nl", "https://www.hurenindemix.nl/aanbod/"),
    _pending("vgwgroup.nl", "https://vgwgroup.nl/aanbod-lange-termijnverhuur/"),
    _pending("rebowonenhuur.nl", "https://www.rebowonenhuur.nl/woningaanbod/"),
    _pending("verhuurtbeter.nl", "https://www.verhuurtbeter.nl/woningaanbod/"),
    _pending("woonruimte-utrecht.nl", "https://www.woonruimte-utrecht.nl/woningaanbod/"),
    _pending("eye-move.nl", "https://www.eye-move.nl/woningaanbod/"),
    _pending("nmgwonen.nl", "https://nmgwonen.nl/woningaanbod/"),
    _pending("deruitermakelaarshuis.nl",
             "https://www.deruitermakelaarshuis.nl/aanbod/?_status=te-huur"),
    # nmgwonen.mijnklantdossier.nl -> redirects to nmgwonen.nl (same aanbod);
    #   the tenant portal (mijnnmgwoning.nl) is login-only. Covered by nmgwonen.nl.
    # hurenviafrits.nl -> DNS no longer resolves (domain dead); dropped.
]


def enabled_sites() -> list[SiteConfig]:
    return [s for s in REGISTRY if s.enabled]


def by_name(name: str) -> SiteConfig | None:
    for s in REGISTRY:
        if s.name == name:
            return s
    return None
