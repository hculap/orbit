"""Unit + integration tests for system_processes (the System drill-down).

No real ``/proc`` access: every test feeds a synthetic proc table + tmux map
and monkeypatches ``orchestrator_meta.get_meta`` — mirroring the
monkeypatch/_FakePool style in test_orchestrator_pool_shape.py. The cpu-math
and TTL tests stub ``proc_table`` / ``time.sleep`` so nothing sleeps.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from orbit import system_processes as sp


def _proc(stdout="", returncode=0):
    """Minimal stand-in for subprocess.run's CompletedProcess."""
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ── fixtures / helpers ──────────────────────────────────────────────

def _row(pid, *, ppid=1, comm="bash", cmdline=None, rss=0, ticks=0, cwd=None):
    return {
        "pid": pid,
        "ppid": ppid,
        "comm": comm,
        "state": "S",
        "cmdline": cmdline if cmdline is not None else [comm],
        "rss_bytes": rss,
        "cpu_ticks": ticks,
        "cwd": cwd,
    }


@pytest.fixture(autouse=True)
def _no_docker_no_cgroup(monkeypatch):
    """Keep attribution deterministic & off the host: stub the cgroup lookup so
    no pid resolves to a container (which is enough — _docker_name_map is only
    consulted when a container id is found). Tests that exercise the docker
    paths patch these themselves."""
    monkeypatch.setattr(sp, "_container_id_from_cgroup", lambda pid: None)
    sp._CACHE.clear()
    sp._DOCKER_NAMES["ts"] = 0.0
    sp._DOCKER_NAMES["map"] = {}


# ── attribute_pid ───────────────────────────────────────────────────

def test_attribute_agent_via_session_id(monkeypatch):
    """A NEW session spawns with --session-id <uuid>."""
    uuid = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {
        "cwd": "/home/testuser/Projects/my-project", "lib_id": "projects/my-project"})
    table = {100: _row(100, comm="claude", cmdline=["claude", "--session-id", uuid])}
    attr = sp.attribute_pid(100, table, {})
    assert attr["kind"] == "agent"
    assert attr["label"] == "My Project"
    assert attr["session_id"] == uuid
    assert attr["key"] == "projects/my-project"


def test_attribute_agent_via_resume(monkeypatch):
    """A RESUMED session spawns with --resume <uuid> — the form the live probe
    surfaced that the original finding missed."""
    uuid = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {
        "cwd": "/home/testuser/Areas/Work", "lib_id": "areas/Work"})
    table = {101: _row(101, comm="claude", cmdline=["claude", "--resume", uuid])}
    attr = sp.attribute_pid(101, table, {})
    assert attr["kind"] == "agent"
    assert attr["label"] == "Work"
    assert attr["session_id"] == uuid


def test_attribute_agent_via_tmux_pane_pid(monkeypatch):
    uuid = "33333333-3333-3333-3333-333333333333"
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {
        "cwd": None, "lib_id": "projects/my-project"})
    # No uuid in cmdline — resolved purely from the pane-pid map.
    table = {200: _row(200, comm="claude", cmdline=["claude"])}
    attr = sp.attribute_pid(200, table, {200: uuid})
    assert attr["kind"] == "agent"
    assert attr["session_id"] == uuid


def test_attribute_agent_child_via_ppid_chain(monkeypatch):
    """A tool/subprocess claude spawned (e.g. `du`) inherits the agent via the
    ppid chain up to the claude pane pid."""
    uuid = "44444444-4444-4444-4444-444444444444"
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {
        "cwd": "/home/testuser/Projects/my-project", "lib_id": "projects/my-project"})
    table = {
        300: _row(300, ppid=1, comm="claude", cmdline=["claude", "--resume", uuid]),
        301: _row(301, ppid=300, comm="du", cmdline=["du", "-sb", "/x"]),
    }
    attr = sp.attribute_pid(301, table, {300: uuid})
    assert attr["kind"] == "agent"
    assert attr["session_id"] == uuid


