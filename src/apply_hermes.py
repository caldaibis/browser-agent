"""Apply stage: hand the external listing to the Hermes browser agent.

Builds a precise task prompt (source URL, reference message, document list,
auto-submit instruction) and runs Hermes non-interactively with the browser +
file toolsets. Hermes adapts to whatever application form the source site shows.

Run standalone:  python -m src.apply_hermes logs/last_listing.json
"""
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import DOCS_DIR, LOG_DIR
from .credentials import for_url
from .message_template import REFERENCE_APPLICATION_MESSAGE

# Model for the apply agent. Default: google/gemini-3.5-flash — cheap, fast,
# Hermes-compatible, and (with the tool guidance below) reliably drives complex
# multi-step Dutch housing portals using snapshot refs. Override via HERMES_MODEL.
# NB: z-ai/glm-5.2 was tried but stalls with empty/reasoning-only responses in
# Hermes's tool loop (OpenRouter reasoning-surfacing issue) — avoid for now.
HERMES_MODEL = os.environ.get("HERMES_MODEL", "google/gemini-3.5-flash")
HERMES_TIMEOUT_SECONDS = int(os.environ.get("HERMES_TIMEOUT_SECONDS", "900"))

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
"""


def _run_streaming(cmd: list[str], logfile: Path, timeout_seconds: int = HERMES_TIMEOUT_SECONDS) -> int:
    """Run cmd attached to a pty so Hermes streams its FULL live output (rich
    tool previews, spinners, model text) straight to this terminal, while also
    teeing a plain copy to logfile. Returns the process exit code. Returns 124
    when the command exceeds timeout_seconds."""
    import pty

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)
    deadline = time.monotonic() + timeout_seconds
    timed_out = False

    with open(logfile, "wb") as lf:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 and proc.poll() is None:
                timed_out = True
                msg = f"\n[apply] Hermes timed out after {timeout_seconds}s; terminating.\n".encode()
                os.write(sys.stdout.fileno(), msg)
                lf.write(msg)
                lf.flush()
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                break

            readable, _, _ = select.select([master_fd], [], [], max(0.1, min(1.0, remaining)))
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    os.write(sys.stdout.fileno(), data)
                    lf.write(data)
                    lf.flush()

            if proc.poll() is not None:
                while True:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    os.write(sys.stdout.fileno(), data)
                    lf.write(data)
                break

    os.close(master_fd)
    return 124 if timed_out else int(proc.returncode or 0)


def apply(listing: dict, model: str = HERMES_MODEL) -> int:
    prompt = build_prompt(listing)
    (LOG_DIR / "last_hermes_prompt.txt").write_text(prompt, encoding="utf-8")
    cmd = [
        "hermes", "chat",
        "-q", prompt,
        # Playwright MCP only: efficient high-level snapshot/click/fill_form +
        # browser_file_upload, all attached to our CDP browser. We deliberately
        # do NOT enable Hermes's built-in `browser` toolset, whose low-level
        # browser_cdp tempts the model into dozens of raw JS evals + full-page
        # innerText dumps (the token bleed we saw).
        "-t", "playwright",
        "--yolo",          # auto-approve tool calls (no interactive prompts)
        "-v",              # verbose: full tool calls + reasoning in the stream
        "--max-turns", "60",  # complex multi-step portals need headroom
    ]
    if model:
        cmd[2:2] = ["-m", model]  # insert right after "chat", not between -t/value
    print(f"[apply] launching Hermes for {listing['source_url']}")
    print("[apply] ----- live Hermes output -----")
    rc = _run_streaming(cmd, LOG_DIR / "last_hermes_output.txt")
    print(f"\n[apply] ----- Hermes finished (exit {rc}) -----")
    if rc != 0:
        print("[apply] Hermes exited non-zero:", rc)
    return rc


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_DIR / "last_listing.json"
    listing = json.loads(path.read_text(encoding="utf-8"))
    return apply(listing)


if __name__ == "__main__":
    sys.exit(main())
