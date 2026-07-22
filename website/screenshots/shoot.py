"""Capture the website screenshots from a running test stack.

Assumes `seed.py` has already built the demo mailbox. Drives the real UI in
Chromium rather than mocking anything, so a shot that renders here is a shot the
app actually produces.

    make screenshots
    .venv-test/bin/python website/screenshots/shoot.py --only markdown

Output lands in website/public/img/screenshots/ at 2x device scale — the page
serves 2880x1800 files and displays them at half that, so they stay sharp on
retina panels.

Two things bite repeatedly and are handled below rather than left to chance:
the SSE stream schedules a 500 ms debounced re-render on every event, which can
repaint mid-capture; and the markdown composer only styles text that arrives
through real key events, so its body is typed rather than filled.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(__file__))
import meerato_stub  # noqa: E402
from seed import touch_agent  # noqa: E402

URL = os.environ.get("MEERAIL_URL", "http://127.0.0.1:18000")
OUT = os.path.join(os.path.dirname(__file__), "..", "public", "img", "screenshots")

# 1440x900 at 2x. Matches the existing files on the site.
VIEWPORT = {"width": 1440, "height": 900}
SCALE = 2

# Long enough for the SSE debounce (500 ms) plus the re-render it triggers.
SETTLE = 900


async def boot(page, *, age_days: int = 30) -> None:
    """Load the app and wait until it has stopped moving.

    localStorage is seeded before the first navigation so the chrome is
    deterministic: the shortcut box expanded, a known list width, and a known
    age-tint horizon.
    """
    # Re-stamped per view, not once per run: the agent-stopped banner appears
    # after 120 s of silence, and a full run takes longer than that.
    touch_agent()
    await page.add_init_script(
        f"""
        localStorage.setItem('meerail.age-days', '{age_days}');
        localStorage.setItem('meerail.listWidth', '360');
        localStorage.setItem('meerail.shortcuts.collapsed', '0');
        localStorage.setItem('meerail.sync.collapsed', '1');
        """
    )
    await page.goto(URL, wait_until="domcontentloaded")
    await page.wait_for_selector("#mailbox-tree .mailbox-row")
    await page.wait_for_selector("#message-list .msg-row, #message-list .list-empty")
    await page.evaluate("document.fonts.ready")
    await page.wait_for_timeout(SETTLE)


async def select_unified(page) -> None:
    await page.click('.mailbox-row[data-key="unified"]')
    await page.wait_for_selector("#message-list .msg-row")
    await page.wait_for_timeout(SETTLE)


async def open_row(page, text: str) -> None:
    """Open the first list row whose subject contains `text`."""
    row = page.locator("#message-list .msg-row", has_text=text).first
    await row.click()
    await page.wait_for_selector("#reader-content:not([hidden])")
    await page.wait_for_selector(".msg-row.active")
    await settle_images(page)
    await page.wait_for_timeout(SETTLE)


async def settle_images(page) -> None:
    """Attachment previews are lazy — scroll them in and wait for real pixels,
    or the shot catches empty boxes."""
    chips = page.locator(".attachment-chip.has-thumb img.att-thumb")
    if await chips.count() == 0:
        return
    await chips.first.scroll_into_view_if_needed()
    await page.wait_for_function(
        """() => [...document.querySelectorAll('.attachment-chip img.att-thumb')]
                 .every(i => i.complete && i.naturalWidth > 0)"""
    )


async def run_search(page, query: str, *, regex: bool = False) -> None:
    if regex:
        await page.check("#rx-toggle")
    await page.fill("#search-input", query)
    # The box debounces at 280 ms; wait for the status line to resolve rather
    # than for a fixed delay.
    await page.wait_for_selector("#search-status:not(:empty)")
    await page.wait_for_function(
        """() => /\\d+ results?|No results/.test(
                   document.querySelector('#search-status').textContent)"""
    )
    await page.wait_for_timeout(SETTLE)


async def shot(page, name: str) -> None:
    path = os.path.abspath(os.path.join(OUT, f"{name}.png"))
    await page.screenshot(path=path, scale="device")
    print(f"  wrote {os.path.relpath(path, os.getcwd())}")


# --- the individual views ----------------------------------------------------

async def cap_inbox(page, dark: bool = False) -> None:
    await boot(page)
    await select_unified(page)
    # The invoice rather than the photo thread: this is the hero shot and the
    # photos belong to `attachments`, which would otherwise be the same frame
    # twice. It also carries a flag and a PDF preview, so the reading pane has
    # something in it besides text.
    await open_row(page, "Jahresabrechnung")
    await shot(page, "inbox-dark" if dark else "inbox")


async def cap_age(page) -> None:
    """The age tint, with Settings open so the control that drives it is in
    frame. A short horizon so the older mail saturates."""
    await boot(page, age_days=14)
    await select_unified(page)
    await page.click("#btn-settings")
    await page.wait_for_selector("#settings-modal:not([hidden])")
    # Centre the Appearance block rather than merely making it visible —
    # scroll_into_view_if_needed leaves it flush with the bottom edge, where the
    # Accounts list above it dominates the frame.
    await page.evaluate(
        "document.querySelector('#age-days').scrollIntoView({block: 'center'})"
    )
    await page.wait_for_timeout(400)
    tinted = await page.locator('.msg-row[style*="--age-t"]').count()
    if tinted == 0:
        sys.exit("age tint: no rows carry --age-t; is the unified inbox selected?")
    await shot(page, "age")


async def cap_search(page) -> None:
    """A regex hit that exists only inside a PDF."""
    await boot(page)
    await select_unified(page)
    # No word boundaries here on purpose: the router validates with Python `re`
    # (which rejects Postgres's \y) but matches with Postgres `~*` (where \b is
    # backspace), so neither spelling works. See website/screenshots/README.md.
    await run_search(page, r"terminat(e|ion)", regex=True)
    await page.locator("#message-list .msg-row").first.click()
    await page.wait_for_selector("#reader-content:not([hidden])")
    await page.wait_for_timeout(SETTLE)
    await shot(page, "search")


async def cap_ocr(page) -> None:
    """A regex hit that exists only as pixels in a scanned PNG."""
    await boot(page)
    await select_unified(page)
    await run_search(page, r"RN-2024-\d{4}", regex=True)
    hits = await page.locator("#message-list .msg-row").count()
    if hits == 0:
        sys.exit("ocr: no results — did Tika OCR the scan? (needs the -full image)")
    await page.locator("#message-list .msg-row").first.click()
    await page.wait_for_selector("#reader-content:not([hidden])")
    await settle_images(page)
    await page.wait_for_timeout(SETTLE)
    await shot(page, "ocr")


async def cap_thread(page) -> None:
    await boot(page)
    await select_unified(page)
    await open_row(page, "Q3 planning")
    await shot(page, "thread")


async def cap_attachments(page) -> None:
    await boot(page)
    await select_unified(page)
    await open_row(page, "Photos from Sunday")
    chips = await page.locator(".attachment-chip.has-thumb").count()
    if chips < 2:
        sys.exit(f"attachments: only {chips} thumbnail chips — did thumb_pending run?")
    await shot(page, "attachments")


async def cap_markdown(page) -> None:
    """Markdown styled as you type, with the markers left in the text.

    Typed with real key events: the editor repaints on `input`, so `fill()` or
    setting innerHTML produces an unstyled block.
    """
    await boot(page)
    await select_unified(page)
    await page.click('#reader-bar .tb-btn[data-act="new"]')
    await page.wait_for_selector("#compose-modal:not([hidden])")
    await page.fill("#compose-to", "priya@northwind-example.com")
    await page.fill("#compose-subject", "Release notes — 2.1")
    await page.click("#compose-body")
    # Clear whatever the footer prefilled, so the body is only our sample.
    await page.keyboard.press("ControlOrMeta+a")
    await page.keyboard.press("Delete")

    # The bulk goes in through the editor's own paste path rather than as
    # keystrokes. Enter is intercepted for list continuation — it carries "- "
    # onto the next line — so typing a pre-formatted bullet list character by
    # character produces doubled markers. Paste routes through the same
    # insertText()/rebuild() the editor uses internally and lands verbatim.
    body = (
        "## What shipped\n"
        "\n"
        "The **migration** is done and the _old scheduler_ is off.\n"
        "\n"
        "- Billing reconciliation moved to Q4\n"
        "- Observability dashboards repointed\n"
        "\n"
        "> Nothing here is decided until Thursday.\n"
        "\n"
        "Run `make deploy` and see "
    )
    await page.evaluate(
        """(text) => {
            const el = document.querySelector('#compose-body');
            const dt = new DataTransfer();
            dt.setData('text/plain', text);
            el.dispatchEvent(new ClipboardEvent('paste', {
                clipboardData: dt, bubbles: true, cancelable: true,
            }));
        }""",
        body,
    )
    await page.wait_for_timeout(200)
    # The tail is typed for real, so the shot is of a live editor mid-sentence
    # with the caret in it — which is what the caption on the site claims.
    await page.keyboard.type("[the plan](https://example.com/plan).", delay=25)

    # Prove the styling actually applied before spending a screenshot on it.
    for sel in ("#compose-body .md-h", "#compose-body .md-li",
                "#compose-body .md-quote", "#compose-body .md-mark",
                "#compose-body code.md-code"):
        if await page.locator(sel).count() == 0:
            sys.exit(f"markdown: nothing matched {sel} — did the editor repaint?")
    await page.wait_for_timeout(400)
    await shot(page, "markdown")


async def cap_task(page) -> None:
    """The Add Task dialog, filing a mail into Meerato.

    The buttons only exist once a Meerato URL is stored, and the dialog's selects
    are populated from Meerato itself — so this stands a stub up, points the
    setting at it, and takes the setting away again afterwards. Written straight
    to the settings table rather than through the UI because saving there probes
    the URL and the probe would have to pass anyway.
    """
    from core.database import SessionLocal
    from core.models import Setting

    httpd, port = meerato_stub.serve()
    url = f"http://{meerato_stub.gateway()}:{port}/api/create?token=demo-token"
    try:
        with SessionLocal() as db:
            db.merge(Setting(key="meerato_url", value=url))
            db.commit()

        await boot(page)
        await select_unified(page)
        await open_row(page, "Jahresabrechnung")
        # The per-message button, not the thread bar's: it carries a text label
        # rather than being icon-only, which reads better in a screenshot.
        await page.locator('.msg-toolbar .tb-btn[data-act="task"]').first.click()
        await page.wait_for_selector("#task-modal:not([hidden])")
        # The selects arrive from the stub; Create stays disabled until they do.
        await page.wait_for_selector("#task-selects select#task-bucket")
        await page.wait_for_selector("#task-create:not([disabled])")
        await page.wait_for_timeout(400)
        await shot(page, "task")
    finally:
        httpd.shutdown()
        with SessionLocal() as db:
            row = db.get(Setting, "meerato_url")
            if row is not None:
                db.delete(row)
                db.commit()


async def cap_stats(page) -> None:
    """The statistics modal, over the whole history rather than the last month.

    A year is the window that has something to show: the seeded history spans
    358 days (website/screenshots/seed.py), so a 30-day view would draw a
    fraction of it and leave the heatmap nearly empty.
    """
    await boot(page)
    await select_unified(page)
    await page.click("#btn-stats")
    await page.wait_for_selector("#stats-modal:not([hidden])")
    await page.click('.an-range[data-range="1y"]')
    # Every panel is drawn from one response, so the last one to appear means
    # the payload landed and rendered.
    await page.wait_for_selector(".an-kpi-value")
    await page.wait_for_selector(".an-corr")
    heat = await page.locator(".an-cell:not(.s0)").count()
    if heat < 40:
        sys.exit(f"stats: only {heat} populated heatmap cells — did _history() seed?")
    await page.wait_for_timeout(SETTLE)
    await shot(page, "stats")


# Nine, so the site's four-across grid runs three-four-two with the last tile
# starting a short row. Each one is the evidence for a specific feature card.
VIEWS = {
    "inbox": cap_inbox,
    "thread": cap_thread,
    "search": cap_search,
    "ocr": cap_ocr,
    "attachments": cap_attachments,
    "markdown": cap_markdown,
    "age": cap_age,
    "task": cap_task,
    "stats": cap_stats,
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=list(VIEWS),
                    help="capture just these views (default: all)")
    args = ap.parse_args()
    # Declaration order, not alphabetical: it matches the order the tiles appear
    # on the site, which makes a full run easy to eyeball against the page.
    wanted = args.only or list(VIEWS)

    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for name in wanted:
                print(f"{name}…")
                ctx = await browser.new_context(
                    viewport=VIEWPORT, device_scale_factor=SCALE,
                    color_scheme="light", reduced_motion="reduce",
                    locale="en-GB", timezone_id="Europe/Berlin",
                )
                page = await ctx.new_page()
                await VIEWS[name](page)
                await ctx.close()

            # The inbox is the only shot the site serves in both themes.
            if "inbox" in wanted:
                print("inbox-dark…")
                ctx = await browser.new_context(
                    viewport=VIEWPORT, device_scale_factor=SCALE,
                    color_scheme="dark", reduced_motion="reduce",
                    locale="en-GB", timezone_id="Europe/Berlin",
                )
                page = await ctx.new_page()
                await cap_inbox(page, dark=True)
                await ctx.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
