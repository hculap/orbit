"""Create the Showroom widgetów demo session — exercises all 7 widget kinds.

Hybrid approach (per plan): pre-stage assets in the new session's uploads dir,
then drive 7 separate model turns (one per widget) through ClaudeRunner. The
real envelope pipeline writes the JSONL — no synthetic injection.

Usage:
    uv run python -m orbit.scripts.create_demo_session [--replace]

Idempotent: skip if a session with title "Showroom widgetów" + title_manual=True
already exists. With --replace, deletes the existing one first. With --force,
adds another demo session even if one exists.

Re-run after any block-schema or system-prompt change to regenerate the
showroom against the current envelope shape.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import uuid
from pathlib import Path

from orbit import orchestrator_compact as compact_mod
from orbit import orchestrator_jsonl as jsonl_mod
from orbit import orchestrator_meta as meta_mod
from orbit import orchestrator_runner as runner_mod
from orbit import orchestrator_uploads as uploads_mod

DEMO_TITLE = "Showroom widgetów"

# Repo root: this file is at .../src/orbit/scripts/create_demo_session.py
DEMO_ASSETS_DIR = Path(__file__).resolve().parents[1] / "static" / "demo-assets"
UPLOADS_ROOT = Path.home() / ".orchestrator" / "uploads"

# Mirror compact_mod's per-event SSE wait cap. Re-export so a future change to
# the runner timeout only needs to flip one constant in compact_mod.
_TURN_QUEUE_TIMEOUT_S = getattr(compact_mod, "_TURN_QUEUE_TIMEOUT_S", 300.0)


# Each turn drives one widget. The model receives a focused prompt and is
# expected to emit an envelope with that ONE widget kind (plus optional
# markdown intro).
TURNS = [
    {
        "kind": "audio",
        "prompt": (
            "Pokaż widget AUDIO. Wygeneruj krótki TTS przez skill "
            "generate-audio (text=\"Cześć, to demo widgetu audio.\", engine=elevenlabs), "
            "a następnie wyemituj envelope z markdownem-intro + jednym blokiem "
            "audio wskazującym na wygenerowany plik (basename only)."
        ),
    },
    {
        "kind": "download",
        "prompt": (
            "Pokaż widget DOWNLOAD. W katalogu uploads tej sesji jest plik "
            "demo-readme.md. Wyemituj envelope z markdownem-intro + jednym "
            "blokiem download wskazującym na demo-readme.md (filename: "
            "\"demo-readme.md\", mime: \"text/markdown\")."
        ),
    },
    {
        "kind": "video",
        "prompt": (
            "Pokaż widget VIDEO. W katalogu uploads tej sesji jest plik "
            "demo-clip.mp4 + demo-cover.png. Wyemituj envelope z markdownem "
            "+ jednym blokiem video wskazującym na demo-clip.mp4 (poster_path: "
            "\"demo-cover.png\")."
        ),
    },
    {
        "kind": "youtube",
        "prompt": (
            "Pokaż widget YOUTUBE. Wyemituj envelope z markdownem + jednym "
            "blokiem youtube z video_id \"jNQXAC9IVRw\" (pierwszy film w "
            "historii YouTube)."
        ),
    },
    {
        "kind": "chart",
        "prompt": (
            "Pokaż widget CHART. Wyemituj envelope z markdownem + jednym "
            "blokiem chart typu \"bar\", labels: [\"Audio\",\"Download\","
            "\"Video\",\"YouTube\",\"Chart\",\"Map\",\"Custom\"], "
            "datasets: [{label: \"LOC\", data: [40, 35, 50, 45, 80, 70, 60]}]."
        ),
    },
    {
        "kind": "map",
        "prompt": (
            "Pokaż widget MAP. Wyemituj envelope z markdownem + jednym "
            "blokiem map: center [52.23, 21.01] (Warszawa), zoom 7, "
            "markers: [{lat: 52.23, lng: 21.01, label: \"Warszawa\"}, "
            "{lat: 52.41, lng: 16.93, label: \"Poznań\"}], "
            "route: [[52.23, 21.01], [52.41, 16.93]]."
        ),
    },
    {
        "kind": "custom_html",
        "prompt": (
            "Pokaż widget CUSTOM_HTML. Wyemituj envelope z markdownem + "
            "jednym blokiem custom_html zawierającym kompletny <!doctype html> "
            "dokument z animowanym CSS gradient + jednym przyciskiem JS który "
            "po kliknięciu zmienia kolor tła. Height: 200."
        ),
    },
]


def _find_existing_demo() -> str | None:
    """Return existing demo session id if one already exists (idempotency check)."""
    for sid, meta in meta_mod.all_meta().items():
        if (
            meta.get("title") == DEMO_TITLE
            and meta.get("title_manual")
            and jsonl_mod.jsonl_path(sid).exists()
        ):
            return sid
    return None


async def _delete_session(sid: str) -> None:
    """Mirror orchestrator._delete_session_handler cleanup."""
    active = runner_mod._active_runs.get(sid)
    if active is not None:
        await active.cancel()
    jsonl_mod.delete_session(sid)
    await meta_mod.remove_meta(sid)
    try:
        uploads_mod.delete_session_uploads(sid)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(f"[demo] uploads cleanup failed: {exc}", file=sys.stderr)


def _stage_assets(sid: str) -> Path:
    """Pre-stage demo files in the new session's uploads dir. Returns dir."""
    if not DEMO_ASSETS_DIR.is_dir():
        raise RuntimeError(f"demo-assets dir missing: {DEMO_ASSETS_DIR}")
    placeholder = DEMO_ASSETS_DIR / "demo-clip.mp4.PLACEHOLDER"
    if placeholder.exists():
        raise RuntimeError(
            f"demo-clip.mp4 not generated yet. See {placeholder} for"
            f" instructions on regenerating with ffmpeg."
        )
    target = UPLOADS_ROOT / sid
    target.mkdir(parents=True, exist_ok=True)
    for name in ("demo-readme.md", "demo-clip.mp4", "demo-cover.png"):
        src = DEMO_ASSETS_DIR / name
        if not src.exists():
            raise RuntimeError(f"missing demo asset: {src}")
        shutil.copy2(src, target / name)
    print(f"[demo] staged {len(list(target.iterdir()))} files in {target}")
    return target


