"""Nested Sync folder navigation + special-character path handling (issue #84).

Covers share.list_dir at arbitrary depth, names with spaces/unicode and
URL-significant characters (& #), traversal refusal, and the HTTP route end to
end — proving an encodeURIComponent'd segment (%26 for '&') reaches the right
directory instead of being chopped off as a query string.
"""

import pytest

from orbit import share


@pytest.fixture
def sync_tree(tmp_path, monkeypatch):
    root = tmp_path / "Sync"
    monkeypatch.setattr(share, "SYNC_ROOT", root)
    monkeypatch.setattr(share, "SHARE_ROOT", root)
    (root / "A B" / "Zażółć" / "deep").mkdir(parents=True)
    (root / "A B" / "Zażółć" / "deep" / "leaf.md").write_text("leaf")
    (root / "Q&A" / "sub").mkdir(parents=True)
    (root / "H#tag" / "sub").mkdir(parents=True)
    return root


def _names(result):
    return {item["name"] for item in result["items"]}


def test_list_dir_nested_levels(sync_tree):
    assert share.list_dir("")["ok"] is True
    assert {"A B", "Q&A", "H#tag"} <= _names(share.list_dir(""))

    lvl1 = share.list_dir("A B")
    assert lvl1["ok"] is True
    assert _names(lvl1) == {"Zażółć"}

    lvl2 = share.list_dir("A B/Zażółć")
    assert lvl2["ok"] is True
    assert _names(lvl2) == {"deep"}

    lvl3 = share.list_dir("A B/Zażółć/deep")
    assert lvl3["ok"] is True
    assert _names(lvl3) == {"leaf.md"}


def test_list_dir_special_chars(sync_tree):
    assert _names(share.list_dir("Q&A")) == {"sub"}
    assert _names(share.list_dir("H#tag")) == {"sub"}


def test_traversal_blocked(sync_tree):
    with pytest.raises(ValueError):
        share._safe_path("../../etc")


def test_route_nested_and_special(client, tmp_path, monkeypatch):
    root = tmp_path / "Sync"
    monkeypatch.setattr(share, "SYNC_ROOT", root)
    monkeypatch.setattr(share, "SHARE_ROOT", root)
    (root / "A B" / "Zażółć" / "deep").mkdir(parents=True)
    (root / "A B" / "Zażółć" / "deep" / "leaf.md").write_text("leaf")
    (root / "Q&A" / "sub").mkdir(parents=True)

    # %20 spaces + encoded unicode reach the nested directory.
    r = client.get("/api/share/A%20B/Zażółć/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert {i["name"] for i in body["items"]} == {"leaf.md"}

    # encodeURIComponent('Q&A') == 'Q%26A'; the %26 must NOT split a query string.
    r2 = client.get("/api/share/Q%26A")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["ok"] is True
    assert {i["name"] for i in body2["items"]} == {"sub"}
