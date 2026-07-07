"""Apply stage: hand the external listing to our browser agent loop.

Builds a precise task prompt (source URL, reference message, document list,
auto-submit instruction) and runs the lightweight agent loop in
`src.browser_agent` (DeepSeek LLM + Playwright MCP over our shared CDP
browser). The agent adapts to whatever application form the source site shows.

Run standalone:  python -m src.apply logs/last_listing.json
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import site_playbooks
from .apply_priority import priority_pending
from .config import DOCS_DIR, LOG_DIR, CDP_URL
from .credentials import available_domains
from .listing_context import fetch_context, is_aggregator
from .message_template import REFERENCE_APPLICATION_MESSAGE
from .applicant_profile import PROFILE, INCOME_TOLERANCE
from .browser_agent import run_agent, AgentResult
from .poller.browser_lock import browser_lock
from .rent_policy import MAX_RENT, parse_rent
from .site_fastpaths import try_fast_apply

# Model for the apply agent. Override via APPLY_MODEL.
APPLY_MODEL = os.environ.get("APPLY_MODEL", "deepseek-v4-pro")
APPLY_MAX_TURNS = int(os.environ.get("APPLY_MAX_TURNS", "60"))
APPLY_TIMEOUT_SECONDS = int(os.environ.get("APPLY_TIMEOUT_SECONDS", "900"))
APPLY_FASTPATH_ENABLED = os.environ.get("APPLY_FASTPATH_ENABLED", "1") != "0"

# Google account used for "Sign in with Google" SSO on source sites (Funda etc.).
GOOGLE_ACCOUNT = os.environ.get("GOOGLE_ACCOUNT", "you@example.com")

# Sites/wording where applying is gated by a paid registration or membership.
# These are not normal free rental forms, and the bot must not spend money or
# spend LLM turns discovering a checkout. your-house.nl was verified on
# 07-07-2026 to lead to a live Mollie EUR 25 membership payment before applying.
KNOWN_PAID_APPLICATION_DOMAINS = {"your-house.nl"}
_PAYMENT_SENTENCE_SPLIT = re.compile(r"[.!?\n|]+")
_PAYMENT_NEGATIONS = ("geen", "niet", "no ", "not ", "gratis", "free")
_PAYMENT_TEXT_RES = (
    re.compile(
        r"\b(lidmaatschap|inschrijfkosten|registratiekosten|servicekosten)\b"
        r"[^.!?\n]{0,120}(€\s*\d|eur\s*\d|\d+[\d.,]*\s*euro)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(€\s*\d[\d.,]*|eur\s*\d[\d.,]*|\d+[\d.,]*\s*euro)"
        r"[^.!?\n]{0,120}\b(lidmaatschap|inschrijven|registratie|reageren)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(paywall|upgrade required|plus account required)\b", re.IGNORECASE),
)


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _payment_required_reason(listing: dict) -> str | None:
    domain = _domain(listing.get("source_url", ""))
    if domain in KNOWN_PAID_APPLICATION_DOMAINS:
        return f"{domain} is a known paid-registration application site"

    description = (listing.get("description") or "").strip()
    if not description:
        ctx = fetch_context(listing.get("source_url", ""))
        if ctx:
            description = ctx.description
    text = "\n".join(
        str(x or "") for x in (
            listing.get("source_name"), listing.get("address"),
            listing.get("title"), description,
        )
    )
    for sentence in _PAYMENT_SENTENCE_SPLIT.split(text):
        low = sentence.lower()
        if not low.strip() or any(n in low for n in _PAYMENT_NEGATIONS):
            continue
        for rx in _PAYMENT_TEXT_RES:
            if rx.search(sentence):
                return f"paid registration/application wording: {sentence.strip()[:180]}"
    return None


# Priority (lower = attach first) + one-line purpose per document type, matched
# by filename substring. The purpose tells the agent WHY each doc matters so it
# can map docs to labelled slots and choose well when slots are limited.
def _classify(name: str) -> tuple[int, str]:
    n = name.lower()
    if "paspoort" in n or "passport" in n:
        return 1, "Identity (ID) — always required, gatekeeping."
    if "werkgeversverklaring" in n:
        return 2, "Permanent employment + income — the decisive proof; attach whenever employer/income proof is asked."
    if "salarisstrook" in n or "loonstrook" in n:
        return 3, "Recent payslip — corroborates actual monthly income (attach the most recent months)."
    if "verhuurdersverklaring" in n:
        return 4, "Landlord reference — long stable tenancy, no arrears or nuisance; strong positive."
    if "huurdersprofiel" in n:
        return 5, "Tenant profile / cover sheet — summarises the whole dossier; good for a general/cover slot."
    if "motivatiebrief" in n or "motivation" in n:
        return 6, "Motivation letter — personal cover letter explaining why you want this home; attach for any motivation/cover-letter slot or general attachment."
    if "uwv" in n or "verzekeringsbericht" in n:
        return 7, "UWV statement — independently confirms the permanent contract and work history."
    if "jaaropgave" in n and "degiro" not in n:
        return 8, "Annual income statement — secondary income proof."
    if "bankafschrift" in n or "bankstatement" in n:
        return 9, "Proof of salary deposit (and rent paid) — privacy-light extract."
    if "degiro" in n:
        return 10, "Investment statement — extra asset proof, only for strict income-multiple cases."
    return 50, "Additional supporting document."


def _month_key(name: str) -> int:
    m = re.search(r"_(\d{2})_", name)
    return -int(m.group(1)) if m else 0  # negative => most recent month first


def _doc_list() -> str:
    if not DOCS_DIR.exists():
        return "(WARNING: documents folder not found at %s)" % DOCS_DIR
    files = [p for p in DOCS_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
    entries = []
    for p in files:
        prio, purpose = _classify(p.name)
        # within payslips, sort most-recent-first; otherwise by name
        secondary = _month_key(p.name) if prio == 3 else p.name
        entries.append((prio, secondary, p, purpose))
    entries.sort(key=lambda e: (e[0], e[1]))
    return "\n".join(f"  - {p} — {purpose}" for _prio, _sec, p, purpose in entries)


def _listing_details_clause(listing: dict) -> str:
    """Pre-fetched listing description + aggregator warning for the prompt.

    The description feeds the eligibility gate up front (runs used to spend
    their first turns hunting for it in-browser, and short-stay/students-only
    gates were sometimes only discovered after opening the form). Fail-open:
    no description, no clause."""
    description = (listing.get("description") or "").strip()
    if not description:
        ctx = fetch_context(listing.get("source_url", ""))
        if ctx:
            description = ctx.description
    clause = ""
    if description:
        clause += f"""
