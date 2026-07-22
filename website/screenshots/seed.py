"""Build the demo mailbox the website screenshots are taken from.

Runs against the *test* stack, never production: the database name must end in
``_test`` or this refuses to start, same guard as tests/conftest.py. Everything
here is fictional — invented people at example.com, invented invoices — because
the output ends up on a public marketing page.

    make screenshots        # seeds, then shoots
    .venv-test/bin/python website/screenshots/seed.py   # seed only

Messages are dated relative to *now* rather than to fixed timestamps, so the age
tint gradient (website/public/index.html's "Mail that lingers turns red" card)
looks the same whenever the shots are regenerated. Ingest happens through
``core.ingest`` — the same path the agent uses — so what the screenshots show is
what a real sync produces, including Tika extraction, OCR and thumbnails.
"""

from __future__ import annotations

import io
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import text  # noqa: E402
from core import ingest  # noqa: E402
from core.database import SessionLocal, engine  # noqa: E402
from core.models import Account, Attachment, Message  # noqa: E402

NOW = datetime.now(timezone.utc)


def _guard() -> None:
    """Refuse to seed anything but a _test database — this script deletes."""
    name = engine.url.database or ""
    if not name.endswith("_test"):
        sys.exit(
            f"refusing to seed {name!r}: the screenshot seed wipes every table, "
            "and only ever runs against a database whose name ends in _test. "
            "Point DATABASE_URL at the test stack (make test-up)."
        )


# --- generated attachments ---------------------------------------------------
#
# Built here rather than committed as binaries: the PDF has to contain the exact
# words the search screenshot greps for, and the OCR sample has to be a real
# raster that Tesseract can actually read. Keeping them as code keeps the two in
# step with the captions on the site.

def _pdf(title: str, lines: list[str]) -> bytes:
    import fitz  # PyMuPDF

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), title, fontname="helv", fontsize=17)
    y = 136
    for line in lines:
        page.insert_text((72, y), line, fontname="helv", fontsize=11)
        y += 19
    out = doc.tobytes()
    doc.close()
    return out


def _sans_font() -> str:
    """Any grotesque the host happens to have — asked of fontconfig rather than
    guessed from a path list, which differs per distro."""
    import subprocess

    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans-serif:style=Regular"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip() and os.path.exists(out.stdout.strip()):
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    sys.exit("no usable sans-serif font found (is fontconfig installed?)")


def _scan_png(lines: list[str]) -> bytes:
    """A rasterised 'scan' with no text layer — only OCR can read this.

    Rendered large and high-contrast because that is what Tesseract is good at;
    a small or anti-aliased sample makes the OCR screenshot a coin flip.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1240, 800), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(_sans_font(), 34)

    y = 70
    for line in lines:
        draw.text((70, y), line, fill="black", font=font)
        y += 58
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _photo(sky: str, hill: tuple[int, int, int], sun: str) -> bytes:
    """A colourful non-text image, so the thumbnail strip is not all documents.

    Three variants hang off one message, which is what makes the attachment
    screenshot show a *row* of previews rather than a single chip.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1400, 900), sky)
    draw = ImageDraw.Draw(img)
    draw.ellipse([980, 110, 1270, 400], fill=sun)
    for i in range(90):
        t = i / 90
        draw.line(
            [(0, int(900 * t)), (1400, int(900 * (1 - t) * 0.7 + 120))],
            fill=(
                max(0, min(255, int(hill[0] + 190 * t))),
                max(0, min(255, int(hill[1] + 120 * (1 - t)))),
                max(0, min(255, int(hill[2] - 90 * t))),
            ),
            width=11,
        )
    img.save(buf := io.BytesIO(), format="JPEG", quality=88)
    return buf.getvalue()


# --- message construction ----------------------------------------------------

