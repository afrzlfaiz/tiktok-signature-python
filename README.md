# 🔑 TikTok Signature API — Python Port

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/Framework-FastAPI-green?logo=fastapi&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Chromium-brightgreen?logo=playwright&logoColor=white)
![Uvicorn](https://img.shields.io/badge/Server-Uvicorn-red)
![License](https://img.shields.io/badge/License-MIT-orange)

Server penandatangan (*signature engine*) API TikTok berbasis Python (Porting dari [`carcabot/tiktok-signature`](https://github.com/carcabot/tiktok-signature) Node.js). 

Server ini secara otomatis mengeksekusi SDK `webmssdk.js` resmi TikTok di dalam Chromium Headless untuk meng-generate token `X-Bogus`, `X-Gnarly`, `X-Dynosaur`, `msToken`, dan mengelola cookies sesi—sehingga klien (*scrapers*) dapat melakukan request API TikTok tanpa perlu menjalankan browser sendiri.

---

## 📌 Daftar Isi
- [Cara Kerja](#-cara-kerja)
- [Arsitektur Alur Signature](#-arsitektur-alur-signature)
- [Cara Menjalankan Server](#-cara-menjalankan-server)
- [Dokumentasi API Endpoint](#-dokumentasi-api-endpoint)
  - [`GET /health`](#1-get-health)
  - [`POST /signature`](#2-post-signature)
  - [`POST /fetch`](#3-post-fetch)
  - [`GET /restart`](#4-get-restart)
- [Contoh Kode Klien](#-contoh-kode-klien)
- [Penggunaan Sesi Login (Opsional)](#-penggunaan-sesi-login-opsional)
- [Struktur Project](#-struktur-project)
- [Tabel Konfigurasi](#-tabel-konfigurasi)
- [Batasan & Troubleshooting](#-batasan--troubleshooting)

---

## 💡 Cara Kerja

Pendekatan lama seperti "menginjeksi file `webmssdk.js` versi lokal/vendor" sudah tidak kompatibel dengan bundle TikTok versi terbaru (menyebabkan penandatanganan gagal dan `X-Bogus=1`). 

Porting ini menggunakan mekanisme modern yang teruji:

1. **Inisialisasi Halaman Nyata**: Browser Chromium headless memuat halaman asli `tiktok.com` (`INIT_URL`), yang secara otomatis memuat SDK resmi dari CDN TikTok serta cookies sesi awal.
2. **Penandatanganan Sintetis**: Melalui `page.evaluate(fetch(url))`, hook SDK resmi di dalam halaman menandatangani request fetch sintetis.
3. **Intersepsi Request**: Request yang telah ditandatangani ditangkap via `page.expect_request` lalu segera di-**abort** (dibatalkan) sebelum keluar ke internet, sehingga tidak ada trafik ganda. Param bertanda tangan (`X-Bogus`, `msToken`, dll.) diekstrak dari URL.
4. **Pewarisan Parameter Lingkungan**: Parameter konteks (seperti `device_id`, `odinId`, `region`, dll.) diwarisi langsung dari request API asli TikTok untuk mencegah kegagalan `missing required fields`.
5. **Mode Proxy (`POST /fetch`)**: Request dikirim langsung dari dalam konteks halaman browser tempat SDK aktif, sehingga klien tidak perlu mengelola User-Agent maupun cookies secara manual.

---

## 🏗️ Arsitektur Alur Signature

```mermaid
graph TD
    Client["Klien Scraping / App"] -->|"1. POST /fetch (URL)"| FastAPI["FastAPI Signature Server"]
    FastAPI -->|"2. Inject synthetic fetch"| Page["Chromium Headless Page"]
    Page -->|"3. Trigger webmssdk.js"| SDK["TikTok Web SDK"]
    SDK -->|"4. Generate X-Bogus & msToken"| Request["Signed Request"]
    Page -->|"5. Intercept & Abort / Proxy Fetch"| Request
    Request -->|"6. Execution Result Data"| FastAPI
    FastAPI -->|"7. JSON Response"| Client
```

---

## 🚀 Cara Menjalankan Server

### 1. Install Dependency & Browser
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Jalankan Uvicorn Server
```bash
python -m uvicorn main:app --port 8080
```
> Server akan berjalan secara default di `http://localhost:8080`.

### 3. Cek Status Kesehatan Server
Saat log terminal menunjukkan `[Server] Browser siap`, verifikasi status dengan:
```bash
curl http://localhost:8080/health
# Output: {"status":"ok","ready":true,"sign_count":0,...}
```

---

## 📡 Dokumentasi API Endpoint

### 1. `GET /health`
Mengecek apakah instance browser Playwright siap menerima request penandatanganan.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "ok",
    "ready": true,
    "sign_count": 12,
    "session_age_s": 145
  }
  ```

---

### 2. `POST /signature`
Menandatangani URL API TikTok tanpa mengeksekusi HTTP request-nya. Mengembalikan URL lengkap bertanda tangan beserta User-Agent dan Cookie header.

* **Request Payload**:
  ```json
  {
    "url": "https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=kuliner&count=12"
  }
  ```
* **Response (`200 OK`)**:
  ```json
  {
    "status": "ok",
    "signed_url": "https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=kuliner&count=12&X-Bogus=DFSzswVY...",
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)...",
    "cookies": "ttwid=1%7C...;"
  }
  ```

---

### 3. `POST /fetch` (Rekomendasi)
Menandatangani URL sekaligus mengeksekusi fetch secara langsung dari dalam browser headless, lalu mengembalikan data JSON mentah dari TikTok.

* **Request Payload**:
  ```json
  {
    "url": "https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=kuliner&count=12&search_source=query&type=1&channel=tiktok_web"
  }
  ```
* **Contoh cURL**:
  ```bash
  curl -X POST http://localhost:8080/fetch \
    -H 'Content-Type: application/json' \
    -d '{"url": "https://www.tiktok.com/api/search/general/full/?aid=1988&keyword=kuliner&count=12&search_source=query&type=1&channel=tiktok_web"}'
  ```

---

### 4. `GET /restart`
Merefresh / mendaur ulang instance browser Playwright secara manual jika sesi usang atau SDK bermasalah.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "ok",
    "message": "Browser restarted"
  }
  ```

---

## 💻 Contoh Kode Klien

Contoh penggunaan service dari aplikasi Python menggunakan `aiohttp`:

```python
import asyncio
import aiohttp

async def fetch_tiktok_search(keyword: str, count: int = 12):
    search_url = (
        f"https://www.tiktok.com/api/search/general/full/"
        f"?aid=1988&keyword={keyword}&count={count}&cursor=0&search_source=query&type=1&channel=tiktok_web"
    )
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:8080/fetch",
            json={"url": search_url},
            timeout=aiohttp.ClientTimeout(total=60)
        ) as response:
            return await response.json()

# Jalankan async function
data = asyncio.run(fetch_tiktok_search("kuliner jakarta"))

if data.get("status") == "ok":
    items = data["data"].get("data", [])
    for item in items:
        video_desc = item.get("item", {}).get("desc", "")
        print(f"📌 {video_desc[:60]}...")
```

> 💡 **CLI bawaan**: Tersedia juga script CLI pengetesan interaktif via [`scraper.py`](file:///Users/mac/Documents/Python/GITHUB/tiktok-signature-python/scraper.py): `python scraper.py`.

---

## 🔐 Penggunaannya Sesi Login (Opsional)

Secara default, server berjalan dalam mode **Guest (Tamu)**. Untuk mengakses data yang memerlukan autentikasi (seperti daftar *following*, *likes*, atau profil privat):

> [!WARNING]
> **Penting tentang Keamanan `sessionid`**  
> String `sessionid` merupakan kredensial akses penuh akun Anda. Jaga kerahasiaannya dan jangan di-commit ke repositori publik!

1. Ambil nilai cookie `sessionid` dari browser setelah login ke TikTok Web.
2. Cukup masukkan cookie `sessionid` tersebut saat menginisialisasi sesi (cookie `ttwid` dan `msToken` akan digenerate secara otomatis oleh server).
3. Anda dapat menguji kecocokan cookie dengan script pembantu:
   ```bash
   COOKIE_JAR="sessionid=xxxxxxx..." python test_cookies.py
   ```

---

## 📂 Struktur Project

```text
tiktok-signature-python/
├── main.py            # Entrypoint FastAPI server (Playwright async engine)
├── scraper.py         # Script CLI testing (klien /fetch + export data)
├── test_cookies.py    # Script pengujian cookie sesi login
├── requirements.txt   # Dependency Python (fastapi, uvicorn, playwright)
├── README.md          # Dokumentasi teknis proyek
└── .gitignore         # Rules git ignore
```

---

## ⚙️ Tabel Konfigurasi

Variabel konfigurasi utama pada [`main.py`](file:///Users/mac/Documents/Python/GITHUB/tiktok-signature-python/main.py):

| Parameter | Nilai Default | Keterangan |
| :--- | :--- | :--- |
| `DEFAULT_UA` | Safari macOS (Intel) | User-Agent yang dikunci untuk pembuatan signature |
| `INIT_URL` | `tiktok.com/@zara` | Halaman nyata untuk menginisialisasi hook SDK & cookies |
| `MAX_SIGS_BEFORE_REFRESH` | `500` | Batas maksimum penandatanganan sebelum browser di-recycle |
| `MAX_SESSION_AGE_S` | `1800` (30 menit) | Umur maksimum sesi browser sebelum di-recycle otomatis |
| `SIGN_TIMEOUT_MS` | `8000` (8 detik) | Timeout maksimal menunggu penandatanganan URL |

---

## ❓ Batasan & Troubleshooting

> [!NOTE]
> **Pembatasan Mode Guest (Guest-Gate)**  
> Endpoint seperti `post/item_list` (video profil) mengembalikan HTTP 200 dengan 0 byte data pada sesi Guest. Ini adalah kebijakan pembatasan dari TikTok. Gunakan cookie `sessionid` jika membutuhkan data tersebut.

> [!TIP]
> **Pencarian Memberikan Hasil Pengganti (Fallback Feed)**  
> Sesi tamu terkadang diberi feed fallback secara acak oleh TikTok. Modul `scraper.py` menangani hal ini dengan melakukan retry otomatis hingga 3 kali.

> [!IMPORTANT]
> **Error SSL Certificate di macOS**  
> Jika mengalami `certificate verify failed` saat melakukan fetch eksternal di macOS, jalankan:
> ```bash
> pip install certifi
> ```
