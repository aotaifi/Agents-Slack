"""Submit project invitations to an operator-configured, TLS-verified SMTP relay."""

import os
import smtplib  # noqa: F401  (tests patch the transport through this module)
import ssl  # noqa: F401
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from urllib.parse import urlencode

from .mail import MailUnavailable, email_enabled, submit_message, workspace_access

__all__ = ["MailUnavailable", "email_enabled", "invitation_message", "submit_invitation"]
submit_invitation = submit_message


def invitation_message(
    project_name, role, expires_at, code, recipient, *, inviter_name="Research Workspace"
):
    intro, base_url, step = workspace_access("this invitation")
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
