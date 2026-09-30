import hashlib
import secrets

from .models import Token


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(session, actor):
    token = secrets.token_urlsafe(32)
    session.add(Token(digest=digest(token), actor_id=actor.id))
    return token
