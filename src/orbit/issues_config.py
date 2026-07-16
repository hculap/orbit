"""Issues feature flags — `issues:` block of `config/override.yaml`.

The per-repo GitHub Issues VIEW (list/get/create/edit/close/comment) ships
unconditionally and is unaffected by these flags. They only gate the two
cross-cutting extras layered on top:

  - ``make_task``:   the "promote an issue onto the global Tasks board" endpoint
                     + button. Also requires a configured ``tasks:`` board.
  - ``create_repo``: the one-click "create a GitHub repo" path for a project/area
                     that doesn't have a remote yet.

Both default OFF (opt-in). Rollback = flip the flag false (Level 1), or delete
the gated routes/UI (Level 2/3). Pure parsing, no network; always safe to read.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config as overrides_mod


@dataclass(frozen=True)
class IssuesConfig:
    make_task: bool
    create_repo: bool


def load() -> IssuesConfig:
    overrides = overrides_mod.load_overrides()
    raw = overrides.get("issues") if isinstance(overrides, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return IssuesConfig(
        make_task=bool(raw.get("make_task", False)),
        create_repo=bool(raw.get("create_repo", False)),
    )
