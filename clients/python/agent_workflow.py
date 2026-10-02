"""Opt-in, single-worker polling reference; no model, scheduler, or provider dependency."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_commons_client import Client


@dataclass(frozen=True)
class Reply:
    """Explicitly address other actors; plain text replies never infer mentions."""

    text: str
    mentions: tuple[str, ...] = ()


class AgentWorkflow:
    """Call an operator-supplied callback sequentially for eligible inbox messages.

    The callback receives (event, bounded_context), and returns text, Reply, or None.
    Only one process may use a checkpoint. Exceptions propagate for the caller's
    retry/backoff policy. A generated reply is persisted before sending, so retries
    retain its exact body and idempotency key, including after a process restart.
    """

    def __init__(
        self,
        client: Client,
        project_id: str,
        actor_id: str,
        checkpoint: str | Path,
        respond: Callable[[dict[str, Any], dict[str, Any]], str | Reply | None],
        *,
        followed_thread_ids=(),
        max_replies_per_thread: int = 3,
        context_limit: int = 20,
        max_chars: int = 12000,
        replay_backlog: bool = False,
    ):
        if max_replies_per_thread < 1:
            raise ValueError("max_replies_per_thread must be positive")
        if not 1 <= context_limit <= 100 or not 1000 <= max_chars <= 50000:
            raise ValueError("context limits must be 1..100 messages and 1000..50000 characters")
        self.client, self.project_id, self.actor_id = client, project_id, actor_id
        self.path, self.respond = Path(checkpoint), respond
        self.followed = tuple(followed_thread_ids)
        self.budget, self.context_limit, self.max_chars = (
            max_replies_per_thread, context_limit, max_chars
        )
        self.identity = {"project_id": project_id, "actor_id": actor_id}
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("identity") != self.identity or self.state.get("version") != 1:
                raise ValueError("checkpoint belongs to another actor/project or version")
            os.chmod(self.path, 0o600)
        else:
            self.state = {
                "version": 1,
                "identity": self.identity,
                "initialized": replay_backlog,
                "cursor": 0,
                "counts": {},
                "paused_threads": [],
                "paused": False,
                "pending": None,
            }

    @property
    def cursor(self):
        return self.state["cursor"]

    def _commit(self, **changes):
        state = {**self.state, **changes}
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            # Persist the rename as well as the file contents on local filesystems.
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.state = state

    def pause(self, thread_id: str | None = None):
        """Global pause preserves backlog; thread pause acknowledges skipped triggers."""
        if thread_id is None:
            self._commit(paused=True)
        else:
            self._commit(paused_threads=sorted(set(self.state["paused_threads"]) | {thread_id}))

    def resume(self, thread_id: str | None = None, *, reset_budget: bool = False):
        if thread_id is None:
            self._commit(paused=False)
        else:
            counts = dict(self.state["counts"])
            if reset_budget:
                counts.pop(thread_id, None)
            self._commit(
                counts=counts,
                paused_threads=[t for t in self.state["paused_threads"] if t != thread_id],
            )

    def _eligible(self, event):
        message, thread_id = event.get("payload", {}), event.get("thread_id")
        return (
            event.get("type") == "message.created"
            and bool(thread_id)
            and message.get("author", {}).get("id") != self.actor_id
            and (not message.get("metadata", {}).get("agent_workflow")
                 or self.actor_id in message.get("mentions", []))
            and (self.actor_id in message.get("mentions", []) or thread_id in self.followed)
            and thread_id not in self.state["paused_threads"]
            and self.state["counts"].get(thread_id, 0) < self.budget
        )

    def _context(self, event):
        context = self.client.context(
            event["thread_id"], trigger_message_id=event["payload"]["id"],
            limit=self.context_limit, max_chars=self.max_chars,
        )
        # Refuse an oversized/malformed response before it reaches the callback.
        # Keep all server truncation flags: the callback must not assume completeness.
        entries = [context["rules"], *context["messages"]]
        entries += [context.get("trigger_message"), context.get("parent_message")]
        if len(context["messages"]) > self.context_limit:
            raise ValueError("context exceeded requested message limit")
        if sum(len(entry.get("text", "")) for entry in entries if entry) > self.max_chars:
            raise ValueError("context exceeded requested character budget")
        if (context.get("trigger_message") or {}).get("id") != event["payload"]["id"]:
            raise ValueError("context did not identify the requested trigger")
        # Pass only useful conversation fields, never arbitrary message metadata,
        # reaction actor lists, or an unbounded duplicate of the event body.
        def message(value):
            if value is None:
                return None
            result = {key: value[key] for key in (
                "id", "thread_id", "sequence", "text", "reply_to", "mentions", "created_at",
                "truncated",
            ) if key in value}
            result["author"] = {key: value.get("author", {})[key]
                                for key in ("id", "handle", "kind")
                                if key in value.get("author", {})}
            return result

        return {
            "thread": {key: context["thread"][key] for key in ("id", "channel_id", "project_id")
                       if key in context["thread"]},
            "rules": {key: context["rules"][key] for key in ("text", "version", "truncated")
                      if key in context["rules"]},
            "messages": [message(value) for value in context["messages"]],
            "trigger_message": message(context["trigger_message"]),
            "parent_message": message(context.get("parent_message")),
            "cursor": context["cursor"], "has_older": context["has_older"],
        }

    def _deliver_pending(self):
        pending = self.state["pending"]
        self.client.post_message(
            pending["thread_id"], pending["text"], reply_to=pending["reply_to"],
            mentions=pending.get("mentions", []),
            metadata={"agent_workflow": {"trigger_event_id": pending["event_id"]}},
            idempotency_key=pending["key"],
        )
        counts = dict(self.state["counts"])
        counts[pending["thread_id"]] = counts.get(pending["thread_id"], 0) + 1
        self._commit(cursor=pending["event_id"], pending=None, counts=counts)

    def run_once(self, *, limit: int = 50) -> int:
        """Process one inbox page; return successful reply count, not event count."""
        if not 1 <= limit <= 100:
            raise ValueError("inbox limit must be 1..100")
        if self.state["paused"]:
            return 0
        replies = 0
        if self.state["pending"]:
            if self.state["pending"]["thread_id"] in self.state["paused_threads"]:
                return 0
            self._deliver_pending()
            replies += 1
        page = self.client.inbox(
            self.project_id, after=self.cursor, limit=limit, followed_thread_ids=self.followed
        )
        if not self.state["initialized"]:
            # Tail only for a new worker. Explicit replay_backlog=True starts at zero.
            self._commit(cursor=page["cursor"], initialized=True)
            return replies
        events = page["items"]
        ids = [event["id"] for event in events]
        end = page["next_cursor"] if page["next_cursor"] is not None else page["cursor"]
        if ids != sorted(set(ids)) or any(i <= self.cursor or i > end for i in ids):
            raise ValueError("inbox events must be ordered after the checkpoint and within page")
        if end < self.cursor:
            raise ValueError("inbox cursor went backwards")
        for event in events:
            if self._eligible(event):
                context = self._context(event)
                envelope = {key: event[key] for key in (
                    "id", "type", "project_id", "thread_id", "created_at"
                ) if key in event}
                envelope["payload"] = {key: context["trigger_message"][key] for key in (
                    "id", "author", "reply_to", "mentions"
                ) if key in context["trigger_message"]}
                reply = self.respond(envelope, context)
                if reply is not None:
                    response = reply if isinstance(reply, Reply) else Reply(reply)
                    if not isinstance(response.text, str) or not response.text.strip():
                        raise ValueError("callback must return nonempty text, Reply, or None")
                    if len(response.text) > 20000:
                        raise ValueError("reply text exceeds the service's 20000 character limit")
                    if len(response.mentions) > 20 or any(
                        not isinstance(actor, str) or not actor for actor in response.mentions
                    ):
                        raise ValueError("Reply.mentions allows at most 20 nonempty actor IDs")
                    material = f"{self.project_id}:{self.actor_id}:{event['id']}"
                    pending = {
                        "event_id": event["id"], "thread_id": event["thread_id"],
                        "text": response.text, "mentions": list(response.mentions),
                        "reply_to": event["payload"].get("reply_to") or event["payload"]["id"],
                        "key": "workflow-" + hashlib.sha256(material.encode()).hexdigest(),
                    }
                    self._commit(pending=pending)
                    self._deliver_pending()
                    replies += 1
                    continue
            self._commit(cursor=event["id"])
        # next_cursor is the scanned position, not the snapshot: ignored events may
        # advance it, but jumping to page.cursor before the final page drops work.
        self._commit(cursor=end)
        return replies
