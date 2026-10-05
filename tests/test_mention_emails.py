import logging
import re
import threading
from datetime import timedelta

import pytest
from conftest import auth, setup_thread
from sqlalchemy import select, update
from test_notifications import mention, recipient

from agent_commons import cli, mail, main, mention_email
from agent_commons.models import (
    Actor,
    ActorEmailLog,
    EmailVerification,
    Message,
    Notification,
    now,
)

ADDRESS = "alex@example.test"


@pytest.fixture
def mail_config(monkeypatch):
    for key in ("PILOT_SMTP_USERNAME", "PILOT_SMTP_PASSWORD", "PILOT_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PILOT_SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("PILOT_EMAIL_FROM", "Research Workspace <workspace@example.test>")
    monkeypatch.setenv("PILOT_SSH_HOST", "lab.example.test")
    monkeypatch.setenv("PILOT_PUBLIC_URL", "https://workspace.example.test")


@pytest.fixture
def outbox(mail_config, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "submit_message", sent.append)
    monkeypatch.setattr(mention_email, "submit_message", sent.append)
    return sent


def code_in(message):
    return re.search(r"code is (\d{6})", message.get_content()).group(1)


def confirm(client, user, outbox, address=ADDRESS):
    headers = auth(user["token"])
    response = client.put("/v1/me/email", json={"email": address}, headers=headers)
    assert response.status_code == 202, response.text
    code = code_in(outbox.pop())
    response = client.post("/v1/me/email/verify", json={"code": code}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def verified_human(client, pid, outbox, address=ADDRESS):
    user = recipient(client, [pid])
    confirm(client, user, outbox, address)
    return user


def emailed(app, actor_id):
    with app.state.session_factory() as db:
        return list(
            db.scalars(
                select(Notification.emailed_at)
                .where(Notification.actor_id == actor_id)
                .order_by(Notification.id)
            )
        )


def test_verify_flow_codes_lockout_expiry_and_reverification(service, outbox):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    user = recipient(client, [pid])
    headers = auth(user["token"])
    me = client.get("/v1/me", headers=headers).json()
    assert (me["email"], me["email_verified"], me["mention_emails"]) == (None, False, True)
    for bad in ("not an address", "a@b", "x@example.test, y@example.test", "Bob <b@example.test>"):
        assert client.put("/v1/me/email", json={"email": bad}, headers=headers).status_code == 422
    assert client.put("/v1/me/email", json={"email": ADDRESS}, headers=headers).status_code == 202
    message = outbox.pop()
    assert str(message["To"]) == ADDRESS
    assert "Your Research Workspace confirmation code is " in message.get_content()
    code = code_in(message)
    # Pending addresses are not exposed and nothing is verified yet.
    assert client.get("/v1/me", headers=headers).json()["email"] is None
    with app.state.session_factory() as db:
        assert code not in repr(db.get(EmailVerification, user["actor"]["id"]).__dict__)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(4):
        assert (
            client.post("/v1/me/email/verify", json={"code": wrong}, headers=headers).status_code
            == 400
        )
    # Fifth wrong attempt locks this code even for the correct value.
    assert (
        client.post("/v1/me/email/verify", json={"code": wrong}, headers=headers).status_code == 400
    )
    assert (
        client.post("/v1/me/email/verify", json={"code": code}, headers=headers).status_code == 429
    )
    assert client.get("/v1/me", headers=headers).json()["email_verified"] is False
    # A fresh code works; expiry is enforced.
    client.put("/v1/me/email", json={"email": ADDRESS}, headers=headers)
    code = code_in(outbox.pop())
    with app.state.session_factory() as db:
        row = db.get(EmailVerification, user["actor"]["id"])
        row.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert (
        client.post("/v1/me/email/verify", json={"code": code}, headers=headers).status_code == 400
    )
    assert (
        client.post("/v1/me/email/verify", json={"code": code}, headers=headers).status_code == 400
    )
    client.put("/v1/me/email", json={"email": ADDRESS}, headers=headers)
    result = client.post(
        "/v1/me/email/verify", json={"code": code_in(outbox.pop())}, headers=headers
    )
    assert result.status_code == 200
    assert (result.json()["email"], result.json()["email_verified"]) == (ADDRESS, True)
    with app.state.session_factory() as db:
        actor = db.get(Actor, user["actor"]["id"])
        assert actor.email == ADDRESS and actor.email_verified_at is not None
        assert db.get(EmailVerification, actor.id) is None
    # Changing the address needs a new confirmation; the old one stays in force meanwhile.
    client.put("/v1/me/email", json={"email": "new@example.test"}, headers=headers)
    new_code = code_in(outbox.pop())
    assert client.get("/v1/me", headers=headers).json()["email"] == ADDRESS
    wrong = "000000" if new_code != "000000" else "111111"
    assert (
        client.post("/v1/me/email/verify", json={"code": wrong}, headers=headers).status_code == 400
    )
    assert client.get("/v1/me", headers=headers).json()["email"] == ADDRESS
    confirmed = client.post("/v1/me/email/verify", json={"code": new_code}, headers=headers)
    assert confirmed.json()["email"] == "new@example.test"
    # The mention-email switch, and removal.
    assert (
        client.patch("/v1/me", json={"mention_emails": False}, headers=headers).json()[
            "mention_emails"
        ]
        is False
    )
    assert client.patch("/v1/me", json={}, headers=headers).status_code == 422
    assert (
        client.patch("/v1/me", json={"name": "Alex K"}, headers=headers).json()["name"] == "Alex K"
    )
    assert client.delete("/v1/me/email", headers=headers).status_code == 204
    me = client.get("/v1/me", headers=headers).json()
    assert (me["email"], me["email_verified"]) == (None, False)
    # Addresses are private: other participants and the inbox never see them.
    confirm(client, user, outbox)
    assert ADDRESS not in client.get(f"/v1/projects/{pid}/members").text
    assert ADDRESS not in client.get("/v1/notifications", headers=headers).text


def test_code_requests_are_rate_limited_and_need_configured_email(service, monkeypatch):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    headers = auth(recipient(client, [pid])["token"])
    for key in ("PILOT_SMTP_HOST", "PILOT_EMAIL_FROM"):
        monkeypatch.delenv(key, raising=False)
    response = client.put("/v1/me/email", json={"email": ADDRESS}, headers=headers)
    assert response.status_code == 503 and "not configured" in response.json()["detail"]
    monkeypatch.setenv("PILOT_SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("PILOT_EMAIL_FROM", "workspace@example.test")
    sent = []
    monkeypatch.setattr(main, "submit_message", sent.append)
    codes = [client.put("/v1/me/email", json={"email": ADDRESS}, headers=headers) for _ in range(6)]
    assert [r.status_code for r in codes] == [202] * 5 + [429]
    assert "Retry-After" in codes[-1].headers and len(sent) == 5

    def broken(_):
        raise mail.MailUnavailable("down")

    monkeypatch.setattr(main, "submit_message", broken)
    other = auth(recipient(client, [pid])["token"])
    assert client.put("/v1/me/email", json={"email": ADDRESS}, headers=other).status_code == 502


def test_agents_cannot_use_email_endpoints(service, outbox):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    agent = client.post("/v1/actors", json={"name": "Bot", "kind": "agent"}).json()
    client.post(f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]})
    headers = auth(agent["token"])
    assert client.put("/v1/me/email", json={"email": ADDRESS}, headers=headers).status_code == 403
    verify = client.post("/v1/me/email/verify", json={"code": "123456"}, headers=headers)
    assert verify.status_code == 403
    assert client.delete("/v1/me/email", headers=headers).status_code == 403
    assert (
        client.patch("/v1/me", json={"mention_emails": False}, headers=headers).status_code == 403
    )
    me = client.get("/v1/me", headers=headers).json()
    assert not {"email", "email_verified", "mention_emails"} & set(me)
    assert outbox == []


def test_verified_human_gets_one_email_after_commit(service, outbox, monkeypatch):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    seen = []

    def submit(message):
        with app.state.session_factory() as db:
            committed = db.scalar(
                select(Message.id).where(Message.text.contains("committed first"))
            )
            claimed = db.scalar(
                select(Notification.emailed_at).where(Notification.actor_id == user["actor"]["id"])
            )
        seen.append((committed is not None, claimed is not None))
        outbox.append(message)

    monkeypatch.setattr(mention_email, "submit_message", submit)
    mention(client, tid, user, "committed first")
    assert seen == [(True, True)]
    assert len(outbox) == 1
    message = outbox[0]
    assert str(message["To"]) == ADDRESS and message["Auto-Submitted"] == "auto-generated"
    assert message["Message-ID"] and message["Date"]
    assert str(message["Subject"]) == "@Owner mentioned you in Physics"
    body = message.get_content()
    assert "Owner in Physics / Results:\n  committed first" in body
    assert "https://workspace.example.test/" in body
    assert body.rstrip().endswith("Turn them off in My account.")
    assert all(emailed(app, user["actor"]["id"]))


def test_tunnel_instructions_when_no_public_url(service, outbox, monkeypatch):
    client, _, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    monkeypatch.delenv("PILOT_PUBLIC_URL")
    mention(client, tid, user, "hello")
    body = outbox[0].get_content()
    assert "YOUR_UNIVERSITY_USERNAME@lab.example.test" in body and "http://127.0.0.1:8002/" in body


def test_unverified_or_opted_out_humans_get_nothing(service, outbox):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    plain = recipient(client, [pid])
    mention(client, tid, plain, "no address")
    pending = recipient(client, [pid])
    client.put("/v1/me/email", json={"email": ADDRESS}, headers=auth(pending["token"]))
    outbox.clear()
    mention(client, tid, pending, "pending only")
    optout = verified_human(client, pid, outbox, "off@example.test")
    client.patch("/v1/me", json={"mention_emails": False}, headers=auth(optout["token"]))
    mention(client, tid, optout, "switched off")
    assert outbox == []
    for user in (plain, pending, optout):
        assert len(emailed(app, user["actor"]["id"])) == 1
        assert not any(emailed(app, user["actor"]["id"]))
        assert (
            client.get("/v1/notifications", headers=auth(user["token"])).json()["unread_count"] == 1
        )


def test_read_notifications_and_self_mentions_are_never_emailed(service, outbox, monkeypatch):
    client, app, owner, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    # The author is a human with a verified address and mentions themselves.
    me = {"actor": {"id": client.get("/v1/me").json()["id"]}, "token": owner["token"]}
    confirm(client, me, outbox, "owner@example.test")
    mention(client, tid, me, "note to self")
    assert outbox == []
    with app.state.session_factory() as db:
        assert (
            db.scalar(select(Notification.id).where(Notification.actor_id == me["actor"]["id"]))
            is None
        )
    monkeypatch.setattr(main, "send_mention_emails", lambda *args: None)
    mention(client, tid, user, "will be read")
    item = client.get("/v1/notifications", headers=auth(user["token"])).json()["items"][0]
    client.patch(
        f"/v1/notifications/{item['id']}", json={"read": True}, headers=auth(user["token"])
    )
    assert mention_email.send_for_actor(app.state.session_factory, user["actor"]["id"]) == "idle"
    assert outbox == []


def test_burst_while_a_send_is_in_flight_is_bundled_without_duplicates(
    service, outbox, monkeypatch
):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    factory = app.state.session_factory
    texts = [f"burst-{n}" for n in range(5)]
    entered, release = threading.Event(), threading.Event()

    def slow(message):
        outbox.append(message)
        if len(outbox) == 1:
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(mention_email, "submit_message", slow)
    monkeypatch.setattr(main, "send_mention_emails", lambda *args: None)
    mention(client, tid, user, texts[0])
    first = threading.Thread(
        target=mention_email.send_mention_emails, args=(factory, [user["actor"]["id"]])
    )
    first.start()
    assert entered.wait(10)
    # Later mentions run the real after-commit hook while the first send is in flight.
    monkeypatch.setattr(main, "send_mention_emails", mention_email.send_mention_emails)
    for text in texts[1:]:
        mention(client, tid, user, text)
    release.set()
    first.join(10)
    assert not first.is_alive()
    assert 1 <= len(outbox) <= 2
    bodies = "\n".join(m.get_content() for m in outbox)
    for text in texts:
        assert bodies.count(text) == 1
    assert all(emailed(app, user["actor"]["id"]))
    assert mention_email.send_for_actor(factory, user["actor"]["id"]) == "idle"


def test_parallel_senders_never_duplicate(service, outbox):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    mention(client, tid, user, "already sent")
    outbox.clear()
    with app.state.session_factory() as db:
        db.execute(update(Notification).values(emailed_at=None))
        db.commit()
    factory, actor_id = app.state.session_factory, user["actor"]["id"]
    threads = [
        threading.Thread(target=mention_email.send_for_actor, args=(factory, actor_id))
        for _ in range(4)
    ]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert len(outbox) == 1


def test_daily_cap_and_next_day_sweep(service, outbox, monkeypatch):
    client, app, _, url = service
    monkeypatch.setenv("PILOT_MENTION_EMAIL_DAILY_CAP", "2")
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    for n in range(3):
        mention(client, tid, user, f"mention {n}")
    assert len(outbox) == 2
    assert [e is not None for e in emailed(app, user["actor"]["id"])] == [True, True, False]
    # Still capped later the same day, including for the sweep.
    assert cli.send_mention_emails(url) == 0 and len(outbox) == 2
    with app.state.session_factory() as db:
        db.execute(update(ActorEmailLog).values(day=now().date() - timedelta(days=1)))
        db.commit()
    assert cli.send_mention_emails(url) == 1 and len(outbox) == 3
    assert "mention 2" in outbox[-1].get_content()
    assert cli.send_mention_emails(url) == 0


def test_smtp_failure_releases_claim_and_sweep_sends_once(service, outbox, monkeypatch):
    client, app, _, url = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)

    def broken(_):
        raise mail.MailUnavailable("relay down")

    monkeypatch.setattr(mention_email, "submit_message", broken)
    mention(client, tid, user, "retry me")
    assert emailed(app, user["actor"]["id"]) == [None]
    with app.state.session_factory() as db:
        assert db.scalar(select(ActorEmailLog.sent)) == 0
    monkeypatch.setattr(mention_email, "submit_message", outbox.append)
    assert cli.send_mention_emails(url) == 1
    assert len(outbox) == 1 and "retry me" in outbox[0].get_content()
    assert cli.send_mention_emails(url) == 0 and len(outbox) == 1


