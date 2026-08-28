"""Unit tests for the geolocation round-trip core (:mod:`orchestrator_geo`).

Everything here drives the FastAPI-free module directly against a stub hub, so
no test opens a socket, forks a browser or touches ``~/.orchestrator``. The
``fresh_geo`` fixture rebinds every module global (``_PENDING`` / ``_last_fix``
/ the anti-nag stamp / the wait counter) per test — the store-isolation
convention the a2a and terminal-shortcuts suites already use.

Three groups are load-bearing rather than incidental and must not be deleted:

* ``test_every_failure_status_has_retryable_and_hint`` — the agent contract.
  A status row without ``retryable``/``hint`` is how a model starts blind-
  retrying a permission denial.
* the JS/Python **drift guard** — ``static/geo-consent.js`` carries a verbatim
  copy of ``STATUSES`` and the browser error-code map; the two halves
  disagreeing means the agent and the browser mean different things by the same
  word.
* the two PRIVACY assertions — nothing lands on disk, and no latitude or
  longitude reaches a log line.

No ``pytest-asyncio`` in this repo: the async surface is driven through a local
``_run = asyncio.run``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest

from orbit import orchestrator_geo as geo

SID = "00000000-0000-0000-0000-000000000000"
SID2 = "11111111-1111-1111-1111-111111111111"
SID3 = "22222222-2222-2222-2222-222222222222"

# A real-looking Warsaw fix. The digits are deliberately distinctive so the
# "no coordinates in the logs" test can grep for them.
COORDS: dict[str, Any] = {"lat": 52.2296756, "lon": 21.0122287, "accuracy_m": 18.4}

_STATIC = Path(__file__).resolve().parents[1] / "src/orbit/static"


def _run(coro):
    return asyncio.run(coro)


class FakeHub:
    """Minimal stand-in for :class:`SessionEventHub` — the three methods geo uses.

    ``attached`` (subscriber_count) and ``live`` (live_session_ids) are kept
    SEPARATE so a test can model "the asking session has no tab open but
    another one does", which is the whole fan-out branch.
    """

    def __init__(self, *, attached: tuple[str, ...] = (), live: tuple[str, ...] = (),
                 broken: bool = False) -> None:
        self.attached = set(attached)
        self.live = list(live)
        self.published: list[tuple[str, str, dict[str, Any]]] = []
        self.on_publish = None
        self.broken = broken

    def subscriber_count(self, session_id: str) -> int:
        if self.broken:
            raise RuntimeError("hub is down")
        return 1 if session_id in self.attached else 0

    def live_session_ids(self) -> list[str]:
        if self.broken:
            raise RuntimeError("hub is down")
        return list(self.live)

    def publish(self, session_id: str, event: str, data: dict[str, Any]) -> None:
        self.published.append((session_id, event, dict(data)))
        if self.on_publish is not None:
            self.on_publish(session_id, event, data)

    def events(self, name: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [row for row in self.published if row[1] == name]


@pytest.fixture
def fresh_geo(monkeypatch):
    """Rebind every module global so tests can't leak state into each other."""
    monkeypatch.setattr(geo, "_PENDING", {})
    monkeypatch.setattr(geo, "_last_fix", None)
    monkeypatch.setattr(geo, "_last_prompt_at", 0.0)
    monkeypatch.setattr(geo, "_wait_inflight", 0)
    return geo


@pytest.fixture
def hub() -> FakeHub:
    """The common case: the asking session has exactly one tab attached."""
    return FakeHub(attached=(SID,), live=(SID,))


def _create(hub: FakeHub, *, session_id: str = SID, now: float | None = None, **kw: Any):
    """``create_request`` with a monotonic-anchored ``now``.

    A fabricated ``now`` far from ``time.monotonic()`` would put ``expires_at``
    in the real past and every ``await_fix`` would answer ``expired``, so the
    default is anchored on the real clock.
    """
    return geo.create_request(
        session_id=session_id,
        hub=hub,
        now=time.monotonic() if now is None else now,
        **kw,
    )


# ── the contract table (keystone) ─────────────────────────────────


@pytest.mark.parametrize("status", sorted(geo.STATUSES))
def test_every_failure_status_has_retryable_and_hint(status: str) -> None:
    """THE agent-facing contract test.

    Every row must carry a machine-readable ``reason``, a boolean ``retryable``
    and human copy. Only the outright success (``fixed``) is allowed an empty
    hint — everything else has to tell the model what to do INSTEAD of asking
    again, which is the one thing that stops a blind retry loop.
    """
    row = geo.STATUSES[status]
    assert set(row) == {"reason", "retryable", "message", "hint"}, status
    assert isinstance(row["reason"], str) and row["reason"], status
    assert isinstance(row["retryable"], bool), status
    assert isinstance(row["message"], str) and row["message"].strip(), status
    assert isinstance(row["hint"], str), status
    if status != "fixed":
        assert row["hint"].strip(), f"{status} must tell the agent what to do instead"


def test_describe_unknown_status_falls_back_to_not_found() -> None:
    assert geo.describe("nonsense") == geo.STATUSES["not_found"]


def test_describe_returns_a_copy_not_the_table_row() -> None:
    # A caller mutating its payload must not rewrite the contract for everyone.
    row = geo.describe("denied")
    row["retryable"] = True
    assert geo.STATUSES["denied"]["retryable"] is False


def test_retryable_helper_matches_the_table() -> None:
    assert geo.retryable("unavailable") is True
    assert geo.retryable("denied") is False
    assert geo.retryable("nonsense") is False  # unknown → not_found → False


def test_failure_payload_shape() -> None:
    payload = geo.failure("denied", request_id="abc")
    assert payload["ok"] is False
    assert payload["status"] == "denied"
    assert payload["terminal"] is True
    assert payload["reason"] == "permission_denied"
    assert payload["request_id"] == "abc"


