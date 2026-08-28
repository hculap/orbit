"""Reverse-geocoding + nearby POIs (:mod:`orchestrator_geocode`) — OFFLINE.

THE HARD RULE OF THIS FILE: no test may reach OpenStreetMap. The module funnels
every request through the single coroutine ``_fetch_json``, and the ``geocode``
fixture replaces it with a recorder that EXPLODES when a test forgot to queue a
response — so a future refactor that adds a second, un-seamed call site fails
loudly here instead of quietly getting the box's IP banned (which would break
every other tool sharing it).

The clock is seamed the same way: ``_monotonic`` and ``_sleep`` are module-level
indirections, so the 1 req/s rate gate and the 24 h cache TTL are asserted in
microseconds instead of waited out.

The fixture also resets the module-global gates and caches per test; they are
process-lifetime singletons by design and would otherwise leak across tests.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from orbit import orchestrator_geocode as gc

# Warsaw, plus two POIs ~150 m and ~1.9 km away.
LAT, LON = 52.2296756, 21.0122287
NEAR_LAT, NEAR_LON = 52.2310000, 21.0122287
FAR_LAT, FAR_LON = 52.2470000, 21.0122287


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    """A monotonic clock a test can move by hand."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Fetcher:
    """Stand-in for ``_fetch_json``: replays queued results, records calls."""

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    def push(self, result: Any) -> None:
        """Queue a parsed body, or an exception instance to raise."""
        self.queue.append(result)

    async def __call__(self, method: str, url: str, *, params=None, data=None) -> Any:
        self.calls.append({"method": method, "url": url, "params": params, "data": data})
        if not self.queue:
            raise AssertionError(
                f"unqueued upstream call — a test would have hit the network: {url}"
            )
        result = self.queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def geocode(monkeypatch):
    """The module with its network, clock and sleep replaced, caches cleared."""
    fetcher = _Fetcher()
    clock = _Clock()
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(gc, "_fetch_json", fetcher)
    monkeypatch.setattr(gc, "_monotonic", clock)
    monkeypatch.setattr(gc, "_sleep", _fake_sleep)
    monkeypatch.setattr(gc._nominatim_gate, "_last", 0.0)
    monkeypatch.setattr(gc._overpass_gate, "_last", 0.0)
    gc.clear_caches()
    yield type("Ctx", (), {"mod": gc, "fetch": fetcher, "clock": clock, "slept": slept})
    gc.clear_caches()


def _reverse_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "display_name": "12, Krakowskie Przedmieście, Warszawa, 00-325, Polska",
        "address": {
            "house_number": "12",
            "road": "Krakowskie Przedmieście",
            "city": "Warszawa",
            "postcode": "00-325",
            "country": "Polska",
        },
    }
    body.update(overrides)
    return body


def _overpass_body(*elements: dict[str, Any]) -> dict[str, Any]:
    return {"elements": list(elements)}


# ── ToS surface ───────────────────────────────────────────────────


def test_user_agent_identifies_this_box() -> None:
    """Nominatim blocks generic clients — the UA must name a contactable host."""
    assert "orbit" in gc.USER_AGENT
    assert "your-dashboard.example" in gc.USER_AGENT


def test_rate_limits_are_at_least_as_strict_as_the_published_policy() -> None:
    assert gc.NOMINATIM_MIN_INTERVAL_S >= 1.0
    assert gc.OVERPASS_MIN_INTERVAL_S >= 2.0
    assert gc.HTTP_TIMEOUT_S <= 10.0


# ── pure helpers ──────────────────────────────────────────────────


def test_haversine_zero_distance() -> None:
    assert gc.haversine_m(LAT, LON, LAT, LON) == pytest.approx(0.0, abs=1e-6)


def test_haversine_matches_a_known_separation() -> None:
    # One degree of latitude is ~111.2 km anywhere on the sphere.
    assert gc.haversine_m(52.0, 21.0, 53.0, 21.0) == pytest.approx(111_195, rel=0.01)


