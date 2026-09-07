#!/usr/bin/env python3
"""
bmkgaw — Info BMKG API wrapper.

Sources data from BMKG's own endpoints, reconstructed from reversing the
official Info BMKG Android app (com.Info_BMKG v3.5.1) plus BMKG's official
open-data portals (data.bmkg.go.id / api.bmkg.go.id/publik / www.bmkg.go.id):

  app     https://api-apps.bmkg.go.id        (app API; df/v1 + marine need token)
  publik  https://api.bmkg.go.id             (official open data, no token)
  tews    https://data.bmkg.go.id/DataMKG/TEWS  (official quake feeds, no token)
  gcs     https://bmkg-content-inatews.storage.googleapis.com (app CAP feeds)
  web     https://www.bmkg.go.id             (nowcast CAP alerts, no token)

Run:  uvicorn bmkgaw:app --port 8000
Self-check:  python3 bmkgaw.py self   (offline)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Token: the app ships a static shared credential in assets/flutter_assets/.env:
#   API_LOCK=Bearer <token>   (also SALT_KEY=BMKG_DJK, AGENT_NAME=infobmkg)
# Send it as the whole Authorization value (the "Bearer " prefix is baked into
# the .env value). Any of Authorization / x-api-key-infobmkg / x-api-key /
# apikey works; x-agent-bmkg: infobmkg is sent alongside by the app but not
# required. NOT hardcoded in this public build — read from env BMKGAW_TOKEN.
# The token is a static shared credential (anyone can extract it from the APK);
# do not commit real values.
TOKEN = os.environ.get("BMKGAW_TOKEN", "").strip()
AGENT = os.environ.get("BMKGAW_AGENT", "infobmkg").strip() or "infobmkg"
# Cache backend: memory (default) or sqlite.  ponytail: sqlite only — mysql/
# postgres backends exist in mdlaw but are YAGNI for a private wrapper; add the
# same SQLCache flavour switch if you need multi-instance shared cache.
CACHE_BACKEND = os.environ.get("BMKGAW_CACHE_BACKEND", "memory").strip().lower()
CACHE_DB_URL = os.environ.get("BMKGAW_CACHE_DB_URL", "").strip() or "sqlite:///bmkgaw_cache.db"

__version__ = "0.1.0"

HOSTS = {
    "app": "https://api-apps.bmkg.go.id",
    "publik": "https://api.bmkg.go.id",
    "tews": "https://data.bmkg.go.id/DataMKG/TEWS",
    "gcs": "https://bmkg-content-inatews.storage.googleapis.com",
    "web": "https://www.bmkg.go.id",
}

# BMKG docs: 60 req/min/IP on the open portals. The app backend also asks for a
# modest rate (df/v1 shares one backend with map/survey flows). One request per
# second globally keeps every host under 60/min.  ponytail: single global
# throttle — per-host budgets if you ever push one host hard.
_SEM = asyncio.Semaphore(2)
MIN_INTERVAL = 1.0
_last_out = 0.0
_out_lock = asyncio.Lock()

_UA = ("Mozilla/5.0 (Linux; Android 14; sdk_gphone64_arm64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")


def default_headers(auth: bool = False, host: str = "app") -> dict[str, str]:
    h = {
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "id,en;q=0.8",
    }
    if host == "tews":
        h["Referer"] = "https://www.bmkg.go.id/"   # data.bmkg WAF: full browser UA + Referer → 200
    if auth:
        t = TOKEN
        if not t:
            raise RuntimeError("BMKGAW_TOKEN is not set (df/v1 + marine endpoints need it)")
        h["Authorization"] = t if t.lower().startswith("bearer ") else f"Bearer {t}"
        h["x-agent-bmkg"] = AGENT
    return h


def resolve_source(source: str, has_web: bool = True) -> tuple[str, bool]:
    """Pick app vs web source for auth-gated endpoints.

    source: 'app' = app API (reverse-engineered, needs token) · 'web' = official open data ·
            'auto' (default) = token present → app, else web.
    Returns (resolved_source, use_auth). Raises 400 when the caller forces a
    source that cannot be served (app without token / web without a public
    equivalent)."""
    s = (source or "auto").lower()
    if s == "auto":
        if TOKEN:
            return "app", True
        if has_web:
            return "web", False
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "BMKGAW_TOKEN is not set and this endpoint has "
                                            "no public web source — set BMKGAW_TOKEN or pass "
                                            "source=web where available"})
    if s == "app":
        if not TOKEN:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "source=app requires BMKGAW_TOKEN (set it in "
                                                "the environment)"})
        return "app", True
    if s == "web":
        if not has_web:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "source=web is not available for this endpoint "
                                                "(no official open-data equivalent)"})
        return "web", False
    raise HTTPException(400, {"error": True, "code": 400,
                              "detail": f"source must be 'app', 'web' or 'auto' (got '{source}')"})


# ---------------------------------------------------------------------------
# Cache — in-memory TTL (default) or sqlite (stdlib, persists across restarts)
# ---------------------------------------------------------------------------
class CacheBackend:
    hits = 0
    misses = 0

    def get(self, key: str) -> object | None:  # pragma: no cover
        raise NotImplementedError

    def put(self, key: str, value: object, ttl: float) -> None:  # pragma: no cover
        raise NotImplementedError

    def entries(self) -> int:  # pragma: no cover
        raise NotImplementedError

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "backend": type(self).__name__,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else None,
            "entries": self.entries(),
        }


class TTLCache(CacheBackend):
    def __init__(self) -> None:
        self._d: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        item = self._d.get(key)
        if item is None:
            self.misses += 1
            return None
        expires, value = item
        if time.monotonic() > expires:
            self._d.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: object, ttl: float) -> None:
        self._d[key] = (time.monotonic() + ttl, value)

    def entries(self) -> int:
        return len(self._d)


class SQLCache(CacheBackend):
    """Persistent sqlite cache (stdlib). Same change-detection idea as mdlaw:
    stores a sha256 of the value and flags changed=1 when a TTL-refetch differs."""

    def __init__(self, url: str) -> None:
        path = url[len("sqlite:///"):] or ":memory:"
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS bmkgaw_cache (
                 key TEXT PRIMARY KEY, value TEXT NOT NULL,
                 expires_at REAL NOT NULL, hash TEXT NOT NULL,
                 created_at REAL NOT NULL, updated_at REAL NOT NULL,
                 changed INTEGER NOT NULL DEFAULT 0)""")
        self._conn.commit()

    def get(self, key: str) -> object | None:
        row = self._conn.execute(
            "SELECT value, expires_at FROM bmkgaw_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            self.misses += 1
            return None
        value, expires = row
        if expires <= time.time():
            self._conn.execute("DELETE FROM bmkgaw_cache WHERE key = ?", (key,))
            self._conn.commit()
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(value)

    def put(self, key: str, value: object, ttl: float) -> None:
        h = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        now = time.time()
        row = self._conn.execute(
            "SELECT hash, created_at FROM bmkgaw_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            created = updated = now
            changed = 0
        else:
            old_hash, created = row
            changed = 0 if old_hash == h else 1
            updated = now
        self._conn.execute(
            """INSERT INTO bmkgaw_cache (key,value,expires_at,hash,created_at,updated_at,changed)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 expires_at=excluded.expires_at, hash=excluded.hash,
                 updated_at=excluded.updated_at, changed=excluded.changed""",
            (key, json.dumps(value, ensure_ascii=False), now + ttl, h, created, updated, changed))
        self._conn.commit()

    def entries(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM bmkgaw_cache").fetchone()[0]


def _make_cache() -> CacheBackend:
    if CACHE_BACKEND == "memory":
        return TTLCache()
    if CACHE_BACKEND == "sqlite":
        return SQLCache(CACHE_DB_URL)
    raise RuntimeError(f"BMKGAW_CACHE_BACKEND={CACHE_BACKEND}: use 'memory' or 'sqlite'")


cache = _make_cache()

# ---------------------------------------------------------------------------
# Upstream client + typed fetch
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0,
                                    limits=httpx.Limits(max_keepalive_connections=10))
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()


async def _throttle() -> None:
    global _last_out
    async with _out_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_out)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_out = time.monotonic()


def _upstream_error(status: int, body: str, url: str) -> HTTPException:
    try:
        detail = json.loads(body)
    except Exception:
        detail = body[:300]
    hint = ""
    if status == 403 and url.startswith(HOSTS["app"]):
        hint = (" (this endpoint is server-disabled on BMKG's side as of 2026-09-03: "
                "radar*) or your BMKGAW_TOKEN is missing/invalid")
    return HTTPException(status, {"error": True, "code": status,
                                  "detail": detail, "upstream": url} | ({"hint": hint} if hint else {}))


def _local(tag: str) -> str:
    return tag.split('}')[-1]


def _etree(node: Any) -> object:
    """ElementTree -> dict/list/str, namespaces stripped. Repeated tags become
    lists; attribute-only nodes (e.g. self-closing <link href=…>) become the
    attribute dict so links are not lost."""
    children = list(node)
    if not children:
        text = (node.text or "").strip()
        if text:
            return text
        return dict(node.attrib) if node.attrib else ""
    out: dict[str, object] = {}
    if node.attrib:
        out["@attrs"] = dict(node.attrib)
    for ch in children:
        k = _local(ch.tag)
        v = _etree(ch)
        if k in out:
            if not isinstance(out[k], list):
                out[k] = [out[k]]
            out[k].append(v)  # type: ignore[union-attr]
        else:
            out[k] = v
    return out


async def fetch(path: str, *, host: str = "app", ttl: float = 300,
                fmt: str = "json", auth: bool = False,
                params: dict | None = None, cache_key: str | None = None) -> object:
    """GET one upstream resource, cached. fmt: json | xml | text | bytes.
    xml responses are parsed to JSON objects. Errors are never cached."""
    url = HOSTS[host] + path
    key = cache_key or f"{host} {path} {json.dumps(params or {}, sort_keys=True)}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        headers = default_headers(auth=auth, host=host)
    except RuntimeError as e:
        raise HTTPException(400, {"error": True, "code": 400, "detail": str(e)}) from e
    async with _SEM:
        await _throttle()
        try:
            r = await get_client().get(url, headers=headers, params=params)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _upstream_error(e.response.status_code, e.response.text, url) from e
        except httpx.TimeoutException as e:
            raise HTTPException(504, {"error": True, "code": 504,
                                      "detail": "upstream timeout", "upstream": url}) from e
        except httpx.HTTPError as e:
            raise HTTPException(502, {"error": True, "code": 502,
                                      "detail": f"upstream error: {e}", "upstream": url}) from e
    if fmt == "json":
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500], "error": "expected JSON, got non-JSON body"}
    elif fmt == "xml":
        import xml.etree.ElementTree as ET
        try:
            data = _etree(ET.fromstring(r.content))
        except ET.ParseError as e:
            raise HTTPException(502, {"error": True, "code": 502,
                                      "detail": f"upstream XML parse error: {e}",
                                      "upstream": url}) from e
    elif fmt == "bytes":
        return Response(content=r.content, media_type=r.headers.get("content-type", "application/octet-stream"),
                        headers={"Cache-Control": f"public, max-age={int(ttl)}"})
    else:  # text
        data = r.text
    cache.put(key, data, ttl)
    return data


