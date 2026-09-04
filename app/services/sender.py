from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import InboundMessage, MailAccount, Prospect, SendLog, TelegramPendingReply
from app.services.inbox import fetch_recent_imap
from app.services.mailer import send_email
from app.services.telegram import notify_inbound_message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_outbound_body() -> str:
    path = Path(settings.outbound_body_file)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise RuntimeError(f'Outbound body file not found: {path}')
    text = path.read_text(encoding='utf-8').strip()
    if not text:
        raise RuntimeError(f'Outbound body file is empty: {path}')
    return text


def render_body(template: str, prospect: Prospect) -> str:
    return template.format(
        nick=prospect.nick or '',
        youtube_link=prospect.youtube_link,
        email=prospect.email or '',
        subscribers=prospect.subscribers or 0,
        telegram=prospect.telegram or '',
        instagram=prospect.instagram or '',
    )


async def pick_next_send(db: AsyncSession) -> tuple[MailAccount, Prospect] | None:
    now = utcnow()
    account_rows = (await db.scalars(select(MailAccount).where(MailAccount.enabled == True).order_by(MailAccount.last_sent_at.asc().nullsfirst()))).all()  # noqa: E712
    if not account_rows:
        return None

    for account in account_rows:
        if account.last_sent_at and account.last_sent_at > now - timedelta(seconds=settings.send_interval_per_account_seconds):
            continue
        # pick first plan=now, unsent, has email, not already sent by this account
        prospects = (await db.scalars(
            select(Prospect)
            .where(and_(Prospect.plans == 'now', Prospect.status == 'new', Prospect.email.is_not(None)))
            .order_by(Prospect.created_at.asc())
            .limit(50)
        )).all()
        for prospect in prospects:
            sent_before = await db.scalar(select(SendLog).where(and_(SendLog.account_id == account.id, SendLog.prospect_id == prospect.id)))
            if not sent_before:
                return account, prospect
    return None


async def send_next(db: AsyncSession) -> dict:
    picked = await pick_next_send(db)
    if not picked:
        return {'success': True, 'sent': False, 'reason': 'no eligible account/prospect'}
    account, prospect = picked
    if not prospect.email:
        return {'success': True, 'sent': False, 'reason': 'prospect has no email'}
    if not settings.outbound_subject:
        raise RuntimeError('OUTBOUND_SUBJECT is empty. Provide first message subject later.')
    template = load_outbound_body()
    body = render_body(template, prospect)

    try:
        await send_email(account_email=account.email, to_email=prospect.email, subject=settings.outbound_subject, body=body)
        prospect.status = 'sent'
        account.last_sent_at = utcnow()
        db.add(SendLog(account_id=account.id, prospect_id=prospect.id, recipient_email=prospect.email, subject=settings.outbound_subject, status='sent'))
        await db.commit()
        return {'success': True, 'sent': True, 'account': account.email, 'prospect_id': prospect.id, 'to': prospect.email}
    except Exception as exc:
        db.add(SendLog(account_id=account.id, prospect_id=prospect.id, recipient_email=prospect.email, subject=settings.outbound_subject or '', status='error', error=str(exc)))
        await db.commit()
        raise


async def check_inbox(db: AsyncSession, limit_per_account: int = 10) -> dict:
    accounts = (await db.scalars(select(MailAccount).where(MailAccount.enabled == True))).all()  # noqa: E712
    created = 0
    notified = 0
    for account in accounts:
        messages = await fetch_recent_imap(account.email, limit=limit_per_account)
        for msg in messages:
            exists = await db.scalar(select(InboundMessage).where(and_(InboundMessage.account_id == account.id, InboundMessage.message_uid == msg.uid)))
            if exists:
                continue
            inbound = InboundMessage(
                account_id=account.id,
                message_uid=msg.uid,
                from_email=msg.from_email,
                from_name=msg.from_name,
                subject=msg.subject,
                body_text=msg.body_text,
                message_id_header=msg.message_id_header,
                in_reply_to=msg.in_reply_to,
            )
            db.add(inbound)
            await db.commit()
            await db.refresh(inbound)
            created += 1
            sent = await notify_inbound_message(inbound_id=inbound.id, from_email=inbound.from_email, subject=inbound.subject, body=inbound.body_text)
            if sent:
                inbound.telegram_notified = True
                notified += sent
                await db.commit()
    return {'success': True, 'created': created, 'telegram_notifications': notified}


async def create_pending_reply(db: AsyncSession, *, chat_id: str, user_id: str | None, inbound_id: int) -> None:
    # deactivate old pending replies for same chat/user
    rows = (await db.scalars(select(TelegramPendingReply).where(and_(TelegramPendingReply.telegram_chat_id == chat_id, TelegramPendingReply.active == True)))).all()  # noqa: E712
    for row in rows:
        row.active = False
    db.add(TelegramPendingReply(telegram_chat_id=chat_id, telegram_user_id=user_id, inbound_message_id=inbound_id, active=True))
    await db.commit()


async def reply_to_pending(db: AsyncSession, *, chat_id: str, user_id: str | None, body: str) -> dict:
    pending = await db.scalar(
        select(TelegramPendingReply)
        .where(and_(TelegramPendingReply.telegram_chat_id == chat_id, TelegramPendingReply.active == True))  # noqa: E712
        .order_by(TelegramPendingReply.created_at.desc())
    )
    if not pending:
        return {'success': False, 'reason': 'no pending reply'}
    inbound = await db.get(InboundMessage, pending.inbound_message_id)
    if not inbound or not inbound.from_email:
        pending.active = False
        await db.commit()
        return {'success': False, 'reason': 'inbound message has no sender email'}
    account = await db.get(MailAccount, inbound.account_id)
    if not account:
        return {'success': False, 'reason': 'mail account not found'}
    subject = inbound.subject or ''
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'
    await send_email(
        account_email=account.email,
        to_email=inbound.from_email,
        subject=subject,
        body=body,
        reply_to_message_id=inbound.message_id_header,
    )
    pending.active = False
    await db.commit()
    return {'success': True, 'sent_to': inbound.from_email, 'from_account': account.email}
