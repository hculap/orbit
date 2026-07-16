#!/usr/bin/env python3
"""hetzner-teleport — move sessions between the dashboard server and LOCAL Claude.

Two directions (the skill auto-detects which from the argument shape):

  IMPORT  (server → here)
      teleport_cli.py import <server-url-or-session-id> [--title NAME]
    Pull a session FROM the dashboard and materialize it as a fresh, resumable
    session in the CURRENT local project (the cwd where Claude runs). No target
    agent needed — "here" IS the target. GET only (no token).

  EXPORT  (here → server)
      teleport_cli.py export <agent-lib-id> [--session LOCAL_ID] [--title NAME]
    Push the current local session (or --session) UP to a chosen server agent
    (e.g. projects/x, areas/Dom, or "global"). POST (token required).

Config when off-box (laptop): <skill_dir>/config.json
  {"dashboard_url": "https://...", "artifact_token": "..."}
On the dashboard host the HD_* env vars are used instead.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid as uuid_mod
from pathlib import Path

DEFAULT_DASHBOARD_URL = "http://localhost:8766"
TIMEOUT_S = 60
SKILL_DIR = Path(__file__).resolve().parent.parent  # scripts/.. == skill dir
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

ENVELOPE_VERSION = 1
ENVELOPE_KIND = "hetzner-session-teleport"

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SLUG_NONALNUM = re.compile(r"[^a-zA-Z0-9]")


# ── config / endpoints ─────────────────────────────────────────────


def _load_config() -> dict:
    try:
        path = SKILL_DIR / "config.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def dashboard_url() -> str:
    return (
        os.environ.get("HD_NOTIFY_URL")
        or _load_config().get("dashboard_url")
        or DEFAULT_DASHBOARD_URL
    ).rstrip("/")


def read_token() -> str | None:
    for cand in (os.environ.get("HD_ARTIFACT_TOKEN_FILE"),
                 str(Path.home() / ".orchestrator" / "artifact_token")):
        if not cand:
            continue
        p = Path(cand).expanduser()
        try:
            if p.is_file():
                tok = p.read_text(encoding="utf-8").strip()
                if tok:
                    return tok
        except OSError:
            continue
    cfg = _load_config().get("artifact_token")
    return cfg.strip() if isinstance(cfg, str) and cfg.strip() else None


def _http(method: str, path: str, *, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Artifact-Token"] = token
    req = urllib.request.Request(dashboard_url() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw}
    except (urllib.error.URLError, OSError) as exc:
        print(f"error: cannot reach dashboard at {dashboard_url()}: {exc}", file=sys.stderr)
        sys.exit(2)


# ── local session filesystem (~/.claude/projects) ──────────────────


def cwd_to_slug(cwd: str | Path) -> str:
    """Match Claude Code's slug: every non-alphanumeric char → '-'."""
    return _SLUG_NONALNUM.sub("-", str(cwd))


def _local_files(session_id: str) -> list[Path]:
    out: list[Path] = []
    if CLAUDE_PROJECTS.is_dir():
        for d in CLAUDE_PROJECTS.iterdir():
            if not d.is_dir():
                continue
            f = d / f"{session_id}.jsonl"
            if f.is_file():
                out.append(f)
    out.sort(key=lambda p: (-p.stat().st_size, p.parent.name))
    return out


def collect_local_lines(session_id: str) -> list[dict]:
    files = _local_files(session_id)
    if not files:
        raise FileNotFoundError(session_id)
    seen_uuid: set[str] = set()
    seen_meta: set[str] = set()
    lines: list[dict] = []
    for path in files:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            u = obj.get("uuid")
            if isinstance(u, str) and u:
                if u in seen_uuid:
                    continue
                seen_uuid.add(u)
            else:
                key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
                if key in seen_meta:
                    continue
                seen_meta.add(key)
            lines.append(obj)
    return lines


def latest_local_session(cwd: str) -> str | None:
    d = CLAUDE_PROJECTS / cwd_to_slug(cwd)
    if not d.is_dir():
        return None
    jsonls = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonls[0].stem if jsonls else None


