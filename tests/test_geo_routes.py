"""HTTP contract for ``/api/orchestrator/geo/*`` (:mod:`geo_routes`).

The client is built AFTER the monkeypatching (the ``a2a_route_client``
pattern): ``register_geo_routes`` captures ``events_mod.get_hub`` at
registration time, so a hub stub installed later would never be seen. Building
the app inside the fixture is what makes the stub hub — and therefore the
"is a browser attached?" branch — reachable at all.

The flag-off group is deliberately exhaustive: it IS the level-1 rollback
documentation. If a future route forgets its ``_require_flag()``, the kill
switch silently stops being a kill switch, and only a test that walks every
path notices.

Nothing here talks to a browser, a tmux pool or OpenStreetMap: the hub is a
stub, and the geocode module's single HTTP seam is replaced per test.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orbit import app as app_mod
from orbit import geo_routes
from orbit import orchestrator_artifacts as artifacts_mod
from orbit import orchestrator_events as events_mod
from orbit import orchestrator_geo as geo_mod
from orbit import orchestrator_geocode as geocode_mod
from orbit import orchestrator_meta as meta_mod
from orbit import orchestrator_settings as settings_mod

SID = "00000000-0000-0000-0000-000000000000"
SID2 = "11111111-1111-1111-1111-111111111111"
TOKEN = "geo-test-token"
AUTH = {"x-geo-token": TOKEN}

BASE = "/api/orchestrator/geo"
COORDS: dict[str, Any] = {"lat": 52.2296756, "lon": 21.0122287, "accuracy_m": 18.4}

# (method, path) for every route an AGENT calls — token- and flag-gated.
AGENT_ROUTES: tuple[tuple[str, str], ...] = (
    ("post", f"{BASE}/request"),
    ("get", f"{BASE}/requests/aaaaaaaaaaaaaaaaaaaaaa/wait"),
    ("delete", f"{BASE}/requests/aaaaaaaaaaaaaaaaaaaaaa"),
    ("get", f"{BASE}/status"),
    ("get", f"{BASE}/last"),
    ("post", f"{BASE}/reverse"),
    ("post", f"{BASE}/nearby"),
)


class StubHub:
    """Only the three methods the geo core asks the real hub for."""

    def __init__(self, *, attached: tuple[str, ...] = (SID,),
                 live: tuple[str, ...] = (SID,)) -> None:
        self.attached = set(attached)
        self.live = list(live)
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    def subscriber_count(self, session_id: str) -> int:
        return 1 if session_id in self.attached else 0

    def live_session_ids(self) -> list[str]:
        return list(self.live)

    def publish(self, session_id: str, event: str, data: dict[str, Any]) -> None:
        self.published.append((session_id, event, dict(data)))


@pytest.fixture(scope="module")
def _geo_app():
    """Build the app ONCE, behind indirections a per-test fixture can re-aim.

    ``register_geo_routes`` binds ``events_mod.get_hub`` at registration time,
    so the patch has to be in place before ``create_app`` — but ``create_app``
    is expensive (it seeds the bundled skills), and doing it per test costs
    more than the rest of the suite combined. The compromise: the module-scoped
    patches read a mutable ``state`` dict that ``geo_api`` resets per test.
    """
    mp = pytest.MonkeyPatch()
    state: dict[str, Any] = {
        "hub": StubHub(),
        "token": TOKEN,
        "flags": {"geolocation_enabled": True, "geolocation_geocode_enabled": True},
    }
    original_get_flag = settings_mod.get_flag
    mp.setattr(events_mod, "get_hub", lambda: state["hub"])
    mp.setattr(artifacts_mod, "read_token", lambda: state["token"])
    mp.setattr(meta_mod, "get_meta",
               lambda sid: {"lib_id": "areas/Home", "title": "Dom"})
    mp.setattr(
        settings_mod, "get_flag",
        lambda name: (state["flags"][name] if name in state["flags"]
                      else original_get_flag(name)),
    )
    app = app_mod.create_app()
    yield app, state
    mp.undo()


@pytest.fixture
def geo_api(_geo_app, monkeypatch):
    """A TestClient with geo enabled, a known token, a stub hub and no network."""
    app, state = _geo_app
    state["hub"] = StubHub()
    state["token"] = TOKEN
    state["flags"]["geolocation_enabled"] = True
    state["flags"]["geolocation_geocode_enabled"] = True

    monkeypatch.setattr(geo_mod, "_PENDING", {})
    monkeypatch.setattr(geo_mod, "_last_fix", None)
    monkeypatch.setattr(geo_mod, "_last_prompt_at", 0.0)
    monkeypatch.setattr(geo_mod, "_wait_inflight", 0)

    async def _no_network(*a, **k):
        raise AssertionError("a route test tried to reach OpenStreetMap")

    monkeypatch.setattr(geocode_mod, "_fetch_json", _no_network)
    monkeypatch.setattr(geocode_mod._nominatim_gate, "_last", 0.0)
    monkeypatch.setattr(geocode_mod._overpass_gate, "_last", 0.0)
    geocode_mod.clear_caches()

    client = TestClient(app, raise_server_exceptions=True)
    yield SimpleNamespace(client=client, hub=state["hub"], flags=state["flags"],
                          state=state, monkeypatch=monkeypatch)
    geocode_mod.clear_caches()


def _call(geo_api, method: str, path: str, *, headers: dict[str, str] | None = None):
    """Hit one route generically. ``get``/``delete`` take no JSON body in httpx."""
    fn = getattr(geo_api.client, method)
    if method == "post":
        return fn(path, headers=headers, json={})
    return fn(path, headers=headers)


def _create(geo_api, **body: Any):
    payload = {"session_id": SID, **body}
    return geo_api.client.post(f"{BASE}/request", headers=AUTH, json=payload)


def _answer(geo_api, request_id: str, **body: Any):
    return geo_api.client.post(f"{BASE}/requests/{request_id}/answer", json=body)


# ── the kill switch (level-1 rollback documentation) ──────────────


@pytest.mark.parametrize(("method", "path"), AGENT_ROUTES)
def test_every_agent_route_403s_with_the_flag_off(geo_api, method, path) -> None:
    geo_api.flags["geolocation_enabled"] = False
    response = _call(geo_api, method, path, headers=AUTH)
    assert response.status_code == 403, response.text
    assert "Ustawienia" in response.json()["detail"], response.text


def test_flag_off_leaves_the_browser_routes_working(geo_api) -> None:
    """"Forget my location" must keep working with the feature switched off."""
    geo_api.flags["geolocation_enabled"] = False
    forget = geo_api.client.post(f"{BASE}/forget")
    assert forget.status_code == 200, forget.text
    assert forget.json()["ok"] is True
    pending = geo_api.client.get(f"{BASE}/requests/pending", params={"session_id": SID})
    assert pending.status_code == 200, pending.text


def test_flag_off_stops_a_stale_tab_from_answering(geo_api) -> None:
    """The kill switch must DELETE, not hide: no coordinate may land after it.

    A phone with a prompt already on screen has not refetched the settings, so
    it will still POST — and a 403 would only make it retry. It gets a typed
    body instead, and its fix never becomes the cache.
    """
    rid = _create(geo_api).json()["request_id"]
    geo_api.flags["geolocation_enabled"] = False

    answered = _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID)
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"ok": True, "accepted": False, "status": "disabled"}
    assert geo_mod.last_fix(300) is None


def test_flag_off_hides_the_pending_list(geo_api) -> None:
    """A tab whose useServerSettings hasn't refetched must not pick up an ask."""
    _create(geo_api)
    geo_api.flags["geolocation_enabled"] = False
    body = geo_api.client.get(f"{BASE}/requests/pending",
                              params={"session_id": SID}).json()
    assert body == {"ok": True, "requests": []}


