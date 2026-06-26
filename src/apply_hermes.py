"""Apply stage: hand the external listing to our browser agent loop.

Builds a precise task prompt (source URL, reference message, document list,
auto-submit instruction) and runs the lightweight agent loop in
`src.browser_agent` (OpenRouter LLM + Playwright MCP over our shared CDP
browser). The agent adapts to whatever application form the source site shows.

Run standalone:  python -m src.apply_hermes logs/last_listing.json
"""
import json
import os
import sys
from pathlib import Path

from .config import DOCS_DIR, LOG_DIR, CDP_URL
from .credentials import for_url
from .message_template import REFERENCE_APPLICATION_MESSAGE
from .browser_agent import run_agent

# Model for the apply agent. Default: google/gemini-3.5-flash — cheap, fast,
# and (with the tool guidance below) reliably drives complex multi-step Dutch
# housing portals using snapshot refs. Override via APPLY_MODEL / HERMES_MODEL.
APPLY_MODEL = os.environ.get("APPLY_MODEL", os.environ.get("HERMES_MODEL", "google/gemini-3.5-flash"))
APPLY_MAX_TURNS = int(os.environ.get("APPLY_MAX_TURNS", "60"))
APPLY_TIMEOUT_SECONDS = int(os.environ.get("APPLY_TIMEOUT_SECONDS", "900"))

# Google account used for "Sign in with Google" SSO on source sites (Funda etc.).
GOOGLE_ACCOUNT = os.environ.get("GOOGLE_ACCOUNT", "you@example.com")


def _doc_list() -> str:
    if not DOCS_DIR.exists():
        return "(WARNING: documents folder not found at %s)" % DOCS_DIR
    files = sorted(p for p in DOCS_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    return "\n".join(f"  - {p}" for p in files)


def build_prompt(listing: dict) -> str:
    cred = for_url(listing["source_url"])
    sso = (
        f"If the site offers \"Sign in with Google\" / \"Continue with Google\" "
        f"(e.g. Funda), PREFER that: click it and complete sign-in with the "
        f"Google account {GOOGLE_ACCOUNT}. The browser should already have a "
        f"Google session, so this is usually one or two clicks. Only fall back "
        f"to email/password login if Google sign-in is not offered."
    )
    if cred:
        login_clause = (
            f"{sso}\n"
            f"If using email/password instead, use these credentials:\n"
            f"   username/email: {cred.get('username','')}\n"
            f"   password: {cred.get('password','')}\n"
            "Log in first, then proceed to the application."
        )
    else:
        login_clause = (
            f"{sso}\n"
            "If neither Google sign-in nor stored credentials are available and "
            "the site requires an account, stop and report that login is needed."
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
   Do not invent new personal details.
4. Upload ALL of these documents wherever the form accepts attachments
   (id, payslips, employer statement, etc.). Match document type to field
   where the field asks for a specific document; otherwise attach all:
{_doc_list()}
5. {submit_clause}
6. Report: did it submit, any errors, and what fields you could not fill.

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
suitability still being recalculated, a message/response already sent, or the
"reageer"/apply control is simply absent — then STOP IMMEDIATELY and report that
status. Do NOT wander the rest of the site, open your profile, or take unrelated
account actions looking for a workaround.

Be decisive. Do not ask me questions mid-task; make reasonable choices and
proceed. Speed matters: complete the application as fast as safely possible.
Never output a page snapshot as your final answer. Finish the job: upload the
documents and SUBMIT. Only stop when you have submitted, or state the exact
blocking reason in one short paragraph.
"""


def apply(listing: dict, model: str = APPLY_MODEL) -> int:
    """Run the apply agent on one listing. Returns 0 on success, 124 on
    timeout, 1 if the turn budget was exhausted, 2 on setup error."""
    prompt = build_prompt(listing)
    (LOG_DIR / "last_hermes_prompt.txt").write_text(prompt, encoding="utf-8")
    print(f"[apply] launching agent ({model}) for {listing['source_url']}")
    rc = run_agent(
        prompt=prompt,
        model=model,
        max_turns=APPLY_MAX_TURNS,
        cdp_url=CDP_URL,
        log_path=LOG_DIR / "last_hermes_output.txt",
        timeout_seconds=APPLY_TIMEOUT_SECONDS,
    )
    print(f"[apply] ----- agent finished (exit {rc}) -----")
    return rc


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_DIR / "last_listing.json"
    listing = json.loads(path.read_text(encoding="utf-8"))
    return apply(listing)


if __name__ == "__main__":
    sys.exit(main())
