from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import SessionLocal, get_db, init_db
from app.models.models import InboundMessage, MailAccount, Prospect, SendLog, TelegramBotUser
from app.services.accounts import sync_accounts_from_env
from app.services.parser import parse_manifest, parse_site, save_manual_youtube_submission
from app.services.sender import check_inbox, create_pending_reply, reply_to_pending, send_next
from app.services.telegram import extract_update_message, parse_youtube_submission_message, telegram_api

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


class ProspectEmailSaveRequest(BaseModel):
    email: str


EMAIL_RE = re.compile(r'^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$', re.I)
YOUTUBE_URL_RE = re.compile(r'https?://[^\s<>"\']*(?:youtube\.com|youtu\.be)[^\s<>"\']*', re.I)


def parse_manual_youtube_message(text: str) -> tuple[str, str] | None:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    email_value = lines[0].lower()
    url_value = lines[1]
    if not EMAIL_RE.match(email_value):
        return None
    match = YOUTUBE_URL_RE.search(url_value)
    if not match:
        return None
    return email_value, match.group(0).rstrip('.,;)')


def manual_youtube_saved_message(prospect: Prospect) -> str:
    parts = [
        '✅ Ютубер сохранён',
        f'ID: {prospect.id}',
        f'Почта: {prospect.email or "-"}',
        f'Канал: {prospect.youtube_link}',
        f'Название: {prospect.nick or "-"}',
        f'Подписчики: {prospect.subscribers if prospect.subscribers is not None else "-"}',
        f'Последнее видео: {prospect.last_video_at.isoformat() if prospect.last_video_at else "-"}',
    ]
    return '\n'.join(parts)


def public_url(path: str) -> str:
    base = (settings.public_base_url or '').rstrip('/')
    if not base:
        base = 'http://127.0.0.1:8094'
    return f'{base}{path}'


async def get_youtubers_without_email(db: AsyncSession, *, offset: int = 0, limit: int = 10) -> list[Prospect]:
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 10), 10))
    return (await db.scalars(
        select(Prospect)
        .where(
            Prospect.youtube_link.like('http%'),
            (Prospect.email.is_(None)) | (Prospect.email == ''),
        )
        .order_by(Prospect.id.asc())
        .offset(safe_offset)
        .limit(safe_limit)
    )).all()


async def upsert_telegram_bot_user(db: AsyncSession, *, chat_id: str, user: dict) -> TelegramBotUser:
    user_id = str(user.get('id') or '')
    if not user_id:
        raise HTTPException(400, 'telegram user id missing')
    row = await db.scalar(select(TelegramBotUser).where(TelegramBotUser.telegram_user_id == user_id))
    if not row:
        row = TelegramBotUser(telegram_user_id=user_id, chat_id=str(chat_id), last_offset=0)
    row.chat_id = str(chat_id)
    row.username = user.get('username')
    row.first_name = user.get('first_name')
    row.last_name = user.get('last_name')
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def prospect_button_markup(prospect_id: int) -> dict:
    url = public_url(f'/prospect-email/{prospect_id}')
    return {'inline_keyboard': [[{'text': 'Додати пошту', 'url': url}]]}


async def send_youtuber_batch(db: AsyncSession, *, chat_id: str, offset: int = 0) -> int:
    prospects = await get_youtubers_without_email(db, offset=offset, limit=10)
    if not prospects:
        await telegram_api('sendMessage', {'chat_id': chat_id, 'text': 'Ютуберів без пошти більше не знайдено.'})
        return 0
    for item in prospects:
        title = item.nick or f'YouTuber #{item.id}'
        text = f'{title}\nID: {item.id}\n{item.youtube_link}'
        await telegram_api('sendMessage', {
            'chat_id': chat_id,
            'text': text,
            'reply_markup': prospect_button_markup(item.id),
            'disable_web_page_preview': True,
        })
    await telegram_api('sendMessage', {
        'chat_id': chat_id,
        'text': 'Показати наступні 10?',
        'reply_markup': {'inline_keyboard': [[{'text': 'next', 'callback_data': 'yt_next'}]]},
    })
    return len(prospects)


