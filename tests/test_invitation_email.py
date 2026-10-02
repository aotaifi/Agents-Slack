from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from test_api import auth, setup_thread
from test_api import service as api_service
from test_invitations import ANONYMOUS, accept, invite

from agent_commons import invitation_email, main
from agent_commons.models import Invitation, now

service = api_service


@pytest.fixture
def mail_config(monkeypatch):
    for key in ("PILOT_SMTP_USERNAME", "PILOT_SMTP_PASSWORD", "PILOT_PUBLIC_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PILOT_SMTP_HOST", "mail.example.test")
    monkeypatch.setenv("PILOT_SMTP_PORT", "587")
    monkeypatch.setenv("PILOT_SMTP_MODE", "starttls")
    monkeypatch.setenv("PILOT_EMAIL_FROM", "Research Workspace <workspace@example.test>")
    monkeypatch.setenv("PILOT_SSH_HOST", "lab.example.test")


def send(client, pid, invitation, recipient="alex@example.test", headers=None):
    return client.post(
        f"/v1/projects/{pid}/invitations/{invitation['id']}/email",
        json={"code": invitation["code"], "to": recipient},
        headers=headers,
    )


def test_owner_email_has_connection_and_agent_steps_without_consuming_invitation(
    service, mail_config, monkeypatch
):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    invitation = invite(client, pid, "owner")
    submitted = []
    monkeypatch.setattr(main, "submit_invitation", submitted.append)
    assert client.get("/v1/connection").json()["email_enabled"] is True
    response = send(client, pid, invitation)
    assert response.status_code == 202 and response.json() == {"status": "submitted"}
    message = submitted[0]
    body = message.get_content()
    assert str(message["To"]) == "alex@example.test"
    assert "Physics" in str(message["Subject"])
    assert f"http://127.0.0.1:8002/#invite={invitation['code']}" in body
    assert "YOUR_UNIVERSITY_USERNAME@lab.example.test" in body
    assert "127.0.0.1:8002:127.0.0.1:18000" in body
    assert "Accept invitation" in body and "a password" in body
    assert "Keep me signed in" in body and "display name" in body
    assert "Add participant" in body
    assert invitation["code"] not in response.text
    with app.state.session_factory() as db:
        saved = db.get(Invitation, invitation["id"])
        assert saved.used_at is None and "alex@example.test" not in str(saved.__dict__)
    assert "alex@example.test" not in client.get(f"/v1/projects/{pid}/events").text
    assert accept(client, invitation).status_code == 201
    assert send(client, pid, invitation).status_code == 410
    assert len(submitted) == 1


def test_email_permission_code_lifecycle_and_shared_rate_limit(service, mail_config, monkeypatch):
    client, app, _, _ = service
    pid, _ = setup_thread(client)
    pending = invite(client, pid)
    guest = accept(client, invite(client, pid), name="Guest").json()
    agent = client.post("/v1/actors", json={"name": "Bot", "kind": "agent"}).json()
    client.post(f"/v1/projects/{pid}/members", json={"actor_id": agent["actor"]["id"]})
    submitted = []
    monkeypatch.setattr(main, "submit_invitation", submitted.append)
    assert send(client, pid, pending, headers=ANONYMOUS).status_code == 401
    assert send(client, pid, pending, headers=auth(guest["token"])).status_code == 403
    assert send(client, pid, pending, headers=auth(agent["token"])).status_code == 403
    wrong = {**pending, "code": "x" * 43}
    assert send(client, pid, wrong).status_code == 404
    other_pid, _ = setup_thread(client)
    assert send(client, other_pid, pending).status_code == 404
    expired = invite(client, pid)
    with app.state.session_factory() as db:
        db.get(Invitation, expired["id"]).expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert send(client, pid, expired).status_code == 410
    withdrawn = invite(client, pid)
    client.delete(f"/v1/projects/{pid}/invitations/{withdrawn['id']}")
    assert send(client, pid, withdrawn).status_code == 410
    assert not submitted
    for _ in range(5):
        assert send(client, pid, pending).status_code == 202
    limited = send(client, pid, pending)
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    assert len(submitted) == 5
    with app.state.session_factory() as db:
        assert db.scalar(select(Invitation).where(Invitation.id == pending["id"])).used_at is None


