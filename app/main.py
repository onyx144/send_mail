from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import SessionLocal, get_db, init_db
from app.models.models import InboundMessage, MailAccount, Prospect, SendLog
from app.services.accounts import sync_accounts_from_env
from app.services.parser import parse_manifest, parse_site
from app.services.sender import check_inbox, create_pending_reply, reply_to_pending, send_next
from app.services.telegram import extract_update_message, telegram_api

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('mail-sender')

app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory='app/templates')
_runtime_tasks: list[asyncio.Task] = []
_send_lock = asyncio.Lock()
_inbox_lock = asyncio.Lock()
_parse_lock = asyncio.Lock()


class ParseRequest(BaseModel):
    start_url: str | None = None
    max_pages: int | None = None


class ManifestParseRequest(BaseModel):
    start_url: str = 'https://manifest.in.ua/rt/play/page/3/?order_type=_subscribercount&order=ASC'
    target_saved: int = 20
    max_pages: int = 80


class EmailFinderRequest(BaseModel):
    youtube_url: str | None = None
    prospect_id: int | None = None
    external_links_limit: int = 6


class BulkEmailFinderRequest(BaseModel):
    limit: int = 20
    external_links_limit: int = 6


class ReplyRequest(BaseModel):
    inbound_id: int
    body: str


async def sender_loop() -> None:
    if not settings.auto_sender_enabled:
        log.info('Auto sender disabled')
        return
    log.info('Auto sender enabled; interval=%ss', settings.send_interval_between_emails_seconds)
    while True:
        try:
            await asyncio.sleep(settings.send_interval_between_emails_seconds)
            async with _send_lock:
                async with SessionLocal() as db:
                    log.info('auto send_next: %s', await send_next(db))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Auto sender loop error')
            await asyncio.sleep(60)


async def inbox_loop() -> None:
    if not settings.auto_inbox_enabled:
        log.info('Auto inbox disabled')
        return
    log.info('Auto inbox enabled')
    while True:
        try:
            await asyncio.sleep(300)
            async with _inbox_lock:
                async with SessionLocal() as db:
                    log.info('auto check_inbox: %s', await check_inbox(db))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Auto inbox loop error')
            await asyncio.sleep(60)


async def parse_loop() -> None:
    if not settings.auto_parse_enabled:
        log.info('Auto parser disabled')
        return
    log.info('Auto parser enabled')
    while True:
        try:
            await asyncio.sleep(3600)
            async with _parse_lock:
                async with SessionLocal() as db:
                    log.info('auto parse_site: %s', await parse_site(db))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Auto parser loop error')
            await asyncio.sleep(120)


@app.on_event('startup')
async def startup() -> None:
    await init_db()
    async with SessionLocal() as db:
        await sync_accounts_from_env(db)
    _runtime_tasks.append(asyncio.create_task(sender_loop()))
    _runtime_tasks.append(asyncio.create_task(inbox_loop()))
    _runtime_tasks.append(asyncio.create_task(parse_loop()))


@app.on_event('shutdown')
async def shutdown() -> None:
    for task in _runtime_tasks:
        task.cancel()
    for task in _runtime_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@app.get('/health')
async def health():
    return {'success': True, 'service': settings.app_name}


@app.get('/admin', response_class=HTMLResponse)
async def admin(request: Request, db: AsyncSession = Depends(get_db)):
    counts = {
        'accounts': await db.scalar(select(func.count(MailAccount.id))),
        'prospects': await db.scalar(select(func.count(Prospect.id))),
        'plan_now_unsent': await db.scalar(select(func.count(Prospect.id)).where(Prospect.plans == 'now', Prospect.status == 'new')),
        'sent': await db.scalar(select(func.count(SendLog.id)).where(SendLog.status == 'sent')),
        'lose': await db.scalar(select(func.count(Prospect.id)).where(Prospect.status == 'lose')),
        'inbound': await db.scalar(select(func.count(InboundMessage.id))),
    }
    prospects = (await db.scalars(select(Prospect).order_by(Prospect.id.desc()).limit(30))).all()
    inbound = (await db.scalars(select(InboundMessage).order_by(InboundMessage.id.desc()).limit(20))).all()
    return templates.TemplateResponse('admin.html', {'request': request, 'counts': counts, 'prospects': prospects, 'inbound': inbound, 'settings': settings})


@app.post('/api/sync-accounts')
async def api_sync_accounts(db: AsyncSession = Depends(get_db)):
    return {'success': True, 'accounts_seen': await sync_accounts_from_env(db)}


@app.post('/api/parse')
async def api_parse(body: ParseRequest, db: AsyncSession = Depends(get_db)):
    async with _parse_lock:
        return {'success': True, 'result': await parse_site(db, body.start_url, body.max_pages)}


@app.post('/api/parse-manifest')
async def api_parse_manifest(body: ManifestParseRequest, db: AsyncSession = Depends(get_db)):
    async with _parse_lock:
        return {'success': True, 'result': await parse_manifest(db, body.start_url, body.target_saved, body.max_pages)}


