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

# Production cost parameters. They are plain module constants, never read from the
# environment; only the test suite replaces them (tests/conftest.py).
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 3


def _derive(password, salt):
    with _SCRYPT_WORK:
        return hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=64 * 1024 * 1024,
            dklen=64,
        )


def hash_password(password):
    salt = secrets.token_bytes(16)
    params = f"{SCRYPT_N}${SCRYPT_R}${SCRYPT_P}"
    return f"scrypt${params}${salt.hex()}${_derive(password, salt).hex()}"


_DUMMY_HASH = hash_password("dummy password for unknown identity")


def verify_password(password, encoded):
    target = encoded or _DUMMY_HASH
    try:
        scheme, n, r, p, salt, expected = target.split("$")
        if (scheme, n, r, p) != ("scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)):
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
