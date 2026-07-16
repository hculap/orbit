"""PoC: drive interactive `claude` via tmux + JSONL tail.

Validates 4 unknowns before we wire this into orbit:
  1. trust       — pre-seed ~/.claude.json so first prompt doesn't block on trust menu
  2. jsonl-live  — JSONL file appears + flushes per-turn with `stop_hook_summary`
  3. status      — `/status` slash command reports Subscription routing (not API)
  4. resume      — kill + `--resume <uuid>` restores conversation context

Each check is independent: `--check trust|jsonl-live|status|resume|all`. Default = all.
Uses dedicated tmux socket `-L hd-poc` so it never collides with the user's tmux server.

Run: `python scripts/poc_tmux_claude.py --check all`
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

HOME = Path.home()
SOCKET = "hd-poc"
CLAUDE_BIN = shutil.which("claude") or str(HOME / ".local" / "bin" / "claude")
TMUX_BIN = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
TURN_TIMEOUT_S = 90.0
STARTUP_WAIT_S = 9.0
TERM_VALUE = "xterm-256color"


# ── tmux primitives ────────────────────────────────────────────────


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [TMUX_BIN, "-L", SOCKET, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _has_session(name: str) -> bool:
    return _tmux("has-session", "-t", name, check=False).returncode == 0


def _kill(name: str) -> None:
    if _has_session(name):
        _tmux("kill-session", "-t", name, check=False)


def _spawn(
    name: str,
    cwd: Path,
    session_id: str,
    *,
    resume: bool = False,
) -> None:
    flag = "--resume" if resume else "--session-id"
    inner = " ".join(
        [
            CLAUDE_BIN,
            flag,
            session_id,
            "--permission-mode",
            "auto",
            "--add-dir",
            str(HOME / ".claude"),
        ]
    )
    _tmux(
        "new-session",
        "-d",
        "-s",
        name,
        "-x",
        "200",
        "-y",
        "50",
        "-c",
        str(cwd),
        "-e",
        "ANTHROPIC_API_KEY=",
        "-e",
        f"TERM={TERM_VALUE}",
        inner,
    )


def _send_prompt(name: str, text: str) -> None:
    buf = f"buf-{name}"
    proc = subprocess.Popen(
        [TMUX_BIN, "-L", SOCKET, "load-buffer", "-b", buf, "-"],
        stdin=subprocess.PIPE,
    )
    proc.communicate(text.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"load-buffer failed: rc={proc.returncode}")
    _tmux("paste-buffer", "-b", buf, "-p", "-d", "-t", name)
    time.sleep(0.15)
    _tmux("send-keys", "-t", name, "Enter")


def _capture(name: str) -> str:
    return _tmux("capture-pane", "-p", "-t", name, check=False).stdout


# ── filesystem helpers ─────────────────────────────────────────────


def _slug(cwd: Path) -> str:
    """Match claude's project-dir encoding: replace [/_.] with `-`."""
    real = os.path.realpath(str(cwd))
    out = []
    for ch in real:
        out.append("-" if ch in "/_." else ch)
    return "".join(out)


def _jsonl_path(cwd: Path, session_id: str) -> Path:
    return HOME / ".claude" / "projects" / _slug(cwd) / f"{session_id}.jsonl"


def _trust_cwd(cwd: Path) -> None:
    """Pre-seed ~/.claude.json so the first interactive run doesn't block."""
    config = HOME / ".claude.json"
    if config.exists():
        data = json.loads(config.read_text())
    else:
        data = {}
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    real = os.path.realpath(str(cwd))
    entry = data["projects"].setdefault(real, {})
    entry["hasTrustDialogAccepted"] = True
    tmp = config.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, config)


