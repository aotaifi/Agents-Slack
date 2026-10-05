"""Email a human right after they are @mentioned.

Sending happens strictly after the posting transaction commits. Each send claims the
actor's pending notifications with one atomic UPDATE, so retries, parallel workers and
the CLI sweep can never send the same notification twice. Message text and recipient
addresses are never written to the log.
"""

import logging
import os
import threading
import unicodedata
from datetime import timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from sqlalchemy import select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import aliased

from .mail import MailUnavailable, email_enabled, submit_message, workspace_access
from .models import (
    Actor,
    ActorEmailLog,
    Membership,
    Message,
    Notification,
    Project,
    Thread,
    now,
)

logger = logging.getLogger("agent_commons.mention_email")

MAX_AGE = timedelta(hours=24)
SNIPPET_CHARS = 200
MAX_LISTED = 10
MAX_ROUNDS = 3
DEFAULT_DAILY_CAP = 30


def daily_cap():
    """Mention emails one person can receive per UTC day (env PILOT_MENTION_EMAIL_DAILY_CAP)."""
    try:
        return max(0, int(os.environ.get("PILOT_MENTION_EMAIL_DAILY_CAP", DEFAULT_DAILY_CAP)))
    except ValueError:
        return DEFAULT_DAILY_CAP


def clean(value, limit=None):
    """One printable line: whitespace collapsed, control and bidi characters removed."""
    flat = " ".join(str(value).split())
    flat = "".join(ch for ch in flat if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    if limit is not None and len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat


def snippet(value):
    flat = clean(value)
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"


def mention_message(recipient, items, intro, link):
    """Build the plain-text email for `items`, a list of dicts (see _claim)."""
    first = items[0]
    if len(items) == 1:
        subject = f"@{clean(first['author'], 60)} mentioned you in {clean(first['project'], 60)}"
    else:
        subject = f"{len(items)} new mentions in Research Workspace"
    blocks = []
    for item in items[:MAX_LISTED]:
        who = clean(item["author"], 80) + (" (agent)" if item["author_kind"] == "agent" else "")
        where = f"{clean(item['project'], 80)} / {clean(item['thread'], 120)}"
        blocks.append(f"{who} in {where}:\n  {snippet(item['text'])}")
    if len(items) > MAX_LISTED:
        blocks.append(f"...and {len(items) - MAX_LISTED} more in the workspace.")
    message = EmailMessage()
    message["From"] = os.environ["PILOT_EMAIL_FROM"]
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(now())
    message["Message-ID"] = make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(
        "\n\n".join(blocks) + f"\n\n{intro}{link}\n\n--\n"
        "You get these because you turned on mention emails. "
        "Turn them off in My account.\n"
    )
    return message


def _dialect_insert(db):
    return postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert


def _begin(db):
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _claim(factory, actor_id):
    """Atomically claim this actor's pending notifications and today's cap slot."""
    with factory() as db:
        _begin(db)
        try:
            actor = db.scalar(
                select(Actor).where(Actor.id == actor_id).with_for_update(key_share=True)
            )
            if (
                actor is None
                or actor.kind != "human"
                or not actor.email
                or actor.email_verified_at is None
                or not actor.mention_emails
            ):
                return None
            current = now()
            day = current.astimezone(timezone.utc).date()
            db.execute(
                _dialect_insert(db)(ActorEmailLog)
                .values(actor_id=actor_id, day=day, sent=0)
                .on_conflict_do_nothing()
            )
            slot = db.execute(
                update(ActorEmailLog)
                .where(
                    ActorEmailLog.actor_id == actor_id,
                    ActorEmailLog.day == day,
                    ActorEmailLog.sent < daily_cap(),
                )
                .values(sent=ActorEmailLog.sent + 1)
            )
            if slot.rowcount == 0:
                return None
            visible = select(Membership.project_id).where(Membership.actor_id == actor_id)
            ids = [
                row[0]
                for row in db.execute(
                    update(Notification)
                    .where(
                        Notification.actor_id == actor_id,
                        Notification.emailed_at.is_(None),
                        Notification.read_at.is_(None),
                        Notification.created_at >= current - MAX_AGE,
                        Notification.project_id.in_(visible),
                    )
                    .values(emailed_at=current)
                    .returning(Notification.id)
                    .execution_options(synchronize_session=False)
                )
            ]
            if not ids:
                return None
            author = aliased(Actor)
            items = [
                {
                    "author": row.name,
                    "author_kind": row.kind,
                    "project": row.project,
                    "thread": row.thread,
                    "text": row.text,
                }
                for row in db.execute(
                    select(
                        author.name,
                        author.kind,
                        Project.name.label("project"),
                        Thread.title.label("thread"),
                        Message.text,
                    )
                    .select_from(Notification)
                    .join(Message, Message.id == Notification.message_id)
                    .join(Thread, Thread.id == Message.thread_id)
                    .join(Project, Project.id == Notification.project_id)
                    .join(author, author.id == Message.author_id)
                    .where(Notification.id.in_(ids))
                    .order_by(Notification.id)
                )
            ]
            address = actor.email
            db.commit()
            return address, items, ids, day
        finally:
            db.rollback()


def _release(factory, actor_id, ids, day):
    """Undo a claim after a failed submission so the sweep can retry."""
    with factory() as db:
        _begin(db)
        db.execute(
            update(Notification)
            .where(Notification.id.in_(ids))
            .values(emailed_at=None)
            .execution_options(synchronize_session=False)
        )
        db.execute(
            update(ActorEmailLog)
            .where(
                ActorEmailLog.actor_id == actor_id,
                ActorEmailLog.day == day,
                ActorEmailLog.sent > 0,
            )
            .values(sent=ActorEmailLog.sent - 1)
        )
        db.commit()


def send_for_actor(factory, actor_id):
    """Send one bundled email. Returns "sent", "idle" (nothing to do) or "failed"."""
    if not email_enabled():
        return "idle"
    try:
        intro, link, _ = workspace_access("the workspace")
        claimed = _claim(factory, actor_id)
        if claimed is None:
            return "idle"
    except (MailUnavailable, ValueError, OSError) as error:
        logger.warning("mention email not attempted (%s)", type(error).__name__)
        return "idle"
    except Exception as error:  # a database hiccup must not break the finished request
        logger.warning("mention email claim failed (%s)", type(error).__name__)
        return "failed"
    address, items, ids, day = claimed
    try:
        submit_message(mention_message(address, items, intro, link))
    except Exception as error:
        logger.warning("mention email submission failed (%s)", type(error).__name__)
        try:
            _release(factory, actor_id, ids, day)
        except Exception as release_error:
            logger.error("mention email claim not released (%s)", type(release_error).__name__)
        return "failed"
    logger.info("mention email submitted: %d mention(s)", len(ids))
    return "sent"


_running = {}
_running_lock = threading.Lock()


def send_mention_emails(factory, actor_ids):
    """After-commit entry point. While one send for an actor is in flight, later calls only
    mark it dirty; the running sender then bundles everything that arrived meanwhile into
    one more email, so a burst of mentions becomes at most a couple of messages."""
    for actor_id in dict.fromkeys(actor_ids):
        with _running_lock:
            if actor_id in _running:
                _running[actor_id] = True
                continue
            _running[actor_id] = False
        rounds = 0
        while True:
            outcome = send_for_actor(factory, actor_id)
            rounds += 1
            with _running_lock:
                if outcome != "failed" and _running[actor_id] and rounds < MAX_ROUNDS:
                    _running[actor_id] = False
                    continue
                del _running[actor_id]
                break


def sweep(factory):
    """Retry pending mention emails (CLI). Returns the number of emails submitted."""
    with factory() as db:
        actor_ids = list(
            db.scalars(
                select(Notification.actor_id)
                .join(Actor, Actor.id == Notification.actor_id)
                .where(
                    Notification.emailed_at.is_(None),
                    Notification.read_at.is_(None),
                    Notification.created_at >= now() - MAX_AGE,
                    Actor.email_verified_at.is_not(None),
                    Actor.mention_emails.is_(True),
                )
                .distinct()
                .order_by(Notification.actor_id)
            )
        )
    sent = 0
    for actor_id in actor_ids:
        with _running_lock:
            if actor_id in _running:
                continue
        sent += send_for_actor(factory, actor_id) == "sent"
    return sent
