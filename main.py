"""
TikTok Signature Server — port Python dari carcabot/tiktok-signature (Node.js).

Menandatangani URL API TikTok dengan mengeksekusi SDK webmssdk.js TikTok di
dalam Chromium headless. Halaman nyata tiktok.com memuat SDK versi sekarang
dari CDN-nya sendiri (SDK lama yang di-vendor repositori asli sudah tidak
kompatibel dengan bundle halaman epoch sekarang — menandatangani fetch
sintetis GAGAL bila SDK lama diinjeksi). Hook fetch/XHR halaman menandatangani
fetch sintetis yang kita picu (X-Bogus + X-Gnarly + X-Dynosaur + msToken);
request bertanda tangan itu kita tangkap lalu batalkan (abort) agar tidak ada
traffic keluar.

Klien WAJIB memakai user_agent + cookies dari respons saat fetch ke TikTok.
"""

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, parse_qs, parse_qsl, urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# Safari on macOS — signature terkunci ke UA ini (dari server.mjs repo asli)
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.6 Safari/605.1.15"
)

INIT_URL = "https://www.tiktok.com/@zara"  # halaman nyata: cookies + hook SDK
ALLOWED_TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com"}
API_TOKEN = os.getenv("SIGNATURE_API_TOKEN", "").strip()
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("SIGNATURE_CORS_ORIGINS", "").split(",")
    if origin.strip()
]
MAX_SIGS_BEFORE_REFRESH = 500
MAX_SESSION_AGE_S = 1800
SIGN_TIMEOUT_MS = 8000
FINGERPRINT = {  # harus cocok dengan env browser (dari repo asli)
    "browser_platform": "MacIntel",
    "os": "mac",
    "screen_width": "1920",
    "screen_height": "1080",
}
MEDIA_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".mp4", ".webm",
    ".m3u8", ".woff", ".woff2", ".ttf", ".otf",
)
# Param yang tidak boleh diwariskan dari request halaman sendiri
REQUEST_SPECIFIC = {"secUid", "cursor", "count", "from_page", "abTestVersion", "offset"}
SIGNATURE_PARAMS = {"X-Bogus", "X-Gnarly", "X-Dynosaur", "msToken"}

state = {
    "playwright": None,
    "browser": None,
    "context": None,
    "page": None,
    "ready": False,
    "sign_count": 0,
    "last_init": 0.0,
    "env_params": {},  # device_id/odinId/region/... dari request API halaman sendiri
}
# Satu handler route permanen (register/unregister berulang memicu race
# Playwright "Route is already handled"). "armed" hanya selama capture.
route_state = {"armed": False, "env_captured": False}
sign_lock = asyncio.Lock()  # ponytail: serialisasi global, ganti per-browser kalau throughput jadi masalah

# konsistensi fingerprint: navigator.platform di halaman
INIT_SCRIPT = """
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
"""


async def route_handler(route):
    """Handler permanen (dipasang sekali). Saat 'armed' (sedang capture),
    request bertanda tangan & aset berat di-abort — cukup ditangkap, tidak
    boleh keluar jaringan. Param lingkungan diwarisi dari request API
    bertanda tangan pertama yang dibuat halaman sendiri (SDK hanya menambah
    param tanda tangan pada fetch sintetis; param lingkungan ditambahkan
    API-client halaman)."""
    url = route.request.url
    if not route_state["env_captured"] and "/api/" in url and "X-Bogus=" in url:
        params = dict(parse_qsl(urlsplit(url).query))
        state["env_params"] = {
            k: v for k, v in params.items()
            if k not in SIGNATURE_PARAMS and k not in REQUEST_SPECIFIC and k != "WebIdLastTime"
        }
        route_state["env_captured"] = True
        print(f"[Server] {len(state['env_params'])} param lingkungan tertangkap.")
    if route_state["armed"] and (
        "X-Bogus=" in url
        or "slardar" in url
        or "acrawler" in url
        or url.lower().endswith(MEDIA_EXTENSIONS)
    ):
        await route.abort()
        return
    await route.continue_()


