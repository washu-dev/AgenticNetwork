"""External tool implementations: web_search, maps_search, reverse_geocode,
place_photos, street_view, fetch_url.

Corresponds to the tool set T in the paper.
All Maps API calls have safe fallbacks when the key lacks Maps permissions."""

from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from config import get_maps_api_key

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "DeepSearchGeo/1.0"})
_MAPS_BASE = "https://maps.googleapis.com/maps/api"
_MAPS_AVAILABLE: bool | None = None


def _check_maps() -> bool:
    """Lazy check whether Maps API is usable."""
    global _MAPS_AVAILABLE
    if _MAPS_AVAILABLE is not None:
        return _MAPS_AVAILABLE
    try:
        key = get_maps_api_key()
        url = f"{_MAPS_BASE}/geocode/json"
        resp = _SESSION.get(url, params={"latlng": "38.8977,-77.0365", "key": key}, timeout=10)
        data = resp.json()
        _MAPS_AVAILABLE = data.get("status") == "OK"
    except Exception:
        _MAPS_AVAILABLE = False
    return _MAPS_AVAILABLE


# ---------------------------------------------------------------------------
# Reverse Geocode
# ---------------------------------------------------------------------------

def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Convert (lat, lon) to structured address components."""
    if not _check_maps():
        return {"error": "MAPS_UNAVAILABLE", "formatted_address": f"({lat:.4f}, {lon:.4f})"}
    key = get_maps_api_key()
    url = f"{_MAPS_BASE}/geocode/json"
    resp = _SESSION.get(url, params={"latlng": f"{lat},{lon}", "key": key}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "OK" or not data.get("results"):
        return {"error": data.get("status", "NO_RESULTS"), "raw": data}
    top = data["results"][0]
    components: dict[str, str] = {}
    for comp in top.get("address_components", []):
        for t in comp["types"]:
            components[t] = comp["long_name"]
    return {
        "formatted_address": top.get("formatted_address", ""),
        "components": components,
        "place_id": top.get("place_id", ""),
        "location": top.get("geometry", {}).get("location", {}),
    }


def forward_geocode(address: str) -> dict[str, Any]:
    """Convert address string to coordinates."""
    if not _check_maps():
        return {"error": "MAPS_UNAVAILABLE"}
    key = get_maps_api_key()
    url = f"{_MAPS_BASE}/geocode/json"
    resp = _SESSION.get(url, params={"address": address, "key": key}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "OK" or not data.get("results"):
        return {"error": data.get("status", "NO_RESULTS")}
    top = data["results"][0]
    loc = top["geometry"]["location"]
    return {
        "formatted_address": top.get("formatted_address", ""),
        "latitude": loc["lat"],
        "longitude": loc["lng"],
        "place_id": top.get("place_id", ""),
    }


# ---------------------------------------------------------------------------
# Maps / Places search
# ---------------------------------------------------------------------------

def maps_search_nearby(lat: float, lon: float, query: str,
                       radius: int = 5000) -> list[dict]:
    """Search nearby places. Returns empty list if Maps unavailable."""
    if not _check_maps():
        return []
    key = get_maps_api_key()
    url = f"{_MAPS_BASE}/place/textsearch/json"
    params = {
        "query": query,
        "location": f"{lat},{lon}",
        "radius": radius,
        "key": key,
    }
    resp = _SESSION.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = []
    for p in data.get("results", [])[:8]:
        results.append({
            "name": p.get("name", ""),
            "address": p.get("formatted_address", ""),
            "types": p.get("types", []),
            "rating": p.get("rating"),
            "location": p.get("geometry", {}).get("location", {}),
        })
    return results


def maps_search_text(query: str) -> list[dict]:
    """General text-based place search. Returns empty list if Maps unavailable."""
    if not _check_maps():
        return []
    key = get_maps_api_key()
    url = f"{_MAPS_BASE}/place/textsearch/json"
    resp = _SESSION.get(url, params={"query": query, "key": key}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = []
    for p in data.get("results", [])[:5]:
        loc = p.get("geometry", {}).get("location", {})
        results.append({
            "name": p.get("name", ""),
            "address": p.get("formatted_address", ""),
            "types": p.get("types", []),
            "latitude": loc.get("lat"),
            "longitude": loc.get("lng"),
        })
    return results


# ---------------------------------------------------------------------------
# Street View
# ---------------------------------------------------------------------------

def get_street_view_url(lat: float, lon: float,
                        size: str = "640x400",
                        heading: int = 0, pitch: int = 0,
                        fov: int = 90) -> str:
    """Build a Street View Static API URL."""
    key = get_maps_api_key()
    return (
        f"{_MAPS_BASE}/streetview"
        f"?size={size}&location={lat},{lon}"
        f"&heading={heading}&pitch={pitch}&fov={fov}&key={key}"
    )


def fetch_street_view_image(lat: float, lon: float,
                            size: str = "640x400") -> Image.Image | None:
    """Download a Street View image. Returns None if Maps unavailable."""
    if not _check_maps():
        return None
    url = get_street_view_url(lat, lon, size)
    try:
        resp = _SESSION.get(url, timeout=20)
        if resp.status_code == 200 and "image" in resp.headers.get("Content-Type", ""):
            return Image.open(BytesIO(resp.content))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Place Photos
# ---------------------------------------------------------------------------

def get_place_photos(place_id: str, max_photos: int = 3) -> list[str]:
    """Fetch photo URLs. Returns empty list if Maps unavailable."""
    if not _check_maps():
        return []
    key = get_maps_api_key()
    url = f"{_MAPS_BASE}/place/details/json"
    try:
        resp = _SESSION.get(
            url, params={"place_id": place_id, "fields": "photos", "key": key}, timeout=15
        )
        resp.raise_for_status()
        photos = resp.json().get("result", {}).get("photos", [])
        urls = []
        for photo in photos[:max_photos]:
            ref = photo.get("photo_reference")
            if ref:
                urls.append(
                    f"{_MAPS_BASE}/place/photo"
                    f"?maxwidth=800&photo_reference={ref}&key={key}"
                )
        return urls
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Web fetch
# ---------------------------------------------------------------------------

def fetch_url_text(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return truncated text content."""
    try:
        resp = _SESSION.get(url, timeout=15)
        resp.raise_for_status()
        return resp.text[:max_chars]
    except Exception as exc:
        return f"[fetch error: {exc}]"


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def image_to_base64(image_path: str) -> str:
    """Encode an image file to base64."""
    img = Image.open(image_path)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def load_image_bytes(image_path: str, max_dim: int = 0, quality: int = 90) -> bytes:
    """Load image file as JPEG bytes, optionally resizing.

    Parameters
    ----------
    image_path
        Path to the source image.
    max_dim
        If > 0, resize so the longest side equals this value (preserving aspect
        ratio). Most iPhone photos are 4000+ px wide; resizing to 1024 cuts
        image tokens by 3-4x with minimal accuracy loss for residential attacks.
        0 = no resize (original behavior).
    quality
        JPEG quality when re-encoding.
    """
    img = Image.open(image_path).convert("RGB")
    if max_dim and max(img.size) > max_dim:
        w, h = img.size
        if w >= h:
            new_w, new_h = max_dim, int(round(h * max_dim / w))
        else:
            new_w, new_h = int(round(w * max_dim / h)), max_dim
        img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------

