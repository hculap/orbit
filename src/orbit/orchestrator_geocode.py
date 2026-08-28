"""Reverse-geocoding + nearby POIs over OpenStreetMap (Nominatim + Overpass).

The geo round trip (:mod:`orchestrator_geocode`'s sibling
:mod:`orchestrator_geo`) hands back a bare lat/lon. This module turns that into
the two answers a human actually asks for — *„gdzie jestem?"* (a street
address) and *„co jest najbliżej?"* (real POIs sorted by distance).

THE EGRESS IS THE WHOLE RISK. Coordinates leave the box to public OSM servers,
and a ban would break every other tool that shares this IP, so ToS compliance
is not best-effort here:

* a real, contactable ``User-Agent`` on EVERY request (Nominatim rejects the
  default httpx one outright);
* a HARD client-side rate gate — 1 req/s Nominatim, 1 req/2 s Overpass —
  enforced with a monotonic clock behind an :class:`asyncio.Lock` held for the
  WHOLE request (not just its start, or slow responses would overlap), so
  concurrent agents queue instead of bursting;
* ONE attempt per lookup, 8 s timeout, never a parallel fan-out. A transient
  upstream failure comes back as a typed ``retryable: True`` refusal and the
  agent decides — retrying inside the module would multiply load under exactly
  the conditions that already broke.

Everything is cached in memory (TTL + a hard entry cap): a follow-up question
about the same corner costs zero requests. Nothing is written to disk, and no
latitude or longitude is ever logged.

All HTTP goes through the ONE private coroutine :func:`_fetch_json` so a test
can replace it and the suite never touches the network. Failures reuse
:data:`orchestrator_geo.STATUSES` — the feature has exactly one status table.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Callable

import httpx

from . import orchestrator_geo as geo_mod

_logger = logging.getLogger(__name__)

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Nominatim's usage policy REQUIRES an identifying UA with a way to reach the
# operator. A generic client string is grounds for a block.
USER_AGENT = "orbit/1.0 (+https://your-dashboard.example)"

HTTP_TIMEOUT_S: float = 8.0
NOMINATIM_MIN_INTERVAL_S: float = 1.0
OVERPASS_MIN_INTERVAL_S: float = 2.0

REVERSE_CACHE_TTL_S: float = 86400.0   # a street corner does not move
REVERSE_CACHE_MAX: int = 256
NEARBY_CACHE_TTL_S: float = 600.0      # shops open and close; 10 min is plenty
NEARBY_CACHE_MAX: int = 128
# Cache key precision — 4 dp ≈ 11 m, tight enough that the address is still
# right and loose enough that GPS jitter reuses the entry.
_CACHE_DP: int = 4

RADIUS_DEFAULT_M: int = 1500
RADIUS_MIN_M: int = 50
RADIUS_MAX_M: int = 10000
LIMIT_DEFAULT: int = 8
LIMIT_MAX: int = 25
QUERY_MAX_CHARS: int = 64

EARTH_RADIUS_M: float = 6_371_000.0

# Curated PL+EN keyword → OSM tag. Deliberately small and hand-checked: these
# values are interpolated into the Overpass QL, so ONLY literals from this
# table ever reach the query string — a free-text query takes the Nominatim
# search path instead, where it is a URL-encoded parameter. An empty value
# means "any node with this key" (e.g. every shop).
KEYWORD_TAGS: dict[str, tuple[str, str]] = {
    # apteka
    "apteka": ("amenity", "pharmacy"),
    "apteki": ("amenity", "pharmacy"),
    "pharmacy": ("amenity", "pharmacy"),
    "drugstore": ("amenity", "pharmacy"),
    # sklep
    "sklep": ("shop", ""),
    "sklepy": ("shop", ""),
    "shop": ("shop", ""),
    "store": ("shop", ""),
    # supermarket
    "supermarket": ("shop", "supermarket"),
    "spożywczy": ("shop", "supermarket"),
    "grocery": ("shop", "supermarket"),
    # piekarnia
    "piekarnia": ("shop", "bakery"),
    "bakery": ("shop", "bakery"),
    # restauracja
    "restauracja": ("amenity", "restaurant"),
    "restauracje": ("amenity", "restaurant"),
    "restaurant": ("amenity", "restaurant"),
    "jedzenie": ("amenity", "restaurant"),
    "food": ("amenity", "restaurant"),
    # kawiarnia
    "kawiarnia": ("amenity", "cafe"),
    "kawa": ("amenity", "cafe"),
    "cafe": ("amenity", "cafe"),
    "coffee": ("amenity", "cafe"),
    # bankomat
    "bankomat": ("amenity", "atm"),
    "atm": ("amenity", "atm"),
    "bank": ("amenity", "bank"),
    # paliwo
    "stacja": ("amenity", "fuel"),
    "paliwo": ("amenity", "fuel"),
    "benzyna": ("amenity", "fuel"),
    "fuel": ("amenity", "fuel"),
    "petrol": ("amenity", "fuel"),
    "gas": ("amenity", "fuel"),
    # transport
    "przystanek": ("highway", "bus_stop"),
    "autobus": ("highway", "bus_stop"),
    "bus": ("highway", "bus_stop"),
    "bus_stop": ("highway", "bus_stop"),
    "tramwaj": ("railway", "tram_stop"),
    "tram": ("railway", "tram_stop"),
    "stacja_kolejowa": ("railway", "station"),
    "dworzec": ("railway", "station"),
    "station": ("railway", "station"),
    # zdrowie
    "szpital": ("amenity", "hospital"),
    "hospital": ("amenity", "hospital"),
    "przychodnia": ("amenity", "clinic"),
    "clinic": ("amenity", "clinic"),
    "lekarz": ("amenity", "doctors"),
    "doctor": ("amenity", "doctors"),
    "dentysta": ("amenity", "dentist"),
    "dentist": ("amenity", "dentist"),
    # usługi
    "poczta": ("amenity", "post_office"),
    "post": ("amenity", "post_office"),
    "hotel": ("tourism", "hotel"),
    "nocleg": ("tourism", "hotel"),
    "park": ("leisure", "park"),
    "parking": ("amenity", "parking"),
    "siłownia": ("leisure", "fitness_centre"),
    "silownia": ("leisure", "fitness_centre"),
    "gym": ("leisure", "fitness_centre"),
    "fitness": ("leisure", "fitness_centre"),
    "toaleta": ("amenity", "toilets"),
    "toilet": ("amenity", "toilets"),
    "policja": ("amenity", "police"),
    "police": ("amenity", "police"),
    "szkoła": ("amenity", "school"),
    "szkola": ("amenity", "school"),
    "school": ("amenity", "school"),
    "biblioteka": ("amenity", "library"),
    "library": ("amenity", "library"),
    "kino": ("amenity", "cinema"),
    "cinema": ("amenity", "cinema"),
    "bar": ("amenity", "bar"),
    "pub": ("amenity", "pub"),
    "paczkomat": ("amenity", "parcel_locker"),
    "parcel": ("amenity", "parcel_locker"),
}

# Address fields we lift out of Nominatim's ``address`` object, newest-name
# first — the API spells the same concept differently per country.
_CITY_KEYS: tuple[str, ...] = (
    "city", "town", "village", "municipality", "hamlet", "suburb",
)


# ── rate gate ─────────────────────────────────────────────────────


def _monotonic() -> float:
    """Indirection so a test can freeze the clock without touching ``time``."""
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    """Indirection so a test can assert the gate slept without waiting."""
    await asyncio.sleep(seconds)


class RateGate:
    """Serialize calls to one upstream: ONE in flight, ``min_interval`` apart.

    Used as ``async with gate:`` around the whole request, not just its start.
    Spacing only the starts is not a rate limit at all when the upstream is
    slow: with an 8 s timeout and 1 s spacing, eight Nominatim connections can
    be open at once — exactly the burst its usage policy bans, and exactly when
    the service is already struggling. Holding the lock for the duration makes
    "never a parallel fan-out" true, and ``_last`` is stamped on EXIT so the
    interval is measured from the end of the previous call.
    """

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = float(min_interval_s)
        self._last: float = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "RateGate":
        await self._lock.acquire()
        try:
            wait = self.min_interval_s - (_monotonic() - self._last)
            if wait > 0:
                await _sleep(wait)
        except BaseException:
            # A cancellation while waiting out the interval must not strand the
            # lock — every later caller would block forever.
            self._lock.release()
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._last = _monotonic()
        self._lock.release()


_nominatim_gate = RateGate(NOMINATIM_MIN_INTERVAL_S)
_overpass_gate = RateGate(OVERPASS_MIN_INTERVAL_S)


# ── TTL cache ─────────────────────────────────────────────────────


class TTLCache:
    """Tiny insertion-ordered TTL cache — in memory, capped, no disk."""

    def __init__(self, ttl_s: float, max_entries: int) -> None:
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if _monotonic() - stored_at > self.ttl_s:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: Any, value: Any) -> None:
        self._data[key] = (_monotonic(), value)
        while len(self._data) > self.max_entries:
            self._data.pop(next(iter(self._data)))

    def clear(self) -> None:
        self._data.clear()


_reverse_cache = TTLCache(REVERSE_CACHE_TTL_S, REVERSE_CACHE_MAX)
_nearby_cache = TTLCache(NEARBY_CACHE_TTL_S, NEARBY_CACHE_MAX)


def clear_caches() -> None:
    """Drop every cached lookup (wired to the same "forget" lever as the fix)."""
    _reverse_cache.clear()
    _nearby_cache.clear()


# ── pure helpers ──────────────────────────────────────────────────


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Good to ~0.5 % at city scale."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def normalize_query(raw: Any) -> str:
    """Lowercased, capped, whitespace-collapsed lookup key."""
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.strip().lower().split())[:QUERY_MAX_CHARS]


def match_tag(query: str) -> tuple[str, str] | None:
    """Curated keyword → ``(osm_key, osm_value)``, or ``None`` for free text.

    Tries the whole phrase first, then each word, so *„najbliższa apteka"*
    still resolves without the table having to list every phrasing.
    """
    normalized = normalize_query(query)
    if not normalized:
        return None
    hit = KEYWORD_TAGS.get(normalized)
    if hit is not None:
        return hit
    for word in normalized.split():
        hit = KEYWORD_TAGS.get(word)
        if hit is not None:
            return hit
    return None


def _validate_coords(lat: Any, lon: Any) -> tuple[float, float]:
    """Range-checked ``(lat, lon)``; raises :class:`ValueError` (→ HTTP 400)."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("lat/lon must be numbers")
    if isinstance(lat, bool) or isinstance(lon, bool):
        raise ValueError("lat/lon must be numbers")
    if not math.isfinite(lat_f) or not (-90.0 <= lat_f <= 90.0):
        raise ValueError("lat must be a finite number in [-90, 90]")
    if not math.isfinite(lon_f) or not (-180.0 <= lon_f <= 180.0):
        raise ValueError("lon must be a finite number in [-180, 180]")
    return lat_f, lon_f


