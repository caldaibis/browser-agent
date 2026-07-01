"""Per-site watch registry — one SiteConfig per source site.

Tiers were assigned by an initial discovery pass (`just discover` + manual
probing on 2026-07-01):

  - **tier 2, JSON-LD** (`parse_jsonld`): site server-renders schema.org
    listings. Works today with plain httpx.
  - **tier 2, anchor** (`make_anchor_parser`): site server-renders listing
    HTML with detail-page links but no JSON-LD; we scrape the links (URL only,
    price/size come later from the apply/judge stage).
  - **tier 3** (rendered tab): site is Cloudflare/DataDome/TLS-fingerprint
    protected, login-walled, or a JS-SPA that blocks both httpx and throwaway
    headless Chromium. The watcher opens the page in the real shared Chromium
    (over CDP, under the browser lock, with the host's anti-automation flags)
    and parses the rendered HTML. These need the browser host running and are
    OFF by default (POLL_ENABLE_TIER3=1). Each tier-3 SPA still needs its
    rendered-DOM parser tuned once against the live host.

Re-run `python -m src.poller.discover` any time to re-check, and use
`python -m src.poller.sniff <site>` (network sniffer) to hunt a JSON API.
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
    _anchor("vgwgroup.nl", "https://vgwgroup.nl/aanbod-lange-termijnverhuur/",
            r"/woningen/[a-z0-9-]+[0-9a-f]{16}"),
    _anchor("nmgwonen.nl", "https://nmgwonen.nl/woningaanbod/",
            r"/woning/[a-z0-9-]+/"),
    _anchor("deruitermakelaarshuis.nl",
            "https://www.deruitermakelaarshuis.nl/aanbod/?_status=te-huur",
            r"/aanbod/[a-z0-9-]+-[a-z0-9-]+/"),

    # ---- tier-3: Cloudflare / DataDome / TLS-fingerprint / login-walled ----
    # (need the shared Chromium host running; httpx alone gets 403/challenge.)
    # VALIDATED against the live host = renders + parses listings today.
    _tier3("huurwoningen.nl", "https://www.huurwoningen.nl/in/utrecht/"),   # VALIDATED: 30 JSON-LD
    _tier3("huurwoningen.nl", "https://www.huurwoningen.nl/in/amsterdam/"),
    # pararius/mijndak serve a challenge/empty page even to the real browser
    # (deepest protection) — kept for reference; need a challenge-solve/login.
    _tier3("pararius.nl", "https://www.pararius.nl/huurwoningen/utrecht"),
    _tier3("pararius.nl", "https://www.pararius.nl/huurwoningen/amsterdam"),
    _tier3("mijndak.nl", "https://www.mijndak.nl/woningaanbod/", needs_login=True),
    _tier3("woningnetregioutrecht.nl", "https://utrecht.mijndak.nl/",
           needs_login=True),
    _tier3("kamernet.nl", "https://kamernet.nl/en/for-rent/properties-utrecht",
           needs_login=True, cadence_s=120),
    # JS-SPAs whose listing list is drawn client-side from an API and which
    # block plain httpx AND throwaway headless Chromium (bot-detected / served a
    # 404/challenge). They render fine in the project's real anti-automation
    # browser host, so they are tier-3. URLs below are the confirmed live search
    # pages. Each still needs its rendered-DOM parser tuned once against the
    # running host (parse defaults to JSON-LD; most will need an anchor/DOM
    # parser) — do that with `just host` up, then flip POLL_ENABLE_TIER3=1.
    _tier3("funda.nl", "https://www.funda.nl/zoeken/huur?selected_area=%5B%22utrecht%22,%22amsterdam%22%5D",
           parse=make_anchor_parser(r"/detail/huur/[a-z-]+/[^/]+/\d+/")),          # VALIDATED: 14
    _tier3("plaza.newnewnew.space", "https://plaza.newnewnew.space/aanbod",
           needs_login=True,
           parse=make_anchor_parser(r"/aanbod/huurwoningen/details/\d+-")),         # VALIDATED: 32
    _tier3("your-house.nl", "https://your-house.nl/woningaanbod/huur",
           parse=make_anchor_parser(r"/woningaanbod/huur/[a-z-]+/[^/?]+/\d")),      # VALIDATED: 12
    _tier3("vesteda.com", "https://www.vesteda.com/nl/woning-zoeken",
           parse=make_anchor_parser(
               r"/nl/huurwoning(?:en)?-(?:utrecht|amsterdam)/[a-z0-9-]+/[a-z0-9-]+")),
    # Not cracked: househunting shows only office pages (listings are JS cards
    # with no anchors); kamernet renders past DataDome but its room detail links
    # are JS onclick, not <a> — both need a DOM click-through or their API.
    _tier3("househunting.nl", "https://www.househunting.nl/aanbod/"),
    _tier3("hurenindemix.nl", "https://www.hurenindemix.nl/aanbod/"),
    _tier3("rebowonenhuur.nl", "https://www.rebowonenhuur.nl/woningaanbod/"),
    _tier3("verhuurtbeter.nl", "https://www.verhuurtbeter.nl/woningaanbod/"),
    _tier3("woonruimte-utrecht.nl", "https://www.woonruimte-utrecht.nl/woningaanbod/"),
    _tier3("eye-move.nl", "https://www.eye-move.nl/woningaanbod/"),
    _tier3("stienstra.nl", "https://www.stienstra.nl/ik-zoek-een-woning"),
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
