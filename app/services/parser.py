from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin
import xml.etree.ElementTree as ET

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import or_, select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.models import Prospect

MANIFEST_DEFAULT_URL = 'https://manifest.in.ua/rt/play/page/3/?order_type=_subscribercount&order=ASC'
MIN_MANIFEST_SUBSCRIBERS = 3000
MAX_MANIFEST_SUBSCRIBERS = 10000
MIN_MANIFEST_VIDEOS_EXCLUSIVE = 3
MAX_LAST_VIDEO_AGE_DAYS = 93

EMAIL_RE = re.compile(r'[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}', re.I)
YOUTUBE_RE = re.compile(r'https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s"\'<>]+', re.I)
SUBS_RE = re.compile(r'(\d[\d\s,.]*)\s*(?:підпис|подпис|subscribers|subs|тис|тыс|k|к)', re.I)
DISCORD_RE = re.compile(r'(?:https?://)?(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9_-]+|(?:^|\s)([A-Za-z0-9_.]{2,32}#[0-9]{4})(?:\s|$)', re.I)
HANDLE_RE = re.compile(r'(?<![\w.])@[A-Za-z0-9_.]{3,32}')
URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)
SOCIAL_RE = {
    'telegram': re.compile(r'(?:https?://)?t\.me/[\w_]+|@[A-Za-z0-9_]{4,}', re.I),
    'viber': re.compile(r'(?:viber://[^\s"\'<>]+|\+?\d[\d\s()\-]{8,})', re.I),
    'whatsapp': re.compile(r'(?:https?://)?(?:wa\.me|api\.whatsapp\.com)/[^\s"\'<>]+|\+?\d[\d\s()\-]{8,}', re.I),
    'facebook': re.compile(r'https?://(?:www\.)?facebook\.com/[^\s"\'<>]+', re.I),
    'instagram': re.compile(r'https?://(?:www\.)?instagram\.com/[^\s"\'<>]+|@[A-Za-z0-9_.]{2,}', re.I),
}


@dataclass
class Candidate:
    youtube_link: str
    email: str | None
    nick: str | None
    subscribers: int | None
    telegram: str | None
    viber: str | None
    whatsapp: str | None
    facebook: str | None
    instagram: str | None
    discord: str | None
    source_url: str
    raw_text: str


@dataclass
class ManifestRow:
    nick: str
    manifest_url: str
    subscribers: int
    source_url: str


@dataclass
class ManifestProfile:
    youtube_link: str | None
    video_count: int | None
    raw_text: str
    about_text: str | None = None
    contacts: dict | None = None


def normalize_url(url: str) -> str:
    return url.strip().rstrip('/').split('?', 1)[0]


def parse_count(text_value: str | None) -> int | None:
    if not text_value:
        return None
    cleaned = re.sub(r'[^0-9]', '', text_value.replace('\xa0', ' '))
    return int(cleaned) if cleaned else None


def parse_subscribers(text_value: str) -> int | None:
    m = SUBS_RE.search(text_value or '')
    if not m:
        return parse_count(text_value)
    raw = m.group(1).replace(' ', '').replace(',', '.')
    try:
        value = float(raw)
    except ValueError:
        return None
    tail = (text_value[m.start():m.end()] or '').lower()
    if 'тис' in tail or 'тыс' in tail or 'k' in tail or 'к' in tail:
        value *= 1000
    return int(value)


def first_match(regex: re.Pattern, text_value: str) -> str | None:
    m = regex.search(text_value or '')
    return m.group(0).strip() if m else None


def first_social_match(regex: re.Pattern, text_value: str, *, forbidden: set[str] | None = None) -> str | None:
    forbidden = {v.lower() for v in (forbidden or set())}
    for m in regex.finditer(text_value or ''):
        value = m.group(0).strip()
        if value.lower() in forbidden:
            continue
        return value
    return None


