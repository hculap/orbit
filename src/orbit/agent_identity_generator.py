"""LLM-driven identity-prompt generation for a single PARA agent.

Spawns ``claude -p <meta-prompt>`` against the agent's cwd to inspect README
/ INDEX / AGENTS / package.json / pyproject.toml / dir layout and produce:

    ICON: <single emoji>

    <markdown identity body, ≤500 words>

The ``--append-system-prompt-file`` flag is intentionally NOT passed: we
want plain claude output (no envelope JSON wrapping) so we can parse the
emoji header + body directly. Output is written to
``~/.orchestrator/agents/<kind>/<lib_id>/identity.md`` and the emoji to the
sibling ``icon.txt``.

Failure modes (timeout, non-zero exit, malformed output) are caught here:
``identity.md`` is written empty so the caller sees a valid "no identity yet"
state and ``ok=False`` is returned with the error string for UI surfacing.
"""
from __future__ import annotations
import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import agent_prompts as _prompts
from . import orchestrator_env as env_mod

# ── tunables ──────────────────────────────────────────────────────

# 120s: the interactive path cold-spawns a tmux slot (~15-25s) then runs an
# AGENTIC turn (Read the project, Write identity.md) — multiple roundtrips, so
# 60s was too tight (mirrors the titles 45→90 bump). Programmatic -p path uses
# the same value (it's a single -p turn, comfortably under).
GENERATION_TIMEOUT_S: float = 120.0
CLAUDE_BIN_DEFAULT: str = "/usr/bin/claude"
SUBPROCESS_BUFFER_LIMIT: int = 2 * 1024 * 1024
MAX_DIR_ENTRIES: int = 30  # cap dir-listing in meta-prompt

_ICON_HEADER_RE: re.Pattern[str] = re.compile(r"^\s*ICON:\s*(.+?)\s*$", re.MULTILINE)


# ── public API ────────────────────────────────────────────────────


async def generate_identity(
    kind: str,
    lib_id: str,
    cwd: Path,
    *,
    regenerate: bool = False,
) -> dict[str, Any]:
    """Spawn ``claude -p`` to author identity.md + pick an icon for one agent.

    Args:
        kind:       PARA bucket — ``"areas"``, ``"projects"``, ``"resources"``.
        lib_id:     Path-rest after the kind prefix (e.g. ``"my-project"``).
                    Validated by ``agent_prompts.agent_identity_path`` so any
                    traversal raises ``ValueError`` before we touch disk.
        cwd:        Directory claude inspects (project root / area dir).
        regenerate: Reserved flag forwarded by the route layer; the meta-prompt
                    is the same either way today, but we keep the parameter so
                    UX copy can differ ("first generation" vs "regenerate").

    Returns:
        ``{"ok": bool, "identity": str, "icon": str | None, "error": str | None}``.
        On any failure ``identity.md`` is still written (empty) so the
        sidecar's ``identity_generated_at`` toggle is the single source of
        truth — readers can rely on file existence without distinguishing
        "never tried" vs "tried and failed".
    """
    del regenerate  # accepted for API symmetry; same prompt for now

    # Validate destination paths up front. Raising ``ValueError`` early keeps
    # path-traversal misuse from spawning a subprocess against a bogus cwd.
    identity_path = _prompts.agent_identity_path(kind, lib_id)
    icon_path = _prompts.agent_icon_path(kind, lib_id)
    identity_path.parent.mkdir(parents=True, exist_ok=True)

    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_dir():
        return _persist_failure(
            identity_path,
            error=f"cwd does not exist: {cwd_path}",
        )

    meta_prompt = _build_meta_prompt(kind=kind, lib_id=lib_id, cwd=cwd_path)

    # Route by identity_runner_mode: "interactive" (default) → tmux pool
    # (subscription, raw extraction so the ICON: header survives); programmatic
    # → the legacy `claude -p` path below (credit pool) kept as rollback.
    try:
        from . import orchestrator_settings as _settings
        mode = _settings.resolve_runner_mode("identity_runner_mode")
    except Exception:  # noqa: BLE001 — settings unavailable → subscription default
        mode = "interactive"
    if mode == "interactive":
        from . import orchestrator_oneshot as oneshot_mod
        res = await oneshot_mod.run_oneshot(
            meta_prompt, cwd=cwd_path, raw=True,
            timeout_s=GENERATION_TIMEOUT_S, label="identity-gen",
        )
        if not res["ok"]:
            return _persist_failure(identity_path, error=res["error"] or "identity generation failed")
        return _persist_identity(identity_path, kind, lib_id, res["text"])

    # ── programmatic rollback (claude -p) ──
    # Force a known session-id so we can delete the JSONL afterwards. Without
    # this the auto-generated transcript would surface as a "Global" session
    # in /api/orchestrator/sessions (its sidecar cwd is None) and pollute
    # the user's chat history with the bootstrap meta-prompt.
    bootstrap_sid = str(uuid.uuid4())
    args = [
        _resolve_claude_bin(),
        "-p",
        meta_prompt,
        "--session-id",
        bootstrap_sid,
        "--output-format",
        "text",
        # See orchestrator_runner.build_args — `auto` + `--add-dir ~/.claude`
        # is the only combination that gets past the cli's hardcoded
        # `.claude/` write gate. `--dangerously-skip-permissions` does NOT.
        "--permission-mode",
        "auto",
        "--add-dir",
        str(_prompts.HOME / ".claude"),
    ]
    env = env_mod.scrubbed_env({"CLAUDE_CONFIG_DIR": str(_prompts.HOME / ".claude")})
    env_mod.log_billing_path("identity-gen", interactive=False)

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd_path),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_BUFFER_LIMIT,
            )
        except FileNotFoundError:
            return _persist_failure(identity_path, error="claude binary not found")
        except OSError as exc:
            return _persist_failure(identity_path, error=f"spawn failed: {exc}")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=GENERATION_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            # Don't leak the subprocess; SIGTERM then SIGKILL after a short grace.
            await _kill_proc(proc)
            return _persist_failure(
                identity_path,
                error=f"identity generation timed out after {GENERATION_TIMEOUT_S}s",
            )

        if proc.returncode != 0:
            tail = stderr_b.decode("utf-8", errors="replace").strip()[-500:]
            return _persist_failure(
                identity_path,
                error=f"claude exited {proc.returncode}: {tail}",
            )

        raw = stdout_b.decode("utf-8", errors="replace").strip()
        return _persist_identity(identity_path, kind, lib_id, raw)
    finally:
        # Always wipe the bootstrap JSONL — success or failure — so it never
        # appears in the orchestrator session list.
        _delete_bootstrap_jsonl(bootstrap_sid, cwd_path)