def test_haversine_is_symmetric() -> None:
    there = gc.haversine_m(LAT, LON, NEAR_LAT, NEAR_LON)
    back = gc.haversine_m(NEAR_LAT, NEAR_LON, LAT, LON)
    assert there == pytest.approx(back)
    assert there == pytest.approx(148, rel=0.05)


def test_normalize_query_lowercases_collapses_and_caps() -> None:
    assert gc.normalize_query("  NAJBLIŻSZA   Apteka \n") == "najbliższa apteka"
    assert gc.normalize_query(None) == ""
    assert gc.normalize_query(42) == ""
    assert len(gc.normalize_query("x" * 200)) == gc.QUERY_MAX_CHARS


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("apteka", ("amenity", "pharmacy")),
        ("Apteka", ("amenity", "pharmacy")),
        ("pharmacy", ("amenity", "pharmacy")),
        ("najbliższa apteka", ("amenity", "pharmacy")),   # phrase → word fallback
        ("gdzie jest bankomat", ("amenity", "atm")),
        ("sklep", ("shop", "")),                          # bare key: any shop
        ("supermarket", ("shop", "supermarket")),
        ("stacja", ("amenity", "fuel")),
        ("przystanek", ("highway", "bus_stop")),
        ("siłownia", ("leisure", "fitness_centre")),
        ("silownia", ("leisure", "fitness_centre")),      # diacritic-free spelling
    ],
)
def test_match_tag_resolves_curated_keywords(query, expected) -> None:
    assert gc.match_tag(query) == expected


@pytest.mark.parametrize("query", ["", "   ", None, "kwiaciarnia u zosi", "zzzz"])
def test_match_tag_returns_none_for_free_text(query) -> None:
    # None ⇒ the Nominatim text path, where the query is a URL parameter and
    # never interpolated into Overpass QL.
    assert gc.match_tag(query) is None


def test_every_curated_tag_value_is_ql_safe() -> None:
    """Only literals from this table are interpolated into Overpass QL."""
    for key, value in gc.KEYWORD_TAGS.values():
        assert key.replace("_", "").isalnum(), key
        assert value == "" or value.replace("_", "").isalnum(), value


# ── the rate gate ─────────────────────────────────────────────────


def test_rate_gate_lets_the_first_call_straight_through(geocode) -> None:
    gate = gc.RateGate(1.0)

    async def scenario():
        async with gate:
            pass

    _run(scenario())
    assert geocode.slept == []


def test_rate_gate_delays_a_burst(geocode) -> None:
    gate = gc.RateGate(1.0)

    async def scenario():
        async with gate:
            pass
        async with gate:
            pass

    _run(scenario())
    assert geocode.slept == [pytest.approx(1.0)]


def test_rate_gate_does_not_delay_a_call_that_is_already_late(geocode) -> None:
    gate = gc.RateGate(1.0)

    async def scenario():
        async with gate:
            pass
        geocode.clock.advance(5.0)
        async with gate:
            pass

    _run(scenario())
    assert geocode.slept == []


def test_rate_gate_serializes_concurrent_callers(geocode) -> None:
    """Without the lock, two coroutines both read the same ``_last`` and burst."""
    gate = gc.RateGate(2.0)

    async def one():
        async with gate:
            pass

    async def scenario():
        await asyncio.gather(one(), one(), one())

    _run(scenario())
    assert geocode.slept == [pytest.approx(2.0), pytest.approx(2.0)]


def test_rate_gate_holds_the_lock_for_the_whole_call(geocode) -> None:
    """The finding this replaced: spacing only STARTS lets slow calls overlap.

    Three callers whose "request" yields to the loop must never be in flight at
    the same time, or an 8 s upstream timeout becomes 8 parallel connections.
    """
    gate = gc.RateGate(0.0)
    inflight = 0
    peak = 0

    async def one():
        nonlocal inflight, peak
        async with gate:
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)   # a real request always yields
            inflight -= 1

    async def scenario():
        await asyncio.gather(one(), one(), one())

    _run(scenario())
    assert peak == 1


