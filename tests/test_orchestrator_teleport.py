"""Unit + integration tests for session teleport (issue #91).

Mirrors the BDD scenarios in tests/features/session_teleport.feature. The
on-disk projects root is redirected to a tmp dir via monkeypatching
``jsonl_mod._PROJECTS_ROOT`` so nothing touches the real ~/.claude. Sidecar
writes are captured by stubbing ``meta_mod.set_meta`` / ``get_meta`` so no real
~/.orchestrator state is mutated.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orbit import app as app_mod
from orbit import orchestrator_teleport as teleport
from orbit import orchestrator_jsonl as jsonl_mod
from orbit import orchestrator_meta as meta_mod
from orbit import orchestrator_artifacts as artifacts_mod
from orbit.discovery import HOME

SRC = "src-uuid-1111"


def _run(coro):
    return asyncio.run(coro)


def _line(uuid_: str, *, role: str = "user", text: str = "hi", cwd: str = "/home/testuser", sid: str = SRC) -> dict:
    return {
        "type": role,
        "uuid": uuid_,
        "sessionId": sid,
        "cwd": cwd,
        "gitBranch": "main",
        "message": {"role": role, "content": text},
        "timestamp": "2026-06-16T00:00:00.000Z",
    }


def _write_session(root: Path, slug: str, sid: str, lines: list[dict]) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(jsonl_mod, "_PROJECTS_ROOT", root)
    monkeypatch.setattr(jsonl_mod, "CLAUDE_PROJECTS_DIR", root / "-home-testuser")
    monkeypatch.setattr(jsonl_mod, "invalidate_cache", lambda *a, **k: None)
    return root


@pytest.fixture
def meta_capture(monkeypatch):
    """Capture set_meta kwargs; serve empty defaults from get_meta."""
    captured: dict = {}

    async def _fake_set(session_id, **kwargs):
        captured["session_id"] = session_id
        captured.update(kwargs)

    monkeypatch.setattr(meta_mod, "set_meta", _fake_set)
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: dict(meta_mod._DEFAULT))
    return captured


# ── cwd_to_slug ────────────────────────────────────────────────────


def test_cwd_to_slug_encoding():
    assert teleport.cwd_to_slug("/home/testuser") == "-home-testuser"
    assert teleport.cwd_to_slug("/home/testuser/Projects/orbit") == (
        "-home-testuser-Projects-orbit"
    )
    assert teleport.cwd_to_slug(Path("/home/testuser")) == "-home-testuser"


def test_cwd_to_slug_matches_claude_for_nonalnum_chars():
    # Claude Code replaces EVERY non-alphanumeric char with "-", not just "/".
    # Proof on disk: /home/testuser/.orchestrator/... → -home-testuser--orchestrator-...
    assert teleport.cwd_to_slug("/home/testuser/.orchestrator/scratch") == (
        "-home-testuser--orchestrator-scratch"
    )
    assert teleport.cwd_to_slug("/home/testuser/Projects/my.proj_v2") == (
        "-home-testuser-Projects-my-proj-v2"
    )


# ── EXPORT ─────────────────────────────────────────────────────────


def test_export_envelope_shape(projects_root, monkeypatch):
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: {
        **meta_mod._DEFAULT, "cwd": "/home/testuser/Projects/x", "lib_id": "projects/x",
        "model": "opus", "title": "My session",
    })
    _write_session(projects_root, "-home-testuser-Projects-x", SRC,
                   [_line("a"), _line("b", text="bye")])
    env = teleport.export_session(SRC)
    assert env["version"] == teleport.ENVELOPE_VERSION
    assert env["kind"] == teleport.ENVELOPE_KIND
    assert env["source_session_id"] == SRC
    assert env["source_cwd"] == "/home/testuser/Projects/x"
    assert env["source_lib_id"] == "projects/x"
    assert env["model"] == "opus"
    assert env["title"] == "My session"
    assert isinstance(env["exported_at"], float)
    assert env["msg_count"] == 2
    assert [l["uuid"] for l in env["transcript"]] == ["a", "b"]


def test_export_unknown_session_raises_filenotfound(projects_root):
    with pytest.raises(FileNotFoundError):
        teleport.export_session("nope-nope-nope")


def test_export_rejects_unsafe_id(projects_root):
    with pytest.raises(ValueError):
        teleport.export_session("../../etc/passwd")


def test_export_merges_and_dedupes_across_slugs(projects_root, monkeypatch):
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: dict(meta_mod._DEFAULT))
    # Canonical (bigger) file holds 3 lines; a stub under another slug repeats line "a".
    _write_session(projects_root, "-home-testuser-Projects-x", SRC,
                   [_line("a"), _line("b"), _line("c")])
    _write_session(projects_root, "-home-testuser", SRC, [_line("a")])
    env = teleport.export_session(SRC)
    uuids = [l["uuid"] for l in env["transcript"]]
    assert uuids.count("a") == 1
    assert set(uuids) == {"a", "b", "c"}
    # canonical ordering preserved (largest file wins)
    assert uuids[:3] == ["a", "b", "c"]


def test_export_cwd_falls_back_to_transcript_when_meta_blank(projects_root, monkeypatch):
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: dict(meta_mod._DEFAULT))
    _write_session(projects_root, "-home-testuser-Projects-y", SRC,
                   [_line("a", cwd="/home/testuser/Projects/y")])
    env = teleport.export_session(SRC)
    assert env["source_cwd"] == "/home/testuser/Projects/y"


# ── IMPORT ─────────────────────────────────────────────────────────


def _envelope(lines: list[dict] | None = None, **over) -> dict:
    base = {
        "version": teleport.ENVELOPE_VERSION,
        "kind": teleport.ENVELOPE_KIND,
        "source_session_id": SRC,
        "source_cwd": "/home/testuser/Projects/x",
        "source_lib_id": "projects/x",
        "model": None,
        "title": None,
        "exported_at": 1.0,
        "msg_count": 2,
        "transcript": lines or [_line("a"), _line("b")],
    }
    base.update(over)
    return base


def test_import_mints_session_and_rewrites_paths(projects_root, meta_capture, monkeypatch, tmp_path):
    target = tmp_path / "agent"
    target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    res = _run(teleport.import_session(_envelope(), lib_id="projects/agent", new_id="new-uuid-9999"))
    assert res["ok"] is True
    assert res["new_session_id"] == "new-uuid-9999"
    slug = teleport.cwd_to_slug(target)
    dest = projects_root / slug / "new-uuid-9999.jsonl"
    assert dest.is_file()
    lines = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert all(l["sessionId"] == "new-uuid-9999" for l in lines)
    assert all(l["cwd"] == str(target) for l in lines)
    # message content preserved
    assert lines[0]["message"]["content"] == "hi"


def test_import_stamps_sidecar_provenance(projects_root, meta_capture, monkeypatch, tmp_path):
    target = tmp_path / "agent"
    target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    _run(teleport.import_session(_envelope(), lib_id="projects/agent", new_id="nid"))
    assert meta_capture["session_id"] == "nid"
    assert meta_capture["lib_id"] == "projects/agent"
    assert meta_capture["cwd"] == str(target)
    assert meta_capture["teleported_from"] == SRC


def test_import_unsaved_by_default_no_title(projects_root, meta_capture, monkeypatch, tmp_path):
    target = tmp_path / "agent"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    _run(teleport.import_session(_envelope(), lib_id="projects/agent", new_id="nid"))
    assert meta_capture.get("title") in (None,)  # not stamped
    assert meta_capture.get("title_manual") in (None,)


def test_import_title_and_model_survive(projects_root, meta_capture, monkeypatch, tmp_path):
    target = tmp_path / "agent"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    _run(teleport.import_session(_envelope(), lib_id="projects/agent",
                                 title="Resumed elsewhere", model="opus", new_id="nid"))
    assert meta_capture["title"] == "Resumed elsewhere"
    assert meta_capture["title_manual"] is True
    assert meta_capture["model"] == "opus"


def test_import_global_lands_under_home(projects_root, meta_capture):
    res = _run(teleport.import_session(_envelope(), lib_id="", new_id="gid"))
    assert res["cwd"] == str(HOME)
    dest = projects_root / teleport.cwd_to_slug(HOME) / "gid.jsonl"
    assert dest.is_file()
    assert meta_capture["lib_id"] == ""  # cleared → global


def test_import_rejects_bad_envelope(projects_root, meta_capture):
    for bad in [
        "not a dict",
        {"version": 999, "kind": teleport.ENVELOPE_KIND, "transcript": [{}]},
        {"version": 1, "kind": "wrong", "transcript": [{}]},
        _envelope(transcript=[]),         # empty
        _envelope(transcript="nope"),     # not a list
        _envelope(transcript=["x"]),      # not dict lines
    ]:
        with pytest.raises(ValueError):
            _run(teleport.import_session(bad, lib_id="", new_id="z"))


def test_import_rejects_unknown_agent(projects_root, meta_capture, monkeypatch):
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: None)
    with pytest.raises(ValueError):
        _run(teleport.import_session(_envelope(), lib_id="projects/ghost", new_id="z"))


def test_import_rejects_bad_model(projects_root, meta_capture, monkeypatch, tmp_path):
    target = tmp_path / "agent"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    with pytest.raises(ValueError):
        _run(teleport.import_session(_envelope(), lib_id="projects/agent",
                                     model="gpt4", new_id="z"))


# ── ROUND-TRIP ─────────────────────────────────────────────────────


def test_round_trip_export_then_import(projects_root, meta_capture, monkeypatch, tmp_path):
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: dict(meta_mod._DEFAULT))
    _write_session(projects_root, "-home-testuser-Projects-x", SRC,
                   [_line("a", text="alpha"), _line("b", text="beta")])
    env = teleport.export_session(SRC)
    target = tmp_path / "dest"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    res = _run(teleport.import_session(env, lib_id="projects/dest", new_id="rt"))
    dest = projects_root / teleport.cwd_to_slug(target) / "rt.jsonl"
    lines = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]
    # ids/cwd rewritten, message content preserved
    assert [l["message"]["content"] for l in lines] == ["alpha", "beta"]
    assert all(l["sessionId"] == "rt" for l in lines)
    assert all(l["cwd"] == str(target) for l in lines)
    assert res["n_lines"] == 2


# ── ROUTES (integration) ───────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app_mod.create_app(), raise_server_exceptions=True)


def test_route_export_200_and_attachment(projects_root, client, monkeypatch):
    monkeypatch.setattr(meta_mod, "get_meta", lambda sid: dict(meta_mod._DEFAULT))
    _write_session(projects_root, "-home-testuser-Projects-x", SRC, [_line("a"), _line("b")])
    r = client.get(f"/api/orchestrator/sessions/{SRC}/teleport")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == teleport.ENVELOPE_KIND
    assert "attachment" in r.headers.get("content-disposition", "")
    assert ".json" in r.headers.get("content-disposition", "")


def test_route_export_404(projects_root, client):
    r = client.get("/api/orchestrator/sessions/missing-xyz/teleport")
    assert r.status_code == 404


def test_route_import_401_without_token(projects_root, client, monkeypatch):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    r = client.post("/api/orchestrator/sessions/teleport",
                    json={"envelope": _envelope(), "lib_id": ""})
    assert r.status_code == 401


def test_route_import_200_with_token(projects_root, client, monkeypatch, meta_capture, tmp_path):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    target = tmp_path / "agent"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    r = client.post("/api/orchestrator/sessions/teleport",
                    headers={"x-artifact-token": "secret-token"},
                    json={"envelope": _envelope(), "lib_id": "projects/agent"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["new_session_id"]


def test_route_import_400_bad_envelope(projects_root, client, monkeypatch, meta_capture):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    r = client.post("/api/orchestrator/sessions/teleport",
                    headers={"x-artifact-token": "secret-token"},
                    json={"envelope": {"version": 7}, "lib_id": ""})
    assert r.status_code == 400


def test_route_import_400_missing_lib_id(projects_root, client, monkeypatch):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    r = client.post("/api/orchestrator/sessions/teleport",
                    headers={"x-artifact-token": "secret-token"},
                    json={"envelope": _envelope()})  # no lib_id key
    assert r.status_code == 400


def test_route_import_400_unknown_agent(projects_root, client, monkeypatch, meta_capture):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: None)
    r = client.post("/api/orchestrator/sessions/teleport",
                    headers={"x-artifact-token": "secret-token"},
                    json={"envelope": _envelope(), "lib_id": "projects/ghost"})
    assert r.status_code == 400


def test_route_import_400_bad_model(projects_root, client, monkeypatch, meta_capture, tmp_path):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    target = tmp_path / "agent"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    r = client.post("/api/orchestrator/sessions/teleport",
                    headers={"x-artifact-token": "secret-token"},
                    json={"envelope": _envelope(), "lib_id": "projects/agent", "model": "gpt4"})
    assert r.status_code == 400


def test_route_import_writes_resumable_file_and_keeps_title(projects_root, client, monkeypatch, meta_capture, tmp_path):
    """Route-level: the file actually lands on disk, path-substituted, and title/model survive."""
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "secret-token")
    target = tmp_path / "agent"; target.mkdir()
    monkeypatch.setattr(artifacts_mod, "_lib_id_to_cwd", lambda lib: target)
    r = client.post("/api/orchestrator/sessions/teleport",
                    headers={"x-artifact-token": "secret-token"},
                    json={"envelope": _envelope(), "lib_id": "projects/agent",
                          "title": "Kept", "model": "sonnet"})
    assert r.status_code == 200, r.text
    nid = r.json()["new_session_id"]
    dest = projects_root / teleport.cwd_to_slug(target) / f"{nid}.jsonl"
    assert dest.is_file()
    lines = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]
    assert all(l["sessionId"] == nid for l in lines)
    assert all(l["cwd"] == str(target) for l in lines)
    assert meta_capture["title"] == "Kept"
    assert meta_capture["model"] == "sonnet"


def test_slug_files_order_is_deterministic_on_equal_size(projects_root):
    # Two equal-size files under different slugs → deterministic order (by dir name).
    _write_session(projects_root, "-zzz-second", SRC, [_line("a")])
    _write_session(projects_root, "-aaa-first", SRC, [_line("a")])
    files = teleport._slug_files(SRC)
    # equal size → sorted by parent dir name ascending
    assert [p.parent.name for p in files] == ["-aaa-first", "-zzz-second"]


# ── meta field ─────────────────────────────────────────────────────


def test_meta_teleported_from_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(meta_mod, "META_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(meta_mod, "_data", None)
    _run(meta_mod.set_meta("sid", teleported_from="origin-sid"))
    assert meta_mod.get_meta("sid")["teleported_from"] == "origin-sid"
    _run(meta_mod.set_meta("sid", teleported_from=""))
    assert meta_mod.get_meta("sid")["teleported_from"] is None


def test_meta_teleported_from_survives_disk_reload(tmp_path, monkeypatch):
    """The on-disk sanitizer (_load_from_disk) must preserve teleported_from."""
    monkeypatch.setattr(meta_mod, "META_PATH", tmp_path / "meta.json")
    monkeypatch.setattr(meta_mod, "_data", None)
    _run(meta_mod.set_meta("sid", teleported_from="origin-sid"))
    # Force a reload from disk (mimics a process restart).
    monkeypatch.setattr(meta_mod, "_data", None)
    assert meta_mod.get_meta("sid")["teleported_from"] == "origin-sid"


# ── skill ships ────────────────────────────────────────────────────


# ── skill distribution (local install) ─────────────────────────────


def test_build_skill_tarball_contains_files_but_not_config(projects_root):
    import io, tarfile
    data = teleport.build_skill_tarball()
    assert isinstance(data, bytes) and data
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "hetzner-teleport/SKILL.md" in names
    assert "hetzner-teleport/scripts/teleport_cli.py" in names
    # live config.json (may hold a token) must be excluded; example is fine
    assert "hetzner-teleport/config.json" not in names
    # no build noise
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


def test_install_doc_bakes_base_url_and_steps():
    doc = teleport.install_doc("https://priv.example.com/")
    assert "https://priv.example.com/api/orchestrator/teleport/skill.tar.gz" in doc
    assert "config.json" in doc
    assert "import" in doc and "export" in doc
    assert "hetzner-teleport" in doc


def test_install_prompt_is_actionable():
    p = teleport.install_prompt("https://priv.example.com/")
    assert "https://priv.example.com/api/orchestrator/teleport/install" in p
    assert "hetzner-teleport" in p
    # it must instruct ACTION (install/update), not just "read"
    assert "zainstaluj" in p.lower()
    assert "zaktualizuj" in p.lower()


def test_route_teleport_install_and_tarball_and_info(projects_root, client, monkeypatch):
    monkeypatch.setattr(artifacts_mod, "read_token", lambda: "tok-123")
    r = client.get("/api/orchestrator/teleport/install")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert "skill.tar.gz" in r.text

    r2 = client.get("/api/orchestrator/teleport/skill.tar.gz")
    assert r2.status_code == 200
    assert r2.headers["content-type"] == "application/gzip"
    assert r2.content[:2] == b"\x1f\x8b"  # gzip magic

    r3 = client.get("/api/orchestrator/teleport/info")
    assert r3.status_code == 200
    body = r3.json()
    assert body["token"] == "tok-123"
    assert body["install_url"].endswith("/api/orchestrator/teleport/install")
    assert "install" in body["install_prompt"].lower()


# ── client-side CLI (the local agent's two flows) ──────────────────


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Load the standalone skill CLI with CLAUDE_PROJECTS pointed at a tmp dir."""
    import importlib.util
    cli_path = Path(__file__).resolve().parents[1] / "skills" / "hetzner-teleport" / "scripts" / "teleport_cli.py"
    spec = importlib.util.spec_from_file_location("hetzner_teleport_cli_under_test", cli_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "CLAUDE_PROJECTS", tmp_path / "claude-projects")
    return mod


