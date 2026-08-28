"""Scrubbing for the UNTRUSTED half of the geolocation round trip.

The answer route (``POST …/geo/requests/{id}/answer``) is deliberately
un-authenticated — the ``request_id`` is the capability — so everything that
arrives on it is attacker-shaped by assumption. This module is the only place
that turns such a payload into numbers the rest of the feature trusts, split
out of :mod:`orchestrator_geo` so the validation reads (and tests) on its own.

The load-bearing rule is REBUILD, NEVER MERGE: :func:`rebuild_coords` copies a
fixed allowlist of keys, one at a time, each finite- and range-checked. A
browser cannot inject an extra field into a fix, cannot smuggle ``NaN`` past a
comparison (every ``NaN`` compare is False, so a naive ``lat > 90`` guard lets
it through — hence the explicit :func:`math.isfinite`), and cannot pass a
``bool`` where a number belongs (``True`` is an ``int`` in Python).

Rounding is a privacy control, not cosmetics: ``coarse`` cuts the fix to 2
decimal places (~1 km) AND floors the reported accuracy to match, so a coarse
answer cannot be mistaken for a precise one downstream.

Pure and import-light: no I/O, no FastAPI, nothing logged — a latitude must
never reach a log line.
"""
from __future__ import annotations

import math
import time
from typing import Any

# Cap on the agent-supplied prompt text shown to the human.
REASON_MAX_CHARS: int = 200
ACCURACIES: tuple[str, ...] = ("fine", "coarse")
# 5 dp ≈ 1 m, 2 dp ≈ 1 km. The coarse floor keeps ``accuracy_m`` honest about
# the rounding we just applied.
PRECISION_DP: dict[str, int] = {"fine": 5, "coarse": 2}
COARSE_ACCURACY_FLOOR_M: float = 1000.0
# Precision is a ONE-WAY street (see :func:`degrade_fix`): higher rank = more
# precise, and a stored fix may only ever be handed out at its own rank or
# below.
_PRECISION_RANK: dict[str, int] = {"coarse": 0, "fine": 1}

# The ONLY keys ever read out of a browser-supplied coords object.
COORD_KEYS: tuple[str, ...] = (
    "lat", "lon", "accuracy_m", "altitude_m", "heading", "speed_mps",
)


def now_ms() -> int:
    """Wall-clock milliseconds — the unit the browser speaks."""
    return int(time.time() * 1000)


def sanitize_reason(raw: Any) -> str:
    """Agent-supplied prompt text → a safe, capped one-liner.

    The frontend renders it as a React TEXT child (never as HTML), but control
    characters would still corrupt the SSE frame, so they are stripped here.
    """
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw if ch == " " or ch.isprintable())
    return cleaned.strip()[:REASON_MAX_CHARS]


def normalize_accuracy(raw: Any) -> str:
    """Anything unrecognised degrades to ``fine`` — never to an unknown mode."""
    return raw if isinstance(raw, str) and raw in ACCURACIES else "fine"


def clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    """Bool-rejecting int coercion clamped into ``[lo, hi]``."""
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))


def finite(raw: Any) -> float | None:
    """``float(raw)`` iff it is a real, finite number — else ``None``.

    Rejects ``bool`` and ``str`` outright: ``float("52.2")`` succeeding would
    let a string coordinate through, and ``float(True)`` is ``1.0``.
    """
    if isinstance(raw, bool) or isinstance(raw, str) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def optional_coord(raw: Any, lo: float, hi: float) -> float | None:
    """An optional in-range float; anything else becomes ``None``, not an error.

    Altitude/heading/speed are extras — a phone that reports garbage for them
    must not cost the user a fix that is otherwise perfectly good.
    """
    value = finite(raw)
    if value is None or not (lo <= value <= hi):
        return None
    return value


def rebuild_coords(raw: Any, precision: str) -> dict[str, Any]:
    """Rebuild a fix from an UNTRUSTED payload, key by key.

    Never merges the caller's dict: only :data:`COORD_KEYS` are read, each is
    finite- and range-checked, and the result is rounded to the precision the
    agent asked for. Raises :class:`ValueError` (→ HTTP 400) on anything that
    isn't a usable fix — an unusable answer must fail loudly rather than
    silently become a fix at (0, 0) off the coast of Africa.
    """
    if not isinstance(raw, dict):
        raise ValueError("coords must be an object")
    lat = finite(raw.get("lat"))
    lon = finite(raw.get("lon"))
    if lat is None or not (-90.0 <= lat <= 90.0):
        raise ValueError("coords.lat must be a finite number in [-90, 90]")
    if lon is None or not (-180.0 <= lon <= 180.0):
        raise ValueError("coords.lon must be a finite number in [-180, 180]")
    accuracy_m = finite(raw.get("accuracy_m"))
    if accuracy_m is None or accuracy_m <= 0:
        raise ValueError("coords.accuracy_m must be a positive finite number")

    dp = PRECISION_DP.get(precision, PRECISION_DP["fine"])
    if precision == "coarse":
        accuracy_m = max(accuracy_m, COARSE_ACCURACY_FLOOR_M)
    return {
        "lat": round(lat, dp),
        "lon": round(lon, dp),
        "accuracy_m": round(accuracy_m, 1),
        # Ranges are physical sanity bounds, not GPS spec: below the Dead Sea
        # / above the Kármán line, a bearing outside a circle, or a speed past
        # escape velocity is a broken sensor, not a location.
        "altitude_m": optional_coord(raw.get("altitude_m"), -12000.0, 100000.0),
        "heading": optional_coord(raw.get("heading"), 0.0, 360.0),
        "speed_mps": optional_coord(raw.get("speed_mps"), 0.0, 10000.0),
        "precision": precision,
    }


def degrade_fix(fix: Any, precision: str) -> dict[str, Any] | None:
    """Re-serve an ALREADY-STORED fix at ``precision`` — or refuse.

    The cache is one global entry but callers ask with different accuracies, so
    something has to reconcile them, and the reconciliation is deliberately
    asymmetric: a ``fine`` fix is re-rounded down to answer a ``coarse`` ask
    (the caller asked for less, they get less — that is the privacy control
    doing its job), while a ``coarse`` fix answering a ``fine`` ask returns
    ``None`` so the caller re-measures instead of receiving ~1 km of error
    labelled as metres. Precision can only ever be LOST here, never invented.

    ``ts_ms`` is carried over untouched: down-rounding does not make a reading
    younger.
    """
    if not isinstance(fix, dict):
        return None
    want = normalize_accuracy(precision)
    have = normalize_accuracy(fix.get("precision"))
    if _PRECISION_RANK[have] < _PRECISION_RANK[want]:
        return None
    if have == want:
        return dict(fix)
    try:
        degraded = rebuild_coords(fix, want)
    except ValueError:
        return None   # a cache entry we can't re-round is not one we can serve
    stamp = fix.get("ts_ms")
    if isinstance(stamp, int) and not isinstance(stamp, bool):
        degraded["ts_ms"] = stamp
    return degraded