def test_rate_gate_releases_the_lock_when_the_call_raises(geocode) -> None:
    gate = gc.RateGate(0.0)

    async def scenario():
        try:
            async with gate:
                raise RuntimeError("upstream exploded")
        except RuntimeError:
            pass
        async with gate:      # would deadlock if the lock leaked
            pass

    _run(asyncio.wait_for(scenario(), 1.0))


# ── the TTL cache ─────────────────────────────────────────────────


def test_ttl_cache_returns_a_hit_then_expires(geocode) -> None:
    cache = gc.TTLCache(10.0, 4)
    cache.put("k", "v")
    assert cache.get("k") == "v"
    geocode.clock.advance(11.0)
    assert cache.get("k") is None


def test_ttl_cache_evicts_past_its_cap(geocode) -> None:
    cache = gc.TTLCache(10.0, 2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    assert (cache.get("b"), cache.get("c")) == (2, 3)


def test_ttl_cache_miss_on_an_unknown_key(geocode) -> None:
    assert gc.TTLCache(10.0, 2).get("nope") is None


# ── input validation ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        (91.0, 21.0),
        (-91.0, 21.0),
        (52.0, 181.0),
        (52.0, -181.0),
        (None, 21.0),
        (52.0, None),
        ("north", 21.0),
        (float("nan"), 21.0),
        (float("inf"), 21.0),
        (True, 21.0),          # bool is an int in Python — rejected explicitly
        (52.0, False),
    ],
)
def test_reverse_rejects_unusable_coordinates(geocode, lat, lon) -> None:
    with pytest.raises(ValueError):
        _run(gc.reverse(lat, lon))
    assert geocode.fetch.calls == []   # never even reached the network seam


def test_nearby_requires_a_query(geocode) -> None:
    with pytest.raises(ValueError):
        _run(gc.nearby(LAT, LON, "   "))
    assert geocode.fetch.calls == []


def test_nearby_clamps_radius_and_limit(geocode) -> None:
    geocode.fetch.push(_overpass_body(
        {"lat": NEAR_LAT, "lon": NEAR_LON, "tags": {"amenity": "pharmacy", "name": "A"}},
        {"lat": FAR_LAT, "lon": FAR_LON, "tags": {"amenity": "pharmacy", "name": "B"}},
    ))
    out = _run(gc.nearby(LAT, LON, "apteka", radius_m=999_999, limit=999))
    assert out["radius_m"] == gc.RADIUS_MAX_M
    # limit is clamped to LIMIT_MAX; only 2 places exist, so both survive.
    assert len(out["places"]) == 2


def test_nearby_clamps_garbage_radius_to_the_default(geocode) -> None:
    geocode.fetch.push(_overpass_body())
    out = _run(gc.nearby(LAT, LON, "apteka", radius_m="wide", limit=None))
    assert out["radius_m"] == gc.RADIUS_DEFAULT_M


# ── reverse ───────────────────────────────────────────────────────


def test_reverse_returns_a_flat_address(geocode) -> None:
    geocode.fetch.push(_reverse_body())
    out = _run(gc.reverse(LAT, LON))
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert out["cached"] is False
    address = out["address"]
    assert address["road"] == "Krakowskie Przedmieście"
    assert address["house_number"] == "12"
    assert address["city"] == "Warszawa"
    assert address["postcode"] == "00-325"
    assert address["country"] == "Polska"
    assert address["display_name"].startswith("12, Krakowskie")


def test_reverse_sends_the_documented_nominatim_parameters(geocode) -> None:
    geocode.fetch.push(_reverse_body())
    _run(gc.reverse(LAT, LON))
    call = geocode.fetch.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == gc.NOMINATIM_REVERSE_URL
    assert call["params"]["format"] == "jsonv2"
    assert call["params"]["zoom"] == 18
    assert call["params"]["addressdetails"] == 1


