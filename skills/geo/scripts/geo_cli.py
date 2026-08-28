#!/usr/bin/env python3
"""
geo_cli.py — argparse CLI for the `geo` skill.

A thin wrapper over geo_lib. Claude runs this inside its tmux shell to ask the
USER'S BROWSER where the user is (``navigator.geolocation``), then optionally
turn that into a street address or a list of nearby places.

Usage:
    python3 ~/.orchestrator/skills-registry/geo/scripts/geo_cli.py <command> [opts]

Commands:
    status  [--session <uuid>] [--json]
    where   [--reason <s>] [--accuracy fine|coarse] [--max-age <s>]
            [--timeout <s>] [--no-address] [--map] [--open] [--json]
    nearby  <query> [--radius <m>] [--limit <n>] [--max-age <s>]
            [--timeout <s>] [--accuracy fine|coarse] [--map] [--open] [--json]
    last    [--json]
    forget  [--json]

EVERY command prints one parseable echo line and exits with a MEANINGFUL code:
0 fix (fresh or cached) · 1 transport · 2 the human refused · 3 asking is
pointless right now · 4 nobody answered in time. A failure is a typed line plus
its Polish hint — never a traceback, never a hang.

Env discovery (see geo_lib): HD_SESSION_ID / ORCHESTRATOR_SESSION_ID / tmux for
the session id; HD_NOTIFY_URL / config.json for the dashboard URL;
HD_ARTIFACT_TOKEN_FILE / ~/.orchestrator/artifact_token for the auth token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make the importable lib resolvable whether run as a script or module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import geo_lib as lib  # noqa: E402


def _yn(value: Any) -> str:
    return "yes" if value else "no"


def _num(value: Any, suffix: str = "") -> str:
    """Render a number for the echo line; ``None`` becomes ``?``."""
    return f"{value}{suffix}" if isinstance(value, (int, float)) else "?"


def _dump(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _print_failure(payload: dict[str, Any], *, label: str = "no-fix") -> None:
    """One machine-readable refusal line, then the hint the agent must obey."""
    print(f"{label} · status={payload.get('status', '?')} "
          f"retryable={_yn(payload.get('retryable'))}")
    message = payload.get("message")
    if isinstance(message, str) and message:
        print(message)
    hint = payload.get("hint")
    if isinstance(hint, str) and hint:
        print(f"hint: {hint}")
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        print(f"detail: {detail}")


def _print_fix(payload: dict[str, Any]) -> None:
    fix = payload.get("fix") or {}
    print(
        f"fix · status={payload.get('status', '?')} "
        f"lat={_num(fix.get('lat'))} lon={_num(fix.get('lon'))} "
        f"acc={_num(fix.get('accuracy_m'), 'm')} "
        f"age={_num(payload.get('age_s'), 's')} "
        f"src={payload.get('source', '?')} prec={fix.get('precision', '?')}"
    )


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def _require_session(args: argparse.Namespace) -> str | None:
    return lib.session_id_or_none(getattr(args, "session", None))


def _fetch_fix(args: argparse.Namespace, reason: str) -> dict[str, Any]:
    """Resolve a fix (cache-first) or return the typed refusal to print."""
    session_id = _require_session(args)
    if session_id is None:
        return lib.failure("no_session")
    return lib.resolve_fix(
        session_id=session_id,
        reason=reason,
        accuracy=args.accuracy,
        max_age_s=args.max_age,
        overall_timeout_s=args.timeout,
    )


def _maybe_address(fix: dict[str, Any], wanted: bool) -> dict[str, Any] | None:
    """Reverse-geocode the fix, or ``None`` when skipped / unavailable.

    A disabled geocoder (or a sulking OSM) is NOT an error here: the caller
    still has coordinates, so the address is simply omitted.
    """
    if not wanted:
        return None
    result = lib.reverse(fix.get("lat"), fix.get("lon"))
    address = result.get("address") if result.get("ok") else None
    return address if isinstance(address, dict) else None


def _maybe_map(args: argparse.Namespace, title: str, fix: dict[str, Any],
               places: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Create the Leaflet artifact when ``--map`` was passed."""
    if not getattr(args, "map", False):
        return None
    return lib.create_map(title, lib.map_spec(fix, places), open_after=args.open)