def test_attribute_project_by_cwd(monkeypatch, tmp_path):
    # HOME-independent: pin PROJECTS to a real RESOLVED tmp dir and build the
    # proc cwd under it. _para_for_cwd compares base.resolve() against an
    # UNRESOLVED Path(cwd) (system_processes.py:329/332), so the cwd must sit
    # under the already-resolved base — else macOS /home-autofs + /var symlink
    # resolution mismatches and it falls through to "system".
    projects = (tmp_path / "Projects").resolve()
    monkeypatch.setattr(sp.discovery, "PROJECTS", projects)
    table = {400: _row(400, comm="node", cmdline=["node", "server.js"],
                       cwd=str(projects / "my-project" / "app"))}
    # No agent uuid → falls to cwd-based PARA attribution.
    attr = sp.attribute_pid(400, table, {})
    assert attr["kind"] == "project"
    assert attr["key"] == "projects/my-project"
    assert attr["label"] == "My Project"


def test_attribute_area_by_cwd(monkeypatch, tmp_path):
    areas = (tmp_path / "Areas").resolve()
    monkeypatch.setattr(sp.discovery, "AREAS", areas)
    table = {401: _row(401, comm="python3", cmdline=["python3", "x.py"],
                       cwd=str(areas / "Work" / "sub"))}
    attr = sp.attribute_pid(401, table, {})
    assert attr["kind"] == "area"
    assert attr["key"] == "areas/Work"
    assert attr["label"] == "Work"


def test_attribute_app_by_comm():
    table = {500: _row(500, comm="nginx", cmdline=["nginx: worker"], cwd="/")}
    attr = sp.attribute_pid(500, table, {})
    assert attr["kind"] == "app"
    assert attr["label"] == "nginx"


def test_attribute_system_fallback():
    table = {600: _row(600, comm="weirdd", cmdline=["weirdd"], cwd="/")}
    attr = sp.attribute_pid(600, table, {})
    assert attr["kind"] == "system"
    assert attr["key"] == "weirdd"


def test_attribute_docker_container(monkeypatch):
    cid = "a" * 64
    monkeypatch.setattr(sp, "_container_id_from_cgroup", lambda pid: cid)
    monkeypatch.setattr(sp, "_docker_name_map", lambda: {cid: "anmar-redis-1"})
    table = {700: _row(700, comm="redis-server", cmdline=["redis-server"], cwd="/data")}
    attr = sp.attribute_pid(700, table, {})
    assert attr["kind"] == "app"
    assert attr["label"] == "anmar-redis-1"
    assert attr["key"] == "docker:anmar-redis-1"


# ── tmux pane parsing ───────────────────────────────────────────────

def test_tmux_pane_parse_filters_non_hd():
    uuid = "55555555-5555-5555-5555-555555555555"
    stdout = f"2616657 hd-{uuid}\n6 zsh\n999 hd-not-a-uuid\n"
    out = sp._parse_tmux_pane_output(stdout)
    assert out == {2616657: uuid}  # stray `zsh` + malformed hd- row dropped


# ── _build (grouping) ───────────────────────────────────────────────

def test_build_groups_sorted_with_other_bucket_and_totals(monkeypatch):
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {"cwd": None, "lib_id": None})
    monkeypatch.setattr(sp.system_status, "memory",
                        lambda: {"used_bytes": 1000, "total_bytes": 2000})
    # 4 distinct system comms with descending mem; top_n=2 rolls 2 into "other".
    table = {
        1: _row(1, comm="aaa", cmdline=["aaa"], rss=400, cwd="/"),
        2: _row(2, comm="bbb", cmdline=["bbb"], rss=300, cwd="/"),
        3: _row(3, comm="ccc", cmdline=["ccc"], rss=200, cwd="/"),
        4: _row(4, comm="ddd", cmdline=["ddd"], rss=100, cwd="/"),
    }
    value_map = {pid: row["rss_bytes"] for pid, row in table.items()}
    out = sp._build("mem", table, value_map, {}, top_n=2, ts=123.0)

    assert out["metric"] == "mem"
    assert out["total"] == {"used_bytes": 1000, "total_bytes": 2000, "unit": "bytes"}
    labels = [g["label"] for g in out["groups"]]
    assert labels[:2] == ["aaa", "bbb"]            # sorted desc
    other = out["groups"][-1]
    assert other["kind"] == "other"
    assert other["value"] == 300                    # ccc(200)+ddd(100)
    # All four processes' bytes are represented (top two + other bucket).
    assert sum(g["value"] for g in out["groups"]) == 1000