def test_failure_extra_can_override_terminal() -> None:
    # rate_limited is the one ok:false row that is NOT the end of the story.
    assert geo.failure("rate_limited", terminal=False)["terminal"] is False


def test_answer_statuses_are_all_known_rows() -> None:
    assert geo.ANSWER_STATUSES <= set(geo.STATUSES)


def test_error_code_status_covers_the_browser_codes() -> None:
    # GeolocationPositionError only ever emits 1/2/3.
    assert geo.ERROR_CODE_STATUS == {1: "denied", 2: "unavailable", 3: "timeout"}
    assert set(geo.ERROR_CODE_STATUS.values()) <= geo.ANSWER_STATUSES


def test_timeout_ladder_does_not_drift() -> None:
    """20 s getCurrentPosition < 45 s poll window (clamp 60) < 180 s TTL."""
    assert geo.GEO_WAIT_TIMEOUT_DEFAULT_S < geo.GEO_WAIT_TIMEOUT_MAX_S
    assert geo.GEO_WAIT_TIMEOUT_MAX_S < geo.GEO_REQUEST_TTL_S
    assert geo.GEO_TTL_MIN_S <= geo.GEO_REQUEST_TTL_S <= geo.GEO_TTL_MAX_S


# ── JS/Python drift guard (keystone) ──────────────────────────────


def _js() -> str:
    return (_STATIC / "geo-consent.js").read_text(encoding="utf-8")


def _js_block(text: str, opener: str, closer: str) -> str:
    start = text.index(opener)
    end = text.index(closer, start)
    return text[start:end]


_JS_ROW = re.compile(
    r"^ {4}(\w+): \{\n"
    r" +reason: '([^']*)',\n"
    r" +retryable: (true|false),\n"
    r" +message: '([^']*)',\n"
    r" +hint: '([^']*)',\n"
    r" +\},",
    re.M,
)


def _js_statuses() -> dict[str, dict[str, Any]]:
    block = _js_block(_js(), "const STATUSES = {", "\n  };")
    return {
        name: {
            "reason": reason,
            "retryable": retryable == "true",
            "message": message,
            "hint": hint,
        }
        for name, reason, retryable, message, hint in _JS_ROW.findall(block)
    }


def test_geo_consent_js_matches_python_statuses() -> None:
    """Two-way drift guard: the browser and the agent must mean the same thing.

    ``static/geo-consent.js`` keeps a verbatim copy of the table because it has
    no import machinery. If the two ever disagree, the browser could answer a
    status the agent classifies differently (e.g. a permission denial the model
    thinks is retryable) — so the copy is pinned character for character.
    """
    js_statuses = _js_statuses()
    assert js_statuses, "STATUSES literal not found / not parseable in geo-consent.js"
    assert set(js_statuses) == set(geo.STATUSES)
    for name, row in geo.STATUSES.items():
        assert js_statuses[name] == row, f"geo-consent.js drifted on '{name}'"


def test_geo_consent_js_matches_python_error_code_map() -> None:
    block = _js_block(_js(), "const ERROR_CODE_STATUS = {", "};")
    parsed = {int(code): status for code, status in re.findall(r"(\d+): '([^']*)'", block)}
    assert parsed == geo.ERROR_CODE_STATUS


def test_geo_consent_js_matches_python_answer_statuses() -> None:
    block = _js_block(_js(), "const ANSWER_STATUSES = [", "];")
    assert set(re.findall(r"'([^']*)'", block)) == set(geo.ANSWER_STATUSES)


def test_geo_consent_js_matches_python_request_id_regex() -> None:
    match = re.search(r"const REQUEST_ID_RE = /(.+?)/;", _js())
    assert match, "REQUEST_ID_RE literal not found in geo-consent.js"
    assert match.group(1) == geo.REQUEST_ID_RE.pattern


def test_geo_consent_js_matches_python_scalar_constants() -> None:
    text = _js()
    assert re.search(r"const REASON_MAX_CHARS = (\d+);", text).group(1) == str(
        geo.REASON_MAX_CHARS
    )
    accuracies = re.search(r"const ACCURACIES = \[([^\]]*)\];", text).group(1)
    assert tuple(re.findall(r"'([^']*)'", accuracies)) == geo.ACCURACIES


# ── ordering invariant: register, THEN publish ────────────────────


def test_request_is_registered_before_it_is_published(fresh_geo, hub) -> None:
    """A phone answering in 40 ms must not 404 against its own request."""
    seen: list[bool] = []
    hub.on_publish = lambda sid, event, data: seen.append(
        data["request_id"] in fresh_geo._PENDING
    )
    _create(hub)
    assert seen == [True]


def test_answer_arriving_inside_the_publish_call_is_accepted(fresh_geo, hub) -> None:
    """The strong form of the ordering invariant: answer DURING publish."""
    answers: list[dict[str, Any]] = []

    def _instant_answer(sid: str, event: str, data: dict[str, Any]) -> None:
        answers.append(fresh_geo.submit_answer(data["request_id"], {"status": "denied"}))

    hub.on_publish = _instant_answer
    created = _create(hub)
    assert answers == [{"ok": True, "accepted": True, "status": "denied"}]
    # The create call still reports `pending` — it was true when it was built —
    # and the very next /wait sees the terminal answer.
    assert created["status"] == "pending"
    assert fresh_geo._PENDING[created["request_id"]].result["status"] == "denied"


def test_create_publishes_the_full_prompt_payload(fresh_geo, hub) -> None:
    created = _create(hub, reason="najbliższa apteka", accuracy="coarse",
                      agent={"lib_id": "areas/Home", "title": "Dom"})
    (_, event, payload), = hub.events(geo.EVENT_REQUEST)
    assert event == geo.EVENT_REQUEST
    assert payload["request_id"] == created["request_id"]
    assert payload["reason"] == "najbliższa apteka"
    assert payload["accuracy"] == "coarse"
    assert payload["agent"] == {"lib_id": "areas/Home", "title": "Dom"}
    assert payload["expires_at_ms"] > 0


