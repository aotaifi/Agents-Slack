"""Stable, globally unique participant handles."""

import re
import unicodedata

from sqlalchemy import select

from .models import Actor

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def slug(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")[:60].rstrip("-") or "participant"


def choose_handle(db, name, owner=None, supplied=None):
    prefix = f"{owner.handle}." if owner else ""
    if supplied is not None:
        local = supplied.removeprefix(prefix) if prefix else supplied
        if not SLUG.fullmatch(local) or len(local) > 60:
            raise ValueError("Handle must be a lowercase slug of at most 60 characters")
        candidate = prefix + local
        if db.scalar(select(Actor.id).where(Actor.handle == candidate)):
            raise FileExistsError("Handle already exists")
        return candidate
    base = prefix + slug(name)
    candidate, suffix = base, 2
    while db.scalar(select(Actor.id).where(Actor.handle == candidate)):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