@pytest.mark.parametrize(
    ("key", "expected"),
    [("town", "Piaseczno"), ("village", "Zalesie"), ("municipality", "Gmina X"),
     ("hamlet", "Kolonia"), ("suburb", "Mokotów")],
)
def test_reverse_accepts_every_city_spelling(geocode, key, expected) -> None:
    geocode.fetch.push(_reverse_body(address={key: expected}))
    assert _run(gc.reverse(LAT, LON))["address"]["city"] == expected


def test_reverse_tolerates_a_partial_address(geocode) -> None:
    geocode.fetch.push(_reverse_body(address={"country": "Polska"}))
    address = _run(gc.reverse(LAT, LON))["address"]
    assert address["country"] == "Polska"
    assert address["road"] is None
    assert address["city"] is None


def test_reverse_caches_on_rounded_coordinates(geocode) -> None:
    """GPS jitter under ~11 m must reuse the entry, not re-hit Nominatim."""
    geocode.fetch.push(_reverse_body())
    first = _run(gc.reverse(LAT, LON))
    second = _run(gc.reverse(LAT + 0.000001, LON - 0.000001))
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(geocode.fetch.calls) == 1


def test_reverse_cache_expires_after_its_ttl(geocode) -> None:
    geocode.fetch.push(_reverse_body())
    _run(gc.reverse(LAT, LON))
    geocode.clock.advance(gc.REVERSE_CACHE_TTL_S + 1)
    geocode.fetch.push(_reverse_body())
    assert _run(gc.reverse(LAT, LON))["cached"] is False
    assert len(geocode.fetch.calls) == 2


def test_reverse_cache_returns_a_copy(geocode) -> None:
    geocode.fetch.push(_reverse_body())
    first = _run(gc.reverse(LAT, LON))
    first["address"]["road"] = "Mutated"
    assert _run(gc.reverse(LAT, LON))["address"]["road"] == "Krakowskie Przedmieście"


def test_clear_caches_forces_a_fresh_lookup(geocode) -> None:
    geocode.fetch.push(_reverse_body())
    _run(gc.reverse(LAT, LON))
    gc.clear_caches()
    geocode.fetch.push(_reverse_body())
    assert _run(gc.reverse(LAT, LON))["cached"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"error": "Unable to geocode"},
        [],           # a list where an object belongs
        "nope",
        None,
        {},           # neither display_name nor address
    ],
)
def test_reverse_reports_an_unusable_body_as_geocode_failed(geocode, body) -> None:
    geocode.fetch.push(body)
    out = _run(gc.reverse(LAT, LON))
    assert out["ok"] is False
    assert out["status"] == "geocode_failed"
    assert out["retryable"] is True
    assert out["hint"]


def test_reverse_does_not_cache_a_failure(geocode) -> None:
    geocode.fetch.push({"error": "Unable to geocode"})
    _run(gc.reverse(LAT, LON))
    geocode.fetch.push(_reverse_body())
    assert _run(gc.reverse(LAT, LON))["ok"] is True


# ── upstream failure modes ────────────────────────────────────────


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", gc.NOMINATIM_REVERSE_URL)
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


@pytest.mark.parametrize("code", [429, 403, 500, 503])
def test_upstream_http_error_becomes_a_typed_retryable_refusal(geocode, code) -> None:
    """A ban or a hiccup must reach the agent as data, never as a traceback."""
    geocode.fetch.push(_http_status_error(code))
    out = _run(gc.reverse(LAT, LON))
    assert out == {
        "ok": False, "status": "geocode_failed", "terminal": True,
        **gc.geo_mod.describe("geocode_failed"),
        "upstream": "nominatim/reverse", "http_status": code,
    }


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("too slow"),
        httpx.RemoteProtocolError("garbled"),
        OSError("dns"),
        asyncio.TimeoutError(),
        ValueError("not json"),
    ],
)
def test_transport_and_parse_failures_become_geocode_failed(geocode, exc) -> None:
    geocode.fetch.push(exc)
    out = _run(gc.reverse(LAT, LON))
    assert out["ok"] is False
    assert out["status"] == "geocode_failed"
    assert out["upstream"] == "nominatim/reverse"