def test_create_returns_a_pending_payload(fresh_geo, hub) -> None:
    created = _create(hub)
    assert created["ok"] is True
    assert created["status"] == "pending"
    assert created["terminal"] is False
    assert created["targets_n"] == 1
    assert geo.REQUEST_ID_RE.fullmatch(created["request_id"])


def test_publish_failure_on_one_target_does_not_sink_the_request(fresh_geo) -> None:
    class _HalfDeadHub(FakeHub):
        def publish(self, session_id, event, data):
            if session_id == SID2:
                raise RuntimeError("dead queue")
            super().publish(session_id, event, data)

    broken = _HalfDeadHub(live=(SID, SID2))
    created = _create(broken)
    assert created["status"] == "pending"
    assert created["targets_n"] == 2


# ── delivery targeting ────────────────────────────────────────────


def test_targets_prefer_the_asking_session(fresh_geo) -> None:
    picked = geo.resolve_targets(FakeHub(attached=(SID,), live=(SID, SID2)), SID)
    assert picked == (SID,)


def test_targets_fan_out_when_the_asking_session_has_no_tab(fresh_geo) -> None:
    picked = geo.resolve_targets(FakeHub(live=(SID2, SID3)), SID)
    assert picked == (SID2, SID3)


def test_targets_fan_out_is_capped(fresh_geo) -> None:
    many = tuple(f"{i:08d}-0000-0000-0000-000000000000" for i in range(20))
    picked = geo.resolve_targets(FakeHub(live=many), SID)
    assert len(picked) == geo.GEO_FANOUT_MAX


def test_targets_empty_when_nobody_is_attached(fresh_geo) -> None:
    assert geo.resolve_targets(FakeHub(), SID) == ()


def test_targets_empty_when_the_hub_raises(fresh_geo) -> None:
    # A broken hub is "no browser", never a 500 into the agent's face.
    assert geo.resolve_targets(FakeHub(broken=True), SID) == ()


def test_no_browser_fast_fails_without_registering_anything(fresh_geo) -> None:
    result = _create(FakeHub())
    assert result["ok"] is False
    assert result["status"] == "no_browser"
    assert result["retryable"] is False
    assert result["hint"]
    # The whole point: no pending entry, so no agent blocks for the full TTL.
    assert fresh_geo._PENDING == {}


# ── the answer path ───────────────────────────────────────────────


def test_fixed_answer_resolves_and_caches(fresh_geo, hub) -> None:
    created = _create(hub)
    accepted = geo.submit_answer(created["request_id"], {"status": "fixed", "coords": COORDS},
                                 hub=hub)
    assert accepted == {"ok": True, "accepted": True, "status": "fixed"}
    result = fresh_geo._PENDING[created["request_id"]].result
    assert result["status"] == "fixed"
    assert result["terminal"] is True
    assert result["source"] == "browser-gps"
    assert result["fresh"] is True
    assert result["fix"]["lat"] == pytest.approx(52.22968)
    # …and the cache now serves the next agent without a second prompt.
    assert geo.last_fix(300)["fix"]["lon"] == pytest.approx(21.01223)


def test_answer_before_wait_is_not_lost(fresh_geo, hub) -> None:
    """The common race: the phone answers before the CLI issues its first /wait.

    Dropping the entry on resolve would turn this into a bogus `not_found`.
    """
    created = _create(hub)
    geo.submit_answer(created["request_id"], {"status": "fixed", "coords": COORDS}, hub=hub)
    waited = _run(geo.await_fix(created["request_id"]))
    assert waited["status"] == "fixed"
    assert waited["terminal"] is True


def test_first_fixed_wins(fresh_geo) -> None:
    two = FakeHub(live=(SID, SID2))
    created = _create(two)
    rid = created["request_id"]
    first = geo.submit_answer(rid, {"status": "fixed", "coords": COORDS,
                                    "session_id": SID}, hub=two)
    second = geo.submit_answer(rid, {"status": "fixed",
                                     "coords": {**COORDS, "lat": 10.0},
                                     "session_id": SID2}, hub=two)
    assert first["accepted"] is True
    assert second == {"ok": True, "accepted": False, "status": "fixed"}
    # The loser's coordinates never became the fix.
    assert geo.last_fix(300)["fix"]["lat"] == pytest.approx(52.22968)


def test_losing_target_is_told_to_close_its_modal(fresh_geo) -> None:
    two = FakeHub(live=(SID, SID2))
    rid = _create(two)["request_id"]
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS, "session_id": SID}, hub=two)
    cancels = two.events(geo.EVENT_CANCELLED)
    assert [sid for sid, _, _ in cancels] == [SID2]  # not the winner
    assert cancels[0][2] == {"request_id": rid, "reason": "answered_elsewhere"}


def test_a_denial_does_not_consume_another_targets_chance(fresh_geo) -> None:
    """The outstanding-targets rule: a laptop with no GPS must not decide."""
    two = FakeHub(live=(SID, SID2))
    rid = _create(two)["request_id"]
    denied = geo.submit_answer(rid, {"status": "denied", "session_id": SID}, hub=two)
    assert denied == {"ok": True, "accepted": True, "status": "pending"}
    assert fresh_geo._PENDING[rid].result is None
    # The phone still gets to answer, and its fix wins.
    fixed = geo.submit_answer(rid, {"status": "fixed", "coords": COORDS,
                                    "session_id": SID2}, hub=two)
    assert fixed["status"] == "fixed"


