"""Capture the application form so we can build a deterministic filler.

Reuses the logged-in profile, opens a listing URL, and dumps:
  - logs/listing.html       (full rendered HTML)
  - logs/form_fields.txt     (inputs/selects/textareas/buttons + labels)
  - logs/screenshots/listing.png

Run:  python -m src.capture_form "<listing_url>"
"""
import sys
from playwright.sync_api import sync_playwright

from .config import USER_DATA_DIR, LOG_DIR, SCREENSHOT_DIR

DUMP_JS = r"""
() => {
  const out = [];
  const desc = (el) => {
    const id = el.id ? `#${el.id}` : '';
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${el.id}"]`);
      if (l) label = l.innerText.trim();
    }
    if (!label && el.closest('label')) label = el.closest('label').innerText.trim();
    return {
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      name: el.name || '',
      id: el.id || '',
      placeholder: el.placeholder || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      label: label.slice(0, 80),
      text: (el.innerText || el.value || '').trim().slice(0, 80),
      accept: el.getAttribute('accept') || '',
    };
  };
  document.querySelectorAll('input, select, textarea, button, [type=submit], form')
    .forEach(el => out.push(desc(el)));
  return out;
}
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m src.capture_form <listing_url>")
        return 2
    url = sys.argv[1]
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR), headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(2000)

        (LOG_DIR / "listing.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(SCREENSHOT_DIR / "listing.png"), full_page=True)

        fields = page.evaluate(DUMP_JS)
        lines = [f"URL: {page.url}", f"TITLE: {page.title()}", ""]
        for f in fields:
            lines.append(
                f"<{f['tag']}> type={f['type']!r} name={f['name']!r} id={f['id']!r} "
                f"accept={f['accept']!r} label={f['label']!r} ph={f['placeholder']!r} "
                f"aria={f['ariaLabel']!r} text={f['text']!r}"
            )
        (LOG_DIR / "form_fields.txt").write_text("\n".join(lines), encoding="utf-8")
        print("Wrote logs/listing.html, logs/form_fields.txt, screenshots/listing.png")
        print(f"Found {len(fields)} interactive elements. Current URL: {page.url}")
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