def _print_map(result: dict[str, Any] | None) -> None:
    """Echo the artifact id — and say plainly that it is ON DISK.

    Everything else in this skill is memory-only, so an agent that read SKILL.md
    could honestly tell the user "nothing was written down". A map artifact
    breaks that: its manifest embeds the exact lat/lon, it has no TTL, `geo
    forget` does not touch it, and in a repo that doesn't ignore `.artifacts/`
    a later `git add -A` commits the user's coordinates.
    """
    if result is None:
        return
    if not result.get("ok"):
        _print_failure(result, label="map-failed")
        return
    print(f"map · id={result.get('artifact_id') or '?'}")
    print("map · UWAGA: manifest .artifacts/<id>.json ZAWIERA współrzędne i "
          "zostaje na dysku — `geo forget` go nie usuwa")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    """Side-effect-free preflight — never prompts, never costs the user a tap."""
    payload = lib.preflight(args.session)
    if args.json_out:
        _dump(payload)
        return lib.exit_code(payload)
    if not payload.get("ok"):
        _print_failure(payload, label="geo-off")
        return lib.exit_code(payload)
    print(
        f"geo · enabled={_yn(payload.get('enabled'))} "
        f"geocode={_yn(payload.get('geocode_enabled'))} "
        f"browser={_yn(payload.get('browser_attached'))} "
        f"browsers={_num(payload.get('browsers_n'))} "
        f"last_fix_age={_num(payload.get('last_fix_age_s'), 's')} "
        f"pending={_num(payload.get('pending_n'))} "
        f"waits={_num(payload.get('wait_inflight'))}"
    )
    return lib.EXIT_OK


def _cmd_where(args: argparse.Namespace) -> int:
    """The round trip: ask the browser, then (by default) name the street."""
    payload = _fetch_fix(args, args.reason)
    fix = payload.get("fix") if payload.get("ok") else None
    if not isinstance(fix, dict):
        if args.json_out:
            _dump(payload)
        else:
            _print_failure(payload)
        return lib.exit_code(payload)

    address = _maybe_address(fix, args.address)
    map_result = _maybe_map(args, "Twoja lokalizacja", fix)
    if args.json_out:
        _dump({**payload, "address": address,
               "artifact_id": (map_result or {}).get("artifact_id")})
        return lib.EXIT_OK
    _print_fix(payload)
    if address:
        line = lib.format_address(address)
        if line:
            print(f"addr · {line}")
    _print_map(map_result)
    return lib.EXIT_OK


def _cmd_nearby(args: argparse.Namespace) -> int:
    """Resolve a fix (cache-first), then ask OSM what is around it."""
    payload = _fetch_fix(args, f"co jest najbliżej: {args.query}")
    fix = payload.get("fix") if payload.get("ok") else None
    if not isinstance(fix, dict):
        if args.json_out:
            _dump(payload)
        else:
            _print_failure(payload)
        return lib.exit_code(payload)

    result = lib.nearby(fix.get("lat"), fix.get("lon"), args.query,
                        args.radius, args.limit)
    if not result.get("ok"):
        if args.json_out:
            _dump(result)
        else:
            _print_failure(result, label="no-places")
        return lib.exit_code(result)

    places = [p for p in result.get("places", []) if isinstance(p, dict)][:args.limit]
    map_result = _maybe_map(args, f"Najbliżej: {args.query}", fix, places)
    if args.json_out:
        _dump({**result, "places": places, "fix": fix,
               "artifact_id": (map_result or {}).get("artifact_id")})
        return lib.EXIT_OK
    _print_nearby(result, places)
    _print_map(map_result)
    return lib.EXIT_OK


def _print_nearby(result: dict[str, Any], places: list[dict[str, Any]]) -> None:
    print(
        f"nearby · query={result.get('query', '?')} found={len(places)} "
        f"source={result.get('source', '?')} "
        f"radius={_num(result.get('radius_m'), 'm')} "
        f"cached={_yn(result.get('cached'))}"
    )
    if not places:
        print("Nic nie znalazłem w tym promieniu — zwiększ --radius "
              "albo spytaj o inną kategorię.")
        return
    for index, place in enumerate(places, start=1):
        name = place.get("name") or "(bez nazwy)"
        print(f"  {index}. {name} · {_num(place.get('distance_m'), ' m')} · "
              f"{place.get('kind', '?')} · {_num(place.get('lat'))},{_num(place.get('lon'))}")