def test_two_tabs_on_one_session_are_two_voices(fresh_geo) -> None:
    """The desktop and the phone can share a session id — and often do.

    Keying the outstanding-answer rule on the SESSION let the laptop's
    ``unavailable`` resolve the ask and throw away the phone's real fix.
    """
    both = FakeHub(attached=(SID,), live=(SID,))
    both.subscriber_count = lambda sid: 2 if sid == SID else 0  # two tabs, one sid
    rid = _create(both)["request_id"]
    assert fresh_geo._PENDING[rid].expected_n == 2

    first = geo.submit_answer(rid, {"status": "unavailable", "session_id": SID}, hub=both)
    assert first == {"ok": True, "accepted": True, "status": "pending"}
    assert fresh_geo._PENDING[rid].result is None

    second = geo.submit_answer(rid, {"status": "fixed", "coords": COORDS,
                                     "session_id": SID}, hub=both)
    assert second["status"] == "fixed"
    assert geo.last_fix(300)["fix"]["lat"] == pytest.approx(52.22968)


def test_two_tabs_both_refusing_still_resolve(fresh_geo) -> None:
    """The other half of the rule: waiting forever is not the answer either."""
    both = FakeHub(attached=(SID,), live=(SID,))
    both.subscriber_count = lambda sid: 2 if sid == SID else 0
    rid = _create(both)["request_id"]
    geo.submit_answer(rid, {"status": "dismissed", "session_id": SID}, hub=both)
    last = geo.submit_answer(rid, {"status": "denied", "session_id": SID}, hub=both)
    assert last == {"ok": True, "accepted": True, "status": "denied"}


def test_a_voice_that_never_arrives_costs_a_grace_not_the_whole_ttl(fresh_geo) -> None:
    """The count is a guess (a subscriber may have gone away) — bound the wait.

    And what the TTL then reports is what we HEARD: a lone `denied` is honestly
    `denied`, not `expired` ("nobody reacted").
    """
    both = FakeHub(attached=(SID,), live=(SID,))
    both.subscriber_count = lambda sid: 2 if sid == SID else 0
    base = time.monotonic()
    rid = _create(both, now=base)["request_id"]
    req = fresh_geo._PENDING[rid]
    assert req.expires_at == pytest.approx(base + geo.GEO_REQUEST_TTL_S)

    geo.submit_answer(rid, {"status": "denied", "session_id": SID}, hub=both, now=base)
    assert req.expires_at == pytest.approx(base + geo.GEO_QUORUM_GRACE_S)

    geo._sweep(hub=both, now=base + geo.GEO_QUORUM_GRACE_S + 0.1)
    assert req.result["status"] == "denied"


def test_an_ask_nobody_answered_still_expires(fresh_geo, hub) -> None:
    base = time.monotonic()
    rid = _create(hub, now=base)["request_id"]
    geo._sweep(hub=hub, now=base + geo.GEO_REQUEST_TTL_S + 1.0)
    assert fresh_geo._PENDING[rid].result["status"] == "expired"


def test_expected_answerers_is_capped_and_floored(fresh_geo) -> None:
    """A stale subscriber count must not park every refusal until the TTL."""
    absurd = FakeHub(attached=(SID,), live=(SID,))
    absurd.subscriber_count = lambda sid: 10_000
    assert fresh_geo._PENDING == {}
    rid = _create(absurd)["request_id"]
    assert fresh_geo._PENDING[rid].expected_n == geo.GEO_EXPECTED_MAX
    # A hub that cannot count is one voice per target, never zero.
    assert geo._expected_answerers(FakeHub(broken=True), (SID, SID2)) == 2


def test_all_targets_refusing_resolves_the_request(fresh_geo) -> None:
    two = FakeHub(live=(SID, SID2))
    rid = _create(two)["request_id"]
    geo.submit_answer(rid, {"status": "denied", "session_id": SID}, hub=two)
    last = geo.submit_answer(rid, {"status": "denied", "session_id": SID2}, hub=two)
    assert last == {"ok": True, "accepted": True, "status": "denied"}
    assert fresh_geo._PENDING[rid].result["status"] == "denied"


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (("denied", "dismissed"), "denied"),
        (("dismissed", "timeout"), "dismissed"),
        (("timeout", "unavailable"), "timeout"),
        (("unavailable", "no_api"), "unavailable"),
        (("insecure_context", "insecure_context"), "insecure_context"),
    ],
)
def test_mixed_refusals_collapse_by_precedence(fresh_geo, answers, expected) -> None:
    two = FakeHub(live=(SID, SID2))
    rid = _create(two)["request_id"]
    geo.submit_answer(rid, {"status": answers[0], "session_id": SID}, hub=two)
    final = geo.submit_answer(rid, {"status": answers[1], "session_id": SID2}, hub=two)
    assert final["status"] == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "denied", "reason": "device_opt_out"}, "device_opt_out"),
        ({"status": "device_opt_out"}, "device_opt_out"),
        ({"status": "unavailable", "reason": "no_api"}, "no_api"),
        ({"status": "no_api"}, "no_api"),
        # An unrelated reason must not rewrite the status.
        ({"status": "denied", "reason": "whatever"}, "denied"),
    ],
)
def test_reason_aliases_normalize_to_their_dedicated_status(fresh_geo, hub, payload,
                                                            expected) -> None:
    rid = _create(hub)["request_id"]
    assert geo.submit_answer(rid, payload, hub=hub)["status"] == expected