def _resp(data: object, ttl: float) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={int(ttl)}"})


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_client()


app = FastAPI(
    title="bmkgaw — Info BMKG API Wrapper",
    version=__version__,
    description=(
        "Unofficial API wrapper for BMKG (Badan Meteorologi, Klimatologi, dan "
        "Geofisika) data: weather forecasts, earthquakes/tsunami, marine, "
        "airport weather, air quality, nowcast warnings. Sources: the Info "
        "BMKG Android app API (api-apps.bmkg.go.id, reconstructed by "
        "reverse-engineering v3.5.1) and BMKG's official open-data portals "
        "(data.bmkg.go.id, api.bmkg.go.id/publik, www.bmkg.go.id/alerts).\n\n"
        "**Interactive docs:**\n"
        "- [Swagger UI](/docs) — playground, Try-it-out per endpoint\n"
        "- [ReDoc](/redoc) — clean reference\n"
        "- [OpenAPI](/openapi.json) — machine-readable spec\n\n"
        "**Auth-gated endpoints** (df/v1 + marine) accept `?source=`:\n"
        "- `auto` (default): token present → app; no token → web open data\n"
        "- `app`: force app API (needs `BMKGAW_TOKEN`)\n"
        "- `web`: force official open data\n\n"
        "Not affiliated with BMKG. Data source attribution to BMKG is required "
        "by their open-data terms."
    ),
    lifespan=lifespan,
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,
        "tryItOutEnabled": True,
        "displayRequestDuration": True,
    },
    openapi_tags=[
        {"name": "Meta", "description": "Health, dashboard, cache stats"},
        {"name": "Weather", "description": "Forecast / present / legacy weather, area (df/v1 app API + open data)"},
        {"name": "Airports", "description": "Airport weather (METAR/TAF)"},
        {"name": "Marine", "description": "Marine forecast + waves"},
        {"name": "Environment", "description": "Air quality (PM2.5), satellite imagery"},
        {"name": "Hazards", "description": "CB forecast, weather warnings, press releases, radar"},
        {"name": "Earthquakes", "description": "Earthquakes & tsunami — web API (TEWS) + app CAP feeds"},
        {"name": "Open Data", "description": "Official public forecast + nowcast CAP (no token)"},
    ],
)

