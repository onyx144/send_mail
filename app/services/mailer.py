from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.core.config import settings
from app.services.accounts import account_display_name, account_password


def _send_email_sync(
    *,
    account_email: str,
    to_email: str,
    subject: str,
    body: str,
    reply_to_message_id: str | None = None,
) -> str:
    password = account_password(account_email)
    if not password:
        raise RuntimeError(f'Password for {account_email} is not configured in MAIL_ACCOUNTS_JSON')

    msg = EmailMessage()
    msg['From'] = formataddr((account_display_name(account_email), account_email))
    msg['To'] = to_email
    msg['Subject'] = subject
    msg['Message-ID'] = make_msgid(domain=account_email.split('@')[-1])
    if reply_to_message_id:
        msg['In-Reply-To'] = reply_to_message_id
        msg['References'] = reply_to_message_id
    msg.set_content(body)

    if settings.mail_smtp_ssl:
        with smtplib.SMTP_SSL(settings.mail_smtp_host, settings.mail_smtp_port, timeout=45) as smtp:
            smtp.login(account_email, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(settings.mail_smtp_host, settings.mail_smtp_port, timeout=45) as smtp:
            smtp.starttls()
            smtp.login(account_email, password)
            smtp.send_message(msg)
    return str(msg['Message-ID'])


async def send_email(
    *,
    account_email: str,
    to_email: str,
    subject: str,
    body: str,
    reply_to_message_id: str | None = None,
) -> str:
    return await asyncio.to_thread(
        _send_email_sync,
        account_email=account_email,
        to_email=to_email,
        subject=subject,
        body=body,
        reply_to_message_id=reply_to_message_id,
    )