def test_old_notifications_are_never_emailed(service, outbox, monkeypatch):
    client, app, _, url = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    monkeypatch.setattr(main, "send_mention_emails", lambda *args: None)
    mention(client, tid, user, "stale")
    with app.state.session_factory() as db:
        db.execute(update(Notification).values(created_at=now() - timedelta(hours=25)))
        db.commit()
    assert cli.send_mention_emails(url) == 0
    assert mention_email.send_for_actor(app.state.session_factory, user["actor"]["id"]) == "idle"
    assert outbox == [] and emailed(app, user["actor"]["id"]) == [None]


def test_rolled_back_posts_send_nothing(service, outbox):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    target = user["actor"]["id"]
    stranger = client.post("/v1/actors", json={"name": "Out", "kind": "human"}).json()
    rejected = client.post(
        f"/v1/threads/{tid}/messages",
        json={"text": "x", "mentions": [target, stranger["actor"]["id"]]},
    )
    assert rejected.status_code == 422
    blank = client.post(f"/v1/threads/{tid}/messages", json={"text": " ", "mentions": [target]})
    assert blank.status_code == 422
    app.state.posting_limit = 1
    mention(client, tid, user, "allowed")
    outbox.clear()
    limited = client.post(
        f"/v1/threads/{tid}/messages", json={"text": "too fast", "mentions": [target]}
    )
    assert limited.status_code == 429
    assert outbox == []
    assert len(emailed(app, target)) == 1


