"""A2A maildir garbage collector — one sweep over every agent's maildir.

The bus (:mod:`orchestrator_a2a`) writes envelopes but NEVER expires them:
``ttl`` is advisory, and a drained message lingers in ``cur/`` forever. This
module is the hourly cron tick that keeps ``~/.orchestrator/a2a`` bounded:

* ``cur/*.json`` (drained mail) — delete once older than its envelope ``ttl``
  (default :data:`orchestrator_a2a.DEFAULT_TTL` = 24h) or, if the envelope is
  unreadable, older than :data:`_UNREADABLE_TTL_S` (24h).
* ``inbox/*.json`` (UNdrained mail) — a message no agent ever drained is a
  symptom (an offline/looping agent), so we keep it far longer; delete only
  past the hard ceiling :data:`_INBOX_CEILING_S` (7 days), and log a WARNING
  before each such delete so the never-drained case is visible in journald.
* per-inbox count cap (:data:`_INBOX_CAP`) — drop the oldest beyond the cap
  (oldest-first by id, which is chronological), each logged, so a flood into a
  cold inbox can't grow unbounded between ceiling sweeps.

Pure stdlib, defensive: a read/stat/unlink error on one file is logged and
skipped, never aborting the sweep or escaping the module. Mirrors the
tasks-reminders tick contract (a ``sweep_once`` + a ``run_tick_cli`` invoked by
the ``a2a-gc-tick`` subcommand). Import-safe: no I/O at import.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import orchestrator_a2a as a2a_mod

_logger = logging.getLogger(__name__)

# Fallback age for a `cur/` envelope we couldn't read a ttl out of — same as the
# bus default ttl (24h) so a corrupt drained message doesn't outlive a good one.
_UNREADABLE_TTL_S = 86_400

# Hard ceiling for UNdrained `inbox/` mail (7 days). A message no agent drained
# in a week is almost certainly stranded; we log + delete past this.
_INBOX_CEILING_S = 7 * 86_400

# Per-inbox count cap. Beyond this, the oldest are dropped (logged) regardless
# of age so a flood into a cold/looping inbox stays bounded between sweeps.
_INBOX_CAP = 1000

_MAILDIR_SUBDIRS = ("cur", "inbox")


@dataclass(frozen=True)
class SweepSummary:
    """Counters for one sweep, mirroring tasks_reminders.ScanSummary."""

    maildirs: int
    cur_deleted: int
    inbox_deleted: int
    capped: int
    errors: int

    def to_dict(self) -> dict[str, int]:
        return {
            "maildirs": self.maildirs,
            "cur_deleted": self.cur_deleted,
            "inbox_deleted": self.inbox_deleted,
            "capped": self.capped,
            "errors": self.errors,
        }


def _safe_unlink(path: Path) -> bool:
    """Delete one file; swallow + log any OSError. True iff removed."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        _logger.warning("a2a-gc: unlink failed for %s: %s", path, exc)
        return False


def _file_mtime(path: Path) -> float | None:
    """`os.stat().st_mtime`, or None on any stat error (logged)."""
    try:
        return path.stat().st_mtime
    except OSError as exc:
        _logger.warning("a2a-gc: stat failed for %s: %s", path, exc)
        return None


