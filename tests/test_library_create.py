"""Pure-helper tests for project-create plumbing.

Covers the bits we can exercise without touching git/gh subprocesses or the
filesystem outside ``tmp_path``: README generation always emits a non-empty
body, and the GitHub repo-name slug strips characters GitHub rejects.
"""
from __future__ import annotations

import pytest

from orbit import library


def test_render_readme_uses_description_when_present():
    out = library._render_readme("My Project", "Does stuff.")
    assert out.startswith("# My Project\n\nDoes stuff.\n")


def test_render_readme_falls_back_to_default_when_description_blank():
    out = library._render_readme("Empty Proj", "")
    assert "# Empty Proj" in out
    # The fallback paragraph must mention the dashboard so the user knows
    # where the placeholder text came from.
    assert "orbit" in out


def test_render_readme_strips_description_whitespace():
    out = library._render_readme("X", "   surrounded   ")
    assert "surrounded" in out
    assert "   surrounded   " not in out


@pytest.mark.parametrize("raw,expected", [
    ("FIFA WC26 Family Game", "FIFA-WC26-Family-Game"),
    ("my project!!", "my-project"),
    ("already-clean.repo_1", "already-clean.repo_1"),
    ("   leading and trailing   ", "leading-and-trailing"),
    ("???", "project"),  # fallback for nothing-legal-left
    ("a/b/c", "a-b-c"),  # slashes are illegal in repo names
])
def test_gh_repo_name_from(raw, expected):
    assert library._gh_repo_name_from(raw) == expected


def test_validate_name_rejects_spaces():
    """Regression for 2026-05-17: "FIFA WC26 Family Game" was accepted by
    the old regex and produced URLs like /projects/FIFA%20WC26%20Family%20Game
    plus shell paths needing quoting on every git/gh invocation.
    """
    with pytest.raises(ValueError, match="invalid characters"):
        library._validate_name("FIFA WC26 Family Game")


def test_validate_name_accepts_dashes_underscores_dots():
    assert library._validate_name("FIFA-WC26-Family-Game") == "FIFA-WC26-Family-Game"
    assert library._validate_name("my_project.v2") == "my_project.v2"


def test_create_project_writes_readme_even_without_description(tmp_path, monkeypatch):
    """Regression for 2026-05-17: empty `.library.json`-only dirs were
    invisible to discovery. Now every new project has a README.md so even
    if the user skips ``git init`` the initial commit isn't empty.
    """
    monkeypatch.setattr(library, "PROJECTS", tmp_path)
    monkeypatch.setattr(library, "HOME", tmp_path.parent)
    res = library.create_project("Solo-Project", description="")
    assert res["ok"]
    readme = tmp_path / "Solo-Project" / "README.md"
    assert readme.exists()
    assert "# Solo-Project" in readme.read_text(encoding="utf-8")
