"""Workflow recovery and feedback safeguards, independent of provider/model behavior."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "python"))
from agent_commons_client import ApiError  # noqa: E402
from agent_workflow import AgentWorkflow, Reply  # noqa: E402


def event(sequence, *, author="human", mentions=("agent",), thread="thread", **message):
    return {
        "id": sequence,
        "type": "message.created",
        "thread_id": thread,
        "payload": {
            "id": f"message-{sequence}", "author": {"id": author},
            "mentions": list(mentions), "metadata": {}, "reply_to": None, **message,
        },
    }


class Transport:
    def __init__(self, events=(), page_limit=None):
        self.events, self.page_limit = list(events), page_limit
        self.calls, self.context_calls, self.inbox_calls = [], [], []
        self.accepted = {}
        self.fail_after_accept = False

    def inbox(self, project_id, after=0, limit=50, **kwargs):
        self.inbox_calls.append((after, kwargs))
        pending = [item for item in self.events if item["id"] > after]
        count = self.page_limit or limit
        page = pending[:count]
        return {
            "items": page,
            "next_cursor": page[-1]["id"] if len(pending) > count else None,
            "cursor": max([after, *[item["id"] for item in self.events]]),
        }

    def context(self, thread_id, **kwargs):
        self.context_calls.append((thread_id, kwargs))
        trigger = next(e["payload"] for e in self.events if e["payload"]["id"] ==
                       kwargs["trigger_message_id"])
        return {
            "thread": {"id": thread_id}, "rules": {"text": "Do useful work", "version": 2},
            "messages": [{"id": trigger["id"], "text": "latest", "truncated": True}],
            "trigger_message": trigger, "parent_message": None, "cursor": 99,
            "has_older": True,
        }

    def post_message(self, thread_id, text, **kwargs):
        body = {"thread_id": thread_id, "text": text, **kwargs}
        self.calls.append(body)
        previous = self.accepted.setdefault(kwargs["idempotency_key"], body)
        assert body == previous, "same retry key must have same body"
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise ApiError(0, "response lost")
        return {"id": "reply"}


def workflow(tmp_path, transport, callback=None, **options):
    return AgentWorkflow(
        transport, "project", "agent", tmp_path / "private" / "checkpoint.json",
        callback or (lambda e, c: f"Reply to {e['id']}"), **options,
    )


def test_new_worker_tails_snapshot_and_backlog_requires_opt_in(tmp_path):
    transport = Transport([event(1), event(2)])
    worker = workflow(tmp_path, transport)
    assert worker.run_once() == 0
    assert worker.cursor == 2
    transport.events.append(event(3))
    assert worker.run_once() == 1
    assert transport.calls[0]["reply_to"] == "message-3"
    replay = workflow(tmp_path / "explicit", Transport([event(1)]), replay_backlog=True)
    assert replay.run_once() == 1


def test_incremental_cursor_never_jumps_to_snapshot_and_ignored_tail_advances(tmp_path):
    transport = Transport([event(2), event(6), event(10)], page_limit=1)
    worker = workflow(tmp_path, transport, replay_backlog=True)
    assert worker.run_once() == 1
    assert worker.cursor == 2  # global snapshot is ten; two more pages remain
    assert worker.run_once() == 1
    assert worker.cursor == 6
    assert worker.run_once() == 1
    assert worker.cursor == 10
    assert [c["text"] for c in transport.calls] == ["Reply to 2", "Reply to 6", "Reply to 10"]
    transport.inbox = lambda *a, **k: {"items": [], "next_cursor": None, "cursor": 18}
    assert worker.run_once() == 0
    assert worker.cursor == 18


def test_self_reactions_unmentioned_and_workflow_replies_do_not_loop(tmp_path):
    reaction = {**event(2), "type": "reaction.created"}
    transport = Transport([
        event(1, author="agent"), reaction, event(3, mentions=()),
        event(4, mentions=(), metadata={"agent_workflow": {"trigger_event_id": 9}}), event(5),
    ])
    worker = workflow(tmp_path, transport, replay_backlog=True)
    assert worker.run_once() == 1
    assert worker.cursor == 5
    assert len(transport.context_calls) == 1
    assert transport.calls[0]["text"] == "Reply to 5"
    assert transport.calls[0]["mentions"] == []  # no automatic mention of another agent


def test_followed_threads_are_explicit_and_budget_stops_feedback(tmp_path):
    transport = Transport([event(n, mentions=()) for n in range(1, 5)])
    worker = workflow(tmp_path, transport, replay_backlog=True,
                      followed_thread_ids=["thread"], max_replies_per_thread=2)
    assert worker.run_once() == 2
    assert worker.cursor == 4
    assert worker.state["counts"] == {"thread": 2}
    assert transport.inbox_calls[0][1]["followed_thread_ids"] == ("thread",)
    worker.resume("thread", reset_budget=True)
    transport.events.append(event(5, mentions=()))
    assert worker.run_once() == 1


def test_callback_failure_acknowledges_only_prior_success_and_recovers(tmp_path):
    transport = Transport([event(1), event(2), event(3)])
    seen = []

    def callback(e, context):
        seen.append(e["id"])
        if e["id"] == 2:
            raise RuntimeError("agent unavailable")
        return "ok"

    worker = workflow(tmp_path, transport, callback, replay_backlog=True)
    with pytest.raises(RuntimeError, match="unavailable"):
        worker.run_once()
    assert worker.cursor == 1
    assert json.loads(worker.path.read_text())["cursor"] == 1
    restarted = workflow(tmp_path, transport)
    assert restarted.run_once() == 2
    assert [c["reply_to"] for c in transport.calls] == ["message-1", "message-2", "message-3"]
    assert seen == [1, 2]


def test_lost_post_response_retries_identical_saved_body_after_restart(tmp_path):
    transport = Transport([event(1)])
    transport.fail_after_accept = True
    worker = workflow(tmp_path, transport, replay_backlog=True)
    with pytest.raises(ApiError):
        worker.run_once()
    assert worker.cursor == 0
    assert worker.state["pending"]["text"] == "Reply to 1"

    def should_not_rerun(*_):
        raise AssertionError("callback must not rerun for a saved pending reply")

    restarted = workflow(tmp_path, transport, should_not_rerun)
    assert restarted.run_once() == 1
    assert transport.calls[0] == transport.calls[1]
    assert len(transport.accepted) == 1
    assert restarted.state["counts"] == {"thread": 1}
    assert restarted.state["pending"] is None
    assert restarted.path.stat().st_mode & 0o777 == 0o600
    assert restarted.path.parent.stat().st_mode & 0o777 == 0o700


def test_context_requests_bounds_preserves_flags_and_reply_targets_parent(tmp_path):
    transport = Transport([event(1, reply_to="top-level")])
    contexts = []
    worker = workflow(tmp_path, transport, lambda e, c: contexts.append(c) or "answer",
                      replay_backlog=True, context_limit=3, max_chars=1000)
    worker.run_once()
    assert transport.context_calls == [("thread", {
        "trigger_message_id": "message-1", "limit": 3, "max_chars": 1000,
    })]
    assert contexts[0]["messages"][0]["truncated"] is True
    assert contexts[0]["has_older"] is True
    assert transport.calls[0]["reply_to"] == "top-level"


def test_oversized_context_is_not_passed_to_callback_or_acknowledged(tmp_path):
    transport = Transport([event(1)])
    original = transport.context

    def oversized(*args, **kwargs):
        context = original(*args, **kwargs)
        context["rules"]["text"] = "x" * 1001
        return context

    transport.context = oversized
    worker = workflow(tmp_path, transport, replay_backlog=True, max_chars=1000)
    with pytest.raises(ValueError, match="character budget"):
        worker.run_once()
    assert worker.cursor == 0
    assert transport.calls == []


def test_pause_resume_preserves_global_backlog_thread_pause_skips_future_triggers(tmp_path):
    transport = Transport([event(1)])
    worker = workflow(tmp_path, transport, replay_backlog=True)
    worker.pause()
    assert worker.run_once() == 0
    assert worker.cursor == 0 and transport.inbox_calls == []
    worker.resume()
    assert worker.run_once() == 1
    worker.pause("thread")
    transport.events.append(event(2))
    assert worker.run_once() == 0 and worker.cursor == 2
    worker.resume("thread")
    transport.events.append(event(3))
    assert worker.run_once() == 1
    assert [c["reply_to"] for c in transport.calls] == ["message-1", "message-3"]


def test_checkpoint_identity_cannot_be_reused_by_another_actor(tmp_path):
    worker = workflow(tmp_path, Transport())
    worker.pause()
    with pytest.raises(ValueError, match="another actor"):
        AgentWorkflow(Transport(), "project", "other-agent", worker.path, lambda *_: None)


def test_callback_has_only_bounded_text_and_no_raw_metadata(tmp_path):
    transport = Transport([event(1, text="x" * 20000, metadata={"huge": "x" * 8000})])
    original = transport.context

    def thin_context(*args, **kwargs):
        context = original(*args, **kwargs)
        context["trigger_message"] = {
            **context["trigger_message"], "text": "bounded", "truncated": True,
        }
        context["messages"][0]["metadata"] = {"huge": "x" * 8000}
        return context

    transport.context = thin_context
    received = []
    worker = workflow(tmp_path, transport, lambda e, c: received.append((e, c)),
                      replay_backlog=True, max_chars=1000)
    assert worker.run_once() == 0
    envelope, context = received[0]
    assert "text" not in envelope["payload"] and "metadata" not in envelope["payload"]
    assert "metadata" not in context["trigger_message"]
    assert "metadata" not in context["messages"][0]
    assert context["trigger_message"]["text"] == "bounded"
    assert context["trigger_message"]["truncated"] is True


def test_missing_context_trigger_fails_cleanly_without_acknowledging(tmp_path):
    transport = Transport([event(1)])
    original = transport.context

    def missing(*args, **kwargs):
        return {**original(*args, **kwargs), "trigger_message": None}

    transport.context = missing
    worker = workflow(tmp_path, transport, replay_backlog=True)
    with pytest.raises(ValueError, match="requested trigger"):
        worker.run_once()
    assert worker.cursor == 0


def test_explicit_agent_delegation_exchanges_bounded_replies_without_passive_loops(tmp_path):
    shared_events = [event(1, mentions=("a",))]

    class ConnectedTransport(Transport):
        def __init__(self, actor):
            super().__init__()
            self.events = shared_events
            self.actor = actor

        def post_message(self, thread_id, text, **kwargs):
            unseen = kwargs["idempotency_key"] not in self.accepted
            result = super().post_message(thread_id, text, **kwargs)
            if unseen:
                self.events.append(event(
                    len(self.events) + 1, author=self.actor, thread=thread_id, text=text,
                    mentions=kwargs["mentions"], metadata=kwargs["metadata"],
                    reply_to=kwargs["reply_to"],
                ))
            return result

    a_transport, b_transport = ConnectedTransport("a"), ConnectedTransport("b")
    a = AgentWorkflow(a_transport, "project", "a", tmp_path / "a.json",
                      lambda *_: Reply("Please review", ("b",)), replay_backlog=True,
                      followed_thread_ids=["thread"], max_replies_per_thread=2)
    b = AgentWorkflow(b_transport, "project", "b", tmp_path / "b.json",
                      lambda *_: Reply("Review completed", ("a",)), replay_backlog=True,
                      max_replies_per_thread=2)
    assert a.run_once() == b.run_once() == a.run_once() == b.run_once() == 1
    assert a.run_once() == b.run_once() == 0  # deliberate handoffs stop at configured budgets
    assert len(shared_events) == 5
    shared_events.append(event(6, author="b", mentions=(),
                               metadata={"agent_workflow": {"trigger_event_id": 5}}))
    a.resume("thread", reset_budget=True)
    assert a.run_once() == 0  # following alone does not answer another helper's plain reply


def test_pending_reply_respects_thread_pause_and_resume_reuses_mentions(tmp_path):
    transport = Transport([event(1)])
    transport.fail_after_accept = True
    worker = workflow(tmp_path, transport, lambda *_: Reply("delegate", ("other",)),
                      replay_backlog=True)
    with pytest.raises(ApiError):
        worker.run_once()
    worker.pause("thread")
    restarted = workflow(tmp_path, transport)
    assert restarted.run_once() == 0
    assert restarted.cursor == 0
    assert len(transport.calls) == 1
    assert restarted.state["pending"]["mentions"] == ["other"]
    restarted.resume("thread")
    assert restarted.run_once() == 1
    assert transport.calls[0] == transport.calls[1]
    assert transport.calls[1]["mentions"] == ["other"]
    assert restarted.cursor == 1


def test_oversized_callback_reply_is_rejected_before_pending_or_cursor_write(tmp_path):
    transport = Transport([event(1)])
    worker = workflow(tmp_path, transport, lambda *_: Reply("x" * 20001), replay_backlog=True)
    with pytest.raises(ValueError, match="20000 character"):
        worker.run_once()
    assert worker.state["pending"] is None
    assert worker.cursor == 0
    assert transport.calls == []
    assert not worker.path.exists()
