"""Cron action runner — execute one fire (LLM or shell).

Two execution strategies, dispatched by ``job.action.mode``:

- ``llm``  → spawn a ``claude -p`` subprocess (mirrors
  :mod:`agent_identity_generator`). When the destination is a real session
  we ALSO inject the prompt into that session so the user can read it in
  the chat UI; ``destination.mode == "none"`` runs a fully isolated
  subprocess + cleans the JSONL after.
- ``shell`` → ``asyncio.create_subprocess_shell`` against the user's
  current shell with no sandbox (matches the existing claude-cli bash tool
  surface — see scheduler plan, Risks #3).

Destination resolution:
- ``fresh``    — create a new session via ``orchestrator._create_session_handler``
- ``rolling``  — reuse ``job.destination.rolling_session_id``; create + persist
  on first fire (or when the previous SID was deleted)
- ``existing`` — reuse a pre-picked SID; downgrade to ``none`` (with a warn)
  when the session was deleted out from under us
- ``none``     — fire-and-forget; no chat injection, only run logs survive
"""
from __future__ import annotations
import asyncio
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from . import agent_prompts as agent_prompts_mod
from . import cron_store as store
from .public_url import public_link
from . import orchestrator_env as env_mod
from . import orchestrator_jsonl as jsonl_mod
from . import orchestrator_meta as meta_mod
from . import skills_per_agent as skills_per_agent_mod
from .discovery import AREAS, HOME, PROJECTS, RESOURCES

CLAUDE_BIN_DEFAULT: str = "/usr/bin/claude"
SUBPROCESS_BUFFER_LIMIT: int = 2 * 1024 * 1024
LLM_TIMEOUT_S: float = 600.0
SHELL_TIMEOUT_S: float = 600.0
STDERR_TAIL_BYTES: int = 8 * 1024
OUTPUT_MAX_BYTES: int = 64 * 1024


def _warn(msg: str) -> None:
    print(f"[cron_runner] {msg}", file=sys.stderr)


# ── public entrypoint ───────────────────────────────────────────


async def run_action(job: dict, run_id: str, manual: bool = False) -> dict:
    """Run one fire of ``job``. Returns the run-record patch dict.

    Shape: ``{status, exit_code, stderr_tail, output, session_id, error,
    started_at, finished_at, duration_ms}``. Caller (cron_scheduler._fire_job)
    is responsible for stamping ``run_id`` + ``trigger`` and persisting.
    """
    started_at = time.time()
    action = job.get("action") or {}
    mode = action.get("mode")
    try:
        if mode == "llm":
            payload = await _run_llm(job, run_id)
        elif mode == "shell":
            payload = await _run_shell(job, run_id)
        else:
            payload = _failure_payload(
                started_at, error=f"unknown action.mode: {mode!r}"
            )
    except Exception as exc:
        _warn(f"run_action({job.get('id')!r}) crashed: {exc}")
        payload = _failure_payload(started_at, error=f"runner crashed: {exc}")
    payload.setdefault("started_at", started_at)
    payload.setdefault("finished_at", time.time())
    if payload.get("duration_ms") is None:
        payload["duration_ms"] = int(
            (float(payload["finished_at"]) - float(payload["started_at"])) * 1000
        )
    return payload


# ── LLM mode ────────────────────────────────────────────────────


async def _run_llm(job: dict, run_id: str) -> dict:
    started_at = time.time()
    action = job.get("action") or {}
    prompt = action.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _failure_payload(started_at, error="action.prompt required for llm mode")

    session_id = await _resolve_destination(job, run_id)
    if session_id is None:
        payload = await _run_llm_isolated_dispatch(job, run_id, started_at)
        # Telegram destination: forward the LLM output to the bot.
        if (job.get("destination") or {}).get("mode") == "telegram":
            await _push_run_to_telegram(job, run_id, payload)
        return payload
    return await _run_llm_via_session(job, run_id, session_id, started_at)


