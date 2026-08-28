"""Guards for the prompt documents — the LIVE stack first, the dead one second.

What agents actually receive is the per-agent stack assembled by
``agent_prompts.prompts_for_session`` (general.md → orchestrator.md → identity.md
→ custom.md), seeded from ``agent_prompts_defaults``. That text IS paid for on
every turn of every agent, so a stale claim in it is worse than a stale comment:
the agent acts on it.

``orchestrator_prompts.system_prompt_content()`` renders
``~/.orchestrator/system_prompt.md``, which is NOT in that stack today —
``PROMPT_PATH`` is a dead constant in both runners. Its guards are kept below
(the document is still rendered on disk and still read by humans) but are
labelled as guarding the dead document, so nobody mistakes them for coverage of
what agents receive.

These tests pin the a2a routing rule introduced after A2A v2 (commit 2d2b208)
removed the Monitor inbox watcher and the cold-revive kicker.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbit import agent_prompts_defaults as defaults
from orbit import orchestrator_prompts as prompts

REPO_ROOT = Path(__file__).resolve().parents[1]
A2A_SKILL_MD = REPO_ROOT / "skills" / "a2a" / "SKILL.md"


# Phrases that describe capabilities A2A v2 does NOT have. Any of them in a
# prompt tells the agent a2a will wake the target — it will not.
STALE_A2A_CLAIMS = (
    "auto-spawns",
    "auto-spawn",
    "auto-revive",
    "Monitor-armed",
    "inbox watcher",
)


@pytest.fixture(name="general_prompt")
def _general_prompt() -> str:
    """The document EVERY agent actually gets (general.md, seeded from this default)."""
    return defaults.GENERAL_PROMPT_DEFAULT


@pytest.fixture(name="system_prompt")
def _system_prompt() -> str:
    """The DEAD document (`~/.orchestrator/system_prompt.md`) — not in the stack."""
    return prompts.system_prompt_content()


def _line_containing(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line containing {needle!r}")


# ── the LIVE per-agent stack ──────────────────────────────────────


def test_general_prompt_header_matches_the_migration_version(general_prompt: str) -> None:
    """A drifting header/constant pair re-migrates the file on every boot."""
    assert general_prompt.startswith(f"<!-- agent-prompt:{defaults.AGENT_PROMPT_VERSION} -->")


def test_orchestrator_prompt_header_matches_the_migration_version() -> None:
    """Same loop, second seeded file: 68 `.v4.bak.N` copies were observed on disk."""
    assert defaults.ORCHESTRATOR_PROMPT_DEFAULT.startswith(
        f"<!-- agent-prompt:{defaults.AGENT_PROMPT_VERSION} -->"
    )


def test_general_prompt_routes_live_peers_to_the_native_tools(general_prompt: str) -> None:
    assert "ListAgents" in general_prompt
    assert "SendMessage" in general_prompt


def test_general_prompt_routes_cold_or_synchronous_work_to_the_mcp(
    general_prompt: str,
) -> None:
    assert "send_and_wait" in general_prompt
    assert "/api/orchestrator/sessions/" in general_prompt


def test_general_prompt_curl_uses_the_env_var_not_a_hardcoded_host(
    general_prompt: str,
) -> None:
    """SKILL.md resolves HD_NOTIFY_URL → config.json → localhost; don't pin localhost."""
    assert "$HD_NOTIFY_URL" in general_prompt
    assert "localhost:8766" not in general_prompt


def test_general_prompt_frames_the_ref_suffix_as_a_confirmation_gate(
    general_prompt: str,
) -> None:
    """Observed: a peer unique in the listing still demands the ref on first send."""
    line = _line_containing(general_prompt, "[ref]")
    assert "confirmation" in line.lower()


def test_general_prompt_says_a2a_send_is_only_a_note(general_prompt: str) -> None:
    lowered = general_prompt.lower()
    assert "note" in lowered
    assert "pure enqueue" in lowered


@pytest.mark.parametrize("claim", STALE_A2A_CLAIMS)
def test_general_prompt_makes_no_removed_a2a_delivery_claim(
    general_prompt: str, claim: str
) -> None:
    assert claim.lower() not in general_prompt.lower()


# ── the a2a SKILL.md shipped in this repo ─────────────────────────


def test_a2a_skill_md_uses_the_env_var_in_its_curl() -> None:
    text = A2A_SKILL_MD.read_text(encoding="utf-8")
    assert "$HD_NOTIFY_URL/api/orchestrator/sessions/" in text
    line = _line_containing(text, "[ref]")
    assert "confirmation" in line.lower()


# ── the DEAD document (`~/.orchestrator/system_prompt.md`) ────────


def test_orchestrator_prompts_module_flags_itself_as_out_of_the_stack() -> None:
    """So a future editor puts behaviour changes in agent_prompts_defaults."""
    doc = (prompts.__doc__ or "").lower()
    assert "not in the prompt stack" in doc
    assert "agent_prompts.prompts_for_session" in doc


def test_version_header_is_stamped_into_both_documents(system_prompt: str) -> None:
    assert system_prompt.startswith(prompts.VERSION_HEADER)
    assert prompts.claude_md_content().startswith(prompts.VERSION_HEADER)
    assert prompts.VERSION_HEADER == f"<!-- orch-prompt:{prompts.PROMPT_VERSION} -->"


def test_prompt_version_is_parseable_and_current(system_prompt: str) -> None:
    """The migration in `_render_with_migration` keys off this integer."""
    assert prompts._current_version_int() >= 19


@pytest.mark.parametrize("claim", STALE_A2A_CLAIMS)
def test_system_prompt_makes_no_removed_a2a_delivery_claim(
    system_prompt: str, claim: str
) -> None:
    assert claim.lower() not in system_prompt.lower()


def test_system_prompt_routes_live_peers_to_the_native_tools(system_prompt: str) -> None:
    assert "SendMessage" in system_prompt
    assert "ListAgents" in system_prompt
    # The first send to a peer not yet addressed in this conversation comes back
    # asking for the `[ref]` suffix — a confirmation gate, not an error.
    assert "[ref]" in system_prompt
    assert "confirmation" in _line_containing(system_prompt, "[ref]").lower()


def test_system_prompt_says_a2a_wakes_nobody(system_prompt: str) -> None:
    lowered = system_prompt.lower()
    assert "a2a" in lowered
    assert "wake" in lowered


def test_claude_md_carries_no_stale_a2a_claim() -> None:
    lowered = prompts.claude_md_content().lower()
    for claim in STALE_A2A_CLAIMS:
        assert claim.lower() not in lowered
