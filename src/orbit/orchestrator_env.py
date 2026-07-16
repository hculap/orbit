"""Subscription-billing env hygiene for every ``claude`` subprocess.

Phase 0 of the subscription-only migration. After 2026-06-15, Claude Code
splits billing: interactive (TTY/tmux) usage draws the Max **subscription**
limit, while ``claude -p`` / Agent-SDK usage draws a separate programmatic
**credit pool** — and an ``ANTHROPIC_API_KEY`` in the env forces raw
pay-as-you-go API billing regardless of mode.

Any code that spawns the ``claude`` binary outside the tmux pool (the legacy
``-p`` programmatic runner, the cron / title / identity / skill one-shots)
MUST build its child env through :func:`scrubbed_env` so a stray
``ANTHROPIC_API_KEY`` can never leak into the child. The tmux interactive
path already enforces this via ``-e ANTHROPIC_API_KEY=`` (orchestrator_tmux
H11); this module is the equivalent guard for the non-tmux spawns + a single
place to log which billing path each spawn took.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Env vars that force programmatic / pay-as-you-go API billing. Stripped from
# every claude child env. Tuple so callers can report exactly what was found.
_BILLING_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

_warned_once = False


def scrubbed_env(
    extra: dict[str, str] | None = None,
    *,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a child env with all billing-forcing keys removed.

    ``base`` defaults to ``os.environ``. ``extra`` is merged AFTER the scrub
    and is itself filtered, so a caller (or a user-managed ``.env`` layered in
    via ``extra``) can never re-introduce a stripped key by accident.
    """
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if k not in _BILLING_ENV_KEYS}
    if extra:
        for k, v in extra.items():
            if k in _BILLING_ENV_KEYS:
                continue
            env[k] = v
    return env


def scope_env_values(cwd: Path | str | None) -> dict[str, str]:
    """Return scrubbed ``<cwd>/.env`` secrets to layer into a child claude turn.

    Centralizes the scope-secret injection the legacy ``-p`` paths inline
    (``cron_runner._run_llm_isolated`` and ``orchestrator_runner.ClaudeRunner``)
    so the interactive tmux one-shot path (:func:`orchestrator_oneshot.run_oneshot`)
    gets the SAME dashboard-managed secrets — without this, flipping the cron /
    one-shot default to the tmux pool silently dropped every ``<scope>/.env``
    secret a cron-fired turn used to receive.

    Returns ``{}`` for ``None`` / HOME / a scope with no ``.env`` / any parse
    error. The values are scrubbed of billing-forcing keys so a user-managed
    ``.env`` can never reintroduce ``ANTHROPIC_API_KEY`` onto a subscription
    (tmux) spawn. Best-effort: a bad ``.env`` logs and yields ``{}`` rather than
    crashing the turn.
    """
    if cwd is None:
        return {}
    try:
        cwd_path = Path(cwd)
        if cwd_path == Path.home():
            return {}
        scope_env = cwd_path / ".env"
        if not scope_env.is_file():
            return {}
        from . import secrets_manager as _sm
        scope_values, _ = _sm.parse_env(scope_env)
        return {k: v for k, v in scope_values.items() if k not in _BILLING_ENV_KEYS}
    except Exception as exc:  # noqa: BLE001 — never let a bad .env crash the turn
        print(f"[orchestrator_env] failed to load scope .env at {cwd}: {exc}", file=sys.stderr)
        return {}


def log_billing_path(label: str, *, interactive: bool) -> None:
    """Emit one line per claude spawn stating its billing path.

    ``interactive=True`` → subscription (tmux); ``False`` → programmatic
    ``-p`` (credit pool / API). Lets prod observability confirm nothing leaks
    onto the credit pool after cutover (grep ``[billing]``).
    """
    mode = "interactive(subscription)" if interactive else "programmatic(-p/credit-pool)"
    print(f"[billing] {label}: {mode}", file=sys.stderr)


def warn_if_api_key_present() -> None:
    """One-shot startup warning if a billing-forcing key is in the server env.

    The key is scrubbed per-spawn, but its mere presence in the systemd unit
    env means a misconfigured future spawn path could leak it — surface it
    loudly once at boot.
    """
    global _warned_once
    if _warned_once:
        return
    _warned_once = True
    present = [k for k in _BILLING_ENV_KEYS if os.environ.get(k)]
    if present:
        print(
            f"[orchestrator_env] WARNING: {', '.join(present)} present in server env; "
            f"scrubbed per-spawn for subscription billing, but remove it from the "
            f"systemd unit to be safe (post-2026-06-15 programmatic credit pool).",
            file=sys.stderr,
        )
