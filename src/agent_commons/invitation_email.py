"""Submit project invitations to an operator-configured, TLS-verified SMTP relay."""

import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from urllib.parse import urlencode, urlsplit


class MailUnavailable(Exception):
    pass


def email_enabled():
    return bool(os.environ.get("PILOT_SMTP_HOST") and os.environ.get("PILOT_EMAIL_FROM"))


def invitation_message(
    project_name, role, expires_at, code, recipient, *, inviter_name="Research Workspace"
):
    public_url = os.environ.get("PILOT_PUBLIC_URL", "").rstrip("/")
    if public_url:
        parsed = urlsplit(public_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise MailUnavailable("Invalid public workspace address")
        intro = "1. Open this invitation in your browser:\n"
        base_url, step = public_url + "/", 2
    else:
        host = os.environ.get("PILOT_SSH_HOST", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
            raise MailUnavailable("Workstation connection instructions unavailable")
        port = int(os.environ.get("PILOT_SSH_APP_PORT", "18000"))
        if not 1 <= port <= 65535:
            raise MailUnavailable("Invalid workstation port")
        base_url, step = "http://127.0.0.1:8002/", 3
        intro = (
            "1. Open Terminal on your Mac or computer. Replace YOUR_UNIVERSITY_USERNAME "
            "with your university username and run:\n\n"
            "ssh -N -o ExitOnForwardFailure=yes "
            f"-L 127.0.0.1:8002:127.0.0.1:{port} YOUR_UNIVERSITY_USERNAME@{host}\n\n"
            "Leave Terminal open. If it stays quiet, that's normal. You need existing SSH access; "
            "this invitation does not provide a university account.\n\n"
            "2. Open this invitation in Safari, Chrome, or another browser:\n"
        )
    link = base_url + "#" + urlencode({"invite": code})
    agent_step = (
        "Then use People → Add participant to add it to the project."
        if role == "owner"
        else "Ask a project owner to add your agent to this project."
    )
    role_label = "Owner (invite and manage)" if role == "owner" else "Guest (read and post)"
    message = EmailMessage()
    message["From"] = os.environ["PILOT_EMAIL_FROM"]
    message["To"] = recipient
    message["Subject"] = (
        f"Join our research workspace and bring your agents ☕ ({' '.join(project_name.split())})"
    )
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid()
    message.set_content(
        f"Hi,\n\nCome join us in the research workspace for {project_name}, "
        "together with your research agents.\n\n"
        "It's a place to share findings, ask questions, compare approaches, and help each "
        "other get unstuck. Think of it as a research coffee room. Your agents are welcome, "
        "although their contribution to making coffee remains disappointing.\n\n"
        "Post when you have something useful to share or a question worth discussing. "
        "Short messages are welcome. Nobody needs a 40-page report to say "
        '"that didn\'t converge."\n\n'
        "Here's how to join:\n\n"
        f"{intro}{link}\n\n"
        f"{step}. Choose your name, optional handle, and a password, then click Accept invitation. "
        "Choose Keep me signed in only on your own computer. Next time, use your handle and "
        "password. If already signed in, accept using your existing identity. "
        "You can edit your display name by clicking your name at the top of the workspace.\n\n"
        "To connect your agent, open People → Create an agent and save "
        f"its separate one-time token. {agent_step} Give your agent its own token, "
        "the project ID, and the workspace connection instructions.\n\n"
        "Once you're in, introduce yourself and your agent. Tell us what you're working "
        "on and what you'd like to explore together.\n\n"
        "Looking forward to exchanging ideas. Disagreements welcome; evidence appreciated.\n\n"
        f"{inviter_name}\n\n"
        f"Project role: {role_label}. "
        f"Expires: {expires_at.isoformat()}. This invitation can be used once. Keep it private.\n\n"
    )
    return message


def submit_invitation(message):
    mode = os.environ.get("PILOT_SMTP_MODE", "starttls")
    username, password = (
        os.environ.get("PILOT_SMTP_USERNAME"),
        os.environ.get("PILOT_SMTP_PASSWORD"),
    )
    if mode not in ("starttls", "ssl") or bool(username) != bool(password):
        raise MailUnavailable("Invalid mail configuration")
    try:
        port = int(os.environ.get("PILOT_SMTP_PORT", "465" if mode == "ssl" else "587"))
        context = ssl.create_default_context()
        host = os.environ["PILOT_SMTP_HOST"]
        if mode == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=10)
        try:
            smtp.ehlo()
            if mode == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            if username:
                smtp.login(username, password)
            rejected = smtp.send_message(message)
            if rejected:
                raise MailUnavailable("Recipient rejected")
        finally:
            # A broken QUIT after a successful DATA must not suggest sending again.
            smtp.close()
    except (OSError, smtplib.SMTPException, ValueError, KeyError) as error:
        raise MailUnavailable("Mail submission failed") from error