def test_unknown_answer_status_is_rejected(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    with pytest.raises(ValueError):
        geo.submit_answer(rid, {"status": "pwned"}, hub=hub)


@pytest.mark.parametrize("status", sorted(set(geo.STATUSES) - geo.ANSWER_STATUSES))
def test_server_only_statuses_cannot_be_claimed_by_a_browser(fresh_geo, hub, status) -> None:
    """A browser must not be able to assert e.g. ``cached`` or ``no_browser``."""
    rid = _create(hub)["request_id"]
    with pytest.raises(ValueError):
        geo.submit_answer(rid, {"status": status}, hub=hub)


def test_unknown_request_id_raises_not_found(fresh_geo, hub) -> None:
    with pytest.raises(geo.GeoNotFound):
        geo.submit_answer("a" * 22, {"status": "denied"}, hub=hub)


def test_malformed_request_id_raises_value_error(fresh_geo, hub) -> None:
    with pytest.raises(ValueError):
        geo.submit_answer("../../etc/passwd", {"status": "denied"}, hub=hub)


def test_non_dict_payload_raises_value_error(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    with pytest.raises(ValueError):
        geo.submit_answer(rid, ["status", "fixed"], hub=hub)  # type: ignore[arg-type]


def test_an_unrecognised_session_id_is_folded_onto_a_real_target(fresh_geo) -> None:
    """The answer route is un-authenticated: a claimed sid can't invent a voice."""
    two = FakeHub(live=(SID, SID2))
    rid = _create(two)["request_id"]
    geo.submit_answer(rid, {"status": "denied", "session_id": "who-am-i"}, hub=two)
    # Folded onto targets[0], so ONE outstanding target remains — not two.
    assert set(fresh_geo._PENDING[rid].answers) == {SID}
    assert fresh_geo._PENDING[rid].result is None


def test_answer_to_an_expired_request_reports_expired(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    fresh_geo._PENDING[rid].expires_at = time.monotonic() - 1.0
    out = geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)
    assert out == {"ok": True, "accepted": False, "status": "expired"}
    assert geo.last_fix(300) is None  # a late fix must not become the cache


def test_browser_timestamp_is_used_when_plausible(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    claimed = geo.now_ms() - 5_000
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS, "ts_ms": claimed}, hub=hub)
    assert fresh_geo._PENDING[rid].result["fix"]["ts_ms"] == claimed


def test_a_platform_cached_position_is_aged_from_its_measurement(fresh_geo, hub) -> None:
    """`maximumAge` mirrors --max-age, so a fix can be minutes old on ARRIVAL.

    Stamping the receipt would report a 4-minute-old reading as "1 s ago" and
    then keep re-serving it as fresh for the whole 15-minute cache window.
    """
    rid = _create(hub)["request_id"]
    measured = geo.now_ms() - 240_000
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS, "ts_ms": measured},
                      hub=hub)
    result = fresh_geo._PENDING[rid].result
    assert result["age_s"] == pytest.approx(240.0, abs=2.0)
    assert result["fresh"] is False
    # …and the cache agrees, so a --max-age 60 caller does NOT get this one.
    assert geo.last_fix(60) is None
    assert geo.last_fix(300)["age_s"] == pytest.approx(240.0, abs=2.0)


def test_a_freshly_measured_fix_is_reported_fresh(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)
    result = fresh_geo._PENDING[rid].result
    assert result["fresh"] is True
    assert result["age_s"] < geo.GEO_FRESH_WINDOW_S


@pytest.mark.parametrize("bogus", [1, 2 ** 52, None, "yesterday", True])
def test_implausible_browser_timestamp_falls_back_to_server_time(fresh_geo, hub, bogus) -> None:
    """A device with a wrong clock must not backdate (or post-date) a fix."""
    rid = _create(hub)["request_id"]
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS, "ts_ms": bogus}, hub=hub)
    stamped = fresh_geo._PENDING[rid].result["fix"]["ts_ms"]
    assert abs(stamped - geo.now_ms()) < 60_000


# ── coords scrubbing (untrusted payload) ──────────────────────────


def test_coords_are_rebuilt_from_an_allowlist(fresh_geo) -> None:
    fix = geo.rebuild_coords({**COORDS, "evil": "x", "__proto__": {}, "cookie": "a=b"}, "fine")
    assert set(fix) == set(geo.COORD_KEYS) | {"precision"}


def test_fine_precision_rounds_to_five_decimals(fresh_geo) -> None:
    fix = geo.rebuild_coords(COORDS, "fine")
    assert fix["lat"] == pytest.approx(52.22968)
    assert fix["lon"] == pytest.approx(21.01223)
    assert fix["precision"] == "fine"


def test_coarse_precision_rounds_and_floors_the_accuracy(fresh_geo) -> None:
    """Rounding is a privacy control: a coarse fix must not LOOK precise."""
    fix = geo.rebuild_coords(COORDS, "coarse")
    assert fix["lat"] == pytest.approx(52.23)
    assert fix["lon"] == pytest.approx(21.01)
    assert fix["accuracy_m"] == 1000.0
    assert fix["precision"] == "coarse"


def test_coarse_keeps_an_already_worse_accuracy(fresh_geo) -> None:
    fix = geo.rebuild_coords({**COORDS, "accuracy_m": 4200.0}, "coarse")
    assert fix["accuracy_m"] == 4200.0


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-an-object",
        {},
        {"lat": 91.0, "lon": 21.0, "accuracy_m": 5},
        {"lat": -91.0, "lon": 21.0, "accuracy_m": 5},
        {"lat": 52.0, "lon": 181.0, "accuracy_m": 5},
        {"lat": 52.0, "lon": "21.0", "accuracy_m": 5},      # string, not a number
        {"lat": float("nan"), "lon": 21.0, "accuracy_m": 5},
        {"lat": float("inf"), "lon": 21.0, "accuracy_m": 5},
        {"lat": 52.0, "lon": 21.0},                          # missing accuracy
        {"lat": 52.0, "lon": 21.0, "accuracy_m": 0},
        {"lat": 52.0, "lon": 21.0, "accuracy_m": -5},
        {"lat": True, "lon": 21.0, "accuracy_m": 5},         # bool is an int in Python
    ],
)
def test_unusable_coords_raise_rather_than_becoming_a_fix_at_null_island(
    fresh_geo, raw
) -> None:
    with pytest.raises(ValueError):
        geo.rebuild_coords(raw, "fine")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("altitude_m", 999_999.0),   # above the Kármán line
        ("altitude_m", "high"),
        ("heading", 400.0),          # outside a circle
        ("heading", float("nan")),
        ("speed_mps", -1.0),
        ("speed_mps", 99_999.0),     # past escape velocity
    ],
)
def test_garbage_optional_fields_are_dropped_not_fatal(fresh_geo, key, value) -> None:
    # A broken barometer must not cost the user an otherwise perfect fix.
    fix = geo.rebuild_coords({**COORDS, key: value}, "fine")
    assert fix[key] is None
    assert fix["lat"] == pytest.approx(52.22968)