async def _run_llm_isolated_dispatch(job: dict, run_id: str, started_at: float) -> dict:
    """Pick the isolated-LLM execution path based on the global setting.

    ``cron_runner_mode == "interactive"`` routes through the tmux pool
    (subscription billing); anything else uses the legacy ``claude -p``
    standalone path (programmatic / API billing).

    Imported lazily to avoid pulling the settings module onto cron's hot
    boot path before its actually needed.
    """
    try:
        from . import orchestrator_settings as _settings_mod
        mode = _settings_mod.resolve_runner_mode("cron_runner_mode")
    except Exception:  # noqa: BLE001 — settings unavailable → subscription default
        # Default to "interactive" (subscription) for consistency with the other
        # 4 one-shot sites. The legacy "programmatic" default here would have
        # silently routed onto the `-p` credit pool the migration exists to avoid.
        mode = "interactive"
    if mode == "interactive":
        return await _run_llm_isolated_interactive(job, run_id, started_at)
    return await _run_llm_isolated(job, run_id, started_at)


async def _run_llm_isolated_interactive(job: dict, run_id: str, started_at: float) -> dict:
    """Run an isolated cron LLM fire through the tmux pool via ``run_oneshot``.

    Subscription-billed (interactive), each fire keyed by a fresh UUID for an
    independent context; the slot is released + its JSONL deleted after.

    NO automatic fallback to ``claude -p`` — that would route onto the credit
    pool, silently defeating the migration. A broken interactive path surfaces
    as a FAILED run (operator sees it) and the documented rollback is to flip
    ``cron_runner_mode`` to ``programmatic``.
    """
    action = job.get("action") or {}
    prompt = (action.get("prompt") or "").strip()
    cwd = _resolve_action_cwd(action)
    lib_id = _action_lib_id(action)
    # NOTE: no auto-fallback to `-p` on failure — that would route onto the
    # credit pool. The rollback is the `cron_runner_mode` flag (operator sees
    # a failed run and flips to programmatic if the interactive path is broken).
    from . import orchestrator_oneshot as oneshot_mod
    append_paths: list = []
    try:
        append_paths = agent_prompts_mod.prompts_for_session(str(cwd) if cwd else None, lib_id)
    except Exception as exc:  # noqa: BLE001
        _warn(f"prompts_for_session failed: {exc}")
    res = await oneshot_mod.run_oneshot(
        prompt,
        cwd=Path(cwd) if cwd is not None else None,
        model=action.get("model") or None,
        append_system_prompt_paths=append_paths,
        agent_skills_dir=_build_skills_farm(action, cwd),
        timeout_s=LLM_TIMEOUT_S,
        # A cron turn that did its work via tools and produced no prose is a
        # SUCCESS — don't let bool(text) stamp a working job as FAILED.
        require_text=False,
        label="cron-llm",
    )
    finished_at = time.time()
    status = "ok" if res["ok"] else "failed"
    if not res["ok"]:
        _warn(f"cron interactive fire failed sid job={job.get('id')!r}: {res['error']}")
    return {
        "status": status,
        "exit_code": 0 if status == "ok" else None,
        "stderr_tail": "",
        "output": (res["text"] or "")[:OUTPUT_MAX_BYTES],
        "session_id": None,
        "error": res["error"],
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((finished_at - started_at) * 1000),
    }



