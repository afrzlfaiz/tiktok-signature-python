"""
Uji: cookie mana (saja) yang mengidentifikasi akun TikTok yang sudah login.

Metode: tiap subset cookie dibuat context Playwright sendiri → buka tiktok.com
→ tangkap request API bertanda tangan pertama milik halaman (feed yang dimuat
halaman itu sendiri) → fetch ULANG URL persis itu secara sintetis. Body > 0 =
subset cukup mengidentifikasi akun (feed login = story/user_list, tamu =
recommend/item_list 0-byte).

Pemakaian: COOKIE_JAR="..." python3 test_cookies.py
"""

import asyncio
import os
from urllib.parse import urlsplit, parse_qsl

from playwright.async_api import async_playwright

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.6 Safari/605.1.15"
)
INIT_SCRIPT = "Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });"


def parse_jar(jar: str):
    return {k: v for c in jar.split(";") if "=" in c
            for k, v in [c.strip().split("=", 1)]}


async def test_subset(browser, subset: dict, label: str) -> str:
    ctx = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
    await ctx.add_init_script(INIT_SCRIPT)
    if subset:
        await ctx.add_cookies([{"name": k, "value": v, "domain": ".tiktok.com", "path": "/"}
                               for k, v in subset.items()])
    page = await ctx.new_page()
    first = {"url": None}

    def on_request(request):
        u = request.url
        if first["url"] is None and "/api/" in u and "X-Bogus=" in u:
            first["url"] = u

    page.on("request", on_request)
    await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
    for _ in range(30):
        if first["url"]:
            break
        await asyncio.sleep(1)

    if not first["url"]:
        await ctx.close()
        return f"{label:34} -> gagal init (tak ada API request bertanda tangan)"

    endpoint = first["url"].split("?")[0]
    result = await page.evaluate(
        "(u) => fetch(u, {credentials:'include'}).then(async r => ({s: r.status, n: (await r.text()).length}))",
        first["url"],
    )
    await ctx.close()
    mode = "LOGIN" if endpoint.endswith("story/user_list") else "guest"
    return f"{label:34} -> feed {mode:5} ({endpoint.split('/')[-2]}/{endpoint.split('/')[-1].rstrip('/')}) refetch: HTTP {result['s']}, {result['n']:>7,} bytes"


async def main():
    jar = parse_jar(os.environ["COOKIE_JAR"])
    print(f"Total cookies di jar: {len(jar)} — {sorted(jar.keys())}\n")

    groups = {
        "SEMUA (jar penuh)": jar,
        "sessionid + ttwid": {k: jar[k] for k in ("sessionid", "ttwid")},
        "sessionid saja": {"sessionid": jar["sessionid"]},
        "sessionid_ss saja": {"sessionid_ss": jar["sessionid_ss"]},
        "sid_tt saja": {"sid_tt": jar["sid_tt"]},
        "sid_guard saja": {"sid_guard": jar["sid_guard"]},
        "uid_tt saja": {"uid_tt": jar["uid_tt"]},
        "ttwid saja": {"ttwid": jar["ttwid"]},
        "msToken saja": {"msToken": jar["msToken"]},
        "sessionid + ttwid + msToken": {k: jar[k] for k in ("sessionid", "ttwid", "msToken")},
        "sessionid + ttwid + uid_tt": {k: jar[k] for k in ("sessionid", "ttwid", "uid_tt")},
        "sid_guard + ttwid": {k: jar[k] for k in ("sid_guard", "ttwid")},
        "ssid_ucp_v1 + ttwid": {k: jar[k] for k in ("ssid_ucp_v1", "ttwid")},
        "tanpa cookies (tamu)": {},
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            for label, subset in groups.items():
                print(await test_subset(browser, subset, label))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