# ── helpers ───────────────────────────────────────────────────────


def _persist_identity(identity_path: Path, kind: str, lib_id: str, raw_text: str) -> dict:
    """Parse a model reply (``ICON:`` header + body), write identity.md + icon,
    return the standard envelope. Shared by the interactive + programmatic paths."""
    icon, body = _parse_output(raw_text)
    if not body:
        return _persist_failure(
            identity_path,
            error=f"claude returned empty identity body (raw: {raw_text[:200]!r})",
        )
    try:
        identity_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return _persist_failure(identity_path, error=f"failed to write identity.md: {exc}")
    if icon:
        try:
            _prompts.write_icon(kind, lib_id, icon)
        except (OSError, ValueError) as exc:
            # Non-fatal: identity body is on disk; just log and skip the icon.
            print(f"[agent_identity_generator] write_icon failed: {exc}")
            icon = None
    return {"ok": True, "identity": body, "icon": icon, "error": None}


def _resolve_claude_bin() -> str:
    if Path(CLAUDE_BIN_DEFAULT).exists():
        return CLAUDE_BIN_DEFAULT
    return shutil.which("claude") or CLAUDE_BIN_DEFAULT


def _build_meta_prompt(*, kind: str, lib_id: str, cwd: Path) -> str:
    """Compose the user-message sent to ``claude -p`` for identity gen.

    Inspects cwd inline — claude will Read the listed files and `ls` the dir
    via its own tools. Polish-friendly tone since the user mainly works in PL.
    """
    # Best-effort top-level dir listing — keeps the prompt grounded even
    # before claude calls its own tools. ≤30 entries to bound prompt size.
    try:
        entries = []
        for entry in sorted(cwd.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            kind_label = "dir/" if entry.is_dir() else "file"
            entries.append(f"  {kind_label} {entry.name}")
            if len(entries) >= MAX_DIR_ENTRIES:
                entries.append(f"  ... (truncated at {MAX_DIR_ENTRIES})")
                break
        listing = "\n".join(entries) if entries else "  (empty)"
    except OSError as exc:
        listing = f"  (cannot list cwd: {exc})"

    return f"""You are bootstrapping the **identity prompt** for a per-PARA-item agent on a self-hosted server. The agent runs with cwd `{cwd}` and serves the user's `{kind}/{lib_id}` item from his PARA directory.

Your job (one-shot — no follow-up turns): inspect the working directory and produce a short identity prompt that future turns of this agent will be appended to.

Inspect (use Read / Bash tools as needed):

- README.md, INDEX.md, AGENTS.md, CLAUDE.md if present at the cwd root
- package.json, pyproject.toml, Cargo.toml, go.mod, requirements.txt — whichever ecosystem markers exist
- Top-level directory layout (don't recurse — one level is enough)

Top-level entries already detected:

{listing}

Output (plain text — NO JSON envelope, no code fences around the whole reply):

ICON: <one emoji that represents the project>

<markdown identity prompt body — addressed to the future agent in second person ("Jesteś agentem…"). ≤500 words. Polish if the project's docs are in Polish, otherwise English. Cover:

- One-line "you are the agent for X" preamble
- Project purpose (2-3 lines)
- Key directories / files the agent should know about
- Conventions / scripts / tooling (npm test, uv run, makefile targets…)
- Any known gotchas or pitfalls from the README>

Pick exactly ONE emoji on the ICON line — must render as a single grapheme cluster (a flag, a tool icon, a thematic emoji like 🌱 for plants, 🤖 for AI, etc). Do NOT wrap the emoji in quotes or backticks.

Begin your response with the literal `ICON:` header."""


def _parse_output(raw: str) -> tuple[str | None, str]:
    """Split claude's reply into (icon, identity_body).

    Tolerant: a missing ICON header just yields ``(None, raw)`` so the caller
    still gets something useful even if claude forgot the header. A multi-line
    "ICON:" claim is normalized to its first non-whitespace token.
    """
    if not raw:
        return None, ""

    match = _ICON_HEADER_RE.search(raw)
    if not match:
        return None, raw.strip()

    icon_raw = match.group(1).strip()
    # Take only the first whitespace-delimited token; some models emit
    # "ICON: 🌱 (a sprout)" — we want only the emoji.
    icon = icon_raw.split()[0] if icon_raw else None
    if icon and (icon.startswith("`") or icon.startswith('"')):
        # Strip stray decoration claude might add despite the instruction.
        icon = icon.strip("`\"' ")
    if not icon:
        icon = None

    # Body is everything after the ICON line (consume one trailing blank line).
    body = (raw[: match.start()] + raw[match.end():]).strip()
    # Drop leading blank lines that follow the consumed header.
    body = body.lstrip("\n")
    return icon, body


def _persist_failure(identity_path: Path, *, error: str) -> dict[str, Any]:
    """Surface a generation failure WITHOUT clobbering an existing identity.

    First-time generation (file absent) writes an empty marker so
    ``prompts_for_session`` stays a sound check ("zero-byte → skip"). But
    when an existing identity is being REGENERATED and claude times out /
    crashes / returns malformed output, we must preserve the user's current
    prompt — a transient failure should never erase a working agent.
    Returns the existing on-disk identity so the UI can still display it
    while showing the regen error.
    """
    existing = ""
    if identity_path.is_file():
        try:
            existing = identity_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    try:
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        # Only write the empty marker for first-time generations. If we
        # already have non-empty content on disk, leave it alone.
        if not existing:
            identity_path.write_text("", encoding="utf-8")
    except OSError as exc:
        # Even the marker write failed — surface the original AND this error.
        error = f"{error}; also: failed to write empty identity.md: {exc}"
    return {"ok": False, "identity": existing, "icon": None, "error": error}


def _delete_bootstrap_jsonl(session_id: str, cwd: Path) -> None:
    """Best-effort cleanup of the ephemeral identity-gen transcript
    (programmatic rollback path). Delegates to the canonical helper, which uses
    claude's real slug rule (``/``, ``_``, ``.`` → ``-``)."""
    from . import orchestrator_oneshot as _oneshot
    _oneshot.delete_bootstrap_jsonl(session_id, cwd)


async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM with a short grace, then SIGKILL — used after a wait_for timeout."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            return
        try:
            await proc.wait()
        except Exception:
            pass