@app.post('/api/find-youtube-email')
async def api_find_youtube_email(body: EmailFinderRequest, db: AsyncSession = Depends(get_db)):
    from app.services.playwright_email_finder import find_emails_with_playwright
    prospect = None
    url = body.youtube_url
    if body.prospect_id:
        prospect = await db.get(Prospect, body.prospect_id)
        if not prospect:
            raise HTTPException(404, 'prospect not found')
        url = prospect.youtube_link
    if not url:
        raise HTTPException(400, 'youtube_url or prospect_id required')
    result = await find_emails_with_playwright(url, external_links_limit=body.external_links_limit)
    if prospect and result.emails:
        prospect.email = result.emails[0]
        prospect.raw_text = ((prospect.raw_text or '') + '\n\nEMAIL_FINDER_CHECKED_URLS:\n' + '\n'.join(result.checked_urls))[:12000]
        db.add(prospect)
        await db.commit()
    return {'success': True, 'result': result.__dict__, 'updated_prospect_id': prospect.id if prospect and result.emails else None}


@app.post('/api/find-youtube-emails-bulk')
async def api_find_youtube_emails_bulk(body: BulkEmailFinderRequest, db: AsyncSession = Depends(get_db)):
    from app.services.playwright_email_finder import find_emails_with_playwright
    prospects = (await db.scalars(
        select(Prospect)
        .where(Prospect.status == 'new', Prospect.email.is_(None), Prospect.youtube_link.like('http%youtube%'))
        .order_by(Prospect.id.desc())
        .limit(max(1, min(body.limit, 100)))
    )).all()
    results = []
    for prospect in prospects:
        result = await find_emails_with_playwright(prospect.youtube_link, external_links_limit=body.external_links_limit)
        if result.emails:
            prospect.email = result.emails[0]
            prospect.raw_text = ((prospect.raw_text or '') + '\n\nEMAIL_FINDER_CHECKED_URLS:\n' + '\n'.join(result.checked_urls))[:12000]
            db.add(prospect)
            await db.commit()
        results.append({'prospect_id': prospect.id, 'nick': prospect.nick, 'emails': result.emails, 'status': result.status, 'checked_urls': result.checked_urls})
    return {'success': True, 'checked': len(results), 'found': sum(1 for r in results if r['emails']), 'results': results}


@app.post('/api/send-next')
async def api_send_next(db: AsyncSession = Depends(get_db)):
    async with _send_lock:
        return await send_next(db)


@app.post('/api/inbox-check')
async def api_inbox_check(db: AsyncSession = Depends(get_db)):
    async with _inbox_lock:
        return await check_inbox(db)


@app.post('/api/reply')
async def api_reply(body: ReplyRequest, db: AsyncSession = Depends(get_db)):
    inbound = await db.get(InboundMessage, body.inbound_id)
    if not inbound:
        raise HTTPException(404, 'inbound not found')
    account = await db.get(MailAccount, inbound.account_id)
    if not account or not inbound.from_email:
        raise HTTPException(400, 'cannot reply: missing account or sender')
    # Reuse telegram pending reply path by creating a synthetic direct reply is intentionally avoided.
    from app.services.mailer import send_email
    subject = inbound.subject or ''
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'
    await send_email(account_email=account.email, to_email=inbound.from_email, subject=subject, body=body.body, reply_to_message_id=inbound.message_id_header)
    return {'success': True, 'sent_to': inbound.from_email, 'from_account': account.email}


@app.get('/api/prospects')
async def api_prospects(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Prospect).order_by(Prospect.id.desc()).limit(200))).all()


@app.get('/api/inbound')
async def api_inbound(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(InboundMessage).order_by(InboundMessage.id.desc()).limit(100))).all()


@app.post('/telegram/webhook')
async def telegram_webhook(update: dict, db: AsyncSession = Depends(get_db)):
    # Inline button: reply:<inbound_id> creates pending reply for this chat.
    if update.get('callback_query'):
        cq = update['callback_query']
        data = cq.get('data') or ''
        msg = cq.get('message') or {}
        chat = msg.get('chat') or {}
        user = cq.get('from') or {}
        if data.startswith('reply:'):
            inbound_id = int(data.split(':', 1)[1])
            await create_pending_reply(db, chat_id=str(chat.get('id')), user_id=str(user.get('id')) if user.get('id') else None, inbound_id=inbound_id)
            await telegram_api('answerCallbackQuery', {'callback_query_id': cq.get('id'), 'text': 'Напишіть відповідь одним повідомленням у цей чат.'})
            await telegram_api('sendMessage', {'chat_id': str(chat.get('id')), 'text': f'Очікую текст відповіді на лист #{inbound_id}.'})
            return {'success': True, 'pending_reply': inbound_id}

    chat_id, user_id, text = extract_update_message(update)
    if chat_id and text:
        result = await reply_to_pending(db, chat_id=chat_id, user_id=user_id, body=text)
        if result.get('success'):
            await telegram_api('sendMessage', {'chat_id': chat_id, 'text': f'Відповідь відправлено: {result.get("sent_to")}'})
            return result
    return {'success': True, 'ignored': True}
