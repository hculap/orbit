#!/usr/bin/env python3
"""
geo_lib.py — importable, stdlib-only API for the `geo` CLI.

An agent running on this box cannot see where its human is standing; the
BROWSER can. This module is the agent-side half of that round trip: it creates
the ask, blocks until the browser answers, and hands back ONE typed payload —
never a traceback, never an unbounded hang.

Three things here are not obvious and are load-bearing:

  1. **The poll window is not the request.** ``GET …/wait`` answers
     ``{"status":"timeout","terminal":false}`` when its 45 s window elapsed
     while the ask is still very much alive. ``terminal`` is the ONLY
     discriminator (a real browser-side timeout is ``terminal:true``), so
     :func:`resolve_fix` re-polls the SAME request id until something terminal
     arrives or the overall budget runs out. The agent never sees the churn.
  2. **Failures are typed, not silent.** Unlike ``artifacts_lib.notify`` (whose
     job is to never break artifact creation), a dead dashboard here means the
     agent asked a question it will not get an answer to — so every transport
     error becomes a payload carrying ``status`` / ``retryable`` / ``hint``,
     exactly like a server-side refusal. The caller has one shape to read.
  3. **``retryable`` is the whole contract.** The server's :data:`STATUSES`
     table travels inside the payload; :data:`LOCAL_STATUSES` below only fills
     in the rows the server CANNOT send us (a bare HTTP 403/404/429, a socket
     error, a client-side abort). Never invent a status outside these two.

Import-safe: importing does NO I/O and reads NO env — all discovery happens
lazily inside functions, so a test or another skill can import this freely.

Config resolution for the dashboard URL (first match wins):
    1. $HD_NOTIFY_URL
    2. <skill_dir>/config.json  "dashboard_url"
    3. http://localhost:8766

Auth token (sent as ``X-Geo-Token``, the SAME token the artifacts skill uses):
    HD_ARTIFACT_TOKEN_FILE → ~/.orchestrator/artifact_token

PRIVACY: coordinates are held in local variables and printed once. Nothing here
writes them to disk, and nothing here logs them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent

DEFAULT_DASHBOARD_URL = "http://localhost:8766"

API_ROOT = "/api/orchestrator/geo"
PATH_REQUEST = f"{API_ROOT}/request"
PATH_STATUS = f"{API_ROOT}/status"
PATH_LAST = f"{API_ROOT}/last"
PATH_FORGET = f"{API_ROOT}/forget"
PATH_REVERSE = f"{API_ROOT}/reverse"
PATH_NEARBY = f"{API_ROOT}/nearby"

# Mirrors the dashboard's orchestrator_uploads.SESSION_ID_RE — checked HERE so a
# missing HD_SESSION_ID becomes an actionable answer instead of an HTTP 400.
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# Mirrors orchestrator_geo.REQUEST_ID_RE.
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

ACCURACIES = ("fine", "coarse")

# ── the timeout ladder (server side: getCurrentPosition 20 s < poll window 45 s
# < request TTL 180 s). These are the CLIENT's half and must stay under it. ──
HTTP_TIMEOUT_S = 15
WAIT_WINDOW_DEFAULT_S = 45.0
# The socket must outlive the server's own poll window, or urllib times out on a
# request that was about to answer normally.
WAIT_SOCKET_MARGIN_S = 12
OVERALL_TIMEOUT_DEFAULT_S = 180.0
MAX_AGE_DEFAULT_S = 300
RATE_LIMIT_SLEEP_S = 2.0
GEOCODE_TIMEOUT_S = 20
ARTIFACT_TIMEOUT_S = 20

# Exit codes, by MEANING rather than by status, so the shell can branch:
EXIT_OK = 0            # a fix (fresh or cached)
EXIT_TRANSPORT = 1     # the dashboard / a helper could not be reached at all
EXIT_REFUSED = 2       # the human said no (or the device is opted out)
EXIT_UNAVAILABLE = 3   # asking is pointless right now (no browser, http://, off)
EXIT_TIMEOUT = 4       # nobody answered in time / transient device failure

_EXIT_BY_STATUS: dict[str, int] = {
    "fixed": EXIT_OK,
    "cached": EXIT_OK,
    "ok": EXIT_OK,
    "denied": EXIT_REFUSED,
    "device_opt_out": EXIT_REFUSED,
    "dismissed": EXIT_REFUSED,
    "no_browser": EXIT_UNAVAILABLE,
    "insecure_context": EXIT_UNAVAILABLE,
    "no_api": EXIT_UNAVAILABLE,
    "disabled": EXIT_UNAVAILABLE,
    "geocode_disabled": EXIT_UNAVAILABLE,
    "no_session": EXIT_UNAVAILABLE,
    "no_cached_fix": EXIT_UNAVAILABLE,
    "unauthorized": EXIT_UNAVAILABLE,
    "no_artifact_cli": EXIT_UNAVAILABLE,
    "timeout": EXIT_TIMEOUT,
    "expired": EXIT_TIMEOUT,
    "aborted": EXIT_TIMEOUT,
    "unavailable": EXIT_TIMEOUT,
    "rate_limited": EXIT_TIMEOUT,
    "not_found": EXIT_TIMEOUT,
    "geocode_failed": EXIT_TIMEOUT,
    "dashboard_unreachable": EXIT_TRANSPORT,
    "bad_response": EXIT_TRANSPORT,
    "route_missing": EXIT_TRANSPORT,
    "map_failed": EXIT_TRANSPORT,
}

# Rows the SERVER cannot deliver: a bare HTTP status has no typed body, and a
# socket error has no body at all. The server's own STATUSES table stays
# authoritative for everything that does arrive over the wire — these are the
# gaps, deliberately worded the same way.
LOCAL_STATUSES: dict[str, dict[str, Any]] = {
    "dashboard_unreachable": {
        "reason": "transport_error",
        "retryable": True,
        "message": "Nie udało się połączyć z dashboardem.",
        "hint": "Sprawdź `systemctl status orbit`. "
                "Jeśli nie wstaje — poproś użytkownika o lokalizację tekstem.",
    },
    "bad_response": {
        "reason": "malformed_response",
        "retryable": True,
        "message": "Dashboard odpowiedział czymś, czego nie umiem odczytać.",
        "hint": "Spróbuj raz jeszcze; jeśli się powtarza, zgłoś to użytkownikowi.",
    },
    "unauthorized": {
        "reason": "invalid_geo_token",
        "retryable": False,
        "message": "Token geolokalizacji został odrzucony przez dashboard.",
        "hint": "Sprawdź ~/.orchestrator/artifact_token. Nie ponawiaj w tej turze.",
    },
    "disabled": {
        "reason": "feature_disabled",
        "retryable": False,
        "message": "Geolokalizacja jest wyłączona na serwerze.",
        "hint": "Włącz w Ustawienia → Serwer.",
    },
    "not_found": {
        "reason": "unknown_request",
        "retryable": False,
        "message": "Taka prośba o lokalizację nie istnieje (wygasła lub anulowana).",
        "hint": "Zacznij od nowa — utwórz świeżą prośbę.",
    },
    "route_missing": {
        "reason": "route_not_deployed",
        "retryable": False,
        "message": "Dashboard nie zna tego endpointu geolokalizacji.",
        "hint": "Usługa jest starsza niż ten skill — poproś użytkownika o "
                "`systemctl restart orbit`. Nie ponawiaj w tej turze.",
    },
    "rate_limited": {
        "reason": "too_many_requests",
        "retryable": True,
        "message": "Za dużo próśb o lokalizację naraz.",
        "hint": "Odczekaj retry_after_s sekund i spróbuj raz jeszcze.",
    },
    "aborted": {
        "reason": "request_aborted",
        "retryable": False,
        "message": "Prośba o lokalizację została anulowana.",
        "hint": "Ponów tylko jeśli użytkownik znów o to poprosi.",
    },
    "no_session": {
        "reason": "session_unknown",
        "retryable": False,
        "message": "Nie umiem ustalić id sesji orchestratora (HD_SESSION_ID).",
        "hint": "Uruchom CLI w sesji dashboardu albo podaj --session <uuid>.",
    },
    "no_cached_fix": {
        "reason": "cache_empty",
        "retryable": False,
        "message": "Serwer nie ma zapamiętanej lokalizacji.",
        "hint": "Użyj `geo where`, żeby poprosić przeglądarkę o świeży odczyt.",
    },
    "no_artifact_cli": {
        "reason": "artifact_cli_missing",
        "retryable": False,
        "message": "Nie znalazłem CLI skilla `artifacts` — nie zrobię mapy.",
        "hint": "Pomiń --map albo ustaw HD_ARTIFACT_CLI na ścieżkę artifacts_cli.py.",
    },
    "map_failed": {
        "reason": "artifact_cli_error",
        "retryable": True,
        "message": "CLI artefaktów nie zapisało mapy.",
        "hint": "Odpowiedz bez mapy — współrzędne i tak masz.",
    },
}


class GeoTransportError(Exception):
    """The dashboard could not be reached at all (socket / DNS / timeout)."""


# ---------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------


def describe(status: str) -> dict[str, Any]:
    """Copy of the :data:`LOCAL_STATUSES` row (``bad_response`` if unknown)."""
    row = LOCAL_STATUSES.get(status) or LOCAL_STATUSES["bad_response"]
    return dict(row)


def failure(status: str, **extra: Any) -> dict[str, Any]:
    """The one canonical client-side ``ok:false`` payload for ``status``."""
    return {"ok": False, "status": status, "terminal": True, **describe(status), **extra}


def exit_code(payload: dict[str, Any]) -> int:
    """Map a payload's status onto the CLI exit code table."""
    status = payload.get("status") if isinstance(payload, dict) else None
    if isinstance(status, str) and status in _EXIT_BY_STATUS:
        return _EXIT_BY_STATUS[status]
    return EXIT_OK if isinstance(payload, dict) and payload.get("ok") else EXIT_TRANSPORT