def build(
    frm: str,
    to: str,
    subject: str,
    body: str,
    days_ago: float,
    *,
    msg_id: str | None = None,
    refs: str | None = None,
    cc: str = "",
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> tuple[str, bytes]:
    msg = EmailMessage()
    msg["Message-ID"] = msg_id or make_msgid(domain="mail.example.com")
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = format_datetime(NOW - timedelta(days=days_ago))
    if refs:
        # Both, the way a well-behaved client replies: threading.py prefers
        # References and falls back to In-Reply-To.
        msg["In-Reply-To"] = refs
        msg["References"] = refs
    msg.set_content(body)
    for filename, ctype, data in attachments or []:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg["Message-ID"], msg.as_bytes()


# The two accounts. Colours are the ones the sidebar dots and list stripes use;
# picked to stay distinguishable in both light and dark.
PERSONAL = "hannah.brandt@example.com"
WORK = "h.brandt@northwind-example.com"

# Invoice text lives in the PDF only — never in a subject or body. That is the
# whole point of the search screenshot: the hit has to come from the attachment.
INVOICE_PDF = None   # built in main(), needs PyMuPDF
SCAN_PNG = None
PHOTO_JPG = None


def _messages() -> list[dict]:
    """Every seeded message, newest first-ish. `days` drives the age tint."""
    thread_root = "<planning-2024-q3.root@mail.example.com>"

    return [
        # --- personal inbox ---------------------------------------------------
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=0.02, seen=False,
             frm="Ada Okonkwo <ada@riverside-example.org>", to=PERSONAL,
             subject="Keys for the weekend",
             body="I'll leave them with the neighbour on the ground floor — he's in\n"
                  "all Saturday. The bins go out Sunday night, everything else can\n"
                  "wait until you're back.\n\nAda"),
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=0.3, seen=False,
             frm="Bruno Català <bruno@cyclepath-example.com>", to=PERSONAL,
             subject="Photos from Sunday",
             body="Came out better than I expected given the weather. The one at the\n"
                  "top of the climb is my favourite.\n\nB",
             attachments=[("summit.jpg", "image/jpeg", "PHOTO1"),
                          ("descent.jpg", "image/jpeg", "PHOTO2"),
                          ("cafe-stop.jpg", "image/jpeg", "PHOTO3")]),
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=2.1, seen=True, flagged=True,
             frm="Rheinstrom Energie <service@rheinstrom-example.de>", to=PERSONAL,
             subject="Ihre Jahresabrechnung 2024",
             body="Guten Tag,\n\nanbei erhalten Sie Ihre Jahresabrechnung. Der Betrag\n"
                  "wird in den nächsten Tagen von Ihrem Konto abgebucht.\n\n"
                  "Mit freundlichen Grüßen\nRheinstrom Energie",
             attachments=[("jahresabrechnung-2024.pdf", "application/pdf", "INVOICE")]),
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=9.4, seen=True,
             frm="Dr. Sofia Lindqvist <praxis@lindqvist-example.se>", to=PERSONAL,
             subject="Appointment confirmation",
             body="This confirms your appointment on the 14th at 09:30. Please bring\n"
                  "the referral letter with you.\n\nReception"),
        # The scan. No text layer anywhere — the words only exist as pixels, so
        # a search that finds it proves OCR ran.
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=16.8, seen=True,
             frm="Praxis Lindqvist <praxis@lindqvist-example.se>", to=PERSONAL,
             subject="Scanned document",
             body="Scanned from the front desk. Sending it on as requested.",
             attachments=[("scan-2024-03-14.png", "image/png", "SCAN")]),
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=24.0, seen=True,
             frm="Nordlicht Verlag <abo@nordlicht-example.de>", to=PERSONAL,
             subject="Your subscription renews next month",
             body="Nothing to do — this is just a heads-up that the annual\n"
                  "subscription renews on the 1st. You can cancel any time before\n"
                  "then from your account page."),
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=33.5, seen=True,
             frm="Miriam Voss <miriam@allotment-example.org>", to=PERSONAL,
             subject="Allotment rota — still need two people",
             body="We're short for the last two weekends in the month. If nobody\n"
                  "signs up I'll ask the committee to move the working day.\n\nMiriam"),
        dict(account=PERSONAL, folder="INBOX", role="inbox", days=47.0, seen=True,
             frm="Kleinwald Möbel <bestellung@kleinwald-example.de>", to=PERSONAL,
             subject="Delivery window for your order",
             body="Your order is ready. The carrier will contact you to arrange a\n"
                  "delivery window — usually two working days ahead."),

        # --- work inbox: a real thread ---------------------------------------
        dict(account=WORK, folder="INBOX", role="inbox", days=1.2, seen=False,
             frm="Priya Raman <priya@northwind-example.com>", to=WORK,
             cc="team@northwind-example.com",
             subject="Re: Q3 planning — draft for review",
             msg_id="<planning-2024-q3.r2@mail.example.com>", refs=thread_root,
             body="Agreed on cutting the third workstream. One thing though: if we\n"
                  "keep the migration in scope we need the extra fortnight, and that\n"
                  "pushes the review past the board meeting.\n\n"
                  "Happy to present it either way — just tell me which.\n\nPriya"),
        dict(account=WORK, folder="INBOX", role="inbox", days=1.9, seen=True,
             frm="Tomasz Wieczorek <tomasz@northwind-example.com>", to=WORK,
             cc="team@northwind-example.com",
             subject="Re: Q3 planning — draft for review",
             msg_id="<planning-2024-q3.r1@mail.example.com>", refs=thread_root,
             body="Read it twice. The shape is right, but three parallel workstreams\n"
                  "with the headcount we actually have is wishful thinking.\n\n"
                  "Suggest we drop the third and revisit in Q4.\n\nT"),
        dict(account=WORK, folder="INBOX", role="inbox", days=2.6, seen=True,
             frm="Priya Raman <priya@northwind-example.com>", to=WORK,
             cc="team@northwind-example.com",
             subject="Q3 planning — draft for review",
             msg_id=thread_root,
             body="Draft attached. Nothing in here is decided — I want the argument\n"
                  "about scope to happen before the board meeting, not during it.\n\n"
                  "Comments by Thursday if you can.\n\nPriya",
             attachments=[("q3-planning-draft.pdf", "application/pdf", "PLANNING")]),

        dict(account=WORK, folder="INBOX", role="inbox", days=6.5, seen=True, flagged=True,
             frm="Legal <legal@vandermeer-example.nl>", to=WORK,
             subject="Revised agreement",
             body="Revised per your comments. The clause you asked about is now in\n"
                  "section 7; everything else is unchanged from the version you saw.",
             attachments=[("agreement-rev3.pdf", "application/pdf", "CONTRACT")]),
        dict(account=WORK, folder="INBOX", role="inbox", days=12.2, seen=True,
             frm="Ines Duarte <ines@northwind-example.com>", to=WORK,
             subject="Onboarding notes for the new starter",
             body="Wrote up what I wish someone had told me in my first week. Feel\n"
                  "free to cut anything that reads as too obvious.\n\nInes"),
        dict(account=WORK, folder="INBOX", role="inbox", days=21.7, seen=True,
             frm="observability-alerts <noreply@northwind-example.com>", to=WORK,
             subject="Weekly digest: 3 dashboards need attention",
             body="Three dashboards have panels querying a datasource that no longer\n"
                  "exists. This digest repeats weekly until they are fixed or muted."),
        dict(account=WORK, folder="INBOX", role="inbox", days=38.9, seen=True,
             frm="Facilities <facilities@northwind-example.com>", to=WORK,
             subject="Desk move — week of the 22nd",
             body="The whole floor shifts one bay north. Label anything you want\n"
                  "kept; unlabelled items go into storage."),
        dict(account=WORK, folder="INBOX", role="inbox", days=61.0, seen=True,
             frm="Ravi Shankar <ravi@northwind-example.com>", to=WORK,
             subject="Handover doc, as promised",
             body="Everything I know about the billing job is in here. Ping me if\n"
                  "something is missing — I'd rather write it down now than dig it\n"
                  "out of my memory in three months.\n\nRavi"),

        # --- sent + archive, so the folders are not empty --------------------
        dict(account=WORK, folder="Sent", role="sent", days=1.0, seen=True,
             frm=WORK, to="Priya Raman <priya@northwind-example.com>",
             subject="Re: Q3 planning — draft for review",
             refs="<planning-2024-q3.r2@mail.example.com>",
             body="Keep the migration in scope and take the fortnight. I'll square\n"
                  "the timing with the board — better a late review than a rushed\n"
                  "migration.\n\nHannah"),
        dict(account=PERSONAL, folder="Archive", role="archive", days=95.0, seen=True,
             frm="Ada Okonkwo <ada@riverside-example.org>", to=PERSONAL,
             subject="Re: that recipe",
             body="Found it. It's the one with the brown butter, not the one with\n"
                  "the cream — I knew I was misremembering.\n\nAda"),
    ]