async def init_browser():
    """Launch Chromium headless dan siapkan halaman TikTok yang hidup."""
    if state["browser"] or state["playwright"]:
        await close_browser()

    playwright = await async_playwright().start()
    browser = None
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
            ],
        )
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()
        await page.route("**/*", route_handler)  # satu-satunya route — dipasang sekali

        state.update(playwright=playwright, browser=browser, context=context, page=page, ready=False)
        route_state["armed"] = False
        route_state["env_captured"] = False

        # Saat init tidak ada yang di-abort: aborting request selama load membuat
        # SPA mandek (render menggantung menunggu API yang di-abort). Load pertama
        # tidak bisa diandalkan — selalu reload.
        await page.goto(INIT_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await page.reload(wait_until="domcontentloaded")

        # Poll SDK + tangkapan param lingkungan; fallback: halaman depan yang
        # pasti memicu request API bertanda tangan.
        async def page_is_ready():
            if not route_state["env_captured"]:
                return False
            return await page.evaluate(
                "typeof window.byted_acrawler === 'object' && typeof window.byted_acrawler.frontierSign === 'function'"
            )

        ready = False
        for _ in range(20):
            ready = await page_is_ready()
            if ready:
                break
            await asyncio.sleep(1)
        if not ready:
            await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
            await asyncio.sleep(4)
            for _ in range(15):
                ready = await page_is_ready()
                if ready:
                    break
                await asyncio.sleep(1)
        if not ready:
            raise RuntimeError("SDK TikTok / param lingkungan tidak siap — coba lagi atau cek jaringan")

        state["ready"] = True
        state["sign_count"] = 0
        state["last_init"] = time.time()
        print("[Server] Browser siap: SDK terpasang, %d param lingkungan tertangkap." % len(state["env_params"]))
    except Exception:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await playwright.stop()
        except Exception:
            pass
        state.update(playwright=None, browser=None, context=None, page=None, ready=False, env_params={})
        route_state.update(armed=False, env_captured=False)
        raise


async def close_browser():
    browser = state["browser"]
    playwright = state["playwright"]
    state.update(playwright=None, browser=None, context=None, page=None, ready=False, env_params={})
    route_state.update(armed=False, env_captured=False)

    if browser:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright:
        try:
            await playwright.stop()
        except Exception:
            pass


async def ensure_browser_locked():
    """Pastikan browser siap; caller wajib sudah memegang sign_lock."""
    if not state["ready"] or not state["page"]:
        await init_browser()
        return

    age = time.time() - state["last_init"]
    if state["sign_count"] >= MAX_SIGS_BEFORE_REFRESH or age >= MAX_SESSION_AGE_S:
        print(f"[Server] Refresh browser ({state['sign_count']} sign, {age:.0f}s)")
        await close_browser()
        await init_browser()


def prepare_url(raw_url: str) -> str:
    """Siapkan URL untuk ditandatangani:
    - buang tanda tangan lama (X-Bogus/X-Gnarly/msToken/X-Dynosaur)
    - paksa param fingerprint cocok dengan env browser (kalau tidak TikTok
      balas 'url doesn't match')
    - isi param lingkungan sesi yang kurang (device_id, odinId, region, ...)
      — tanpa ini TikTok balas 'missing required fields'."""
    parts = urlsplit(raw_url)
    params = {k: v for k, v in parse_qsl(parts.query, keep_blank_values=True)}
    for key in list(params):
        if key in SIGNATURE_PARAMS:
            del params[key]
    params.update(FINGERPRINT)
    for key, value in state.get("env_params", {}).items():
        params.setdefault(key, value)
    params.setdefault("WebIdLastTime", str(int(time.time())))
    return parts._replace(query=urlencode(params)).geturl()


def make_predicate(cleaned_url: str):
    """Request hasil tangkapan harus request KITA: semua param asli target harus
    ada dengan nilai identik di URL hasil (SDK hanya MENAMBAH param tanda
    tangan). Mencegah tertukar dengan request latar halaman sendiri."""
    ours = dict(parse_qsl(urlsplit(cleaned_url).query))

    def predicate(request):
        if "X-Bogus=" not in request.url:
            return False
        request_params = dict(parse_qsl(urlsplit(request.url).query))
        return all(request_params.get(k) == v for k, v in ours.items())

    return predicate


async def get_cookie_string() -> str:
    cookie_list = await state["context"].cookies()  # cookies ada di context, bukan page
    return "; ".join(f"{c['name']}={c['value']}" for c in cookie_list)


async def _generate_signature_locked(target_url: str) -> dict:
    await ensure_browser_locked()
    page = state["page"]
    cleaned = prepare_url(target_url)
    route_state["armed"] = True
    try:
        try:
            async with page.expect_request(
                make_predicate(cleaned), timeout=SIGN_TIMEOUT_MS
            ) as request_info:
                await page.evaluate(
                    "(url) => fetch(url, { method: 'GET', credentials: 'include', headers: { Accept: '*/*' } }).catch(() => {})",
                    cleaned,
                )
            request_obj = await request_info.value
            signed_url = request_obj.url
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Timeout menunggu URL bertanda tangan — SDK/epoch sesi mati"
            ) from exc
    finally:
        route_state["armed"] = False

    state["sign_count"] += 1
    params = parse_qs(urlsplit(signed_url).query)
    return {
        "signedUrl": signed_url,
        "xBogus": params.get("X-Bogus", [""])[0],
        "xGnarly": params.get("X-Gnarly", [""])[0],
        "xDynosaur": params.get("X-Dynosaur", [""])[0],
        "secUid": params.get("secUid", [""])[0],
        "cursor": params.get("cursor", [""])[0],
        "deviceId": params.get("device_id", [""])[0],
        "userAgent": DEFAULT_UA,
        "cookies": await get_cookie_string(),
    }


