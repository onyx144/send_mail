from __future__ import annotations

import asyncio
import imaplib
import email
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from dataclasses import dataclass

from app.core.config import settings
from app.services.accounts import account_password


@dataclass
class ParsedInbound:
    uid: str
    from_email: str | None
    from_name: str | None
    subject: str | None
    body_text: str
    message_id_header: str | None
    in_reply_to: str | None


def _decode_header_value(value: str | None) -> str | None:
    if not value:
        return None
    parts = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or 'utf-8', errors='replace'))
        else:
            parts.append(chunk)
    return ''.join(parts)


def _extract_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition') or '').lower()
            if ctype == 'text/plain' and 'attachment' not in disp:
                payload = part.get_payload(decode=True) or b''
                return payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                payload = part.get_payload(decode=True) or b''
                return payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
        return ''
    payload = msg.get_payload(decode=True) or b''
    return payload.decode(msg.get_content_charset() or 'utf-8', errors='replace')


def _fetch_recent_imap_sync(account_email: str, limit: int = 10) -> list[ParsedInbound]:
    password = account_password(account_email)
    if not password:
        raise RuntimeError(f'Password for {account_email} is not configured in MAIL_ACCOUNTS_JSON')

    cls = imaplib.IMAP4_SSL if settings.mail_imap_ssl else imaplib.IMAP4
    with cls(settings.mail_imap_host, settings.mail_imap_port) as imap:
        imap.login(account_email, password)
        imap.select('INBOX')
        typ, data = imap.uid('search', None, 'ALL')
        if typ != 'OK':
            return []
        uids = (data[0] or b'').split()[-limit:]
        result: list[ParsedInbound] = []
        for uid_b in uids:
            uid = uid_b.decode('ascii', errors='ignore')
            typ, msg_data = imap.uid('fetch', uid, '(RFC822)')
            if typ != 'OK' or not msg_data:
                continue
            raw = None
            for item in msg_data:
                if isinstance(item, tuple):
                    raw = item[1]
                    break
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            from_name, from_addr = parseaddr(_decode_header_value(msg.get('From')) or '')
            result.append(
                ParsedInbound(
                    uid=uid,
                    from_email=from_addr or None,
                    from_name=from_name or None,
                    subject=_decode_header_value(msg.get('Subject')),
                    body_text=_extract_text(msg)[:12000],
                    message_id_header=msg.get('Message-ID'),
                    in_reply_to=msg.get('In-Reply-To'),
                )
            )
        return result


async def fetch_recent_imap(account_email: str, limit: int = 10) -> list[ParsedInbound]:
    return await asyncio.to_thread(_fetch_recent_imap_sync, account_email, limit)
