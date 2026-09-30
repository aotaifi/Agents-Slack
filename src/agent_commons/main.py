import hashlib
import json
import os
from datetime import timezone
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, object_session

from .db import make_engine, session_factory
from .identity import choose_handle
from .models import (
    Actor,
    Channel,
    Event,
    Idempotency,
    Membership,
    Message,
    Project,
    RateWindow,
    Reaction,
    Thread,
    Token,
    now,
)
from .schemas import (
    ActorInput,
    MemberInput,
    MessageInput,
    MuteInput,
    ProjectInput,
    ReactionInput,
    RulesInput,
    ThreadInput,
)
from .security import digest, issue_token


def actor_json(a):
    db = object_session(a)
    owner = db.get(Actor, a.owner_id) if db and a.owner_id else None
    return {
        **{k: getattr(a, k) for k in ("id", "name", "handle", "kind", "owner_id", "is_admin")},
        "owner": {k: getattr(owner, k) for k in ("id", "name", "handle")} if owner else None,
    }


def reaction_actor(a):
    return {k: getattr(a, k) for k in ("id", "name", "handle", "kind")}


def reactions_json(db, message_id):
    groups = {}
    for reaction, actor in db.execute(
        select(Reaction, Actor)
        .join(Actor, Actor.id == Reaction.actor_id)
        .where(Reaction.message_id == message_id)
        .order_by(Reaction.emoji, Actor.handle, Actor.id)
    ):
        groups.setdefault(reaction.emoji, []).append(reaction_actor(actor))
    return [
        {"emoji": emoji, "actors": actors, "count": len(actors)} for emoji, actors in groups.items()
    ]


def project_json(p):
    return {k: getattr(p, k) for k in ("id", "name", "description")}


def channel_json(c):
    return {k: getattr(c, k) for k in ("id", "project_id", "name", "description")}


def utc(value):
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def timestamp(value):
    return utc(value).isoformat()


def thread_json(t):
    return {
        **{k: getattr(t, k) for k in ("id", "channel_id", "project_id", "title")},
        "created_at": timestamp(t.created_at),
    }


def member_json(db, m):
    return {"actor": actor_json(db.get(Actor, m.actor_id)), "role": m.role, "muted": m.muted}


def message_json(db, m):
    return {
        **{
            k: getattr(m, k)
            for k in ("id", "thread_id", "project_id", "text", "mentions", "sequence")
        },
        "author": actor_json(db.get(Actor, m.author_id)),
        "metadata": m.data,
        "reply_to": m.reply_to,
        "reactions": reactions_json(db, m.id),
        "reply_count": db.scalar(
            select(func.count()).select_from(Message).where(Message.reply_to == m.id)
        ),
        "created_at": timestamp(m.created_at),
    }


def event_json(e):
    return {
        **{k: getattr(e, k) for k in ("id", "project_id", "type", "thread_id", "payload")},
        "created_at": timestamp(e.created_at),
    }


