# bmkgaw — Info BMKG API wrapper

**English** · [Bahasa Indonesia](README.id.md)

An unofficial Python wrapper for **BMKG** (Badan Meteorologi, Klimatologi, dan
Geofisika) data — weather, earthquakes/tsunami, marine, airports, air quality,
and early warnings (nowcast). It exposes weather, earthquake/tsunami, marine,
airport, air-quality, and warning data as a typed REST API, an async Python
library, and a CLI — with a TTL cache, outbound throttling, Swagger `/docs`,
and an offline self-check.

- PyPI package: **`bmkgaw`**
- Repo: **`BMKG-API-Wrapper`**

## Data sources

| # | Source | Host | Auth | Rate limit |
|---|--------|------|------|-----------|
| 1 | **App API** Info BMKG Android v3.5.1 (`com.Info_BMKG`) | `api-apps.bmkg.go.id` | token (`df/v1`, marine) | — |
| 2 | **Open data** official weather forecast | `api.bmkg.go.id/publik/prakiraan-cuaca` | — | 60 req/min/IP |
| 3 | **Open data** earthquakes & tsunami (TEWS) | `data.bmkg.go.id/DataMKG/TEWS/*` | — | 60 req/min/IP |
| 4 | **Open data** CAP/nowcast warnings | `www.bmkg.go.id/alerts/nowcast/{id\|en}` | — | 60 req/min/IP |
| 5 | InaTEWS GCS feeds (`live30event.xml`, CAP) | GCS feed host | — | — |