async def _run_turn(sid: str, prompt: str, *, fresh: bool) -> None:
    """Drive one turn through ClaudeRunner. fresh=True for the very first turn.

    Mirrors orchestrator_compact._run_summary_turn pattern: ClaudeRunner
    subscribed via queue, drained until done. has_run_before=False on the
    fresh turn forces --session-id; True after that uses --resume.
    """
    existing = runner_mod._active_runs.get(sid)
    if existing is not None and not existing._done.is_set():
        raise RuntimeError(f"session {sid} has an in-flight turn")
    if existing is not None:
        runner_mod._active_runs.pop(sid, None)
    active = runner_mod.ClaudeRunner(sid, has_run_before=not fresh)
    runner_mod._active_runs[sid] = active
    queue = active.subscribe()
    turn_task = asyncio.create_task(active.start_turn(prompt))
    try:
        # Drain SSE until done. We don't need to inspect blocks here — if the
        # turn errors, ClaudeRunner broadcasts an error event then closes.
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=_TURN_QUEUE_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"demo turn timed out after {_TURN_QUEUE_TIMEOUT_S}s"
                    f" (no SSE events from claude subprocess)"
                ) from exc
            if evt is None:
                break
    finally:
        try:
            active.subscribers.remove(queue)
        except ValueError:
            pass
        if not turn_task.done():
            turn_task.cancel()
        try:
            await turn_task
        except (Exception, asyncio.CancelledError):
            pass


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Create Showroom widgetów demo session (7 widget kinds)."
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="If a demo session already exists, delete it first.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Create another demo session even if one exists.",
    )
    args = ap.parse_args()

    existing = _find_existing_demo()
    if existing and not args.force:
        if args.replace:
            print(f"[demo] deleting existing demo session {existing}")
            await _delete_session(existing)
        else:
            print(f"[demo] demo session already exists: {existing}")
            print("[demo]   re-run with --replace to recreate or --force to add another")
            return 0

    sid = str(uuid.uuid4())
    print(f"[demo] new session id: {sid}")
    _stage_assets(sid)

    # Turn 0 = fresh seed (compact_mod._seed_new_session uses new_id kwarg).
    seed_prompt = (
        "Jesteś agentem prezentującym 7 widgetów dashboardu w demo-sesji "
        "\"Showroom widgetów\". To pierwsza tura — krótko zapowiedz "
        "showroom (2 zdania w markdown), nic więcej. Kolejne tury będą "
        "demonstrować kolejne widgety."
    )
    print("[demo] turn 0 (seed): introduction")
    seed_id = await compact_mod._seed_new_session(seed_prompt, new_id=sid)
    if seed_id != sid:
        raise RuntimeError(f"seed returned different id: {seed_id} != {sid}")

    # Turns 1-7 — one widget each.
    for i, turn in enumerate(TURNS, start=1):
        print(f"[demo] turn {i}/{len(TURNS)}: {turn['kind']}")
        try:
            await _run_turn(sid, turn["prompt"], fresh=False)
        except Exception as exc:  # noqa: BLE001 — surface + rollback
            print(f"[demo] turn {i} ({turn['kind']}) failed: {exc}", file=sys.stderr)
            print(f"[demo] rolling back demo session {sid}", file=sys.stderr)
            await _delete_session(sid)
            return 1

    # Stamp sidecar — pin + manual title so it floats to the top of the list.
    await meta_mod.set_meta(
        sid,
        title=DEMO_TITLE,
        title_manual=True,
        pinned=True,
        model="opus",
    )
    jsonl_mod.invalidate_cache()

    print(f"[demo] DONE — demo session {sid} pinned and titled '{DEMO_TITLE}'.")
    print("[demo]   open the dashboard, the session is at the top of the list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