def test_disabled_or_failed_mail_preserves_link_and_does_not_leak_transport_error(
    service, mail_config, monkeypatch
):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    pending = invite(client, pid)
    monkeypatch.delenv("PILOT_SMTP_HOST")
    assert client.get("/v1/connection").json()["email_enabled"] is False
    assert send(client, pid, pending).status_code == 503
    monkeypatch.setenv("PILOT_SMTP_HOST", "mail.example.test")

    def failure(message):
        raise invitation_email.MailUnavailable("secret transport details")

    monkeypatch.setattr(main, "submit_invitation", failure)
    response = send(client, pid, pending)
    assert response.status_code == 502
    assert "secret transport details" not in response.text and "confirmed" in response.text
    assert accept(client, pending).status_code == 201


def test_email_rejects_header_injection_and_recipient_lists(service, mail_config, monkeypatch):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    pending = invite(client, pid)
    submitted = []
    monkeypatch.setattr(main, "submit_invitation", submitted.append)
    for address in (
        "alex@example.test\r\nBcc: other@example.test",
        "alex@example.test,other@example.test",
        "Alex <alex@example.test>",
        "a..b@example.test",
        "a@" + "x" * 64 + ".test",
    ):
        assert send(client, pid, pending, recipient=address).status_code == 422
    assert not submitted


def test_uncertain_smtp_attempts_are_still_rate_limited(service, mail_config, monkeypatch):
    client, _, _, _ = service
    pid, _ = setup_thread(client)
    pending = invite(client, pid)
    attempted = []

    def uncertain(message):
        attempted.append(message)
        raise invitation_email.MailUnavailable("Lost acknowledgement after DATA")

    monkeypatch.setattr(main, "submit_invitation", uncertain)
    for _ in range(5):
        assert send(client, pid, pending).status_code == 502
    assert send(client, pid, pending).status_code == 429
    assert len(attempted) == 5
    assert accept(client, pending).status_code == 201


@pytest.mark.parametrize("mode", ["starttls", "ssl"])
def test_smtp_uses_verified_tls_and_never_retries_after_acceptance(mail_config, monkeypatch, mode):
    monkeypatch.setenv("PILOT_SMTP_MODE", mode)
    smtp = MagicMock()
    smtp.send_message.return_value = {}
    constructor = MagicMock(return_value=smtp)
    monkeypatch.setattr(
        invitation_email.smtplib, "SMTP_SSL" if mode == "ssl" else "SMTP", constructor
    )
    message = invitation_email.invitation_message(
        "Physics\r\nBcc: someone", "guest", now(), "c" * 43, "alex@example.test"
    )
    assert "\n" not in str(message["Subject"])
    assert "Ask a project owner" in message.get_content()
    invitation_email.submit_invitation(message)
    constructor.assert_called_once()
    if mode == "ssl":
        context = constructor.call_args.kwargs["context"]
    else:
        context = smtp.starttls.call_args.kwargs["context"]
    assert context.check_hostname and context.verify_mode == invitation_email.ssl.CERT_REQUIRED
    smtp.send_message.assert_called_once_with(message)
    smtp.close.assert_called_once()
    smtp.login.assert_not_called()


def test_public_url_mail_and_invalid_configuration(mail_config, monkeypatch):
    monkeypatch.setenv("PILOT_PUBLIC_URL", "https://workspace.example.test")
    message = invitation_email.invitation_message(
        "Physics", "guest", now(), "c" * 43, "alex@example.test"
    )
    assert "https://workspace.example.test/#invite=" in message.get_content()
    assert "ssh -N" not in message.get_content()
    monkeypatch.setenv("PILOT_PUBLIC_URL", "https://user:password@workspace.example.test/#bad")
    with pytest.raises(invitation_email.MailUnavailable):
        invitation_email.invitation_message(
            "Physics", "guest", now(), "c" * 43, "alex@example.test"
        )
    monkeypatch.setenv("PILOT_SMTP_MODE", "plaintext")
    with pytest.raises(invitation_email.MailUnavailable):
        invitation_email.submit_invitation(message)