def _read_ttl(path: Path) -> int | None:
    """Read the envelope ttl from a `cur/` file; None if unreadable/missing.

    Read-only and tolerant — a non-JSON / non-object / non-int-ttl file yields
    None so the caller falls back to the unreadable-age policy.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    ttl = raw.get("ttl")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        return None
    return ttl


def _json_files(directory: Path) -> list[Path]:
    """`*.json` files whose stem matches the a2a id regex, oldest-first by id.

    Skips junk names (the id's leading compact-UTC stamp makes a lexical sort
    chronological). Any scandir error yields an empty list (logged).
    """
    if not directory.is_dir():
        return []
    out: list[Path] = []
    try:
        scanner = os.scandir(directory)
    except OSError as exc:
        _logger.warning("a2a-gc: scandir failed for %s: %s", directory, exc)
        return []
    with scanner as it:
        for entry in it:
            try:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
            except OSError:
                continue
            if not a2a_mod.A2A_ID_RE.fullmatch(entry.name[:-5]):
                continue
            out.append(Path(entry.path))
    out.sort(key=lambda p: p.name)
    return out


def _sweep_cur(directory: Path, now: float) -> tuple[int, int]:
    """Delete drained `cur/*.json` past its ttl (or the unreadable fallback).

    Returns ``(deleted, errors)``. Errors are already logged inside the helpers;
    the count is best-effort observability, not a failure signal.
    """
    deleted = 0
    errors = 0
    for path in _json_files(directory):
        mtime = _file_mtime(path)
        if mtime is None:
            errors += 1
            continue
        ttl = _read_ttl(path)
        max_age = ttl if ttl is not None else _UNREADABLE_TTL_S
        if (now - mtime) > max_age:
            if _safe_unlink(path):
                deleted += 1
            else:
                errors += 1
    return deleted, errors


def _sweep_inbox(directory: Path, now: float) -> tuple[int, int, int]:
    """Sweep UNdrained `inbox/*.json`: hard ceiling + count cap.

    Returns ``(deleted_by_age, capped, errors)``. A WARNING is logged before
    every age-based delete (a never-drained message is a symptom worth seeing),
    and once per over-cap drop.
    """
    files = _json_files(directory)  # oldest-first
    deleted = 0
    capped = 0
    errors = 0

    # Count cap: drop the oldest beyond _INBOX_CAP first, so the ceiling pass
    # below operates on a bounded set. `files` is oldest-first by id.
    if len(files) > _INBOX_CAP:
        overflow = files[: len(files) - _INBOX_CAP]
        files = files[len(files) - _INBOX_CAP :]
        for path in overflow:
            _logger.warning(
                "a2a-gc: inbox %s over cap (%d) — dropping oldest %s",
                directory, _INBOX_CAP, path.name,
            )
            if _safe_unlink(path):
                capped += 1
            else:
                errors += 1

    for path in files:
        mtime = _file_mtime(path)
        if mtime is None:
            errors += 1
            continue
        if (now - mtime) > _INBOX_CEILING_S:
            _logger.warning(
                "a2a-gc: deleting never-drained inbox message %s (age > %ds)",
                path, _INBOX_CEILING_S,
            )
            if _safe_unlink(path):
                deleted += 1
            else:
                errors += 1
    return deleted, capped, errors


def _sweep_maildir(
    maildir: Path, root: Path, now: float
) -> tuple[int, int, int, int]:
    """Apply the cur+inbox policy to one ``{cur,inbox}`` pair.

    Returns ``(cur_deleted, inbox_deleted, capped, errors)``. Containment-guards
    the maildir under ``root`` before touching anything; a guard/stat failure is
    counted as one error and the maildir skipped. Shared by the agent-level
    sweep and each per-session maildir.
    """
    try:
        if not maildir.resolve().is_relative_to(root.resolve()):
            return 0, 0, 0, 0
    except OSError:
        return 0, 0, 0, 1
    cur_deleted, ce = _sweep_cur(maildir / "cur", now)
    inbox_deleted, capped, ie = _sweep_inbox(maildir / "inbox", now)
    return cur_deleted, inbox_deleted, capped, ce + ie


def _sweep_sessions(
    agent_dir: Path, root: Path, now: float
) -> tuple[int, int, int, int]:
    """Sweep ``<agent_dir>/sessions/*/{cur,inbox}`` with the agent-level policy.

    Returns ``(cur_deleted, inbox_deleted, capped, errors)``. Walks each
    ``sessions/<sid>/`` dir, applies the same ttl/ceiling/cap policy, and
    best-effort removes an empty ``sessions/<sid>/`` (both inbox+cur gone or
    empty) afterwards. Defensive: any scandir/guard error is counted + skipped,
    never raised.
    """
    sessions_root = agent_dir / "sessions"
    if not sessions_root.is_dir():
        return 0, 0, 0, 0
    cur_deleted = 0
    inbox_deleted = 0
    capped = 0
    errors = 0
    try:
        scanner = os.scandir(sessions_root)
    except OSError as exc:
        _logger.warning("a2a-gc: scandir failed for %s: %s", sessions_root, exc)
        return 0, 0, 0, 1
    with scanner as it:
        for entry in it:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                errors += 1
                continue
            session_dir = Path(entry.path)
            cd, idel, icap, err = _sweep_maildir(session_dir, root, now)
            cur_deleted += cd
            inbox_deleted += idel
            capped += icap
            errors += err
            _prune_empty_session(session_dir)
    return cur_deleted, inbox_deleted, capped, errors


def _prune_empty_session(session_dir: Path) -> None:
    """Best-effort: remove an empty ``sessions/<sid>/`` (inbox+cur empty/gone).

    Removes the inbox/cur subdirs if empty, then the session dir itself. Any
    OSError (non-empty, race, perms) is swallowed — pruning is a nicety, never a
    failure path.
    """
    try:
        for sub in ("inbox", "cur"):
            p = session_dir / sub
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        # Only drop the session dir if nothing remains under it.
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            session_dir.rmdir()
    except OSError:
        pass


def sweep_once(now: float | None = None) -> SweepSummary:
    """One GC pass over ``A2A_ROOT/*/{cur,inbox}`` + ``*/sessions/*/{cur,inbox}``.

    Never raises. Iterates each agent maildir directly under
    :data:`orchestrator_a2a.A2A_ROOT` (a flat tree of ``<agent_key>`` dirs),
    sweeps its agent-level inbox/cur AND every per-session maildir under
    ``<agent_key>/sessions/<sid>/`` with the SAME ttl/ceiling/cap policy. Any
    error on a single maildir/file is counted + logged and the sweep continues.
    Returns a :class:`SweepSummary`.
    """
    root = a2a_mod.A2A_ROOT
    if not root.is_dir():
        return SweepSummary(0, 0, 0, 0, 0)

    clock = time.time() if now is None else now
    maildirs = 0
    cur_deleted = 0
    inbox_deleted = 0
    capped = 0
    errors = 0

    try:
        scanner = os.scandir(root)
    except OSError as exc:
        _logger.warning("a2a-gc: scandir failed for root %s: %s", root, exc)
        return SweepSummary(0, 0, 0, 0, 1)

    with scanner as it:
        for entry in it:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                errors += 1
                continue
            # Containment guard: the resolved maildir must stay under root.
            agent_dir = Path(entry.path)
            try:
                if not agent_dir.resolve().is_relative_to(root.resolve()):
                    continue
            except OSError:
                errors += 1
                continue
            maildirs += 1
            # Agent-level inbox/cur.
            cd, idel, icap, err = _sweep_maildir(agent_dir, root, clock)
            cur_deleted += cd
            inbox_deleted += idel
            capped += icap
            errors += err
            # Per-session inbox/cur (same policy), pruning empties as we go.
            scd, sidel, sicap, serr = _sweep_sessions(agent_dir, root, clock)
            cur_deleted += scd
            inbox_deleted += sidel
            capped += sicap
            errors += serr

    return SweepSummary(maildirs, cur_deleted, inbox_deleted, capped, errors)


# ── CLI tick entry (called from __main__) ───────────────────────


def run_tick_cli() -> int:
    """Subprocess entry: ``python -m orbit a2a-gc-tick``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        summary = sweep_once()
    except Exception as exc:  # noqa: BLE001 — never let the tick crash the cron run
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **summary.to_dict()}, ensure_ascii=False))
    return 0