async def handle_youtube_start(db: AsyncSession, *, chat_id: str, user: dict) -> dict:
    row = await upsert_telegram_bot_user(db, chat_id=chat_id, user=user)
    row.last_offset = 0
    db.add(row)
    await db.commit()
    sent = await send_youtuber_batch(db, chat_id=chat_id, offset=0)
    return {'success': True, 'registered_user_id': row.telegram_user_id, 'sent': sent}


async def handle_youtube_next(db: AsyncSession, *, chat_id: str, user: dict) -> dict:
    row = await upsert_telegram_bot_user(db, chat_id=chat_id, user=user)
    row.last_offset = int(row.last_offset or 0) + 10
    db.add(row)
    await db.commit()
    sent = await send_youtuber_batch(db, chat_id=chat_id, offset=row.last_offset)
    return {'success': True, 'offset': row.last_offset, 'sent': sent}


async def prospect_email_payload(prospect_id: int, db: AsyncSession) -> dict:
    prospect = await db.get(Prospect, prospect_id)
    if not prospect:
        raise HTTPException(404, 'prospect not found')
    email_exists = bool((prospect.email or '').strip())
    return {
        'success': True,
        'id': prospect.id,
        'nick': prospect.nick,
        'youtube_link': prospect.youtube_link,
        'email': prospect.email,
        'email_exists': email_exists,
        'message': 'почта есть' if email_exists else 'email missing',
    }


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


@app.get('/api/prospect-email/{prospect_id}')
async def api_get_prospect_email(prospect_id: int, db: AsyncSession = Depends(get_db)):
    return await prospect_email_payload(prospect_id, db)


@app.post('/api/prospect-email/{prospect_id}')
async def api_save_prospect_email(prospect_id: int, body: ProspectEmailSaveRequest, db: AsyncSession = Depends(get_db)):
    email_value = (body.email or '').strip().lower()
    if not email_value or '@' not in email_value or len(email_value) > 255:
        raise HTTPException(400, 'valid email required')
    prospect = await db.get(Prospect, prospect_id)
    if not prospect:
        raise HTTPException(404, 'prospect not found')
    prospect.email = email_value
    db.add(prospect)
    await db.commit()
    return await prospect_email_payload(prospect_id, db)


