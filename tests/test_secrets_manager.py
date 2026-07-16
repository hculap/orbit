"""Focused unit tests for the env/secrets backend.

Covers:
* .env round-trip with quotes / comments / blanks / single-quoted /
  double-quoted with internal '=', and rejection of multi-line values.
* mask_value shapes for short / long / binary / ssh.
* captcha issue/verify happy-path, expiry (TTL), single-use, wrong code,
  wrong token.
* _validate_scope rejects path traversal, unknown kinds, malformed lib_id.
* authorized_keys last-line guard logic (delete-the-only-line refused).
* Atomic-write fault: monkeypatch os.replace to raise → original file
  untouched and tempfile cleaned up.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from orbit import secrets_captcha, secrets_manager as sm, secrets_paths


# ── .env round-trip ────────────────────────────────────────────────


def test_parse_env_basic_kv(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\nBAZ=qux\n")
    values, lines = sm.parse_env(p)
    assert values == {"FOO": "bar", "BAZ": "qux"}
    assert [l.kind for l in lines] == ["kv", "kv"]


def test_parse_env_preserves_comments_and_blanks(tmp_path: Path):
    src = "# header\n\nFOO=1\n# inline\nBAR=2\n"
    p = tmp_path / ".env"
    p.write_text(src)
    values, lines = sm.parse_env(p)
    assert values == {"FOO": "1", "BAR": "2"}
    assert [l.kind for l in lines] == ["comment", "comment", "kv", "comment", "kv"]
    rebuilt = sm.serialize_env(values, lines)
    assert rebuilt == src


def test_parse_env_double_quoted_with_internal_equals(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text('TOKEN="aa=bb=cc"\nESC="line1\\nline2"\n')
    values, _ = sm.parse_env(p)
    assert values["TOKEN"] == "aa=bb=cc"
    assert values["ESC"] == "line1\nline2"


def test_parse_env_single_quoted_is_literal(tmp_path: Path):
    p = tmp_path / ".env"
    # Single-quoted values do NOT process escapes
    p.write_text("RAW='line1\\nline2'\n")
    values, _ = sm.parse_env(p)
    assert values["RAW"] == "line1\\nline2"


def test_parse_env_unquoted_strips_inline_comment(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("PORT=8080  # trailing\n")
    values, _ = sm.parse_env(p)
    assert values["PORT"] == "8080"


def test_parse_env_multi_line_value_rejected(tmp_path: Path):
    p = tmp_path / ".env"
    # Unterminated double quote → multi-line attempt
    p.write_text('K="line1\nline2"\n')
    with pytest.raises(ValueError, match="multi-line"):
        sm.parse_env(p)


def test_write_env_round_trip_preserves_order(tmp_path: Path):
    p = tmp_path / ".env"
    src = "# comment\nA=1\nB=two words\n"
    p.write_text(src)
    values, lines = sm.parse_env(p)
    values["A"] = "10"
    values["C"] = "new"
    sm.write_env(p, values, lines)
    rebuilt = p.read_text()
    # Comment + A first, then B, then appended C
    assert "# comment" in rebuilt
    assert rebuilt.index("A=10") < rebuilt.index("B=") < rebuilt.index("C=new")


def test_write_env_quotes_values_with_spaces_or_special(tmp_path: Path):
    p = tmp_path / ".env"
    sm.write_env(p, {"X": "hello world", "Y": "plain"}, [])
    text = p.read_text()
    assert 'X="hello world"' in text
    assert "Y=plain" in text


def test_write_env_default_mode_is_0600(tmp_path: Path):
    p = tmp_path / ".env"
    sm.write_env(p, {"K": "v"}, [])
    assert (p.stat().st_mode & 0o777) == 0o600


def test_write_env_preserves_existing_mode(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("K=old\n")
    os.chmod(p, 0o640)
    sm.write_env(p, {"K": "new"}, [])
    assert (p.stat().st_mode & 0o777) == 0o640


# ── init helpers ───────────────────────────────────────────────────


def test_init_env_file_creates_at_0600(tmp_path: Path):
    p = tmp_path / ".env"
    assert sm.init_env_file(p) is True
    assert p.is_file()
    assert p.read_text() == ""
    assert (p.stat().st_mode & 0o777) == 0o600


def test_init_env_file_is_idempotent(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("K=v\n")
    os.chmod(p, 0o640)
    assert sm.init_env_file(p) is False
    assert p.read_text() == "K=v\n"
    assert (p.stat().st_mode & 0o777) == 0o640  # untouched


def test_init_env_file_rejects_directory(tmp_path: Path):
    p = tmp_path / ".env"
    p.mkdir()
    with pytest.raises(ValueError):
        sm.init_env_file(p)


def test_init_secrets_dir_creates_at_0700(tmp_path: Path):
    d = tmp_path / ".secrets"
    assert sm.init_secrets_dir(d) is True
    assert d.is_dir()
    assert (d.stat().st_mode & 0o777) == 0o700


def test_init_secrets_dir_is_idempotent(tmp_path: Path):
    d = tmp_path / ".secrets"
    d.mkdir(mode=0o755)
    assert sm.init_secrets_dir(d) is False
    assert (d.stat().st_mode & 0o777) == 0o755  # mode untouched


def test_init_secrets_dir_rejects_file(tmp_path: Path):
    p = tmp_path / ".secrets"
    p.write_text("oops")
    with pytest.raises(ValueError):
        sm.init_secrets_dir(p)


# ── masking ────────────────────────────────────────────────────────


def test_mask_value_short_token():
    assert sm.mask_value("sk-proj-abcdcv2D", kind="env") == "••••cv2D"


def test_mask_value_very_short():
    assert sm.mask_value("ab", kind="env") == "••"


def test_mask_value_empty():
    assert sm.mask_value("", kind="env") == "(empty)"


def test_mask_value_binary_payload():
    raw = b"\x00\x01\x02\x03" * 8
    masked = sm.mask_value(raw, kind="binary")
    assert masked == f"<<<{len(raw)} bytes>>>"


def test_mask_value_ssh_uses_sha256():
    masked = sm.mask_value("ssh-ed25519 AAAA…", kind="ssh")
    assert masked.startswith("SHA256:")
    assert len(masked) > len("SHA256:")


# ── captcha ────────────────────────────────────────────────────────


def test_captcha_issue_then_verify_success():
    secrets_captcha.reset_for_tests()
    challenge = secrets_captcha.issue()
    assert set(challenge.keys()) >= {"token", "code", "expires_at"}
    assert len(challenge["code"]) == secrets_captcha.CODE_LEN
    assert secrets_captcha.verify(challenge["token"], challenge["code"]) is True


def test_captcha_single_use():
    secrets_captcha.reset_for_tests()
    c = secrets_captcha.issue()
    assert secrets_captcha.verify(c["token"], c["code"]) is True
    # second attempt with same token must fail (consumed)
    assert secrets_captcha.verify(c["token"], c["code"]) is False


def test_captcha_wrong_code():
    secrets_captcha.reset_for_tests()
    c = secrets_captcha.issue()
    assert secrets_captcha.verify(c["token"], "ZZZZZ") is False
    # Token NOT consumed by a wrong-code attempt — correct one still works
    assert secrets_captcha.verify(c["token"], c["code"]) is True


def test_captcha_unknown_token():
    secrets_captcha.reset_for_tests()
    assert secrets_captcha.verify("not-a-token", "ABCDE") is False


def test_captcha_expired(monkeypatch: pytest.MonkeyPatch):
    secrets_captcha.reset_for_tests()
    base = time.time()
    monkeypatch.setattr(secrets_captcha, "_now", lambda: base)
    c = secrets_captcha.issue()
    monkeypatch.setattr(secrets_captcha, "_now", lambda: base + secrets_captcha.TTL_SECONDS + 1)
    assert secrets_captcha.verify(c["token"], c["code"]) is False


def test_captcha_alphabet_excludes_ambiguous():
    forbidden = set("OIL01")
    for _ in range(50):
        c = secrets_captcha.issue()
        assert not (set(c["code"]) & forbidden)


# ── secrets_paths.parse_scope / resolve ──────────────────────────────


def test_parse_scope_global():
    assert secrets_paths.parse_scope("global") == ("global", None)


def test_parse_scope_areas():
    assert secrets_paths.parse_scope("areas/health") == ("areas", "health")


def test_parse_scope_projects_with_group():
    assert secrets_paths.parse_scope("projects/work/foo") == ("projects", "work/foo")


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "garbage",
    "areas",
    "areas/",
    "projects/",
    "resources/x",   # not in the supported set
])
def test_parse_scope_rejects(bad: str):
    with pytest.raises(ValueError):
        secrets_paths.parse_scope(bad)


def test_resolve_rejects_path_traversal():
    with pytest.raises(ValueError):
        secrets_paths.resolve("areas/../etc")


def test_resolve_rejects_separators_in_lib_id():
    with pytest.raises(ValueError):
        secrets_paths.resolve("areas/foo/bar/baz")


def test_resolve_global_has_ssh_dir():
    paths = secrets_paths.resolve("global")
    assert paths.ssh_dir is not None
    assert paths.kind == "global"


def test_require_global_rejects_non_global(tmp_path: Path):
    paths = secrets_paths.ScopePaths(
        kind="areas",
        lib_id="foo",
        env=tmp_path / ".env",
        secrets_dir=tmp_path / ".secrets",
        ssh_dir=None,
    )
    with pytest.raises(ValueError):
        secrets_paths.require_global(paths)


# ── authorized_keys last-line guard ──────────────────────────────────


def test_delete_line_refuses_last_when_guarded(tmp_path: Path):
    p = tmp_path / "authorized_keys"
    p.write_text("ssh-ed25519 AAAA only-line\n")
    with pytest.raises(ValueError, match="refusing"):
        sm.delete_line(p, 0, refuse_last=True)
    # File untouched
    assert p.read_text() == "ssh-ed25519 AAAA only-line\n"


def test_delete_line_allows_last_when_not_guarded(tmp_path: Path):
    p = tmp_path / "known_hosts"
    p.write_text("github.com ssh-rsa AAAA only\n")
    sm.delete_line(p, 0, refuse_last=False)
    assert p.read_text() == ""


def test_delete_line_keeps_other_lines(tmp_path: Path):
    p = tmp_path / "authorized_keys"
    p.write_text("ssh-ed25519 AAAA one\nssh-ed25519 BBBB two\n")
    sm.delete_line(p, 0, refuse_last=True)
    assert p.read_text() == "ssh-ed25519 BBBB two\n"


# ── atomic write fault injection ────────────────────────────────────


def test_atomic_write_fault_leaves_original_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / ".env"
    p.write_text("ORIG=value\n")
    original_mode = p.stat().st_mode & 0o777

    boom = RuntimeError("simulated replace failure")

    real_replace = os.replace

    def fake_replace(src, dst):
        # Trigger the failure path on our target only
        if str(dst) == str(p):
            raise boom
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(RuntimeError):
        sm.write_env(p, {"ORIG": "new", "EXTRA": "v"}, [])

    # Original content + mode untouched
    assert p.read_text() == "ORIG=value\n"
    assert (p.stat().st_mode & 0o777) == original_mode

    # No leftover .secret.*.tmp turds in the parent dir
    leftovers = [child.name for child in p.parent.iterdir() if child.name.startswith(".secret.")]
    assert leftovers == []


# ── env key validation ─────────────────────────────────────────────


@pytest.mark.parametrize("bad_key", [
    "",
    "1FOO",
    "FOO BAR",
    "foo-bar",
    "FOO/BAR",
])
def test_validate_env_key_rejects(bad_key: str):
    with pytest.raises(ValueError):
        sm.validate_env_key(bad_key)


def test_validate_env_key_accepts_uppercase_underscore_digits():
    assert sm.validate_env_key("MY_TOKEN_42") == "MY_TOKEN_42"


# ── secret-file name validation ────────────────────────────────────


@pytest.mark.parametrize("bad_name", [
    "",
    ".",
    "..",
    "..hidden",
    ".dotfile",
    "with/slash",
    "with\\back",
])
def test_validate_secret_file_name_rejects(bad_name: str):
    with pytest.raises(ValueError):
        sm.validate_secret_file_name(bad_name)


def test_validate_secret_file_name_accepts_typical():
    assert sm.validate_secret_file_name("service-account.json") == "service-account.json"


# ── round-trip via secrets-dir CRUD ────────────────────────────────


def test_write_secret_file_default_mode(tmp_path: Path):
    info = sm.write_secret_file(tmp_path, "creds.json", b'{"k":"v"}')
    assert info.mode == 0o600
    assert info.size == len(b'{"k":"v"}')
    assert sm.read_secret_file(tmp_path, "creds.json") == b'{"k":"v"}'


def test_delete_secret_file_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        sm.delete_secret_file(tmp_path, "nope")


def test_list_secret_files_skips_dotfiles(tmp_path: Path):
    sm.write_secret_file(tmp_path, "real.txt", b"hi")
    (tmp_path / ".hidden").write_bytes(b"x")
    listing = sm.list_secret_files(tmp_path)
    assert [info.name for info in listing] == ["real.txt"]
