# TikTok Signature API — Port Python

Port Python dari [`carcabot/tiktok-signature`](https://github.com/carcabot/tiktok-signature) (Node.js): server yang menandatangani URL API TikTok dengan mengeksekusi SDK `webmssdk.js` TikTok di dalam Chromium headless, lalu mengembalikan URL bertanda tangan — atau mengambil datanya langsung sebagai proxy.

**Tidak perlu** khawatir soal `X-Bogus`, `X-Gnarly`, `X-Dynosaur`, `msToken`, atau cookies: semua itu dikelola server di dalam browser.

```
Klien ── POST /signature ──> Server (FastAPI + Playwright)
Klien ── POST /fetch    ──>  └─ halaman nyata tiktok.com (headless)
                              └─ SDK halaman menandatangani fetch sintetis
                              └─ URL bertanda tangan → data kembali ke klien
```

## Cara kerja (inti)

Pendekatan lama "injeksi webmssdk.js versi vendor" **sudah tidak kompatibel** dengan bundle TikTok sekarang — SDK lama mematahkan penandatanganan (fetch sintetis jadi `X-Bogus=1`). Port ini memakai trik yang terbukti jalan di epoch sekarang:

1. Browser memuat **halaman nyata** `tiktok.com` (mis. `@zara`), yang menyediakan cookies sesi + hook fetch/XHR halaman sendiri.
2. `page.evaluate(fetch(url))` → hook SDK **halaman** menandatangani fetch sintetis (X-Bogus + X-Gnarly + X-Dynosaur + msToken).
3. Request bertanda tangan itu ditangkap dengan `page.expect_request()` lalu **di-abort** (tidak ada traffic keluar) → URL bertanda tangan diekstrak.
4. Param lingkungan (device_id, odinId, region, tz_name, browser_version, ~28 param lain) **diwarisi dari request API pertama halaman** — tanpa ini TikTok membalas `missing required fields`.
5. `POST /fetch` = varian proxy: URL ditandatangani lalu **benar-benar dikirim** dari dalam halaman, body JSON dikembalikan — klien tak perlu memegang UA/cookies sama sekali.

## Struktur

```
tiktok-signature-python/
├── main.py            # server (FastAPI + Playwright async) — satu file
├── scraper.py         # CLI pencarian TikTok (klien /fetch, export JSON/CSV/XLSX)
├── test_cookies.py    # alat uji: cookie mana yang mengidentifikasi akun login
└── requirements.txt   # fastapi, uvicorn, playwright, aiohttp (scraper)
```

## Instalasi

```bash
pip install -r requirements.txt
playwright install chromium
```

## Menjalankan

```bash
uvicorn main:app --port 8080
```

Log `[Server] Browser siap: ...` berarti SDK + param lingkungan sudah tertangkap. Cek:

```bash
curl localhost:8080/health
# {"status":"ok","ready":true,"signCount":0,"sessionAgeMinutes":0,
#  "maxGenerationsBeforeRefresh":500,"maxSessionAgeMinutes":30.0}
```

## Endpoint

| Endpoint | Deskripsi |
|---|---|
| `POST /signature` | `{"url": ...}` (atau string polos) → URL bertanda tangan + UA + cookies |
| `POST /fetch` | `{"url": ...}` → kirim URL dari dalam browser, balas body JSON (`{"status":"ok","httpStatus":200,"data":{...}}`) |
| `GET /health` | status sesi browser |
| `GET /restart` | recycle browser (SDK mati / sesi usang) |

### Contoh `curl`

```bash
# tanda tangan URL API
curl -X POST localhost:8080/signature -H 'Content-Type: application/json' \
  -d '{"url": "https://www.tiktok.com/api/recommend/item_list/?aid=1988&count=30"}'

# ambil data langsung (paling praktis untuk skrip)
curl -X POST localhost:8080/fetch -H 'Content-Type: application/json' \
  -d '{"url": "https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=makanan&count=12&search_source=query&type=1&channel=tiktok_web"}'
```

Respons `/signature`:

```json
{"status": "ok", "data": {
  "signedUrl": "https://www.tiktok.com/api/...&X-Bogus=...&msToken=...",
  "xBogus": "...", "xGnarly": "...", "xDynosaur": "...",
  "secUid": "", "cursor": "", "deviceId": "...",
  "userAgent": "Mozilla/5.0 (Macintosh; ... Safari/605.1.15)",
  "cookies": "ttwid=...; msToken=..."
}}
```

## Klien Python

```python
import aiohttp, asyncio

async def search(keyword: str, count: int = 12):
    url = ("https://www.tiktok.com/api/search/general/full/?"
           f"aid=1988&keyword={keyword}&count={count}&cursor=0"
           "&search_source=query&type=1&channel=tiktok_web")
    async with aiohttp.ClientSession() as s:
        async with s.post("http://localhost:8080/fetch",
                          json={"url": url}, timeout=aiohttp.ClientTimeout(total=60)) as r:
            return await r.json()

data = asyncio.run(search("makanan"))
for entry in data["data"]["data"]:        # daftar {item: {...}}
    v = entry["item"]
    print(v["id"], v["desc"][:60], v["stats"]["playCount"])
```

Atau pakai CLI jadi-jadian `scraper.py` (pencarian + paginasi + export JSON/CSV/XLSX):

```bash
python3 scraper.py
# masukkan keyword, jumlah video, pilih format export
```

## Sesi login (opsional)

Server berjalan sebagai **tamu**. Untuk data yang butuh login (following, likes, history, profil privat):

- **`sessionid` saja sudah cukup mengidentifikasi akun** — diuji live: dengan `sessionid` saja, halaman masuk mode login dan `post/item_list` mengembalikan data nyata (tamu: 0 byte). `sessionid_ss`, `sid_tt`, dan hash di `sid_guard` adalah salinan nilai yang sama — redundan.
- `ttwid`/`msToken` **tidak perlu disalin** — halaman men-generate sendiri yang segar.
- `passport_csrf_token`/`tt_csrf_token` wajib hanya untuk POST yang mengubah state (like, follow, comment).
- Alat uji: `COOKIE_JAR="...;..." python3 test_cookies.py` — menampilkan subset mana yang masuk mode login (feed `story/user_list` berdata 14 KB vs tamu 0–104 byte).

> ⚠️ `sessionid` adalah kredensial penuh. Jangan sebarkan; rotasi lewat *Settings → Security → log out perangkat lain* bila bocor.

## Batasan yang diketahui

- **Guest-gate TikTok**: `post/item_list` (video profil) mengembalikan `200` dengan **0 byte** untuk sesi tamu — semua jalur (in-page maupun eksternal). Ini pembatasan akun, bukan cacat pipeline. Login (sessionid) menyelesaikannya.
- **Hasil pencarian pengganti**: sesi tamu kadang diberi feed fallback yang tak relevan (mis. konten bola untuk kata kunci "makanan"), dan hasilnya bergilir per-request. `scraper.py` menanganinya dengan retry halaman yang tak relevan (maks. 3×).
- **Fetch eksternal diblokir**: URL bertanda tangan yang di-fetch dari luar browser (requests/aiohttp) kini dibalas `200`/0-byte untuk tamu — gunakan `POST /fetch` (dari dalam browser) sebagai gantinya.
- **macOS Python SSL**: `certificate verify failed` saat fetch eksternal → `pip install certifi` (atau pakai `/fetch`).
- **Refresh otomatis**: browser di-recycle tiap 500 sign atau 30 menit (konstanta di `main.py`).

## Konfigurasi (konstanta di `main.py`)

| Konstanta | Nilai | Arti |
|---|---|---|
| `DEFAULT_UA` | Safari macOS | signature terkunci ke UA ini |
| `INIT_URL` | `tiktok.com/@zara` | halaman init (cookies + hook SDK) |
| `MAX_SIGS_BEFORE_REFRESH` | 500 | recycle browser setelah N sign |
| `MAX_SESSION_AGE_S` | 1800 | recycle browser setelah N detik |
| `SIGN_TIMEOUT_MS` | 8000 | timeout tunggu URL bertanda tangan |
| `FINGERPRINT` | MacIntel/mac/1920×1080 | harus cocok dengan env browser |