# --- background history ------------------------------------------------------
#
# The hand-written messages above are staged for specific screenshots: each one
# is chosen and opened by subject. They are far too few to draw the statistics
# panel, whose charts need a year of traffic before "volume over time" or the
# hour-of-day heatmap say anything.
#
# So this generates the bulk underneath them — and files all of it in Archive
# and Sent, never INBOX. Every other shot works from the unified inbox and picks
# its row by subject, so filler landing there would push the staged mail out of
# frame and break them. Archive and Sent are invisible to those views and still
# count towards every analytics panel, which reads all folders but drafts and
# junk.

HISTORY_SEED = 20260722

# name, address, messages per week, how often we answer them
HISTORY_PEOPLE = [
    ("Priya Raman",     "priya@northwind-example.com",      13, 0.85),
    ("Tomas Lindqvist", "t.lindqvist@northwind-example.com", 9, 0.80),
    ("Aisha Bello",     "aisha@harbourline-example.co",      6, 0.75),
    ("Elena Vasquez",   "e.vasquez@meridian-example.org",    4, 0.65),
    ("Jonas Weber",     "jonas@harbourline-example.co",      3, 0.60),
    ("Mira Kowalski",   "mira@ashgrove-example.studio",      3, 0.55),
    ("Ada Okonkwo",     "ada@riverside-example.org",         3, 0.70),
    ("Freya Nilsen",    "freya@baseline-example.com",        2, 0.45),
]