LISTING PAGE DETAILS (pre-fetched from the listing's own page; may be stale --
the live page always wins). Use this NOW for the eligibility gate and for
tailoring the application message, instead of spending turns reading the
description in the browser:
\"\"\"
{description[:1800]}
\"\"\"
"""
    if is_aggregator(listing.get("source_url", "")):
        clause += """
AGGREGATOR NOTE: this listing URL is on an aggregator site (a shop window).
The real application usually happens on the landlord/agency's own website,
reached through THIS listing page's own apply/contact control ("Contact
landlord", "Reageer", "Bekijk woning"...). Click that control and follow
where it leads. NEVER go searching the agency's own website for the listing
by hand -- a previous run burned 55 of its 60 turns doing exactly that.
"""
    return clause


def build_prompt(listing: dict) -> str:
    domains = available_domains()
    domain_list = ", ".join(domains) if domains else "(none stored)"
    sso = (
        f"If the site offers \"Sign in with Google\" / \"Continue with Google\" "
        f"(e.g. Funda), PREFER that: click it and complete sign-in with the "
        f"Google account {GOOGLE_ACCOUNT}. The browser should already have a "
        f"Google session, so this is usually one or two clicks. Only fall back "
        f"to email/password login if Google sign-in is not offered."
    )
    login_clause = (
        f"{sso}\n"
        "For email/password login, do NOT guess credentials: call the "
        "`lookup_credential` tool with the site's domain or current URL (e.g. "
        "\"ikwilhuren.nu\") and it returns the username/password to use. A single "
        "application can span more than one site (the listing host plus a backing "
        "portal), so look up the credential for WHATEVER login page you actually "
        "land on, by its own domain. We hold logins for: "
        f"{domain_list}.\n"
        "Note: some backing portals are a SHARED third-party auth provider used "
        "by multiple unrelated rental sites under DIFFERENT accounts (e.g. "
        "eye-move.nl / mijnklantdossier.nl) — if that shared domain itself has "
        "no stored credential, retry the lookup with THIS listing's own source "
        "domain above instead (the account is tied to the originating site, not "
        "the shared provider).\n"
        "If lookup_credential returns no match and the site requires an account, "
        "stop and report that login is needed. NEVER reset, change, or recover a "
        "password (no \"wachtwoord vergeten\" / \"forgot password\" flow): if a "
        "stored password is rejected, stop and report a login failure."
    )
    submit_clause = "Then SUBMIT the application. Confirm submission succeeded."
    playbook = site_playbooks.load_for_url(listing.get("source_url", ""))
    playbook_clause = ""
    if playbook:
        domain, playbook_text = playbook
        playbook_clause = f"""
