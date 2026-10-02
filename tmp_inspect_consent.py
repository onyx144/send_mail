from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright

URL = 'https://www.youtube.com/@Juv4ik/videos'

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox','--disable-dev-shm-usage'])
        page = await browser.new_page(locale='uk-UA', user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        await page.goto(URL, wait_until='domcontentloaded', timeout=60000)
        print('url', page.url)
        print('title', await page.title())
        for sel in ['button', '[role=button]', 'form button', 'input[type=submit]']:
            try:
                loc=page.locator(sel)
                n=await loc.count()
                print('SEL', sel, 'count', n)
                for i in range(min(n,20)):
                    try:
                        print(i, await loc.nth(i).inner_text(timeout=1000), await loc.nth(i).get_attribute('aria-label'), await loc.nth(i).get_attribute('jsname'))
                    except Exception as e:
                        print(i, 'ERR', repr(e))
            except Exception as e:
                print('sel err', sel, e)
        await page.screenshot(path='/root/projects/mail-sender/data/youtube_consent.png', full_page=True)
        print('screenshot saved')
        await browser.close()
asyncio.run(main())
