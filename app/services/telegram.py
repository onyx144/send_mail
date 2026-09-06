from __future__ import annotations

import httpx

from app.core.config import settings


def telegram_enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_admin_chat_ids())


async def telegram_api(method: str, payload: dict) -> dict:
    if not settings.telegram_bot_token:
        return {'ok': False, 'description': 'TELEGRAM_BOT_TOKEN is empty'}
    url = f'https://api.telegram.org/bot{settings.telegram_bot_token}/{method}'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def notify_inbound_message(*, inbound_id: int, from_email: str | None, subject: str | None, body: str | None) -> int:
    if not telegram_enabled():
        return 0
    text = (
        '📩 Новий вхідний лист\n\n'
        f'ID: {inbound_id}\n'
        f'Від: {from_email or "-"}\n'
        f'Тема: {subject or "-"}\n\n'
        f'{(body or "")[:2500]}'
    )
    keyboard = {
        'inline_keyboard': [[
            {'text': 'Відповісти', 'callback_data': f'reply:{inbound_id}'},
        ]]
    }
    sent = 0
    for chat_id in settings.telegram_admin_chat_ids():
        payload = {'chat_id': chat_id, 'text': text, 'reply_markup': keyboard}
        data = await telegram_api('sendMessage', payload)
        if data.get('ok'):
            sent += 1
    return sent


def extract_update_message(update: dict) -> tuple[str | None, str | None, str | None]:
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return None, None, None
    chat = msg.get('chat') or {}
    user = msg.get('from') or {}
    return str(chat.get('id')) if chat.get('id') else None, str(user.get('id')) if user.get('id') else None, msg.get('text')