def test_build_cpu_total_is_sum_of_pct(monkeypatch):
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {"cwd": None, "lib_id": None})
    table = {
        1: _row(1, comm="aaa", cmdline=["aaa"], cwd="/"),
        2: _row(2, comm="bbb", cmdline=["bbb"], cwd="/"),
    }
    value_map = {1: 30.0, 2: 12.5}
    out = sp._build("cpu", table, value_map, {}, top_n=10, ts=1.0)
    assert out["total"]["unit"] == "pct"
    assert out["total"]["cores"] == sp._CORES
    assert out["total"]["value"] == 42.5


def test_build_proc_members_capped_to_five(monkeypatch):
    monkeypatch.setattr(sp.meta_mod, "get_meta", lambda sid: {"cwd": None, "lib_id": None})
    monkeypatch.setattr(sp.system_status, "memory",
                        lambda: {"used_bytes": 0, "total_bytes": 0})
    # 7 procs, all comm "nginx" → one group, members trimmed to top 5.
    table = {i: _row(i, comm="nginx", cmdline=["nginx"], rss=i * 10, cwd="/")
             for i in range(1, 8)}
    value_map = {pid: row["rss_bytes"] for pid, row in table.items()}
    out = sp._build("mem", table, value_map, {}, top_n=5, ts=1.0)
    nginx = next(g for g in out["groups"] if g["label"] == "nginx")
    assert len(nginx["procs"]) == 5
    assert nginx["procs"][0]["pid"] == 7  # highest rss first


# ── cpu sampling math ───────────────────────────────────────────────

def test_cpu_sample_math(monkeypatch):
    snaps = iter([
        {1: _row(1, comm="x", ticks=100), 2: _row(2, comm="y", ticks=50)},
        {1: _row(1, comm="x", ticks=100 + int(sp._CLK_TCK)),  # +1 core-second
         2: _row(2, comm="y", ticks=50)},                      # idle
    ])
    monkeypatch.setattr(sp, "proc_table", lambda: next(snaps))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    _table, pct = sp.cpu_sample(interval=1.0)
    assert pct[1] == pytest.approx(100.0)  # one core fully busy over 1s
    assert pct[2] == pytest.approx(0.0)


def test_human_bytes_parsing():
    assert sp._parse_human_bytes("2.618GB") == 2_618_000_000
    assert sp._parse_human_bytes("8.761MB") == 8_761_000
    assert sp._parse_human_bytes("12.29kB") == 12_290
    assert sp._parse_human_bytes("nonsense") == 0


# ── TTL cache ───────────────────────────────────────────────────────

def test_ttl_cache_probes_once(monkeypatch):
    calls = {"n": 0}

    def _fake_top(metric, top_n):
        calls["n"] += 1
        return {"metric": metric, "groups": [], "total": {}, "ts": 0.0}

    monkeypatch.setattr(sp, "top_consumers", _fake_top)
    sp._CACHE.clear()
    sp.top_for("cpu", 12)
    sp.top_for("cpu", 12)
    assert calls["n"] == 1  # second call served from cache


def test_top_for_rejects_bad_metric():
    with pytest.raises(ValueError):
        sp.top_for("bogus")


# ── integration: the FastAPI route ─────────────────────────────────

def test_api_system_processes_ok(client, monkeypatch):
    from orbit import system_processes as sp_mod
    payload = {"metric": "cpu", "ts": 1.0,
               "total": {"value": 5.0, "unit": "pct", "cores": 8},
               "groups": [{"kind": "agent", "key": "projects/x", "label": "X",
                           "value": 5.0, "procs": []}]}
    monkeypatch.setattr(sp_mod, "top_for", lambda metric, top: payload)
    res = client.get("/api/system/processes?metric=cpu")
    assert res.status_code == 200
    body = res.json()
    assert body["metric"] == "cpu"
    assert body["total"]["cores"] == 8
    assert body["groups"][0]["label"] == "X"