def test_cli_reads_dashboard_url_and_token_from_config(cli, tmp_path, monkeypatch):
    """The skill CLI must work off-box via config.json."""
    monkeypatch.setattr(cli, "SKILL_DIR", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"dashboard_url": "https://remote.example", "artifact_token": "cfg-token"})
    )
    monkeypatch.delenv("HD_NOTIFY_URL", raising=False)
    monkeypatch.delenv("HD_ARTIFACT_TOKEN_FILE", raising=False)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path / "nohome"))
    assert cli.dashboard_url() == "https://remote.example"
    assert cli.read_token() == "cfg-token"


def test_cli_import_writes_local_session_in_cwd(cli, tmp_path, monkeypatch):
    """IMPORT: pull a server bundle and materialize it in the current local project."""
    bundle = {
        "version": 1, "kind": "hetzner-session-teleport", "source_session_id": SRC,
        "transcript": [_line("a", text="alpha"), _line("b", text="beta")],
    }
    monkeypatch.setattr(cli, "_http", lambda method, path, **k: (200, bundle))
    proj = tmp_path / "localproj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    server_uuid = "11111111-2222-3333-4444-555555555555"
    args = cli.argparse.Namespace(source=f"https://x/chat/{server_uuid}", title="hello")
    assert cli.cmd_import(args) == 0
    dest_dir = cli.CLAUDE_PROJECTS / cli.cwd_to_slug(str(proj))
    files = list(dest_dir.glob("*.jsonl"))
    assert len(files) == 1
    new_id = files[0].stem
    lines = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    convo = [l for l in lines if l.get("type") != "ai-title"]
    assert all(l["sessionId"] == new_id for l in convo)
    assert all(l["cwd"] == str(proj) for l in convo)
    assert [l["message"]["content"] for l in convo] == ["alpha", "beta"]
    # a searchable native title record is stamped (so /resume finds it by name)
    titles = [l for l in lines if l.get("type") == "ai-title"]
    assert len(titles) == 1
    assert titles[0]["aiTitle"] == "hello"
    assert titles[0]["sessionId"] == new_id


