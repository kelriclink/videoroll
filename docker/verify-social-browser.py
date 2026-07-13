from __future__ import annotations

import asyncio

from patchright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page()
        await page.goto("about:blank")
        print(f"social-publisher browser compatibility OK: {browser.version}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
