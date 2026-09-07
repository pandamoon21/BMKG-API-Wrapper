# bmkgaw — Info BMKG API wrapper

Unofficial Python wrapper for **BMKG** (Badan Meteorologi, Klimatologi, dan
Geofisika) data — cuaca, gempabumi/tsunami, maritim, bandara, kualitas udara,
peringatan dini (nowcast). Pola sama dengan
[MDL-API-Wrapper](https://github.com/pandamoon21/MDL-API-Wrapper) (`mdlaw`):
satu file FastAPI + httpx, cache TTL, throttle, `/docs` Swagger, library
async, CLI, dan self-check offline.

- Paket PyPI: **`bmkgaw`**
- Repo: **`BMKG-API-Wrapper`**

## Data sources

| # | Sumber | Host | Auth | Rate limit |
|---|--------|------|------|-----------|
| 1 | **App API** Info BMKG Android v3.5.1 (`com.Info_BMKG`) | `api-apps.bmkg.go.id` | token (`df/v1`, marine) | — |
| 2 | **Open data** prakiraan cuaca resmi | `api.bmkg.go.id/publik/prakiraan-cuaca` | — | 60 req/menit/IP |
| 3 | **Open data** gempabumi & tsunami (TEWS) | `data.bmkg.go.id/DataMKG/TEWS/*` | — | 60 req/menit/IP |
| 4 | **Open data** peringatan dini CAP/nowcast | `www.bmkg.go.id/alerts/nowcast/{id\|en}` | — | 60 req/menit/IP |
| 5 | Feed InaTEWS GCS (`live30event.xml`, CAP) | GCS feed host | — | — |

Endpoint `df/v1/*` + maritim (sumber #1) butuh token statis `API_LOCK` yang
dikirim app. Token **tidak di-commit** — set `BMKGAW_TOKEN` via env (lihat
`.env.example`). Tanpa token, `source=auto` tetap jalan ke open data web.

> **Atribusi:** wajib mencantumkan BMKG sebagai sumber data (syarat portal
> terbuka resmi). Proyek ini **tidak berafiliasi dengan BMKG**; data mentah
> milik BMKG, kode wrapper ini MIT.

## Fitur

- **33 route API** — meta, cuaca, bandara, maritim, lingkungan, bahaya, gempa, open data.
- **Sumber ganda + pemilihan** (`?source=auto|app|web`) untuk endpoint auth-gated.
- **Respons heterogen di-parse typed**: JSON passthrough, XML→JSON
  (Infogempa + CAP 1.2), binary/image passthrough.
- **Cache TTL** memory atau SQLite + deteksi perubahan (sha256) ala mdlaw.
- **Throttle global 1 req/detik** outbound → aman di bawah 60 req/menit/IP.
- **Library async** (`BMKG`) + **CLI** (`bmkgaw …`) + **self-check offline**.
- **Deploy siap pakai**: Docker, Compose, Fly.io, Vercel, systemd.
- **Dokumentasi interaktif**: Swagger UI `/docs`, ReDoc `/redoc`, OpenAPI `/openapi.json`.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# token untuk endpoint df/v1 + marine (dari .env APK Info BMKG).
# TANPA token pun tetap jalan: source=auto otomatis pakai web open-data.
# export BMKGAW_TOKEN='Bearer …'    # placeholder — jangan commit token asli

uvicorn bmkgaw:app --port 8000
# → http://localhost:8000/docs
```

### Verify

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/earthquake/latest
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001&source=web"

python bmkgaw.py self        # offline self-check
python -m pytest tests/ -q   # tes offline
```

## Environment

| Var | Perlu | Default | Keterangan |
|---|---|---|---|
| `BMKGAW_TOKEN` | endpoint app `df/v1` + marine | — (kosong) | tanpa ini `auto` → web; isi dari `API_LOCK` APK |
| `BMKGAW_AGENT` | tidak | `infobmkg` | nama agent app (header `AGENT`) |
| `BMKGAW_CACHE_BACKEND` | tidak | `memory` | `memory` atau `sqlite` |
| `BMKGAW_CACHE_DB_URL` | jika `sqlite` | `sqlite:///bmkgaw_cache.db` | lokasi db |

Token (`API_LOCK`) adalah kredensial **statis yang dikirim app** — shared
client credential, bukan rahasia per-user. Route auth mengirimnya via header
`Authorization` (atau `x-api-key-infobmkg` / `x-api-key` / `apikey` — semua
diterima server). Token **tidak di-commit** — isi `BMKGAW_TOKEN` di env lokal
(dari `API_LOCK` di APK Info BMKG) kalau mau pakai endpoint app df/v1 + marine.

## Pilih sumber data (`?source=`)

Endpoint auth-gated (app df/v1 + marine) menerima param query `source`:

| `source` | Perilaku |
|---|---|
| `auto` (default) | token ada → **app** (API riset); token kosong → **web** (open data) |
| `app` | paksa API riset (`api-apps.bmkg.go.id`) — tanpa token → 400 |
| `web` | paksa open data publik — tanpa token; 400 jika endpoint tak punya padanan web |

Hanya **forecast** yang punya padanan web resmi
(`/publik/prakiraan-cuaca?adm4=…`, live-verified). Endpoint tanpa padanan web
(`present`, `area/search`, `area/coord`, `marine forecast`) → `source=web`
ditolak 400, dan `auto` tanpa token → 400 minta token.

```bash
# tanpa token: auto → web (open data)
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001"
# paksa app (butuh BMKGAW_TOKEN)
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001&source=app"
# paksa web
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001&source=web"
```

## Endpoints (`/api/v1`)

| Route | Sumber | Token | TTL | Catatan |
|---|---|---|---|---|
| `GET /health` | — | — | — | liveness |
| `GET /cache/stats` · `GET /dashboard` | — | — | — | cache + info |
| `GET /weather/forecast?adm4=…` atau `?lat=&lon=` | app **atau** web | ✅/— dinamis | 300 | prakiraan; `source` |
| `GET /weather/present?adm4=…` | app | ✅ | 120 | cuaca saat ini (no web) |
| `GET /weather/legacy?lat=&lon=` | app | — | 300 | ⚠️ deprecated (cuaca kosong) |
| `GET /area/search?q=` | app | ✅ | 60 | ⚠️ search rusak server (ignore `q`) |
| `GET /area/coord?lat=&lon=` | app | ✅ | 86400 | reverse geocode → adm1–adm4 |
| `GET /airports` | app | — | 300 | semua stasiun bandara (111) |
| `GET /airports/{icao}` | app | — | 300 | detail 1 bandara (METAR/TAF) |
| `GET /marine/forecast` | app | ✅ | 1800 | raw ~1.5 MB (`fct_code` codec) |
| `GET /marine/waves` | app | — | 1800 | kategori gelombang per area |
| `GET /air-quality` | app | — | 600 | PM2.5 |
| `GET /satellite` · `GET /satellite/image/{file}` | app | — | 120/600 | Himawari-8 |
| `GET /cb?lat=&lon=` | app | — | 3600 | prakiraan awan CB |
| `GET /warnings` · `GET /warnings/{lokasi}` | app | — | 120 | peringatan dini (404 = tidak ada) |
| `GET /press` | app | — | 3600 | siaran pers |
| `GET /radar` · `GET /radar/image` | app | — | 300 | ⚠️ 403 server-side (2026-09-03) |
| `GET /earthquake/latest` | tews | — | 60 | gempa terbaru (resmi) |
| `GET /earthquake/recent` | tews | — | 120 | 15 gempa M≥5 |
| `GET /earthquake/felt` | tews | — | 120 | 15 gempa terasa |
| `GET /earthquake/shakemap/{file}` | tews | — | 86400 | gambar shakemap (bytes) |
| `GET /earthquake/live` | gcs | — | 60 | buffer ~3 hari (XML→JSON) |
| `GET /earthquake/last30[/felt\|/tsunami]` | gcs | — | 120 | CAP 1.2 → JSON |
| `GET /earthquake/warning` | gcs | — | 60 | warning geo/tsunami aktif |
| `GET /publik/weather?adm4=…` | publik | — | 300 | prakiraan resmi open-data |
| `GET /nowcast/{id\|en}` | web | — | 60 | RSS peringatan dini provinsi |
| `GET /nowcast/{id\|en}/{kode}` | web | — | 300 | detail CAP per kecamatan |

`tews` = `data.bmkg.go.id/DataMKG/TEWS`, `gcs` = GCS InaTEWS, `publik` =
`api.bmkg.go.id`, `web` = `www.bmkg.go.id`. Feed GCS + nowcast di-parse
XML→JSON otomatis.

### ⚠️ Endpoint yang error / bermasalah (hasil validasi live 2026-09-08)

Sebagian route tetap terdaftar karena endpoint-nya nyata dari riset APK, tapi
**error-nya berasal dari server BMKG**, bukan bug wrapper. Wrapper mengembalikan
status upstream apa adanya + pesan jelas (bukan hang/500 internal).

| Route | Status (live) | Arti / penyebab |
|---|---|---|
| `GET /weather/legacy?lat=&lon=` | `200` tapi `[]` | deprecated — server kembalikan daftar kosong |
| `GET /area/search?q=` | `200` tapi statis | bug server: `q` diabaikan, selalu daftar Kemayoran — pakai `/area/coord` |
| `GET /warnings/{lokasi}` | `404` | **normal** — tidak ada peringatan aktif untuk kode itu; cek `/warnings` dulu untuk daftar `ID_Kode` valid |
| `GET /radar` · `GET /radar/image` | `403` | nonaktif server-side BMKG (sejak 2026-09-03) — `radar*` di-`Forbidden`; coba `?lat=&lon=` / `?radar=` tetap 403 |
| `GET /earthquake/warning` | `200` (bisa kosong) | kosong = tidak ada warning geo/tsunami aktif (valid) |

Tambahan (upstream, bukan route wrapper): `translate` → `500`,
`warningcuaca/{loc}` app → `404`, `radar.bmkg.go.id` & `mhews.id` → NXDOMAIN,
`nowcasting.bmkg.go.id` → `523`.

### Contoh respons (live-captured)

`GET /api/v1/earthquake/latest`:

```json
{
  "Infogempa": {
    "gempa": {
      "Tanggal": "07 Sep 2026", "Jam": "19:50:39 WIB",
      "DateTime": "2026-09-07T12:50:39+00:00",
      "Coordinates": "-8.34,121.30", "Lintang": "8.34 LS", "Bujur": "121.30 BT",
      "Magnitude": "4.0", "Kedalaman": "10 km",
      "Wilayah": "Pusat gempa berada di laut 37 km Utara Mbay Nagekeo",
      "Potensi": "Gempa ini dirasakan untuk diteruskan pada masyarakat",
      "Dirasakan": "II-III Kabupaten Nagekeo, II - III Kabupaten Ngada",
      "Shakemap": "20260907195039.mmi.jpg"
    }
  }
}
```

`GET /api/v1/publik/weather?adm4=31.71.03.1001` (open data, tanpa token):

```json
{
  "lokasi": {
    "adm1": "31", "adm2": "31.71", "adm3": "31.71.03", "adm4": "31.71.03.1001",
    "provinsi": "DKI Jakarta", "kotkab": "Kota Adm. Jakarta Pusat",
    "kecamatan": "Kemayoran", "desa": "Kemayoran",
    "lon": 106.8453837867, "lat": -6.1647214778, "timezone": "Asia/Jakarta"
  },
  "data": [ { "lokasi": { "...": "..." }, "cuaca": [ { "...": "..." } ] } ]
}
```

## Library (async)

```python
import asyncio, json
from bmkgaw import BMKG

async def main():
    b = BMKG()                          # baca BMKGAW_TOKEN dari env (opsional)
    g  = await b.quake_latest()         # gempa terbaru (TEWS, tanpa token)
    f  = await b.forecast(adm4="31.71.03.1001")                 # auto: token→app, tanpa→web
    fw = await b.forecast(adm4="31.71.03.1001", source="web")   # paksa open data
    p  = await b.present(adm4="31.71.03.1001")                  # butuh token
    pw = await b.publik_weather(adm4="31.71.03.1001")           # open data resmi
    n  = await b.nowcast("id")          # XML→JSON
    print(json.dumps(g, ensure_ascii=False)[:300])
    print("cache:", b.stats())
    await b.close()

asyncio.run(main())
```

Metode `BMKG`:

| Metode | Route | Token |
|---|---|---|
| `forecast(adm4=…|lat=,lon=…, source=)` | `/weather/forecast` | ✅/— |
| `present(adm4, source=)` | `/weather/present` | ✅ |
| `area_coord(lat, lon, source=)` | `/area/coord` | ✅ |
| `airports()` / `airport_detail(icao)` | `/airports[/{icao}]` | — |
| `marine_forecast(source=)` / `marine_waves()` | `/marine/…` | ✅/— |
| `air_quality()` / `satellite()` / `cb(lat,lon)` | `/air-quality` `/satellite` `/cb` | — |
| `warnings()` / `press()` | `/warnings` `/press` | — |
| `quake_latest()` `quake_recent()` `quake_felt()` | `/earthquake/*` TEWS | — |
| `quake_live()` `quake_last30()` `quake_warning()` | `/earthquake/*` GCS | — |
| `publik_weather(adm4)` | `/publik/weather` | — |
| `nowcast(lang="id")` | `/nowcast/{lang}` | — |
| `stats()` | — | — |

## CLI

```bash
python bmkgaw.py health
python bmkgaw.py weather 31.71.03.1001        # atau 'lat,lon'; + --source app|web|auto
python bmkgaw.py quake-latest | quake-recent | quake-live
python bmkgaw.py nowcast id
python bmkgaw.py airports
python bmkgaw.py self                          # self-check offline
```

Setelah `pip install .` (atau `pip install -e .`), perintahnya jadi `bmkgaw`
langsung dari shell — sama seperti `mdlaw`.

## Cache

- **`memory`** (default): dict TTL dalam proses.
- **`sqlite`**: persisten lintas-restart, cocok untuk server.

```bash
export BMKGAW_CACHE_BACKEND=sqlite
export BMKGAW_CACHE_DB_URL=sqlite:////data/bmkgaw_cache.db
```

Setiap entri menyimpan `sha256` isi respons; saat TTL kedaluwarsa lalu konten
berubah, flag `changed=1` (untuk polling perubahan, mis. gempa / feed
peringatan dini). Inspeksi: `curl :8000/api/v1/cache/stats`.

## Rate limit & throttle

BMKG open-data membatasi **60 req/menit/IP**. Wrapper memakai throttle global
**1 req/detik** outbound (shared `asyncio.Lock` di seluruh host + route),
jadi pemakaian normal jauh di bawah batas. **Jangan hapus throttle/cache** —
itu pelindung dari WAF & rate limit. Cache per-endpoint (TTL 60–86400 dtk)
mengurangi request berulang.

## Error handling

Wrapper meneruskan kegagalan upstream sebagai HTTP error yang jelas
(mengikuti pola `_upstream_error` mdlaw), bukan 500 internal:

| Kode | Arti |
|---|---|
| `400` | param salah / `source` tak valid / butuh token |
| `401`/`403` | token salah / diblokir WAF (cek UA + Referer) |
| `404` | tidak ada data (`/warnings/{lokasi}`, `warningcuaca/{loc}`) |
| `429` | kena rate limit — throttle mencegah ini |
| `501`/`502` | endpoint upstream mati/deprecated server-side |

Endpoint yang **mati server-side (2026-09-03)** tetap terdaftar tapi
mengembalikan error jelas dari upstream, bukan hang/500: `adm/search`
(ignore `q`), `radar*` (403), `translate` (500), `warningcuaca/{loc}` (404),
`radar.bmkg.go.id` & `mhews.id` (NXDOMAIN), `nowcasting.bmkg.go.id` (523).

## Catatan teknis (dari riset)

- **XML dua schema gempa:** `live30event.xml` = `<Infogempa>` polos; feed CAP
  (`last30*`, `warninggeof`) = `<alert>` CAP 1.2. Dikenali otomatis.
- **`data.bmkg.go.id` butuh browser UA + `Referer`** (WAF) — sudah di-set.
- **Ikon cuaca bisa mengandung spasi** (`berawan tebal-am.svg`) → URL-encode.
- Marine `forecast-raw` memakai codec `fct_code` (algoritme decode tidak
  terdokumentasi) → v1 mem-proxy raw; decode menyusul.

## Deploy

### Docker

```bash
docker build -t bmkgaw .
docker run --rm -p 8000:8000 -e BMKGAW_TOKEN='Bearer …' bmkgaw
```

### Docker Compose

```bash
cp .env.example .env     # isi BMKGAW_TOKEN (opsional)
docker compose up -d     # healthcheck bawaan
```

### Fly.io

```bash
fly launch --no-deploy
# uncomment [mounts] + set sqlite di fly.toml untuk cache persisten
fly deploy
```

### Vercel

```bash
vercel deploy            # pakai vercel.json + api/index.py (ASGI)
```

### systemd (VPS)

```ini
[Unit]
Description=bmkgaw API
After=network-online.target

[Service]
WorkingDirectory=/opt/BMKG-API-Wrapper
EnvironmentFile=/opt/BMKG-API-Wrapper/.env
ExecStart=/opt/BMKG-API-Wrapper/.venv/bin/uvicorn bmkgaw:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp bmkgaw.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now bmkgaw
```

## Project layout

```
bmkgaw.py            # seluruh wrapper: FastAPI app + BMKG client + CLI + self-check
tests/test_bmkgaw.py # tes offline (pytest)
api/index.py         # entrypoint Vercel
Dockerfile           # docker
docker-compose.yml   # compose + healthcheck
fly.toml             # fly.io
vercel.json          # vercel
pytest.ini
requirements.txt     # fastapi==0.141.1, uvicorn[standard]==0.52.4, httpx==0.28.1
pyproject.toml       # PyPI: bmkgaw (console script)
.env.example         # token placeholder (aman untuk publik)
.gitignore / .dockerignore
CHANGELOG.md / LICENSE (MIT)
```

## Tes

```bash
pip install pytest        # dev-only
python -m pytest tests/ -q
python bmkgaw.py self     # self-check offline tanpa pytest
```

## Keamanan

- Token **tidak di-commit**. `.env.example` hanya placeholder; isi `BMKGAW_TOKEN`
  via env / file `.env` lokal (di-`.gitignore`).
- Header auth dikirim **hanya** ke host `api-apps.bmkg.go.id` (app API) — tidak
  pernah ke host open-data.
- Non-root user di Docker; healthcheck bawaan.

## Disclaimer

Data © BMKG. Wrapper ini tidak berafiliasi dengan BMKG dan tidak resmi.
Gunakan sesuai syarat portal terbuka resmi BMKG; sertakan atribusi BMKG.