def test_cli_import_defaults_title_when_none(cli, tmp_path, monkeypatch):
    """Even without --title, import stamps a searchable ai-title (teleport <id>)."""
    server_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    bundle = {"version": 1, "kind": "hetzner-session-teleport",
              "source_session_id": server_uuid, "transcript": [_line("a")]}
    monkeypatch.setattr(cli, "_http", lambda method, path, **k: (200, bundle))
    proj = tmp_path / "p"; proj.mkdir()
    monkeypatch.chdir(proj)
    assert cli.cmd_import(cli.argparse.Namespace(source=server_uuid, title=None)) == 0
    f = list((cli.CLAUDE_PROJECTS / cli.cwd_to_slug(str(proj))).glob("*.jsonl"))[0]
    titles = [json.loads(l) for l in f.read_text().splitlines()
              if l.strip() and json.loads(l).get("type") == "ai-title"]
    assert len(titles) == 1
    assert titles[0]["aiTitle"] == f"teleport {server_uuid[:8]}"


def test_cli_export_builds_envelope_and_posts(cli, tmp_path, monkeypatch):
    """EXPORT: read the newest local session and POST it to a server agent."""
    cwd = tmp_path / "localproj"
    cwd.mkdir()
    slug_dir = cli.CLAUDE_PROJECTS / cli.cwd_to_slug(str(cwd))
    slug_dir.mkdir(parents=True)
    (slug_dir / "loc-1.jsonl").write_text(
        "\n".join(json.dumps(o) for o in [_line("a"), _line("b")]) + "\n"
    )
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "read_token", lambda: "tok")
    captured = {}

    def _fake_http(method, path, *, body=None, token=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        captured["token"] = token
        return 200, {"new_session_id": "srv-new", "lib_id": "projects/x", "n_lines": 2}

    monkeypatch.setattr(cli, "_http", _fake_http)
    args = cli.argparse.Namespace(agent="projects/x", session=None, title="T")
    assert cli.cmd_export(args) == 0
    assert captured["method"] == "POST"
    assert captured["token"] == "tok"
    env = captured["body"]["envelope"]
    assert env["kind"] == "hetzner-session-teleport" and env["version"] == 1
    assert len(env["transcript"]) == 2
    assert captured["body"]["lib_id"] == "projects/x"


def test_cli_export_global_maps_to_empty_lib_id(cli, tmp_path, monkeypatch):
    cwd = tmp_path / "p"; cwd.mkdir()
    slug_dir = cli.CLAUDE_PROJECTS / cli.cwd_to_slug(str(cwd)); slug_dir.mkdir(parents=True)
    (slug_dir / "loc-9.jsonl").write_text(json.dumps(_line("a")) + "\n")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "read_token", lambda: "tok")
    captured = {}
    monkeypatch.setattr(cli, "_http", lambda m, p, *, body=None, token=None: (captured.update(body=body) or (200, {"new_session_id": "s", "lib_id": None, "n_lines": 1})))
    assert cli.cmd_export(cli.argparse.Namespace(agent="global", session=None, title=None)) == 0
    assert captured["body"]["lib_id"] == ""


def test_decorate_session_surfaces_teleported_from():
    """The session-list overlay must expose teleported_from like compacted_from."""
    from orbit import orchestrator as orch
    decorated = orch._decorate_session(
        {"id": "nid", "msg_count": 1},
        {**meta_mod._DEFAULT, "teleported_from": "origin-sid"},
    )
    assert decorated["teleported_from"] == "origin-sid"
    # blank/missing → None (parity with compacted_from)
    assert orch._decorate_session({"id": "x", "msg_count": 0}, {})["teleported_from"] is None


def test_teleport_skill_ships_with_skill_md():
    repo = Path(__file__).resolve().parents[1]
    skill = repo / "skills" / "hetzner-teleport"
    assert (skill / "SKILL.md").is_file(), "skill must ship SKILL.md for auto-seeding"
    cli = skill / "scripts" / "teleport_cli.py"
    assert cli.is_file()
    src = cli.read_text()
    assert "export" in src and "import" in src
    assert "HD_" in src  # uses the HD_* env contract
    # register.json must be valid JSON with source "local" so seeding picks it up.
    reg = json.loads((skill / "register.json").read_text())
    assert reg["source"] == "local"
    assert isinstance(reg.get("description"), str) and reg["description"]