def test_snippet_is_bounded_and_header_injection_is_neutralized(service, outbox):
    client, app, owner, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    evil = "Eve\r\nBcc: evil@example.test X-Injected: 1"
    assert client.patch("/v1/me", json={"name": evil}).status_code == 200
    long_text = "alpha\n\n  beta\t" + "word " * 100 + "THE-TAIL"
    mention(client, tid, user, long_text)
    body = outbox[0].get_content()
    line = next(row for row in body.splitlines() if row.startswith("  alpha beta"))
    assert len(line.strip()) == 201 and line.endswith("…")
    assert "THE-TAIL" not in body and "\t" not in line
    p2 = client.post("/v1/projects", json={"name": "Lab\r\nBcc: x@example.test"}).json()
    c2 = client.post(f"/v1/projects/{p2['id']}/channels", json={"name": "G"}).json()
    t2 = client.post(
        f"/v1/channels/{c2['id']}/threads", json={"title": "Topic\nBcc: y@example.test"}
    ).json()
    client.post(f"/v1/projects/{p2['id']}/members", json={"actor_id": user["actor"]["id"]})
    outbox.clear()
    mention(client, t2["id"], user, "hi")
    message = outbox[0]
    raw = message.as_bytes().decode()
    header_block = raw.split("\n\n", 1)[0]
    assert not [
        row for row in header_block.splitlines() if row.lower().startswith(("bcc", "x-inj"))
    ]
    assert "\n" not in str(message["Subject"]) and "\r" not in str(message["Subject"])
    assert message["Bcc"] is None and message["X-Injected"] is None
    assert [m for m in message.get_content().splitlines() if m.startswith("Bcc")] == []


def test_many_mentions_use_one_email(service, outbox, monkeypatch):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    monkeypatch.setattr(main, "send_mention_emails", lambda *args: None)
    for n in range(12):
        mention(client, tid, user, f"item {n}")
    assert mention_email.send_for_actor(app.state.session_factory, user["actor"]["id"]) == "sent"
    assert len(outbox) == 1
    message = outbox[0]
    assert str(message["Subject"]) == "12 new mentions in Research Workspace"
    assert "...and 2 more in the workspace." in message.get_content()


def test_message_text_and_address_never_reach_info_logs(service, outbox, monkeypatch, caplog):
    client, app, _, _ = service
    pid, tid = setup_thread(client)
    user = verified_human(client, pid, outbox)
    caplog.set_level(logging.INFO)
    mention(client, tid, user, "classified-sentence-42")

    def broken(_):
        raise mail.MailUnavailable(f"cannot reach {ADDRESS} classified-sentence-42")

    monkeypatch.setattr(mention_email, "submit_message", broken)
    mention(client, tid, user, "classified-sentence-43")
    assert "mention email" in caplog.text
    assert "classified-sentence" not in caplog.text and ADDRESS not in caplog.text