async def _run_llm_isolated(job: dict, run_id: str, started_at: float) -> dict:
    """Spawn ``claude -p`` with a forced session id; delete the JSONL after.

    Resolves the agent's 4-layer prompt stack (general.md / orchestrator.md
    / identity.md / custom.md) via :func:`agent_prompts.prompts_for_session`
    and emits one ``--append-system-prompt-file`` flag per layer so isolated
    cron fires get the same context as a regular orchestrator turn. This
    mirrors what ``orchestrator_runner.build_args`` does for the in-app
    sessions; we don't reuse that helper directly because isolated runs
    use ``--output-format text`` (not stream-json) and force the session id.
    """
    action = job.get("action") or {}
    prompt = action.get("prompt") or ""
    cwd = _resolve_action_cwd(action)
    lib_id = _action_lib_id(action)
    bootstrap_sid = str(uuid.uuid4())
    args = [
        _resolve_claude_bin(),
        "-p",
        prompt,
        "--session-id",
        bootstrap_sid,
        "--output-format",
        "text",
        "--permission-mode",
        "auto",
        "--add-dir",
        str(Path.home() / ".claude"),
    ]
    # Optional per-job model override (e.g. haiku for cheap recurring jobs
    # that don't need opus). None / empty falls back to claude-cli's
    # default. Validated upstream in cron_routes._validate_action.
    model = action.get("model")
    if isinstance(model, str) and model.strip():
        args.extend(["--model", model.strip()])
    # Per-agent prompt stack (general → orchestrator → identity → custom).
    # Tolerate missing module / resolution failure — the cron still runs,
    # just without the agent-specific context (matches the isolated path's
    # original behaviour as a graceful degradation).
    try:
        from . import agent_prompts as _prompts_mod
        for path in _prompts_mod.prompts_for_session(str(cwd) if cwd else None, lib_id):
            args.extend(["--append-system-prompt-file", str(path)])
    except Exception as exc:  # noqa: BLE001 — never block a fire on prompt resolution
        print(f"[cron_runner] prompts_for_session failed: {exc}")
    farm_dir = _build_skills_farm(action, cwd)
    if farm_dir is not None:
        args.extend(["--add-dir", str(farm_dir)])
    env = env_mod.scrubbed_env({"CLAUDE_CONFIG_DIR": str(Path.home() / ".claude")})
    # Layer scope-local .env (Areas/Projects) onto subprocess env so secrets
    # managed in the dashboard reach cron-fired claude turns. Matches the
    # interactive orchestrator runner's behaviour (orchestrator_runner.py).
    if cwd is not None:
        cwd_path = Path(cwd)
        scope_env = cwd_path / ".env"
        if cwd_path != Path.home() and scope_env.is_file():
            try:
                from . import secrets_manager as _sm
                scope_values, _ = _sm.parse_env(scope_env)
                # Re-scrub: a user .env must not reintroduce ANTHROPIC_API_KEY.
                env = env_mod.scrubbed_env(scope_values, base=env)
            except Exception as exc:  # noqa: BLE001
                print(f"[cron_runner] failed to load {scope_env}: {exc}")
    env_mod.log_billing_path("cron-llm", interactive=False)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=SUBPROCESS_BUFFER_LIMIT,
        )
    except FileNotFoundError:
        return _failure_payload(started_at, error="claude binary not found")
    except OSError as exc:
        return _failure_payload(started_at, error=f"spawn failed: {exc}")
    try:
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=LLM_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            await _kill_proc(proc)
            return _failure_payload(
                started_at,
                error=f"llm fire timed out after {LLM_TIMEOUT_S}s",
            )
    finally:
        _delete_bootstrap_jsonl(bootstrap_sid, cwd)

    output = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    finished_at = time.time()
    if proc.returncode != 0:
        return {
            "status": "failed",
            "exit_code": proc.returncode,
            "stderr_tail": stderr[-STDERR_TAIL_BYTES:],
            "output": output[:OUTPUT_MAX_BYTES],
            "session_id": None,
            "error": f"claude exited {proc.returncode}",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": int((finished_at - started_at) * 1000),
        }
    return {
        "status": "ok",
        "exit_code": 0,
        "stderr_tail": stderr[-STDERR_TAIL_BYTES:],
        "output": output[:OUTPUT_MAX_BYTES],
        "session_id": None,
        "error": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((finished_at - started_at) * 1000),
    }


async def _run_llm_via_session(
    job: dict,
    run_id: str,
    session_id: str,
    started_at: float,
) -> dict:
    """Inject the prompt into ``session_id`` via the orchestrator post-message handler.

    Imported lazily to avoid circular imports — orchestrator pulls in many
    of our modules and we don't want cron_runner on its boot path.
    """
    from . import orchestrator as orch_mod  # local: avoid circular

    action = job.get("action") or {}
    prompt = (action.get("prompt") or "").strip()
    payload = {"text": prompt}
    try:
        result = await orch_mod._post_message_handler(session_id, payload)
    except Exception as exc:
        finished_at = time.time()
        return {
            "status": "failed",
            "exit_code": None,
            "stderr_tail": "",
            "output": "",
            "session_id": session_id,
            "error": f"post_message_handler raised: {exc}",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": int((finished_at - started_at) * 1000),
        }
    finished_at = time.time()
    if not isinstance(result, dict) or not result.get("ok"):
        err = result.get("error") if isinstance(result, dict) else "unknown"
        return {
            "status": "failed",
            "exit_code": None,
            "stderr_tail": "",
            "output": "",
            "session_id": session_id,
            "error": f"post_message_handler refused: {err}",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": int((finished_at - started_at) * 1000),
        }
    return {
        "status": "ok",
        "exit_code": 0,
        "stderr_tail": "",
        "output": "",
        "session_id": session_id,
        "error": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((finished_at - started_at) * 1000),
    }


# ── shell mode ──────────────────────────────────────────────────