def test_api_system_processes_bad_metric(client):
    res = client.get("/api/system/processes?metric=bogus")
    assert res.status_code == 422


def test_api_system_processes_clamps_top(client, monkeypatch):
    """The route clamps `top` to [1, 50] before handing it to top_for."""
    from orbit import system_processes as sp_mod
    seen = {}

    def _capture(metric, top):
        seen["top"] = top
        return {"metric": metric, "ts": 0.0, "total": {}, "groups": []}

    monkeypatch.setattr(sp_mod, "top_for", _capture)
    client.get("/api/system/processes?metric=mem&top=999")
    assert seen["top"] == 50
    client.get("/api/system/processes?metric=mem&top=0")
    assert seen["top"] == 1


# ── /proc parsing (stat with parens, real-process integration) ──────

def test_parse_stat_handles_parens_in_comm():
    # comm contains spaces AND parens; utime/stime are stat fields 14/15.
    raw = "100 (weird (proc) name) S 7 0 0 0 0 0 0 0 0 0 250 130 0 0"
    comm, state, ppid, ticks = sp._parse_stat(raw)
    assert comm == "weird (proc) name"
    assert state == "S"
    assert ppid == 7
    assert ticks == 380  # utime 250 + stime 130


def test_parse_stat_malformed_returns_none():
    assert sp._parse_stat("garbage with no parens") is None
    assert sp._parse_stat("1 (short) S 2") is None  # too few fields for utime


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs Linux /proc")
def test_read_pid_reads_real_process():
    """Integration: parse our own /proc entry (real stat/statm/cmdline/cwd)."""
    import os
    row = sp._read_pid(os.getpid())
    assert row is not None
    assert row["pid"] == os.getpid()
    assert row["cmdline"]            # python ... (kernel threads return None)
    assert row["rss_bytes"] > 0      # statm resident pages × page size
    assert row["cwd"] == os.getcwd()


# ── _para_for_cwd boundaries ────────────────────────────────────────

def test_para_for_cwd_boundaries():
    # Exactly at the PARA root (no child segment) → not a project/area.
    assert sp._para_for_cwd(str(sp.discovery.PROJECTS)) is None
    # Home itself, and None, are not under any PARA root.
    assert sp._para_for_cwd(str(sp._HOME)) is None
    assert sp._para_for_cwd(None) is None


# ── _scan_children ──────────────────────────────────────────────────

def test_scan_children_aggregates_and_skips_files(monkeypatch, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "loose.txt").write_text("x")  # files are skipped
    monkeypatch.setattr(sp.share_mod, "_folder_size",
                        lambda p: {"alpha": 300, "beta": 100}.get(p.name, 0))
    rows = sp._scan_children(tmp_path, "project")
    by_label = {r["label"]: r for r in rows}
    assert set(by_label) == {"alpha", "beta"}     # loose.txt skipped
    assert by_label["alpha"]["value"] == 300
    assert by_label["alpha"]["kind"] == "project"
    assert by_label["alpha"]["key"] == f"{tmp_path.name.lower()}/alpha"


def test_scan_children_missing_base_returns_empty():
    from pathlib import Path
    assert sp._scan_children(Path("/no/such/dir/here"), "project") == []


# ── disk_attribution ────────────────────────────────────────────────

def test_disk_attribution_sorted_and_capped(monkeypatch):
    monkeypatch.setattr(sp, "_scan_children", lambda base, kind: (
        [{"kind": "project", "key": "projects/a", "label": "a", "value": 500, "path": "/a"},
         {"kind": "project", "key": "projects/b", "label": "b", "value": 50, "path": "/b"}]
        if kind == "project" else []))
    monkeypatch.setattr(sp.share_mod, "_folder_size", lambda p: 10)  # special dirs (if any)
    monkeypatch.setattr(sp, "_docker_disk_bytes", lambda: 999)
    monkeypatch.setattr(sp.system_status, "disk",
                        lambda path="/": {"used_bytes": 2000, "total_bytes": 5000})
    out = sp.disk_attribution(top_n=3)
    assert out["metric"] == "disk"
    assert out["total"] == {"used_bytes": 2000, "total_bytes": 5000, "unit": "bytes"}
    assert len(out["groups"]) == 3                      # capped to top_n
    vals = [g["value"] for g in out["groups"]]
    assert vals == sorted(vals, reverse=True)           # descending
    assert out["groups"][0]["value"] == 999             # Docker biggest
    assert out["groups"][0]["label"] == "Docker"