def parse_session_id(arg: str) -> str:
    m = _UUID_RE.search(arg or "")
    if not m:
        print(f"error: no session id / uuid found in {arg!r}", file=sys.stderr)
        sys.exit(2)
    return m.group(0)


# ── IMPORT: server → here (local project) ──────────────────────────


def cmd_import(args: argparse.Namespace) -> int:
    sid = parse_session_id(args.source)
    status, body = _http("GET", f"/api/orchestrator/sessions/{sid}/teleport")
    if status != 200:
        print(f"error: download failed ({status}): {body.get('detail')}", file=sys.stderr)
        return 1
    transcript = body.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        print("error: server bundle has no transcript", file=sys.stderr)
        return 1
    target_cwd = os.getcwd()
    new_id = str(uuid_mod.uuid4())
    dest_dir = CLAUDE_PROJECTS / cwd_to_slug(target_cwd)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{new_id}.jsonl"
    # Searchable name in the `/resume` picker comes from a native `ai-title`
    # record. Default to "teleport <short-id>" so the session is ALWAYS findable
    # by name, not just by first-prompt text.
    name = (args.title or "").strip() or f"teleport {sid[:8]}"
    with dest.open("w", encoding="utf-8") as fh:
        for obj in transcript:
            o = dict(obj)
            if "sessionId" in o:
                o["sessionId"] = new_id
            if "cwd" in o:
                o["cwd"] = target_cwd
            fh.write(json.dumps(o, ensure_ascii=False) + "\n")
        # Stamp the title so `claude --resume <name>` / the picker finds it.
        fh.write(json.dumps(
            {"type": "ai-title", "aiTitle": name, "sessionId": new_id},
            ensure_ascii=False) + "\n")
    print(f"imported {len(transcript)} lines from server session {sid} as “{name}”")
    print(f"→ local session {new_id} in {target_cwd}")
    print(f"resume it:  claude --resume {new_id}   (or pick “{name}” in /resume)")
    return 0


# ── EXPORT: here (local session) → server agent ────────────────────


def cmd_export(args: argparse.Namespace) -> int:
    cwd = os.getcwd()
    local_id = args.session or latest_local_session(cwd)
    if not local_id:
        print(f"error: no local session found for project {cwd} (pass --session ID)", file=sys.stderr)
        return 2
    try:
        lines = collect_local_lines(local_id)
    except FileNotFoundError:
        print(f"error: local session {local_id} not found", file=sys.stderr)
        return 1
    agent = "" if args.agent.strip().lower() == "global" else args.agent.strip()
    envelope = {
        "version": ENVELOPE_VERSION,
        "kind": ENVELOPE_KIND,
        "source_session_id": local_id,
        "source_cwd": cwd,
        "source_lib_id": None,
        "model": None,
        "title": args.title,
        "exported_at": time.time(),
        "msg_count": len(lines),
        "transcript": lines,
    }
    token = read_token()
    if not token:
        print("error: no artifact token (set artifact_token in config.json — "
              "copy it from Settings → Serwer → Teleport)", file=sys.stderr)
        return 2
    payload: dict = {"envelope": envelope, "lib_id": agent}
    if args.title:
        payload["title"] = args.title
    status, body = _http("POST", "/api/orchestrator/sessions/teleport", body=payload, token=token)
    if status != 200:
        print(f"error: upload failed ({status}): {body.get('detail')}", file=sys.stderr)
        return 1
    print(f"exported local session {local_id} → server session {body.get('new_session_id')} "
          f"(agent={body.get('lib_id') or 'global'}, {body.get('n_lines')} lines)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hetzner-teleport",
        description="Teleport sessions between the dashboard and local Claude (#91).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("import", help="server → here: pull a session into the current local project")
    p_imp.add_argument("source", help="server chat URL or session id/uuid")
    p_imp.add_argument("--title", default=None, help="optional name (informational)")
    p_imp.set_defaults(func=cmd_import)

    p_exp = sub.add_parser("export", help="here → server: push the current local session to an agent")
    p_exp.add_argument("agent", help='target agent: projects/<x>, areas/<x>, or "global"')
    p_exp.add_argument("--session", default=None, help="local session id (default: newest in this project)")
    p_exp.add_argument("--title", default=None, help="optional title for the new server session")
    p_exp.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
