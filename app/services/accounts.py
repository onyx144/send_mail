from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import MailAccount


async def sync_accounts_from_env(db: AsyncSession) -> int:
    """Upsert email accounts from MAIL_ACCOUNTS_JSON without storing passwords in DB."""
    count = 0
    for item in settings.mail_accounts():
        email = str(item.get('email') or '').strip().lower()
        if not email:
            continue
        account = await db.scalar(select(MailAccount).where(MailAccount.email == email))
        if not account:
            account = MailAccount(email=email)
            db.add(account)
        account.display_name = item.get('display_name') or email
        account.enabled = bool(item.get('enabled', True))
        count += 1
    await db.commit()
    return count


def account_password(email: str) -> str | None:
    target = email.strip().lower()
    for item in settings.mail_accounts():
        if str(item.get('email') or '').strip().lower() == target:
            password = str(item.get('password') or '')
            return password if password and password != 'CHANGE_ME' else None
    return None


def account_display_name(email: str) -> str:
    target = email.strip().lower()
    for item in settings.mail_accounts():
        if str(item.get('email') or '').strip().lower() == target:
            return str(item.get('display_name') or email)
    return email
