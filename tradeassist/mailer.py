"""Sending the morning plan and trigger alerts by email (2.8). Standard library only.

SMTP with STARTTLS, which Gmail accepts with an **app password** (myaccount.google.com/apppasswords;
2-Step Verification must be on; Google revokes app passwords when the account password changes). Any
other SMTP provider works the same way through SMTP_HOST and SMTP_PORT. The password stays in .env on
this computer; it is never logged, and a failure's message is cleaned before it is shown.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any

from .events import redact

log = logging.getLogger(__name__)

TIMEOUT = 30


class MailError(RuntimeError):
    pass


def configured(settings: Any) -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def to_address(settings: Any) -> str:
    return settings.email_to or settings.smtp_user


def _scrub(exc: BaseException, settings: Any) -> str:
    text = redact(f"{type(exc).__name__}: {exc}")
    if settings.smtp_password:
        text = text.replace(settings.smtp_password, "[the SMTP password]")
    return text[:300]


def send(settings: Any, subject: str, text: str, html: str | None = None) -> str:
    """Send one message; returns the address it went to. Raises MailError with a cleaned message."""
    if not configured(settings):
        raise MailError("Email is not set up: put SMTP_USER and SMTP_PASSWORD in .env (with Gmail, an app "
                        "password from myaccount.google.com/apppasswords; 2-Step Verification must be on).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = to_address(settings)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT) as server:
            # The default STARTTLS context does not verify the server's certificate; this one does, so the
            # password goes only to the real SMTP host, never to whoever answers on a hostile network.
            server.starttls(context=ssl.create_default_context())
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("The mail server refused the sign-in. With Gmail this usually means the app password "
                        "is wrong or was revoked (changing the Google password revokes them): make a new one "
                        f"at myaccount.google.com/apppasswords. The server said: {_scrub(exc, settings)}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"The email could not be sent: {_scrub(exc, settings)}") from exc
    return to_address(settings)


__all__ = ["send", "configured", "to_address", "MailError"]
