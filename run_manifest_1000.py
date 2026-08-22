import asyncio
import json
import sqlite3
from pathlib import Path

from app.db.session import init_db, SessionLocal
from app.services.parser import parse_manifest

START_URL = 'https://manifest.in.ua/rt/play/page/3/?order_type=_subscribercount&order=ASC'
OUT_PATH = Path('data/manifest_1000_result.json')
DB_PATH = Path('data/mail_sender.sqlite3')


def stats():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    data = {
        'status_counts': [dict(r) for r in cur.execute("select status, plans, count(*) c from prospects group by status, plans order by status, plans")],
        'contact_counts': [dict(r) for r in cur.execute("select coalesce(contact_status,'null') contact_status, count(*) c from prospects group by coalesce(contact_status,'null') order by c desc")],
        'email_count': cur.execute("select count(*) from prospects where email is not null and email!=''").fetchone()[0],
        'social_count': cur.execute("select count(*) from prospects where telegram is not null or instagram is not null or discord is not null or facebook is not null or raw_contacts like '%@%'").fetchone()[0],
        'website_count': cur.execute("select count(*) from prospects where website is not null and website!=''").fetchone()[0],
        'total': cur.execute("select count(*) from prospects").fetchone()[0],
    }
    con.close()
    return data


async def main():
    await init_db()
    async with SessionLocal() as db:
        result = await parse_manifest(
            db,
            start_url=START_URL,
            target_saved=1000,
            max_pages=500,
        )
    payload = {'parse_result': result, 'stats': stats()}
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(payload, ensure_ascii=False, default=str))


if __name__ == '__main__':
    asyncio.run(main())
