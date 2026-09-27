from __future__ import annotations

import asyncio
import re
from playwright.async_api import async_playwright

URL = 'https://www.youtube.com/@Juv4ik/videos'

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
        context = await browser.new_context(locale='uk-UA', user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        page = await context.new_page()
        await page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        if 'consent.youtube.com' in page.url:
            await page.locator('button[jsname="b3VHJd"]').first.click(timeout=10000)
            await page.wait_for_load_state('domcontentloaded', timeout=60000)
        print('URL_AFTER:', page.url)
        print('TITLE_AFTER:', await page.title())
        await page.wait_for_timeout(10000)
        text = await page.locator('body').inner_text(timeout=10000)
        print('TEXT_LEN:', len(text))
        print('TEXT_HEAD:', text[:5000].replace('\n', ' | '))
        print('MATCH_LINES:')
        for line in text.splitlines():
            if re.search(r'(перегляд|просмотр|views|view|рік|рок|year|month|місяц|міс|дн|day|тиж|week|ago|тому)', line, re.I):
                print(line[:300])
        await page.screenshot(path='/root/projects/mail-sender/data/youtube_after_consent.png', full_page=True)
        await browser.close()
asyncio.run(main())