The `df/v1/*` + marine endpoints (source #1) need the static `API_LOCK` token
the app ships. The token is **not committed** — set `BMKGAW_TOKEN` in the
environment (see `.env.example`). Without a token, `source=auto` falls back to
the public open-data web API.

> **Attribution:** BMKG must be credited as the data source (required by their
> open-data terms). This project is **not affiliated with BMKG**; the raw data
> belongs to BMKG, the wrapper code is MIT.

## Features

- **33 API routes** — meta, weather, airports, marine, environment, hazards, earthquakes, open data.
- **Dual source + selection** (`?source=auto|app|web`) on auth-gated endpoints.
- **Typed responses**: JSON passthrough, XML→JSON (Infogempa + CAP 1.2), binary/image passthrough.
- **TTL cache** — in-memory or SQLite, with sha256 change detection.
- **Global 1 req/s throttle** outbound — safely under BMKG's 60 req/min/IP.
- **Async library** (`BMKG`) + **CLI** (`bmkgaw …`) + **offline self-check**.
- **Deploy-ready**: Docker, Compose, Fly.io, Vercel, systemd.
- **Interactive docs**: Swagger UI `/docs`, ReDoc `/redoc`, OpenAPI `/openapi.json`.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Token for df/v1 + marine endpoints (from the Info BMKG APK's .env).
# Optional — without it, source=auto falls back to the web open-data API.
# export BMKGAW_TOKEN='Bearer …'

uvicorn bmkgaw:app --port 8000
# → http://localhost:8000/docs
```

### Verify

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/earthquake/latest
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001&source=web"

python bmkgaw.py self        # offline self-check
python -m pytest tests/ -q   # offline tests
```

## Environment

| Var | Needed | Default | Notes |
|---|---|---|---|
| `BMKGAW_TOKEN` | app `df/v1` + marine endpoints | — (empty) | without it `auto` → web; fill from the APK's `API_LOCK` |
| `BMKGAW_AGENT` | no | `infobmkg` | agent name sent to the app API (`AGENT` header) |
| `BMKGAW_CACHE_BACKEND` | no | `memory` | `memory` or `sqlite` |
| `BMKGAW_CACHE_DB_URL` | if `sqlite` | `sqlite:///bmkgaw_cache.db` | database location |

The token (`API_LOCK`) is a **static shared credential** the app sends — a
client credential, not a per-user secret. Auth routes send it via the
`Authorization` header (or `x-api-key-infobmkg` / `x-api-key` / `apikey` — the
server accepts all of them). The token is **never committed**; set
`BMKGAW_TOKEN` in your local env (from the `API_LOCK` line in the Info BMKG
APK) if you want the app df/v1 + marine endpoints.

## Source selection (`?source=`)

Auth-gated endpoints (app df/v1 + marine) accept a `source` query param:

| `source` | Behavior |
|---|---|
| `auto` (default) | token set → **app** (app API); no token → **web** (open data) |
| `app` | force the app API (`api-apps.bmkg.go.id`) — no token → 400 |
| `web` | force public open data — no token needed; 400 if the endpoint has no web equivalent |

Only **forecast** has an official web equivalent
(`/publik/prakiraan-cuaca?adm4=…`, live-verified). Endpoints without a web
equivalent (`present`, `area/search`, `area/coord`, `marine forecast`) →
`source=web` is rejected with 400, and `auto` without a token → 400 asking for
a token.

```bash
# no token: auto → web (open data)
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001"
# force app (needs BMKGAW_TOKEN)
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001&source=app"
# force web
curl "http://localhost:8000/api/v1/weather/forecast?adm4=31.71.03.1001&source=web"
```

## Endpoints (`/api/v1`)

| Route | Source | Token | TTL | Notes |
|---|---|---|---|---|
| `GET /health` | — | — | — | liveness |
| `GET /cache/stats` · `GET /dashboard` | — | — | — | cache + info |
| `GET /weather/forecast?adm4=…` or `?lat=&lon=` | app **or** web | ✅/— dynamic | 300 | forecast; `source` |
| `GET /weather/present?adm4=…` | app | ✅ | 120 | current weather (no web) |
| `GET /weather/legacy?lat=&lon=` | app | — | 300 | ⚠️ deprecated (empty payload) |
| `GET /area/search?q=` | app | ✅ | 60 | ⚠️ server ignores `q` |
| `GET /area/coord?lat=&lon=` | app | ✅ | 86400 | reverse geocode → adm1–adm4 |
| `GET /airports` | app | — | 300 | all airport stations (111) |
| `GET /airports/{icao}` | app | — | 300 | 1 airport detail (METAR/TAF) |
| `GET /marine/forecast` | app | ✅ | 1800 | raw ~1.5 MB (`fct_code` codec) |
| `GET /marine/waves` | app | — | 1800 | wave categories per area |
| `GET /air-quality` | app | — | 600 | PM2.5 |
| `GET /satellite` · `GET /satellite/image/{file}` | app | — | 120/600 | Himawari-8 |
| `GET /cb?lat=&lon=` | app | — | 3600 | CB cloud forecast |
| `GET /warnings` · `GET /warnings/{location}` | app | — | 120 | warnings (404 = none) |
| `GET /press` | app | — | 3600 | press releases |
| `GET /radar` · `GET /radar/image` | app | — | 300 | ⚠️ 403 server-side (2026-09-03) |
| `GET /earthquake/latest` | tews | — | 60 | latest quake (official) |
| `GET /earthquake/recent` | tews | — | 120 | 15 quakes M≥5 |
| `GET /earthquake/felt` | tews | — | 120 | 15 felt quakes |
| `GET /earthquake/shakemap/{file}` | tews | — | 86400 | shakemap image (bytes) |
| `GET /earthquake/live` | gcs | — | 60 | ~3-day buffer (XML→JSON) |
| `GET /earthquake/last30[/felt\|/tsunami]` | gcs | — | 120 | CAP 1.2 → JSON |
| `GET /earthquake/warning` | gcs | — | 60 | active geo/tsunami warning |
| `GET /publik/weather?adm4=…` | publik | — | 300 | official open-data forecast |
| `GET /nowcast/{id\|en}` | web | — | 60 | provincial warning RSS |
| `GET /nowcast/{id\|en}/{code}` | web | — | 300 | per-district CAP detail |

`tews` = `data.bmkg.go.id/DataMKG/TEWS`, `gcs` = GCS InaTEWS, `publik` =
`api.bmkg.go.id`, `web` = `www.bmkg.go.id`. GCS + nowcast feeds are parsed
XML→JSON automatically.

### ⚠️ Errored / problematic endpoints (live validation, 2026-09-08)

These routes are kept because the endpoints are real (reverse-engineered from the Info BMKG Android app), but
the **errors come from BMKG's servers**, not from wrapper bugs. The wrapper
passes upstream status through with a clear message (no hang / internal 500).

| Route | Status (live) | Meaning / cause |
|---|---|---|
| `GET /weather/legacy?lat=&lon=` | `200` but `[]` | deprecated — server returns an empty list |
| `GET /area/search?q=` | `200` but static | server bug: `q` ignored, always returns the Kemayoran list — use `/area/coord` |
| `GET /warnings/{location}` | `404` | **normal** — no active warning for that code; check `/warnings` for valid `ID_Kode`s first |
| `GET /radar` · `GET /radar/image` | `403` | disabled server-side by BMKG (since 2026-09-03) — `radar*` is `Forbidden`; `?lat=&lon=` / `?radar=` still 403 |
| `GET /earthquake/warning` | `200` (may be empty) | empty = no active geo/tsunami warning (valid) |

Additional (upstream, not wrapper routes): `translate` → `500`,
`warningcuaca/{loc}` app → `404`, `radar.bmkg.go.id` & `mhews.id` → NXDOMAIN,
`nowcasting.bmkg.go.id` → `523`.

### Example responses (live-captured)

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

`GET /api/v1/publik/weather?adm4=31.71.03.1001` (open data, no token):

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

Note: field names in responses are Indonesian because they come straight from
BMKG's own APIs — that's upstream data, kept as-is.

## Library (async)

```python
import asyncio, json
from bmkgaw import BMKG

async def main():
    b = BMKG()                          # reads BMKGAW_TOKEN from env (optional)
    g  = await b.quake_latest()         # latest quake (TEWS, no token)
    f  = await b.forecast(adm4="31.71.03.1001")                 # auto: token→app, none→web
    fw = await b.forecast(adm4="31.71.03.1001", source="web")   # force open data
    p  = await b.present(adm4="31.71.03.1001")                  # needs token
    pw = await b.publik_weather(adm4="31.71.03.1001")           # official open data
    n  = await b.nowcast("id")          # XML→JSON
    print(json.dumps(g, ensure_ascii=False)[:300])
    print("cache:", b.stats())
    await b.close()

asyncio.run(main())
```

`BMKG` methods:

| Method | Route | Token |
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
python bmkgaw.py weather 31.71.03.1001        # or 'lat,lon'; + --source app|web|auto
python bmkgaw.py quake-latest | quake-recent | quake-live
python bmkgaw.py nowcast id
python bmkgaw.py airports
python bmkgaw.py self                          # offline self-check
```

After `pip install .` (or `pip install -e .`), the command becomes `bmkgaw`
straight from your shell — same as the module name.

## Cache

- **`memory`** (default): in-process TTL dict.
- **`sqlite`**: persistent across restarts, good for servers.

```bash
export BMKGAW_CACHE_BACKEND=sqlite
export BMKGAW_CACHE_DB_URL=sqlite:////data/bmkgaw_cache.db
```

Each entry stores a `sha256` of the response body; when a TTL expires and the
content changed, the entry gets `changed=1` (handy for polling — e.g.
earthquakes / warning feeds). Inspect: `curl :8000/api/v1/cache/stats`.

## Rate limit & throttle

BMKG open data allows **60 req/min/IP**. The wrapper uses a global **1 req/s**
outbound throttle (a shared `asyncio.Lock` across all hosts and routes), so
normal usage stays far below the limit. **Don't remove the throttle/cache** —
they're your protection from WAF and rate limits. Per-endpoint caching
(TTL 60–86400 s) cuts down repeated requests.

## Error handling

The wrapper surfaces upstream failures as clear HTTP errors, not internal
500s:

| Code | Meaning |
|---|---|
| `400` | bad param / invalid `source` / token required |
| `401`/`403` | bad token / blocked by WAF (check UA + Referer) |
| `404` | no data (`/warnings/{location}`, `warningcuaca/{loc}`) |
| `429` | rate limited — the throttle prevents this |
| `501`/`502` | upstream endpoint dead/deprecated server-side |

Endpoints that are **dead server-side (2026-09-03)** stay registered but
return a clear upstream error rather than hanging or 500ing: `adm/search`
(ignores `q`), `radar*` (403), `translate` (500), `warningcuaca/{loc}` (404),
`radar.bmkg.go.id` & `mhews.id` (NXDOMAIN), `nowcasting.bmkg.go.id` (523).

## Technical notes

- **Two XML schemas for quakes:** `live30event.xml` is plain `<Infogempa>`;
  the CAP feeds (`last30*`, `warninggeof`) are `<alert>` CAP 1.2. Auto-detected.
- **`data.bmkg.go.id` needs a browser UA + `Referer`** (WAF) — already set.
- **Weather icons can contain spaces** (`berawan tebal-am.svg`) → URL-encoded.
- Marine `forecast-raw` uses the `fct_code` compact codec (decode algorithm
  undocumented) → v1 proxies raw; decoding comes later.

## Deploy

### Docker

```bash
docker build -t bmkgaw .
docker run --rm -p 8000:8000 -e BMKGAW_TOKEN='Bearer …' bmkgaw
```

### Docker Compose

```bash
cp .env.example .env     # fill BMKGAW_TOKEN (optional)
docker compose up -d     # built-in healthcheck
```

### Fly.io

```bash
fly launch --no-deploy
# uncomment [mounts] + set sqlite in fly.toml for persistent cache
fly deploy
```

### Vercel

```bash
vercel deploy            # uses vercel.json + api/index.py (ASGI)
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

Single-file core by design — the whole service, library, and CLI live in
`bmkgaw.py`; everything else is packaging, deployment, or tests.

```
bmkgaw.py              # core module: BMKG client, cache, throttling, 33 FastAPI routes, CLI
tests/test_bmkgaw.py   # offline pytest suite (16 tests, no network)
api/index.py           # Vercel serverless entrypoint (ASGI)
Dockerfile             # container image (non-root + healthcheck)
docker-compose.yml     # local/orchestrated run with healthcheck
fly.toml               # Fly.io deployment config
vercel.json            # Vercel deployment config
bmkgaw.service         # systemd unit for VPS deployment
requirements.txt       # runtime deps: fastapi, uvicorn[standard], httpx
pyproject.toml         # PyPI metadata + console script (bmkgaw)
pytest.ini             # pytest config
.env.example           # env template — token placeholder (safe to commit)
.dockerignore          # keeps .venv/.git out of the image
.gitignore             # keeps .venv/.env/cache/artifacts out of git
CHANGELOG.md           # Keep a Changelog
LICENSE                # MIT
README.md              # this file (English)
README.id.md           # Indonesian translation
```

## Tests

```bash
pip install pytest        # dev-only
python -m pytest tests/ -q
python bmkgaw.py self     # offline self-check, no pytest needed
```

## Security

- The token is **never committed**. `.env.example` only carries a placeholder;
  set `BMKGAW_TOKEN` via env or a local `.env` file (git-ignored).
- Auth headers are sent **only** to `api-apps.bmkg.go.id` (app API) — never to
  open-data hosts.
- Non-root user in Docker; built-in healthcheck.

## Disclaimer

Data © BMKG. This wrapper is not affiliated with or endorsed by BMKG. Use it
in line with BMKG's official open-data terms; credit BMKG as the source.
