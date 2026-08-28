"""The agent-facing contract table for device geolocation.

Split out of :mod:`orchestrator_geo` for one reason only: that module owns the
round-trip LOGIC and this file owns the WORDS, and keeping both in one file put
it past the house 800-line ceiling. Nothing here has behaviour — it is a table
plus three accessors — so it is imported and re-exported by
:mod:`orchestrator_geo`, which stays the single import site for the whole
contract.

:data:`STATUSES` is the SINGLE source of truth: routes, the CLI, SKILL.md and
the browser all read this and nothing else. Every row carries ``reason`` /
``retryable`` / a Polish ``message`` / a Polish ``hint``. ``retryable: False``
means "do not call geo again this turn" — that one field is what stops a model
blind-retrying a permission denial. ``static/geo-consent.js`` keeps a VERBATIM
copy for the browser half and a drift test pins the two together from both
sides; fix the row here and re-copy, never the other way round.
"""
from __future__ import annotations

from typing import Any

STATUSES: dict[str, dict[str, Any]] = {
    # — success —
    "fixed": {
        "reason": "browser_fix",
        "retryable": False,
        "message": "Pobrano lokalizację z przeglądarki użytkownika.",
        "hint": "",
    },
    "cached": {
        "reason": "cached_fix",
        "retryable": False,
        "message": "Zwrócono ostatnio znaną lokalizację z pamięci serwera.",
        "hint": "Podaj wiek odczytu (age_s). --max-age 0 wymusza świeży pomiar.",
    },
    # — in flight (never terminal) —
    "pending": {
        "reason": "awaiting_browser",
        "retryable": True,
        "message": "Prośba trafiła do przeglądarki — czekam na odpowiedź.",
        "hint": "Odpytuj /wait tym samym request_id aż do odpowiedzi terminalnej.",
    },
    # — refusals (never retry in the same turn) —
    "denied": {
        "reason": "permission_denied",
        "retryable": False,
        "message": "Użytkownik odrzucił prośbę o lokalizację.",
        "hint": "Nie pytaj ponownie w tej turze. Poproś o lokalizację tekstem.",
    },
    "device_opt_out": {
        "reason": "device_opt_out",
        "retryable": False,
        "message": "Na tym urządzeniu geolokalizacja jest wyłączona w ustawieniach.",
        "hint": "Nie ponawiaj. Powiedz, że można to włączyć w Ustawienia → Serwer.",
    },
    "dismissed": {
        "reason": "user_dismissed",
        "retryable": False,
        "message": "Użytkownik zamknął prośbę bez decyzji.",
        "hint": "Nie ponawiaj automatycznie — zapytaj w czacie, czy podać lokalizację.",
    },
    "insecure_context": {
        "reason": "insecure_context",
        "retryable": False,
        "message": "Przeglądarka działa po http:// — API geolokalizacji jest zablokowane.",
        "hint": "Poproś użytkownika, żeby otworzył https://your-dashboard.example/.",
    },
    "no_api": {
        "reason": "no_geolocation_api",
        "retryable": False,
        "message": "Przeglądarka nie udostępnia API geolokalizacji.",
        "hint": "Nie ponawiaj — poproś o lokalizację tekstem.",
    },
    # — transient device/environment failures (ONE more try is reasonable) —
    "unavailable": {
        "reason": "position_unavailable",
        "retryable": True,
        "message": "Urządzenie nie ustaliło pozycji (brak GPS/WiFi).",
        "hint": "Spróbuj RAZ z --accuracy coarse.",
    },
    "timeout": {
        "reason": "browser_timeout",
        "retryable": True,
        "message": "Przeglądarka nie zdążyła ustalić pozycji.",
        "hint": "Jedna próba z --accuracy coarse --timeout 30.",
    },
    # — server-side outcomes —
    "expired": {
        "reason": "request_expired",
        "retryable": False,
        "message": "Nikt nie odpowiedział na prośbę o lokalizację.",
        "hint": "Użytkownik nie zareagował — zapytaj o lokalizację w czacie.",
    },
    "aborted": {
        "reason": "request_aborted",
        "retryable": False,
        "message": "Prośba o lokalizację została anulowana.",
        "hint": "Ponów tylko jeśli użytkownik znów o to poprosi.",
    },
    "no_browser": {
        "reason": "no_browser_attached",
        "retryable": False,
        "message": "Żadna przeglądarka nie jest podłączona do dashboardu.",
        "hint": (
            "Otwórz https://your-dashboard.example/ i poproś ponownie, "
            "albo poproś użytkownika o lokalizację tekstem."
        ),
    },
    "not_found": {
        "reason": "unknown_request",
        "retryable": False,
        "message": "Taka prośba o lokalizację nie istnieje (wygasła lub anulowana).",
        "hint": "Zacznij od nowa — utwórz świeżą prośbę.",
    },
    "rate_limited": {
        "reason": "too_many_requests",
        "retryable": True,
        "message": "Za dużo próśb o lokalizację naraz.",
        "hint": "Odczekaj retry_after_s sekund i spróbuj raz jeszcze.",
    },
    "disabled": {
        "reason": "feature_disabled",
        "retryable": False,
        "message": "Geolokalizacja jest wyłączona na serwerze.",
        "hint": "Włącz w Ustawienia → Serwer.",
    },
    # — geocoding (orchestrator_geocode reads these rows; same table on purpose) —
    "geocode_disabled": {
        "reason": "geocode_disabled",
        "retryable": False,
        "message": "Reverse-geocoding jest wyłączony na serwerze.",
        "hint": "Podaj surowe współrzędne albo włącz w Ustawienia → Serwer.",
    },
    "geocode_failed": {
        "reason": "upstream_error",
        "retryable": True,
        "message": "Serwis OSM nie odpowiedział poprawnie.",
        "hint": "Spróbuj raz później albo podaj surowe współrzędne.",
    },
}

# Statuses a BROWSER may answer with. ``device_opt_out`` / ``no_api`` are also
# accepted spelled as a ``reason`` on ``denied`` / ``unavailable`` (the shape
# the consent module finds natural) and normalized to the dedicated status.
ANSWER_STATUSES: frozenset[str] = frozenset({
    "fixed", "denied", "device_opt_out", "dismissed",
    "unavailable", "no_api", "insecure_context", "timeout",
})

# Browser ``GeolocationPositionError.code`` → our status. Duplicated in
# ``static/geo-consent.js`` and drift-guarded from both sides.
ERROR_CODE_STATUS: dict[int, str] = {1: "denied", 2: "unavailable", 3: "timeout"}


def describe(status: str) -> dict[str, Any]:
    """Copy of the :data:`STATUSES` row (or the ``not_found`` row if unknown)."""
    row = STATUSES.get(status) or STATUSES["not_found"]
    return dict(row)


def failure(status: str, **extra: Any) -> dict[str, Any]:
    """Build the one canonical ``ok:false`` payload for ``status``."""
    return {"ok": False, "status": status, "terminal": True, **describe(status), **extra}


def retryable(status: str) -> bool:
    """``True`` when an agent may reasonably try once more this turn."""
    return bool(describe(status)["retryable"])
