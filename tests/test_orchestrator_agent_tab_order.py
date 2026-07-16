"""Unit tests for the agent-tab custom-order store."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Fresh module instance pointed at a temp order file."""
    mod = importlib.import_module("orbit.orchestrator_agent_tab_order")
    monkeypatch.setattr(mod, "ORDER_PATH", tmp_path / "agent_tab_order.json")
    return mod


def test_empty_when_nothing_saved(store):
    assert store.get_order() == []


def test_round_trip_preserves_order(store):
    keys = ["@Global", "areas/Finance", "projects/my-project"]
    assert store.set_order(keys) == keys
    assert store.get_order() == keys


def test_set_order_dedupes_preserving_first_position(store):
    saved = store.set_order(["a", "b", "a", "c", "b"])
    assert saved == ["a", "b", "c"]


def test_set_order_drops_non_strings_and_bad_keys(store):
    saved = store.set_order(["areas/Work", 42, None, "x/" * 40, "ctrl\x01key", "@Global"])
    # 42/None dropped (non-str); the 80-char key dropped (>64); the control-char
    # key dropped (regex); valid ones kept in order.
    assert saved == ["areas/Work", "@Global"]


def test_set_order_allows_spaces_and_unicode_keys(store):
    # A cwd-rooted agent with no lib_id keys to "@<Human Name>" which can contain
    # spaces — its reorder must persist, not get silently dropped.
    saved = store.set_order(["@My Project", "areas/Health", "@Wystąpienia"])
    assert saved == ["@My Project", "areas/Health", "@Wystąpienia"]


def test_set_order_caps_at_256(store):
    saved = store.set_order([f"k{i}" for i in range(300)])
    assert len(saved) == 256
    assert saved[0] == "k0" and saved[-1] == "k255"


def test_set_order_non_list_is_empty(store):
    assert store.set_order("nope") == []
    assert store.set_order(None) == []
    assert store.get_order() == []


def test_oversized_file_ignored(store, monkeypatch):
    store.set_order(["a", "b"])
    monkeypatch.setattr(store, "_MAX_FILE_BYTES", 1)  # smaller than any real file
    assert store.get_order() == []


def test_corrupt_file_ignored(store):
    store.ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.ORDER_PATH.write_text("{ not json", encoding="utf-8")
    assert store.get_order() == []


def test_wrong_schema_version_ignored(store):
    import json
    store.ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.ORDER_PATH.write_text(json.dumps({"version": 99, "order": ["a"]}), encoding="utf-8")
    assert store.get_order() == []
