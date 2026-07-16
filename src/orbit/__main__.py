"""CLI entry: `python -m orbit`.

Default invocation boots the uvicorn server. Subcommand
``system-check`` runs the watchdog and pushes one notification per
non-info event (or just prints them when ``--dry-run`` is set).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

import uvicorn


_logger = logging.getLogger(__name__)


def _run_server(args: argparse.Namespace) -> None:
    # App-module INFO logs (e.g. orchestrator_read_aloud's "[read-aloud] …"
    # breadcrumbs) were INVISIBLE in production: the serve path had no logging
    # config, so module `logger.info()` propagated to a WARNING-level root and
    # vanished — only uvicorn's own access logger showed. That left the
    # eyes-free voice/read-aloud audio path with ZERO server observability
    # (diagnosed 2026-06-13 from a failed in-car attempt). Attach a dedicated
    # INFO handler to the PACKAGE logger so every orbit.* module's
    # INFO reaches stderr → journald. propagate=False avoids double lines; the
    # package logger is untouched by uvicorn's dictConfig (it only configures
    # uvicorn.*), so this survives uvicorn.run().
    pkg_logger = logging.getLogger("orbit")
    if not pkg_logger.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        pkg_logger.addHandler(_h)
        pkg_logger.setLevel(logging.INFO)
        pkg_logger.propagate = False
    uvicorn.run(
        "orbit.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
        root_path=args.root_path,
        proxy_headers=True,
        forwarded_allow_ips="*",
        # Bound shutdown so `systemctl restart` (incl. agent self-restart) is
        # fast and clean. Without this, uvicorn waits FOREVER for the panel's
        # long-lived connections (terminal WebSocket, SSE /events + /stream,
        # read-aloud) to close — they never do, so systemd hits its 90s
        # TimeoutStopSec and SIGKILLs (journal: "stop-sigterm timed out").
        # 10s gives lifespan cleanup headroom, then uvicorn force-closes.
        timeout_graceful_shutdown=10,
    )


def _run_system_check(args: argparse.Namespace) -> int:
    from . import notify as notify_mod
    from . import system_watchdog

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    events = system_watchdog.check_all()

    if args.json:
        sys.stdout.write(json.dumps(events, indent=2, ensure_ascii=False) + "\n")
    else:
        if not events:
            sys.stdout.write("system-check: no new events\n")
        for evt in events:
            sys.stdout.write(
                f"[{evt.get('severity','?'):>8}] {evt.get('type','?'):<12} "
                f"{evt.get('message','')}\n"
            )

    if args.dry_run:
        return 0

    notifiable = [e for e in events if e.get("severity") in ("warning", "critical")]
    if not notifiable:
        return 0

    async def _send_all() -> None:
        for evt in notifiable:
            severity = evt.get("severity", "info")
            priority = 5 if severity == "critical" else 4
            tag = "rotating_light" if severity == "critical" else "warning"
            await notify_mod.notify(
                topic=os.getenv("NOTIFY_SYSTEM_TOPIC", "system"),
                message=str(evt.get("message", "")),
                title=f"system: {evt.get('type','?')}",
                priority=priority,
                tags=[tag, str(evt.get("type", "system"))],
            )

    try:
        asyncio.run(_send_all())
    except Exception as exc:
        _logger.warning("system-check: notify dispatch failed: %s", exc)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="orbit")
    subparsers = parser.add_subparsers(dest="command")

    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8766")))
    parser.add_argument("--reload", action="store_true", help="dev mode")
    parser.add_argument(
        "--root-path", default=os.getenv("BASE_PATH", ""),
        help="prefix when behind a reverse proxy (default: $BASE_PATH or empty)",
    )

    sc = subparsers.add_parser(
        "system-check",
        help="Run system_watchdog.check_all() and push notify events.",
    )
    sc.add_argument("--dry-run", action="store_true",
                    help="Print events but do not call notify().")
    sc.add_argument("--json", action="store_true",
                    help="Emit events as JSON instead of human-readable lines.")

    subparsers.add_parser(
        "tasks-reminders-tick",
        help="Single sweep of the tasks reminder loop (invoked by the cron job).",
    )

    subparsers.add_parser(
        "a2a-gc-tick",
        help="Single sweep of the A2A maildir GC (invoked by the cron job).",
    )

    args = parser.parse_args()

    if args.command == "system-check":
        sys.exit(_run_system_check(args))

    if args.command == "tasks-reminders-tick":
        from . import tasks_reminders
        sys.exit(tasks_reminders.run_tick_cli())

    if args.command == "a2a-gc-tick":
        from . import orchestrator_a2a_gc
        sys.exit(orchestrator_a2a_gc.run_tick_cli())

    _run_server(args)


if __name__ == "__main__":
    main()
