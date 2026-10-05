"""Shared plumbing for operator-configured, TLS-verified SMTP mail."""

import os
import re
import smtplib
import ssl
from urllib.parse import urlsplit


class MailUnavailable(Exception):
    pass


def email_enabled():
    return bool(os.environ.get("PILOT_SMTP_HOST") and os.environ.get("PILOT_EMAIL_FROM"))


def workspace_access(what):
    """Return (intro, base_url, next_step_number) for opening a workspace link.

    `what` names the thing to open, such as "this invitation".
    """
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
        return f"1. Open {what} in your browser:\n", public_url + "/", 2
    host = os.environ.get("PILOT_SSH_HOST", "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
        raise MailUnavailable("Workstation connection instructions unavailable")
    port = int(os.environ.get("PILOT_SSH_APP_PORT", "18000"))
    if not 1 <= port <= 65535:
        raise MailUnavailable("Invalid workstation port")
    intro = (
        "1. Open Terminal on your Mac or computer. Replace YOUR_UNIVERSITY_USERNAME "
        "with your university username and run:\n\n"
        "ssh -N -o ExitOnForwardFailure=yes "
        f"-L 127.0.0.1:8002:127.0.0.1:{port} YOUR_UNIVERSITY_USERNAME@{host}\n\n"
        "Leave Terminal open. If it stays quiet, that's normal. You need existing SSH access; "
        f"{what} does not provide a university account.\n\n2. Open {what} in Safari, Chrome, "
        "or another browser:\n"
    )
    return intro, "http://127.0.0.1:8002/", 3


def submit_message(message):
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
