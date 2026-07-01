# Implementation Plan — Active Listing Poller

Status: **plumbing complete; 19 sites live and verified**, 2026-07-01. Code
in `src/poller/`. See "Outcome" below for exactly what works and what is left.

Author: derived from a grilling session, 2026-07-01.

## Outcome (what actually shipped)

All site-agnostic plumbing is built and the registry classifies all 26 sites.
Build-order items 1–4 are done; item 5 is done except the VPS deploy has not
been run. Tools: `just discover` (tier triage), `just sniff <site>` (network
sniffer — DevTools Network tab in code), `just poll` / `poll-once`.

**Working & validated (19):**
- *tier-1, real JSON API, plain httpx (1):* hurenindemix (`/feed/woningen.js`;
  parser emits only AVAILABLE Huur units — currently 0, whole project is rented).
- *tier-2, server-rendered HTML, plain httpx (8):* huurportaal, huurexpert,
  livresidential, ikwilhuren, vgwgroup, nmgwonen, deruitermakelaarshuis,
  stienstra (owner-supplied Utrecht + 20 km / min 30 m2 filter).
- *tier-3, rendered browser, validated live (10):* huurwoningen (30), pararius
  (30, dedicated launched browser because CDP attachment trips Cloudflare),
  funda (14), vesteda (127), plaza (32), your-house (12),
  woonruimte-utrecht (94, no login), woningnetregioutrecht (logged in;
  `/HuisDetails?PublicatieId=` anchors), vbtverhuurmakelaars
  (`/woningen` → `/woning/<city>-<street>`), kamernet
  (`/en/for-rent/(apartment|studio)-...`; rooms excluded). Gated off by default;
  enable with `POLL_ENABLE_TIER3=1`.

**Not live — verified individually, and NOT fixable by code alone:**
- *rebowonenhuur.nl* — login works (creds valid), but currently has ZERO stock:
  the aanbod is a 7 KB empty shell with no listings and no data feed. Nothing to
  scrape or to build/verify a parser against until it has stock.
- *verhuurtbeter.nl* — likewise negligible/zero current stock; only widget
  scripts load, no listing data source discoverable.

**Resolved as covered / not a site (no separate watcher needed):**
- *househunting.nl* — outsources its listing display to huurwoningen.nl (already
  live); its `/woningaanbod` links out to huurwoningen.
- *mijndak.nl* — national WoningNet-DAK portal; Utrecht stock == woningnetregioutrecht.
- *nmgwonen.mijnklantdossier.nl* — eye-move-auth application backend; its public
  listings are on nmgwonen.nl (tier-2).
- *eye-move.nl* — shared third-party AUTH provider, not a rental site — dropped.
- *hurenviafrits.nl* — DNS dead — dropped.

The generic login+verify pass (over the shared browser host, using
`sources_credentials.json`) plus the Pararius dedicated-browser probe produced
these per-site verdicts. The remaining inactive sites are blocked on actual
inventory rather than parser work.

**Deviations from the original design, and why:**
- *Tier 1 (JSON API interception) is rare.* Only hurenindemix exposed a clean
  public JSON list endpoint; most sites are server-rendered (tier 2) or
  JS/anti-bot (tier 3).
- *Tier 3 parses the rendered DOM deterministically, not "a cheap LLM reads the
  list".* Cheaper and more reliable; the LLM is used only for the
  distance/roommate judgment, as for every tier.
- *No `id_field` in SiteConfig* — the canonical listing URL is the id directly.

**Remaining to be "complete":**
- One-time login upkeep for the login-walled sites.
- Run the VPS deploy (`just deploy`; enable `poller.service`).
- Open items below (cheap-model choice is set to `google/gemini-2.5-flash-lite`;
  LLM-judgment batching and long-term login upkeep still open).

## Goal

Stop depending on push mail from Stekkies (and huurwoningen), which is minutes
slow and incomplete. Instead **actively poll the source rental sites directly**
and hand fresh listings to the existing apply pipeline faster than any
aggregator can. Beat everyone else to the application.

