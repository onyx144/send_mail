from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

EMAIL_RE = re.compile(r'([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})')
BLOCKED_EMAIL_DOMAINS = {
    'example.com', 'email.com', 'domain.com', 'test.com', 'yourdomain.com',
}


@dataclass
class EmailFindResult:
    emails: list[str]
    source_url: str
    checked_urls: list[str]
    status: str
    error: str | None = None


def clean_email(email: str) -> str | None:
    email = email.strip().strip('.,;:()[]{}<>"\'').lower()
    if not email or '@' not in email:
        return None
    domain = email.rsplit('@', 1)[-1]
    if domain in BLOCKED_EMAIL_DOMAINS:
        return None
    if len(email) > 253:
        return None
    return email


def unique_emails(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in EMAIL_RE.findall(text or ''):
        email = clean_email(raw)
        if email and email not in seen:
            seen.add(email)
            result.append(email)
    return result


async def extract_emails_from_page(page) -> list[str]:
    # Same core logic as the Chrome extension: body innerText + mailto links.
    payload = await page.evaluate(
        """
        () => {
          const bodyText = document.body ? document.body.innerText : '';
          const links = Array.from(document.querySelectorAll('a[href^="mailto:"]'))
            .map(a => a.href.replace('mailto:', '').split('?')[0]);
          return bodyText + '\n' + links.join('\n');
        }
        """
    )
    return unique_emails(payload)


async def collect_candidate_links(page, limit: int = 8) -> list[str]:
    hrefs = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
        """
    )
    allowed_hosts = (
        'linktr.ee', 'beacons.ai', 'solo.to', 'taplink', 'instagram.com',
        't.me', 'telegram.me', 'discord.gg', 'discord.com', 'facebook.com'
    )
    result: list[str] = []
    for href in hrefs:
        if not isinstance(href, str) or not href.startswith('http'):
            continue
        if 'youtube.com/redirect?' in href:
            continue
        if any(host in href for host in allowed_hosts) or ('youtube.com' not in href and 'youtu.be' not in href):
            if href not in result:
                result.append(href)
        if len(result) >= limit:
            break
    return result


async def find_emails_with_playwright(youtube_url: str, *, external_links_limit: int = 6, timeout_ms: int = 25000) -> EmailFindResult:
    checked: list[str] = []
    emails: list[str] = []
    seen: set[str] = set()

    def add(found: list[str]) -> None:
        for email in found:
            if email not in seen:
                seen.add(email)
                emails.append(email)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = await browser.new_context(
            viewport={'width': 1365, 'height': 900},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36',
        )
        page = await context.new_page()
        try:
            base = youtube_url.rstrip('/')
            urls = []
            if '/about' in base:
                urls.append(base)
            else:
                urls.extend([base + '/about', base])
            external_links: list[str] = []
            for url in urls:
                try:
                    checked.append(url)
                    await page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                    # Google/YouTube consent fallback.
                    for label in ('Accept all', 'Reject all', 'Принять все', 'Відхилити все'):
                        try:
                            btn = page.get_by_role('button', name=label)
                            if await btn.count():
                                await btn.first.click(timeout=2000)
                                await page.wait_for_timeout(1500)
                                break
                        except Exception:
                            pass
                    await page.wait_for_timeout(2500)
                    add(await extract_emails_from_page(page))
                    external_links.extend(await collect_candidate_links(page, external_links_limit))
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue

            for url in external_links[:external_links_limit]:
                if emails:
                    break
                try:
                    checked.append(url)
                    await page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                    await page.wait_for_timeout(1500)
                    add(await extract_emails_from_page(page))
                except Exception:
                    continue
        finally:
            await context.close()
            await browser.close()

    return EmailFindResult(
        emails=emails,
        source_url=youtube_url,
        checked_urls=checked,
        status='found' if emails else 'not_found_open_pages',
    )


async def main() -> None:
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--external-links-limit', type=int, default=6)
    args = parser.parse_args()
    result = await find_emails_with_playwright(args.url, external_links_limit=args.external_links_limit)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