def _clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


def _viewbox(lat: float, lon: float, radius_m: int) -> str:
    """``left,top,right,bottom`` box of ``radius_m`` around the fix."""
    d_lat = radius_m / 111_320.0
    d_lon = radius_m / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    return f"{lon - d_lon:.6f},{lat + d_lat:.6f},{lon + d_lon:.6f},{lat - d_lat:.6f}"


def _overpass_ql(key: str, value: str, lat: float, lon: float,
                 radius_m: int, limit: int) -> str:
    """Build the QL. ``key``/``value`` come ONLY from :data:`KEYWORD_TAGS`."""
    selector = f'["{key}"="{value}"]' if value else f'["{key}"]'
    around = f"(around:{radius_m},{lat:.6f},{lon:.6f})"
    parts = "\n  ".join(
        f"{kind}{selector}{around};" for kind in ("node", "way", "relation")
    )
    return (
        f"[out:json][timeout:{int(HTTP_TIMEOUT_S)}];\n"
        f"(\n  {parts}\n);\n"
        f"out center {min(limit * 5, 100)};"
    )


# ── the ONE http seam (monkeypatch this in tests) ─────────────────


async def _fetch_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> Any:
    """The only network call in this module. Raises on transport/HTTP errors.

    Kept deliberately dumb (no retry, no fallback host) so the rate gate above
    it is the single place that decides how hard we hit OSM — and so a test can
    replace this one coroutine and cover every branch offline.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers) as client:
        if method == "POST":
            response = await client.post(url, data=data)
        else:
            response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def _guarded(gate: RateGate, call: Callable[[], Any], what: str) -> Any:
    """Run one upstream call behind ``gate``; typed refusal on any failure.

    Returns the parsed JSON, or a ``geocode_failed`` payload — never raises, so
    every caller has exactly one failure shape to forward. The call runs INSIDE
    the gate, so a slow upstream delays the next request instead of overlapping
    with it.
    """
    try:
        async with gate:
            return await call()
    except httpx.HTTPStatusError as exc:
        _logger.warning("geocode: %s upstream HTTP %s", what, exc.response.status_code)
        return geo_mod.failure("geocode_failed", upstream=what,
                               http_status=exc.response.status_code)
    except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        _logger.warning("geocode: %s transport failure: %s", what, type(exc).__name__)
        return geo_mod.failure("geocode_failed", upstream=what)
    except ValueError as exc:  # malformed / non-JSON body
        _logger.warning("geocode: %s returned unparseable body: %s", what, exc)
        return geo_mod.failure("geocode_failed", upstream=what)


def _is_failure(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("ok") is False


# ── reverse ───────────────────────────────────────────────────────


def _address_from_nominatim(payload: Any) -> dict[str, Any] | None:
    """Nominatim reverse JSON → our flat address dict (``None`` if unusable)."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    address = payload.get("address")
    address = address if isinstance(address, dict) else {}
    city = next((address[k] for k in _CITY_KEYS if isinstance(address.get(k), str)), None)
    display = payload.get("display_name")
    if not isinstance(display, str) and not address:
        return None
    return {
        "display_name": display if isinstance(display, str) else "",
        "road": address.get("road") if isinstance(address.get("road"), str) else None,
        "house_number": (address.get("house_number")
                         if isinstance(address.get("house_number"), str) else None),
        "city": city,
        "postcode": (address.get("postcode")
                     if isinstance(address.get("postcode"), str) else None),
        "country": (address.get("country")
                    if isinstance(address.get("country"), str) else None),
        "raw": address,
    }


