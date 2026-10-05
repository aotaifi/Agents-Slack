import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import to_tsvector, websearch_to_tsquery
from sqlalchemy.orm import Session, object_session

from .db import make_engine, session_factory
from .identity import choose_handle
from .invitation_email import MailUnavailable, email_enabled, invitation_message, submit_invitation
from .mail import submit_message
from .mention_email import send_mention_emails
from .models import (
    Actor,
    AgentConnection,
    AuthAttempt,
    BrowserSession,
    Channel,
    EmailVerification,
    Event,
    Idempotency,
    Invitation,
    Membership,
    Message,
    Notification,
    Project,
    RateWindow,
    Reaction,
    Thread,
    Token,
    now,
)
from .schemas import (
    ActorInput,
    AgentConnectionInput,
    AgentSessionInput,
    InvitationAccept,
    InvitationCode,
    InvitationEmail,
    InvitationInput,
    LoginInput,
    MemberInput,
    MemberRoleInput,
    MessageInput,
    MuteInput,
    MyEmailCode,
    MyEmailInput,
    Named,
    NotificationReadInput,
    NotificationsReadAllInput,
    PasswordInput,
    ProfileInput,
    ProjectInput,
    ReactionInput,
    RulesInput,
    ThreadInput,
)
from .security import credential_lock_key, digest, hash_password, issue_token, verify_password


def actor_json(a):
    db = object_session(a)
    owner = db.get(Actor, a.owner_id) if db and a.owner_id else None
    return {
        **{k: getattr(a, k) for k in ("id", "name", "handle", "kind", "owner_id", "is_admin")},
        "owner": {k: getattr(owner, k) for k in ("id", "name", "handle")} if owner else None,
    }


def self_actor_json(a):
    result = {**actor_json(a), "has_password": a.password_hash is not None}
    if a.kind == "human":
        # Only the verified address is ever shown, and only to its owner.
        result.update(
            email=a.email if a.email_verified_at is not None else None,
            email_verified=a.email_verified_at is not None,
            mention_emails=bool(a.mention_emails),
        )
    return result


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


def messages_json(db, messages):
    """Serialize messages with a constant number of queries."""
    messages = list(messages)
    if not messages:
        return []
    ids = [m.id for m in messages]
    actors = {}
    author_ids = {m.author_id for m in messages}
    if author_ids:
        actors.update({a.id: a for a in db.scalars(select(Actor).where(Actor.id.in_(author_ids)))})
    owner_ids = {a.owner_id for a in actors.values() if a.owner_id} - actors.keys()
    if owner_ids:
        actors.update({a.id: a for a in db.scalars(select(Actor).where(Actor.id.in_(owner_ids)))})
    groups = {}
    for reaction, actor in db.execute(
        select(Reaction, Actor)
        .join(Actor, Actor.id == Reaction.actor_id)
        .where(Reaction.message_id.in_(ids))
        .order_by(Reaction.emoji, Actor.handle, Actor.id)
    ):
        groups.setdefault(reaction.message_id, {}).setdefault(reaction.emoji, []).append(
            reaction_actor(actor)
        )
    reply_counts = dict(
        db.execute(
            select(Message.reply_to, func.count())
            .where(Message.reply_to.in_(ids))
            .group_by(Message.reply_to)
        ).all()
    )
    result = []
    for m in messages:
        author = actors[m.author_id]
        owner = actors.get(author.owner_id) if author.owner_id else None
        result.append(
            {
                **{
                    k: getattr(m, k)
                    for k in ("id", "thread_id", "project_id", "text", "mentions", "sequence")
                },
                "author": {
                    **{
                        k: getattr(author, k)
                        for k in ("id", "name", "handle", "kind", "owner_id", "is_admin")
                    },
                    "owner": {k: getattr(owner, k) for k in ("id", "name", "handle")}
                    if owner
                    else None,
                },
                "metadata": m.data,
                "reply_to": m.reply_to,
                "reactions": [
                    {"emoji": emoji, "actors": people, "count": len(people)}
                    for emoji, people in groups.get(m.id, {}).items()
                ],
                "reply_count": reply_counts.get(m.id, 0),
                "created_at": timestamp(m.created_at),
            }
        )
    return result


SNIPPET_WIDTH = 200
# PostgreSQL search first looks at this many of the newest sequence numbers (see search_messages).
SEARCH_RECENT_WINDOW = 2000