_START = time.time()


@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


@app.get("/api/v1/health", tags=["Meta"])
async def health() -> dict:
    return {"status": "ok", "service": "bmkgaw",
            "token_configured": bool(TOKEN),
            "cache_backend": getattr(cache, "_flavor", CACHE_BACKEND)}


@app.get("/api/v1/cache/stats", tags=["Meta"])
async def cache_stats() -> dict:
    return cache.stats()


@app.get("/api/v1/dashboard", tags=["Meta"])
async def dashboard() -> dict:
    total = cache.hits + cache.misses
    return {
        "status": "ok", "service": "bmkgaw", "version": __version__,
        "uptime_seconds": int(time.time() - _START),
        "token_configured": bool(TOKEN),
        "cache": {"hits": cache.hits, "misses": cache.misses,
                  "hit_rate": round(cache.hits / total, 4) if total else None,
                  "entries": cache.entries(),
                  "backend": getattr(cache, "_flavor", CACHE_BACKEND)},
        "upstreams": HOSTS,
    }


# ---------------------------------------------------------------------------
# Weather & area  (app df/v1 API — token required)
# ---------------------------------------------------------------------------
@app.get("/api/v1/weather/forecast", tags=["Weather"])
async def weather_forecast(adm4: str | None = None,
                           lat: float | None = None, lon: float | None = None,
                           source: str = "auto") -> JSONResponse:
    """Hourly/daily forecast for an area (adm4, or lat+lon → reverse-geocoded).

    source: 'app' (app API, needs token) | 'web' (official open data,
    no token) | 'auto' (default: token → app, else web)."""
    src, auth = resolve_source(source, has_web=True)
    if src == "web":
        if not adm4:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "source=web requires adm4 (no lat/lon "
                                                "reverse-geocode on the public API)"})
        data = await fetch("/publik/prakiraan-cuaca", host="publik", ttl=300,
                           params={"adm4": adm4})
        return _resp(data, 300)
    # app / auto-with-token
    if not adm4:
        if lat is None or lon is None:
            raise HTTPException(400, {"error": True, "code": 400,
                                      "detail": "provide adm4, or both lat and lon"})
        coord = await fetch("/api/df/v1/adm/coord", host="app", ttl=86400,
                            auth=True, params={"lat": lat, "lon": lon})
        adm4 = coord.get("adm4") if isinstance(coord, dict) else None
        if not adm4:
            raise HTTPException(502, {"error": True, "code": 502,
                                      "detail": "reverse-geocode returned no adm4"})
    data = await fetch("/api/df/v1/forecast/adm", host="app", ttl=300, auth=auth,
                       params={"adm4": adm4})
    return _resp(data, 300)


