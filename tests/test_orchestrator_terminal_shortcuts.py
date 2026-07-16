"""Tests for orchestrator_terminal_shortcuts — full-layout store: seeding,
strict per-kind sanitization, caps, migration/discard, and the layout API."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest


@pytest.fixture
def fresh_shortcuts(tmp_path: Path, monkeypatch):
    """Reload the shortcuts module against a tmp file so each test starts clean."""
    from orbit import orchestrator_terminal_shortcuts as mod
    monkeypatch.setattr(mod, "SHORTCUTS_PATH", tmp_path / "terminal_shortcuts.json")
    monkeypatch.setattr(mod, "_data", None)
    return mod


@pytest.fixture
def fresh_settings(tmp_path: Path, monkeypatch):
    from orbit import orchestrator_settings as mod
    monkeypatch.setattr(mod, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(mod, "_data", None)
    return mod


def _run(coro):
    return asyncio.run(coro)


def _ids(layout: dict) -> set[str]:
    out = set()
    for v in layout["layout"]["views"]:
        for b in v["buttons"]:
            out.add(b["id"])
    return out


def _one_view_layout(button: dict) -> dict:
    return {"layout": {"views": [{"id": "v1", "label": "V", "icon": None, "buttons": [button]}]}}


def _first_button(out: dict) -> dict:
    return out["layout"]["views"][0]["buttons"][0]


# ── seed / defaults ─────────────────────────────────────────────────


def test_seed_when_no_file(fresh_shortcuts):
    layout = fresh_shortcuts.get_layout()
    assert layout["version"] == fresh_shortcuts._SCHEMA_VERSION
    assert len(layout["layout"]["views"]) >= 1
    assert "arrow-up" in _ids(layout) and "session-switcher" in _ids(layout)


def test_no_file_written_on_read(fresh_shortcuts):
    fresh_shortcuts.get_layout()
    assert not fresh_shortcuts.SHORTCUTS_PATH.exists()


def test_get_layout_returns_deep_copy(fresh_shortcuts):
    a = fresh_shortcuts.get_layout()
    a["layout"]["views"][0]["buttons"][0]["label"] = "MUTATED"
    a["layout"]["views"][0]["buttons"][0]["payload"]["keyCode"] = 999
    b = fresh_shortcuts.get_layout()
    assert b["layout"]["views"][0]["buttons"][0]["label"] != "MUTATED"
    assert b["layout"]["views"][0]["buttons"][0]["payload"].get("keyCode") != 999


def test_flag_defaults_off(fresh_settings):
    assert fresh_settings.get_settings()["terminal_shortcuts_enabled"] is False


def test_flag_toggles_via_set_settings(fresh_settings):
    assert _run(fresh_settings.set_settings({"terminal_shortcuts_enabled": True}))["terminal_shortcuts_enabled"] is True


# ── persistence / reset ─────────────────────────────────────────────


def test_set_layout_persists_and_round_trips(fresh_shortcuts):
    btn = {"id": "x", "kind": "send-key", "label": "X", "payload": {"code": "KeyX", "keyCode": 88}}
    out = _run(fresh_shortcuts.set_layout(_one_view_layout(btn)))
    assert _ids(out) == {"x"}
    fresh_shortcuts._data = None
    assert _ids(fresh_shortcuts.get_layout()) == {"x"}


def test_set_layout_is_full_replace(fresh_shortcuts):
    _run(fresh_shortcuts.set_layout(_one_view_layout({"id": "a", "kind": "send-key", "payload": {"code": "KeyA", "keyCode": 65}})))
    _run(fresh_shortcuts.set_layout(_one_view_layout({"id": "b", "kind": "send-key", "payload": {"code": "KeyB", "keyCode": 66}})))
    assert _ids(fresh_shortcuts.get_layout()) == {"b"}


def test_reset_restores_defaults(fresh_shortcuts):
    _run(fresh_shortcuts.set_layout(_one_view_layout({"id": "a", "kind": "send-key", "payload": {"code": "KeyA", "keyCode": 65}})))
    out = _run(fresh_shortcuts.reset_layout())
    assert _ids(out) == _ids(fresh_shortcuts.get_layout())
    assert "arrow-up" in _ids(out)


def test_bad_payload_type_raises(fresh_shortcuts):
    with pytest.raises(ValueError):
        _run(fresh_shortcuts.set_layout("not a dict"))


def test_empty_views_reseeds(fresh_shortcuts):
    out = _run(fresh_shortcuts.set_layout({"layout": {"views": []}}))
    # An empty layout is meaningless (blank toolbar) → reseed defaults.
    assert "arrow-up" in _ids(out)


def test_all_buttonless_views_reseed(fresh_shortcuts):
    # Views survive but every button is invalid → no buttons anywhere →
    # reseed (a near-blank toolbar with only structural anchors is useless).
    layout = {"layout": {"views": [
        {"id": "v1", "label": "A", "buttons": [{"id": "b", "kind": "exec-shell", "payload": {}}]},
        {"id": "v2", "label": "B", "buttons": []},
    ]}}
    out = _run(fresh_shortcuts.set_layout(layout))
    assert "arrow-up" in _ids(out)  # reseeded
    assert {v["id"] for v in out["layout"]["views"]} != {"v1", "v2"}


# ── per-kind sanitization ───────────────────────────────────────────


def test_send_key_descriptor_sanitized(fresh_shortcuts):
    btn = {"id": "k", "kind": "send-key", "payload": {
        "key": "a", "code": "KeyA", "keyCode": 65, "which": 65, "ctrlKey": True, "bogus": "x"}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert out["payload"] == {"key": "a", "code": "KeyA", "keyCode": 65, "which": 65, "ctrlKey": True}


def test_send_key_bool_keycode_rejected_button_dropped(fresh_shortcuts):
    btn = {"id": "k", "kind": "send-key", "payload": {"keyCode": True}}
    out = _run(fresh_shortcuts.set_layout(_one_view_layout(btn)))
    # No usable descriptor → button dropped → only view is buttonless → reseed.
    assert "k" not in _ids(out)


def test_send_raw_keeps_control_bytes(fresh_shortcuts):
    btn = {"id": "r", "kind": "send-raw", "payload": {"data": "\\x00v"}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert out["payload"] == {"data": "\\x00v"}
    # decoded form is NUL + 'v'
    assert fresh_shortcuts._decode_raw("\\x00v") == "\x00v"
    assert fresh_shortcuts._decode_raw("a\\nb\\x1bc") == "a\nb\x1bc"


def test_send_raw_rejects_malformed_escape(fresh_shortcuts):
    for bad in ("\\", "\\x", "\\xZZ", "\\q"):
        out = _run(fresh_shortcuts.set_layout(_one_view_layout({"id": "r", "kind": "send-raw", "payload": {"data": bad}})))
        assert "r" not in _ids(out), bad


def test_send_raw_rejects_overlong(fresh_shortcuts):
    big = "a" * (fresh_shortcuts._MAX_RAW_LEN + 1)
    out = _run(fresh_shortcuts.set_layout(_one_view_layout({"id": "r", "kind": "send-raw", "payload": {"data": big}})))
    assert "r" not in _ids(out)


def test_paste_text_caps_and_submit(fresh_shortcuts):
    btn = {"id": "p", "kind": "paste-text", "payload": {"text": "hi", "submit": 1}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert out["payload"] == {"text": "hi", "submit": True}


def test_slash_command_strips_and_validates(fresh_shortcuts):
    btn = {"id": "c", "kind": "slash-command", "payload": {"command": "/compact", "submit": True}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert out["payload"] == {"command": "compact", "submit": True}
    bad = {"id": "c", "kind": "slash-command", "payload": {"command": "rm -rf /"}}
    assert "c" not in _ids(_run(fresh_shortcuts.set_layout(_one_view_layout(bad))))


def test_special_actiontype_whitelist(fresh_shortcuts):
    ok = {"id": "s", "kind": "special", "payload": {"actionType": "microphone"}}
    assert "s" in _ids(_run(fresh_shortcuts.set_layout(_one_view_layout(ok))))
    bad = {"id": "s", "kind": "special", "payload": {"actionType": "exec-shell"}}
    assert "s" not in _ids(_run(fresh_shortcuts.set_layout(_one_view_layout(bad))))


def test_modifier_whitelist(fresh_shortcuts):
    ok = {"id": "m", "kind": "modifier", "payload": {"modifier": "ctrlKey"}}
    assert "m" in _ids(_run(fresh_shortcuts.set_layout(_one_view_layout(ok))))
    bad = {"id": "m", "kind": "modifier", "payload": {"modifier": "hyperKey"}}
    assert "m" not in _ids(_run(fresh_shortcuts.set_layout(_one_view_layout(bad))))


def test_unknown_kind_dropped(fresh_shortcuts):
    bad = {"id": "z", "kind": "exec-shell", "payload": {"cmd": "rm"}}
    assert "z" not in _ids(_run(fresh_shortcuts.set_layout(_one_view_layout(bad))))


# ── caps / structure ────────────────────────────────────────────────


def test_view_cap_enforced(fresh_shortcuts):
    views = [{"id": f"v{i}", "label": "V", "buttons": [{"id": f"b{i}", "kind": "modifier", "payload": {"modifier": "ctrlKey"}}]} for i in range(40)]
    out = _run(fresh_shortcuts.set_layout({"layout": {"views": views}}))
    assert len(out["layout"]["views"]) == fresh_shortcuts._MAX_VIEWS


def test_global_button_cap_enforced(fresh_shortcuts):
    btns = [{"id": f"b{i}", "kind": "modifier", "payload": {"modifier": "ctrlKey"}} for i in range(300)]
    views = [{"id": f"v{i}", "label": "V", "buttons": btns[i * 30:(i + 1) * 30]} for i in range(10)]
    out = _run(fresh_shortcuts.set_layout({"layout": {"views": views}}))
    assert len(_ids(out)) <= fresh_shortcuts._MAX_BUTTONS


def test_duplicate_view_ids_deduped(fresh_shortcuts):
    views = [
        {"id": "dup", "label": "A", "buttons": [{"id": "a", "kind": "modifier", "payload": {"modifier": "ctrlKey"}}]},
        {"id": "dup", "label": "B", "buttons": [{"id": "b", "kind": "modifier", "payload": {"modifier": "altKey"}}]},
    ]
    out = _run(fresh_shortcuts.set_layout({"layout": {"views": views}}))
    assert len(out["layout"]["views"]) == 1


def test_id_length_clamped(fresh_shortcuts):
    btn = {"id": "a" * 200, "kind": "modifier", "payload": {"modifier": "ctrlKey"}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert len(out["id"]) == fresh_shortcuts._MAX_ID_LEN


def test_label_and_hint_capped(fresh_shortcuts):
    btn = {"id": "a", "kind": "modifier", "label": "L" * 100, "hint": "H" * 300, "payload": {"modifier": "ctrlKey"}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert len(out["label"]) == fresh_shortcuts._MAX_LABEL_LEN
    assert len(out["hint"]) == fresh_shortcuts._MAX_HINT_LEN


def test_pinned_and_hidden_only_true_kept(fresh_shortcuts):
    btn = {"id": "a", "kind": "modifier", "pinned": True, "hidden": False, "payload": {"modifier": "ctrlKey"}}
    out = _first_button(_run(fresh_shortcuts.set_layout(_one_view_layout(btn))))
    assert out.get("pinned") is True and "hidden" not in out


# ── migration / hostile file on read ────────────────────────────────


def test_old_sparse_format_discarded_and_reseeded(fresh_shortcuts):
    fresh_shortcuts.SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh_shortcuts.SHORTCUTS_PATH.write_text(json.dumps({"overrides": {"enter": {"hidden": True}}}), encoding="utf-8")
    fresh_shortcuts._data = None
    assert "arrow-up" in _ids(fresh_shortcuts.get_layout())  # reseeded


def test_wrong_version_discarded(fresh_shortcuts):
    fresh_shortcuts.SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh_shortcuts.SHORTCUTS_PATH.write_text(json.dumps({"version": 99, "layout": {"views": []}}), encoding="utf-8")
    fresh_shortcuts._data = None
    assert "arrow-up" in _ids(fresh_shortcuts.get_layout())


def test_corrupt_file_seeds(fresh_shortcuts):
    fresh_shortcuts.SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh_shortcuts.SHORTCUTS_PATH.write_text("{not json", encoding="utf-8")
    fresh_shortcuts._data = None
    assert "arrow-up" in _ids(fresh_shortcuts.get_layout())


def test_oversized_file_rejected(fresh_shortcuts):
    fresh_shortcuts.SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh_shortcuts.SHORTCUTS_PATH.write_text(" " * (fresh_shortcuts._MAX_FILE_BYTES + 1), encoding="utf-8")
    fresh_shortcuts._data = None
    assert "arrow-up" in _ids(fresh_shortcuts.get_layout())


def test_hand_edited_v1_file_resanitized_on_read(fresh_shortcuts):
    fresh_shortcuts.SHORTCUTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    hostile = {"version": 1, "layout": {"views": [{"id": "v", "label": "V", "buttons": [
        {"id": "good", "kind": "send-key", "payload": {"code": "KeyA", "keyCode": 65}},
        {"id": "bad", "kind": "exec-shell", "payload": {"cmd": "rm"}},
    ]}]}}
    fresh_shortcuts.SHORTCUTS_PATH.write_text(json.dumps(hostile), encoding="utf-8")
    fresh_shortcuts._data = None
    ids = _ids(fresh_shortcuts.get_layout())
    assert "good" in ids and "bad" not in ids


def test_esc_and_tab_seeded_pinned_per_view(fresh_shortcuts):
    views = fresh_shortcuts.get_layout()["layout"]["views"]
    by_id = {v["id"]: {b["id"]: b for b in v["buttons"]} for v in views}
    # Esc on every view; Tab only on Akcje + Nawigacja (matches v1 placement).
    for vid in ("actions", "nav", "sessions", "special"):
        assert "esc" in by_id[vid], vid
        assert by_id[vid]["esc"].get("pinned") is True
    assert "tab" in by_id["actions"] and "tab" in by_id["nav"]
    assert "tab" not in by_id["sessions"] and "tab" not in by_id["special"]
    assert by_id["nav"]["esc"]["payload"]["keyCode"] == 27
    assert by_id["nav"]["tab"]["payload"]["keyCode"] == 9


# ── drift guard: DEFAULT_LAYOUT vs the frontend fallback arrays ──────


def test_default_layout_matches_frontend_arrays(fresh_shortcuts):
    """The Python seed and the frontend literal arrays (kept as the flag-off
    fallback) are the only intentional duplication — guard their button ids."""
    jsx = (Path(__file__).resolve().parents[1]
           / "src/orbit/static/orchestrator-terminal-preview.jsx").read_text(encoding="utf-8")
    section = jsx.split("const _NAV_KEYS", 1)[1].split("const _SOFT_VIEWS", 1)[0]
    frontend_ids = set(re.findall(r"id:\s*'([a-z0-9-]+)'", section))
    seed_ids = _ids(fresh_shortcuts.get_layout())
    # session-switcher / esc / tab are synthetic seed buttons (the dynamic Sesje
    # view + the formerly-structural Esc/Tab anchors), not array entries;
    # everything else must match the frontend fallback arrays 1:1.
    assert seed_ids - {"session-switcher", "esc", "tab"} == frontend_ids