@app.get('/prospect-email/{prospect_id}', response_class=HTMLResponse)
async def prospect_email_page(prospect_id: int):
    escaped_id = html.escape(str(prospect_id))
    return HTMLResponse(f'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube email #{escaped_id}</title>
  <style>
    body {{ margin:0; font-family:Arial,sans-serif; background:#f6f7fb; color:#171923; }}
    .wrap {{ max-width:560px; margin:0 auto; padding:24px; }}
    .card {{ background:white; border-radius:18px; padding:22px; box-shadow:0 12px 35px rgba(20,25,40,.12); }}
    h1 {{ font-size:22px; margin:0 0 12px; }}
    a {{ color:#2563eb; word-break:break-all; }}
    input {{ width:100%; box-sizing:border-box; padding:13px 14px; border:1px solid #cbd5e1; border-radius:12px; font-size:16px; margin:16px 0 10px; }}
    button {{ width:100%; border:0; border-radius:12px; padding:13px 14px; background:#111827; color:white; font-weight:700; font-size:16px; }}
    .ok {{ color:#15803d; font-weight:800; margin-top:16px; }}
    .err {{ color:#b91c1c; font-weight:700; margin-top:12px; }}
    .muted {{ color:#64748b; font-size:14px; }}
  </style>
</head>
<body>
  <div class="wrap"><div class="card" id="app">Загрузка...</div></div>
<script>
const id = {int(prospect_id)};
const root = document.getElementById('app');
async function load() {{
  const res = await fetch(`/api/prospect-email/${{id}}`);
  const data = await res.json();
  if (!res.ok || !data.success) throw new Error(data.detail || 'Ошибка загрузки');
  render(data);
}}
function render(data) {{
  const nick = data.nick || `YouTuber #${{data.id}}`;
  if (data.email_exists) {{
    root.innerHTML = `<h1>${{escapeHtml(nick)}}</h1><p><a href="${{data.youtube_link}}" target="_blank">${{data.youtube_link}}</a></p><div class="ok">почта есть</div><p class="muted">${{escapeHtml(data.email || '')}}</p>`;
    return;
  }}
  root.innerHTML = `<h1>${{escapeHtml(nick)}}</h1><p><a href="${{data.youtube_link}}" target="_blank">${{data.youtube_link}}</a></p><input id="email" type="email" placeholder="Введите почту"><button onclick="saveEmail()">Сохранить</button><div id="msg"></div>`;
}}
async function saveEmail() {{
  const email = document.getElementById('email').value.trim();
  const msg = document.getElementById('msg');
  msg.textContent = 'Сохраняю...';
  const res = await fetch(`/api/prospect-email/${{id}}`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{email}})}});
  const data = await res.json();
  if (!res.ok || !data.success) {{ msg.className='err'; msg.textContent = data.detail || 'Ошибка сохранения'; return; }}
  render(data);
}}
function escapeHtml(s) {{ return String(s || '').replace(/[&<>'"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[c])); }}
load().catch(err => {{ root.innerHTML = `<div class="err">${{escapeHtml(err.message)}}</div>`; }});
</script>
</body>
</html>''')


@app.post('/telegram/webhook')
async def telegram_webhook(update: dict, db: AsyncSession = Depends(get_db)):
    # Inline button: reply:<inbound_id> creates pending reply for this chat.
    if update.get('callback_query'):
        cq = update['callback_query']
        data = cq.get('data') or ''
        msg = cq.get('message') or {}
        chat = msg.get('chat') or {}
        user = cq.get('from') or {}
        if data == 'yt_next':
            await telegram_api('answerCallbackQuery', {'callback_query_id': cq.get('id'), 'text': 'Старый список отключён'})
            await telegram_api('sendMessage', {'chat_id': str(chat.get('id')), 'text': 'Теперь отправь 2 строки:\n1) email ютубера\n2) ссылку на YouTube-канал'})
            return {'success': True, 'obsolete_callback': data}
        if data.startswith('reply:'):
            inbound_id = int(data.split(':', 1)[1])
            await create_pending_reply(db, chat_id=str(chat.get('id')), user_id=str(user.get('id')) if user.get('id') else None, inbound_id=inbound_id)
            await telegram_api('answerCallbackQuery', {'callback_query_id': cq.get('id'), 'text': 'Напишіть відповідь одним повідомленням у цей чат.'})
            await telegram_api('sendMessage', {'chat_id': str(chat.get('id')), 'text': f'Очікую текст відповіді на лист #{inbound_id}.'})
            return {'success': True, 'pending_reply': inbound_id}

    chat_id, user_id, text = extract_update_message(update)
    if chat_id and text:
        normalized_text = (text or '').strip().lower()
        if normalized_text.startswith('/start'):
            await telegram_api('sendMessage', {'chat_id': chat_id, 'text': 'Отправь одним сообщением ровно 2 строки:\n1) почта ютубера\n2) ссылка на YouTube-канал\n\nПример:\ncreator@example.com\nhttps://www.youtube.com/@channel'})
            msg = update.get('message') or update.get('edited_message') or {}
            user = msg.get('from') or {'id': user_id}
            await upsert_telegram_bot_user(db, chat_id=chat_id, user=user)
            return {'success': True, 'mode': 'manual_youtube_submission'}

        parsed = parse_manual_youtube_message(text)
        if parsed:
            email_value, youtube_url = parsed
            try:
                prospect = await save_manual_youtube_submission(db, email=email_value, youtube_link=youtube_url)
            except Exception as exc:
                log.exception('manual youtube submission failed')
                await telegram_api('sendMessage', {'chat_id': chat_id, 'text': f'❌ Не смог распарсить/сохранить канал: {exc}'})
                return {'success': False, 'error': str(exc)}
            await telegram_api('sendMessage', {'chat_id': chat_id, 'text': manual_youtube_saved_message(prospect), 'disable_web_page_preview': True})
            return {'success': True, 'prospect_id': prospect.id}

        result = await reply_to_pending(db, chat_id=chat_id, user_id=user_id, body=text)
        if result.get('success'):
            await telegram_api('sendMessage', {'chat_id': chat_id, 'text': f'Відповідь відправлено: {result.get("sent_to")}'})
            return result
        await telegram_api('sendMessage', {'chat_id': chat_id, 'text': 'Формат не распознан. Отправь ровно 2 строки:\n1) email ютубера\n2) ссылка на YouTube-канал'})
        return {'success': True, 'ignored': False, 'reason': 'invalid_manual_youtube_format'}
    return {'success': True, 'ignored': True}