async def generate_signature(target_url: str) -> dict:
    """Satu siklus sign; serialisasi diperlukan karena page hanya satu."""
    async with sign_lock:
        return await _generate_signature_locked(target_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_browser()
    except Exception as exc:  # biarkan server hidup untuk /health, tandaikan gagal
        print(f"[Server] Init gagal: {exc}")
    yield
    await close_browser()


app = FastAPI(title="TikTok Signature API (Python Port)", lifespan=lifespan)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


def parse_body(request_body) -> str:
    if isinstance(request_body, str):
        return request_body.strip()
    if isinstance(request_body, dict):
        url = request_body.get("url", "")
        return url.strip() if isinstance(url, str) else ""
    return ""


def require_api_token(request: Request):
    if not API_TOKEN:
        return None

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, API_TOKEN):
        return JSONResponse(
            {"status": "error", "message": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def validate_target_url(raw_url: str):
    """Terima hanya URL HTTPS API TikTok; target bebas adalah blind proxy."""
    try:
        parts = urlsplit(raw_url)
        hostname = parts.hostname
        port = parts.port
    except (AttributeError, ValueError):
        return "", "Invalid URL"

    if (
        parts.scheme.lower() != "https"
        or hostname is None
        or hostname.lower().rstrip(".") not in ALLOWED_TIKTOK_HOSTS
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/api/")
        or parts.fragment
    ):
        return "", "Only HTTPS TikTok API URLs are allowed"
    return raw_url, None


async def get_target(request: Request):
    """Body request → URL target + validasi (shared /signature & /fetch)."""
    auth_error = require_api_token(request)
    if auth_error:
        return "", auth_error

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_url = parse_body(body)
    if not raw_url:
        return "", JSONResponse({"status": "error", "message": "URL is required"}, status_code=400)

    target_url, validation_error = validate_target_url(raw_url)
    if validation_error:
        return "", JSONResponse({"status": "error", "message": validation_error}, status_code=400)
    return target_url, None


@app.post("/signature")
async def signature_endpoint(request: Request):
    target_url, error = await get_target(request)
    if error:
        return error

    try:
        result = await generate_signature(target_url)
    except Exception as exc:
        print(f"[Server] Sign gagal: {exc}")
        # SDK bisa mati di tengah sesi — recycle sekali lalu retry (pola repo asli)
        try:
            async with sign_lock:
                await close_browser()
                await init_browser()
                result = await _generate_signature_locked(target_url)
        except Exception as retry_exc:
            print(f"[Server] Retry sign gagal: {retry_exc}")
            return JSONResponse(
                {"status": "error", "message": "Signature server unavailable"}, status_code=503
            )
    return {"status": "ok", "data": result}


@app.post("/fetch")
async def fetch_endpoint(request: Request):
    """Proxy fetch via browser: URL ditandatangani SDK di dalam halaman, lalu
    benar-benar dikirim (tidak di-abort) dan body JSON-nya dikembalikan.
    Fallback '100% reliable' gaya repo asli — dipakai skrip yang butuh
    respons langsung (mis. pencarian)."""
    target_url, error = await get_target(request)
    if error:
        return error

    async with sign_lock:
        for attempt in range(2):
            try:
                await ensure_browser_locked()
                page = state["page"]
                cleaned = prepare_url(target_url)
                result = await asyncio.wait_for(
                    page.evaluate(
                        """(url) => fetch(url, { method: 'GET', credentials: 'include', headers: { Accept: 'application/json' } })
                            .then(async r => ({ status: r.status, body: await r.text() }))
                            .catch(e => ({ status: 0, body: String(e) }))""",
                        cleaned,
                    ),
                    timeout=45,
                )
                state["sign_count"] += 1
                break
            except asyncio.TimeoutError:
                if attempt:
                    return JSONResponse({"status": "error", "message": "Fetch timeout"}, status_code=504)
                print("[Server] Fetch timeout; recycle browser lalu retry")
            except Exception as exc:
                if attempt:
                    print(f"[Server] Fetch gagal setelah retry: {exc}")
                    return JSONResponse(
                        {"status": "error", "message": "Signature server unavailable"}, status_code=503
                    )
                print(f"[Server] Fetch gagal; recycle browser lalu retry: {exc}")

            try:
                await close_browser()
                await init_browser()
            except Exception as exc:
                print(f"[Server] Recycle fetch gagal: {exc}")
                return JSONResponse(
                    {"status": "error", "message": "Signature server unavailable"}, status_code=503
                )

    if not isinstance(result, dict):
        return JSONResponse({"status": "error", "message": "Invalid browser response"}, status_code=502)

    try:
        http_status = int(result.get("status", 0) or 0)
    except (TypeError, ValueError):
        http_status = 0
    if http_status <= 0:
        return JSONResponse(
            {"status": "error", "message": "TikTok request failed", "httpStatus": http_status},
            status_code=502,
        )

    body = result.get("body", "")
    try:
        data = json.loads(body) if isinstance(body, str) and body else None
    except ValueError:
        data = None

    if not 200 <= http_status < 300:
        error_status = http_status if 400 <= http_status <= 599 else 502
        return JSONResponse(
            {"status": "error", "httpStatus": http_status, "data": data},
            status_code=error_status,
        )
    return {"status": "ok", "httpStatus": http_status, "data": data}


@app.get("/health")
async def health():
    age = (time.time() - state["last_init"]) if state["last_init"] else 0
    ready = bool(state["ready"] and state["page"])
    payload = {
        "status": "ok" if ready else "starting",
        "ready": ready,
        "signCount": state["sign_count"],
        "sessionAgeMinutes": round(age / 60),
        "maxGenerationsBeforeRefresh": MAX_SIGS_BEFORE_REFRESH,
        "maxSessionAgeMinutes": MAX_SESSION_AGE_S / 60,
    }
    return JSONResponse(payload, status_code=200 if ready else 503)


@app.post("/restart")
async def restart(request: Request):
    auth_error = require_api_token(request)
    if auth_error:
        return auth_error

    try:
        async with sign_lock:
            await close_browser()
            await init_browser()
    except Exception as exc:
        print(f"[Server] Restart gagal: {exc}")
        return JSONResponse({"status": "error", "message": "Browser restart failed"}, status_code=503)
    return {"status": "ok", "message": "Browser restarted"}


if __name__ == "__main__":
    uvicorn.run("main:app", port=8080, reload=False)