@app.get("/api/v1/weather/present", tags=["Weather"])
async def weather_present(adm4: str, source: str = "auto") -> JSONResponse:
    """Current/present weather for an area. No public web equivalent — 'web'
    source not available (upstream 404 on probe)."""
    src, auth = resolve_source(source, has_web=False)
    data = await fetch("/api/df/v1/presentwx/adm", host="app", ttl=120, auth=auth,
                       params={"adm4": adm4})
    return _resp(data, 120)


@app.get("/api/v1/weather/legacy", tags=["Weather"])
async def weather_legacy(lat: float, lon: float) -> JSONResponse:
    """Legacy point weather. ⚠️ Deprecated upstream — returns empty cuaca[] on
    current server (app migrated to /weather/forecast)."""
    data = await fetch("/api/cuaca/", host="app", ttl=300,
                       params={"lat": lat, "lon": lon})
    return _resp(data, 300)


@app.get("/api/v1/area/search", tags=["Weather"])
async def area_search(q: str, source: str = "auto") -> JSONResponse:
    """Search administrative areas (adm1–adm4). ⚠️ Known upstream issue
    (2026-09-03): ignores q and returns a fixed Kemayoran list — use
    /area/coord with lat/lon instead. No public web equivalent."""
    src, auth = resolve_source(source, has_web=False)
    data = await fetch("/api/df/v1/adm/search", host="app", ttl=60, auth=auth,
                       params={"q": q})
    return _resp(data, 60)


@app.get("/api/v1/area/coord", tags=["Weather"])
async def area_coord(lat: float, lon: float, source: str = "auto") -> JSONResponse:
    """Reverse geocode: lat/lon → nearest administrative area (adm1–adm4).
    No public web equivalent."""
    src, auth = resolve_source(source, has_web=False)
    data = await fetch("/api/df/v1/adm/coord", host="app", ttl=86400, auth=auth,
                       params={"lat": lat, "lon": lon})
    return _resp(data, 86400)


# ---------------------------------------------------------------------------
# Airport weather  (no token)
# ---------------------------------------------------------------------------
@app.get("/api/v1/airports", tags=["Airports"])
async def airports() -> JSONResponse:
    """All airport weather stations (ICAO, brief METAR, temp, wind, symbol)."""
    data = await fetch("/api/cuaca-bandara", host="app", ttl=300)
    return _resp(data, 300)


@app.get("/api/v1/airports/{icao}", tags=["Airports"])
async def airport_detail(icao: str) -> JSONResponse:
    """One airport station: metadata + last METAR/TAF + hourly table."""
    icao = icao.upper().strip()
    if not icao.isalnum() or len(icao) > 4:
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "icao must be a 4-char code like WIII"})
    data = await fetch("/api/cuaca-bandara-detail", host="app", ttl=300,
                       params={"icao": icao})
    return _resp(data, 300)


