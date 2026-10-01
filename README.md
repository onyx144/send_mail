# Mail Sender

FastAPI-проект для сбора блогеров, сохранения в БД и рассылки писем через несколько SMTP/IMAP ящиков.

## Что уже реализовано

- Почтовые аккаунты берутся из `.env` через `MAIL_ACCOUNTS_JSON`.
- Серверы по умолчанию: `mx1.cityhost.com.ua` для SMTP/IMAP/POP3.
- БД SQLite по умолчанию: `data/mail_sender.sqlite3`.
- Таблицы:
  - `mail_accounts`
  - `prospects`
  - `send_logs`
  - `inbound_messages`
  - `telegram_pending_replies`
- Парсер сайта ищет:
  - `youtube_link`
  - `email`
  - `nick`
  - `subscribers`
  - `telegram`
  - `viber`
  - `whatsapp`
  - `facebook`
  - `instagram`
- Перед добавлением проверяет дубль по `youtube_link`.
- `subscribers < 3000` → `plans='later'`.
- `subscribers >= 3000` → `plans='now'`.
- `status=False` по умолчанию, после успешной отправки становится `True`.
- Отправка идёт только по `plans='now'` + `status=False` + есть email.
- Интервал между отправками глобально: `SEND_INTERVAL_BETWEEN_EMAILS_SECONDS=600`.
- Интервал на один ящик: `SEND_INTERVAL_PER_ACCOUNT_SECONDS=3600`.
- Один и тот же prospect не отправляется повторно с того же ящика благодаря unique log `account_id + prospect_id`.
- Входящие письма можно проверять через IMAP, уведомления уходят в Telegram при наличии токена.
- В Telegram уведомлении есть кнопка `Відповісти`; следующий текст в чате отправляется как reply на email.

## Важно по секретам

Реальные пароли не записаны в проект. Ты дал их в чате, но я не сохранил их в файлы. Вставь их вручную в локальный `.env`.

## Установка

```bash
cd /root/projects/mail-sender
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Потом открой `.env` и вставь реальные пароли вместо `CHANGE_ME`.

## Запуск локально

```bash
cd /root/projects/mail-sender
. .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8095
```

## Проверка

```bash
curl http://127.0.0.1:8095/health
```

Админка:

```text
http://127.0.0.1:8095/admin
```

## Основные endpoints

### Синхронизировать аккаунты из `.env`

```bash
curl -X POST http://127.0.0.1:8095/api/sync-accounts
```

### Запустить парсер

```bash
curl -X POST http://127.0.0.1:8095/api/parse \
  -H 'Content-Type: application/json' \
  -d '{"start_url":"https://example.com/bloggers","max_pages":1}'
```

Если `start_url` не передавать, берёт `PARSE_START_URL` из `.env`.

### Отправить следующее письмо

```bash
curl -X POST http://127.0.0.1:8095/api/send-next
```

Перед этим нужно заполнить:

```text
OUTBOUND_SUBJECT
prompts/first_message.txt
```

### Проверить входящие

```bash
curl -X POST http://127.0.0.1:8095/api/inbox-check
```

### Ответить на входящее вручную через API

```bash
curl -X POST http://127.0.0.1:8095/api/reply \
  -H 'Content-Type: application/json' \
  -d '{"inbound_id":1,"body":"Текст ответа"}'
```

## Telegram webhook

Когда дашь токен, нужно будет:

1. Указать в `.env`:

```text
TELEGRAM_BOT_TOKEN="..."
TELEGRAM_ADMIN_CHAT_IDS_JSON='["123456789"]'
```

2. Поставить webhook на публичный URL сервиса:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://YOUR_DOMAIN/telegram/webhook"
```

## Автоматические циклы

По умолчанию выключены, чтобы ничего случайно не отправлять.

Включаются через `.env`:

```text
AUTO_SENDER_ENABLED=true
AUTO_INBOX_ENABLED=true
AUTO_PARSE_ENABLED=true
```

Не включай `AUTO_SENDER_ENABLED`, пока не готов subject и текст первого письма.