async def reverse(lat: Any, lon: Any) -> dict[str, Any]:
    """Coordinates → a street address (cached 24 h on 4-dp coordinates)."""
    lat_f, lon_f = _validate_coords(lat, lon)
    key = (round(lat_f, _CACHE_DP), round(lon_f, _CACHE_DP))
    cached = _reverse_cache.get(key)
    if cached is not None:
        return {"ok": True, "status": "ok", "cached": True, "address": dict(cached)}

    payload = await _guarded(
        _nominatim_gate,
        lambda: _fetch_json("GET", NOMINATIM_REVERSE_URL, params={
            "format": "jsonv2", "zoom": 18, "addressdetails": 1,
            "lat": f"{lat_f:.6f}", "lon": f"{lon_f:.6f}",
        }),
        "nominatim/reverse",
    )
    if _is_failure(payload):
        return payload
    address = _address_from_nominatim(payload)
    if address is None:
        return geo_mod.failure("geocode_failed", upstream="nominatim/reverse",
                               detail="no address at these coordinates")
    _reverse_cache.put(key, address)
    return {"ok": True, "status": "ok", "cached": False, "address": dict(address)}


# ── nearby ────────────────────────────────────────────────────────


def _place_from_overpass(element: Any, lat: float, lon: float,
                         tag_key: str) -> dict[str, Any] | None:
    """One Overpass element → a place dict, or ``None`` when unplottable."""
    if not isinstance(element, dict):
        return None
    center = element.get("center") if isinstance(element.get("center"), dict) else {}
    try:
        p_lat = float(element.get("lat", center.get("lat")))
        p_lon = float(element.get("lon", center.get("lon")))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p_lat) or not math.isfinite(p_lon):
        return None
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    name = tags.get("name")
    return {
        "name": name if isinstance(name, str) and name else None,
        "kind": tags.get(tag_key) if isinstance(tags.get(tag_key), str) else tag_key,
        "lat": round(p_lat, 6),
        "lon": round(p_lon, 6),
        "distance_m": round(haversine_m(lat, lon, p_lat, p_lon)),
        "tags": tags,
    }


