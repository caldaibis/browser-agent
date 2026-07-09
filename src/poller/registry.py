"""Per-site watch registry — one SiteConfig per source site.

Tiers were assigned by an initial discovery pass (`just discover` + manual
probing on 2026-07-01):

  - **tier 2, JSON-LD** (`parse_jsonld`): site server-renders schema.org
    listings. Works today with plain httpx.
  - **tier 2, anchor** (`make_anchor_parser`): site server-renders listing
    HTML with detail-page links but no JSON-LD; we scrape the links (URL only,
    price/size come later from the apply/judge stage).
  - **tier 3** (rendered browser): site is Cloudflare/DataDome/TLS-fingerprint
    protected, login-walled, or a JS-SPA that blocks both httpx and throwaway
    headless Chromium. Most tier-3 sites open the page in the real shared
    Chromium (over CDP, under the browser lock, with the host's anti-automation
    flags) and parse the rendered HTML. A few public pages can instead use
    own_browser=True to launch a dedicated throwaway Chromium when CDP itself
    triggers anti-bot detection. These are OFF by default (POLL_ENABLE_TIER3=1).

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


# Tier-3 usually opens a real tab under the browser lock, so it can compete with
# live submissions. Kept OFF by default and on a slow cadence to limit
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

    # ---- working now: tier-2 anchor (detail links in server HTML) ----------
    # huurexpert.nl: dropped 07-07-2026 — applying requires a PAID account the
    # owner decided not to buy (2 listings had already died as login_required).
    # Polling a site we can never apply on only burns fetches and judge calls.
    _anchor("livresidential.nl", "https://livresidential.nl/huurwoningen/utrecht",
            r"/huurwoningen/[a-z-]+/[a-z-]+/[a-z0-9-]+"),
    _anchor("ikwilhuren.nu", "https://ikwilhuren.nu/aanbod/utrecht",
            r"/object/[a-z0-9-]+/"),
    _anchor("vgwgroup.nl", "https://vgwgroup.nl/aanbod-lange-termijnverhuur/",
            r"/woningen/[a-z0-9-]+[0-9a-f]{16}"),
    _anchor("nmgwonen.nl", "https://nmgwonen.nl/woningaanbod/",
            r"/woning/[a-z0-9-]+/"),
    _anchor("deruitermakelaarshuis.nl",
            "https://www.deruitermakelaarshuis.nl/aanbod/?_status=te-huur",
            r"/aanbod/[a-z0-9-]+-[a-z0-9-]+/"),
    # stienstra: owner-supplied fine filter (Utrecht + 20 km radius, min 30 m²);
    # server-rendered /woning/<slug> anchors, city in the slug.
    _anchor("stienstra.nl",
            "https://www.stienstra.nl/uitgebreid-zoeken?use_radius=on&search_location=Utrecht%2C+Nederland&radius=20&min-area=30&min-price=0&max-price=400000",
            r"/woning/[a-z0-9-]+"),

    # ---- tier-3: Cloudflare / DataDome / TLS-fingerprint / login-walled ----
    # (need the shared Chromium host running; httpx alone gets 403/challenge.)
    # VALIDATED against the live host = renders + parses listings today.
    _tier3("huurwoningen.nl", "https://www.huurwoningen.nl/in/utrecht/"),   # VALIDATED: 30 JSON-LD
    # pararius: Cloudflare "Just a moment" JS challenge that a CDP-attached
    # browser NEVER clears (CF detects the CDP attachment) but a freshly LAUNCHED
    # browser sails through — so own_browser=True. VALIDATED: 30 listings.
    _tier3("pararius.nl", "https://www.pararius.nl/huurwoningen/utrecht",
           own_browser=True,
           parse=make_anchor_parser(r"/(?:appartement|huis|studio)-te-huur/[a-z-]+/[0-9a-f]+/")),
    # mijndak serves a challenge/empty page; its Utrecht stock == woningnetregioutrecht.
    _tier3("mijndak.nl", "https://www.mijndak.nl/woningaanbod/", needs_login=True),
    _tier3("woningnetregioutrecht.nl", "https://utrecht.mijndak.nl/WoningOverzicht",
           needs_login=True,
           parse=make_anchor_parser(r"HuisDetails\?PublicatieId=\d+")),  # VALIDATED (logged in)
    # JS-SPAs whose listing list is drawn client-side from an API and which
    # block plain httpx AND throwaway headless Chromium (bot-detected / served a
    # 404/challenge). They render fine in the project's real anti-automation
    # browser host, so they are tier-3. URLs below are the confirmed live search
    # pages. Each still needs its rendered-DOM parser tuned once against the
    # running host (parse defaults to JSON-LD; most will need an anchor/DOM
    # parser) — do that with `just host` up, then flip POLL_ENABLE_TIER3=1.
    _tier3("funda.nl", "https://www.funda.nl/zoeken/huur?selected_area=%5B%22utrecht%22%5D",
           parse=make_anchor_parser(r"/detail/huur/[a-z-]+/[^/]+/\d+/")),          # VALIDATED: 14
    _tier3("plaza.newnewnew.space", "https://plaza.newnewnew.space/aanbod",
           needs_login=True,
           parse=make_anchor_parser(r"/aanbod/huurwoningen/details/\d+-")),         # VALIDATED: 32
    # your-house.nl: dropped 07-07-2026. Applying now requires a paid EUR 25
    # membership and led the agent to a live Mollie checkout. We never pay, so
    # polling it only burns tier-3 browser time and LLM turns.
    _tier3("vesteda.com", "https://www.vesteda.com/nl/woning-zoeken",
           parse=make_anchor_parser(
               r"/nl/huurwoning(?:en)?-utrecht/[a-z0-9-]+/[a-z0-9-]+")),
    _tier3("vbtverhuurmakelaars.nl", "https://vbtverhuurmakelaars.nl/woningen",
           parse=make_anchor_parser(r"/woning/[a-z]+-[a-z0-9-]+")),   # VALIDATED: /woning/<city>-<street>

    # kamernet: exclude rooms (we don't do shared/room listings) — apartments &
    # studios only. City is in the URL slug, so the deterministic filter handles
    # other-city listings that the (loose) city list still returns.
    _tier3("kamernet.nl", "https://kamernet.nl/en/for-rent/properties-utrecht",
           needs_login=True, cadence_s=120,
           parse=make_anchor_parser(
               r"/en/for-rent/(?:apartment|studio)-[a-z-]+/[^/]+/(?:apartment|studio)-\d+")),  # VALIDATED
    # househunting.nl outsources its listing display to huurwoningen.nl (its
    # /woningaanbod links out to huurwoningen.nl), which is already covered above.
    # rebowonenhuur.nl: disabled 2026-07-09 — login wall not resolvable without
    # human login in shared browser (second occurrence; prior needs_login=True +
    # email didn't resolve). Re-enable after logging into rebowonenhuur.nl in the
    # shared CDP browser and verifying /woningaanbod/ shows listings.
    _tier3("rebowonenhuur.nl", "https://www.rebowonenhuur.nl/woningaanbod/",
           enabled=False, needs_login=True),
    # rebogroep.nl: the agency behind 4 of the first 38 real submissions — it
    # clearly carries Utrecht stock, but until now it was only discovered
    # indirectly (and late) via huurportaal/huurwoningen aggregator pages.
    # Server HTML has no listing anchors (client-side rendered; verified
    # 07-07-2026), so tier-3: render in the shared browser and scrape the
    # /nl/aanbod/<uuid>-<slug> detail links.
    _tier3("rebogroep.nl", "https://www.rebogroep.nl/nl/aanbod",
           parse=make_anchor_parser(r"/nl/aanbod/[0-9a-f]{8}-[0-9a-f-]{27}[a-z0-9-]*")),
    _tier3("verhuurtbeter.nl", "https://www.verhuurtbeter.nl/aanbod",
           parse=make_anchor_parser(r"/appartement-te-huur/[a-zA-Z]+/[a-zA-Z0-9-]+/\d+/")),  # VALIDATED: 40
    _tier3("woonruimte-utrecht.nl", "https://www.woonruimte-utrecht.nl/woningaanbod/",
           parse=make_anchor_parser(r"/woning/[0-9a-f]{16,}")),   # VALIDATED: 94, no login
    # eye-move.nl is NOT a rental site — it's a shared third-party auth provider
    #   used by nmgwonen.mijnklantdossier.nl and vbtverhuurmakelaars.nl under
    #   SEPARATE accounts. Dropped from the registry.
    # nmgwonen.mijnklantdossier.nl -> its public listings live on nmgwonen.nl
    #   (tier-2, above); mijnklantdossier is the eye-move-auth application backend.
    # hurenviafrits.nl -> DNS no longer resolves (domain dead); dropped.
]


def enabled_sites() -> list[SiteConfig]:
    return [s for s in REGISTRY if s.enabled]


def by_name(name: str) -> SiteConfig | None:
    for s in REGISTRY:
        if s.name == name:
            return s
    return None
