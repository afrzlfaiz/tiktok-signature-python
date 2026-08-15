# TikTok Signature API — Port Python

Port Python dari [`carcabot/tiktok-signature`](https://github.com/carcabot/tiktok-signature) (Node.js): server yang menandatangani URL API TikTok dengan mengeksekusi SDK `webmssdk.js` TikTok di dalam Chromium headless. `X-Bogus`, `X-Gnarly`, `X-Dynosaur`, `msToken`, dan cookies dikelola server — klien tidak perlu tahu apa pun.

## Cara kerja

Pendekatan lama "injeksi webmssdk.js versi vendor" sudah tidak kompatibel dengan bundle TikTok sekarang (SDK lama mematahkan penandatanganan — fetch sintetis jadi `X-Bogus=1`). Port ini memakai trik yang terbukti jalan di epoch sekarang:

1. Browser memuat **halaman nyata** `tiktok.com` (`INIT_URL`), yang menyediakan cookies sesi + hook fetch/XHR halaman.
2. `page.evaluate(fetch(url))` → hook SDK **halaman** menandatangani fetch sintetis.
3. Request bertanda tangan ditangkap (`page.expect_request`) lalu **di-abort** — tidak ada traffic keluar; URL bertanda tangan diekstrak.
4. Param lingkungan (device_id, odinId, region, ~28 param lain) **diwarisi dari request API pertama halaman** — tanpa ini TikTok balas `missing required fields`.
5. `POST /fetch` = proxy: URL ditandatangani lalu benar-benar dikirim dari dalam halaman; klien tidak perlu memegang UA/cookies.

## Menjalankan

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --port 8080
```

Log `[Server] Browser siap: ...` berarti SDK + param lingkungan tertangkap. Cek `curl localhost:8080/health` → `{"status":"ok","ready":true,...}`.

## Endpoint

| Endpoint | Deskripsi |
|---|---|
| `POST /signature` | `{"url": ...}` (atau string polos) → URL bertanda tangan + UA + cookies |
| `POST /fetch` | kirim URL dari dalam browser → body JSON (`{"status":"ok","httpStatus":200,"data":{...}}`) |
| `GET /health` | status sesi browser |
| `GET /restart` | recycle browser (SDK mati / sesi usang) |

```bash
curl -X POST localhost:8080/fetch -H 'Content-Type: application/json' \
  -d '{"url": "https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=makanan&count=12&search_source=query&type=1&channel=tiktok_web"}'
```

## Struktur

```
main.py            # server (FastAPI + Playwright async) — satu file
scraper.py         # CLI pencarian: klien /fetch + export JSON/CSV/XLSX
test_cookies.py    # alat uji: cookie mana yang mengidentifikasi akun login
requirements.txt
```

## Klien

```python
import aiohttp, asyncio

async def search(keyword: str, count: int = 12):
    url = (f"https://www.tiktok.com/api/search/general/full/?aid=1988&keyword={keyword}"
           f"&count={count}&cursor=0&search_source=query&type=1&channel=tiktok_web")
    async with aiohttp.ClientSession() as s:
        async with s.post("http://localhost:8080/fetch", json={"url": url},
                          timeout=aiohttp.ClientTimeout(total=60)) as r:
            return await r.json()

data = asyncio.run(search("makanan"))
for entry in data["data"]["data"]:
    print(entry["item"]["desc"][:60])
```

Atau CLI jadi-jadian: `python3 scraper.py` (interaktif: keyword → jumlah video → format export).

## Sesi login (opsional)

Server jalan sebagai **tamu**; data yang butuh login (following, likes, profil privat) memerlukan `sessionid`:

- **`sessionid` saja sudah cukup mengidentifikasi akun** (diuji live: feed masuk mode login dan `post/item_list` berisi data nyata; tamu: 0 byte). `sessionid_ss`, `sid_tt`, dan hash di `sid_guard` adalah salinan nilai yang sama — redundan.
- `ttwid`/`msToken` **tidak perlu disalin** — halaman generate sendiri yang segar.
- `passport_csrf_token`/`tt_csrf_token` wajib hanya untuk POST yang mengubah state (like, follow, comment).
- Uji subset cookie mana yang masuk mode login: `COOKIE_JAR="...;..." python3 test_cookies.py`

> ⚠️ `sessionid` adalah kredensial penuh. Jangan sebarkan; rotasi lewat *Settings → Security → log out perangkat lain* bila bocor.

## Batasan yang diketahui

- **Guest-gate**: `post/item_list` (video profil) balas `200` dengan 0 byte untuk sesi tamu — pembatasan akun, bukan cacat pipeline. Login menyelesaikannya.
- **Hasil pengganti**: sesi tamu kadang diberi feed fallback yang tak relevan, bergilir per-request; `scraper.py` menanganinya dengan retry halaman tak relevan (maks. 3×).
- **Fetch eksternal diblokir**: URL bertanda tangan yang di-fetch dari luar browser balas `200`/0-byte untuk tamu — pakai `POST /fetch`.
- **macOS Python SSL**: `certificate verify failed` saat fetch eksternal → `pip install certifi`.
- **Refresh otomatis**: browser di-recycle tiap `MAX_SIGS_BEFORE_REFRESH` (500) sign atau `MAX_SESSION_AGE_S` (1800 s).

## Konfigurasi (konstanta di `main.py`)

| Konstanta | Nilai | Arti |
|---|---|---|
| `DEFAULT_UA` | Safari macOS | signature terkunci ke UA ini |
| `INIT_URL` | `tiktok.com/@zara` | halaman init (cookies + hook SDK) |
| `MAX_SIGS_BEFORE_REFRESH` | 500 | recycle browser setelah N sign |
| `MAX_SESSION_AGE_S` | 1800 | recycle browser setelah N detik |
| `SIGN_TIMEOUT_MS` | 8000 | timeout tunggu URL bertanda tangan |
| `FINGERPRINT` | MacIntel/mac/1920×1080 | harus cocok dengan env browser |