def test_disabling_the_feature_sweeps_and_forgets(geo_api) -> None:
    """`on_feature_disabled` is what makes "nothing survives" true."""
    rid = _create(geo_api).json()["request_id"]
    _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID)
    geo_api.monkeypatch.setattr(geo_mod, "_last_prompt_at", 0.0)   # past anti-nag
    second = _create(geo_api, max_age_s=0).json()
    assert geo_mod.last_fix(300) is not None
    assert geo_mod.active_requests()

    geo_routes.on_feature_disabled()
    assert geo_mod.last_fix(300) is None
    assert geo_mod.active_requests() == []
    # The abandoned prompt is told to leave the screen, not left dangling.
    cancels = [row for row in geo_api.hub.published if row[1] == geo_mod.EVENT_CANCELLED]
    assert [row[2]["request_id"] for row in cancels] == [second["request_id"]]


# ── the token gate ────────────────────────────────────────────────


@pytest.mark.parametrize(("method", "path"), AGENT_ROUTES)
def test_every_agent_route_403s_without_a_token(geo_api, method, path) -> None:
    response = _call(geo_api, method, path)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "invalid geo token"


@pytest.mark.parametrize(("method", "path"), AGENT_ROUTES)
def test_every_agent_route_403s_on_a_wrong_token(geo_api, method, path) -> None:
    response = _call(geo_api, method, path, headers={"x-geo-token": "nope"})
    assert response.status_code == 403, response.text