async def _run_shell(job: dict, run_id: str) -> dict:
    started_at = time.time()
    action = job.get("action") or {}
    command = action.get("command")
    if not isinstance(command, str) or not command.strip():
        return _failure_payload(started_at, error="action.command required for shell mode")

    cwd = _resolve_action_cwd(action)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=SUBPROCESS_BUFFER_LIMIT,
        )
    except OSError as exc:
        return _failure_payload(started_at, error=f"shell spawn failed: {exc}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SHELL_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        await _kill_proc(proc)
        return _failure_payload(
            started_at, error=f"shell command timed out after {SHELL_TIMEOUT_S}s"
        )

    output = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    finished_at = time.time()
    status = "ok" if proc.returncode == 0 else "failed"
    payload: dict[str, Any] = {
        "status": status,
        "exit_code": proc.returncode,
        "stderr_tail": stderr[-STDERR_TAIL_BYTES:],
        "output": output[:OUTPUT_MAX_BYTES],
        "session_id": None,
        "error": None if status == "ok" else f"shell exited {proc.returncode}",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((finished_at - started_at) * 1000),
    }

    dest_session = await _resolve_destination(job, run_id)
    if dest_session is not None:
        await _inject_shell_output(job, run_id, dest_session, payload)
        payload["session_id"] = dest_session
    elif (job.get("destination") or {}).get("mode") == "telegram":
        await _push_run_to_telegram(job, run_id, payload)
    return payload


async def _push_run_to_telegram(job: dict, run_id: str, payload: dict) -> None:
    """Forward a complete run (success OR failure) to the Telegram bot.

    Distinct from ``cron_alerts.report_failure`` (which fires only on
    failure as a short alert) and from ``cron_scheduler._maybe_notify_run``
    (which gates by the per-job ``notify_on`` filter). This path is the
    primary output channel when the user picked ``destination.mode=telegram``
    — they get the full run output as the message (or attached as a file
    when long).

    Best-effort: never raises. Misconfigured TELEGRAM_* creds → soft
    fail with a stderr warn.
    """
    try:
        from . import notify as notify_mod  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        _warn(f"telegram destination: notify module unavailable: {exc}")
        return
    notify_fn = getattr(notify_mod, "notify", None)
    if not callable(notify_fn):
        _warn("telegram destination: notify.notify is not callable")
        return

    name = job.get("name") or job.get("id") or "cron"
    job_id = job.get("id") or "cron"
    status = payload.get("status") or "?"
    exit_code = payload.get("exit_code")
    duration_ms = payload.get("duration_ms")
    if not isinstance(duration_ms, (int, float)):
        duration_label = "—"
    elif duration_ms < 100:
        duration_label = "instant"
    elif duration_ms < 1000:
        duration_label = f"{int(duration_ms)}ms"
    else:
        duration_label = f"{duration_ms / 1000:.1f}s"
    output = (payload.get("output") or "").rstrip()
    stderr_tail = (payload.get("stderr_tail") or "").rstrip()

    title_prefix = "✓" if status == "ok" else "✗"
    title = f"{title_prefix} {name}"
    summary = f"status={status} · exit={exit_code} · {duration_label}"

    # Long output → ship as attached .txt so the message stays readable.
    # Short output → embed as a <pre> code block (notify's `code` param)
    # so columns / alignment in shell tools (`df -h`, `ps`, …) survive.
    LONG_OUTPUT_THRESHOLD = 1500
    attach_path: str | None = None
    code_block: str | None = None
    if len(output) > LONG_OUTPUT_THRESHOLD:
        attach_path = f"/tmp/cron-{job_id}-{run_id[:8]}.txt"
        try:
            with open(attach_path, "w", encoding="utf-8") as fh:
                fh.write(f"# {name} ({job_id}) — run {run_id}\n")
                fh.write(f"status={status} exit={exit_code} duration_ms={duration_ms}\n\n")
                fh.write("--- stdout ---\n")
                fh.write(output)
                if stderr_tail:
                    fh.write("\n\n--- stderr (tail) ---\n")
                    fh.write(stderr_tail)
        except OSError as exc:
            _warn(f"telegram destination: writing attach failed: {exc}")
            attach_path = None
            summary += "\noutput truncated (couldn't write tmp file)"
    else:
        # Build a single code block: stderr tail (if failure) then stdout.
        chunks: list[str] = []
        if stderr_tail and status != "ok":
            chunks.append("[stderr]\n" + "\n".join(stderr_tail.splitlines()[-5:]))
        if output:
            chunks.append(output)
        if chunks:
            code_block = "\n\n".join(chunks)

    priority = 5 if status == "failed" else 3
    try:
        await notify_fn(
            topic="cron",
            title=title,
            message=summary,
            code=code_block,
            priority=priority,
            tags=[status],
            click=public_link(f"/scheduler/{job_id}"),
            attach=attach_path,
        )
    except Exception as exc:  # noqa: BLE001
        _warn(f"telegram destination: notify call failed: {exc}")


async def _inject_shell_output(
    job: dict,
    run_id: str,
    session_id: str,
    payload: dict,
) -> None:
    """Wrap shell output as a chat turn and post it to the destination session.

    Best-effort: any error becomes a stderr warn — shell command exit code
    remains the source of truth for run status.
    """
    from . import orchestrator as orch_mod  # local: avoid circular

    name = job.get("name") or job.get("id") or "cron job"
    body = (
        f"## Cron run · {name}\n\n"
        f"`exit_code={payload.get('exit_code')}` · "
        f"duration={payload.get('duration_ms')}ms · run_id=`{run_id}`\n\n"
        f"### stdout\n```\n{(payload.get('output') or '')[:OUTPUT_MAX_BYTES]}\n```\n"
    )
    if payload.get("stderr_tail"):
        body += f"\n### stderr (tail)\n```\n{payload['stderr_tail']}\n```\n"
    try:
        await orch_mod._post_message_handler(session_id, {"text": body})
    except Exception as exc:
        _warn(f"shell-output inject failed for {job.get('id')!r}: {exc}")


# ── destination resolution ──────────────────────────────────────


async def _resolve_destination(job: dict, run_id: str) -> str | None:
    """Return the target session id or None ("fire-and-forget").

    See module docstring for per-mode semantics. Mutates ``jobs.json`` in
    the rolling-with-empty-or-deleted case to persist a freshly-created sid.
    """
    dest = job.get("destination") or {}
    mode = dest.get("mode")
    if mode in (None, "none"):
        return None
    if mode == "existing":
        sid = dest.get("session_id")
        if isinstance(sid, str) and sid and _session_alive(sid):
            return sid
        _warn(
            f"destination.existing for job {job.get('id')!r}: "
            f"session {sid!r} missing — falling back to none"
        )
        return None
    if mode == "rolling":
        sid = dest.get("rolling_session_id")
        if isinstance(sid, str) and sid and _session_alive(sid):
            return sid
        new_sid = await _create_destination_session(job, dest, run_id)
        if new_sid is None:
            return None
        merged_dest = {**dest, "rolling_session_id": new_sid}
        try:
            await store.patch_job(job["id"], {"destination": merged_dest})
        except Exception as exc:
            _warn(f"persist rolling_session_id for {job.get('id')!r} failed: {exc}")
        return new_sid
    if mode == "fresh":
        return await _create_destination_session(job, dest, run_id)
    if mode == "telegram":
        # Telegram is a "no chat session" destination handled separately
        # by ``_push_run_to_telegram`` after the run completes. Returning
        # None here makes the runner skip session injection.
        return None
    _warn(f"unknown destination.mode: {mode!r}; treating as none")
    return None


async def _create_destination_session(
    job: dict,
    dest: dict,
    run_id: str,
) -> str | None:
    """Call ``orchestrator._create_session_handler`` to spawn a fresh sid."""
    from . import orchestrator as orch_mod  # local: avoid circular

    agent_str = dest.get("agent")
    cwd_str = _agent_lib_id_to_cwd(agent_str)
    title = _format_session_title(job)
    payload: dict[str, Any] = {"title": title}
    if cwd_str is not None:
        payload["cwd"] = cwd_str
    if isinstance(agent_str, str) and agent_str.strip():
        payload["lib_id"] = agent_str.strip()
    try:
        result = await orch_mod._create_session_handler(payload)
    except Exception as exc:
        _warn(f"_create_session_handler failed for {job.get('id')!r}: {exc}")
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    sid = result.get("id")
    return sid if isinstance(sid, str) and sid else None


def _format_session_title(job: dict) -> str:
    name = job.get("name") or job.get("id") or "Cron job"
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"{name} · {stamp}"


def _session_alive(session_id: str) -> bool:
    """True when the session has either a JSONL transcript OR a sidecar entry.

    Mirrors orchestrator's "session exists" checks: an orphan (sidecar but
    no JSONL) is still a valid post target — first injected turn writes
    the transcript.
    """
    try:
        if jsonl_mod.jsonl_path(session_id).exists():
            return True
    except Exception:
        pass
    try:
        meta = meta_mod.get_meta(session_id)
    except Exception:
        return False
    return bool(meta) and any(meta.get(k) for k in ("title", "cwd", "lib_id"))


# ── helpers ─────────────────────────────────────────────────────


def _agent_lib_id_to_cwd(agent: str | None) -> str | None:
    """Map ``"areas/Home"`` / ``"projects/foo"`` / ``"resources/bar"`` → cwd path.

    ``None`` / empty / ``"global"`` → None (Global session, cwd defaults to HOME).
    Validates the kind segment; unknown kinds fall back to None.
    """
    if not isinstance(agent, str) or not agent.strip():
        return None
    cleaned = agent.strip().strip("/")
    if not cleaned or cleaned.lower() == "global":
        return None
    if "/" not in cleaned:
        return None
    kind, rest = cleaned.split("/", 1)
    rest = rest.strip("/")
    if not rest:
        return None
    root_map = {"areas": AREAS, "projects": PROJECTS, "resources": RESOURCES}
    root = root_map.get(kind)
    if root is None:
        return None
    candidate = (root / Path(rest)).resolve()
    try:
        if candidate.is_dir():
            return str(candidate)
    except OSError:
        return None
    return None


def _resolve_action_cwd(action: dict) -> Path:
    """Pick the cwd a subprocess (LLM or shell) should run in."""
    agent = action.get("agent") if isinstance(action, dict) else None
    cwd_str = _agent_lib_id_to_cwd(agent)
    if cwd_str:
        return Path(cwd_str)
    return HOME


def _action_lib_id(action: dict) -> str | None:
    """Return the ``<kind>/<lib_id>`` form of action.agent, or None for Global.

    ``agent_prompts.prompts_for_session`` accepts ``None`` (Global) or a
    string like ``"projects/my-project"``. Normalises sentinel values
    ("global" / "" / null) to None.
    """
    if not isinstance(action, dict):
        return None
    agent = action.get("agent")
    if not isinstance(agent, str):
        return None
    cleaned = agent.strip()
    if not cleaned or cleaned.lower() in ("global", "null"):
        return None
    return cleaned


def _build_skills_farm(action: dict, cwd: Path) -> Path | None:
    """Build a per-agent skills symlink farm for the LLM spawn (best-effort).

    Mirrors orchestrator._post_message_handler (lines 514-521): resolves
    (kind, lib_id) for the action's agent and rebuilds the farm dir. Failure
    is logged + swallowed — a missing farm shouldn't block a fire.
    """
    agent = action.get("agent") if isinstance(action, dict) else None
    cwd_str = str(cwd) if cwd != HOME else None
    try:
        kind, lib_id = skills_per_agent_mod.resolve_lib_id_from_session(
            cwd_str, agent if isinstance(agent, str) else None
        )
        return skills_per_agent_mod.build_symlink_farm(kind, lib_id)
    except Exception as exc:
        _warn(f"skills farm build failed: {exc}")
        return None


def _resolve_claude_bin() -> str:
    if Path(CLAUDE_BIN_DEFAULT).exists():
        return CLAUDE_BIN_DEFAULT
    return shutil.which("claude") or CLAUDE_BIN_DEFAULT


def _delete_bootstrap_jsonl(session_id: str, cwd: Path) -> None:
    """Wipe the ephemeral isolated-LLM transcript (programmatic rollback path).

    Delegates to the canonical helper which uses claude's real slug rule
    (``/``, ``_``, ``.`` → ``-``); a ``/``-only replace misses dirs with ``_``/``.``.
    """
    from . import orchestrator_oneshot as _oneshot
    _oneshot.delete_bootstrap_jsonl(session_id, cwd)


async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
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


def _failure_payload(started_at: float, *, error: str) -> dict:
    finished_at = time.time()
    return {
        "status": "failed",
        "exit_code": None,
        "stderr_tail": "",
        "output": "",
        "session_id": None,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((finished_at - started_at) * 1000),
    }


# Reserved for cron_alerts: it imports this for the prompt-pretty path.
_ = agent_prompts_mod