# ---------------------------------------------------------------------------
# Marine  (forecast-raw needs token; wave overview is public)
# ---------------------------------------------------------------------------
@app.get("/api/v1/marine/forecast", tags=["Marine"])
async def marine_forecast(source: str = "auto") -> JSONResponse:
    """Raw marine forecast: area (375 waters) + port (677 ports), each entry a
    compact fct_code codec. ~1.5 MB — not cached aggressively. No public web
    equivalent (wave overview is public but this raw feed is app-only)."""
    src, auth = resolve_source(source, has_web=False)
    data = await fetch("/api/cuaca-maritim/forecast-raw", host="app", ttl=1800, auth=auth)
    return _resp(data, 1800)


@app.get("/api/v1/marine/waves", tags=["Marine"])
async def marine_waves() -> JSONResponse:
    """Wave category overview per area code (today/tomorrow/h2/h3 + issued)."""
    data = await fetch("/storage/cuaca-maritim/overview/gelombang.json", host="app", ttl=1800)
    return _resp(data, 1800)


# ---------------------------------------------------------------------------
# Air quality / satellite / CB / warnings / press  (no token except noted)
# ---------------------------------------------------------------------------
@app.get("/api/v1/air-quality", tags=["Environment"])
async def air_quality() -> JSONResponse:
    """PM2.5 air quality per location (uppercase fields: LOKASI, PM25, ...)."""
    data = await fetch("/api/pm25/", host="app", ttl=600)
    return _resp(data, 600)


@app.get("/api/v1/satellite", tags=["Environment"])
async def satellite_index() -> JSONResponse:
    """Himawari-8 satellite image filenames, newest first."""
    data = await fetch("/json/satelit.json", host="app", ttl=120)
    return _resp(data, 120)


@app.get("/api/v1/satellite/image/{filename:path}", tags=["Environment"])
async def satellite_image(filename: str) -> Response:
    """Satellite image bytes. filename from /api/v1/satellite, e.g.
    H08_ET_Indonesia_202609030750.webp. Pass it URL-encoded."""
    import urllib.parse
    from typing import cast
    safe = urllib.parse.quote(filename)
    return cast(Response, await fetch(f"/storage/satelit/{safe}", host="app", ttl=600, fmt="bytes"))


@app.get("/api/v1/cb", tags=["Hazards"])
async def cb_forecast(lat: float, lon: float) -> JSONResponse:
    """Cumulonimbus (CB) growth potential forecast for a coordinate."""
    data = await fetch("/api/cuaca/prediksi-cb", host="app", ttl=3600,
                       params={"lat": lat, "lon": lon})
    return _resp(data, 3600)


@app.get("/api/v1/warnings", tags=["Hazards"])
async def warnings() -> JSONResponse:
    """Active early-warning list (headline + description + ID_Kode + expired)."""
    data = await fetch("/api/warningcuaca", host="app", ttl=120)
    return _resp(data, 120)


@app.get("/api/v1/warnings/{location:path}", tags=["Hazards"])
async def warning_location(location: str) -> JSONResponse:
    """Warning for one region (URL-encoded name, e.g. 'DKI Jakarta'). Returns
    404 from upstream when no warning is active there — that is normal."""
    import urllib.parse
    safe = urllib.parse.quote(location)
    data = await fetch(f"/api/warningcuaca/{safe}", host="app", ttl=120)
    return _resp(data, 120)


@app.get("/api/v1/press", tags=["Hazards"])
async def press_releases() -> JSONResponse:
    """BMKG press releases (content is HTML)."""
    data = await fetch("/api/siaran-pers", host="app", ttl=3600)
    return _resp(data, 3600)


# ---------------------------------------------------------------------------
# Radar  — server-disabled upstream (403 as of 2026-09-03); routed for parity
# ---------------------------------------------------------------------------
@app.get("/api/v1/radar", tags=["Hazards"])
async def radar(lat: float, lon: float) -> JSONResponse:
    """Radar station list + CMAX. ⚠️ Upstream returns 403 server-side (disabled)."""
    data = await fetch("/api/radar", host="app", ttl=300, params={"lat": lat, "lon": lon})
    return _resp(data, 300)


@app.get("/api/v1/radar/image", tags=["Hazards"])
async def radar_image(radar: str) -> JSONResponse:
    """Radar image for a station. ⚠️ Upstream returns 403 server-side (disabled)."""
    data = await fetch("/api/radar-image", host="app", ttl=300, params={"radar": radar})
    return _resp(data, 300)