def test_a_missing_token_file_fails_closed(geo_api) -> None:
    """No token on disk must mean "nobody gets in", not "everybody gets in"."""
    geo_api.state["token"] = ""
    response = geo_api.client.post(f"{BASE}/request", headers={"x-geo-token": ""},
                                   json={"session_id": SID})
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", f"{BASE}/requests/aaaaaaaaaaaaaaaaaaaaaa/answer", {"status": "denied"}),
        ("post", f"{BASE}/forget", None),
    ],
)
def test_browser_routes_need_no_token(geo_api, method, path, body) -> None:
    # A browser holds no CLI secret — the request_id is the capability instead.
    response = getattr(geo_api.client, method)(path, json=body)
    assert response.status_code != 403, response.text


def test_pending_route_needs_no_token(geo_api) -> None:
    response = geo_api.client.get(f"{BASE}/requests/pending", params={"session_id": SID})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "requests": []}


# ── create ────────────────────────────────────────────────────────


def test_create_returns_pending_and_publishes(geo_api) -> None:
    response = _create(geo_api, reason="gdzie jestem")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "pending"
    assert body["terminal"] is False
    assert body["targets_n"] == 1
    assert geo_mod.REQUEST_ID_RE.fullmatch(body["request_id"])
    (target, event, payload), = geo_api.hub.published
    assert (target, event) == (SID, geo_mod.EVENT_REQUEST)
    assert payload["reason"] == "gdzie jestem"
    # The consent UI needs to say WHO is asking.
    assert payload["agent"] == {"lib_id": "areas/Home", "title": "Dom"}


@pytest.mark.parametrize(
    "session_id",
    [None, "", "not-a-uuid", "../../etc/passwd", 42, SID + "x",
     "AAAAAAAA-0000-0000-0000-000000000000"],  # uppercase hex — the RE is strict
)
def test_create_rejects_a_bad_session_id(geo_api, session_id) -> None:
    response = geo_api.client.post(f"{BASE}/request", headers=AUTH,
                                   json={"session_id": session_id})
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "invalid session_id"


def test_create_rejects_a_non_object_body(geo_api) -> None:
    response = geo_api.client.post(f"{BASE}/request", headers=AUTH, json=["nope"])
    assert response.status_code in (400, 422), response.text


def test_create_fast_fails_when_no_browser_is_attached(geo_api) -> None:
    geo_api.hub.attached.clear()
    geo_api.hub.live.clear()
    response = _create(geo_api)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "no_browser"
    assert body["retryable"] is False
    assert body["hint"]
    # Nothing registered ⇒ no agent can block for the full TTL on an empty room.
    assert geo_mod._PENDING == {}
    assert geo_api.hub.published == []


def test_create_fans_out_to_another_live_session(geo_api) -> None:
    geo_api.hub.attached.clear()
    geo_api.hub.live[:] = [SID2]
    response = _create(geo_api)
    assert response.json()["targets_n"] == 1
    assert [target for target, _, _ in geo_api.hub.published] == [SID2]


def test_create_serves_the_cache_without_prompting(geo_api) -> None:
    geo_mod.store_fix({"lat": 52.23, "lon": 21.01, "precision": "fine"})
    body = _create(geo_api).json()
    assert body["status"] == "cached"
    assert body["terminal"] is True
    assert body["fresh"] is False
    assert geo_api.hub.published == []


def test_create_with_max_age_zero_forces_a_prompt(geo_api) -> None:
    geo_mod.store_fix({"lat": 52.23, "lon": 21.01, "precision": "fine"})
    assert _create(geo_api, max_age_s=0).json()["status"] == "pending"


def test_create_anti_nag_is_reported_as_rate_limited(geo_api) -> None:
    assert _create(geo_api, max_age_s=0).json()["status"] == "pending"
    second = _create(geo_api, max_age_s=0).json()
    assert second["status"] == "rate_limited"
    assert second["terminal"] is False
    assert second["retry_after_s"] > 0