def clean_contact_value(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip('.,;:()[]{}<>"\'')
    if not value or value.lower() in {'@media', '@import', '@font-face'}:
        return None
    return value


def uniq(values: list[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_contact_value(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def extract_about_text(soup: BeautifulSoup) -> str:
    candidates: list[str] = []
    selectors = [
        '.channel-profile__description', '.channel-profile--description', '.channel-description',
        '.profile-description', '.entry-content', '.content', '.card-body', 'main'
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text_value = node.get_text('\n', strip=True)
            if text_value and len(text_value) > 20:
                candidates.append(text_value)
    if candidates:
        return max(candidates, key=len)[:8000]
    return soup.get_text('\n', strip=True)[:8000]


def extract_contacts_from_soup(soup: BeautifulSoup, source_url: str, extra_text: str = '') -> dict:
    hrefs = [urljoin(source_url, a.get('href') or '') for a in soup.select('a[href]')]
    visible = soup.get_text('\n', strip=True)
    combined = visible + '\n' + extra_text + '\n' + '\n'.join(hrefs)
    handles = [h for h in uniq(HANDLE_RE.findall(combined)) if h.lower() not in {'@media'}]
    urls = uniq([u.rstrip('/)];,') for u in URL_RE.findall(combined)])
    website = None
    for href in hrefs:
        if not href.startswith('http'):
            continue
        blocked_hosts = [
            'manifest.in.ua', 'youtube.com', 'youtu.be', 'facebook.com', 'instagram.com',
            't.me', 'telegram.me', 'discord.gg', 'discord.com', 'wa.me', 'viber',
            'send.monobank.ua', 'donatello.to', 'buymeacoffee.com', 'patreon.com',
            'fondy.eu', 'liqpay.ua', 'paypal.com', 'ko-fi.com', 'socialblade.com',
        ]
        if any(host in href for host in blocked_hosts):
            continue
        website = normalize_url(href)
        break
    contacts = {
        'email': first_match(EMAIL_RE, combined),
        'telegram': first_social_match(SOCIAL_RE['telegram'], combined, forbidden={'@media'}),
        'instagram': first_social_match(SOCIAL_RE['instagram'], combined, forbidden={'@media'}),
        'facebook': first_social_match(SOCIAL_RE['facebook'], combined),
        'discord': first_social_match(DISCORD_RE, combined),
        'website': website,
        'handles': handles,
        'urls': urls[:30],
    }
    contacts['raw_contacts'] = json.dumps(contacts, ensure_ascii=False)
    if contacts.get('email'):
        contacts['contact_status'] = 'email_found'
    elif contacts.get('telegram') or contacts.get('instagram') or contacts.get('discord') or contacts.get('facebook') or handles:
        contacts['contact_status'] = 'social_found'
    elif contacts.get('website'):
        contacts['contact_status'] = 'website_found'
    else:
        contacts['contact_status'] = 'not_found'
    return contacts


async def fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=settings.parse_timeout_seconds, follow_redirects=True) as client:
        resp = await client.get(url, headers={'User-Agent': 'Mozilla/5.0 MailSenderBot/1.0 (+https://questalize.com)'})
        resp.raise_for_status()
        return resp.text


def extract_candidates_from_html(html: str, source_url: str) -> list[Candidate]:
    soup = BeautifulSoup(html, 'lxml')
    text_value = soup.get_text('\n', strip=True)
    all_text = text_value + '\n' + '\n'.join(str(a.get('href')) for a in soup.select('a[href]'))
    emails = EMAIL_RE.findall(all_text)
    youtube_links = [normalize_url(urljoin(source_url, m.group(0))) for m in YOUTUBE_RE.finditer(all_text)]
    for a in soup.select('a[href]'):
        href = a.get('href') or ''
        if 'youtube.com' in href or 'youtu.be' in href:
            youtube_links.append(normalize_url(urljoin(source_url, href)))
    youtube_links = list(dict.fromkeys(youtube_links))

    candidates: list[Candidate] = []
    for yt in youtube_links:
        nearby = text_value
        link_node = soup.find('a', href=lambda h: h and yt.split('?', 1)[0] in normalize_url(urljoin(source_url, h)))
        if link_node:
            card = link_node.find_parent(['article', 'li', 'tr', 'div'])
            if card:
                nearby = card.get_text('\n', strip=True)
        email_value = first_match(EMAIL_RE, nearby) or (emails[0] if emails else None)
        nick = link_node.get_text(' ', strip=True) if link_node else None
        if not nick:
            nick = yt.rstrip('/').split('/')[-1]
        candidates.append(
            Candidate(
                youtube_link=yt,
                email=email_value,
                nick=nick,
                subscribers=parse_subscribers(nearby) or parse_subscribers(text_value),
                telegram=first_match(SOCIAL_RE['telegram'], nearby),
                viber=first_match(SOCIAL_RE['viber'], nearby),
                whatsapp=first_match(SOCIAL_RE['whatsapp'], nearby),
                facebook=first_match(SOCIAL_RE['facebook'], nearby),
                instagram=first_match(SOCIAL_RE['instagram'], nearby),
                discord=first_match(DISCORD_RE, nearby),
                source_url=source_url,
                raw_text=nearby[:12000],
            )
        )
    return candidates


def extract_manifest_rows(html: str, source_url: str) -> list[ManifestRow]:
    soup = BeautifulSoup(html, 'lxml')
    rows: list[ManifestRow] = []
    for tr in soup.select('tr'):
        name_link = tr.select_one('a.rating-item__name[href]')
        if not name_link:
            continue
        nick = name_link.get_text(' ', strip=True)
        manifest_url = normalize_url(urljoin(source_url, name_link.get('href') or ''))
        counts = [parse_count(node.get_text(' ', strip=True)) for node in tr.select('.rating-item__rating.count')]
        counts = [c for c in counts if c is not None]
        if not counts:
            continue
        # On manifest rating rows ordered by subscriber count, the first rating count is subscribers.
        subscribers = counts[0]
        rows.append(ManifestRow(nick=nick, manifest_url=manifest_url, subscribers=subscribers, source_url=source_url))
    return rows


def extract_manifest_profile(html: str, source_url: str) -> ManifestProfile:
    soup = BeautifulSoup(html, 'lxml')
    text_value = soup.get_text('\n', strip=True)
    about_text = extract_about_text(soup)
    contacts = extract_contacts_from_soup(soup, source_url, about_text)
    video_count = None
    for item in soup.select('.mini-stats__item'):
        desc = item.select_one('.mini-stats__desc')
        if desc and 'відео' in desc.get_text(' ', strip=True).lower():
            video_count = parse_count(item.select_one('.mini-stats__count').get_text(' ', strip=True) if item.select_one('.mini-stats__count') else item.get_text(' ', strip=True))
            break
    youtube_link = None
    btn_wrap = soup.select_one('.channel-profile--btns') or soup
    for a in btn_wrap.select('a[href]'):
        href = a.get('href') or ''
        if 'youtube.com' in href or 'youtu.be' in href:
            youtube_link = normalize_url(urljoin(source_url, href))
            break
    return ManifestProfile(youtube_link=youtube_link, video_count=video_count, raw_text=text_value[:12000], about_text=about_text, contacts=contacts)


def extract_yt_initial_data(html: str) -> dict | None:
    # YouTube pages embed `ytInitialData = {...};`. Parse with brace matching.
    marker = 'ytInitialData'
    idx = html.find(marker)
    while idx != -1:
        eq = html.find('=', idx)
        brace = html.find('{', eq)
        if eq == -1 or brace == -1:
            break
        depth = 0
        in_str = False
        esc = False
        for pos in range(brace, len(html)):
            ch = html[pos]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(html[brace:pos + 1])
                        except json.JSONDecodeError:
                            break
        idx = html.find(marker, idx + 1)
    return None


def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v)


def text_from_runs(obj) -> str:
    if isinstance(obj, dict):
        if 'simpleText' in obj:
            return str(obj.get('simpleText') or '')
        if isinstance(obj.get('runs'), list):
            return ''.join(str(r.get('text') or '') for r in obj['runs'] if isinstance(r, dict))
    return ''


def parse_youtube_latest_video_date(html: str) -> datetime | None:
    data = extract_yt_initial_data(html)
    search_text = html[:500000]
    if data:
        dates: list[datetime] = []
        for node in walk_json(data):
            for key in ('publishedTimeText', 'dateText'):
                value = text_from_runs(node.get(key)) if isinstance(node, dict) and node.get(key) else ''
                dt = relative_or_absolute_date(value)
                if dt:
                    dates.append(dt)
            if 'videoRenderer' in node and isinstance(node['videoRenderer'], dict):
                value = text_from_runs(node['videoRenderer'].get('publishedTimeText'))
                dt = relative_or_absolute_date(value)
                if dt:
                    dates.append(dt)
        if dates:
            return max(dates)
    # RSS fallback link exists for channel IDs and includes published dates.
    m = re.search(r'"channelId":"(UC[\w-]+)"', search_text)
    if not m:
        m = re.search(r'youtube\.com/channel/(UC[\w-]+)', html)
    return None


def relative_or_absolute_date(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.lower().strip()
    now = datetime.now(timezone.utc)
    patterns = [
        (r'(\d+)\s*(?:second|seconds|сек|секунд)', 'seconds'),
        (r'(\d+)\s*(?:minute|minutes|хв|мин)', 'minutes'),
        (r'(\d+)\s*(?:hour|hours|год|час)', 'hours'),
        (r'(\d+)\s*(?:day|days|дн|день|дні|дней)', 'days'),
        (r'(\d+)\s*(?:week|weeks|тиж|нед)', 'weeks'),
        (r'(\d+)\s*(?:month|months|міс|мес)', 'months'),
        (r'(\d+)\s*(?:year|years|рік|роки|лет|год)', 'years'),
    ]
    for pat, unit in patterns:
        m = re.search(pat, s)
        if not m:
            continue
        n = int(m.group(1))
        if unit == 'months':
            return now - timedelta(days=30 * n)
        if unit == 'years':
            return now - timedelta(days=365 * n)
        kwargs = {unit: n}
        return now - timedelta(**kwargs)
    return None


def extract_channel_id(value: str) -> str | None:
    m = re.search(r'(UC[\w-]{20,})', value or '')
    return m.group(1) if m else None


async def fetch_youtube_rss_latest(channel_id: str) -> datetime | None:
    url = f'https://www.youtube.com/feeds/videos.xml?channel_id={quote(channel_id)}'
    try:
        xml_text = await fetch(url)
    except Exception:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    dates: list[datetime] = []
    for entry in root.findall('atom:entry', ns):
        published = entry.findtext('atom:published', default='', namespaces=ns)
        updated = entry.findtext('atom:updated', default='', namespaces=ns)
        for value in (published, updated):
            if not value:
                continue
            try:
                dates.append(datetime.fromisoformat(value.replace('Z', '+00:00')))
            except ValueError:
                pass
    return max(dates) if dates else None


async def resolve_channel_id(youtube_link: str, html_hint: str | None = None) -> str | None:
    direct = extract_channel_id(youtube_link)
    if direct:
        return direct
    if html_hint:
        hinted = extract_channel_id(html_hint)
        if hinted:
            return hinted
    for suffix in ('', '/videos', '/about'):
        try:
            html = await fetch(normalize_url(youtube_link) + suffix)
        except Exception:
            continue
        cid = extract_channel_id(html)
        if cid:
            return cid
        await asyncio.sleep(0.3)
    return None


async def fetch_youtube_contacts_and_latest(youtube_link: str) -> tuple[dict, datetime | None, str]:
    urls = []
    normalized = normalize_url(youtube_link)
    if normalized.endswith('/videos'):
        urls.append(normalized)
        urls.append(normalized.rsplit('/videos', 1)[0] + '/about')
    else:
        urls.append(normalized + '/about')
        urls.append(normalized)

    combined = ''
    for url in urls:
        try:
            html = await fetch(url)
        except Exception:
            continue
        combined += '\n' + html[:700000]
        await asyncio.sleep(0.35)
    channel_id = await resolve_channel_id(youtube_link, combined)
    latest = await fetch_youtube_rss_latest(channel_id) if channel_id else None
    if latest is None:
        latest = parse_youtube_latest_video_date(combined)
    contacts = extract_contacts_from_text(combined)
    return contacts, latest, combined[:12000]


def extract_contacts_from_text(text_value: str) -> dict:
    # HTML often escapes @/links in JSON, but regex over raw HTML still catches most public contacts.
    compact = text_value.replace('\\u0026', '&').replace('\\/', '/')
    return {
        'email': first_match(EMAIL_RE, compact),
        'telegram': first_social_match(SOCIAL_RE['telegram'], compact, forbidden={'@media'}),
        'instagram': first_social_match(SOCIAL_RE['instagram'], compact, forbidden={'@media'}),
        'discord': first_social_match(DISCORD_RE, compact),
        'facebook': first_social_match(SOCIAL_RE['facebook'], compact),
    }


async def ensure_schema(db: AsyncSession) -> None:
    # SQLite-friendly lightweight migration for existing dev DB.
    result = await db.execute(sql_text('PRAGMA table_info(prospects)'))
    cols = {row[1] for row in result.fetchall()}
    needed = {
        'discord': 'TEXT',
        'website': 'TEXT',
        'manifest_url': 'TEXT',
        'video_count': 'INTEGER',
        'last_video_at': 'DATETIME',
        'lose_reason': 'TEXT',
        'contact_status': 'TEXT',
        'raw_contacts': 'TEXT',
        'about_text': 'TEXT',
    }
    for col, typ in needed.items():
        if col not in cols:
            await db.execute(sql_text(f'ALTER TABLE prospects ADD COLUMN {col} {typ}'))
    # Convert old boolean statuses 0/1 to string-ish values for SQLite if needed.
    await db.execute(sql_text("UPDATE prospects SET status='new' WHERE status IN ('0', 'false', 'False')"))
    await db.execute(sql_text("UPDATE prospects SET status='sent' WHERE status IN ('1', 'true', 'True')"))
    await db.commit()


async def existing_bloger(db: AsyncSession, *, nick: str | None = None, youtube_link: str | None = None, manifest_url: str | None = None) -> Prospect | None:
    conds = []
    if nick:
        conds.append(Prospect.nick == nick)
    if youtube_link:
        conds.append(Prospect.youtube_link == youtube_link)
    if manifest_url:
        conds.append(Prospect.manifest_url == manifest_url)
    if not conds:
        return None
    return await db.scalar(select(Prospect).where(or_(*conds)).limit(1))


async def save_manifest_result(
    db: AsyncSession,
    *,
    row: ManifestRow,
    youtube_link: str | None,
    status: str,
    plans: str,
    lose_reason: str | None,
    profile: ManifestProfile | None,
    contacts: dict | None = None,
    last_video_at: datetime | None = None,
    raw_text: str | None = None,
) -> Prospect:
    youtube_value = youtube_link or f'manifest:{row.manifest_url}'
    existing = await existing_bloger(db, nick=row.nick, youtube_link=youtube_value, manifest_url=row.manifest_url)
    if existing:
        return existing
    merged_contacts = dict(profile.contacts or {}) if profile and profile.contacts else {}
    for key, value in (contacts or {}).items():
        if value and not merged_contacts.get(key):
            merged_contacts[key] = value
    contacts = merged_contacts
    if contacts.get('email'):
        contacts['contact_status'] = 'email_found'
    elif contacts.get('telegram') or contacts.get('instagram') or contacts.get('discord') or contacts.get('facebook') or contacts.get('handles'):
        contacts['contact_status'] = 'social_found'
    elif contacts.get('website'):
        contacts['contact_status'] = 'website_found'
    else:
        contacts['contact_status'] = 'not_found'
    prospect = Prospect(
        youtube_link=youtube_value,
        email=contacts.get('email'),
        nick=row.nick,
        subscribers=row.subscribers,
        status=status,
        plans=plans,
        telegram=contacts.get('telegram'),
        instagram=contacts.get('instagram'),
        facebook=contacts.get('facebook'),
        discord=contacts.get('discord'),
        website=contacts.get('website'),
        manifest_url=row.manifest_url,
        video_count=profile.video_count if profile else None,
        last_video_at=last_video_at,
        lose_reason=lose_reason,
        contact_status=contacts.get('contact_status'),
        raw_contacts=contacts.get('raw_contacts') or json.dumps(contacts, ensure_ascii=False),
        about_text=profile.about_text[:8000] if profile and profile.about_text else None,
        source_url=row.source_url,
        raw_text=(raw_text or (profile.raw_text if profile else ''))[:12000],
    )
    db.add(prospect)
    await db.commit()
    await db.refresh(prospect)
    return prospect


async def parse_manifest(db: AsyncSession, start_url: str = MANIFEST_DEFAULT_URL, target_saved: int = 20, max_pages: int = 80) -> dict:
    await ensure_schema(db)
    current = start_url
    pages = 0
    rows_seen = 0
    eligible_subs = 0
    saved_ok = 0
    saved_lose = 0
    skipped_duplicate = 0
    skipped_subscribers = 0
    errors: list[str] = []

    while pages < max_pages and saved_ok < target_saved:
        try:
            html = await fetch(current)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                errors.append(f'pagination finished: {current} returned 404')
                break
            raise
        pages += 1
        rows = extract_manifest_rows(html, current)
        if not rows:
            break
        for row in rows:
            rows_seen += 1
            if not (MIN_MANIFEST_SUBSCRIBERS < row.subscribers < MAX_MANIFEST_SUBSCRIBERS or row.subscribers in range(MIN_MANIFEST_SUBSCRIBERS + 1, MAX_MANIFEST_SUBSCRIBERS)):
                # User said больше 3000 и до 10000; interpret strictly >3000 and <=10000.
                if not (row.subscribers > MIN_MANIFEST_SUBSCRIBERS and row.subscribers <= MAX_MANIFEST_SUBSCRIBERS):
                    skipped_subscribers += 1
                    continue
            eligible_subs += 1
            if await existing_bloger(db, nick=row.nick, manifest_url=row.manifest_url):
                skipped_duplicate += 1
                continue
            try:
                profile_html = await fetch(row.manifest_url)
                profile = extract_manifest_profile(profile_html, row.manifest_url)
            except Exception as exc:
                errors.append(f'{row.nick}: profile fetch failed: {exc}')
                profile = ManifestProfile(youtube_link=None, video_count=None, raw_text='')

            if not profile.youtube_link:
                await save_manifest_result(db, row=row, youtube_link=None, status='lose', plans='later', lose_reason='no_youtube_link', profile=profile)
                saved_lose += 1
                continue
            if await existing_bloger(db, nick=row.nick, youtube_link=profile.youtube_link, manifest_url=row.manifest_url):
                skipped_duplicate += 1
                continue
            if profile.video_count is None or profile.video_count <= MIN_MANIFEST_VIDEOS_EXCLUSIVE:
                await save_manifest_result(db, row=row, youtube_link=profile.youtube_link, status='lose', plans='later', lose_reason='video_count_le_3', profile=profile)
                saved_lose += 1
                continue

            contacts, latest, yt_raw = await fetch_youtube_contacts_and_latest(profile.youtube_link)
            if latest is None:
                await save_manifest_result(db, row=row, youtube_link=profile.youtube_link, status='lose', plans='later', lose_reason='youtube_latest_video_unknown', profile=profile, contacts=contacts, raw_text=yt_raw)
                saved_lose += 1
                continue
            if latest < datetime.now(timezone.utc) - timedelta(days=MAX_LAST_VIDEO_AGE_DAYS):
                await save_manifest_result(db, row=row, youtube_link=profile.youtube_link, status='lose', plans='later', lose_reason='last_video_older_than_3_months', profile=profile, contacts=contacts, last_video_at=latest, raw_text=yt_raw)
                saved_lose += 1
                continue

            await save_manifest_result(db, row=row, youtube_link=profile.youtube_link, status='new', plans='now', lose_reason=None, profile=profile, contacts=contacts, last_video_at=latest, raw_text=yt_raw)
            saved_ok += 1
            if saved_ok >= target_saved:
                break
            await asyncio.sleep(0.7)

        soup = BeautifulSoup(html, 'lxml')
        next_node = soup.select_one('a.next.page-numbers[href], .pagination a.next[href], a[rel="next"][href]')
        if next_node and next_node.get('href'):
            current = urljoin(current, next_node.get('href'))
        else:
            # Build next page URL if explicit next link is absent.
            m = re.search(r'/page/(\d+)/', current)
            if m:
                n = int(m.group(1)) + 1
                current = re.sub(r'/page/\d+/', f'/page/{n}/', current)
            else:
                break
        await asyncio.sleep(0.7)

    return {
        'success': True,
        'pages': pages,
        'rows_seen': rows_seen,
        'eligible_subscribers': eligible_subs,
        'saved_ok': saved_ok,
        'saved_lose': saved_lose,
        'skipped_duplicate': skipped_duplicate,
        'skipped_subscribers': skipped_subscribers,
        'errors': errors[:20],
    }


async def parse_site(db: AsyncSession, start_url: str | None = None, max_pages: int | None = None) -> dict:
    current = start_url or settings.parse_start_url
    if not current:
        raise ValueError('PARSE_START_URL is empty. Provide website URL later in .env or request body.')
    if 'manifest.in.ua' in current:
        return await parse_manifest(db, start_url=current, target_saved=20, max_pages=max_pages or 80)
    max_pages = max_pages or settings.parse_max_pages
    created = 0
    skipped_existing = 0
    seen = 0
    pages = 0

    for _ in range(max(max_pages, 1)):
        html = await fetch(current)
        pages += 1
        for cand in extract_candidates_from_html(html, current):
            seen += 1
            existing = await db.scalar(select(Prospect).where(Prospect.youtube_link == cand.youtube_link))
            if existing:
                skipped_existing += 1
                continue
            plans = 'later'
            if (cand.subscribers or 0) >= settings.min_subscribers_plan_now:
                plans = 'now'
            prospect = Prospect(
                youtube_link=cand.youtube_link,
                email=cand.email,
                nick=cand.nick,
                subscribers=cand.subscribers,
                status='new',
                plans=plans,
                telegram=cand.telegram,
                viber=cand.viber,
                whatsapp=cand.whatsapp,
                facebook=cand.facebook,
                instagram=cand.instagram,
                discord=cand.discord,
                source_url=cand.source_url,
                raw_text=cand.raw_text,
            )
            db.add(prospect)
            created += 1
        await db.commit()

        soup = BeautifulSoup(html, 'lxml')
        next_node = soup.select_one(settings.parse_next_selector) if settings.parse_next_selector else None
        if not next_node or not next_node.get('href'):
            break
        current = urljoin(current, next_node.get('href'))

    return {'pages': pages, 'seen': seen, 'created': created, 'skipped_existing': skipped_existing}