# Automated senders: volume without conversation, which is what makes the
# "you reply to" figure and the domain breakdown interesting.
HISTORY_ROBOTS = [
    ("Forgejo",        "notifications@forge-example.dev", 18),
    ("Northwind CI",   "ci@northwind-example.com",        14),
    ("Statuspage",     "alerts@status-example.io",         6),
    ("Rheinstrom",     "service@rheinstrom-example.de",    2),
    ("Bahn",           "no-reply@travel-example.de",       3),
]

HISTORY_SUBJECTS = [
    "Q3 roadmap review", "Contract draft for Harbourline", "Onboarding checklist",
    "Design review Thursday", "Migration plan feedback", "Board pack — final",
    "Notes from the standup", "Budget sign-off", "Interview debrief",
    "Renewal terms", "Postmortem: cache outage", "Conference travel",
    "Audit log retention", "Pricing experiment results", "Supplier quote",
]

HISTORY_ROBOT_SUBJECTS = [
    "[northwind/core] Build failed on main", "New pull request opened",
    "Deployment succeeded", "Weekly usage summary",
    "Alert: latency above threshold", "Your monthly statement",
]


def _history() -> list[dict]:
    """A year of archived correspondence, shaped like a working mailbox."""
    rnd = random.Random(HISTORY_SEED)
    out: list[dict] = []

    def moment(max_days: float = 358.0) -> float:
        """days_ago for a plausible working moment — weekday, business hours."""
        for _ in range(10):
            when = NOW - timedelta(days=rnd.uniform(1.5, max_days))
            hour = (rnd.randint(9, 17) if rnd.random() < 0.72
                    else rnd.choice([7, 8, 18, 19, 20]) if rnd.random() < 0.6
                    else rnd.randint(0, 23))
            when = when.replace(hour=hour, minute=rnd.randint(0, 59))
            if when.weekday() < 5 or rnd.random() < 0.18:   # quiet weekends, not empty
                return (NOW - when).total_seconds() / 86400.0
        return (NOW - when).total_seconds() / 86400.0

    def add(account, frm, to, subject, days, folder, role, msg_id=None, refs=None):
        out.append(dict(account=account, folder=folder, role=role, days=days,
                        seen=True, frm=frm, to=to, subject=subject, msg_id=msg_id,
                        refs=refs, body="(archived correspondence)"))

    n = [0]

    def next_id() -> str:
        n[0] += 1
        return f"<history-{n[0]}@mail.example.com>"

    def lag() -> float:
        """A reply delay in days. Mostly minutes to hours, occasionally days, so
        the median and the 90th percentile are not the same number."""
        return rnd.choice([
            rnd.uniform(4, 55) / 1440,          # minutes
            rnd.uniform(1, 8) / 24,             # hours
            rnd.uniform(9, 40) / 24,            # overnight
            rnd.uniform(2, 5),                  # days
        ])

    for name, addr, per_week, reply_odds in HISTORY_PEOPLE:
        for _ in range(int(per_week * 52 * 0.5)):
            days = moment()
            subject = rnd.choice(HISTORY_SUBJECTS)
            root = next_id()
            who = f"{name} <{addr}>"
            # A third of threads are ones we open. Without them the panel has no
            # data for "they reply in" and the tile renders empty — both
            # directions need to exist for the shot to show the feature.
            we_start = rnd.random() < 0.34
            first_from, first_to = (WORK, who) if we_start else (who, WORK)
            add(WORK, first_from, first_to, subject, days,
                "Sent" if we_start else "Archive", "sent" if we_start else "archive",
                msg_id=root)
            if rnd.random() < reply_odds:
                # days_ago counts backwards, so a later message has a smaller one.
                gap = lag()
                if days - gap > 0.6:
                    add(WORK, first_to, first_from, f"Re: {subject}", days - gap,
                        "Archive" if we_start else "Sent",
                        "archive" if we_start else "sent", refs=root)

    for name, addr, per_week in HISTORY_ROBOTS:
        for _ in range(int(per_week * 52 * 0.5)):
            add(WORK, f"{name} <{addr}>", WORK, rnd.choice(HISTORY_ROBOT_SUBJECTS),
                moment(), "Archive", "archive", msg_id=next_id())

    return out


