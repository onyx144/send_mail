from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.db.session import SessionLocal, init_db
from app.services.parser import cleanup_stale_youtube_channels

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'data' / 'mail_sender.sqlite3'
OUT_PATH = ROOT / 'data' / 'cleanup_stale_youtube_channels_result.json'
BACKUP_DIR = ROOT / 'backups'


def db_counts() -> dict:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    data = {
        'total': cur.execute('select count(*) from prospects').fetchone()[0],
        'youtube_total': cur.execute("select count(*) from prospects where youtube_link is not null and youtube_link!=''").fetchone()[0],
        'with_last_video_at': cur.execute('select count(*) from prospects where last_video_at is not null').fetchone()[0],
        'status_counts': [
            {'status': row[0], 'plans': row[1], 'count': row[2]}
            for row in cur.execute('select status, plans, count(*) from prospects group by status, plans order by status, plans')
        ],
    }
    con.close()
    return data


def make_backup() -> str | None:
    if not DB_PATH.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_path = BACKUP_DIR / f'before_cleanup_stale_youtube_{stamp}' / DB_PATH.name
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, backup_path)
    return str(backup_path)


async def main() -> None:
    parser = argparse.ArgumentParser(description='Delete YouTube prospects whose latest video is older than the requested age.')
    parser.add_argument('--max-age-days', type=int, default=60, help='Delete if latest video is older than this many days. Default: 60')
    parser.add_argument('--limit', type=int, default=None, help='Check only first N DB rows. Omit for all rows.')
    parser.add_argument('--dry-run', action='store_true', help='Check and report only; do not delete.')
    args = parser.parse_args()

    await init_db()
    before = db_counts()
    backup_path = None if args.dry_run else make_backup()
    async with SessionLocal() as db:
        cleanup = await cleanup_stale_youtube_channels(
            db,
            max_age_days=args.max_age_days,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    after = db_counts()
    payload = {
        'success': True,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'backup_path': backup_path,
        'before': before,
        'cleanup': cleanup,
        'after': after,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, default=str))


if __name__ == '__main__':
    asyncio.run(main())
