import hashlib
import hmac
import secrets
import threading

from .models import Token


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(session, actor, token=None, connection_id=None):
    token = token or secrets.token_urlsafe(32)
    session.add(Token(digest=digest(token), actor_id=actor.id, connection_id=connection_id))
    return token


# Scrypt uses roughly 32 MiB per calculation. Keep peak work bounded in a small pilot.

_SCRYPT_WORK = threading.BoundedSemaphore(2)


def _derive(password, salt):
    with _SCRYPT_WORK:
        return hashlib.scrypt(
            password.encode(), salt=salt, n=32768, r=8, p=3, maxmem=64 * 1024 * 1024, dklen=64
        )


def hash_password(password):
    salt = secrets.token_bytes(16)
    return f"scrypt$32768$8$3${salt.hex()}${_derive(password, salt).hex()}"


_DUMMY_HASH = hash_password("dummy password for unknown identity")


def verify_password(password, encoded):
    target = encoded or _DUMMY_HASH
    try:
        scheme, n, r, p, salt, expected = target.split("$")
        if (scheme, n, r, p) != ("scrypt", "32768", "8", "3"):
            return False
        result = _derive(password, bytes.fromhex(salt))
        valid = hmac.compare_digest(result, bytes.fromhex(expected))
        return bool(encoded) and valid
    except (ValueError, TypeError):
        return False


def credential_lock_key(actor_id):
    return int.from_bytes(
        hashlib.sha256(("agent-credentials:" + actor_id).encode()).digest()[:8], "big", signed=True
    )