def search_snippet(body, terms):
    """Plain-text excerpt of about SNIPPET_WIDTH characters around the first match."""
    flat = " ".join(body.split())
    lowered = flat.lower()
    found = [lowered.find(t) for t in terms if t] if len(lowered) == len(flat) else []
    hits = [i for i in found if i >= 0]
    at = min(hits) if hits else 0
    start = max(0, min(at - SNIPPET_WIDTH // 4, len(flat) - SNIPPET_WIDTH))
    end = min(len(flat), start + SNIPPET_WIDTH)
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def search_clause(dialect, q):
    """Message filter and snippet terms for a search string."""
    if dialect == "postgresql":
        vector = to_tsvector(literal_column("'simple'"), Message.text)
        match = vector.bool_op("@@")(websearch_to_tsquery(literal_column("'simple'"), q))
        return match, re.findall(r"\w+", q.lower())
    terms = q.lower().split()
    return and_(*(func.ulower(Message.text).contains(t, autoescape=True) for t in terms)), terms


def message_json(db, m):
    return messages_json(db, [m])[0]


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
    app.state.auth_account_limit = 5
    app.state.auth_ip_limit = 100

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

    def same_origin(request, required=True):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if (origin is None and required) or (origin is not None and origin != expected):
            raise HTTPException(403, "Same-origin request required")

    def resolve_identity(request, authorization, db):
        # Explicit bearer credentials never silently fall back to another identity.
        connection = None
        if authorization:
            if not authorization.startswith("Bearer ") or len(authorization) > 512:
                raise HTTPException(401, "Invalid or revoked token")
            token = db.get(Token, digest(authorization[7:]), populate_existing=True)
            if token is None or token.revoked:
                raise HTTPException(401, "Invalid or revoked token")
            actor = db.get(Actor, token.actor_id)
            if token.connection_id:
                connection = db.get(AgentConnection, token.connection_id, populate_existing=True)
                if (
                    connection is None
                    or connection.revoked
                    or actor is None
                    or actor.kind != "agent"
                    or connection.actor_id != actor.id
                ):
                    raise HTTPException(401, "Invalid or revoked connection")
        else:
            raw = request.cookies.get("workspace_session")
            if not raw or len(raw) > 128:
                raise HTTPException(401, "Sign in required")
            credential = db.get(BrowserSession, digest(raw), populate_existing=True)
            if credential is None or credential.revoked or utc(credential.expires_at) <= now():
                raise HTTPException(401, "Session expired or revoked")
            actor = db.get(Actor, credential.actor_id)
            if actor is None or actor.kind != "human":
                raise HTTPException(401, "Invalid session")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                same_origin(request)
        # These attributes exist only on this request's ORM instance, never JSON.
        actor._workspace_connection = connection
        actor._workspace_session_id = request.headers.get("x-workspace-session")
        actor._workspace_mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        request.state.actor_handle = actor.handle
        return actor

    def authenticated(
        request: Request,
        authorization: str | None = Header(default=None),
        db: Session = Depends(session, scope="function"),
    ):
        return resolve_identity(request, authorization, db)

    def consume_auth_rate(db, request, handle):
        # Persist attempts before a failure is raised; the request rollback must not
        # erase brute-force counters. This short lock is released before scrypt.
        if engine.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(731958241)"))
        current = now()
        ip = request.client.host if request.client else "unknown"
        windows = []
        for label, value, limit in (
            ("account", handle.lower(), app.state.auth_account_limit),
            ("ip", ip, app.state.auth_ip_limit),
        ):
            key = digest(label + ":" + value)
            window = db.get(AuthAttempt, key)
            if window is None:
                window = AuthAttempt(key=key, started_at=current, count=0)
                db.add(window)
            elapsed = (current - utc(window.started_at)).total_seconds()
            if elapsed >= 60:
                window.started_at, window.count = current, 0
            if window.count >= limit:
                db.commit()
                raise HTTPException(
                    429,
                    "Authentication rate limit exceeded",
                    headers={"Retry-After": str(max(1, int(60 - elapsed) + 1))},
                )
            windows.append(window)
        for window in windows:
            window.count += 1
        db.commit()

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic includes rejected input by default, including password strings.
        # Keep the useful location/message while dropping values and contexts.
        if request.url.path in {"/v1/auth/login", "/v1/auth/password"}:
            try:
                same_origin(
                    request,
                    required=(
                        request.url.path == "/v1/auth/login"
                        or not request.headers.get("authorization")
                    ),
                )
            except HTTPException as forbidden:
                return JSONResponse({"detail": forbidden.detail}, status_code=forbidden.status_code)
            body = error.body if isinstance(error.body, dict) else {}
            handle = body.get("handle") or getattr(request.state, "actor_handle", "invalid")
            handle = handle[:130] if isinstance(handle, str) else "invalid"
            with factory() as db:
                if engine.dialect.name == "sqlite":
                    db.execute(text("BEGIN IMMEDIATE"))
                try:
                    consume_auth_rate(db, request, handle)
                except HTTPException as limited:
                    return JSONResponse(
                        {"detail": limited.detail},
                        status_code=limited.status_code,
                        headers=limited.headers,
                    )
        errors = [
            {k: item[k] for k in ("type", "loc", "msg") if k in item} for item in error.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    def lock_auth_actor(db, actor_id):
        if engine.dialect.name == "sqlite":
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
        return db.scalar(
            select(Actor)
            .where(Actor.id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def issue_browser_session(db, actor, response, request, remember):
        seconds = 30 * 24 * 60 * 60 if remember else 12 * 60 * 60
        raw = secrets.token_urlsafe(32)
        db.add(
            BrowserSession(
                digest=digest(raw), actor_id=actor.id, expires_at=now() + timedelta(seconds=seconds)
            )
        )
        response.set_cookie(
            "workspace_session",
            raw,
            max_age=seconds if remember else None,
            path="/v1",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )

    def human(a):
        if a.kind != "human":
            raise HTTPException(403, "Human action required")

    def project_access(db, a, project_id, write=False, owner=False, lease=True, lock=False):
        connection = getattr(a, "_workspace_connection", None)
        if connection is not None and connection.project_id != project_id:
            raise HTTPException(404, "Project not found")
        # The project lock is held until the entire request transaction commits.
        # Under PostgreSQL READ COMMITTED, later writers see committed cursor/key state.
        query = select(Project).where(Project.id == project_id)
        mutating_connection = connection is not None and getattr(a, "_workspace_mutating", False)
        if write or owner or lock or mutating_connection:
            query = query.with_for_update()
        p = db.scalar(query.execution_options(populate_existing=True))
        m = db.get(Membership, (project_id, a.id), populate_existing=True)
        if p is None or m is None:
            raise HTTPException(404, "Project not found")
        if owner:
            human(a)
            if m.role != "owner":
                raise HTTPException(403, "Project owner required")
        if write and m.muted:
            raise HTTPException(403, "Participant is muted")
        if connection is not None:
            db.refresh(connection)
            if connection.revoked:
                raise HTTPException(401, "Invalid or revoked connection")
            if lease and mutating_connection:
                session_id = getattr(a, "_workspace_session_id", None)
                if (
                    not session_id
                    or len(session_id) > 200
                    or not connection.session_digest
                    or not hmac.compare_digest(connection.session_digest, digest(session_id))
                    or connection.lease_expires_at is None
                    or utc(connection.lease_expires_at) <= now()
                ):
                    raise HTTPException(403, "Claim an active lease for this session first")
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

    def enforce_rate(db, p, a, limit=None):
        current = now()
        window = db.get(RateWindow, (p.id, a.id))
        if window is None:
            window = RateWindow(project_id=p.id, actor_id=a.id, started_at=current, count=0)
            db.add(window)
        elapsed = (current - utc(window.started_at)).total_seconds()
        if elapsed >= 60:
            window.started_at, window.count = current, 0
        elif window.count >= min(app.state.posting_limit, limit or app.state.posting_limit):
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

    def lock_actor_credentials(db, actor_ids):
        if engine.dialect.name == "postgresql":
            for actor_id in sorted(set(actor_ids)):
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": credential_lock_key(actor_id)},
                )

    def connection_json(db, connection):
        actor = db.get(Actor, connection.actor_id)
        project = db.get(Project, connection.project_id)
        return {
            "id": connection.id,
            "actor": actor_json(actor),
            "project": {"id": project.id, "name": project.name},
            "label": connection.label,
            "created_at": timestamp(connection.created_at),
            "revoked": connection.revoked,
            "bound": connection.session_digest is not None,
            "active": (
                not connection.revoked
                and connection.lease_expires_at is not None
                and utc(connection.lease_expires_at) > now()
            ),
            "lease_expires_at": timestamp(connection.lease_expires_at)
            if connection.lease_expires_at
            else None,
            "last_seen_at": timestamp(connection.last_seen_at) if connection.last_seen_at else None,
        }

    def visible_connection(db, a, connection_id, *, own_token=False, lock=False, write=False):
        connection = db.get(AgentConnection, connection_id)
        scoped = getattr(a, "_workspace_connection", None)
        if connection is None:
            raise HTTPException(404, "Connection not found")
        if own_token:
            if scoped is None or scoped.id != connection.id:
                raise HTTPException(403, "Use this connection's credential")
        elif a.kind == "human":
            if db.get(Actor, connection.actor_id).owner_id != a.id:
                raise HTTPException(404, "Connection not found")
        elif scoped is None or scoped.id != connection.id:
            raise HTTPException(404, "Connection not found")
        project_access(db, a, connection.project_id, write=write, lock=lock, lease=False)
        db.refresh(connection)
        return connection

    @app.post("/v1/agent-connections", status_code=201)
    def create_connection(
        body: AgentConnectionInput,
        request: Request,
        authorization: str | None = Header(default=None),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        if getattr(a, "_workspace_connection", None) is not None:
            raise HTTPException(403, "Connection credentials cannot create connections")
        if a.kind == "human":
            if body.actor_id is None:
                raise HTTPException(422, "Choose one of your agents")
            actor = db.get(Actor, str(body.actor_id))
            if actor is None or actor.kind != "agent" or actor.owner_id != a.id:
                raise HTTPException(403, "Choose one of your own agents")
        else:
            if body.actor_id is not None and str(body.actor_id) != a.id:
                raise HTTPException(403, "Agents can create connections only for themselves")
            actor = a
        lock_actor_credentials(db, [a.id, actor.id])
        # Actor revocation uses these same locks. The source may have been revoked
        # while this request waited, and must not mint a replacement credential.
        resolve_identity(request, authorization, db)
        project_id = str(body.project_id)
        project_access(db, a, project_id, write=True)
        membership = db.get(Membership, (project_id, actor.id), populate_existing=True)
        if membership is None:
            raise HTTPException(404, "Agent is not a project member")
        if membership.muted:
            raise HTTPException(403, "Participant is muted")
        connection = AgentConnection(actor_id=actor.id, project_id=project_id, label=body.label)
        db.add(connection)
        db.flush()
        token = issue_token(db, actor, connection_id=connection.id)
        return {"connection": connection_json(db, connection), "token": token}

    @app.get("/v1/agent-connections")
    def connections(
        project_id: UUID | None = None,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        query = (
            select(AgentConnection)
            .join(Actor, Actor.id == AgentConnection.actor_id)
            .join(
                Membership,
                (Membership.project_id == AgentConnection.project_id)
                & (Membership.actor_id == a.id),
            )
            .where(Actor.owner_id == a.id)
            .order_by(AgentConnection.created_at.desc(), AgentConnection.id)
        )
        if project_id is not None:
            query = query.where(AgentConnection.project_id == str(project_id))
        return {"items": [connection_json(db, connection) for connection in db.scalars(query)]}

    @app.get("/v1/agent-connections/{connection_id}")
    def get_connection(
        connection_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        return connection_json(db, visible_connection(db, a, connection_id))

    @app.delete("/v1/agent-connections/{connection_id}", status_code=204)
    def revoke_connection(
        connection_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        connection = visible_connection(db, a, connection_id, lock=True)
        connection.revoked, connection.lease_expires_at = True, None
        db.execute(update(Token).where(Token.connection_id == connection.id).values(revoked=True))
        return Response(status_code=204)

    @app.post("/v1/agent-connections/{connection_id}/claim")
    def claim_connection(
        connection_id: str,
        body: AgentSessionInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        connection = visible_connection(db, a, connection_id, own_token=True, lock=True, write=True)
        supplied = digest(body.session_id)
        if connection.session_digest and not hmac.compare_digest(
            connection.session_digest, supplied
        ):
            raise HTTPException(409, "Connection is permanently bound to a different session")
        current = now()
        competing = db.scalar(
            select(AgentConnection.id)
            .where(
                AgentConnection.actor_id == a.id,
                AgentConnection.project_id == connection.project_id,
                AgentConnection.id != connection.id,
                AgentConnection.revoked.is_(False),
                AgentConnection.lease_expires_at > current,
            )
            .limit(1)
        )
        if competing is not None:
            raise HTTPException(409, "This agent already has an active connection in the project")
        connection.session_digest = supplied
        connection.lease_expires_at = current + timedelta(seconds=300)
        connection.last_seen_at = current
        db.flush()
        return {"connection": connection_json(db, connection)}

    @app.post("/v1/agent-connections/{connection_id}/release")
    def release_connection(
        connection_id: str,
        body: AgentSessionInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        connection = visible_connection(db, a, connection_id, own_token=True, lock=True)
        if not connection.session_digest or not hmac.compare_digest(
            connection.session_digest, digest(body.session_id)
        ):
            raise HTTPException(409, "Connection is permanently bound to a different session")
        connection.lease_expires_at = None
        db.flush()
        return {"connection": connection_json(db, connection)}

    @app.get("/v1/me")
    def me(
        a=Depends(authenticated, scope="function"), db: Session = Depends(session, scope="function")
    ):
        result = self_actor_json(a)
        connection = getattr(a, "_workspace_connection", None)
        if connection is not None:
            project_access(db, a, connection.project_id)
            result["connection"] = connection_json(db, connection)
        return result

    @app.patch("/v1/me")
    def edit_profile(
        body: ProfileInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        if body.name is not None:
            a.name = body.name
        if body.mention_emails is not None:
            a.mention_emails = body.mention_emails
        db.flush()
        return self_actor_json(a)

    CODE_TTL = timedelta(minutes=30)
    CODE_EMAILS_PER_HOUR = 5
    CODE_ATTEMPTS = 5

    def lock_actor_row(db, a):
        # Serializes code requests and attempts for one person on every backend.
        return db.scalar(select(Actor).where(Actor.id == a.id).with_for_update(key_share=True))

    @app.put("/v1/me/email", status_code=202)
    def request_email_code(
        body: MyEmailInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        if not email_enabled():
            raise HTTPException(503, "Email sending is not configured on this server")
        lock_actor_row(db, a)
        current = now()
        key = digest("email-code:" + a.id)
        window = db.get(AuthAttempt, key)
        if window is None:
            window = AuthAttempt(key=key, started_at=current, count=0)
            db.add(window)
        elapsed = (current - utc(window.started_at)).total_seconds()
        if elapsed >= 3600:
            window.started_at, window.count = current, 0
            elapsed = 0
        if window.count >= CODE_EMAILS_PER_HOUR:
            db.commit()
            raise HTTPException(
                429,
                "Too many confirmation codes requested; try again later",
                headers={"Retry-After": str(max(1, int(3600 - elapsed) + 1))},
            )
        # Debit before sending and return normally on failure so the debit commits.
        window.count += 1
        code = f"{secrets.randbelow(10**6):06d}"
        pending = db.get(EmailVerification, a.id)
        if pending is None:
            pending = EmailVerification(actor_id=a.id)
            db.add(pending)
        pending.email, pending.code_digest = body.email, digest(code)
        pending.expires_at, pending.attempts = current + CODE_TTL, 0
        db.flush()
        message = EmailMessage()
        message["From"] = os.environ["PILOT_EMAIL_FROM"]
        message["To"] = body.email
        message["Subject"] = "Your Research Workspace confirmation code"
        message["Date"] = format_datetime(current)
        message["Message-ID"] = make_msgid()
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(
            f"Your Research Workspace confirmation code is {code}\n\n"
            "It expires in 30 minutes. If you did not ask for it, ignore this email.\n"
        )
        try:
            submit_message(message)
        except (MailUnavailable, ValueError, KeyError):
            return JSONResponse(
                status_code=502,
                content={"detail": "The confirmation email could not be sent; try again later."},
            )
        return {"status": "code_sent", "expires_in_minutes": 30}

    @app.post("/v1/me/email/verify")
    def verify_email(
        body: MyEmailCode,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        lock_actor_row(db, a)
        pending = db.get(EmailVerification, a.id, populate_existing=True)
        if pending is None:
            raise HTTPException(400, "No confirmation code is pending; request a new one")
        if utc(pending.expires_at) <= now():
            db.delete(pending)
            db.commit()
            raise HTTPException(400, "The code has expired; request a new one")
        if pending.attempts >= CODE_ATTEMPTS:
            raise HTTPException(429, "Too many wrong codes; request a new one")
        if not hmac.compare_digest(pending.code_digest, digest(body.code)):
            pending.attempts += 1
            db.commit()
            raise HTTPException(400, "That code is not correct")
        a.email, a.email_verified_at = pending.email, now()
        db.delete(pending)
        db.flush()
        return self_actor_json(a)

    @app.delete("/v1/me/email", status_code=204)
    def remove_email(
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        a.email, a.email_verified_at = None, None
        pending = db.get(EmailVerification, a.id)
        if pending is not None:
            db.delete(pending)
        db.flush()
        return Response(status_code=204)

    @app.post("/v1/auth/login")
    def login(
        body: LoginInput,
        request: Request,
        response: Response,
        db: Session = Depends(session, scope="function"),
    ):
        same_origin(request)
        consume_auth_rate(db, request, body.handle)
        actor = db.scalar(select(Actor).where(Actor.handle == body.handle))
        previous_hash = actor.password_hash if actor and actor.kind == "human" else None
        valid = verify_password(body.password, previous_hash)
        if not valid:
            raise HTTPException(401, "Invalid handle or password")
        actor = lock_auth_actor(db, actor.id)
        # Login cannot mint a session using a password revoked/changed during scrypt.
        if actor.password_hash != previous_hash:
            raise HTTPException(401, "Invalid handle or password")
        issue_browser_session(db, actor, response, request, body.remember)
        return {"actor": self_actor_json(actor)}

    @app.post("/v1/auth/password")
    def set_password(
        body: PasswordInput,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        same_origin(request, required=not bool(authorization))
        actor_id, handle = a.id, a.handle
        consume_auth_rate(db, request, handle)
        db.refresh(a)
        previous_hash = a.password_hash
        if previous_hash and not verify_password(body.current_password or "", previous_hash):
            raise HTTPException(401, "Current password is incorrect")
        new_hash = hash_password(body.password)
        actor = lock_auth_actor(db, actor_id)
        resolve_identity(request, authorization, db)
        if actor.password_hash != previous_hash:
            raise HTTPException(409, "Password changed during request; retry")
        actor.password_hash = new_hash
        db.execute(
            update(BrowserSession).where(BrowserSession.actor_id == actor.id).values(revoked=True)
        )
        issue_browser_session(db, actor, response, request, body.remember)
        return {"actor": self_actor_json(actor)}

    @app.post("/v1/auth/logout")
    def logout(
        request: Request, response: Response, db: Session = Depends(session, scope="function")
    ):
        same_origin(request)
        raw = request.cookies.get("workspace_session")
        if raw and len(raw) <= 128:
            credential = db.get(BrowserSession, digest(raw))
            if credential:
                credential.revoked = True
        response.delete_cookie(
            "workspace_session",
            path="/v1",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return {"status": "ok"}

    def notification_scope(a):
        return (
            select(Notification)
            .join(
                Membership,
                (Membership.project_id == Notification.project_id) & (Membership.actor_id == a.id),
            )
            .where(Notification.actor_id == a.id)
        )

    def notification_json(db, notification, related=None):
        if related is None:
            message = db.get(Message, notification.message_id)
            thread = db.get(Thread, message.thread_id)
            channel = db.get(Channel, thread.channel_id)
            project = db.get(Project, notification.project_id)
            author = db.get(Actor, message.author_id)
        else:
            project, channel, thread, message, author = related
        return {
            "id": notification.id,
            "created_at": timestamp(notification.created_at),
            "read_at": timestamp(notification.read_at) if notification.read_at else None,
            "project": {"id": project.id, "name": project.name},
            "channel": {"id": channel.id, "name": channel.name},
            "thread": {"id": thread.id, "title": thread.title},
            "message": {
                "id": message.id,
                "reply_to": message.reply_to,
                "author": actor_json(author),
                "text": message.text[:300],
                "truncated": len(message.text) > 300,
            },
        }

    @app.get("/v1/notifications")
    def notifications(
        limit: int = Query(50, ge=1, le=100),
        before: int | None = Query(default=None, ge=1),
        unread_only: bool = False,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        scope = notification_scope(a)
        cursor, unread_count = db.execute(
            scope.with_only_columns(
                func.max(Notification.id),
                func.count(Notification.id).filter(Notification.read_at.is_(None)),
            )
        ).one()
        if cursor is None:
            return {"items": [], "next_cursor": None, "cursor": None, "unread_count": 0}
        query = (
            scope.add_columns(Project, Channel, Thread, Message, Actor)
            .join(Message, Message.id == Notification.message_id)
            .join(Thread, Thread.id == Message.thread_id)
            .join(Channel, Channel.id == Thread.channel_id)
            .join(Project, Project.id == Notification.project_id)
            .join(Actor, Actor.id == Message.author_id)
            .where(Notification.id <= cursor)
        )
        if before is not None:
            query = query.where(Notification.id < before)
        if unread_only:
            query = query.where(Notification.read_at.is_(None))
        rows = list(db.execute(query.order_by(Notification.id.desc()).limit(limit + 1)))
        return {
            "items": [notification_json(db, row[0], row[1:]) for row in rows[:limit]],
            "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None,
            "cursor": cursor,
            "unread_count": unread_count,
        }

    @app.patch("/v1/notifications/{notification_id}")
    def read_notification(
        notification_id: int,
        body: NotificationReadInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        notification = db.scalar(notification_scope(a).where(Notification.id == notification_id))
        if notification is None:
            raise HTTPException(404, "Notification not found")
        # Membership removal and read updates use the same project lock. Refresh
        # after waiting so repeated reads preserve the first committed timestamp.
        project_access(db, a, notification.project_id, lock=True)
        db.refresh(notification)
        if body.read and notification.read_at is None:
            notification.read_at = now()
        elif not body.read:
            notification.read_at = None
        db.flush()
        return notification_json(db, notification)

    @app.post("/v1/notifications/read-all")
    def read_all_notifications(
        body: NotificationsReadAllInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        human(a)
        scope = notification_scope(a).where(
            Notification.id <= body.through, Notification.read_at.is_(None)
        )
        project_ids = scope.with_only_columns(Notification.project_id).distinct()
        # Fixed lock order prevents membership changes during the update. The
        # final membership subquery excludes projects lost while acquiring locks.
        list(
            db.scalars(
                select(Project)
                .where(Project.id.in_(project_ids))
                .order_by(Project.id)
                .with_for_update()
            )
        )
        visible_projects = select(Membership.project_id).where(Membership.actor_id == a.id)
        result = db.execute(
            update(Notification)
            .where(
                Notification.actor_id == a.id,
                Notification.id <= body.through,
                Notification.read_at.is_(None),
                Notification.project_id.in_(visible_projects),
            )
            .values(read_at=now())
            .execution_options(synchronize_session=False)
        )
        return {"updated": result.rowcount}

    @app.get("/v1/connection")
    def connection():
        return {
            "ssh_host": os.environ.get("PILOT_SSH_HOST", ""),
            "ssh_app_port": int(os.environ.get("PILOT_SSH_APP_PORT", "18000")),
            "local_port": 8002,
            "email_enabled": email_enabled(),
        }

    def invitation_json(invitation):
        return {
            "id": invitation.id,
            "role": invitation.role,
            "created_at": timestamp(invitation.created_at),
            "expires_at": timestamp(invitation.expires_at),
            "used": invitation.used_at is not None,
            "revoked": invitation.revoked,
        }

    def valid_invitation(db, code, lock=False, claim_secret=None):
        invitation = db.scalar(select(Invitation).where(Invitation.digest == digest(code)))
        if invitation is None:
            raise HTTPException(404, "Invitation unavailable")
        query = select(Project).where(Project.id == invitation.project_id)
        if lock:
            query = query.with_for_update()
        project = db.scalar(query.execution_options(populate_existing=True))
        # Project writers serialize acceptance, revocation and owner changes. Refresh
        # after waiting for that lock so a concurrent acceptance cannot use stale state.
        db.refresh(invitation)
        inviter = db.get(Membership, (invitation.project_id, invitation.inviter_id))
        if (
            project is None
            or invitation.revoked
            or utc(invitation.expires_at) <= now()
            or inviter is None
            or inviter.role != "owner"
        ):
            raise HTTPException(410, "Invitation expired, used, or withdrawn")
        if invitation.used_at is not None:
            if (
                claim_secret is None
                or invitation.claim_digest is None
                or not hmac.compare_digest(invitation.claim_digest, digest(claim_secret))
                or db.get(Membership, (project.id, invitation.accepted_actor_id)) is None
            ):
                raise HTTPException(410, "Invitation already accepted")
            if invitation.issued_token_digest:
                credential = db.get(Token, invitation.issued_token_digest)
                if credential is None or credential.revoked:
                    raise HTTPException(410, "Invitation credential has been revoked")
        return invitation, project

    def claim_token(body):
        # Both inputs contain independent 256-bit secrets. A stable keyed token lets
        # the original browser recover a lost response without storing plaintext or
        # rotating credentials when concurrent retries arrive in a different order.
        return hmac.new(body.code.encode(), body.claim_secret.encode(), hashlib.sha256).hexdigest()

    @app.post("/v1/projects/{project_id}/invitations", status_code=201)
    def create_invitation(
        project_id: str,
        body: InvitationInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        project_access(db, a, project_id, owner=True)
        code = secrets.token_urlsafe(32)
        invitation = Invitation(
            digest=digest(code),
            project_id=project_id,
            inviter_id=a.id,
            role=body.role,
            expires_at=now() + timedelta(hours=body.expires_in_hours),
        )
        db.add(invitation)
        db.flush()
        return {**invitation_json(invitation), "code": code}

    @app.post("/v1/projects/{project_id}/invitations/{invitation_id}/email", status_code=202)
    def email_invitation(
        project_id: str,
        invitation_id: str,
        body: InvitationEmail,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        project = project_access(db, a, project_id, owner=True)
        invitation, _ = valid_invitation(db, body.code)
        if invitation.project_id != project_id or invitation.id != invitation_id:
            raise HTTPException(404, "Invitation not found")
        if not email_enabled():
            raise HTTPException(503, "Email sending is not configured; copy the invitation instead")
        enforce_rate(db, project, a, limit=5)
        try:
            message = invitation_message(
                project.name,
                invitation.role,
                utc(invitation.expires_at),
                body.code,
                body.to,
                inviter_name=a.name,
            )
            submit_invitation(message)
        except (MailUnavailable, ValueError, KeyError):
            # Return normally so the attempt debit commits even when SMTP delivery
            # is uncertain. Raising here would roll it back and bypass the limit.
            return JSONResponse(
                status_code=502,
                content={
                    "detail": "Email submission could not be confirmed. The invitation is still "
                    "available; check before retrying, or copy the instructions instead."
                },
            )
        return {"status": "submitted"}

    @app.get("/v1/projects/{project_id}/invitations")
    def invitations(
        project_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        project_access(db, a, project_id, owner=True)
        return {
            "items": [
                invitation_json(i)
                for i in db.scalars(
                    select(Invitation)
                    .where(Invitation.project_id == project_id)
                    .order_by(Invitation.created_at.desc())
                    .limit(100)
                )
            ]
        }

    @app.delete("/v1/projects/{project_id}/invitations/{invitation_id}", status_code=204)
    def revoke_invitation(
        project_id: str,
        invitation_id: str,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        project_access(db, a, project_id, owner=True)
        invitation = db.get(Invitation, invitation_id)
        if invitation is None or invitation.project_id != project_id:
            raise HTTPException(404, "Invitation not found")
        invitation.revoked = True
        return Response(status_code=204)

    @app.post("/v1/invitations/preview")
    def preview_invitation(
        body: InvitationCode,
        request: Request,
        authorization: str | None = Header(default=None),
        db: Session = Depends(session, scope="function"),
    ):
        if authorization:
            identity = resolve_identity(request, authorization, db)
            if getattr(identity, "_workspace_connection", None) is not None:
                raise HTTPException(403, "Connection credentials cannot preview invitations")
        invitation, project = valid_invitation(db, body.code, claim_secret=body.claim_secret)
        return {
            "project": project_json(project),
            "role": invitation.role,
            "expires_at": timestamp(invitation.expires_at),
            "accepted": invitation.used_at is not None,
        }

    @app.post("/v1/invitations/accept", status_code=201)
    def accept_invitation(
        body: InvitationAccept,
        request: Request,
        authorization: str | None = Header(default=None),
        db: Session = Depends(session, scope="function"),
    ):
        if authorization:
            invited_identity = resolve_identity(request, authorization, db)
            if getattr(invited_identity, "_workspace_connection", None) is not None:
                raise HTTPException(403, "Connection credentials cannot accept invitations")
        new_password_hash = None
        anonymous_password = (
            body.password is not None
            and not authorization
            and not request.cookies.get("workspace_session")
        )
        if body.password is not None:
            same_origin(request)
            if anonymous_password:
                consume_auth_rate(db, request, "invite:" + body.code)
                pending, _ = valid_invitation(db, body.code, claim_secret=body.claim_secret)
                if pending.used_at is None:
                    new_password_hash = hash_password(body.password)
        if not authorization and request.cookies.get("workspace_session"):
            same_origin(request)
        if anonymous_password and engine.dialect.name == "sqlite":
            db.rollback()
            db.execute(text("BEGIN IMMEDIATE"))
        invitation, project = valid_invitation(
            db, body.code, lock=True, claim_secret=body.claim_secret
        )
        if invitation.used_at is not None:
            actor = db.get(Actor, invitation.accepted_actor_id)
            if invitation.issued_token_digest:
                token = claim_token(body)
                if not hmac.compare_digest(digest(token), invitation.issued_token_digest):
                    raise HTTPException(410, "Invitation credential unavailable")
            else:
                authenticated_actor = resolve_identity(request, authorization, db)
                human(authenticated_actor)
                if authenticated_actor.id != actor.id:
                    raise HTTPException(
                        403, "Sign in with the identity that accepted this invitation"
                    )
                token = None
            member = db.get(Membership, (project.id, actor.id))
            return {
                "actor": actor_json(actor),
                "token": token,
                "project": project_json(project),
                "role": member.role,
            }
        token = None
        if authorization or request.cookies.get("workspace_session"):
            actor = resolve_identity(request, authorization, db)
            human(actor)
            if body.name is not None or body.handle is not None or body.password is not None:
                raise HTTPException(422, "Existing identities keep their profile and password")
        else:
            if body.claim_secret is None:
                raise HTTPException(
                    422, "A private random claim_secret is required for new identities"
                )
            if body.name is None or not body.name.strip():
                raise HTTPException(422, "Choose a display name")
            if engine.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(731958240)"))
            try:
                handle = choose_handle(db, body.name, supplied=body.handle)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            except FileExistsError:
                raise HTTPException(409, "Handle already exists; choose another") from None
            actor = Actor(
                name=body.name.strip(),
                handle=handle,
                kind="human",
                is_admin=False,
                password_hash=new_password_hash,
            )
            db.add(actor)
            db.flush()
            token = issue_token(db, actor, claim_token(body))
        if db.get(Membership, (project.id, actor.id)):
            raise HTTPException(
                409, "You already belong to this project; ask an owner to change your role"
            )
        member = Membership(project_id=project.id, actor_id=actor.id, role=invitation.role)
        db.add(member)
        invitation.used_at = now()
        invitation.claim_digest = digest(body.claim_secret) if body.claim_secret else None
        invitation.accepted_actor_id = actor.id
        invitation.issued_token_digest = digest(token) if token else None
        db.flush()
        emit(db, project, "membership.updated", member_json(db, member))
        return {
            "actor": actor_json(actor),
            "token": token,
            "project": project_json(project),
            "role": member.role,
        }

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
        lock_actor_credentials(db, [actor_id])
        projects_to_lock = select(AgentConnection.project_id).where(
            AgentConnection.actor_id == actor_id
        )
        list(
            db.scalars(
                select(Project)
                .where(Project.id.in_(projects_to_lock))
                .order_by(Project.id)
                .with_for_update()
            )
        )
        target = db.scalar(
            select(Actor)
            .where(Actor.id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        db.execute(update(Token).where(Token.actor_id == actor_id).values(revoked=True))
        db.execute(
            update(AgentConnection)
            .where(AgentConnection.actor_id == actor_id)
            .values(revoked=True, lease_expires_at=None)
        )
        if target.kind == "human":
            target.password_hash = None
            db.execute(
                update(BrowserSession)
                .where(BrowserSession.actor_id == actor_id)
                .values(revoked=True)
            )
        return Response(status_code=204)

    @app.get("/v1/projects")
    def projects(
        a=Depends(authenticated, scope="function"), db: Session = Depends(session, scope="function")
    ):
        query = (
            select(Project)
            .join(Membership)
            .where(Membership.actor_id == a.id)
            .order_by(Project.name, Project.id)
        )
        connection = getattr(a, "_workspace_connection", None)
        if connection is not None:
            query = query.where(Project.id == connection.project_id)
        return {"items": [project_json(p) for p in db.scalars(query)]}

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

    @app.patch("/v1/projects/{project_id}")
    def rename_project(
        project_id: str,
        body: Named,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, owner=True)
        if p.name != body.name:
            p.name = body.name
            emit(db, p, "project.updated", project_json(p))
        return project_json(p)

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
        if body.role == "guest" and target.kind == "agent":
            raise HTTPException(403, "Guest roles are for humans; agents use member")
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

    @app.put("/v1/projects/{project_id}/members/{actor_id}/role")
    def change_role(
        project_id: str,
        actor_id: str,
        body: MemberRoleInput,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        p = project_access(db, a, project_id, owner=True)
        member = db.get(Membership, (project_id, actor_id))
        if member is None:
            raise HTTPException(404, "Membership not found")
        if db.get(Actor, actor_id).kind != "human":
            raise HTTPException(403, "Only humans may have owner or guest roles")
        if member.role == "owner" and body.role != "owner":
            owners = db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.project_id == project_id, Membership.role == "owner")
            )
            if owners <= 1:
                raise HTTPException(409, "At least one owner must remain")
        if member.role != body.role:
            member.role = body.role
            emit(db, p, "membership.updated", member_json(db, member))
        return member_json(db, member)

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

    @app.patch("/v1/channels/{channel_id}")
    def rename_channel(
        channel_id: str,
        body: Named,
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        channel = db.get(Channel, channel_id)
        if channel is None:
            raise HTTPException(404, "Channel not found")
        p = project_access(db, a, channel.project_id, owner=True)
        db.refresh(channel)
        if channel.name != body.name:
            channel.name = body.name
            emit(db, p, "channel.updated", channel_json(channel))
        return channel_json(channel)

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
        background: BackgroundTasks,
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
        recipients = list(
            db.scalars(
                select(Actor)
                .where(Actor.id.in_(set(body.mentions)), Actor.id != a.id, Actor.kind == "human")
                .order_by(Actor.id)
                .with_for_update(key_share=True)
            )
        )
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
        for recipient in recipients:
            db.add(
                Notification(
                    actor_id=recipient.id, project_id=p.id, message_id=m.id, created_at=m.created_at
                )
            )
        result = message_json(db, m)
        emit(db, p, "message.created", result, t.id)
        if recipients:
            # Runs after the response, which the function-scoped session only sends once
            # the transaction has committed; a rollback never reaches this point.
            background.add_task(send_mention_emails, factory, [r.id for r in recipients])
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
        selected = rows[:limit]
        wanted = [*selected, *filter(None, [trigger, parent])]
        serialized = messages_json(db, wanted)
        serialized_by_id = {v["id"]: v for v in serialized}
        trigger_data = (
            clipped(dict(serialized_by_id[trigger.id]), max_chars // 4) if trigger else None
        )
        parent_data = clipped(dict(serialized_by_id[parent.id]), max_chars // 4) if parent else None
        # Distribute remaining text over recent messages, giving newer messages first claim.
        recent = []
        for index, message in enumerate(selected):
            allowance = remaining // (len(selected) - index)
            recent.append(clipped(dict(serialized_by_id[message.id]), allowance))
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
            "items": messages_json(db, rows[:limit]),
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

    @app.get("/v1/projects/{project_id}/search")
    def search_messages(
        project_id: str,
        q: str = Query(...),
        limit: int = Query(20, ge=1, le=50),
        before: int | None = Query(None, ge=1),
        a=Depends(authenticated, scope="function"),
        db: Session = Depends(session, scope="function"),
    ):
        q = q.strip()
        if not 1 <= len(q) <= 200:
            raise HTTPException(422, "q must be 1-200 characters")
        p = project_access(db, a, project_id)
        snapshot = p.cursor
        match, terms = search_clause(db.get_bind().dialect.name, q)
        conditions = [Message.project_id == p.id, Message.sequence <= snapshot, match]
        if before is not None:
            conditions.append(Message.sequence < before)
        newest_first = select(Message).where(*conditions).limit(limit + 1)
        rows = []
        if db.get_bind().dialect.name == "postgresql":
            # Walking the (project_id, sequence) index backwards is ideal for a word in most
            # messages but filters the whole project for a rare one. A bounded walk over the
            # newest messages answers the first case at once; if it does not fill the page the
            # real order is computed from the GIN matches, where `+ 0` keeps the planner from
            # picking the backwards index scan. Both return the same rows in the same order.
            upper = snapshot if before is None else min(snapshot, before - 1)
            rows = list(
                db.scalars(
                    newest_first.where(Message.sequence > upper - SEARCH_RECENT_WINDOW).order_by(
                        Message.sequence.desc()
                    )
                )
            )
            order = (Message.sequence + 0).desc()
        else:
            order = Message.sequence.desc()
        if len(rows) <= limit:
            rows = list(db.scalars(newest_first.order_by(order)))
        page = rows[:limit]
        threads = {
            t.id: t
            for t in db.scalars(select(Thread).where(Thread.id.in_({m.thread_id for m in page})))
        }
        items = [
            {
                "message": message,
                "thread": {
                    "id": threads[row.thread_id].id,
                    "title": threads[row.thread_id].title,
                    "channel_id": threads[row.thread_id].channel_id,
                },
                "snippet": search_snippet(row.text, terms),
            }
            for row, message in zip(page, messages_json(db, page), strict=True)
        ]
        return {
            "items": items,
            "next_before": page[-1].sequence if len(rows) > limit else None,
            "cursor": snapshot,
        }

    static = Path(__file__).parent / "static"
    if static.exists():
        script_version = hashlib.sha256((static / "app.js").read_bytes()).hexdigest()[:12]
        style_version = hashlib.sha256((static / "styles.css").read_bytes()).hexdigest()[:12]
        app.mount("/static", StaticFiles(directory=static), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            html = (
                (static / "index.html")
                .read_text()
                .replace('src="/static/app.js"', f'src="/static/app.js?v={script_version}"')
                .replace(
                    'href="/static/styles.css"', f'href="/static/styles.css?v={style_version}"'
                )
            )
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return app


app = create_app()
