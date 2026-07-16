"""Unit tests for the A2A message-bus module (orchestrator_a2a).

Covers the pure/maildir surface: ``agent_key`` mapping + traversal rejection,
``maildir_for`` tree creation + containment, envelope build/validate
normalization + caps, ``enqueue`` atomic landing + dedup detection, the
inbox listing / read / drain helpers, and the ``list_agents`` directory
(reconciled against a fake ``list_sessions`` + ``all_meta`` and a live-id set).

Each test points the module's ``A2A_ROOT`` at a fresh ``tmp_path`` (the maildir
root is computed once at import from ``HOME``), mirroring how the
terminal-shortcuts / agent-tab-order tests monkeypatch their store paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit import orchestrator_a2a as a2a


@pytest.fixture
def fresh_a2a(tmp_path: Path, monkeypatch):
    """Point A2A_ROOT at a tmp maildir root so each test starts clean."""
    root = tmp_path / ".orchestrator" / "a2a"
    monkeypatch.setattr(a2a, "A2A_ROOT", root)
    return a2a


# ── agent_key ─────────────────────────────────────────────────────────


def test_agent_key_global_variants(fresh_a2a):
    assert fresh_a2a.agent_key(None) == "__global__"
    assert fresh_a2a.agent_key("") == "__global__"
    assert fresh_a2a.agent_key("   ") == "__global__"
    assert fresh_a2a.agent_key("global") == "__global__"
    assert fresh_a2a.agent_key("__global__") == "__global__"


def test_agent_key_flat_lib_id(fresh_a2a):
    assert fresh_a2a.agent_key("projects/foo") == "projects__foo"
    assert fresh_a2a.agent_key("areas/Home") == "areas__Home"
    assert fresh_a2a.agent_key("resources/Notes") == "resources__Notes"


def test_agent_key_nested_lib_id(fresh_a2a):
    assert fresh_a2a.agent_key("projects/a/b") == "projects__a__b"
    assert fresh_a2a.agent_key("areas/x/y/z") == "areas__x__y__z"


def test_agent_key_strips_whitespace(fresh_a2a):
    assert fresh_a2a.agent_key("  projects/foo  ") == "projects__foo"


def test_agent_key_rejects_dotdot(fresh_a2a):
    # ".." is excluded by the charset AND the belt-and-suspenders segment check.
    with pytest.raises(ValueError):
        fresh_a2a.agent_key("projects/../etc")
    with pytest.raises(ValueError):
        fresh_a2a.agent_key("areas/..")


def test_agent_key_rejects_absolute_and_traversal(fresh_a2a):
    for bad in ("/etc/passwd", "../escape", "projects/foo/../../bar"):
        with pytest.raises(ValueError):
            fresh_a2a.agent_key(bad)


def test_agent_key_rejects_unknown_kind(fresh_a2a):
    for bad in ("secrets/foo", "random/foo", "projectsfoo", "projects", "foo/bar"):
        with pytest.raises(ValueError):
            fresh_a2a.agent_key(bad)


# ── maildir_for ───────────────────────────────────────────────────────


def test_maildir_for_creates_tree(fresh_a2a):
    dirs = fresh_a2a.maildir_for("areas/Home")
    assert set(dirs) == {"root", "inbox", "tmp", "cur"}
    for key in ("root", "inbox", "tmp", "cur"):
        assert dirs[key].is_dir(), f"{key} should be created"
    assert dirs["root"].name == "areas__Home"


def test_maildir_for_global(fresh_a2a):
    dirs = fresh_a2a.maildir_for(None)
    assert dirs["root"].name == "__global__"
    assert dirs["inbox"].is_dir()


def test_maildir_for_stays_within_root(fresh_a2a):
    dirs = fresh_a2a.maildir_for("projects/a/b")
    root_resolved = fresh_a2a.A2A_ROOT.resolve()
    for key in ("root", "inbox", "tmp", "cur"):
        assert dirs[key].resolve().is_relative_to(root_resolved)


def test_maildir_for_rejects_bad_lib_id(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.maildir_for("projects/../escape")


# ── new_id / _validate_id ─────────────────────────────────────────────


def test_new_id_matches_regex(fresh_a2a):
    for _ in range(50):
        mid = fresh_a2a.new_id()
        assert fresh_a2a.A2A_ID_RE.fullmatch(mid), mid


def test_new_id_unique(fresh_a2a):
    ids = {fresh_a2a.new_id() for _ in range(100)}
    # Random 6-hex suffix → collisions astronomically unlikely within one stamp.
    assert len(ids) >= 99


def test_validate_id_accepts_minted(fresh_a2a):
    mid = fresh_a2a.new_id()
    assert fresh_a2a._validate_id(mid) == mid


def test_validate_id_rejects_junk(fresh_a2a):
    for bad in ("", "nope", "a2a-bad-xyz", "a2a-20250101T000000-ZZZZZZ",
                "../a2a-20250101T000000-abcdef", "a2a-20250101T000000-abc"):
        with pytest.raises(ValueError):
            fresh_a2a._validate_id(bad)


# ── build_envelope ────────────────────────────────────────────────────


def test_build_envelope_good(fresh_a2a):
    env = fresh_a2a.build_envelope(
        from_lib="areas/Home", to_lib="projects/my-project", text="hi there"
    )
    assert fresh_a2a.A2A_ID_RE.fullmatch(env["id"])
    assert env["from"] == "areas/Home"
    assert env["to"] == "projects/my-project"
    assert env["type"] == "message"
    assert env["hops"] == 0
    assert env["schema_version"] == 1
    assert env["ttl"] == fresh_a2a.DEFAULT_TTL
    assert env["correlation_id"] is None
    assert env["reply_to"] is None
    assert env["payload"] == {"text": "hi there", "meta": {}}
    # ISO-8601 UTC stamp ending in Z.
    assert env["ts"].endswith("Z")


def test_build_envelope_normalizes_global(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib=None, to_lib="global", text="x")
    assert env["from"] == "global"
    assert env["to"] == "global"
    env2 = fresh_a2a.build_envelope(from_lib="__global__", to_lib="", text="x")
    assert env2["from"] == "global"
    assert env2["to"] == "global"


def test_build_envelope_reply_type_and_corr(fresh_a2a):
    corr = fresh_a2a.new_id()
    env = fresh_a2a.build_envelope(
        from_lib="areas/Home",
        to_lib="areas/Work",
        type="reply",
        text="re: hi",
        correlation_id=corr,
        reply_to="areas/Home",
    )
    assert env["type"] == "reply"
    assert env["correlation_id"] == corr
    assert env["reply_to"] == "areas/Home"


def test_build_envelope_rejects_empty_text(fresh_a2a):
    for bad in ("", "   ", "\n\t  "):
        with pytest.raises(ValueError):
            fresh_a2a.build_envelope(from_lib="global", to_lib="global", text=bad)


def test_build_envelope_rejects_non_string_text(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.build_envelope(from_lib="global", to_lib="global", text=123)  # type: ignore[arg-type]


def test_build_envelope_rejects_oversize_text(fresh_a2a):
    too_big = "a" * (fresh_a2a.TEXT_MAX_BYTES + 1)
    with pytest.raises(ValueError):
        fresh_a2a.build_envelope(from_lib="global", to_lib="global", text=too_big)


def test_build_envelope_accepts_text_at_cap(fresh_a2a):
    at_cap = "a" * fresh_a2a.TEXT_MAX_BYTES
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="global", text=at_cap)
    assert len(env["payload"]["text"].encode("utf-8")) == fresh_a2a.TEXT_MAX_BYTES


def test_build_envelope_oversize_counts_bytes_not_chars(fresh_a2a):
    # Multi-byte char: just under the cap in chars but over in bytes.
    text = "é" * (fresh_a2a.TEXT_MAX_BYTES // 2 + 1)  # é = 2 bytes UTF-8
    assert len(text) <= fresh_a2a.TEXT_MAX_BYTES
    with pytest.raises(ValueError):
        fresh_a2a.build_envelope(from_lib="global", to_lib="global", text=text)


def test_build_envelope_rejects_bad_type(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.build_envelope(
            from_lib="global", to_lib="global", type="broadcast", text="x"
        )


def test_build_envelope_rejects_bad_correlation_id(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.build_envelope(
            from_lib="global", to_lib="global", text="x", correlation_id="not-an-id"
        )


def test_build_envelope_rejects_bad_from_lib(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.build_envelope(from_lib="secrets/foo", to_lib="global", text="x")


# ── validate_envelope ─────────────────────────────────────────────────


def _good_dict(fresh_a2a) -> dict:
    return {
        "id": fresh_a2a.new_id(),
        "from": "areas/Home",
        "to": "global",
        "type": "message",
        "correlation_id": None,
        "reply_to": None,
        "ts": "2026-01-01T00:00:00Z",
        "ttl": 100,
        "schema_version": 1,
        "hops": 2,
        "payload": {"text": "hello", "meta": {"k": "v"}},
    }


def test_validate_envelope_good(fresh_a2a):
    out = fresh_a2a.validate_envelope(_good_dict(fresh_a2a))
    assert out["from"] == "areas/Home"
    assert out["to"] == "global"
    assert out["payload"] == {"text": "hello", "meta": {"k": "v"}}
    assert out["hops"] == 2
    assert out["schema_version"] == 1


def test_validate_envelope_not_a_dict(fresh_a2a):
    for bad in (None, [], "x", 5):
        with pytest.raises(ValueError):
            fresh_a2a.validate_envelope(bad)


def test_validate_envelope_bad_id(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["id"] = "garbage"
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)


def test_validate_envelope_bad_type(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["type"] = "nope"
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)


def test_validate_envelope_bad_from(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["from"] = "secrets/x"
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)


def test_validate_envelope_missing_payload(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["payload"] = None
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)
    d["payload"] = {"text": ""}
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)


def test_validate_envelope_coerces_meta_and_ints(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["payload"]["meta"] = "not-a-dict"
    d["ttl"] = "garbage"
    d["hops"] = -5
    out = fresh_a2a.validate_envelope(d)
    assert out["payload"]["meta"] == {}
    assert out["ttl"] == fresh_a2a.DEFAULT_TTL
    assert out["hops"] == 0


def test_validate_envelope_blank_ts_replaced(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["ts"] = "   "
    out = fresh_a2a.validate_envelope(d)
    assert out["ts"].endswith("Z") and out["ts"].strip()


def test_validate_envelope_normalizes_reply_to_global(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["reply_to"] = "__global__"
    out = fresh_a2a.validate_envelope(d)
    assert out["reply_to"] == "global"


def test_validate_envelope_bad_correlation_id(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["correlation_id"] = "bogus"
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)


def test_validate_envelope_negative_ttl_reset(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["ttl"] = -1
    out = fresh_a2a.validate_envelope(d)
    assert out["ttl"] == fresh_a2a.DEFAULT_TTL


# ── enqueue + inbox_has ───────────────────────────────────────────────


def test_enqueue_lands_in_target_inbox(fresh_a2a):
    env = fresh_a2a.build_envelope(
        from_lib="areas/Home", to_lib="projects/foo", text="ping"
    )
    path = fresh_a2a.enqueue(env)
    assert path.is_file()
    assert path.name == f"{env['id']}.json"
    # Lives under the *target's* inbox, not the sender's.
    expected_inbox = fresh_a2a.maildir_for("projects/foo")["inbox"].resolve()
    assert path.resolve().parent == expected_inbox
    # Round-trips to the same normalized envelope.
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == env


def test_enqueue_routes_global(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="areas/Home", to_lib="global", text="hey")
    path = fresh_a2a.enqueue(env)
    assert path.resolve().parent.parent.name == "__global__"


def test_enqueue_revalidates_tampered_dict(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="global", text="x")
    env["id"] = "tampered"
    with pytest.raises(ValueError):
        fresh_a2a.enqueue(env)


def test_inbox_has_detects_in_inbox(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    assert fresh_a2a.inbox_has("areas/Home", env["id"]) is False
    fresh_a2a.enqueue(env)
    assert fresh_a2a.inbox_has("areas/Home", env["id"]) is True
    # Not present in a *different* agent's maildir.
    assert fresh_a2a.inbox_has("areas/Work", env["id"]) is False


def test_inbox_has_detects_in_cur(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    fresh_a2a.enqueue(env)
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is True
    # Moved to cur/, still counts as "seen" for dedup.
    assert fresh_a2a.inbox_has("areas/Home", env["id"]) is True


def test_inbox_has_rejects_bad_id(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.inbox_has("areas/Home", "not-an-id")


def test_inbox_has_bad_lib_id_returns_false(fresh_a2a):
    mid = fresh_a2a.new_id()
    assert fresh_a2a.inbox_has("secrets/x", mid) is False


# ── list_inbox / read_message / mark_read ─────────────────────────────


def test_list_inbox_oldest_first(fresh_a2a, monkeypatch):
    # Force three distinct, increasing ids so lexical filename sort == age order.
    minted = [
        "a2a-20260101T000001-aaaaaa",
        "a2a-20260101T000002-bbbbbb",
        "a2a-20260101T000003-cccccc",
    ]
    seq = iter(minted)
    monkeypatch.setattr(fresh_a2a, "new_id", lambda: next(seq))
    # Enqueue in reverse-age order; list must still come back oldest-first.
    for mid in reversed(minted):
        env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text=mid)
        fresh_a2a.enqueue(env)
    listed = [p.name[:-5] for p in fresh_a2a.list_inbox("areas/Home")]
    assert listed == minted


def test_list_inbox_skips_junk(fresh_a2a):
    inbox = fresh_a2a.maildir_for("areas/Home")["inbox"]
    (inbox / "notes.txt").write_text("x", encoding="utf-8")
    (inbox / "not-an-id.json").write_text("{}", encoding="utf-8")
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="real")
    fresh_a2a.enqueue(env)
    listed = [p.name for p in fresh_a2a.list_inbox("areas/Home")]
    assert listed == [f"{env['id']}.json"]


def test_list_inbox_empty_when_no_dir(fresh_a2a):
    # Never-touched agent → maildir_for creates an empty inbox → [].
    assert fresh_a2a.list_inbox("areas/Work") == []


def test_list_inbox_bad_lib_id_returns_empty(fresh_a2a):
    assert fresh_a2a.list_inbox("secrets/x") == []


def test_read_message_round_trip(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="areas/Home", to_lib="areas/Work", text="yo")
    fresh_a2a.enqueue(env)
    got = fresh_a2a.read_message("areas/Work", env["id"])
    assert got == env


def test_read_message_finds_in_cur(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    fresh_a2a.enqueue(env)
    fresh_a2a.mark_read("areas/Home", env["id"])
    got = fresh_a2a.read_message("areas/Home", env["id"])
    assert got is not None and got["id"] == env["id"]


def test_read_message_missing_returns_none(fresh_a2a):
    assert fresh_a2a.read_message("areas/Home", fresh_a2a.new_id()) is None


def test_read_message_corrupt_returns_none(fresh_a2a):
    inbox = fresh_a2a.maildir_for("areas/Home")["inbox"]
    mid = fresh_a2a.new_id()
    (inbox / f"{mid}.json").write_text("{ not json", encoding="utf-8")
    assert fresh_a2a.read_message("areas/Home", mid) is None


def test_read_message_invalid_envelope_returns_none(fresh_a2a):
    inbox = fresh_a2a.maildir_for("areas/Home")["inbox"]
    mid = fresh_a2a.new_id()
    # Valid JSON, but the envelope fails validation (empty text).
    bad = {"id": mid, "from": "global", "to": "areas/Home", "type": "message",
           "payload": {"text": "", "meta": {}}}
    (inbox / f"{mid}.json").write_text(json.dumps(bad), encoding="utf-8")
    assert fresh_a2a.read_message("areas/Home", mid) is None


def test_read_message_rejects_bad_id(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.read_message("areas/Home", "../escape")


def test_mark_read_moves_inbox_to_cur(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    fresh_a2a.enqueue(env)
    dirs = fresh_a2a.maildir_for("areas/Home")
    assert (dirs["inbox"] / f"{env['id']}.json").is_file()
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is True
    assert not (dirs["inbox"] / f"{env['id']}.json").exists()
    assert (dirs["cur"] / f"{env['id']}.json").is_file()


def test_mark_read_idempotent(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    fresh_a2a.enqueue(env)
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is True
    # Second call: already drained → False, no error.
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is False


def test_mark_read_never_delivered_returns_false(fresh_a2a):
    assert fresh_a2a.mark_read("areas/Home", fresh_a2a.new_id()) is False


def test_mark_read_rejects_bad_id(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.mark_read("areas/Home", "bogus")


# ── list_agents ───────────────────────────────────────────────────────


def _patch_sessions(monkeypatch, fresh_a2a, summaries, meta):
    monkeypatch.setattr(a2a.jsonl_mod, "list_sessions", lambda: list(summaries))
    monkeypatch.setattr(a2a.meta_mod, "all_meta", lambda: dict(meta))


def test_list_agents_groups_by_lib_id_and_warm(fresh_a2a, monkeypatch):
    summaries = [
        {"id": "s-dom-old", "updated_at": 100.0},
        {"id": "s-dom-new", "updated_at": 200.0},
        {"id": "s-praca", "updated_at": 150.0},
    ]
    meta = {
        "s-dom-old": {"lib_id": "areas/Home"},
        "s-dom-new": {"lib_id": "areas/Home"},
        "s-praca": {"lib_id": "areas/Work"},
    }
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"s-dom-new"})
    by_lib = {a["lib_id"]: a for a in agents}

    assert set(by_lib) == {"global", "areas/Home", "areas/Work"}
    dom = by_lib["areas/Home"]
    assert dom["warm"] is True  # s-dom-new is live
    assert dom["session_id"] == "s-dom-new"  # most recent represents the group
    assert dom["last_active"] == 200.0
    assert dom["name"] == "Home"

    praca = by_lib["areas/Work"]
    assert praca["warm"] is False
    assert praca["session_id"] == "s-praca"


def test_list_agents_includes_global_when_empty(fresh_a2a, monkeypatch):
    _patch_sessions(monkeypatch, fresh_a2a, [], {})
    agents = fresh_a2a.list_agents(live_session_ids=set())
    assert len(agents) == 1
    g = agents[0]
    assert g["lib_id"] == "global"
    assert g["name"] == "Global"
    assert g["warm"] is False
    assert g["session_id"] is None
    assert g["last_active"] == 0.0


def test_list_agents_no_lib_id_collapses_to_global(fresh_a2a, monkeypatch):
    summaries = [
        {"id": "s-cwd", "updated_at": 50.0},  # no meta entry → global
        {"id": "s-blank", "updated_at": 75.0},  # blank lib_id → global
    ]
    meta = {"s-blank": {"lib_id": "  "}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"s-blank"})
    g = next(a for a in agents if a["lib_id"] == "global")
    assert g["warm"] is True  # s-blank is live and grouped into global
    assert g["session_id"] == "s-blank"  # most recent of the two
    assert g["last_active"] == 75.0


def test_list_agents_bad_lib_id_collapses_to_global(fresh_a2a, monkeypatch):
    summaries = [{"id": "s-bad", "updated_at": 10.0}]
    meta = {"s-bad": {"lib_id": "secrets/oops"}}  # invalid → ValueError path
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    assert {a["lib_id"] for a in agents} == {"global"}
    assert next(a for a in agents)["session_id"] == "s-bad"


def test_list_agents_sorted_by_last_active_desc(fresh_a2a, monkeypatch):
    summaries = [
        {"id": "s-a", "updated_at": 10.0},
        {"id": "s-b", "updated_at": 30.0},
        {"id": "s-c", "updated_at": 20.0},
    ]
    meta = {
        "s-a": {"lib_id": "projects/a"},
        "s-b": {"lib_id": "projects/b"},
        "s-c": {"lib_id": "projects/c"},
    }
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    active = [a["last_active"] for a in agents]
    assert active == sorted(active, reverse=True)
    # Humanized name from the slug.
    assert next(a for a in agents if a["lib_id"] == "projects/b")["name"] == "B"


def test_list_agents_humanizes_kebab_slug(fresh_a2a, monkeypatch):
    summaries = [{"id": "s1", "updated_at": 1.0}]
    meta = {"s1": {"lib_id": "projects/my-project"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    gb = next(a for a in agents if a["lib_id"] == "projects/my-project")
    assert gb["name"] == "My Project"


def test_list_agents_skips_summaries_without_id(fresh_a2a, monkeypatch):
    summaries = [
        {"id": "", "updated_at": 1.0},
        {"updated_at": 2.0},
        {"id": "s-ok", "updated_at": 3.0},
    ]
    meta = {"s-ok": {"lib_id": "areas/Home"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    assert {a["lib_id"] for a in agents} == {"global", "areas/Home"}


def test_list_agents_bad_updated_at_coerced(fresh_a2a, monkeypatch):
    summaries = [{"id": "s1", "updated_at": "garbage"}]
    meta = {"s1": {"lib_id": "areas/Home"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    dom = next(a for a in agents if a["lib_id"] == "areas/Home")
    assert dom["last_active"] == 0.0


def test_list_agents_survives_list_sessions_error(fresh_a2a, monkeypatch):
    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(a2a.jsonl_mod, "list_sessions", boom)
    monkeypatch.setattr(a2a.meta_mod, "all_meta", lambda: {})
    agents = fresh_a2a.list_agents(live_session_ids=set())
    # Never raises; still returns the seeded global agent.
    assert [a["lib_id"] for a in agents] == ["global"]


def test_read_message_bad_lib_id_returns_none(fresh_a2a):
    # Valid id, but the agent key is invalid → maildir_for raises, swallowed → None.
    assert fresh_a2a.read_message("secrets/x", fresh_a2a.new_id()) is None


def test_mark_read_bad_lib_id_returns_false(fresh_a2a):
    assert fresh_a2a.mark_read("secrets/x", fresh_a2a.new_id()) is False


# ── traversal / write internals ───────────────────────────────────────


def test_maildir_for_escape_guard(fresh_a2a, monkeypatch):
    # Force agent_key to emit a traversing key so the resolve() containment
    # guard (the "should never happen" insurance) actually fires.
    monkeypatch.setattr(fresh_a2a, "agent_key", lambda lib_id: "../escape")
    with pytest.raises(ValueError):
        fresh_a2a.maildir_for("areas/Home")


def test_atomic_write_json_cleans_up_on_error(fresh_a2a, tmp_path, monkeypatch):
    target = tmp_path / "out.json"

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        fresh_a2a._atomic_write_json(target, {"bad": Unserializable()})
    assert not target.exists()
    # No leftover .tmp scratch file in the dir.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_agent_name_for_global_when_none(fresh_a2a):
    assert fresh_a2a._agent_name_for(None) == "Global"
    assert fresh_a2a._agent_name_for("") == "Global"


def test_list_agents_survives_all_meta_error(fresh_a2a, monkeypatch):
    def boom():
        raise RuntimeError("meta gone")

    monkeypatch.setattr(a2a.jsonl_mod, "list_sessions",
                        lambda: [{"id": "s1", "updated_at": 5.0}])
    monkeypatch.setattr(a2a.meta_mod, "all_meta", boom)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    # all_meta blew up → no overlay → s1 collapses to global, no crash.
    assert {a["lib_id"] for a in agents} == {"global"}


# ══════════════════════════════════════════════════════════════════════
#  NEW: atomic-claim + per-session maildir + session targeting
# ══════════════════════════════════════════════════════════════════════

# A canonical, well-formed uuid4-shaped session id (matches _SESSION_ID_RE).
SID = "abcdef01-2345-6789-abcd-ef0123456789"
SID2 = "11112222-3333-4444-5555-666677778888"


def _drained_text(env_or_none) -> str | None:
    """Pull payload.text out of a read_message result (or None)."""
    return None if env_or_none is None else env_or_none["payload"]["text"]


# ── atomic-claim: read_message ────────────────────────────────────────


def test_read_message_claims_inbox_to_cur(fresh_a2a):
    # A successful read CLAIMS (moves) the file from inbox/ → cur/.
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="claim me")
    fresh_a2a.enqueue(env)
    dirs = fresh_a2a.maildir_for("areas/Home")
    assert (dirs["inbox"] / f"{env['id']}.json").is_file()
    got = fresh_a2a.read_message("areas/Home", env["id"])
    assert got is not None and got["id"] == env["id"]
    # After the claim, the file lives in cur/, not inbox/.
    assert not (dirs["inbox"] / f"{env['id']}.json").exists()
    assert (dirs["cur"] / f"{env['id']}.json").is_file()


def test_read_message_concurrent_drains_exactly_one_consumer(fresh_a2a):
    # Two reads of the SAME message: the first claims + returns the envelope,
    # the second sees it already in cur/ and returns the SAME envelope (no dup,
    # no crash). The message is never lost and never duplicated on disk.
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="once")
    fresh_a2a.enqueue(env)
    first = fresh_a2a.read_message("areas/Home", env["id"])
    second = fresh_a2a.read_message("areas/Home", env["id"])
    assert first is not None and first["id"] == env["id"]
    # Falls through to the cur/ copy — readable, identical, no exception.
    assert second is not None and second["id"] == env["id"]
    assert first == second


def test_read_message_claim_skipped_when_already_moved(fresh_a2a):
    # mark_read drains inbox→cur first; a subsequent read finds nothing in
    # inbox/ (FileNotFoundError on the claim) and reads the existing cur/ copy.
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="moved")
    fresh_a2a.enqueue(env)
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is True
    got = fresh_a2a.read_message("areas/Home", env["id"])
    assert got is not None and got["id"] == env["id"]


def test_read_message_claim_oserror_falls_through(fresh_a2a, monkeypatch):
    # A non-FileNotFoundError on the os.replace claim is warned + swallowed,
    # then the code falls through to read the cur/ copy (pre-seeded here).
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="fell")
    fresh_a2a.enqueue(env)
    dirs = fresh_a2a.maildir_for("areas/Home")
    # Pre-place a valid copy in cur/ so the fall-through read succeeds.
    (dirs["cur"] / f"{env['id']}.json").write_text(
        json.dumps(env), encoding="utf-8"
    )

    def boom_replace(src, dst):
        raise OSError("EXDEV-ish")

    monkeypatch.setattr(a2a.os, "replace", boom_replace)
    got = fresh_a2a.read_message("areas/Home", env["id"])
    assert got is not None and got["id"] == env["id"]


# ── atomic-claim: drain semantics over the same inbox ─────────────────


def _drain_inbox(fresh_a2a, lib_id):
    """Module-side drain: list → claim-then-read each. Mirrors the CLI path."""
    out = []
    for p in fresh_a2a.list_inbox(lib_id):
        got = fresh_a2a.read_message(lib_id, p.name[:-5])
        if got is not None:
            out.append(got)
    return out


def test_two_sequential_drains_yield_message_once(fresh_a2a):
    # The contract: two drains over the same inbox yield the message to exactly
    # ONE. list_inbox only ever sees inbox/ entries; after the first drain claims
    # them all, the second drain's list_inbox is empty → nothing returned.
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="solo")
    fresh_a2a.enqueue(env)
    first = _drain_inbox(fresh_a2a, "areas/Home")
    second = _drain_inbox(fresh_a2a, "areas/Home")
    assert [_drained_text(m) for m in first] == ["solo"]
    assert second == []  # nothing left in inbox/ for the second drainer


def test_claim_on_already_moved_file_is_skipped_no_crash(fresh_a2a):
    # Capture the id list BEFORE draining (simulating a stale candidate list),
    # drain once to claim it, THEN replay the read on the now-moved id: the
    # claim raises FileNotFoundError internally → falls through to cur/ → no dup
    # added to inbox, no crash.
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="stale")
    fresh_a2a.enqueue(env)
    candidate_ids = [p.name[:-5] for p in fresh_a2a.list_inbox("areas/Home")]
    assert candidate_ids == [env["id"]]
    # Drainer A claims it.
    assert fresh_a2a.read_message("areas/Home", env["id"]) is not None
    dirs = fresh_a2a.maildir_for("areas/Home")
    assert not (dirs["inbox"] / f"{env['id']}.json").exists()
    # Drainer B replays the stale candidate id: skip-no-dup, reads cur/ copy.
    replayed = fresh_a2a.read_message("areas/Home", candidate_ids[0])
    assert replayed is not None and replayed["id"] == env["id"]


def test_mark_read_concurrent_only_one_claims(fresh_a2a):
    # Two mark_read calls race over the same file: first wins (True), second
    # finds inbox/ empty (FileNotFoundError) → False. Exactly-one consumer.
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    fresh_a2a.enqueue(env)
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is True
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is False


def test_mark_read_claim_oserror_returns_false(fresh_a2a, monkeypatch):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    fresh_a2a.enqueue(env)

    def boom_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(a2a.os, "replace", boom_replace)
    # Non-FNF OSError on the claim → warned + swallowed → False (not claimed).
    assert fresh_a2a.mark_read("areas/Home", env["id"]) is False


# ── _validate_session_id ──────────────────────────────────────────────


def test_validate_session_id_accepts_uuid(fresh_a2a):
    assert fresh_a2a._validate_session_id(SID) == SID


def test_validate_session_id_rejects_malformed(fresh_a2a):
    for bad in (
        "",
        "   ",
        "not-a-uuid",
        "ABCDEF01-2345-6789-abcd-ef0123456789",  # uppercase hex rejected
        "abcdef01-2345-6789-abcd-ef012345678",   # too short
        "abcdef01-2345-6789-abcd-ef01234567890",  # too long
        "../escape",
        "abcdef01_2345_6789_abcd_ef0123456789",   # wrong separators
    ):
        with pytest.raises(ValueError):
            fresh_a2a._validate_session_id(bad)


def test_validate_session_id_rejects_non_string(fresh_a2a):
    for bad in (None, 123, [], {}):
        with pytest.raises(ValueError):
            fresh_a2a._validate_session_id(bad)  # type: ignore[arg-type]


# ── session_maildir ───────────────────────────────────────────────────


def test_session_maildir_creates_tree(fresh_a2a):
    dirs = fresh_a2a.session_maildir("areas/Home", SID)
    assert set(dirs) == {"root", "inbox", "cur", "tmp"}
    for key in ("root", "inbox", "cur"):
        assert dirs[key].is_dir(), f"{key} should be created"


def test_session_maildir_correct_path(fresh_a2a):
    dirs = fresh_a2a.session_maildir("areas/Home", SID)
    # <key>/sessions/<sid>/{inbox,cur}
    assert dirs["root"].parent.name == "sessions"
    assert dirs["root"].name == SID
    assert dirs["root"].parent.parent.name == "areas__Home"
    assert dirs["inbox"] == dirs["root"] / "inbox"
    assert dirs["cur"] == dirs["root"] / "cur"


def test_session_maildir_tmp_is_agent_level(fresh_a2a):
    # tmp must be the AGENT-level <key>/tmp (same fs → os.replace stays atomic),
    # NOT a per-session tmp.
    agent = fresh_a2a.maildir_for("areas/Home")
    dirs = fresh_a2a.session_maildir("areas/Home", SID)
    assert dirs["tmp"] == agent["tmp"]
    assert dirs["tmp"].name == "tmp"
    assert dirs["tmp"].parent.name == "areas__Home"


def test_session_maildir_global(fresh_a2a):
    dirs = fresh_a2a.session_maildir(None, SID)
    assert dirs["root"].parent.parent.name == "__global__"
    assert dirs["inbox"].is_dir()


def test_session_maildir_stays_within_root(fresh_a2a):
    dirs = fresh_a2a.session_maildir("projects/a/b", SID)
    root_resolved = fresh_a2a.A2A_ROOT.resolve()
    for key in ("root", "inbox", "cur", "tmp"):
        assert dirs[key].resolve().is_relative_to(root_resolved)


def test_session_maildir_rejects_bad_session_id(fresh_a2a):
    for bad in ("../escape", "not-a-uuid", "", "a/b"):
        with pytest.raises(ValueError):
            fresh_a2a.session_maildir("areas/Home", bad)


def test_session_maildir_rejects_bad_lib_id(fresh_a2a):
    with pytest.raises(ValueError):
        fresh_a2a.session_maildir("secrets/oops", SID)
    with pytest.raises(ValueError):
        fresh_a2a.session_maildir("projects/../escape", SID)


def test_session_maildir_escape_guard(fresh_a2a, monkeypatch):
    # Force agent_key to emit a traversing key so the resolved session root
    # escapes A2A_ROOT and the containment guard fires.
    monkeypatch.setattr(fresh_a2a, "agent_key", lambda lib_id: "../escape")
    with pytest.raises(ValueError):
        fresh_a2a.session_maildir("areas/Home", SID)


# ── build_envelope / validate_envelope: to_session ────────────────────


def test_build_envelope_to_session_default_null(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    assert env["to_session"] is None


def test_build_envelope_to_session_stamped(fresh_a2a):
    env = fresh_a2a.build_envelope(
        from_lib="global", to_lib="areas/Home", text="x", to_session=SID
    )
    assert env["to_session"] == SID


def test_build_envelope_to_session_blank_is_null(fresh_a2a):
    env = fresh_a2a.build_envelope(
        from_lib="global", to_lib="areas/Home", text="x", to_session="   "
    )
    assert env["to_session"] is None


def test_build_envelope_rejects_malformed_to_session(fresh_a2a):
    for bad in ("not-a-uuid", "../escape", "ABCDEF01-2345-6789-abcd-ef0123456789"):
        with pytest.raises(ValueError):
            fresh_a2a.build_envelope(
                from_lib="global", to_lib="areas/Home", text="x", to_session=bad
            )


def test_validate_envelope_to_session_default_null(fresh_a2a):
    d = _good_dict(fresh_a2a)
    out = fresh_a2a.validate_envelope(d)  # _good_dict has no to_session key
    assert out["to_session"] is None


def test_validate_envelope_to_session_preserved(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["to_session"] = SID
    out = fresh_a2a.validate_envelope(d)
    assert out["to_session"] == SID


def test_validate_envelope_to_session_blank_to_null(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["to_session"] = "   "
    out = fresh_a2a.validate_envelope(d)
    assert out["to_session"] is None


def test_validate_envelope_rejects_malformed_to_session(fresh_a2a):
    d = _good_dict(fresh_a2a)
    d["to_session"] = "bogus-session"
    with pytest.raises(ValueError):
        fresh_a2a.validate_envelope(d)


# ── enqueue(envelope, session=...) ────────────────────────────────────


def _session_inbox(fresh_a2a, lib_id, sid):
    return fresh_a2a.session_maildir(lib_id, sid)["inbox"].resolve()


def test_enqueue_session_kwarg_lands_in_session_inbox(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="targeted")
    path = fresh_a2a.enqueue(env, session=SID)
    assert path.is_file()
    assert path.name == f"{env['id']}.json"
    # Lands in the SESSION inbox, not the agent-level inbox.
    assert path.resolve().parent == _session_inbox(fresh_a2a, "areas/Home", SID)
    agent_inbox = fresh_a2a.maildir_for("areas/Home")["inbox"].resolve()
    assert path.resolve().parent != agent_inbox
    # The agent-level inbox stays empty.
    assert fresh_a2a.list_inbox("areas/Home") == []


def test_enqueue_to_session_envelope_field_routes(fresh_a2a):
    # The envelope's own to_session wins (no kwarg needed).
    env = fresh_a2a.build_envelope(
        from_lib="global", to_lib="areas/Home", text="via-field", to_session=SID
    )
    path = fresh_a2a.enqueue(env)
    assert path.resolve().parent == _session_inbox(fresh_a2a, "areas/Home", SID)


def test_enqueue_to_session_field_wins_over_kwarg(fresh_a2a):
    # Envelope to_session is preferred over the explicit kwarg.
    env = fresh_a2a.build_envelope(
        from_lib="global", to_lib="areas/Home", text="x", to_session=SID
    )
    path = fresh_a2a.enqueue(env, session=SID2)
    assert path.resolve().parent == _session_inbox(fresh_a2a, "areas/Home", SID)


def test_enqueue_without_session_lands_in_agent_inbox(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="agent")
    path = fresh_a2a.enqueue(env)
    agent_inbox = fresh_a2a.maildir_for("areas/Home")["inbox"].resolve()
    assert path.resolve().parent == agent_inbox


def test_enqueue_session_kwarg_rejects_bad_session(fresh_a2a):
    env = fresh_a2a.build_envelope(from_lib="global", to_lib="areas/Home", text="x")
    with pytest.raises(ValueError):
        fresh_a2a.enqueue(env, session="../escape")


def test_enqueue_session_round_trips_via_session_read(fresh_a2a):
    # Mail dropped in a session inbox is claimable by reading the session maildir
    # directly (the CLI's per-session drain path uses the same claim primitive).
    env = fresh_a2a.build_envelope(
        from_lib="global", to_lib="areas/Home", text="hello session", to_session=SID
    )
    fresh_a2a.enqueue(env)
    sdirs = fresh_a2a.session_maildir("areas/Home", SID)
    src = sdirs["inbox"] / f"{env['id']}.json"
    assert src.is_file()
    loaded = json.loads(src.read_text(encoding="utf-8"))
    assert loaded == env
    assert loaded["to_session"] == SID


# ── list_agents: the new `sessions` field ─────────────────────────────


def test_list_agents_sessions_field_lists_all_live(fresh_a2a, monkeypatch):
    # One lib_id with TWO live sessions → both appear in `sessions`, sorted by
    # last_active desc; back-compat fields stay intact.
    summaries = [
        {"id": "s-dom-1", "updated_at": 100.0},
        {"id": "s-dom-2", "updated_at": 300.0},
        {"id": "s-dom-cold", "updated_at": 50.0},
    ]
    meta = {
        "s-dom-1": {"lib_id": "areas/Home"},
        "s-dom-2": {"lib_id": "areas/Home"},
        "s-dom-cold": {"lib_id": "areas/Home"},
    }
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"s-dom-1", "s-dom-2"})
    dom = next(a for a in agents if a["lib_id"] == "areas/Home")
    # `sessions` lists ALL live sessions, sorted by last_active desc.
    assert [s["session_id"] for s in dom["sessions"]] == ["s-dom-2", "s-dom-1"]
    assert [s["last_active"] for s in dom["sessions"]] == [300.0, 100.0]
    # The cold session is excluded from `sessions` (live-only).
    assert "s-dom-cold" not in {s["session_id"] for s in dom["sessions"]}
    # Back-compat fields intact.
    assert dom["warm"] is True
    assert dom["session_id"] == "s-dom-2"  # most-recent of ANY state
    assert dom["last_active"] == 300.0
    assert dom["name"] == "Home"


def test_list_agents_sessions_empty_for_cold_agent(fresh_a2a, monkeypatch):
    summaries = [{"id": "s-praca", "updated_at": 10.0}]
    meta = {"s-praca": {"lib_id": "areas/Work"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids=set())
    praca = next(a for a in agents if a["lib_id"] == "areas/Work")
    assert praca["sessions"] == []
    assert praca["warm"] is False


def test_list_agents_global_has_sessions_field(fresh_a2a, monkeypatch):
    # Even the always-seeded global agent carries a (possibly empty) sessions list.
    _patch_sessions(monkeypatch, fresh_a2a, [], {})
    agents = fresh_a2a.list_agents(live_session_ids=set())
    g = next(a for a in agents if a["lib_id"] == "global")
    assert g["sessions"] == []


def test_list_agents_sessions_no_staging_key_leaks(fresh_a2a, monkeypatch):
    # The private `_live` staging key must be popped — never surface it.
    summaries = [{"id": "s1", "updated_at": 1.0}]
    meta = {"s1": {"lib_id": "areas/Home"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"s1"})
    for a in agents:
        assert "_live" not in a
        assert "sessions" in a


def test_list_agents_global_collects_live_sessions(fresh_a2a, monkeypatch):
    # Two live no-lib_id sessions collapse into global and both land in sessions.
    summaries = [
        {"id": "g1", "updated_at": 10.0},
        {"id": "g2", "updated_at": 20.0},
    ]
    meta = {}  # no lib_id → both global
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"g1", "g2"})
    g = next(a for a in agents if a["lib_id"] == "global")
    assert [s["session_id"] for s in g["sessions"]] == ["g2", "g1"]


# ══════════════════════════════════════════════════════════════════════
#  v2: PARA dir + identity helpers, enriched list_agents, whois
# ══════════════════════════════════════════════════════════════════════


def _write_identity(home: Path, lib_id: str, body: str) -> None:
    """Seed HOME/.orchestrator/agents/<kind>/<rest>/identity.md."""
    kind, rest = lib_id.split("/", 1)
    d = home / ".orchestrator" / "agents" / kind / rest
    d.mkdir(parents=True, exist_ok=True)
    (d / "identity.md").write_text(body, encoding="utf-8")


# ── _para_dir_for ─────────────────────────────────────────────────────


def test_para_dir_for_capitalizes_para_root(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    assert a2a._para_dir_for("areas/Home") == str(tmp_path / "Areas" / "Home")
    assert a2a._para_dir_for("projects/my-project") == str(
        tmp_path / "Projects" / "my-project"
    )
    assert a2a._para_dir_for("resources/Notes") == str(tmp_path / "Resources" / "Notes")
    # Nested rest is preserved.
    assert a2a._para_dir_for("projects/a/b") == str(tmp_path / "Projects" / "a" / "b")


def test_para_dir_for_global_is_home(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    for lib in (None, "", "global", "__global__"):
        assert a2a._para_dir_for(lib) == str(tmp_path)


# ── _identity_for ─────────────────────────────────────────────────────


def test_identity_for_first_paragraph_strips_heading(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    _write_identity(
        tmp_path, "areas/Home",
        "# Dom agent\n\nManages the home — heating + lights.\n\nSecond para here.\n",
    )
    assert a2a._identity_for("areas/Home") == "Manages the home — heating + lights."


def test_identity_for_full_returns_whole_file(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    body = "# Dom agent\n\nFirst para.\n\nSecond para.\n"
    _write_identity(tmp_path, "areas/Home", body)
    full = a2a._identity_for("areas/Home", full=True)
    assert full == body
    assert "Second para." in full


def test_identity_for_missing_or_global_is_empty(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    assert a2a._identity_for("areas/DoesNotExist") == ""
    assert a2a._identity_for("global") == ""
    assert a2a._identity_for(None, full=True) == ""
    assert a2a._identity_for("secrets/oops") == ""


# ── list_agents enrichment ────────────────────────────────────────────


def test_list_agents_enriched_description_dir_title_transcript(
    fresh_a2a, monkeypatch, tmp_path
):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    _write_identity(tmp_path, "areas/Home", "# Dom\n\nHome agent.\n")
    summaries = [{"id": "s-dom", "updated_at": 100.0, "ai_title": "Heating fix"}]
    meta = {"s-dom": {"lib_id": "areas/Home"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"s-dom"})

    dom = next(a for a in agents if a["lib_id"] == "areas/Home")
    assert dom["description"] == "Home agent."
    assert dom["dir"] == str(tmp_path / "Areas" / "Home")
    # Existing back-compat session keys stay.
    sess = dom["sessions"][0]
    assert sess["session_id"] == "s-dom"
    assert sess["last_active"] == 100.0
    # New per-session keys.
    assert sess["title"] == "Heating fix"
    assert sess["transcript"].endswith("s-dom.jsonl")

    # The always-seeded global agent carries dir=~/ and an empty description.
    g = next(a for a in agents if a["lib_id"] == "global")
    assert g["dir"] == str(tmp_path)
    assert g["description"] == ""


def test_list_agents_sessions_stay_live_only_after_enrich(
    fresh_a2a, monkeypatch, tmp_path
):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    summaries = [
        {"id": "s-live", "updated_at": 200.0, "ai_title": "Live"},
        {"id": "s-cold", "updated_at": 100.0, "ai_title": "Cold"},
    ]
    meta = {"s-live": {"lib_id": "areas/Home"}, "s-cold": {"lib_id": "areas/Home"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    agents = fresh_a2a.list_agents(live_session_ids={"s-live"})
    dom = next(a for a in agents if a["lib_id"] == "areas/Home")
    # sessions is LIVE-only (cold session absent) but each live one is enriched.
    assert [s["session_id"] for s in dom["sessions"]] == ["s-live"]
    assert dom["sessions"][0]["title"] == "Live"
    assert dom["sessions"][0]["transcript"].endswith("s-live.jsonl")


# ── whois ─────────────────────────────────────────────────────────────


def test_whois_full_identity_dir_and_all_sessions(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    _write_identity(tmp_path, "areas/Home", "# Dom\n\nFirst para.\n\nSecond para.\n")
    summaries = [
        {"id": "s-live", "updated_at": 200.0, "ai_title": "Live one"},
        {"id": "s-cold", "updated_at": 100.0, "ai_title": "Cold one"},
    ]
    meta = {"s-live": {"lib_id": "areas/Home"}, "s-cold": {"lib_id": "areas/Home"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)

    rec = fresh_a2a.whois("areas/Home", live_session_ids={"s-live"})
    assert rec["lib_id"] == "areas/Home"
    assert rec["name"] == "Home"
    assert rec["dir"] == str(tmp_path / "Areas" / "Home")
    assert rec["warm"] is True
    # FULL identity (both paragraphs), not just the first.
    assert "First para." in rec["identity"] and "Second para." in rec["identity"]
    # ALL sessions (live AND cold), sorted by last_active desc, with a live flag.
    assert [s["session_id"] for s in rec["sessions"]] == ["s-live", "s-cold"]
    assert rec["sessions"][0]["live"] is True
    assert rec["sessions"][1]["live"] is False
    assert rec["sessions"][0]["title"] == "Live one"
    assert rec["sessions"][0]["transcript"].endswith("s-live.jsonl")
    assert rec["sessions"][1]["transcript"].endswith("s-cold.jsonl")


def test_whois_identity_first_para_differs_from_full(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    _write_identity(tmp_path, "areas/Home", "# Dom\n\nOnly this line.\n\nHidden.\n")
    _patch_sessions(monkeypatch, fresh_a2a, [], {})
    # list_agents uses first-para only; whois returns the full file.
    agents = fresh_a2a.list_agents(live_session_ids=set())
    # (Dom has no sessions here, so build a group by asking whois directly.)
    assert a2a._identity_for("areas/Home") == "Only this line."
    rec = fresh_a2a.whois("areas/Home", live_session_ids=set())
    assert "Hidden." in rec["identity"]


def test_whois_global_dir_is_home_and_lists_sessions(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    summaries = [{"id": "g1", "updated_at": 10.0}]  # no meta → global
    _patch_sessions(monkeypatch, fresh_a2a, summaries, {})
    rec = fresh_a2a.whois("global", live_session_ids=set())
    assert rec["lib_id"] == "global"
    assert rec["name"] == "Global"
    assert rec["dir"] == str(tmp_path)
    assert rec["identity"] == ""
    assert [s["session_id"] for s in rec["sessions"]] == ["g1"]
    assert rec["sessions"][0]["live"] is False


def test_whois_unknown_but_valid_lib_id_not_found_shape(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    _patch_sessions(monkeypatch, fresh_a2a, [], {})
    rec = fresh_a2a.whois("areas/Nonexistent", live_session_ids=set())
    # Not-found shape still carries a resolvable dir so a sender can address it.
    assert rec["lib_id"] == "areas/Nonexistent"
    assert rec["dir"] == str(tmp_path / "Areas" / "Nonexistent")
    assert rec["identity"] == ""
    assert rec["warm"] is False
    assert rec["sessions"] == []


def test_whois_invalid_lib_id_returns_graceful_record(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    _patch_sessions(monkeypatch, fresh_a2a, [], {})
    rec = fresh_a2a.whois("secrets/oops", live_session_ids=set())
    assert rec["sessions"] == []
    assert rec["identity"] == ""
    # Unparseable kind → home fallback dir, never raises into the route.
    assert rec["dir"] == str(tmp_path)


def test_whois_only_groups_the_requested_agent(fresh_a2a, monkeypatch, tmp_path):
    monkeypatch.setattr(a2a, "HOME", tmp_path)
    summaries = [
        {"id": "s-dom", "updated_at": 10.0},
        {"id": "s-praca", "updated_at": 20.0},
    ]
    meta = {"s-dom": {"lib_id": "areas/Home"}, "s-praca": {"lib_id": "areas/Work"}}
    _patch_sessions(monkeypatch, fresh_a2a, summaries, meta)
    rec = fresh_a2a.whois("areas/Home", live_session_ids=set())
    assert [s["session_id"] for s in rec["sessions"]] == ["s-dom"]


# ══════════════════════════════════════════════════════════════════════
#  v2 route: /a2a/send is a pure enqueue (no revive / warm / kicker)
# ══════════════════════════════════════════════════════════════════════

from fastapi.testclient import TestClient  # noqa: E402

from orbit import app as app_mod  # noqa: E402
from orbit import orchestrator as orch_mod  # noqa: E402
from orbit import orchestrator_artifacts as artifacts_mod  # noqa: E402
from orbit import orchestrator_meta as meta_mod  # noqa: E402
from orbit import orchestrator_settings as settings_mod  # noqa: E402


@pytest.fixture
def a2a_route_client(tmp_path, monkeypatch):
    """A TestClient with A2A enabled, a known token, and a stub tmux pool.

    Any cold-revive / warm-nudge handler is monkeypatched to explode so a
    regression back to push-delivery fails loudly.
    """
    root = tmp_path / ".orchestrator" / "a2a"
    monkeypatch.setattr(a2a, "A2A_ROOT", root)
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "tok")
    orig_get_flag = settings_mod.get_flag
    monkeypatch.setattr(
        settings_mod, "get_flag",
        lambda name: True if name == "a2a_enabled" else orig_get_flag(name),
    )
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: {"lib_id": "global"})

    class _FakePool:
        async def live_session_ids(self):
            return set()

    monkeypatch.setattr(orch_mod, "_get_tmux_pool", lambda: _FakePool())

    def _boom(*a, **k):
        raise AssertionError("v2 /send must not revive/warm/kick any session")

    monkeypatch.setattr(orch_mod, "_create_session_handler", _boom)
    monkeypatch.setattr(orch_mod, "_warm_session_slot", _boom)
    monkeypatch.setattr(orch_mod, "_post_message_handler", _boom)
    return TestClient(app_mod.create_app(), raise_server_exceptions=True)


def test_send_route_is_pure_enqueue(a2a_route_client):
    r = a2a_route_client.post(
        "/api/orchestrator/a2a/send",
        headers={"x-a2a-token": "tok"},
        json={"to": "areas/Home", "text": "hi", "session_id": SID},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["to"] == "areas/Home"
    # Single delivery value in v2 — the mail is enqueued, not pushed.
    assert body["delivery"] == "enqueued"
    # The envelope really landed in the target maildir (drainable later).
    assert a2a.inbox_has("areas/Home", body["id"]) is True


def test_send_route_session_targeted_still_enqueues(a2a_route_client):
    # A --session no longer 400s on a non-live session — it just enqueues into
    # that session's sub-maildir; delivery stays "enqueued".
    r = a2a_route_client.post(
        "/api/orchestrator/a2a/send",
        headers={"x-a2a-token": "tok"},
        json={"to": "areas/Home", "text": "hi", "session_id": SID, "session": SID2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] == "enqueued"
    # Landed in the SESSION sub-maildir, not the agent-level inbox.
    sinbox = a2a.session_maildir("areas/Home", SID2)["inbox"]
    assert (sinbox / f"{body['id']}.json").is_file()
    assert a2a.list_inbox("areas/Home") == []


def test_send_route_403_when_disabled(a2a_route_client, monkeypatch):
    monkeypatch.setattr(settings_mod, "get_flag", lambda name: False)
    r = a2a_route_client.post(
        "/api/orchestrator/a2a/send",
        headers={"x-a2a-token": "tok"},
        json={"to": "areas/Home", "text": "hi", "session_id": SID},
    )
    assert r.status_code == 403


def test_arm_route_is_gone(a2a_route_client):
    # The /arm POST endpoint was removed in v2 — no handler accepts it now
    # (404 if unmatched, or 405 if only a GET catch-all matches the path).
    r = a2a_route_client.post(
        "/api/orchestrator/a2a/arm",
        headers={"x-a2a-token": "tok"},
        json={"session": SID},
    )
    assert r.status_code in (404, 405)


def test_whois_route_returns_agent_record(a2a_route_client, monkeypatch):
    monkeypatch.setattr(a2a.jsonl_mod, "list_sessions", lambda: [])
    monkeypatch.setattr(a2a.meta_mod, "all_meta", lambda: {})
    r = a2a_route_client.get(
        "/api/orchestrator/a2a/whois", params={"lib_id": "areas/Home"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["agent"]["lib_id"] == "areas/Home"
    assert body["agent"]["sessions"] == []
