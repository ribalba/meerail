"""A conversation, flattened into the text a language model reads.

This is the only place in meerail that turns mail into something to send outside
the building, so what it composes is the whole of what leaves: which messages,
in which order, under which headers, and what happens to a thread too long to
fit. Kept apart from the router for that reason — it is the part with decisions
in it, and none of them are about HTTP.

The prompt that wraps this text is in app/aiprompts.py; the router that calls it
is app/routers/ai.py.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from core.mail.parse import html_to_text
from core.models import Attachment, Message, Recipient, still_filed

# One message's body, before it is cut. Long enough for any mail somebody wrote
# and short enough that a 2 MB machine-generated report does not crowd the rest
# of the conversation out of the budget.
MAX_MESSAGE_CHARS = 40_000


def thread_messages(db: DBSession, msg: Message) -> list[Message]:
    """Every message of the conversation this one is in, oldest first.

    A message that was never threaded is its own conversation — the same rule the
    reader and the search results use, so "the whole thread" means here what it
    means on screen. Deleted placements are excluded for the same reason
    `_readable` guards the endpoint: mail the person emptied out of Trash is not
    mail this can hand to a third party.
    """
    if not msg.thread_id:
        return [msg]
    return list(db.execute(
        select(Message)
        .where(Message.account_id == msg.account_id, Message.thread_id == msg.thread_id,
               still_filed())
        .order_by(Message.date_sent.asc().nulls_first(), Message.id.asc())
    ).scalars().all())


def _addr(name: str, address: str) -> str:
    name = (name or "").strip()
    address = (address or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return address or name or "(unknown)"


def body_of(msg: Message) -> str:
    """The message as text. Plain text where the sender provided it, else the HTML
    flattened — the same fallback the search corpus and the task text use, so what
    the model reads matches what the person searched."""
    if msg.body_text and msg.body_text.strip():
        return msg.body_text.strip()
    text = html_to_text(msg.body_html).strip()
    if text:
        return text
    if msg.content_status != "full":
        # Outside the content window: the row is real, the body was never kept or
        # has since been pruned. Saying so beats an empty message, which the model
        # would otherwise reason about as though the sender had written nothing.
        return "[the body of this message is not stored on this server]"
    return "[no text content]"


def render(db: DBSession, msgs: list[Message], max_chars: int) -> tuple[str, dict]:
    """The conversation as one block of text, plus what had to be left out.

    Newest-first when it comes to what fits: a thread too long for the budget
    keeps its recent end, because that is the part every question is about —
    "draft a reply" is about the last message, and a summary of the recent half
    is worth more than an error. What was dropped comes back in the second return
    value and is shown in the dialog, because a summary that silently covers half
    a conversation is the one failure the person cannot see for themselves.
    """
    recipients: dict[int, list[Recipient]] = {}
    files: dict[int, list[str]] = {}
    ids = [m.id for m in msgs]
    if ids:
        for r in db.execute(
            select(Recipient).where(Recipient.message_pk.in_(ids),
                                    # Bcc is left out on purpose: it is the one
                                    # header whose point is not to be passed on,
                                    # and it adds nothing to a summary.
                                    Recipient.kind.in_(("to", "cc")))
        ).scalars().all():
            recipients.setdefault(r.message_pk, []).append(r)
        # Names only. A filename is context a thread talks about ("the report you
        # attached"); the bytes would multiply the size of every call, and neither
        # feature needs them.
        for pk, name in db.execute(
            select(Attachment.message_pk, Attachment.filename)
            .where(Attachment.message_pk.in_(ids), Attachment.is_inline.is_(False))
        ).all():
            files.setdefault(pk, []).append(name or "(unnamed)")

    total = len(msgs)
    blocks: list[str] = []
    used = 0
    dropped = 0
    shortened = 0
    for index in range(total - 1, -1, -1):       # newest first, so the tail survives
        msg = msgs[index]
        body = body_of(msg)
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + "\n[… this message continues, and was cut here]"
            shortened += 1
        head = [f"--- Message {index + 1} of {total} ---",
                f"From: {_addr(msg.from_name, msg.from_addr)}"]
        to = [_addr(r.name, r.address) for r in recipients.get(msg.id, []) if r.kind == "to"]
        cc = [_addr(r.name, r.address) for r in recipients.get(msg.id, []) if r.kind == "cc"]
        if to:
            head.append("To: " + ", ".join(to))
        if cc:
            head.append("Cc: " + ", ".join(cc))
        if msg.date_sent:
            head.append(f"Date: {msg.date_sent.strftime('%Y-%m-%d %H:%M')} UTC")
        head.append(f"Subject: {msg.subject or '(no subject)'}")
        if files.get(msg.id):
            head.append("Attachments: " + ", ".join(files[msg.id]))
        block = "\n".join(head) + "\n\n" + body

        # `and blocks` so the newest message is always included, even alone and
        # even over budget: a dialog that answers "nothing fitted" to a question
        # about a conversation the person is looking at is no answer at all.
        if used + len(block) > max_chars and blocks:
            dropped = index + 1
            break
        blocks.append(block)
        used += len(block)

    blocks.reverse()
    text = "\n\n".join(blocks)
    if dropped:
        text = (f"[{dropped} earlier message{'s' if dropped != 1 else ''} in this "
                f"conversation would not fit and {'are' if dropped != 1 else 'is'} "
                f"not shown]\n\n") + text
    return text, {"messages": total, "included": len(blocks), "dropped": dropped,
                  "shortened": shortened, "chars": len(text)}