def test_disk_attribution_docker_unavailable(monkeypatch):
    monkeypatch.setattr(sp, "_scan_children", lambda base, kind: [])
    monkeypatch.setattr(sp.share_mod, "_folder_size", lambda p: 5)
    monkeypatch.setattr(sp, "_docker_disk_bytes", lambda: None)  # daemon down / no access
    monkeypatch.setattr(sp.system_status, "disk",
                        lambda path="/": {"used_bytes": 1, "total_bytes": 2})
    out = sp.disk_attribution(top_n=12)
    assert all(g["key"] != "docker" for g in out["groups"])  # no docker row added


# ── subprocess wrappers (tmux / docker), monkeypatched ──────────────

def test_tmux_pid_to_session(monkeypatch):
    uuid = "66666666-6666-6666-6666-666666666666"
    monkeypatch.setattr(sp.subprocess, "run",
                        lambda *a, **k: _proc(stdout=f"4242 hd-{uuid}\n6 zsh\n"))
    assert sp.tmux_pid_to_session() == {4242: uuid}


def test_tmux_pid_to_session_failure_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("tmux missing")
    monkeypatch.setattr(sp.subprocess, "run", _boom)
    assert sp.tmux_pid_to_session() == {}


def test_docker_name_map(monkeypatch):
    sp._DOCKER_NAMES["ts"] = 0.0
    sp._DOCKER_NAMES["map"] = {}
    cid = "c" * 64
    monkeypatch.setattr(sp.subprocess, "run",
                        lambda *a, **k: _proc(stdout=f"{cid} anmar-pg17\n"))
    assert sp._docker_name_map() == {cid: "anmar-pg17"}


def test_docker_disk_bytes_sums_system_df(monkeypatch):
    lines = (
        '{"Type":"Images","Size":"2.618GB"}\n'
        '{"Type":"Containers","Size":"8.761MB"}\n'
        '{"Type":"Local Volumes","Size":"1.715GB"}\n'
    )
    monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: _proc(stdout=lines))
    total = sp._docker_disk_bytes()
    assert total == 2_618_000_000 + 8_761_000 + 1_715_000_000


def test_docker_disk_bytes_none_on_failure(monkeypatch):
    monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: _proc(returncode=1))
    assert sp._docker_disk_bytes() is None


def test_attribute_docker_unknown_name_falls_back_to_short_id(monkeypatch):
    cid = "d" * 64
    monkeypatch.setattr(sp, "_container_id_from_cgroup", lambda pid: cid)
    monkeypatch.setattr(sp, "_docker_name_map", lambda: {})  # name unknown
    table = {800: _row(800, comm="redis-server", cmdline=["redis-server"], cwd="/")}
    attr = sp.attribute_pid(800, table, {})
    assert attr["kind"] == "app"
    assert attr["label"] == f"docker:{cid[:12]}"


def test_proc_table_skips_non_numeric_and_none(monkeypatch):
    entries = [
        types.SimpleNamespace(name="100"),
        types.SimpleNamespace(name="self"),   # non-numeric → skipped
        types.SimpleNamespace(name="200"),
    ]

    class _Scan:  # os.scandir result: a context manager that is also iterable
        def __iter__(self): return iter(entries)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(sp.os, "scandir", lambda p: _Scan())
    monkeypatch.setattr(sp, "_read_pid",
                        lambda pid: _row(pid) if pid == 100 else None)  # 200 = race/exit
    table = sp.proc_table()
    assert set(table) == {100}


def test_cmd_short_basenames_and_truncates():
    assert sp._cmd_short(["/usr/bin/python3", "x.py"]) == "python3 x.py"
    assert sp._cmd_short([]) == ""
    long = sp._cmd_short(["claude", "a" * 200], limit=20)
    assert len(long) == 20 and long.endswith("…")