SITE PLAYBOOK for {domain} — durable lessons distilled from previous runs on
this exact site. Use it to go straight to the right flow instead of
rediscovering it, but VERIFY against the live page: the site may have changed,
and the current page always wins over the playbook.
\"\"\"
{playbook_text}
\"\"\"
"""
    return f"""You are applying to a Dutch rental listing on my behalf. Act autonomously.

LISTING (external source: {listing.get('source_name','?')})
  Address: {listing.get('address','?')}
  Price:   {listing.get('price','?')}
  Apply at this URL: {listing['source_url']}

PERSONAL RENT CAP (hard stop):
The maximum rent I am willing to pay is EUR {MAX_RENT:.0f} per month. Before
you fill or submit anything, verify the listing rent/price on the page. If the
rent is above EUR {MAX_RENT:.0f}, STOP immediately and report `OUTCOME:
not_eligible`, stating the listed rent and the cap. This cap overrides all other
instructions.

LOGIN
{login_clause}

APPLICANT PROFILE (use this to judge eligibility — these are hard facts):
{PROFILE.to_prompt_block()}

ELIGIBILITY GATE — CHECK BEFORE YOU APPLY (this is mandatory):
After opening the listing, READ the full property description and any
requirements/conditions section BEFORE filling anything. Dutch listings state
criteria under wording like "inkomenseis", "bruto (maand)inkomen minimaal",
"X keer de (kale) huur", "voorwaarden", "doelgroep", "inschrijfvoorwaarden".
Look specifically for:
  - A minimum gross monthly income (bruto maandinkomen) or an income-to-rent
    multiple (e.g. "4x de kale huur"). Compute the required income from the
    multiple and the rent if only the multiple is given.
  - Exclusion of students or woningdelers/room-sharers ("studenten en
    woningdelers behoren niet tot onze doelgroep").
  - Any other hard gate (max household size, required employment type, etc.).
Compare each stated HARD requirement against the APPLICANT PROFILE above.
  - INCOME: the applicant's gross monthly income is EUR {PROFILE.gross_monthly_income:,.2f}.
    If a required minimum is stated and the applicant's income is below it by
    MORE than {INCOME_TOLERANCE:.0%}, the applicant does NOT qualify. (Within
    {INCOME_TOLERANCE:.0%} of the minimum is acceptable — proceed.) Do the
    arithmetic explicitly before deciding.
  - SAVINGS / ASSETS: if income is slightly short but the listing text says
    savings, assets, eigen vermogen, waarborg, guarantor, or a broader
    financial file may count, proceed and mention the applicant's savings
    (EUR {PROFILE.savings_amount:,.0f}) plus complete dossier. Only stop for
    income when the site states a hard minimum that clearly cannot be met and
    does NOT allow assets/savings to compensate.
  - If the listing excludes students/woningdelers, the applicant is neither, so
    that exclusion does NOT block this application.
If ANY hard requirement is clearly NOT met, DO NOT fill or submit anything:
STOP immediately and report `OUTCOME: not_eligible`, stating the requirement,
the applicant's value, and the gap. Only proceed to fill the form when the
applicant plausibly meets every stated hard requirement. If no requirements are
stated, proceed normally.
{_listing_details_clause(listing)}{playbook_clause}
YOUR TASK
1. Open the URL above in the browser.
2. Run the ELIGIBILITY GATE above. If not eligible, stop now (do not apply).
3. Find the apply / "reageer" / contact / application form for this property.
4. Fill the application form using the applicant details contained in the
   reference message below. For any message/motivation field, write a warm,
   natural application message inspired by the reference. Keep the same facts,
   keep both Dutch and English sections, replace [[ADDRESS]] with the current
   property address when known, and make small listing-specific adjustments so
   it does not read like a rigid template. Do not paste the reference verbatim.
   Do not invent new personal details. NEVER use em dashes (—) or en dashes (–)
   anywhere in the text you write; use a comma, period, or the word "to" instead.
   Put the full message in the main
   message/motivation field. For a separate OPTIONAL "opmerkingen"/remarks field,
   leave it blank or write one short sentence — these often have a tight
   character limit; if a field shows [invalid] after typing, it is likely too
   long, so shorten drastically rather than retrying the same length.
5. Upload documents BEFORE the final submit, into whatever attachment slots the
   form provides. Match a document to a labelled slot when the label names it
   (use the "why it matters" note to pick the right one); otherwise put it in a
   generic/extra slot. If a slot accepts multiple files, add several at once.
   Slots are often LIMITED — do not loop hunting for more slots or re-uploading
   the same file. The list below is ALREADY in priority order, so attach from the
   top down and stop when slots run out. Each line is "<path> — why it matters":
{_doc_list()}
6. {submit_clause}
7. After you see a submission/confirmation message, you are DONE: report success
   in one short line and STOP. NEVER re-open ("Aanvraag wijzigen"/edit), modify,
   or resubmit an application you have already submitted, even to add documents.
   Report: did it submit, any errors, and what fields you could not fill.

REFERENCE APPLICATION MESSAGE (contains name, age, job, income, phone):
\"\"\"
{REFERENCE_APPLICATION_MESSAGE}
\"\"\"

TOOL USE — BE EFFICIENT AND CORRECT (this saves tokens and time):
- Use browser_snapshot to see the page as a compact accessibility tree. Each
  element has a ref like `e37`.
- To click/type, pass that EXACT ref (e.g. target "e37") plus a short `element`
  description. NEVER invent CSS selectors (no "#e37 > button", no
  "button[ref=...]", no ":has-text(...)"). Refs come only from the latest
  snapshot.
- If a click/type fails ("does not match any elements"), take a FRESH
  browser_snapshot and use the new ref — do not guess selectors.
- Fill several fields at once with browser_fill_form.
- Upload documents with browser_file_upload (pass the absolute paths above).
- Accept any cookie banner first (click its "Accept"/"Alles accepteren" ref).
- NEVER use browser_evaluate or browser_run_code_unsafe. Use the high-level
  tools only. Do not dump full page text.
- Snapshot discipline: do NOT re-snapshot after every click. Re-snapshot only
  when the page has clearly changed (new page, modal opened/closed) and you need
  fresh refs. Redundant snapshots waste the whole budget.
- If you clicked something that should open a dialog/modal but browser_snapshot
  doesn't show it (some HTML dialogs aren't built with proper accessibility
  roles, so they never get a ref), use dom_scan — a raw-DOM fallback report,
  not the accessibility tree — to see what's actually there. Then, still
  inside that same ref-less dialog: click_by_text to click something by its
  exact visible text, fill_by_label to type into a text/email/tel/textarea
  field by its label text (browser_type/browser_fill_form CANNOT reach a
  field with no ref — fill_by_label is the only way to type into one), and
  select_option_by_label for a custom dropdown whose toggle has no text of
  its own (an icon only) so click_by_text can't target it. Use these four
  ONLY for that situation, not as your normal way to read/fill/click the page.
