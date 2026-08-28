"""The :class:`SessionEventHub` read accessors (and that they stay pure reads).

``subscriber_count`` / ``live_session_ids`` were added for the geolocation
round trip: they turn "is anyone actually looking at this?" into a sub-
millisecond answer, so an agent fast-fails with ``no_browser`` instead of
blocking for the full 180 s TTL against an empty room. That makes them a
correctness dependency, not a convenience — a count that lies by one is an
agent hanging for three minutes.

Everything runs inside ``asyncio.run`` (this repo has no ``pytest-asyncio``)
because the hub's queues belong to a live loop.
"""
from __future__ import annotations

import asyncio

from orbit.orchestrator_events import SessionEventHub

SID = "00000000-0000-0000-0000-000000000000"
SID2 = "11111111-1111-1111-1111-111111111111"


def _run(coro):
    return asyncio.run(coro)


# ── subscriber_count ──────────────────────────────────────────────


def test_subscriber_count_is_zero_for_an_unknown_session() -> None:
    async def scenario():
        return SessionEventHub().subscriber_count(SID)

    assert _run(scenario()) == 0


def test_subscriber_count_tracks_subscribe_and_unsubscribe() -> None:
    async def scenario():
        hub = SessionEventHub()
        q1 = hub.subscribe(SID)
        first = hub.subscriber_count(SID)
        q2 = hub.subscribe(SID)
        second = hub.subscriber_count(SID)
        hub.unsubscribe(SID, q1)
        third = hub.subscriber_count(SID)
        hub.unsubscribe(SID, q2)
        return first, second, third, hub.subscriber_count(SID)

    assert _run(scenario()) == (1, 2, 1, 0)


def test_subscriber_count_is_per_session() -> None:
    async def scenario():
        hub = SessionEventHub()
        hub.subscribe(SID)
        return hub.subscriber_count(SID), hub.subscriber_count(SID2)

    assert _run(scenario()) == (1, 0)


def test_subscriber_count_does_not_create_state() -> None:
    """A pure read: asking about a stranger must not register the stranger.

    Otherwise ``live_session_ids`` would start advertising phantom browsers and
    the geo fan-out would target tabs that do not exist.
    """
    async def scenario():
        hub = SessionEventHub()
        hub.subscriber_count("nobody")
        return hub.live_session_ids(), dict(hub._subscribers)

    live, subscribers = _run(scenario())
    assert live == []
    assert subscribers == {}


# ── live_session_ids ──────────────────────────────────────────────


def test_live_session_ids_is_empty_on_a_fresh_hub() -> None:
    async def scenario():
        return SessionEventHub().live_session_ids()

    assert _run(scenario()) == []


def test_live_session_ids_lists_every_attached_session() -> None:
    async def scenario():
        hub = SessionEventHub()
        hub.subscribe(SID)
        hub.subscribe(SID2)
        return sorted(hub.live_session_ids())

    assert _run(scenario()) == [SID, SID2]


def test_live_session_ids_drops_a_session_whose_last_subscriber_left() -> None:
    async def scenario():
        hub = SessionEventHub()
        q_a = hub.subscribe(SID)
        q_b = hub.subscribe(SID2)
        hub.unsubscribe(SID, q_a)
        after_one = list(hub.live_session_ids())
        hub.unsubscribe(SID2, q_b)
        return after_one, list(hub.live_session_ids())

    after_one, after_all = _run(scenario())
    assert after_one == [SID2]
    assert after_all == []


def test_live_session_ids_keeps_a_session_with_a_second_tab_open() -> None:
    async def scenario():
        hub = SessionEventHub()
        q1 = hub.subscribe(SID)
        hub.subscribe(SID)
        hub.unsubscribe(SID, q1)
        return hub.live_session_ids(), hub.subscriber_count(SID)

    assert _run(scenario()) == ([SID], 1)


# ── the accessors change nothing about publish/subscribe ──────────


def test_publish_still_reaches_subscribers_and_leaves_counts_alone() -> None:
    async def scenario():
        hub = SessionEventHub()
        queue = hub.subscribe(SID)
        hub.publish(SID, "geo_request", {"request_id": "abc"})
        before = hub.subscriber_count(SID)
        frame = queue.get_nowait()
        return frame, before, hub.subscriber_count(SID)

    frame, before, after = _run(scenario())
    assert b"event: geo_request" in frame
    assert b'"request_id": "abc"' in frame
    assert (before, after) == (1, 1)


def test_publish_to_a_session_with_no_subscribers_does_not_make_it_live() -> None:
    """A buffered event must not make an empty session look attached.

    ``publish`` creates a replay deque; ``live_session_ids`` reads the
    SUBSCRIBER map, so the two must not be conflated — a geo fan-out onto a
    buffer-only session would silently never be answered.
    """
    async def scenario():
        hub = SessionEventHub()
        hub.publish(SID, "geo_request", {"request_id": "abc"})
        return hub.live_session_ids(), hub.subscriber_count(SID)

    assert _run(scenario()) == ([], 0)
