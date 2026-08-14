# Mail Sender

FastAPI project for collecting bloggers, saving them to a database, and sending emails through multiple SMTP/IMAP accounts.

## What's already implemented

- Email accounts are loaded from `.env` via `MAIL_ACCOUNTS_JSON`.
- Default servers: `mx1.cityhost.com.ua` for SMTP/IMAP/POP3.
- Default SQLite database: `data/mail_sender.sqlite3`.
- Tables:
  - `mail_accounts`
  - `prospects`
  - `send_logs`
  - `inbound_messages`
  - `telegram_pending_replies`
- Website parser searches for:
  - `youtube_link`
  - `email`
  - `nick`
  - `subscribers`
  - `telegram`
  - `viber`
  - `whatsapp`
  - `facebook`
  - `instagram`
- Before adding, checks for duplicates by `youtube_link`.
- `subscribers < 3000` → `plans='later'`.
- `subscribers >= 3000` → `plans='now'`.
- `status=False` by default; becomes `True` after successful send.
- Sending only occurs for `plans='now'` + `status=False` + email present.
- Global interval between sends: `SEND_INTERVAL_BETWEEN_EMAILS_SECONDS=600`.
- Per-account interval: `SEND_INTERVAL_PER_ACCOUNT_SECONDS=3600`.
- Same prospect won't be sent twice from the same account thanks to unique log `account_id + prospect_id`.
- Incoming emails can be checked via IMAP; notifications go to Telegram if a token is provided.
- Telegram notification includes a `Reply` button; the next message in the chat is sent as a reply to the email.

## Important note on secrets

Real passwords are not stored in the project. You provided them in the chat, but I did not save them to files. Insert them manually into your local `.env`.

## Installation

```bash
cd /root/projects/mail-sender
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then open `.env` and replace `CHANGE_ME` with real passwords.

## Running locally

```bash
cd /root/projects/mail-sender
. .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8095
```

## Verification

```bash
curl http://127.0.0.1:8095/health
```

Admin panel:

```text
http://127.0.0.1:8095/admin
```

## Main endpoints

### Sync accounts from `.env`

```bash
curl -X POST http://127.0.0.1:8095/api/sync-accounts
```

### Run the parser

```bash
curl -X POST http://127.0.0.1:8095/api/parse \
  -H 'Content-Type: application/json' \
  -d '{"start_url":"https://example.com/bloggers","max_pages":1}'
```

If `start_url` is not provided, it uses `PARSE_START_URL` from `.env`.

### Send the next email

```bash
curl -X POST http://127.0.0.1:8095/api/send-next
```

Before this, you need to fill in:

```text
OUTBOUND_SUBJECT
prompts/first_message.txt
```

### Check incoming emails

```bash
curl -X POST http://127.0.0.1:8095/api/inbox-check
```

### Reply to an incoming email manually via API

```bash
curl -X POST http://127.0.0.1:8095/api/reply \
  -H 'Content-Type: application/json' \
  -d '{"inbound_id":1,"body":"Reply text"}'
```

## Telegram webhook

Once you provide the token, you'll need to:

1. Set in `.env`:

```text
TELEGRAM_BOT_TOKEN="..."
TELEGRAM_ADMIN_CHAT_IDS_JSON='["123456789"]'
```

2. Set the webhook to the service's public URL:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://YOUR_DOMAIN/telegram/webhook"
```

## Automatic loops

Disabled by default to avoid accidental sending.

Enable via `.env`:

```text
AUTO_SENDER_ENABLED=true
AUTO_INBOX_ENABLED=true
AUTO_PARSE_ENABLED=true
```

Do not enable `AUTO_SENDER_ENABLED` until you're ready with the subject and first message text.