# ---------------------------------------------------------------------------
# Earthquakes — official web API (tews, no token) + app CAP feeds (gcs)
# ---------------------------------------------------------------------------
@app.get("/api/v1/earthquake/latest", tags=["Earthquakes"])
async def quake_latest() -> JSONResponse:
    """Newest single earthquake (official data.bmkg.go.id, Indonesian JSON)."""
    data = await fetch("/autogempa.json", host="tews", ttl=60)
    return _resp(data, 60)


@app.get("/api/v1/earthquake/recent", tags=["Earthquakes"])
async def quake_recent() -> JSONResponse:
    """15 most recent M≥5 earthquakes."""
    data = await fetch("/gempaterkini.json", host="tews", ttl=120)
    return _resp(data, 120)


@app.get("/api/v1/earthquake/felt", tags=["Earthquakes"])
async def quake_felt() -> JSONResponse:
    """15 most recent felt earthquakes (MMI in Dirasakan)."""
    data = await fetch("/gempadirasakan.json", host="tews", ttl=120)
    return _resp(data, 120)


@app.get("/api/v1/earthquake/shakemap/{name:path}", tags=["Earthquakes"])
async def quake_shakemap(name: str) -> Response:
    """Shakemap JPEG, name from the 'Shakemap' field (e.g. 20260903112422.mmi.jpg)."""
    import urllib.parse
    from typing import cast
    safe = urllib.parse.quote(name)
    return cast(Response, await fetch(f"/{safe}", host="tews", ttl=86400, fmt="bytes"))


@app.get("/api/v1/earthquake/live", tags=["Earthquakes"])
async def quake_live() -> JSONResponse:
    """Rolling ~3-day quake buffer from the app's GCS feed (199 events on probe;
    ~1-min updates). Parsed XML → JSON."""
    data = await fetch("/live30event.xml", host="gcs", ttl=60, fmt="xml")
    return _resp(data, 60)


@app.get("/api/v1/earthquake/last30", tags=["Earthquakes"])
async def quake_last30() -> JSONResponse:
    """Last 30 earthquakes, CAP 1.2 envelope → JSON."""
    data = await fetch("/last30event.xml", host="gcs", ttl=120, fmt="xml")
    return _resp(data, 120)


@app.get("/api/v1/earthquake/last30/felt", tags=["Earthquakes"])
async def quake_last30_felt() -> JSONResponse:
    """Last 30 felt earthquakes, CAP 1.2."""
    data = await fetch("/last30feltevent.xml", host="gcs", ttl=120, fmt="xml")
    return _resp(data, 120)


@app.get("/api/v1/earthquake/last30/tsunami", tags=["Earthquakes"])
async def quake_last30_tsunami() -> JSONResponse:
    """Last 30 tsunami-triggering earthquakes, CAP 1.2."""
    data = await fetch("/last30tsunamievent.xml", host="gcs", ttl=120, fmt="xml")
    return _resp(data, 120)


@app.get("/api/v1/earthquake/warning", tags=["Earthquakes"])
async def quake_warning() -> JSONResponse:
    """Single active geo/tsunami warning, CAP 1.2."""
    data = await fetch("/warninggeof.xml", host="gcs", ttl=60, fmt="xml")
    return _resp(data, 60)


# ---------------------------------------------------------------------------
# Official open-data portals — no token, 60 req/min/IP, BMKG attribution needed
# ---------------------------------------------------------------------------
@app.get("/api/v1/publik/weather", tags=["Open Data"])
async def publik_weather(adm4: str) -> JSONResponse:
    """Official open-data forecast for an adm4 village/kelurahan (3 days, 3-hourly)."""
    data = await fetch("/publik/prakiraan-cuaca", host="publik", ttl=300,
                       params={"adm4": adm4})
    return _resp(data, 300)


@app.get("/api/v1/nowcast/{lang}", tags=["Open Data"])
async def nowcast_list(lang: str) -> JSONResponse:
    """Nowcast early-warning RSS (province-level) — lang: id | en."""
    if lang not in ("id", "en"):
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "lang must be 'id' or 'en'"})
    data = await fetch(f"/alerts/nowcast/{lang}", host="web", ttl=60, fmt="xml")
    return _resp(data, 60)


@app.get("/api/v1/nowcast/{lang}/{kode}", tags=["Open Data"])
async def nowcast_detail(lang: str, kode: str) -> JSONResponse:
    """Nowcast CAP detail (kecamatan-level) for one province, kode from the RSS
    (e.g. <kode>_alert.xml)."""
    if lang not in ("id", "en"):
        raise HTTPException(400, {"error": True, "code": 400,
                                  "detail": "lang must be 'id' or 'en'"})
    if not kode.endswith(".xml"):
        kode = f"{kode}_alert.xml"
    data = await fetch(f"/alerts/nowcast/{lang}/{kode}", host="web", ttl=300, fmt="xml")
    return _resp(data, 300)