- Tools do NOT go "offline" and there is no "cooldown" — if something fails,
  re-snapshot and retry; never claim the server crashed or ask me to type
  "retry". You run autonomously to completion.

NEVER SPEND MONEY — ABSOLUTE RULE. You must NEVER enter payment details, and
NEVER proceed past any checkout, payment, or paid-registration step. If applying
or registering requires a fee — wording like "lidmaatschap", "inschrijfkosten",
"registratiekosten", "servicekosten voor inschrijving", "€X eenmalig/per jaar
om te reageren", an iDEAL/creditcard/Mollie/PayPal payment screen, or any
"betalen"/"afrekenen" button gating the application — STOP IMMEDIATELY without
paying and report `OUTCOME: payment_required`, stating the fee and what it was
for. A normal FREE application form that merely asks for your income type or a
credit-check consent checkbox is NOT a payment and is fine to complete. When in
doubt about whether something costs money, do NOT proceed: stop and report
payment_required. (A one-time paid email-alert subscription is the separate
upsell trap already described above — same rule: never pay.)

STOP EARLY WHEN THE LISTING CANNOT BE RESPONDED TO. If the property shows any of:
paywall ("Plus"/"upgrade required"), a paid registration/membership requirement
(report payment_required, per the rule above), not eligible ("je komt niet in
aanmerking"), suitability still being recalculated, or the "reageer"/apply
control is simply absent — then STOP IMMEDIATELY and report that status.

ALREADY-APPLIED = STOP (never resubmit). If the page offers to MODIFY, CHANGE,
WITHDRAW, or CONTINUE an existing request — e.g. "Aanvraag wijzigen", "wijzig je
reactie", "Reactie intrekken", "je hebt gereageerd", "Bezichtiging aangevraagd",
"Je bericht is verstuurd", "Doorgaan met gesprek" — that means an application was
ALREADY submitted for this property. STOP immediately and report it; do NOT
re-open, re-fill, or resubmit.
IMPORTANT distinction: a form whose fields are PRE-FILLED with your personal data
or that already shows your saved documents is just the site remembering your
profile — that ALONE does NOT mean you already applied. Judge "already applied"
by the control wording / an explicit "already requested/sent" status, NOT by
pre-filled data. When the entry control is a normal apply button ("Bezichtiging
aanvragen", "Reageer"), proceed and submit.

BEWARE PAID UPSELL DIALOGS DISGUISED AS "APPLY"/"REGISTER". Some rental-agency
sites (verified on REBO Groep, Hof van Oslo listing) have a prominent button
labelled something like "Inschrijven huuraanbod" ("Register for rental offer")
that actually opens a PAID email-alert subscription signup — a recurring fee
(e.g. "€34,95 per jaar"), wording like "e-mailservice"/"zoekopdracht aanmaken" —
NOT an application for this specific property. NEVER fill or submit a dialog
that mentions a recurring price or a search-alert subscription; close it and
use the real per-listing action instead: usually a button like "Bezichtiging
aanvragen" ("Request a viewing") that opens a form asking for your name, email,
phone, and income type to register interest in THIS property (no fee, may ask
you to consent to a credit check once assigned — that consent checkbox is
normal, not a red flag). Trust that concrete in-page action over prose in the
description that tells you to sign up on a separate site/account — the direct
dialog is usually the real application path even when the description text
suggests a different portal.

Do NOT wander the rest of the site, open your profile, or take unrelated
account actions looking for a workaround.

Be decisive. Do not ask me questions mid-task; make reasonable choices and
proceed. Speed matters: complete the application as fast as safely possible.
Never output a page snapshot as your final answer. Finish the job: upload the
documents and SUBMIT. Only stop when you have submitted, or state the exact
blocking reason in one short paragraph.

FINAL ANSWER FORMAT: end your final message with one short status paragraph, then
a last line that is EXACTLY one of:
  OUTCOME: submitted          (you completed and submitted a new application)
  OUTCOME: already_applied    (an application already existed; you did not resubmit)
  OUTCOME: not_available      (listing expired/archived/removed)
  OUTCOME: not_eligible       (you do not qualify / blocked by criteria)
  OUTCOME: login_required     (could not log in / account needed)
  OUTCOME: payment_required   (applying/registering requires paying a fee; you did NOT pay)
  OUTCOME: blocked            (any other reason you could not submit)
"""


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (s or "listing")[:50]


def apply(listing: dict, model: str = APPLY_MODEL,
          yield_to_priority: bool = False) -> AgentResult:
    """Run the apply agent on one listing. Returns an AgentResult with the true
    outcome (submitted / already_applied / not_available / ... / timeout).

    yield_to_priority: set by the poller's applier. The run then checks the
    mail-apply priority flag once per turn and aborts with outcome "yielded"
    (listing untouched, caller requeues) so a time-critical mail-triggered
    apply gets the shared browser within seconds. Mail/manual runs ARE the
    priority path and leave this off."""
    listing_price = parse_rent(listing.get("price"))
    if listing_price is not None and listing_price > MAX_RENT:
        summary = (
            f"Skipped before opening the browser: listed rent €{listing_price:.0f} "
            f"is above the configured max rent €{MAX_RENT:.0f}."
        )
        print(f"[apply] {summary}")
        return AgentResult(rc=0, outcome="not_eligible", summary=summary)

    payment_reason = _payment_required_reason(listing)
    if payment_reason:
        summary = (
            "Skipped before opening the browser: applying or registering "
            f"requires payment ({payment_reason}). I did not pay."
        )
        print(f"[apply] {summary}")
        return AgentResult(rc=0, outcome="payment_required", summary=summary)

    prompt = build_prompt(listing)
    # Persist a per-run transcript + prompt so nothing is overwritten/lost.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slug(f"{listing.get('source_name','')}-{listing.get('address','')}")
    run_dir = LOG_DIR / "transcripts"
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / f"{ts}_{slug}.log"
    (run_dir / f"{ts}_{slug}.prompt.txt").write_text(prompt, encoding="utf-8")
    (LOG_DIR / "last_apply_prompt.txt").write_text(prompt, encoding="utf-8")

    print(f"[apply] launching agent ({model}) for {listing['source_url']}")
    print(f"[apply] transcript: {transcript}")
    # Exclusive browser lock: only one component drives the shared CDP browser
    # at a time. Coordinates the Stekkies orchestrator and the poller's applier.
    with browser_lock(holder=f"apply:{slug}"):
        result = None
        if APPLY_FASTPATH_ENABLED:
            result = try_fast_apply(
                listing=listing,
                cdp_url=CDP_URL,
                log_path=transcript,
                message=REFERENCE_APPLICATION_MESSAGE.replace(
                    "[[ADDRESS]]", listing.get("address") or "de woning"),
            )
        if result is None:
            result = run_agent(
                prompt=prompt,
                model=model,
                max_turns=APPLY_MAX_TURNS,
                cdp_url=CDP_URL,
                log_path=transcript,
                timeout_seconds=APPLY_TIMEOUT_SECONDS,
                source_url=listing["source_url"],
                yield_check=priority_pending if yield_to_priority else None,
            )
    # Keep the convenience "latest" copy too.
    try:
        (LOG_DIR / "last_apply_output.txt").write_text(
            transcript.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    print(f"[apply] ----- agent finished: outcome={result.outcome} (rc={result.rc}) -----")
    result.transcript_path = str(transcript)
    # Distill durable site knowledge out of this run for the next one on the
    # same domain(s). Fail-open and outside the browser lock — see the module.
    site_playbooks.update_after_run(listing, result)
    return result


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_DIR / "last_listing.json"
    listing = json.loads(path.read_text(encoding="utf-8"))
    result = apply(listing)
    print(f"OUTCOME: {result.outcome}")
    return 0 if result.rc == 0 else result.rc


if __name__ == "__main__":
    sys.exit(main())