def test_good_optional_fields_survive(fresh_geo) -> None:
    fix = geo.rebuild_coords(
        {**COORDS, "altitude_m": 113.5, "heading": 271.0, "speed_mps": 1.4}, "fine"
    )
    assert (fix["altitude_m"], fix["heading"], fix["speed_mps"]) == (113.5, 271.0, 1.4)


def test_sanitize_reason_strips_control_characters_and_caps(fresh_geo) -> None:
    assert geo.sanitize_reason("  a\x00b\nc  ") == "abc"
    assert len(geo.sanitize_reason("x" * 500)) == geo.REASON_MAX_CHARS
    assert geo.sanitize_reason(None) == ""
    assert geo.sanitize_reason(12) == ""


def test_normalize_accuracy_degrades_to_fine(fresh_geo) -> None:
    assert geo.normalize_accuracy("coarse") == "coarse"
    assert geo.normalize_accuracy("military") == "fine"
    assert geo.normalize_accuracy(None) == "fine"


def test_clamp_int_rejects_bool_and_clamps(fresh_geo) -> None:
    assert geo.clamp_int(True, 7, 0, 10) == 7
    assert geo.clamp_int(None, 7, 0, 10) == 7
    assert geo.clamp_int("nope", 7, 0, 10) == 7
    assert geo.clamp_int(99, 7, 0, 10) == 10
    assert geo.clamp_int(-5, 7, 0, 10) == 0


def test_agent_identity_is_capped_and_never_none(fresh_geo, hub) -> None:
    _create(hub, agent={"lib_id": "x" * 400, "title": None, "extra": "dropped"})
    payload = hub.events(geo.EVENT_REQUEST)[0][2]
    assert set(payload["agent"]) == {"lib_id", "title"}
    assert len(payload["agent"]["lib_id"]) == 120
    assert payload["agent"]["title"] == ""


# ── the cache ─────────────────────────────────────────────────────


def test_cache_hit_skips_the_prompt_entirely(fresh_geo, hub) -> None:
    base = time.monotonic()
    geo.store_fix({"lat": 52.23, "lon": 21.01, "precision": "fine"}, now=base)
    result = _create(hub, now=base + 47.0)
    assert result["status"] == "cached"
    assert result["terminal"] is True
    assert result["fresh"] is False
    assert result["age_s"] == pytest.approx(47.0)
    assert hub.published == []          # no SSE frame at all
    assert fresh_geo._PENDING == {}     # nothing to wait on


def test_max_age_zero_forces_a_fresh_prompt(fresh_geo, hub) -> None:
    base = time.monotonic()
    geo.store_fix({"lat": 52.23, "lon": 21.01}, now=base)
    result = _create(hub, max_age_s=0, now=base + 1.0)
    assert result["status"] == "pending"
    assert len(hub.events(geo.EVENT_REQUEST)) == 1


def test_cache_misses_once_it_is_older_than_max_age(fresh_geo) -> None:
    base = time.monotonic()
    geo.store_fix({"lat": 52.23}, now=base)
    assert geo.last_fix(60, now=base + 120) is None
    assert geo.last_fix(600, now=base + 120)["age_s"] == pytest.approx(120.0)


def test_cache_never_outlives_the_hard_ttl(fresh_geo) -> None:
    # Even an agent asking for a 100-hour-old fix is capped at GEO_CACHE_TTL_S.
    base = time.monotonic()
    geo.store_fix({"lat": 52.23}, now=base)
    assert geo.last_fix(360_000, now=base + geo.GEO_CACHE_TTL_S + 1) is None


def test_cache_returns_a_copy(fresh_geo) -> None:
    geo.store_fix({"lat": 52.23})
    got = geo.last_fix(300)
    got["fix"]["lat"] = 0.0
    assert geo.last_fix(300)["fix"]["lat"] == 52.23


def test_a_coarse_ask_is_served_a_re_rounded_copy(fresh_geo, hub) -> None:
    """`coarse` is a PRIVACY control, so the cache must honour it too."""
    base = time.monotonic()
    geo.store_fix({"lat": 52.22968, "lon": 21.01223, "accuracy_m": 12.0,
                   "precision": "fine"}, now=base)
    served = _create(hub, accuracy="coarse", now=base + 5.0)
    assert served["status"] == "cached"
    assert served["fix"] == {"lat": 52.23, "lon": 21.01, "accuracy_m": 1000.0,
                             "altitude_m": None, "heading": None,
                             "speed_mps": None, "precision": "coarse"}
    assert hub.published == []


def test_a_fine_ask_never_gets_a_coarse_cache_entry(fresh_geo, hub) -> None:
    """Precision can be lost, never invented — the miss re-measures instead."""
    base = time.monotonic()
    geo.store_fix({"lat": 52.23, "lon": 21.01, "accuracy_m": 1000.0,
                   "precision": "coarse"}, now=base)
    assert _create(hub, accuracy="fine", now=base + 5.0)["status"] == "pending"


def test_cached_payload_carries_the_measurement_timestamp(fresh_geo) -> None:
    stamp = geo.now_ms() - 4_000
    geo.store_fix({"lat": 52.22968, "lon": 21.01223, "accuracy_m": 12.0,
                   "precision": "fine", "ts_ms": stamp})
    assert geo.cached_payload("coarse", 300)["fix"]["ts_ms"] == stamp