def test_a_failed_lookup_is_not_retried_inside_the_module(geocode) -> None:
    """ONE attempt: retrying here would multiply load on an already-sick host."""
    geocode.fetch.push(_http_status_error(503))
    _run(gc.reverse(LAT, LON))
    assert len(geocode.fetch.calls) == 1


# ── nearby: the Overpass (curated keyword) path ───────────────────


def test_nearby_by_tag_sorts_by_distance(geocode) -> None:
    geocode.fetch.push(_overpass_body(
        {"lat": FAR_LAT, "lon": FAR_LON, "tags": {"amenity": "pharmacy", "name": "Daleka"}},
        {"lat": NEAR_LAT, "lon": NEAR_LON, "tags": {"amenity": "pharmacy", "name": "Bliska"}},
    ))
    out = _run(gc.nearby(LAT, LON, "apteka", radius_m=3000))
    assert out["ok"] is True
    assert out["source"] == "overpass"
    assert out["tag"] == "amenity=pharmacy"
    assert out["query"] == "apteka"
    assert [p["name"] for p in out["places"]] == ["Bliska", "Daleka"]
    assert out["places"][0]["distance_m"] < out["places"][1]["distance_m"]
    assert out["places"][0]["kind"] == "pharmacy"


def test_nearby_by_tag_posts_bounded_overpass_ql(geocode) -> None:
    geocode.fetch.push(_overpass_body())
    _run(gc.nearby(LAT, LON, "apteka", radius_m=800))
    call = geocode.fetch.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == gc.OVERPASS_URL
    ql = call["data"]["data"]
    assert '["amenity"="pharmacy"]' in ql
    assert "(around:800," in ql
    assert ql.startswith("[out:json][timeout:")


def test_nearby_bare_key_tag_selects_any_value(geocode) -> None:
    geocode.fetch.push(_overpass_body())
    _run(gc.nearby(LAT, LON, "sklep"))
    ql = geocode.fetch.calls[0]["data"]["data"]
    assert '["shop"]' in ql
    assert "=" not in ql.split("node")[1].split(";")[0]


def test_nearby_reads_the_center_of_a_way(geocode) -> None:
    # Ways/relations have no lat/lon of their own — Overpass `out center` adds
    # a `center` object instead, and a POI we cannot plot is worthless.
    geocode.fetch.push(_overpass_body(
        {"type": "way", "center": {"lat": NEAR_LAT, "lon": NEAR_LON},
         "tags": {"amenity": "pharmacy", "name": "W bloku"}},
    ))
    out = _run(gc.nearby(LAT, LON, "apteka"))
    assert out["places"][0]["lat"] == pytest.approx(NEAR_LAT)


@pytest.mark.parametrize(
    "element",
    ["not-a-dict", {}, {"lat": "x", "lon": "y"}, {"lat": None, "lon": None},
     {"lat": float("nan"), "lon": 21.0}],
)
def test_nearby_skips_unplottable_elements(geocode, element) -> None:
    geocode.fetch.push(_overpass_body(
        element,
        {"lat": NEAR_LAT, "lon": NEAR_LON, "tags": {"amenity": "pharmacy"}},
    ))
    out = _run(gc.nearby(LAT, LON, "apteka"))
    assert len(out["places"]) == 1


def test_nearby_place_without_a_name_is_kept_but_nameless(geocode) -> None:
    geocode.fetch.push(_overpass_body(
        {"lat": NEAR_LAT, "lon": NEAR_LON, "tags": {"amenity": "atm"}},
    ))
    place = _run(gc.nearby(LAT, LON, "bankomat"))["places"][0]
    assert place["name"] is None
    assert place["kind"] == "atm"


@pytest.mark.parametrize("body", [{"elements": "nope"}, {}, [], "garbage"])
def test_nearby_unexpected_overpass_shape_is_typed(geocode, body) -> None:
    geocode.fetch.push(body)
    out = _run(gc.nearby(LAT, LON, "apteka"))
    assert out["ok"] is False
    assert out["status"] == "geocode_failed"
    assert out["upstream"] == "overpass"