def _place_from_nominatim(entry: Any, lat: float, lon: float) -> dict[str, Any] | None:
    """One Nominatim search hit → a place dict (free-text fallback path)."""
    if not isinstance(entry, dict):
        return None
    try:
        p_lat, p_lon = float(entry.get("lat")), float(entry.get("lon"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p_lat) or not math.isfinite(p_lon):
        return None
    name = entry.get("name") or entry.get("display_name")
    return {
        "name": name if isinstance(name, str) and name else None,
        "kind": entry.get("type") if isinstance(entry.get("type"), str) else "place",
        "lat": round(p_lat, 6),
        "lon": round(p_lon, 6),
        "distance_m": round(haversine_m(lat, lon, p_lat, p_lon)),
        "tags": {"display_name": entry.get("display_name")}
        if isinstance(entry.get("display_name"), str) else {},
    }


async def _nearby_by_tag(lat: float, lon: float, tag: tuple[str, str],
                         radius_m: int, limit: int) -> dict[str, Any]:
    key, value = tag
    payload = await _guarded(
        _overpass_gate,
        lambda: _fetch_json("POST", OVERPASS_URL, data={
            "data": _overpass_ql(key, value, lat, lon, radius_m, limit),
        }),
        "overpass",
    )
    if _is_failure(payload):
        return payload
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return geo_mod.failure("geocode_failed", upstream="overpass",
                               detail="unexpected response shape")
    places = [p for p in (_place_from_overpass(e, lat, lon, key) for e in elements)
              if p is not None]
    return {
        "ok": True, "status": "ok",
        "source": "overpass",
        "tag": f"{key}={value}" if value else key,
        "places": places,
    }


async def _nearby_by_text(lat: float, lon: float, query: str,
                          radius_m: int, limit: int) -> dict[str, Any]:
    payload = await _guarded(
        _nominatim_gate,
        lambda: _fetch_json("GET", NOMINATIM_SEARCH_URL, params={
            "format": "jsonv2", "q": query, "limit": min(limit * 2, 40),
            "viewbox": _viewbox(lat, lon, radius_m), "bounded": 1,
        }),
        "nominatim/search",
    )
    if _is_failure(payload):
        return payload
    if not isinstance(payload, list):
        return geo_mod.failure("geocode_failed", upstream="nominatim/search",
                               detail="unexpected response shape")
    places = [p for p in (_place_from_nominatim(e, lat, lon) for e in payload)
              if p is not None]
    return {"ok": True, "status": "ok", "source": "nominatim", "tag": None,
            "places": places}


async def nearby(lat: Any, lon: Any, query: Any, radius_m: Any = RADIUS_DEFAULT_M,
                 limit: Any = LIMIT_DEFAULT) -> dict[str, Any]:
    """POIs around a fix, nearest first.

    A curated keyword takes the Overpass path (exact OSM tag, ``around:``
    filter); anything else falls back to a Nominatim free-text search bounded
    by a viewbox — the query is a URL parameter there, never QL source.
    """
    lat_f, lon_f = _validate_coords(lat, lon)
    normalized = normalize_query(query)
    if not normalized:
        raise ValueError("query is required")
    radius = _clamp_int(radius_m, RADIUS_DEFAULT_M, RADIUS_MIN_M, RADIUS_MAX_M)
    cap = _clamp_int(limit, LIMIT_DEFAULT, 1, LIMIT_MAX)

    key = (round(lat_f, _CACHE_DP), round(lon_f, _CACHE_DP), normalized, radius)
    cached = _nearby_cache.get(key)
    if cached is not None:
        return {**cached, "cached": True, "places": [dict(p) for p in cached["places"]][:cap]}

    tag = match_tag(normalized)
    result = (await _nearby_by_tag(lat_f, lon_f, tag, radius, cap) if tag
              else await _nearby_by_text(lat_f, lon_f, normalized, radius, cap))
    if _is_failure(result):
        return result

    places = sorted(result["places"], key=lambda p: p["distance_m"])
    places = [p for p in places if p["distance_m"] <= radius] or places
    payload = {
        "ok": True, "status": "ok",
        "query": normalized,
        "radius_m": radius,
        "source": result["source"],
        "tag": result["tag"],
        "places": places,
    }
    _nearby_cache.put(key, payload)
    return {**payload, "cached": False, "places": [dict(p) for p in places][:cap]}