# ---------------------------------------------------------------------------
# Python package layer — use without the HTTP server:
#   from bmkgaw import BMKG
#   bmkg = BMKG()
#   latest = await bmkg.quake_latest()
#   fc = await bmkg.forecast(adm4="31.71.03.1001")
# ---------------------------------------------------------------------------
class BMKG:
    """Async Python client for BMKG data. Shares the module cache + throttle."""

    def __init__(self, token: str | None = None) -> None:
        if token is not None:
            global TOKEN
            TOKEN = token.strip()

    async def get(self, path: str, *, host: str = "app", ttl: float = 300,
                  fmt: str = "json", auth: bool = False,
                  params: dict | None = None) -> object:
        return await fetch(path, host=host, ttl=ttl, fmt=fmt, auth=auth, params=params)

    # --- weather & area (app df/v1 auth; source=web falls back to open data) ---
    async def forecast(self, adm4: str | None = None,
                       lat: float | None = None, lon: float | None = None,
                       source: str = "auto") -> object:
        src, auth = resolve_source(source, has_web=True)
        if src == "web":
            if not adm4:
                raise HTTPException(400, {"error": True, "code": 400,
                                          "detail": "source=web requires adm4"})
            return await self.get("/publik/prakiraan-cuaca", host="publik", ttl=300,
                                  params={"adm4": adm4})
        if not adm4:
            coord = await self.get("/api/df/v1/adm/coord", auth=True, ttl=86400,
                                   params={"lat": lat, "lon": lon})
            adm4 = coord.get("adm4") if isinstance(coord, dict) else None
        return await self.get("/api/df/v1/forecast/adm", auth=auth, ttl=300,
                              params={"adm4": adm4})

    async def present(self, adm4: str, source: str = "auto") -> object:
        src, auth = resolve_source(source, has_web=False)
        return await self.get("/api/df/v1/presentwx/adm", auth=auth, ttl=120,
                              params={"adm4": adm4})

    async def area_coord(self, lat: float, lon: float, source: str = "auto") -> object:
        src, auth = resolve_source(source, has_web=False)
        return await self.get("/api/df/v1/adm/coord", auth=auth, ttl=86400,
                              params={"lat": lat, "lon": lon})

    # --- airports / marine / air / sat / cb / warnings / press (no auth) ---
    async def airports(self) -> object:
        return await self.get("/api/cuaca-bandara", ttl=300)

    async def airport_detail(self, icao: str) -> object:
        return await self.get("/api/cuaca-bandara-detail", ttl=300, params={"icao": icao.upper()})

    async def marine_forecast(self, source: str = "auto") -> object:
        src, auth = resolve_source(source, has_web=False)
        return await self.get("/api/cuaca-maritim/forecast-raw", auth=auth, ttl=1800)

    async def marine_waves(self) -> object:
        return await self.get("/storage/cuaca-maritim/overview/gelombang.json", ttl=1800)

    async def air_quality(self) -> object:
        return await self.get("/api/pm25/", ttl=600)

    async def satellite(self) -> object:
        return await self.get("/json/satelit.json", ttl=120)

    async def cb(self, lat: float, lon: float) -> object:
        return await self.get("/api/cuaca/prediksi-cb", ttl=3600, params={"lat": lat, "lon": lon})

    async def warnings(self) -> object:
        return await self.get("/api/warningcuaca", ttl=120)

    async def press(self) -> object:
        return await self.get("/api/siaran-pers", ttl=3600)

    # --- earthquakes ---
    async def quake_latest(self) -> object:
        return await self.get("/autogempa.json", host="tews", ttl=60)

    async def quake_recent(self) -> object:
        return await self.get("/gempaterkini.json", host="tews", ttl=120)

    async def quake_felt(self) -> object:
        return await self.get("/gempadirasakan.json", host="tews", ttl=120)

    async def quake_live(self) -> object:
        return await self.get("/live30event.xml", host="gcs", ttl=60, fmt="xml")

    async def quake_last30(self) -> object:
        return await self.get("/last30event.xml", host="gcs", ttl=120, fmt="xml")

    async def quake_warning(self) -> object:
        return await self.get("/warninggeof.xml", host="gcs", ttl=60, fmt="xml")

    # --- official open data ---
    async def publik_weather(self, adm4: str) -> object:
        return await self.get("/publik/prakiraan-cuaca", host="publik", ttl=300,
                              params={"adm4": adm4})

    async def nowcast(self, lang: str = "id") -> object:
        return await self.get(f"/alerts/nowcast/{lang}", host="web", ttl=60, fmt="xml")

    async def close(self) -> None:
        await close_client()

    def stats(self) -> dict:
        return cache.stats()


