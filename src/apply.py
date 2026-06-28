"""Apply stage: hand the external listing to our browser agent loop.

Builds a precise task prompt (source URL, reference message, document list,
auto-submit instruction) and runs the lightweight agent loop in
`src.browser_agent` (OpenRouter LLM + Playwright MCP over our shared CDP
browser). The agent adapts to whatever application form the source site shows.

Run standalone:  python -m src.apply logs/last_listing.json
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .config import DOCS_DIR, LOG_DIR, CDP_URL
from .credentials import available_domains
from .message_template import REFERENCE_APPLICATION_MESSAGE
from .browser_agent import run_agent, AgentResult

# Model for the apply agent. Default: z-ai/glm-5.2 — strong tool-use/agentic
# model. (Its earlier empty-response failure was a Hermes-loop quirk; our own
# OpenRouter loop reads content/tool_calls directly.) gemini-3.5-flash proved
# too flaky (degenerate loops). Override via APPLY_MODEL.
APPLY_MODEL = os.environ.get("APPLY_MODEL", "z-ai/glm-5.2")
APPLY_MAX_TURNS = int(os.environ.get("APPLY_MAX_TURNS", "60"))
APPLY_TIMEOUT_SECONDS = int(os.environ.get("APPLY_TIMEOUT_SECONDS", "900"))

# Google account used for "Sign in with Google" SSO on source sites (Funda etc.).
GOOGLE_ACCOUNT = os.environ.get("GOOGLE_ACCOUNT", "you@example.com")


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
    if "uwv" in n or "verzekeringsbericht" in n:
        return 6, "UWV statement — independently confirms the permanent contract and work history."
    if "jaaropgave" in n and "degiro" not in n:
        return 7, "Annual income statement — secondary income proof."
    if "bankafschrift" in n or "bankstatement" in n:
        return 8, "Proof of salary deposit (and rent paid) — privacy-light extract."
    if "degiro" in n:
        return 9, "Investment statement — extra asset proof, only for strict income-multiple cases."
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
        "If lookup_credential returns no match and the site requires an account, "
        "stop and report that login is needed. NEVER reset, change, or recover a "
        "password (no \"wachtwoord vergeten\" / \"forgot password\" flow): if a "
        "stored password is rejected, stop and report a login failure."
    )
    submit_clause = "Then SUBMIT the application. Confirm submission succeeded."
    return f"""You are applying to a Dutch rental listing on my behalf. Act autonomously.

LISTING (external source: {listing.get('source_name','?')})
  Address: {listing.get('address','?')}
  Price:   {listing.get('price','?')}
  Apply at this URL: {listing['source_url']}

LOGIN
{login_clause}

YOUR TASK
1. Open the URL above in the browser.
2. Find the apply / "reageer" / contact / application form for this property.
3. Fill the application form using the applicant details contained in the
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
4. Upload documents BEFORE the final submit, into whatever attachment slots the
   form provides. Match a document to a labelled slot when the label names it
   (use the "why it matters" note to pick the right one); otherwise put it in a
   generic/extra slot. If a slot accepts multiple files, add several at once.
   Slots are often LIMITED — do not loop hunting for more slots or re-uploading
   the same file. The list below is ALREADY in priority order, so attach from the
   top down and stop when slots run out. Each line is "<path> — why it matters":
{_doc_list()}
5. {submit_clause}
6. After you see a submission/confirmation message, you are DONE: report success
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
- Tools do NOT go "offline" and there is no "cooldown" — if something fails,
  re-snapshot and retry; never claim the server crashed or ask me to type
  "retry". You run autonomously to completion.

STOP EARLY WHEN THE LISTING CANNOT BE RESPONDED TO. If the property shows any of:
paywall ("Plus"/"upgrade required"), not eligible ("je komt niet in aanmerking"),
suitability still being recalculated, or the "reageer"/apply control is simply
absent — then STOP IMMEDIATELY and report that status.

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
  OUTCOME: blocked            (any other reason you could not submit)
"""


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (s or "listing")[:50]


def apply(listing: dict, model: str = APPLY_MODEL) -> AgentResult:
    """Run the apply agent on one listing. Returns an AgentResult with the true
    outcome (submitted / already_applied / not_available / ... / timeout)."""
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
    result = run_agent(
        prompt=prompt,
        model=model,
        max_turns=APPLY_MAX_TURNS,
        cdp_url=CDP_URL,
        log_path=transcript,
        timeout_seconds=APPLY_TIMEOUT_SECONDS,
    )
    # Keep the convenience "latest" copy too.
    try:
        (LOG_DIR / "last_apply_output.txt").write_text(
            transcript.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    print(f"[apply] ----- agent finished: outcome={result.outcome} (rc={result.rc}) -----")
    return result


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_DIR / "last_listing.json"
    listing = json.loads(path.read_text(encoding="utf-8"))
    result = apply(listing)
    print(f"OUTCOME: {result.outcome}")
    return 0 if result.rc == 0 else result.rc


if __name__ == "__main__":
    sys.exit(main())
