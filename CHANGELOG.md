# Changelog

All notable changes to **bmkgaw** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] — 2026-09-08

Initial release. Unofficial Info BMKG API wrapper.

### Added

- **Single-file FastAPI service** (`bmkgaw.py`) with Swagger UI (`/docs`),
  ReDoc (`/redoc`) and OpenAPI JSON (`/openapi.json`), endpoints grouped by
  tag (Weather, Airports, Marine, Environment, Hazards, Earthquakes, Open Data, Meta).
- **33 routes** across two data families:
  - **Info BMKG app API** (`api-apps.bmkg.go.id`, from reverse-engineering
    `com.Info_BMKG` v3.5.1): forecast / present / legacy weather, area search
    + reverse geocode, airports, marine raw + wave overview, air quality
    (PM2.5), satellite index + imagery, CB forecast, weather warnings,
    press releases, radar.
  - **Official open-data portals**: public weather forecast
    (`api.bmkg.go.id/publik/prakiraan-cuaca`), earthquake TEWS feeds
    (`data.bmkg.go.id/DataMKG/TEWS/*` + shakemap), nowcast CAP alerts
    (`www.bmkg.go.id/alerts/nowcast/{id|en}`).
- **Source selection** (`?source=auto|app|web`) on auth-gated endpoints:
  `auto` uses the app API when `BMKGAW_TOKEN` is set, else falls back to the
  public web API (forecast only has a web equivalent).
- **Typed responses**: JSON passthrough, XML→JSON conversion (Infogempa +
  CAP 1.2 feeds), raw bytes for images (satellite, shakemap).
- **Cache**: TTL in-memory (`memory`) or persistent SQLite, with sha256
  change detection (`changed=1` on TTL refetch).
- **Throttle**: global 1 request/second outbound, respecting BMKG's
  60 req/min/IP open-data limit.
- **Async library client** (`BMKG`), **CLI** (`bmkgaw …` console script),
  and **offline self-check** (`bmkgaw self`).
- **Deploy assets**: `Dockerfile`, `docker-compose.yml`, `fly.toml`,
  `vercel.json` + `api/index.py`, and a systemd unit example in the README.
- **Tests**: offline `tests/test_bmkgaw.py` (headers, source resolution,
  XML→JSON, cache backends, route/OpenAPI groups).
- `pyproject.toml` (PyPI name **`bmkgaw`**, console script), `requirements.txt`,
  `.env.example` (token placeholder — never commit the real token).

### Security notes

- The app API token (`API_LOCK`, a static shared credential embedded in the
  APK) is **not hardcoded**. Set `BMKGAW_TOKEN` in the environment; the
  checked-in `.env.example` only carries a placeholder.
- Several app endpoints are dead server-side (as of 2026-09-03):
  `adm/search` ignores `q`; `radar*` returns 403; `translate` returns 500;
  `warningcuaca/{loc}` 404s. Official open-data routes cover forecast,
  earthquakes and nowcast warnings instead.