def test_forget_is_idempotent(fresh_geo) -> None:
    geo.store_fix({"lat": 52.23})
    assert geo.forget() == {"ok": True, "forgotten": True}
    assert geo.forget() == {"ok": True, "forgotten": False}
    assert geo.last_fix(300) is None


# ── caps + anti-nag ───────────────────────────────────────────────


def test_anti_nag_refuses_a_back_to_back_second_prompt(fresh_geo, hub) -> None:
    base = time.monotonic()
    _create(hub, now=base)
    second = _create(hub, now=base + 1.6)
    assert second["status"] == "rate_limited"
    assert second["retryable"] is True
    assert second["terminal"] is False
    assert second["retry_after_s"] == pytest.approx(3.4)
    assert len(hub.events(geo.EVENT_REQUEST)) == 1


def test_anti_nag_lets_a_later_prompt_through(fresh_geo, hub) -> None:
    base = time.monotonic()
    _create(hub, now=base)
    assert _create(hub, now=base + geo.GEO_MIN_PROMPT_INTERVAL_S + 0.1)["status"] == "pending"


def test_pending_cap_raises_geo_saturated(fresh_geo, hub) -> None:
    base = time.monotonic()
    for i in range(geo.GEO_MAX_PENDING):
        # Spaced past the anti-nag window so the CAP is what we're measuring.
        assert _create(hub, now=base + i * 6.0)["status"] == "pending"
    with pytest.raises(geo.GeoSaturated):
        _create(hub, now=base + geo.GEO_MAX_PENDING * 6.0)


def test_resolved_requests_do_not_count_against_the_cap(fresh_geo, hub) -> None:
    base = time.monotonic()
    rid = _create(hub, now=base)["request_id"]
    geo.submit_answer(rid, {"status": "denied"}, hub=hub)
    assert geo.active_requests() == []


def test_wait_cap_raises_geo_wait_saturated(fresh_geo, hub, monkeypatch) -> None:
    rid = _create(hub)["request_id"]
    monkeypatch.setattr(geo, "_wait_inflight", geo.GEO_WAIT_MAX_CONCURRENT)
    with pytest.raises(geo.GeoWaitSaturated):
        _run(geo.await_fix_guarded(rid, 0.01, hub=hub))


def test_wait_counter_is_released_after_a_normal_call(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    geo.submit_answer(rid, {"status": "denied"}, hub=hub)
    _run(geo.await_fix_guarded(rid, 0.01, hub=hub))
    assert geo._wait_inflight == 0


def test_wait_counter_is_released_even_when_the_wait_raises(fresh_geo, hub, monkeypatch) -> None:
    async def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(geo, "await_fix", _boom)
    with pytest.raises(RuntimeError):
        _run(geo.await_fix_guarded("a" * 22, 0.01, hub=hub))
    assert geo._wait_inflight == 0


# ── the blocking half ─────────────────────────────────────────────


def test_wait_on_an_unknown_request_is_not_found(fresh_geo, hub) -> None:
    out = _run(geo.await_fix("z" * 22, 0.01, hub=hub))
    assert out["status"] == "not_found"
    assert out["terminal"] is True
    assert out["retryable"] is False


def test_wait_timeout_is_non_terminal_and_the_slot_survives(fresh_geo, hub) -> None:
    """The poll window elapsing is NOT the request expiring.

    ``terminal`` is the discriminator between the two same-named outcomes; the
    CLI re-polls iff ``terminal is False``, and this pins that the re-poll can
    still resolve.
    """
    rid = _create(hub)["request_id"]
    first = _run(geo.await_fix(rid, 0.05, hub=hub))
    assert first["ok"] is True
    assert first["status"] == "timeout"
    assert first["terminal"] is False
    assert first["reason"] == "poll_window_elapsed"
    assert rid in fresh_geo._PENDING
    assert fresh_geo._PENDING[rid].result is None
    # …and the same id still resolves on the next poll.
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)
    assert _run(geo.await_fix(rid, 0.05, hub=hub))["status"] == "fixed"


def test_wait_window_is_clamped_to_the_maximum(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    started = time.monotonic()
    # A huge requested window must not park the connection for an hour; it is
    # clamped, and here the (much earlier) TTL ends the call anyway.
    fresh_geo._PENDING[rid].expires_at = started + 0.05
    out = _run(geo.await_fix(rid, 3600.0, hub=hub))
    assert out["status"] == "expired"
    assert time.monotonic() - started < 5.0


def test_wait_resolves_when_the_answer_lands_mid_wait(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]

    async def scenario():
        async def answer():
            await asyncio.sleep(0.05)
            geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)

        waited, _ = await asyncio.gather(geo.await_fix(rid, 5.0, hub=hub), answer())
        return waited

    out = _run(scenario())
    assert out["status"] == "fixed"
    assert out["fix"]["lat"] == pytest.approx(52.22968)