def create_app(database_url: str | None = None):
    app = FastAPI(title="Research Workspace", version="0.1.0")
    engine = make_engine(database_url)
    factory = session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.posting_limit = int(os.environ.get("POSTING_RATE_LIMIT", "60"))

    def session(request: Request):
        with factory() as db:
            if engine.dialect.name == "sqlite" and request.method in {
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            }:
                db.execute(text("BEGIN IMMEDIATE"))
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def authenticated(
        authorization: str | None = Header(default=None),
        db: Session = Depends(session, scope="function"),
    ):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        token = db.get(Token, digest(authorization[7:]))
        if token is None or token.revoked:
            raise HTTPException(401, "Invalid or revoked token")
        return db.get(Actor, token.actor_id)

    def human(a):
        if a.kind != "human":
            raise HTTPException(403, "Human action required")

    def project_access(db, a, project_id, write=False, owner=False):
        # The project lock is held until the entire request transaction commits.
        # Under PostgreSQL READ COMMITTED, later writers see committed cursor/key state.
        query = select(Project).where(Project.id == project_id)
        if write or owner:
            query = query.with_for_update()
        p = db.scalar(query.execution_options(populate_existing=True))
        m = db.get(Membership, (project_id, a.id))
        if p is None or m is None:
            raise HTTPException(404, "Project not found")
        if owner:
            human(a)
            if m.role != "owner":
                raise HTTPException(403, "Project owner required")
        if write and m.muted:
            raise HTTPException(403, "Participant is muted")
        return p

    def resource(db, a, model, resource_id, write=False):
        item = db.get(model, resource_id)
        if item is None:
            raise HTTPException(404, "Resource not found")
        p = project_access(db, a, item.project_id, write=write)
        return item, p

    def emit(db, p, kind, payload, thread_id=None):
        p.cursor += 1
        e = Event(project_id=p.id, id=p.cursor, type=kind, payload=payload, thread_id=thread_id)
        db.add(e)
        db.flush()
        return e

    def enforce_rate(db, p, a):
        current = now()
        window = db.get(RateWindow, (p.id, a.id))
        if window is None:
            window = RateWindow(project_id=p.id, actor_id=a.id, started_at=current, count=0)
            db.add(window)
        elapsed = (current - utc(window.started_at)).total_seconds()
        if elapsed >= 60:
            window.started_at, window.count = current, 0
        elif window.count >= app.state.posting_limit:
            raise HTTPException(
                429,
                "Posting rate limit exceeded",
                headers={"Retry-After": str(max(1, int(60 - elapsed) + 1))},
            )
        window.count += 1

    @app.get("/health")
    def health(db: Session = Depends(session, scope="function")):
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(503, "Database unavailable") from None
        return {"status": "ok"}

    @app.get("/v1/me")
    def me(a=Depends(authenticated, scope="function")):
        return actor_json(a)

    @app.get("/v1/actors")
    def actors(
        a=Depends(authenticated, scope="function"), db: Session = Depends(session, scope="function")
    ):
        human(a)
        query = select(Actor).order_by(Actor.name, Actor.id)
        if not a.is_admin:
            query = query.where((Actor.id == a.id) | (Actor.owner_id == a.id))
        return {"items": [actor_json(x) for x in db.scalars(query)]}

    @app.post("/v1/actors", status_code=201)
    def create_actor(
        body: ActorInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        if body.kind == "human" and not a.is_admin:
            raise HTTPException(403, "Administrator required")
        if engine.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(731958240)"))
        try:
            handle = choose_handle(db, body.name, a if body.kind == "agent" else None, body.handle)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except FileExistsError:
            raise HTTPException(409, "Handle already exists") from None
        actor = Actor(
            name=body.name,
            handle=handle,
            kind=body.kind,
            owner_id=a.id if body.kind == "agent" else None,
        )
        db.add(actor)
        db.flush()
        return {"actor": actor_json(actor), "token": issue_token(db, actor)}

    @app.post("/v1/actors/{actor_id}/revoke", status_code=204)
    def revoke(
        actor_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        target = db.get(Actor, actor_id)
        if target is None or not (a.is_admin or target.owner_id == a.id or target.id == a.id):
            raise HTTPException(404, "Actor not found")
        db.execute(update(Token).where(Token.actor_id == actor_id).values(revoked=True))
        return Response(status_code=204)

    @app.get("/v1/projects")
    def projects(
        a=Depends(authenticated, scope="function"), db: Session = Depends(session, scope="function")
    ):
        items = db.scalars(
            select(Project)
            .join(Membership)
            .where(Membership.actor_id == a.id)
            .order_by(Project.name, Project.id)
        )
        return {"items": [project_json(p) for p in items]}

    @app.post("/v1/projects", status_code=201)
    def create_project(
        body: ProjectInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        p = Project(**body.model_dump())
        db.add(p)
        db.flush()
        m = Membership(project_id=p.id, actor_id=a.id, role="owner")
        db.add(m)
        db.flush()
        emit(db, p, "membership.updated", member_json(db, m))
        return project_json(p)

    @app.get("/v1/projects/{project_id}")
    def get_project(
        project_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        return project_json(project_access(db, a, project_id))

    @app.get("/v1/projects/{project_id}/members")
    def members(
        project_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        project_access(db, a, project_id)
        return {
            "items": [
                member_json(db, m)
                for m in db.scalars(
                    select(Membership)
                    .where(Membership.project_id == project_id)
                    .order_by(Membership.actor_id)
                )
            ]
        }

    @app.post("/v1/projects/{project_id}/members", status_code=201)
    def add_member(
        project_id: str,
        body: MemberInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, owner=True)
        target = db.get(Actor, body.actor_id)
        if target is None:
            raise HTTPException(404, "Actor not found")
        if body.role == "owner" and target.kind == "agent":
            raise HTTPException(403, "Agents cannot own projects")
        if db.get(Membership, (project_id, target.id)):
            raise HTTPException(409, "Membership already exists")
        m = Membership(project_id=project_id, actor_id=target.id, role=body.role)
        db.add(m)
        db.flush()
        result = member_json(db, m)
        emit(db, p, "membership.updated", result)
        return result

    @app.patch("/v1/projects/{project_id}/members/{actor_id}")
    def mute(
        project_id: str,
        actor_id: str,
        body: MuteInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, owner=True)
        m = db.get(Membership, (project_id, actor_id))
        if m is None:
            raise HTTPException(404, "Membership not found")
        if db.get(Actor, actor_id).kind != "agent":
            raise HTTPException(403, "Only agents may be muted")
        m.muted = body.muted
        result = member_json(db, m)
        emit(db, p, "membership.updated", result)
        return result

    @app.delete("/v1/projects/{project_id}/members/{actor_id}", status_code=204)
    def remove_member(
        project_id: str,
        actor_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, owner=True)
        m = db.get(Membership, (project_id, actor_id))
        if m is None:
            raise HTTPException(404, "Membership not found")
        owners = db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.project_id == project_id, Membership.role == "owner")
        )
        if m.role == "owner" and owners <= 1:
            raise HTTPException(409, "At least one owner must remain")
        db.delete(m)
        emit(db, p, "membership.removed", {"actor_id": actor_id})
        return Response(status_code=204)

    @app.get("/v1/projects/{project_id}/rules")
    def rules(
        project_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id)
        return {"text": p.rules, "version": p.rules_version}

    @app.put("/v1/projects/{project_id}/rules")
    def set_rules(
        project_id: str,
        body: RulesInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, owner=True)
        p.rules = body.text
        p.rules_version += 1
        result = {"text": p.rules, "version": p.rules_version}
        emit(db, p, "rules.updated", result)
        return result

    @app.get("/v1/projects/{project_id}/channels")
    def channels(
        project_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        project_access(db, a, project_id)
        return {
            "items": [
                channel_json(c)
                for c in db.scalars(
                    select(Channel)
                    .where(Channel.project_id == project_id)
                    .order_by(Channel.name, Channel.id)
                )
            ]
        }

    @app.post("/v1/projects/{project_id}/channels", status_code=201)
    def create_channel(
        project_id: str,
        body: ProjectInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, write=True)
        c = Channel(project_id=project_id, **body.model_dump())
        db.add(c)
        db.flush()
        result = channel_json(c)
        emit(db, p, "channel.created", result)
        return result

    @app.get("/v1/channels/{channel_id}/threads")
    def threads(
        channel_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        resource(db, a, Channel, channel_id)
        return {
            "items": [
                thread_json(t)
                for t in db.scalars(
                    select(Thread)
                    .where(Thread.channel_id == channel_id)
                    .order_by(Thread.created_at.desc(), Thread.id)
                )
            ]
        }

    @app.post("/v1/channels/{channel_id}/threads", status_code=201)
    def create_thread(
        channel_id: str,
        body: ThreadInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        c, p = resource(db, a, Channel, channel_id, write=True)
        t = Thread(channel_id=c.id, project_id=p.id, title=body.title)
        db.add(t)
        db.flush()
        result = thread_json(t)
        emit(db, p, "thread.created", result, t.id)
        return result

    @app.get("/v1/threads/{thread_id}")
    def get_thread(
        thread_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        t, _ = resource(db, a, Thread, thread_id)
        return thread_json(t)

    @app.post("/v1/threads/{thread_id}/messages", status_code=201)
    def post_message(
        thread_id: str,
        body: MessageInput,
        response: Response,
        idempotency_key: str | None = Header(default=None),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        t, p = resource(db, a, Thread, thread_id, write=True)
        explicit = str(body.reply_to) if body.reply_to else None
        fingerprint_body = body.model_dump(mode="json")
        # Keep pre-migration retry keys valid for requests using the legacy shape.
        if "reply_to" not in body.model_fields_set or explicit is None:
            fingerprint_body.pop("reply_to")
        else:
            fingerprint_body["reply_to"] = explicit
        fingerprint = hashlib.sha256(
            json.dumps(
                {"thread_id": thread_id, **fingerprint_body},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if idempotency_key is not None:
            if not idempotency_key.strip() or len(idempotency_key) > 200:
                raise HTTPException(422, "Idempotency-Key must contain 1 to 200 characters")
            previous = db.get(Idempotency, (a.id, p.id, idempotency_key))
            if previous:
                if previous.fingerprint != fingerprint:
                    raise HTTPException(409, "Idempotency-Key was used for another request")
                response.status_code = 200
                return message_json(db, db.get(Message, previous.message_id))
        legacy = body.metadata.get("reply_to")
        if legacy is not None:
            try:
                legacy = str(UUID(str(legacy)))
            except ValueError:
                raise HTTPException(422, "Invalid metadata.reply_to") from None
        if "reply_to" in body.model_fields_set and legacy and explicit != legacy:
            raise HTTPException(422, "Conflicting reply_to fields")
        reply_to = explicit if "reply_to" in body.model_fields_set else legacy
        if reply_to:
            parent = db.get(Message, reply_to)
            if parent is None or parent.thread_id != t.id or parent.reply_to is not None:
                raise HTTPException(422, "Reply must reference a top-level message in this thread")
        for actor_id in body.mentions:
            if not db.get(Membership, (p.id, actor_id)):
                raise HTTPException(422, "Mentioned actor is not a project member")
        enforce_rate(db, p, a)
        m = Message(
            thread_id=t.id,
            project_id=p.id,
            author_id=a.id,
            text=body.text,
            reply_to=reply_to,
            mentions=body.mentions,
            data=body.metadata,
            sequence=p.cursor + 1,
        )
        db.add(m)
        db.flush()
        result = message_json(db, m)
        emit(db, p, "message.created", result, t.id)
        if idempotency_key is not None:
            db.add(
                Idempotency(
                    actor_id=a.id,
                    project_id=p.id,
                    key=idempotency_key,
                    fingerprint=fingerprint,
                    message_id=m.id,
                )
            )
        return result

    def change_reaction(message_id, body, a, db, adding):
        m, p = resource(db, a, Message, message_id, write=True)
        key = (m.id, a.id, body.emoji)
        existing = db.get(Reaction, key)
        if (adding and existing is None) or (not adding and existing is not None):
            enforce_rate(db, p, a)
            if adding:
                db.add(Reaction(message_id=m.id, actor_id=a.id, emoji=body.emoji))
            else:
                db.delete(existing)
            db.flush()
            emit(
                db,
                p,
                "reaction.added" if adding else "reaction.removed",
                {
                    "message_id": m.id,
                    "emoji": body.emoji,
                    "actor": reaction_actor(a),
                    "reactions": reactions_json(db, m.id),
                },
                m.thread_id,
            )
        return message_json(db, m)

    @app.put("/v1/messages/{message_id}/reactions")
    def add_reaction(
        message_id: str,
        body: ReactionInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        return change_reaction(message_id, body, a, db, True)

    @app.delete("/v1/messages/{message_id}/reactions")
    def remove_reaction(
        message_id: str,
        body: ReactionInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        return change_reaction(message_id, body, a, db, False)

    @app.get("/v1/threads/{thread_id}/context")
    def context(
        thread_id: str,
        limit: int = Query(20, ge=1, le=100),
        trigger_message_id: UUID | None = None,
        max_chars: int = Query(12000, ge=1000, le=50000),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        t, p = resource(db, a, Thread, thread_id)
        snapshot = p.cursor
        rows = list(
            db.scalars(
                select(Message)
                .where(Message.thread_id == t.id, Message.sequence <= snapshot)
                .order_by(Message.sequence.desc())
                .limit(limit + 1)
            )
        )
        trigger = db.get(Message, str(trigger_message_id)) if trigger_message_id else None
        if trigger_message_id and (
            trigger is None or trigger.thread_id != t.id or trigger.sequence > snapshot
        ):
            raise HTTPException(404, "Trigger message not found")
        parent = db.get(Message, trigger.reply_to) if trigger and trigger.reply_to else None
        remaining = max_chars

        def clipped(value, allowance):
            nonlocal remaining
            original = value["text"]
            take = min(remaining, allowance, len(original))
            value["text"] = original[:take]
            remaining -= take
            if take < len(original):
                value["truncated"] = True
            return value

        rules = clipped({"text": p.rules, "version": p.rules_version}, max_chars // 4)
        trigger_data = clipped(message_json(db, trigger), max_chars // 4) if trigger else None
        parent_data = clipped(message_json(db, parent), max_chars // 4) if parent else None
        # Distribute remaining text over recent messages, giving newer messages first claim.
        recent = []
        selected = rows[:limit]
        for index, message in enumerate(selected):
            allowance = remaining // (len(selected) - index)
            recent.append(clipped(message_json(db, message), allowance))
        recent.reverse()
        return {
            "thread": thread_json(t),
            "rules": rules,
            "messages": recent,
            "cursor": snapshot,
            "has_older": len(rows) > limit,
            "trigger_message": trigger_data,
            "parent_message": parent_data,
        }

    @app.get("/v1/projects/{project_id}/inbox")
    def inbox(
        project_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        followed_thread_ids: list[UUID] = Query(default=[]),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id)
        if len(followed_thread_ids) > 100:
            raise HTTPException(422, "At most 100 followed threads are allowed")
        followed = {str(value) for value in followed_thread_ids}
        for thread_id in followed:
            thread = db.get(Thread, thread_id)
            if thread is None or thread.project_id != p.id:
                raise HTTPException(404, "Followed thread not found")
        snapshot = p.cursor
        rows = list(
            db.scalars(
                select(Event)
                .where(Event.project_id == p.id, Event.id > after, Event.id <= snapshot)
                .order_by(Event.id)
                .limit(1000)
            )
        )
        items, scanned = [], after
        for event in rows:
            scanned = event.id
            payload = event.payload
            if (
                event.type == "message.created"
                and payload.get("author", {}).get("id") != a.id
                and (a.id in payload.get("mentions", []) or event.thread_id in followed)
            ):
                items.append(event_json(event))
                if len(items) == limit:
                    break
        return {
            "items": items,
            "next_cursor": scanned if scanned < snapshot else None,
            "cursor": snapshot,
        }

    @app.get("/v1/threads/{thread_id}/messages")
    def messages(
        thread_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        t, p = resource(db, a, Thread, thread_id)
        snapshot = p.cursor
        rows = list(
            db.scalars(
                select(Message)
                .where(
                    Message.thread_id == t.id,
                    Message.sequence > after,
                    Message.sequence <= snapshot,
                )
                .order_by(Message.sequence)
                .limit(limit + 1)
            )
        )
        return {
            "items": [message_json(db, m) for m in rows[:limit]],
            "next_cursor": rows[limit - 1].sequence if len(rows) > limit else None,
            "cursor": snapshot,
        }

    @app.get("/v1/projects/{project_id}/events")
    def events(
        project_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id)
        snapshot = p.cursor
        rows = list(
            db.scalars(
                select(Event)
                .where(Event.project_id == p.id, Event.id > after, Event.id <= snapshot)
                .order_by(Event.id)
                .limit(limit + 1)
            )
        )
        return {
            "items": [event_json(e) for e in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
            "cursor": snapshot,
        }

    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(static / "index.html")

    return app


app = create_app()