def _cmd_last(args: argparse.Namespace) -> int:
    """The server's cached fix — reads only, never prompts the user."""
    payload = lib.last_known()
    if args.json_out:
        _dump(payload)
        return lib.exit_code(payload)
    if not isinstance(payload.get("fix"), dict):
        _print_failure(payload)
        return lib.exit_code(payload)
    _print_fix(payload)
    return lib.EXIT_OK


def _cmd_forget(args: argparse.Namespace) -> int:
    """Drop the server's in-memory fix + the geocode caches."""
    payload = lib.forget()
    if args.json_out:
        _dump(payload)
        return lib.exit_code(payload)
    if not payload.get("ok"):
        _print_failure(payload, label="forget-failed")
        return lib.exit_code(payload)
    print(f"forgotten · had={_yn(payload.get('forgotten'))}")
    return lib.EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", help="Session uuid (default: HD_SESSION_ID)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Dump the full typed payload as JSON")


def _add_fix_opts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accuracy", choices=list(lib.ACCURACIES), default="fine",
                        help="coarse = ~1 km rounding (privacy / faster fix)")
    parser.add_argument("--max-age", dest="max_age", type=int,
                        default=lib.MAX_AGE_DEFAULT_S,
                        help="Accept a cached fix up to N s old (0 = force fresh)")
    parser.add_argument("--timeout", type=float,
                        default=lib.OVERALL_TIMEOUT_DEFAULT_S,
                        help="Overall budget in seconds before giving up")
    parser.add_argument("--map", action="store_true",
                        help="Also create a Leaflet map artifact")
    parser.add_argument("--open", action="store_true",
                        help="With --map: pop the artifact modal immediately")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geo",
        description="Ask the user's BROWSER where the user is (not the server).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Preflight: is anyone home? (no prompt)")
    _add_common(p_status)
    p_status.set_defaults(func=_cmd_status)

    p_where = sub.add_parser("where", help="Get the user's location (+ address)")
    _add_common(p_where)
    _add_fix_opts(p_where)
    p_where.add_argument("--reason", default="",
                         help="Short PL text shown to the user (≤200 chars)")
    p_where.add_argument("--address", dest="address", action="store_true",
                         default=True, help="Reverse-geocode the fix (default)")
    p_where.add_argument("--no-address", dest="address", action="store_false",
                         help="Raw lat/lon only — no OSM egress")
    p_where.set_defaults(func=_cmd_where)

    p_nearby = sub.add_parser("nearby", help="POIs around the user, nearest first")
    _add_common(p_nearby)
    _add_fix_opts(p_nearby)
    p_nearby.add_argument("query", help='What to look for, e.g. "apteka"')
    p_nearby.add_argument("--radius", type=int, default=1500,
                          help="Search radius in metres (50–10000)")
    p_nearby.add_argument("--limit", type=int, default=8,
                          help="Max places to return (1–25)")
    p_nearby.set_defaults(func=_cmd_nearby)

    # No --session here: this verb reads a PROCESS-GLOBAL cache over a
    # read-only route, so there is no session to scope it to (and accepting a
    # flag that changes nothing would only mislead).
    p_last = sub.add_parser("last", help="The last known fix, without prompting")
    p_last.add_argument("--json", dest="json_out", action="store_true",
                        help="Dump the full typed payload as JSON")
    p_last.set_defaults(func=_cmd_last)

    p_forget = sub.add_parser("forget", help="Forget the cached fix + geocodes")
    _add_common(p_forget)
    p_forget.set_defaults(func=_cmd_forget)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        # resolve_fix withdraws its own ask; anything else has nothing pending.
        print("aborted · status=aborted retryable=no", file=sys.stderr)
        sys.exit(lib.EXIT_TIMEOUT)
    except BrokenPipeError:
        # Downstream closed the pipe (e.g. `| head`); exit quietly.
        try:
            sys.stdout.close()
        except OSError:
            pass
        os._exit(0)
    sys.exit(rc)


if __name__ == "__main__":
    main()
