import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Named(StrictModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class ActorInput(Named):
    kind: Literal["human", "agent"]
    handle: str | None = Field(default=None, min_length=1, max_length=130)


class ProjectInput(Named):
    description: str = Field(default="", max_length=10000)


class MemberInput(StrictModel):
    actor_id: str
    role: Literal["owner", "guest", "member"] = "member"


class MemberRoleInput(StrictModel):
    role: Literal["owner", "guest"]


class InvitationInput(StrictModel):
    role: Literal["owner", "guest"] = "guest"
    expires_in_hours: int = Field(default=72, ge=1, le=168)


class InvitationCode(StrictModel):
    code: str = Field(min_length=40, max_length=100)
    claim_secret: str | None = Field(default=None, min_length=43, max_length=100)


class InvitationAccept(InvitationCode):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    handle: str | None = Field(default=None, min_length=1, max_length=60)


class InvitationEmail(StrictModel):
    code: str = Field(min_length=40, max_length=100)
    to: str = Field(min_length=3, max_length=254)

    @field_validator("to")
    @classmethod
    def single_address(cls, value):
        # One ASCII mailbox; reject display names, lists, header breaks and SMTPUTF8.
        if not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+",
            value,
        ):
            raise ValueError("Use one plain email address")
        local, domain = value.rsplit("@", 1)
        if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
            raise ValueError("Invalid email address")
        if any(len(label) > 63 for label in domain.split(".")):
            raise ValueError("Invalid email domain")
        return value


class MuteInput(StrictModel):
    muted: bool


class RulesInput(StrictModel):
    text: str = Field(max_length=50000)


class ThreadInput(StrictModel):
    title: str = Field(min_length=1, max_length=300)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


class MessageInput(StrictModel):
    text: str = Field(min_length=1, max_length=20000)
    mentions: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict = Field(default_factory=dict)
    reply_to: UUID | None = None

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value, allow_nan=False).encode()) > 8192:
            raise ValueError("metadata exceeds 8192 bytes")
        return value


class ReactionInput(StrictModel):
    emoji: Literal["👍", "✅", "👀", "❓", "❤️", "🎉"]
