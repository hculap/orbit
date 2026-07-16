"""Pure scope -> filesystem-paths resolver for the env/secrets feature.

Scopes:
    "global"          -> { ~/.env, ~/.secrets/, ~/.ssh/ }
    "areas/<id>"      -> { ~/Areas/<id>/.env, ~/Areas/<id>/.secrets/, None }
    "projects/<id>"   -> { ~/Projects/<id>/.env, ~/Projects/<id>/.secrets/, None }

Reuses the validators from :mod:`library`:
    * ``_NAME_RE`` for syntactic checks
    * ``_safe_area_path`` / ``_safe_project_path`` for path-traversal-safe
      resolution under ``~/Areas`` / ``~/Projects``

SSH paths are only valid for the global scope (per plan section 2).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .discovery import HOME
from .library import _safe_area_path, _safe_project_path

GLOBAL_ENV = HOME / ".env"
GLOBAL_SECRETS_DIR = HOME / ".secrets"
GLOBAL_SSH_DIR = HOME / ".ssh"


@dataclass(frozen=True)
class ScopePaths:
    kind: str
    lib_id: str | None
    env: Path
    secrets_dir: Path
    ssh_dir: Path | None


def parse_scope(scope: str) -> tuple[str, str | None]:
    """Split a scope token into ``(kind, lib_id)``.

    Accepts:
        ``"global"``           -> ``("global", None)``
        ``"areas/<id>"``       -> ``("areas",  "<id>")``
        ``"projects/<id>"``    -> ``("projects", "<id>")``
        ``"projects/<g>/<n>"`` -> ``("projects", "<g>/<n>")`` (group/name form)

    Anything else raises ``ValueError``.
    """
    if not isinstance(scope, str):
        raise ValueError("scope must be a string")
    s = scope.strip().strip("/")
    if not s:
        raise ValueError("scope is required")
    if s == "global":
        return "global", None
    parts = s.split("/", 1)
    if len(parts) != 2 or not parts[1].strip():
        raise ValueError(
            "scope must be 'global', 'areas/<id>' or 'projects/<id>'"
        )
    kind, lib_id = parts[0], parts[1].strip().strip("/")
    if kind not in ("areas", "projects"):
        raise ValueError(
            "scope kind must be 'global', 'areas' or 'projects'"
        )
    if not lib_id:
        raise ValueError("scope lib_id is required")
    return kind, lib_id


def resolve(scope: str) -> ScopePaths:
    """Validate the scope and return the resolved ``ScopePaths``.

    Does NOT check existence — callers are expected to ``.is_dir()`` /
    ``.is_file()`` themselves, since most endpoints want to surface
    "no .env yet" as an empty list rather than 404.
    """
    kind, lib_id = parse_scope(scope)
    if kind == "global":
        return ScopePaths(
            kind="global",
            lib_id=None,
            env=GLOBAL_ENV,
            secrets_dir=GLOBAL_SECRETS_DIR,
            ssh_dir=GLOBAL_SSH_DIR,
        )

    if kind == "areas":
        base = _safe_area_path(lib_id)
    elif kind == "projects":
        base = _safe_project_path(lib_id)
    else:  # pragma: no cover — parse_scope already filtered
        raise ValueError(f"unknown scope kind: {kind}")

    return ScopePaths(
        kind=kind,
        lib_id=lib_id,
        env=base / ".env",
        secrets_dir=base / ".secrets",
        ssh_dir=None,
    )


def require_global(scope_paths: ScopePaths) -> Path:
    """Helper: assert the resolved scope is global, return its ``ssh_dir``.

    Raises ``ValueError`` (HTTP 400 via :func:`library._http_for`) if the
    caller passed a non-global scope to an SSH endpoint.
    """
    if scope_paths.kind != "global" or scope_paths.ssh_dir is None:
        raise ValueError("SSH endpoints are only valid for the global scope")
    return scope_paths.ssh_dir


def has_env(scope_paths: ScopePaths) -> bool:
    return scope_paths.env.is_file()


def has_secrets_dir(scope_paths: ScopePaths) -> bool:
    return scope_paths.secrets_dir.is_dir()


def has_ssh(scope_paths: ScopePaths) -> bool:
    return scope_paths.ssh_dir is not None and scope_paths.ssh_dir.is_dir()