def test_nearby_overpass_failure_is_forwarded(geocode) -> None:
    geocode.fetch.push(_http_status_error(429))
    out = _run(gc.nearby(LAT, LON, "apteka"))
    assert out["ok"] is False
    assert out["retryable"] is True


# ── nearby: the Nominatim (free text) path ────────────────────────


def test_nearby_free_text_falls_back_to_nominatim_search(geocode) -> None:
    geocode.fetch.push([
        {"lat": str(NEAR_LAT), "lon": str(NEAR_LON), "type": "florist",
         "name": "Kwiaciarnia", "display_name": "Kwiaciarnia, Warszawa"},
    ])
    out = _run(gc.nearby(LAT, LON, "kwiaciarnia u zosi"))
    assert out["source"] == "nominatim"
    assert out["tag"] is None
    assert out["places"][0]["name"] == "Kwiaciarnia"
    assert out["places"][0]["kind"] == "florist"
    call = geocode.fetch.calls[0]
    assert call["url"] == gc.NOMINATIM_SEARCH_URL
    # The user's text is a URL PARAMETER here, never QL source.
    assert call["params"]["q"] == "kwiaciarnia u zosi"
    assert call["params"]["bounded"] == 1
    assert len(call["params"]["viewbox"].split(",")) == 4


@pytest.mark.parametrize("body", [{"elements": []}, {}, "garbage", None])
def test_nearby_unexpected_nominatim_shape_is_typed(geocode, body) -> None:
    geocode.fetch.push(body)
    out = _run(gc.nearby(LAT, LON, "kwiaciarnia u zosi"))
    assert out["ok"] is False
    assert out["upstream"] == "nominatim/search"


def test_nearby_skips_unparseable_nominatim_hits(geocode) -> None:
    geocode.fetch.push(["nope", {"lat": "x"},
                        {"lat": NEAR_LAT, "lon": NEAR_LON, "display_name": "OK"}])
    out = _run(gc.nearby(LAT, LON, "kwiaciarnia u zosi"))
    assert [p["name"] for p in out["places"]] == ["OK"]


# ── nearby: caching ───────────────────────────────────────────────


def test_nearby_caches_on_coords_query_and_radius(geocode) -> None:
    geocode.fetch.push(_overpass_body(
        {"lat": NEAR_LAT, "lon": NEAR_LON, "tags": {"amenity": "pharmacy", "name": "A"}},
    ))
    first = _run(gc.nearby(LAT, LON, "apteka", radius_m=1500))
    second = _run(gc.nearby(LAT, LON, "apteka", radius_m=1500))
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(geocode.fetch.calls) == 1


def test_nearby_a_different_radius_is_a_different_key(geocode) -> None:
    geocode.fetch.push(_overpass_body())
    geocode.fetch.push(_overpass_body())
    _run(gc.nearby(LAT, LON, "apteka", radius_m=1500))
    _run(gc.nearby(LAT, LON, "apteka", radius_m=500))
    assert len(geocode.fetch.calls) == 2


def test_nearby_cache_expires_after_its_ttl(geocode) -> None:
    geocode.fetch.push(_overpass_body())
    _run(gc.nearby(LAT, LON, "apteka"))
    geocode.clock.advance(gc.NEARBY_CACHE_TTL_S + 1)
    geocode.fetch.push(_overpass_body())
    assert _run(gc.nearby(LAT, LON, "apteka"))["cached"] is False


def test_nearby_cache_returns_copies(geocode) -> None:
    geocode.fetch.push(_overpass_body(
        {"lat": NEAR_LAT, "lon": NEAR_LON, "tags": {"amenity": "pharmacy", "name": "A"}},
    ))
    first = _run(gc.nearby(LAT, LON, "apteka"))
    first["places"][0]["name"] = "Mutated"
    assert _run(gc.nearby(LAT, LON, "apteka"))["places"][0]["name"] == "A"