def test_create_429s_at_the_pending_cap(geo_api) -> None:
    geo_api.monkeypatch.setattr(geo_mod, "GEO_MAX_PENDING", 1)
    assert _create(geo_api).status_code == 200
    response = _create(geo_api)
    assert response.status_code == 429, response.text
    assert "pending" in response.json()["detail"]


# ── answer ────────────────────────────────────────────────────────


def test_answer_resolves_the_request(geo_api) -> None:
    rid = _create(geo_api).json()["request_id"]
    response = _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "accepted": True, "status": "fixed"}


def test_answer_404s_on_an_unknown_request(geo_api) -> None:
    response = _answer(geo_api, "z" * 22, status="denied")
    assert response.status_code == 404, response.text


# Kept inside ONE path segment: a literal "../.." is normalized away by the
# client before it ever reaches the route, so it would test nothing.
@pytest.mark.parametrize("rid", ["short", "a" * 200, "has spaces", "id.with.dots"])
def test_answer_400s_on_a_malformed_request_id(geo_api, rid) -> None:
    response = _answer(geo_api, rid, status="denied")
    assert response.status_code == 400, response.text


@pytest.mark.parametrize(
    "coords",
    [
        {"lat": 91.0, "lon": 21.0, "accuracy_m": 5},
        {"lat": 52.0, "lon": 181.0, "accuracy_m": 5},
        {"lat": "north", "lon": 21.0, "accuracy_m": 5},
        {"lat": 52.0, "lon": 21.0, "accuracy_m": 0},
        {"lat": 52.0, "lon": 21.0},
        {},
        None,
    ],
)
def test_answer_400s_on_out_of_range_coordinates(geo_api, coords) -> None:
    rid = _create(geo_api).json()["request_id"]
    response = _answer(geo_api, rid, status="fixed", coords=coords)
    assert response.status_code == 400, response.text
    # The request survives a rejected answer — the user can still try again.
    assert geo_mod._PENDING[rid].result is None


def test_answer_400s_on_an_unknown_status(geo_api) -> None:
    rid = _create(geo_api).json()["request_id"]
    response = _answer(geo_api, rid, status="pwned")
    assert response.status_code == 400, response.text


def test_answer_strips_keys_the_browser_invented(geo_api) -> None:
    rid = _create(geo_api).json()["request_id"]
    _answer(geo_api, rid, status="fixed", session_id=SID,
            coords={**COORDS, "note": "hi", "__proto__": {"x": 1}})
    fix = geo_mod._PENDING[rid].result["fix"]
    assert set(fix) == set(geo_mod.COORD_KEYS) | {"precision", "ts_ms"}


def test_a_losing_device_is_told_politely_not_404ed(geo_api) -> None:
    geo_api.hub.attached.clear()
    geo_api.hub.live[:] = [SID, SID2]
    rid = _create(geo_api).json()["request_id"]
    _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID)
    late = _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID2)
    assert late.status_code == 200, late.text
    assert late.json() == {"ok": True, "accepted": False, "status": "fixed"}


# ── wait ──────────────────────────────────────────────────────────