def touch_agent() -> None:
    """Stamp both accounts as just-synced.

    The UI calls an agent stopped after 120 s of silence and puts a red banner
    over the sidebar, which is not what a marketing screenshot wants. Seeding and
    shooting are minutes apart, so the shooter re-stamps immediately before it
    captures rather than relying on the seed's timestamps still being fresh.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        for account in db.query(Account).all():
            account.last_agent_seen = now
            account.last_sync_at = now
        db.commit()


def main() -> None:
    _guard()

    print("building attachments…")
    blobs = {
        "INVOICE": ("application/pdf", _pdf(
            "Rheinstrom Energie — Jahresabrechnung 2024",
            [
                "Kundennummer 84-220-119        Zeitraum 01.01.2024 - 31.12.2024",
                "",
                "Verbrauch Strom            3.412 kWh",
                "Grundpreis                   142,80 EUR",
                "Arbeitspreis               1.088,45 EUR",
                "",
                "Rechnungsbetrag            1.231,25 EUR",
                "Abschlagszahlungen        -1.140,00 EUR",
                "Nachzahlung                   91,25 EUR",
            ])),
        "PLANNING": ("application/pdf", _pdf(
            "Q3 Planning — Draft",
            [
                "Three workstreams are proposed for the quarter:",
                "",
                "1. Platform migration off the legacy scheduler",
                "2. Observability — replace the retired datasource",
                "3. Billing reconciliation (stretch)",
                "",
                "Headcount assumes two engineers per workstream, which we do",
                "not currently have. Scope is the open question, not sequencing.",
            ])),
        "CONTRACT": ("application/pdf", _pdf(
            "Services Agreement — Revision 3",
            [
                "7. Termination clause",
                "",
                "Either party may terminate this agreement on ninety (90) days",
                "written notice. Termination does not affect any obligation",
                "accrued before the effective date.",
                "",
                "8. Governing law",
                "",
                "This agreement is governed by the laws of the Netherlands.",
            ])),
        # Deliberately image-only: this is the OCR proof.
        "SCAN": ("image/png", _scan_png([
            "PRAXIS LINDQVIST",
            "",
            "Referral note",
            "",
            "Patient referred for a follow-up",
            "ultrasound appointment.",
            "",
            "Reference: RN-2024-0518",
        ])),
        "PHOTO1": ("image/jpeg", _photo("#12324f", (30, 90, 180), "#ffd36e")),
        "PHOTO2": ("image/jpeg", _photo("#3a1f4d", (120, 40, 150), "#ff9e6e")),
        "PHOTO3": ("image/jpeg", _photo("#14403a", (20, 130, 110), "#f2f0c4")),
    }

    print("wiping the test database…")
    with SessionLocal() as db:
        # Ordered by dependency; ingest recreates everything it needs.
        db.execute(text(
            "TRUNCATE messages, mailboxes, accounts, settings, outbound, "
            "pending_actions RESTART IDENTITY CASCADE"
        ))
        db.commit()

    print("ingesting messages…")
    uids: dict[tuple[str, str], int] = {}
    for spec in sorted(_messages() + _history(), key=lambda s: -s["days"]):
        atts = [
            (name, blobs[key][0], blobs[key][1])
            for name, _ctype, key in spec.get("attachments", [])
        ]
        _mid, raw = build(
            spec["frm"], spec["to"], spec["subject"], spec["body"], spec["days"],
            msg_id=spec.get("msg_id"), refs=spec.get("refs"),
            cc=spec.get("cc", ""), attachments=atts,
        )
        key = (spec["account"], spec["folder"])
        uids[key] = uids.get(key, 0) + 1
        uid = uids[key]

        with SessionLocal() as db:
            account = ingest.get_or_create_account(db, spec["account"])
            mailbox = ingest.register_folder(
                db, account, spec["folder"], spec.get("role", ""), 1, None
            )
            ingest.store_message(
                db, account, mailbox, uid,
                {"seen": spec.get("seen", True), "flagged": spec.get("flagged", False)},
                raw,
            )
            ingest.advance_cursor(db, mailbox, uid)
            db.commit()

    print("setting account presentation…")
    with SessionLocal() as db:
        personal = db.query(Account).filter_by(email=PERSONAL).one()
        personal.label = "Personal"
        personal.color = "#e0653a"
        personal.footer = ""
        personal.footer_customized = True
        personal.last_agent_seen = NOW.replace(tzinfo=None)
        personal.last_sync_at = NOW.replace(tzinfo=None)
        personal.backfill_complete = True

        work = db.query(Account).filter_by(email=WORK).one()
        work.label = "Northwind"
        work.color = "#1d6ff2"
        # Aliases, so the composer's From picker has something to show — the row
        # is hidden entirely when there is only one sendable identity.
        work.send_addresses = [
            "hannah@northwind-example.com",
            "press@northwind-example.com",
        ]
        work.footer = ""
        work.footer_customized = True
        work.last_agent_seen = NOW.replace(tzinfo=None)
        work.last_sync_at = NOW.replace(tzinfo=None)
        work.backfill_complete = True
        db.commit()

    print("extracting attachment text (Tika + OCR — this is the slow part)…")
    with SessionLocal() as db:
        for _ in range(60):
            if ingest.extract_pending(db) == 0:
                break
            db.commit()
        db.commit()

    print("rendering thumbnails…")
    with SessionLocal() as db:
        ingest.backfill_thumbs(db)
        db.commit()
        for _ in range(60):
            if ingest.thumb_pending(db) == 0:
                break
            db.commit()
        db.commit()

    with SessionLocal() as db:
        n_msg = db.query(Message).count()
        n_att = db.query(Attachment).count()
        n_txt = db.query(Attachment).filter(Attachment.extract_status == "done").count()
        n_thumb = db.query(Attachment).filter(Attachment.thumb_status == "done").count()
        ocr = db.query(Attachment).filter(
            Attachment.content_type.like("image/png%")
        ).first()
    print(f"\n  {n_msg} messages, {n_att} attachments, "
          f"{n_txt} extracted, {n_thumb} thumbnailed")
    if ocr is not None:
        got = (ocr.extracted_text or "").strip()
        print(f"  OCR sample: {got[:70]!r}" if got else "  OCR sample: EMPTY — is Tika the -full image?")


if __name__ == "__main__":
    main()