def retryable(payload: dict[str, Any]) -> bool:
    """``True`` when trying once more THIS TURN is reasonable."""
    return bool(payload.get("retryable")) if isinstance(payload, dict) else False


# ---------------------------------------------------------------------------
# Config / env discovery (idioms shared with artifacts_lib)
# ---------------------------------------------------------------------------


def _load_skill_config() -> dict[str, Any]:
    """Read <skill_dir>/config.json if present. Never raises — returns {}."""
    path = SKILL_DIR / "config.json"
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except (OSError, ValueError):
        pass
    return {}


def notify_url() -> str:
    """Dashboard base URL: HD_NOTIFY_URL → config.json → localhost:8766."""
    return (
        os.environ.get("HD_NOTIFY_URL")
        or _load_skill_config().get("dashboard_url")
        or DEFAULT_DASHBOARD_URL
    ).rstrip("/")


def read_token() -> str | None:
    """Read the shared CLI token: HD_ARTIFACT_TOKEN_FILE → ~/.orchestrator/…"""
    candidates = [
        os.environ.get("HD_ARTIFACT_TOKEN_FILE"),
        str(Path.home() / ".orchestrator" / "artifact_token"),
    ]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand).expanduser()
        try:
            if path.is_file():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    return token
        except OSError:
            continue
    return None