## Non-negotiable outcomes

- Detect a new qualifying listing within ~60s of it appearing.
- Fit in 4 GB RAM (scale up only if a specific site forces it).
- Don't get IP-banned from the sites we depend on to apply.
- Reuse the existing `apply.py` pipeline unchanged (verified: it only needs
  `source_url`; see "Handoff" below).

## Filters (what counts as a listing worth applying to)

| Filter | Rule | Where enforced |
|--------|------|----------------|
| Price | ≤ €1750 | Deterministic, on structured field |
| City | Utrecht or Amsterdam | Deterministic, on structured field |
| Distance to center | ≤ ~15 min cycling from city center | LLM judgment on address/neighbourhood |
| Roommates | Not a shared/room (`kamer`) listing — judged from content, not just the word | LLM judgment |
| Surface | ≥ ~30 m² **if published**; if absent, apply anyway | Deterministic when present |

Whenever the site exposes filters via URL query params, encode price/city/type
into the request so the list is pre-narrowed before we ever look at it.

Note on guardrails: the owner has explicitly chosen **no deterministic veto**
before submit. This plan honours that. (Dissent is on record from the design
session: a free microsecond price/city veto would prevent a cheap-model misread
from auto-submitting to a wrong listing. Not implemented per owner decision.)

## Architecture

One browser, one profile (unchanged from today — all logins live in the single
`state/chromium-profile`). The poller is mostly **outside** the browser.

### Detection tiers (per site, in priority order)

1. **API interception (primary).** Most target sites are SPAs that fetch their
   listing list from a JSON endpoint. Reverse-engineer that endpoint once
   (devtools → network tab → copy request), then poll it with **httpx** — no
   browser, no tab, no LLM. Parse price/m²/city/type from **structured JSON
   fields**. This is the default path and the reason 4 GB + 60s cadence + token
   budget all work simultaneously.
2. **Filtered URL + parse.** Site has no clean API but does encode filters in the
   URL and serves listing data in server-rendered HTML. Fetch with httpx, parse
   the list.
3. **Rendered tab + LLM (fallback only).** Site defeats tiers 1–2 (JS-gated,
   login-walled list, no stable API). Open a real tab via the raw
   Playwright/CDP path, snapshot, let a cheap LLM read the list. This is the
   exception path, not the norm.

### Per-site registry

Each site is one entry describing how to watch it. Discovery (below) fills this.

```
sources/registry.py   (or sources/<site>.py per site)

SiteConfig:
  name:            "huurwoningen.nl"
  tier:            1 | 2 | 3
  endpoint:        "https://..."         # tier 1: JSON API URL (+ method/params/headers)
  list_url:        "https://...?price=..."# tier 2/3: filtered listing page
  parse:           callable(payload) -> list[RawListing]
  needs_login:     bool                   # whether the profile must be authed
  cadence_s:       60                     # base poll interval
  jitter_s:        (0, 30)                # randomized added delay per poll
  id_field:        how to extract the canonical listing URL
```

`RawListing` carries at minimum: `source_url`, and best-effort
`price / address / surface / city / type / source_name`.

### Watcher / applier split (no MCP contention)

- **Watcher** uses **httpx + raw Playwright/CDP only** (the deterministic
  `stekkies.py` style). It NEVER speaks to the Playwright MCP, so it cannot
  corrupt the MCP's active-tab state.
- **Applier** (`apply.py`) is the only component that speaks MCP. It takes an
  **exclusive lock** on the browser for the duration of a submission.
- Tiers 1–2 need no browser at all, so nearly all watching happens outside the
  browser. Only tier-3 sites contend for the browser; for those the watcher
  queues its tab work and yields while the applier holds the lock.

### Cross-site dedup

Key = the **canonical source listing URL**, stripped of tracking/query cruft
(utm_*, ref, session params → normalize to scheme+host+path, drop trailing
slash). The same flat on pararius + huurwoningen + a makelaar site dedupes to
one apply. Persist seen keys alongside the existing
`state/processed_listings.jsonl`.

### Block / challenge detection