def test_gemini_api_key() -> tuple[bool, str]:
    """Test Gemini API key by listing models."""
    try:
        from google import genai
        from config import get_google_api_key
        client = genai.Client(api_key=get_google_api_key())
        models = list(client.models.list())
        names = [m.name for m in models[:5]]
        return True, f"Gemini OK. Sample models: {names}"
    except Exception as exc:
        return False, f"Gemini FAIL: {exc}"


def test_maps_api_key() -> tuple[bool, str]:
    """Test Maps API key with a simple geocode request."""
    global _MAPS_AVAILABLE
    _MAPS_AVAILABLE = None  # force re-check
    try:
        key = get_maps_api_key()
        url = f"{_MAPS_BASE}/geocode/json"
        resp = _SESSION.get(url, params={"latlng": "38.8977,-77.0365", "key": key}, timeout=10)
        data = resp.json()
        if data.get("status") == "OK":
            addr = data["results"][0].get("formatted_address", "")
            _MAPS_AVAILABLE = True
            return True, f"Maps OK: {addr}"
        _MAPS_AVAILABLE = False
        return False, f"Maps API error: {data.get('status', 'UNKNOWN')}"
    except Exception as exc:
        _MAPS_AVAILABLE = False
        return False, f"Maps FAIL: {exc}"