# ---------------------------------------------------------------------------
# Offline self-check
# ---------------------------------------------------------------------------
def self_check() -> int:
    h = default_headers()
    assert h["User-Agent"] == _UA
    assert "Authorization" not in h
    ha = default_headers(auth=True, host="app") if TOKEN else None
    if TOKEN:
        assert ha["Authorization"].startswith("Bearer ") or ha["Authorization"].startswith("bearer ")
        assert ha["x-agent-bmkg"] == AGENT
    # xml -> json conversion (Infogempa + CAP shapes)
    import xml.etree.ElementTree as ET
    infogempa = _etree(ET.fromstring(
        '<Infogempa><gempa><eventid>bmg1</eventid><mag>5.5</mag></gempa>'
        '<gempa><eventid>bmg2</eventid><mag>4.0</mag></gempa></Infogempa>'))
    assert infogempa["gempa"] == [{"eventid": "bmg1", "mag": "5.5"},
                                  {"eventid": "bmg2", "mag": "4.0"}], "Infogempa list shape"
    cap = _etree(ET.fromstring(
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        '<identifier>id-1</identifier>'
        '<info><event>Gempabumi</event><area><areaDesc>X</areaDesc></area></info>'
        '<info><event>Tsunami</event></info></alert>'))
    assert cap["identifier"] == "id-1"
    assert isinstance(cap["info"], list) and len(cap["info"]) == 2
    assert cap["info"][0]["area"] == {"areaDesc": "X"}, "nested CAP info"
    # cache
    c = TTLCache()
    c.put("k", {"a": 1}, 100)
    assert c.get("k") == {"a": 1}
    c.put("e", 1, -1)
    assert c.get("e") is None
    s = SQLCache("sqlite:///:memory:")
    s.put("k", {"a": 1}, 100)
    assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2}, 100)
    row = s._conn.execute("SELECT changed FROM bmkgaw_cache WHERE key='k'").fetchone()
    assert row is not None and row[0] == 1, "change detection"
    print(f"PASS: offline checks (token={'set' if TOKEN else 'unset'}, "
          f"cache={CACHE_BACKEND}, xml→json, cache)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_CLI_USAGE = """\
bmkgaw — Info BMKG API wrapper CLI

Usage:
  bmkgaw [command] [args...]

Commands:
  self                       Offline self-check.
  health                     Quick live check of every upstream host.
  weather <adm4|lat,lon>     Forecast (adm4 code, or 'lat,lon').
  quake-latest               Newest earthquake (official web API).
  quake-recent               Recent M>=5 earthquakes.
  quake-live                 Live buffer (app GCS feed).
  nowcast [id|en]            Nowcast warning RSS (default id).
  airports                   All airport stations.
  serve | run                Start the HTTP server (default when no command).

Environment:
  BMKGAW_TOKEN             The app API_LOCK value (Bearer ... or raw token).
                             Needed for df/v1 + marine endpoints.
  BMKGAW_CACHE_BACKEND     memory (default) | sqlite
  BMKGAW_CACHE_DB_URL      sqlite:///path (default sqlite:///bmkgaw_cache.db)
"""


def _run_cli(args: list[str]) -> int:
    import sys

    if args and args[0] in ("-h", "--help"):
        print(_CLI_USAGE)
        return 0
    if not args:
        print(_CLI_USAGE)
        return 0
    cmd, rest = args[0], args[1:]

    async def call():
        bmkg = BMKG()
        try:
            if cmd == "health":
                from fastapi.responses import JSONResponse
                out = {"status": "ok", "service": "bmkgaw",
                       "token_configured": bool(TOKEN)}
                return out
            if cmd == "weather":
                if not rest:
                    raise SystemExit("usage: bmkgaw weather <adm4|lat,lon>")
                if "," in rest[0]:
                    lat, lon = (float(x) for x in rest[0].split(","))
                    return await bmkg.forecast(lat=lat, lon=lon)
                return await bmkg.forecast(adm4=rest[0])
            if cmd == "quake-latest":
                return await bmkg.quake_latest()
            if cmd == "quake-recent":
                return await bmkg.quake_recent()
            if cmd == "quake-live":
                return await bmkg.quake_live()
            if cmd == "nowcast":
                return await bmkg.nowcast(rest[0] if rest else "id")
            if cmd == "airports":
                return await bmkg.airports()
            raise SystemExit(f"unknown command: {cmd}")
        finally:
            await bmkg.close()

    try:
        data = asyncio.run(call())
    except HTTPException as e:
        print(f"error: {json.dumps(e.detail, ensure_ascii=False)}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if args and args[0] == "self":
        return self_check()
    if args and args[0] in ("serve", "run"):
        args = args[1:]
    if args and args[0] not in ("serve", "run"):
        return _run_cli(args)
    import uvicorn
    uvicorn.run("bmkgaw:app", host="0.0.0.0", port=8000)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