def resolve_session_id() -> str:
    """HD_SESSION_ID → ORCHESTRATOR_SESSION_ID → tmux session name → ""."""
    for key in ("HD_SESSION_ID", "ORCHESTRATOR_SESSION_ID"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            name = result.stdout.strip()
            if name:
                return name[3:] if name.startswith("hd-") else name
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def session_id_or_none(override: str | None = None) -> str | None:
    """The session id to send, or ``None`` when we have nothing valid.

    The route rejects anything that isn't a session uuid, so validating here
    turns an opaque HTTP 400 into the ``no_session`` row with a hint.
    """
    candidate = (override or "").strip() or resolve_session_id()
    return candidate if SESSION_ID_RE.fullmatch(candidate or "") else None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_json(
    path: str,
    *,
    method: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float,
) -> tuple[int, Any]:
    """One request to the dashboard → ``(http_status, parsed_json)``.

    Raises :class:`GeoTransportError` only when nothing came back at all; a
    non-2xx WITH a body is returned so the caller can surface the server's own
    typed payload (`geocode_disabled` arrives that way).
    """
    url = notify_url() + path
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = read_token()
    if token:
        headers["X-Geo-Token"] = token

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return exc.code, _parse_body(raw)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GeoTransportError(f"{url}: {exc}") from exc


def _parse_body(raw: bytes) -> Any:
    """Parse a response body as JSON; an empty / non-JSON body becomes ``None``."""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _detail_of(body: Any) -> str:
    """Best human message out of a server error body."""
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def call(
    path: str,
    *,
    method: str = "POST",
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_S,
) -> dict[str, Any]:
    """A dashboard call that ALWAYS returns a typed payload — never raises.

    Every HTTP-level outcome the server cannot type for itself (403 with a bare
    detail, 404, 429, a socket error, a body that isn't our envelope) is mapped
    onto a :data:`LOCAL_STATUSES` row here, so callers never branch on codes.
    """
    try:
        status, payload = _http_json(
            path, method=method, body=body, params=params, timeout=timeout,
        )
    except GeoTransportError as exc:
        return failure("dashboard_unreachable", detail=str(exc))

    # ``ok`` is the marker of OUR envelope; a FastAPI error body is {"detail":…}
    # and a geo/status body carries no ``status`` key at all, so keying on
    # ``status`` here would misread the healthy preflight as malformed.
    if isinstance(payload, dict) and "ok" in payload:
        return payload  # the server typed it for us — pass it through untouched
    detail = _detail_of(payload)
    if status == 403:
        # Two very different 403s: the kill switch vs. a rejected token.
        row = "unauthorized" if "token" in detail.lower() else "disabled"
        return failure(row, detail=detail or None)
    if status == 404:
        # Only a REQUEST-scoped path can legitimately 404 ("that ask is gone").
        # Everywhere else (/status, /last, /request, /forget, /reverse,
        # /nearby) the server answers 200 with a typed body, so a 404 there
        # means the route isn't deployed — telling the agent its location
        # request expired would be the exact opposite of the truth, and its
        # hint ("start a fresh one") would loop it straight back into another
        # 404.
        row = "not_found" if path.startswith(f"{API_ROOT}/requests/") else "route_missing"
        return failure(row, detail=detail or None, http_status=404)
    if status == 429:
        return failure("rate_limited", retry_after_s=RATE_LIMIT_SLEEP_S,
                       terminal=False, detail=detail or None)
    return failure("bad_response", http_status=status, detail=detail or None)


# ---------------------------------------------------------------------------
# Verbs — the round trip
# ---------------------------------------------------------------------------


def preflight(session_id: str | None = None) -> dict[str, Any]:
    """``GET /geo/status`` — side-effect free. Never prompts anybody."""
    sid = session_id_or_none(session_id)
    return call(PATH_STATUS, method="GET",
                params={"session_id": sid} if sid else None)


def request_fix(
    *,
    session_id: str,
    reason: str = "",
    accuracy: str = "fine",
    max_age_s: int = MAX_AGE_DEFAULT_S,
    ttl_s: float = OVERALL_TIMEOUT_DEFAULT_S,
) -> dict[str, Any]:
    """``POST /geo/request`` — cached / no_browser / rate_limited / pending."""
    return call(PATH_REQUEST, body={
        "session_id": session_id,
        "reason": reason,
        "accuracy": accuracy if accuracy in ACCURACIES else "fine",
        "max_age_s": int(max_age_s),
        "ttl_s": float(ttl_s),
    })


def wait_once(request_id: str, timeout_s: float) -> dict[str, Any]:
    """``GET …/wait`` for ONE poll window. ``terminal:false`` ⇒ poll again."""
    window = max(1.0, float(timeout_s))
    return call(
        f"{API_ROOT}/requests/{urllib.parse.quote(request_id)}/wait",
        method="GET", params={"timeout": round(window, 3)},
        timeout=window + WAIT_SOCKET_MARGIN_S,
    )


def cancel(request_id: str) -> dict[str, Any]:
    """``DELETE …`` — withdraw an ask so a stale prompt leaves the screen."""
    return call(f"{API_ROOT}/requests/{urllib.parse.quote(request_id)}",
                method="DELETE")


def forget() -> dict[str, Any]:
    """``POST /geo/forget`` — drop the server's in-memory fix + geocode caches."""
    return call(PATH_FORGET)


def resolve_fix(
    *,
    session_id: str,
    reason: str = "",
    accuracy: str = "fine",
    max_age_s: int = MAX_AGE_DEFAULT_S,
    overall_timeout_s: float = OVERALL_TIMEOUT_DEFAULT_S,
    poll_window_s: float = WAIT_WINDOW_DEFAULT_S,
) -> dict[str, Any]:
    """The whole round trip, collapsed to ONE terminal payload.

    Absorbs everything the agent should not have to think about: the
    non-terminal poll-window timeout (re-poll the same id), a 429 (sleep, then
    re-poll), and Ctrl-C (withdraw the ask so no modal is left dangling). The
    ask is cancelled on the way out of the overall budget too — otherwise an
    abandoned prompt sits on the user's phone for the rest of the TTL.
    """
    budget = max(1.0, float(overall_timeout_s))
    deadline = time.monotonic() + budget
    created = request_fix(session_id=session_id, reason=reason, accuracy=accuracy,
                          max_age_s=max_age_s, ttl_s=budget)
    if created.get("status") != "pending":
        return created  # cached / no_browser / rate_limited / disabled / transport

    request_id = created.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return failure("bad_response", detail="server returned no usable request_id")

    try:
        return _poll_until_terminal(request_id, deadline, poll_window_s)
    except KeyboardInterrupt:
        cancel(request_id)
        return failure("aborted", request_id=request_id,
                       detail="przerwane z klawiatury (SIGINT)")


def _poll_until_terminal(
    request_id: str, deadline: float, poll_window_s: float,
) -> dict[str, Any]:
    """Re-poll ``request_id`` until something terminal arrives or time is up."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancel(request_id)
            return failure("aborted", request_id=request_id,
                           detail="minął budżet czasu CLI (--timeout)")
        answer = wait_once(request_id, min(poll_window_s, remaining))
        if answer.get("terminal") is not False:
            return answer
        # Non-terminal: the poll window elapsed, or we were throttled.
        if answer.get("status") == "rate_limited":
            pause = answer.get("retry_after_s")
            pause = pause if isinstance(pause, (int, float)) else RATE_LIMIT_SLEEP_S
            time.sleep(max(0.0, min(float(pause), deadline - time.monotonic())))


def last_known() -> dict[str, Any]:
    """``GET /geo/last`` — the server's cached fix, read-only. NEVER prompts.

    This used to emulate a read by creating an ask with a ``max_age_s`` wide
    enough to guarantee a cache hit, and withdrawing it if one slipped through.
    That was not side-effect-free: ``create_request`` publishes to the browser
    BEFORE the CLI can withdraw anything, so any gap in the cache (a `forget`, a
    restart, another agent) turned this verb into a real permission prompt plus
    a "an agent took your location" toast. A dedicated read-only route has no
    such window.
    """
    payload = call(PATH_LAST, method="GET")
    if not payload.get("ok"):
        return payload
    if not isinstance(payload.get("fix"), dict):
        return failure("no_cached_fix")
    return payload


# ---------------------------------------------------------------------------
# Verbs — geocoding (both degrade to a body, never an exception)
# ---------------------------------------------------------------------------


def reverse(lat: float, lon: float) -> dict[str, Any]:
    """``POST /geo/reverse`` — coordinates → a street address (OSM egress!)."""
    return call(PATH_REVERSE, body={"lat": lat, "lon": lon},
                timeout=GEOCODE_TIMEOUT_S)


def nearby(
    lat: float, lon: float, query: str, radius_m: int, limit: int,
) -> dict[str, Any]:
    """``POST /geo/nearby`` — POIs around a fix, nearest first (OSM egress!)."""
    return call(PATH_NEARBY, body={
        "lat": lat, "lon": lon, "query": query,
        "radius_m": int(radius_m), "limit": int(limit),
    }, timeout=GEOCODE_TIMEOUT_S)


def format_address(address: dict[str, Any]) -> str:
    """A one-line human address: street + number, postcode + city.

    Falls back to Nominatim's own ``display_name`` when the structured parts are
    missing (rural addresses often have neither road nor house number).
    """
    if not isinstance(address, dict):
        return ""
    street = " ".join(p for p in (address.get("road"), address.get("house_number")) if p)
    town = " ".join(p for p in (address.get("postcode"), address.get("city")) if p)
    line = ", ".join(p for p in (street, town) if p)
    display = address.get("display_name")
    return line or (display if isinstance(display, str) else "")


# ---------------------------------------------------------------------------
# Map artifact (shells the `artifacts` skill — no shared code, just its CLI)
# ---------------------------------------------------------------------------


def artifact_cli_path() -> Path | None:
    """Locate ``artifacts_cli.py``: $HD_ARTIFACT_CLI → registry → repo sibling."""
    candidates = [
        os.environ.get("HD_ARTIFACT_CLI"),
        str(Path.home() / ".orchestrator" / "skills-registry" / "artifacts"
            / "scripts" / "artifacts_cli.py"),
        str(SKILL_DIR.parent / "artifacts" / "scripts" / "artifacts_cli.py"),
    ]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand).expanduser()
        if path.is_file():
            return path
    return None


def map_spec(
    fix: dict[str, Any], places: list[dict[str, Any]] | None = None, *, zoom: int = 15,
) -> dict[str, Any]:
    """Build a Leaflet spec: the fix as marker #1, then each POI. Pure."""
    lat, lon = fix.get("lat"), fix.get("lon")
    accuracy = fix.get("accuracy_m")
    here = f"Tutaj (±{round(float(accuracy))} m)" if isinstance(accuracy, (int, float)) else "Tutaj"
    markers = [{"lat": lat, "lng": lon, "label": here}]
    for place in places or []:
        name = place.get("name") or place.get("kind") or "POI"
        distance = place.get("distance_m")
        label = f"{name} · {distance} m" if isinstance(distance, (int, float)) else str(name)
        markers.append({"lat": place.get("lat"), "lng": place.get("lon"), "label": label})
    return {"center": [lat, lon], "zoom": zoom, "markers": markers, "route": []}


def create_map(title: str, spec: dict[str, Any], *, open_after: bool = False) -> dict[str, Any]:
    """Create a `map` artifact by shelling the artifacts CLI. Typed failures."""
    cli = artifact_cli_path()
    if cli is None:
        return failure("no_artifact_cli")
    cmd = [sys.executable, str(cli), "create", "--type", "map",
           "--title", title, "--stdin"]
    if open_after:
        cmd.append("--open")
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(spec, ensure_ascii=False),
            capture_output=True, text=True, timeout=ARTIFACT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return failure("map_failed", detail=str(exc)[:200])
    if proc.returncode != 0:
        return failure("map_failed", detail=(proc.stderr or proc.stdout).strip()[:200])
    match = re.search(r"id=(art-[0-9A-Za-z-]+)", proc.stdout or "")
    return {"ok": True, "status": "ok", "terminal": True,
            "artifact_id": match.group(1) if match else None}
