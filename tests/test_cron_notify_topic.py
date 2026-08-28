"""Which Telegram topic a cron run lands in.

Before this split, every run — a nightly report that succeeded and a cert
renewal that failed — published to the single topic ``cron``, so the alert
channel doubled as a results feed. Failures now keep ``cron``; ordinary runs go
to ``cron-out``, and an explicit per-job ``notify.topic`` still overrides both
(that field already existed and was simply never used).
"""
from __future__ import annotations

import asyncio

import pytest

from orbit import cron_scheduler as cs


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def captured(monkeypatch):
    """Swap the notify primitive for a recorder."""
    calls: list[dict] = []

    async def _fake_notify(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    import orbit.notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", _fake_notify)
    return calls


def _job(**over):
    job = {"id": "job-1", "name": "nightly report"}
    job.update(over)
    return job


def _run_row(status="ok"):
    return {"run_id": "r-1", "status": status, "exit_code": 0, "duration_ms": 12}


def test_failed_run_stays_in_cron(captured):
    _run(cs._maybe_notify_run(_job(notify={"on": ["all"]}), _run_row("failed")))
    assert captured[0]["topic"] == "cron"


def test_ok_run_goes_to_cron_out(captured):
    _run(cs._maybe_notify_run(_job(notify={"on": ["all"]}), _run_row("ok")))
    assert captured[0]["topic"] == "cron-out"


def test_skipped_run_goes_to_cron_out(captured):
    _run(cs._maybe_notify_run(_job(notify={"on": ["all"]}), _run_row("skipped")))
    assert captured[0]["topic"] == "cron-out"


def test_explicit_topic_still_wins_on_failure(captured):
    """An operator who named a topic meant it — the split must not hijack it."""
    _run(cs._maybe_notify_run(
        _job(notify={"on": ["all"], "topic": "garden"}), _run_row("failed")))
    assert captured[0]["topic"] == "garden"


def test_explicit_topic_still_wins_on_success(captured):
    _run(cs._maybe_notify_run(
        _job(notify={"on": ["all"], "topic": "garden"}), _run_row("ok")))
    assert captured[0]["topic"] == "garden"


def test_legacy_job_without_notify_field_only_fires_on_failure(captured):
    """The default is unchanged: no `notify` block ⇒ failures only, in `cron`."""
    _run(cs._maybe_notify_run(_job(), _run_row("ok")))
    assert captured == []
    _run(cs._maybe_notify_run(_job(), _run_row("failed")))
    assert len(captured) == 1
    assert captured[0]["topic"] == "cron"


def test_cron_out_is_a_known_topic():
    """Settings must be able to route + mute it like any other topic."""
    from orbit import notify as notify_mod
    from orbit import notify_topics

    assert "cron-out" in notify_topics.KNOWN_TOPICS
    assert "cron-out" in notify_mod._TOPIC_EMOJI