def _tail_until_turn_end(
    path: Path,
    timeout: float = TURN_TIMEOUT_S,
    since_byte: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Read JSONL from ``since_byte``; return (all_lines, end_offset) once the
    current turn finishes.

    Primary signal: a ``system / stop_hook_summary`` event after the latest
    user line within the tailed range. Fallback: most recent assistant line
    has ``stop_reason: end_turn`` and mtime has been quiet for 3 s.

    Returns the absolute end offset so callers can chain multiple turns on
    the same JSONL (e.g. resume tests).
    """
    deadline = time.monotonic() + timeout
    lines: list[dict[str, Any]] = []
    offset = since_byte
    user_seen_at: int | None = None
    last_change = time.monotonic()
    buffer = b""
    while time.monotonic() < deadline:
        if not path.exists():
            time.sleep(0.1)
            continue
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
            offset += len(chunk)
        if chunk:
            last_change = time.monotonic()
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                lines.append(obj)
                if obj.get("type") == "user":
                    user_seen_at = len(lines)
                if (
                    user_seen_at is not None
                    and obj.get("type") == "system"
                    and obj.get("subtype") == "stop_hook_summary"
                ):
                    return lines, offset
        else:
            quiet = time.monotonic() - last_change
            if user_seen_at is not None and quiet > 3.0:
                for ln in reversed(lines):
                    if ln.get("type") == "assistant":
                        if ln.get("message", {}).get("stop_reason") == "end_turn":
                            return lines, offset
                        break
            time.sleep(0.1)
    raise TimeoutError(f"turn never completed within {timeout}s")


def _last_assistant_text(lines: list[dict[str, Any]]) -> str:
    for obj in reversed(lines):
        if obj.get("type") != "assistant":
            continue
        content = obj.get("message", {}).get("content", [])
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts).strip()
    return ""


# ── checks ─────────────────────────────────────────────────────────


def _setup() -> tuple[Path, str, str]:
    cwd = Path(tempfile.mkdtemp(prefix="poc-tmux-claude-"))
    sid = str(uuid.uuid4())
    name = f"hd-poc-{sid[:8]}"
    print(f"  cwd: {cwd}")
    print(f"  sid: {sid}")
    print(f"  tmux session: {name}")
    return cwd, sid, name


def _wait_for_jsonl(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise TimeoutError(f"jsonl never appeared at {path}")


def check_trust() -> bool:
    print("\n[check: trust] ─────────────────")
    cwd, sid, name = _setup()
    try:
        _trust_cwd(cwd)
        print("  pre-seeded ~/.claude.json")
        _spawn(name, cwd, sid)
        time.sleep(STARTUP_WAIT_S)
        if not _has_session(name):
            print("  FAIL: tmux session died at startup")
            print("  pane:", _capture(name)[-400:])
            return False
        _send_prompt(name, "Reply with the single word PONG.")
        jsonl = _jsonl_path(cwd, sid)
        _wait_for_jsonl(jsonl)
        lines, _ = _tail_until_turn_end(jsonl)
        text = _last_assistant_text(lines)
        ok = bool(text) and "pong" in text.lower()
        print(f"  assistant: {text!r}")
        print(f"  RESULT: {'PASS' if ok else 'FAIL'} — trust dialog did not block first turn" if ok else f"  RESULT: FAIL — no PONG: {text!r}")
        return ok
    finally:
        _kill(name)


def check_jsonl_live() -> bool:
    print("\n[check: jsonl-live] ─────────────")
    cwd, sid, name = _setup()
    try:
        _trust_cwd(cwd)
        _spawn(name, cwd, sid)
        time.sleep(STARTUP_WAIT_S)
        t0 = time.monotonic()
        _send_prompt(name, "Reply with exactly: ONE TWO THREE FOUR FIVE.")
        jsonl = _jsonl_path(cwd, sid)
        _wait_for_jsonl(jsonl)
        lines, _ = _tail_until_turn_end(jsonl)
        elapsed = time.monotonic() - t0
        types = [(ln.get("type"), ln.get("subtype")) for ln in lines]
        has_stop_hook = any(t == "system" and s == "stop_hook_summary" for t, s in types)
        text = _last_assistant_text(lines)
        print(f"  lines: {len(lines)}  elapsed: {elapsed:.1f}s")
        for i, (t, s) in enumerate(types):
            print(f"   [{i}] type={t} subtype={s or ''}")
        print(f"  assistant: {text!r}")
        ok = has_stop_hook and bool(text)
        print(f"  RESULT: {'PASS' if ok else 'FAIL'} — JSONL flush detected" if ok else "  RESULT: FAIL — no stop_hook_summary")
        return ok
    finally:
        _kill(name)


def check_status() -> bool:
    print("\n[check: status] ─────────────────")
    cwd, sid, name = _setup()
    try:
        _trust_cwd(cwd)
        _spawn(name, cwd, sid)
        time.sleep(STARTUP_WAIT_S)
        _send_prompt(name, "/status")
        time.sleep(5.0)
        pane = _capture(name)
        normalized = pane.lower()
        is_subscription = (
            "subscription" in normalized
            or "claude max" in normalized
            or "claude pro" in normalized
        )
        is_api_billing = "api usage billing" in normalized or "api billing" in normalized
        print("  pane tail:")
        for line in pane.splitlines()[-25:]:
            print(f"    {line}")
        ok = is_subscription and not is_api_billing
        print(f"  RESULT: {'PASS' if ok else 'FAIL'} — Subscription routing" if ok else f"  RESULT: FAIL — sub={is_subscription} api={is_api_billing}")
        return ok
    finally:
        _kill(name)


def check_resume() -> bool:
    print("\n[check: resume] ─────────────────")
    cwd, sid, name = _setup()
    try:
        _trust_cwd(cwd)
        _spawn(name, cwd, sid)
        time.sleep(STARTUP_WAIT_S)
        marker = f"MARKER-{uuid.uuid4().hex[:8].upper()}"
        _send_prompt(name, f"Remember the codeword: {marker}. Reply with just 'noted'.")
        jsonl = _jsonl_path(cwd, sid)
        _wait_for_jsonl(jsonl)
        _, end_offset = _tail_until_turn_end(jsonl)
        print(f"  first turn done; marker={marker}  offset={end_offset}")

        _kill(name)
        time.sleep(1.0)
        _spawn(name, cwd, sid, resume=True)
        time.sleep(STARTUP_WAIT_S)
        if not _has_session(name):
            print("  FAIL: tmux session died after resume")
            print("  pane:", _capture(name)[-400:])
            return False

        _send_prompt(name, "What was the codeword I asked you to remember? Just the codeword, nothing else.")
        lines, _ = _tail_until_turn_end(jsonl, since_byte=end_offset)
        text = _last_assistant_text(lines)
        ok = marker in text
        print(f"  assistant: {text!r}")
        print(f"  RESULT: {'PASS' if ok else 'FAIL'} — context survived resume" if ok else f"  RESULT: FAIL — marker {marker} missing")
        return ok
    finally:
        _kill(name)


# ── main ──────────────────────────────────────────────────────────


CHECKS = {
    "trust": check_trust,
    "jsonl-live": check_jsonl_live,
    "status": check_status,
    "resume": check_resume,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        choices=[*CHECKS.keys(), "all"],
        default="all",
        help="which check to run",
    )
    args = parser.parse_args()

    print(f"claude: {CLAUDE_BIN}")
    print(f"tmux: {TMUX_BIN}  (socket: {SOCKET})")

    if shutil.which(CLAUDE_BIN) is None and not Path(CLAUDE_BIN).exists():
        print(f"claude binary not found at {CLAUDE_BIN}", file=sys.stderr)
        return 2
    if shutil.which(TMUX_BIN) is None and not Path(TMUX_BIN).exists():
        print(f"tmux binary not found at {TMUX_BIN}", file=sys.stderr)
        return 2

    to_run = list(CHECKS.keys()) if args.check == "all" else [args.check]
    results: dict[str, bool] = {}
    for name in to_run:
        try:
            results[name] = CHECKS[name]()
        except Exception as exc:
            print(f"  EXCEPTION in {name}: {exc}", file=sys.stderr)
            results[name] = False

    print("\n══════ SUMMARY ══════")
    for name, ok in results.items():
        print(f"  {name:12s} {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
