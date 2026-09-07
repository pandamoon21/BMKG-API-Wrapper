"""Offline checks for bmkgaw — no network required.

Run:  python -m pytest tests/ -q
"""
import sys
import os

# ensure the repo root (module) is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xml.etree.ElementTree as ET
from fastapi import HTTPException

from bmkgaw import (
    AGENT, TOKEN, TTLCache, SQLCache, _etree, app, default_headers, resolve_source,
)

# --- headers ----------------------------------------------------------------
def test_default_headers_no_auth():
    h = default_headers()
    assert "Authorization" not in h
    assert h["User-Agent"].startswith("Mozilla/5.0")


def test_default_headers_auth_requires_token():
    # no token in this env (test) -> raises
    if not TOKEN:
        import pytest
        with pytest.raises(RuntimeError):
            default_headers(auth=True)


def test_default_headers_tews_referer():
    h = default_headers(host="tews")
    assert h["Referer"] == "https://www.bmkg.go.id/"


# --- resolve_source ---------------------------------------------------------
def test_resolve_auto_no_token_web(monkeypatch):
    monkeypatch.setattr("bmkgaw.TOKEN", "")
    src, auth = resolve_source("auto", has_web=True)
    assert (src, auth) == ("web", False)


def test_resolve_auto_token_app(monkeypatch):
    monkeypatch.setattr("bmkgaw.TOKEN", "Bearer xyz")
    src, auth = resolve_source("auto", has_web=True)
    assert (src, auth) == ("app", True)


def test_resolve_auto_no_token_no_web_raises(monkeypatch):
    monkeypatch.setattr("bmkgaw.TOKEN", "")
    try:
        resolve_source("auto", has_web=False)
        assert False, "should raise HTTPException"
    except HTTPException as e:
        assert e.status_code == 400


def test_resolve_app_requires_token(monkeypatch):
    monkeypatch.setattr("bmkgaw.TOKEN", "")
    try:
        resolve_source("app", has_web=True)
        assert False, "should raise HTTPException"
    except HTTPException as e:
        assert e.status_code == 400


def test_resolve_web_no_web_raises(monkeypatch):
    monkeypatch.setattr("bmkgaw.TOKEN", "Bearer xyz")
    try:
        resolve_source("web", has_web=False)
        assert False, "should raise HTTPException"
    except HTTPException as e:
        assert e.status_code == 400


def test_resolve_bad_source(monkeypatch):
    monkeypatch.setattr("bmkgaw.TOKEN", "Bearer xyz")
    try:
        resolve_source("bogus", has_web=True)
        assert False, "should raise HTTPException"
    except HTTPException as e:
        assert e.status_code == 400


# --- XML -> JSON ------------------------------------------------------------
def test_etree_infogempa_list():
    root = ET.fromstring(
        "<Infogempa><gempa><eventid>bmg1</eventid><mag>5.5</mag></gempa>"
        "<gempa><eventid>bmg2</eventid><mag>4.0</mag></gempa></Infogempa>")
    out = _etree(root)
    assert out["gempa"] == [{"eventid": "bmg1", "mag": "5.5"},
                            {"eventid": "bmg2", "mag": "4.0"}]


def test_etree_cap_namespace_and_nested():
    root = ET.fromstring(
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        "<identifier>id-1</identifier>"
        "<info><event>Gempabumi</event><area><areaDesc>X</areaDesc></area></info>"
        "<info><event>Tsunami</event></info></alert>")
    out = _etree(root)
    assert out["identifier"] == "id-1"
    assert isinstance(out["info"], list) and len(out["info"]) == 2
    assert out["info"][0]["area"] == {"areaDesc": "X"}


def test_etree_attrib_only_node():
    root = ET.fromstring('<rss><link href="http://x/rss.xml"/></rss>')
    out = _etree(root)
    assert out["link"] == {"href": "http://x/rss.xml"}


# --- cache ------------------------------------------------------------------
def test_ttl_cache_expiry():
    c = TTLCache()
    c.put("k", {"a": 1}, 100)
    assert c.get("k") == {"a": 1}
    c.put("e", 1, -1)
    assert c.get("e") is None


def test_sql_cache_change_detection():
    s = SQLCache("sqlite:///:memory:")
    s.put("k", {"a": 1}, 100)
    assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2}, 100)
    row = s._conn.execute("SELECT changed FROM bmkgaw_cache WHERE key='k'").fetchone()
    assert row is not None and row[0] == 1
    s.put("k", {"a": 2}, 100)
    row = s._conn.execute("SELECT changed FROM bmkgaw_cache WHERE key='k'").fetchone()
    assert row is not None and row[0] == 0


# --- app / routes -----------------------------------------------------------
def test_app_routes_present():
    paths = {r.path for r in app.routes}
    for expected in ("/api/v1/health", "/api/v1/weather/forecast",
                     "/api/v1/earthquake/latest", "/api/v1/publik/weather",
                     "/api/v1/nowcast/{lang}"):
        assert expected in paths, f"missing route {expected}"


def test_app_openapi_groups():
    spec = app.openapi()
    assert spec["info"]["title"] == "bmkgaw — Info BMKG API Wrapper"
    names = [t["name"] for t in spec["tags"]]
    for g in ("Weather", "Earthquakes", "Marine", "Open Data"):
        assert g in names