def test_expired_request_resolves_terminally(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    fresh_geo._PENDING[rid].expires_at = time.monotonic() - 1.0
    out = _run(geo.await_fix(rid, 5.0, hub=hub))
    assert out["status"] == "expired"
    assert out["terminal"] is True
    assert out["retryable"] is False
    assert [row[2]["reason"] for row in hub.events(geo.EVENT_CANCELLED)] == ["expired"]


def test_sweep_expires_overdue_requests(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    fresh_geo._PENDING[rid].expires_at = time.monotonic() - 1.0
    assert geo._sweep(hub=hub) == 1
    assert fresh_geo._PENDING[rid].result["status"] == "expired"


def test_sweep_drops_results_past_the_retention_grace(fresh_geo, hub) -> None:
    base = time.monotonic()
    rid = _create(hub, now=base)["request_id"]
    geo.submit_answer(rid, {"status": "denied"}, hub=hub, now=base)
    geo._sweep(hub=hub, now=base + geo.GEO_RESULT_GRACE_S - 1)
    assert rid in fresh_geo._PENDING          # still answerable by a late /wait
    geo._sweep(hub=hub, now=base + geo.GEO_RESULT_GRACE_S + 1)
    assert rid not in fresh_geo._PENDING


# ── cancel ────────────────────────────────────────────────────────


def test_cancel_aborts_a_live_request(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    assert geo.cancel(rid, hub=hub) == {"ok": True, "cancelled": True, "status": "aborted"}
    assert fresh_geo._PENDING[rid].result["status"] == "aborted"
    assert [row[2]["reason"] for row in hub.events(geo.EVENT_CANCELLED)] == ["aborted"]


def test_cancel_is_idempotent(fresh_geo, hub) -> None:
    rid = _create(hub)["request_id"]
    geo.cancel(rid, hub=hub)
    assert geo.cancel(rid, hub=hub) == {"ok": True, "cancelled": False, "status": "aborted"}


def test_cancel_never_overwrites_a_granted_fix(fresh_geo, hub) -> None:
    """The CLI's SIGINT path must not destroy a fix the user already granted."""
    rid = _create(hub)["request_id"]
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)
    assert geo.cancel(rid, hub=hub) == {"ok": True, "cancelled": False, "status": "fixed"}
    assert fresh_geo._PENDING[rid].result["status"] == "fixed"


def test_cancel_unknown_id_is_a_clean_no_op(fresh_geo, hub) -> None:
    assert geo.cancel("q" * 22, hub=hub) == {"ok": True, "cancelled": False,
                                             "status": "not_found"}


def test_cancel_rejects_a_malformed_id(fresh_geo, hub) -> None:
    with pytest.raises(ValueError):
        geo.cancel("../x", hub=hub)


# ── the reconnect poll ────────────────────────────────────────────


def test_pending_for_lists_this_sessions_outstanding_asks(fresh_geo, hub) -> None:
    created = _create(hub, reason="gdzie jestem")
    listed = geo.pending_for(SID)
    assert [row["request_id"] for row in listed] == [created["request_id"]]
    assert listed[0]["reason"] == "gdzie jestem"
    assert listed[0]["accuracy"] == "fine"
    assert listed[0]["expires_at_ms"] > 0


def test_pending_for_ignores_other_sessions(fresh_geo, hub) -> None:
    _create(hub)
    assert geo.pending_for(SID2) == []


def test_pending_for_ignores_resolved_and_expired_asks(fresh_geo, hub) -> None:
    base = time.monotonic()
    rid = _create(hub, now=base)["request_id"]
    geo.submit_answer(rid, {"status": "denied"}, hub=hub, now=base)
    assert geo.pending_for(SID, now=base) == []

    other = _create(hub, now=base + 10)["request_id"]
    fresh_geo._PENDING[other].expires_at = base
    assert geo.pending_for(SID, now=base + 20) == []


def test_pending_for_still_lists_an_unresolved_ask_to_every_target(fresh_geo) -> None:
    """One session can be TWO tabs, so "already answered" is not "done".

    Filtering the answering session out would hide the ask from its OTHER tab —
    the one whose push was dropped, which is the only reason this poll exists.
    The tab that did answer suppresses the repeat client-side (`handledRef`).
    """
    two = FakeHub(live=(SID, SID2))
    rid = _create(two)["request_id"]
    geo.submit_answer(rid, {"status": "denied", "session_id": SID}, hub=two)
    assert [r["request_id"] for r in geo.pending_for(SID)] == [rid]
    assert [r["request_id"] for r in geo.pending_for(SID2)] == [rid]


# ── preflight ─────────────────────────────────────────────────────


def test_snapshot_reports_the_room(fresh_geo, hub) -> None:
    base = time.monotonic()
    _create(hub, now=base)
    # Stored AFTER the ask, or the cache hit would have skipped the prompt.
    geo.store_fix({"lat": 52.23}, now=base - 12.0)
    snap = geo.snapshot(hub, SID)
    assert snap["browser_attached"] is True
    assert snap["browsers_n"] == 1
    assert snap["pending_n"] == 1
    assert snap["wait_inflight"] == 0
    assert snap["last_fix_age_s"] == pytest.approx(12.0, abs=1.0)


def test_snapshot_survives_a_broken_hub(fresh_geo) -> None:
    snap = geo.snapshot(FakeHub(broken=True), SID)
    assert snap["browser_attached"] is False
    assert snap["browsers_n"] == 0


def test_snapshot_without_a_session_id(fresh_geo, hub) -> None:
    assert geo.snapshot(hub, None)["browser_attached"] is False


# ── privacy (keystone) ────────────────────────────────────────────


def test_a_full_round_trip_writes_nothing_to_disk(fresh_geo, hub, tmp_path, monkeypatch) -> None:
    """Storage is memory-only. Nothing about a fix may ever hit the filesystem."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    rid = _create(hub)["request_id"]
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)
    _run(geo.await_fix(rid, 0.01, hub=hub))
    geo.snapshot(hub, SID)
    geo.pending_for(SID)
    geo.forget()

    assert list(tmp_path.rglob("*")) == []


def test_no_coordinates_reach_the_logs(fresh_geo, hub, caplog, capsys) -> None:
    """Log lines carry rid + sid + status — never a latitude or a longitude."""
    caplog.set_level(logging.DEBUG)
    rid = _create(hub, reason="gdzie jestem")["request_id"]
    geo.submit_answer(rid, {"status": "fixed", "coords": COORDS}, hub=hub)
    _run(geo.await_fix(rid, 0.01, hub=hub))
    geo.cancel(rid, hub=hub)

    captured = capsys.readouterr()
    text = caplog.text + captured.out + captured.err
    for needle in ("52.2296", "21.0122", "52.22968", "21.01223", "52.23", "21.01"):
        assert needle not in text, f"a coordinate leaked into a log line: {needle}"
    # …and the useful identifiers ARE there, so this isn't passing by silence.
    assert rid in caplog.text
