"""`ensure_labels` must auto-provision EVERY label the dashboard invents.

The dashboard tags each task with `area:<slug>` / `proj:<slug>` — and, when a
task belongs to neither, with a bare `global` (tasks.py: "if not area and not
proj ... labels.append('global')"). `gh` refuses to attach a label that does
not exist, so any label the dashboard mints itself has to be auto-created.

`global` was missing from that set, which made "New task" with no area fail
outright on a repo that had never seen one:

    gh rc=1: could not add label: 'global' not found

Regression guard: the namespaced labels and the bare `global` are created,
labels the *user* typed are still left alone (creating those would silently
paper over a typo instead of surfacing it).

Async is driven with the house `asyncio.run` helper — the suite has no
pytest-asyncio dependency, and `async def test_*` is silently collected but
never awaited without it.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from orbit import tasks_github as tg


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_label_cache():
    tg._repo_labels_cache.clear()
    yield
    tg._repo_labels_cache.clear()


class _FakeGh:
    """Stands in for `_gh_cli`, recording every `label create` it is asked for."""

    def __init__(self, existing: set[str] | None = None):
        self.existing = set(existing or ())
        self.created: list[str] = []

    async def __call__(self, args: list[str], **kwargs):
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": n} for n in sorted(self.existing)])
        if args[:2] == ["label", "create"]:
            name = args[2]
            self.created.append(name)
            self.existing.add(name)
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


def _ensure(monkeypatch, labels, existing=None):
    fake = _FakeGh(existing)
    monkeypatch.setattr(tg, "_gh_cli", fake)
    _run(tg.ensure_labels("owner/repo", labels))
    return fake


def test_creates_bare_global_label(monkeypatch):
    """The regression: a global task on a repo with no `global` label."""
    fake = _ensure(monkeypatch, ["global"])
    assert fake.created == ["global"]


def test_creates_area_and_proj_labels(monkeypatch):
    fake = _ensure(monkeypatch, ["area:dom", "proj:orbit"])
    assert sorted(fake.created) == ["area:dom", "proj:orbit"]


def test_skips_labels_that_already_exist(monkeypatch):
    fake = _ensure(monkeypatch, ["global", "area:dom"], existing={"global", "area:dom"})
    assert fake.created == []


def test_leaves_user_supplied_labels_alone(monkeypatch):
    """`bug` / `p1-must` are the user's own taxonomy — never auto-create them."""
    fake = _ensure(monkeypatch, ["bug", "p1-must", "global"])
    assert fake.created == ["global"]


def test_global_is_matched_exactly(monkeypatch):
    """`globalny` / `Global` are somebody else's labels, not ours."""
    fake = _ensure(monkeypatch, ["globalny", "Global", "global-ish"])
    assert fake.created == []