def test_wait_returns_a_terminal_answer(geo_api) -> None:
    rid = _create(geo_api).json()["request_id"]
    _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID)
    response = geo_api.client.get(f"{BASE}/requests/{rid}/wait", headers=AUTH,
                                 params={"timeout": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "fixed"
    assert body["terminal"] is True
    assert body["source"] == "browser-gps"
    assert body["fix"]["lat"] == pytest.approx(52.22968)
    assert body["fix"]["precision"] == "fine"


def test_wait_poll_window_timeout_is_non_terminal(geo_api) -> None:
    """``terminal`` is the ONE discriminator between the two 'timeout's.

    The CLI re-polls iff ``terminal is False``; getting this wrong either hangs
    the agent forever or gives up on a request the user is still answering.
    """
    rid = _create(geo_api).json()["request_id"]
    response = geo_api.client.get(f"{BASE}/requests/{rid}/wait", headers=AUTH,
                                  params={"timeout": 0.05})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "timeout"
    assert body["terminal"] is False
    assert body["reason"] == "poll_window_elapsed"
    # The slot survives, so the same id resolves on the next poll.
    _answer(geo_api, rid, status="denied", session_id=SID)
    again = geo_api.client.get(f"{BASE}/requests/{rid}/wait", headers=AUTH,
                               params={"timeout": 1})
    assert again.json()["status"] == "denied"
    assert again.json()["terminal"] is True


def test_wait_on_an_unknown_request_is_not_found(geo_api) -> None:
    response = geo_api.client.get(f"{BASE}/requests/{'z' * 22}/wait", headers=AUTH,
                                  params={"timeout": 0.05})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_found"
    assert response.json()["terminal"] is True


def test_wait_400s_on_a_malformed_request_id(geo_api) -> None:
    response = geo_api.client.get(f"{BASE}/requests/nope/wait", headers=AUTH)
    assert response.status_code == 400, response.text


def test_wait_429s_at_the_concurrency_cap(geo_api) -> None:
    geo_api.monkeypatch.setattr(geo_mod, "GEO_WAIT_MAX_CONCURRENT", 0)
    rid = _create(geo_api).json()["request_id"]
    response = geo_api.client.get(f"{BASE}/requests/{rid}/wait", headers=AUTH,
                                  params={"timeout": 0.05})
    assert response.status_code == 429, response.text


# ── cancel ────────────────────────────────────────────────────────


def test_cancel_aborts_and_is_idempotent(geo_api) -> None:
    rid = _create(geo_api).json()["request_id"]
    first = geo_api.client.delete(f"{BASE}/requests/{rid}", headers=AUTH)
    assert first.json() == {"ok": True, "cancelled": True, "status": "aborted"}
    second = geo_api.client.delete(f"{BASE}/requests/{rid}", headers=AUTH)
    assert second.json() == {"ok": True, "cancelled": False, "status": "aborted"}
    cancels = [row for row in geo_api.hub.published if row[1] == geo_mod.EVENT_CANCELLED]
    assert cancels[0][2]["reason"] == "aborted"


def test_cancel_of_an_unknown_request_is_a_clean_no_op(geo_api) -> None:
    response = geo_api.client.delete(f"{BASE}/requests/{'z' * 22}", headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_found"


def test_cancel_400s_on_a_malformed_request_id(geo_api) -> None:
    assert geo_api.client.delete(f"{BASE}/requests/nope", headers=AUTH).status_code == 400


# ── pending (the reconnect guarantee) ─────────────────────────────


def test_pending_lists_only_unexpired_unanswered_asks(geo_api) -> None:
    rid = _create(geo_api).json()["request_id"]
    listed = geo_api.client.get(f"{BASE}/requests/pending",
                                params={"session_id": SID}).json()["requests"]
    assert [row["request_id"] for row in listed] == [rid]
    assert set(listed[0]) == {"request_id", "reason", "accuracy", "max_age_s",
                              "expires_at_ms", "agent"}
    _answer(geo_api, rid, status="denied", session_id=SID)
    after = geo_api.client.get(f"{BASE}/requests/pending",
                               params={"session_id": SID}).json()["requests"]
    assert after == []


def test_pending_400s_on_a_bad_session_id(geo_api) -> None:
    response = geo_api.client.get(f"{BASE}/requests/pending",
                                  params={"session_id": "nope"})
    assert response.status_code == 400, response.text


def test_pending_422s_without_a_session_id(geo_api) -> None:
    assert geo_api.client.get(f"{BASE}/requests/pending").status_code == 422


# ── status (preflight) ────────────────────────────────────────────


def test_status_reports_the_room(geo_api) -> None:
    _create(geo_api)
    body = geo_api.client.get(f"{BASE}/status", headers=AUTH,
                              params={"session_id": SID}).json()
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["geocode_enabled"] is True
    assert body["browser_attached"] is True
    assert body["browsers_n"] == 1
    assert body["pending_n"] == 1
    assert body["last_fix_age_s"] is None
    assert body["wait_inflight"] == 0


def test_status_without_a_session_id(geo_api) -> None:
    body = geo_api.client.get(f"{BASE}/status", headers=AUTH).json()
    assert body["browser_attached"] is False
    assert body["browsers_n"] == 1


def test_status_400s_on_a_bad_session_id(geo_api) -> None:
    response = geo_api.client.get(f"{BASE}/status", headers=AUTH,
                                  params={"session_id": "nope"})
    assert response.status_code == 400, response.text


def test_status_mirrors_the_geocode_flag(geo_api) -> None:
    geo_api.flags["geolocation_geocode_enabled"] = False
    body = geo_api.client.get(f"{BASE}/status", headers=AUTH).json()
    assert body["geocode_enabled"] is False


# ── the read-only cache view ──────────────────────────────────────


def test_last_reads_the_cache_without_registering_an_ask(geo_api) -> None:
    """`geo last` is documented as never prompting — this route is why.

    Emulating it with a wide-max_age create published to the browser BEFORE it
    could discover the cache was empty, so an empty cache fired a real
    permission prompt for a read-only verb.
    """
    geo_mod.store_fix({"lat": 52.22968, "lon": 21.01223, "accuracy_m": 12.0,
                       "precision": "fine"})
    body = geo_api.client.get(f"{BASE}/last", headers=AUTH).json()
    assert body["status"] == "cached"
    assert body["fix"]["lat"] == pytest.approx(52.22968)
    assert geo_api.hub.published == []
    assert geo_mod._PENDING == {}


def test_last_on_an_empty_cache_is_a_clean_miss(geo_api) -> None:
    body = geo_api.client.get(f"{BASE}/last", headers=AUTH).json()
    assert body == {"ok": True, "cached": False, "fix": None}
    assert geo_api.hub.published == []


def test_last_serves_a_coarse_entry_as_stored(geo_api) -> None:
    """It asks for no precision, so it can be refused none — it just reads."""
    geo_mod.store_fix({"lat": 52.23, "lon": 21.01, "accuracy_m": 1000.0,
                       "precision": "coarse"})
    body = geo_api.client.get(f"{BASE}/last", headers=AUTH).json()
    assert body["status"] == "cached"
    assert body["fix"]["precision"] == "coarse"


# ── forget ────────────────────────────────────────────────────────


def test_forget_clears_the_fix_and_the_geocode_caches(geo_api) -> None:
    geo_mod.store_fix({"lat": 52.23, "lon": 21.01})
    geocode_mod._reverse_cache.put((52.23, 21.01), {"display_name": "x"})
    response = geo_api.client.post(f"{BASE}/forget")
    assert response.json() == {"ok": True, "forgotten": True}
    assert geo_mod.last_fix(300) is None
    # A cached address is a derived location — "forget" has to take it too.
    assert geocode_mod._reverse_cache.get((52.23, 21.01)) is None
    assert geo_api.client.post(f"{BASE}/forget").json()["forgotten"] is False


# ── geocoding ─────────────────────────────────────────────────────


def _seed_fetch(geo_api, result: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fetch(method, url, *, params=None, data=None):
        calls.append({"method": method, "url": url, "params": params, "data": data})
        return result

    geo_api.monkeypatch.setattr(geocode_mod, "_fetch_json", _fetch)
    return calls


def test_reverse_returns_an_address(geo_api) -> None:
    _seed_fetch(geo_api, {
        "display_name": "12, Krakowskie Przedmieście, Warszawa",
        "address": {"road": "Krakowskie Przedmieście", "city": "Warszawa"},
    })
    response = geo_api.client.post(f"{BASE}/reverse", headers=AUTH,
                                   json={"lat": 52.2296756, "lon": 21.0122287})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["address"]["city"] == "Warszawa"


@pytest.mark.parametrize("payload", [{}, {"lat": 91, "lon": 21}, {"lat": "x", "lon": 21}])
def test_reverse_400s_on_bad_coordinates(geo_api, payload) -> None:
    response = geo_api.client.post(f"{BASE}/reverse", headers=AUTH, json=payload)
    assert response.status_code == 400, response.text


def test_reverse_with_the_geocode_flag_off_returns_a_body_not_a_403(geo_api) -> None:
    """The CLI must be able to DEGRADE to raw lat/lon, so this is 200 + status."""
    geo_api.flags["geolocation_geocode_enabled"] = False
    response = geo_api.client.post(f"{BASE}/reverse", headers=AUTH,
                                   json={"lat": 52.23, "lon": 21.01})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "geocode_disabled"
    assert body["retryable"] is False
    assert body["hint"]


def test_nearby_returns_places(geo_api) -> None:
    calls = _seed_fetch(geo_api, {"elements": [
        {"lat": 52.2310, "lon": 21.0122,
         "tags": {"amenity": "pharmacy", "name": "Apteka Bliska"}},
    ]})
    response = geo_api.client.post(f"{BASE}/nearby", headers=AUTH, json={
        "lat": 52.2296756, "lon": 21.0122287, "query": "apteka", "radius_m": 800,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "overpass"
    assert body["tag"] == "amenity=pharmacy"
    assert body["radius_m"] == 800
    assert body["places"][0]["name"] == "Apteka Bliska"
    assert calls[0]["url"] == geocode_mod.OVERPASS_URL


def test_nearby_400s_without_a_query(geo_api) -> None:
    response = geo_api.client.post(f"{BASE}/nearby", headers=AUTH,
                                   json={"lat": 52.23, "lon": 21.01})
    assert response.status_code == 400, response.text


def test_nearby_with_the_geocode_flag_off_returns_a_body(geo_api) -> None:
    geo_api.flags["geolocation_geocode_enabled"] = False
    body = geo_api.client.post(f"{BASE}/nearby", headers=AUTH, json={
        "lat": 52.23, "lon": 21.01, "query": "apteka",
    }).json()
    assert body["status"] == "geocode_disabled"


def test_upstream_failure_surfaces_as_geocode_failed(geo_api) -> None:
    async def _boom(*a, **k):
        raise TimeoutError("osm is slow")

    geo_api.monkeypatch.setattr(geocode_mod, "_fetch_json", _boom)
    body = geo_api.client.post(f"{BASE}/reverse", headers=AUTH,
                               json={"lat": 52.23, "lon": 21.01}).json()
    assert body["ok"] is False
    assert body["status"] == "geocode_failed"
    assert body["retryable"] is True


# ── the whole round trip ──────────────────────────────────────────


def test_full_round_trip_over_http(geo_api) -> None:
    """create → SSE push → browser answers → the agent's /wait resolves."""
    created = _create(geo_api, reason="co jest najbliżej", accuracy="coarse").json()
    rid = created["request_id"]

    # The browser learns about the ask over the session's own SSE channel.
    (_, event, pushed), = geo_api.hub.published
    assert event == geo_mod.EVENT_REQUEST
    assert pushed["request_id"] == rid

    accepted = _answer(geo_api, rid, status="fixed", coords=COORDS,
                       ts_ms=geo_mod.now_ms(), session_id=SID).json()
    assert accepted["accepted"] is True

    waited = geo_api.client.get(f"{BASE}/requests/{rid}/wait", headers=AUTH,
                                params={"timeout": 1}).json()
    assert waited["status"] == "fixed"
    # `coarse` was honoured end to end: 2 dp + a 1 km accuracy floor.
    assert waited["fix"]["precision"] == "coarse"
    assert waited["fix"]["lat"] == pytest.approx(52.23)
    assert waited["fix"]["accuracy_m"] == 1000.0

    # …and the next agent asking for the SAME precision gets it from the cache
    # with no second prompt.
    geo_api.hub.published.clear()
    cached = _create(geo_api, accuracy="coarse").json()
    assert cached["status"] == "cached", cached
    assert cached["fix"]["precision"] == "coarse"
    assert geo_api.hub.published == []


def test_a_fine_ask_is_not_answered_from_a_coarse_cache(geo_api) -> None:
    """`coarse` is a privacy control, so it can only ever be handed DOWNWARDS.

    Serving this entry would give an agent that asked for metres ~1 km of error
    labelled ``precision: fine`` — and SKILL.md actively tells the model to fall
    back to ``--accuracy coarse``, so the entry is there routinely.
    """
    rid = _create(geo_api, accuracy="coarse").json()["request_id"]
    _answer(geo_api, rid, status="fixed", coords=COORDS, session_id=SID)
    # Past the anti-nag window, so a miss here is the PRECISION rule, not it.
    geo_api.monkeypatch.setattr(geo_mod, "_last_prompt_at", 0.0)

    fresh = _create(geo_api, accuracy="fine").json()
    assert fresh["status"] == "pending", fresh


def test_capabilities_advertises_the_feature(geo_api) -> None:
    body = geo_api.client.get("/api/orchestrator/capabilities").json()
    assert body["features"]["geolocation"] is True
    assert body["features"]["geolocation_geocode"] is True
    assert "geo_request" in body["sse_events"]
    assert "geo_cancelled" in body["sse_events"]
    assert body["limits"]["geo_wait_timeout_max_s"] == geo_mod.GEO_WAIT_TIMEOUT_MAX_S
    assert body["limits"]["geo_max_pending"] == geo_mod.GEO_MAX_PENDING