The watcher must distinguish "no new listings" from "we've been blocked."
Signal, per poll:

- **Not HTTP 200** (403/429/503/redirect-to-challenge) → treat as blocked.
- **200 but unusable**: body is a Cloudflare/CAPTCHA interstitial, or the
  expected JSON shape / listing container is missing (schema mismatch) → treat
  as blocked, not as "empty".

On block: back off that site (exponential), stop counting its empty results as
truth, and **alert** via the existing `notify.py` / healthcheck path so a human
knows a source went dark. Per-site cadence + randomized jitter reduce the odds
of tripping this in the first place.

## Handoff to apply.py (verified, no new work)

`apply(listing: dict)` requires exactly one hard field: `listing['source_url']`.
`source_name / address / price` are optional (`.get(..., '?')`). The Stekkies
`letter` is **not** consumed. So the poller emits
`{"source_url", + optional address/price/source_name}` and the existing
pipeline runs unchanged.

## Orchestration flow

```
watcher loop (per site, on its own cadence+jitter):
  poll (tier 1/2/3) -> RawListings
  block-detect -> back off + notify on block
  deterministic filter (price<=1750, city in {Utrecht,Amsterdam}, surface if present)
  dedup by canonical URL -> new candidates only
  LLM judgment (distance-to-center, roommates) on each new candidate
  qualifying -> enqueue {source_url, ...} for applier

applier:
  acquire exclusive browser lock
  apply(listing)  # existing pipeline, logs transcript, notifies, marks processed
  release lock
```

## Sites to cover (big-bang, all 26)

From `state/sources_credentials.json`: pararius.nl, ikwilhuren.nu, vesteda.com,
hurenviafrits.nl, hurenindemix.nl, mijndak.nl, vgwgroup.nl, rebowonenhuur.nl,
verhuurtbeter.nl, huurexpert.nl, huurwoningen.nl, huurmatcher.nl,
huurportaal.nl, woningnetregioutrecht.nl, plaza.newnewnew.space,
woonruimte-utrecht.nl, eye-move.nl, nmgwonen.mijnklantdossier.nl. Plus:
stienstra.nl, kamernet.nl, funda.nl, nmgwonen.nl, livresidential.nl,
househunting.nl, your-house.nl, deruitermakelaarshuis.nl.

Rollout decision: **big-bang all 26** (owner's call). Highest-regret risk was
the apply handoff, now verified safe, which de-risks it.

## Discovery phase (structured, not open-ended)

This is a **templated per-site spike**, one checklist per site, timeboxed. For
each site produce a filled `SiteConfig`:

1. Open the site's listing search in devtools → network tab.
2. Is there a JSON API driving the list? → **tier 1**. Record endpoint, method,
   params, headers, and how filters map to params.
3. If not, do URL filters + server-rendered HTML work? → **tier 2**. Record
   `list_url` template + a parse function.
4. Else → **tier 3**. Record the list selector and the rendered-tab approach.
5. Note `needs_login` and confirm the profile is/can be authed for it.
6. Map filter fields (price, m², city, type) to concrete JSON/DOM fields.
7. Pick `cadence_s` + `jitter` conservative enough to avoid blocks.

Expected fallout: funda, kamernet, pararius are the likeliest to resist tiers
1–2 (aggressive anti-bot). Budget extra time there.

## Build order

1. Core plumbing first (site-agnostic): `SiteConfig`/registry, watcher loop,
   httpx fetch, block-detector, canonical-URL dedup, deterministic filter,
   LLM-judgment step, browser lock, applier enqueue. Wire to `apply.py`.
2. Discovery + registry entries for all 26 sites (the templated spike).
3. Integrate cadence/jitter + per-site backoff.
4. Notify/healthcheck integration for "source went dark."
5. justfile recipes + systemd unit for the watcher; deploy to VPS.

## Open items still to nail during build

- Exact cheap watcher model for tier-3 LLM reads (cost vs. reliability).
- Whether the LLM-judgment step can be batched to cut token spend.
- Login upkeep for `needs_login` sites within the single profile.
```
