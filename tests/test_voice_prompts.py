"""Drift/contract test for static/orchestrator-voice-prompts.jsx.

The module is plain JS (no JSX syntax), so node can evaluate it directly with a
window stub. Guards the contract both voice surfaces depend on: the published
keys, the "(głos)" marker, the picker ban, and the builder shapes — a rename or
accidental markdown in the prompt strings breaks dictation AND conversation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_MODULE = Path(__file__).parent.parent / "src" / "orbit" / "static" / "orchestrator-voice-prompts.jsx"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _eval_module() -> dict:
    script = f"""
    const window = {{}};
    Object.assign = Object.assign.bind(Object);
    {_MODULE.read_text(encoding="utf-8")}
    const vp = window.HubVoicePrompts;
    console.log(JSON.stringify({{
      keys: Object.keys(vp).sort(),
      marker: vp.MARKER,
      first: vp.convFirstTurn('test pytanie'),
      turn: vp.convTurn('test pytanie'),
      dict: vp.dictationPrefix(),
    }}));
    """
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout.strip())


def test_publishes_expected_contract():
    data = _eval_module()
    assert data["marker"] == "(głos)"
    for key in ("MARKER", "STYLE", "NO_PICKER", "OFFLOAD", "CONV_HEADER",
                "convFirstTurn", "convTurn", "dictationPrefix"):
        assert key in data["keys"], f"missing HubVoicePrompts.{key}"


def test_conv_turn_is_marker_only():
    data = _eval_module()
    assert data["turn"] == "(głos) test pytanie"


def test_conv_first_turn_teaches_protocol_and_carries_marker():
    data = _eval_module()
    first = data["first"]
    assert first.endswith("(głos) test pytanie")
    assert "AskUserQuestion" in first              # picker ban present
    assert "run_in_background" in first            # offload instruction present
    assert "prowadzi samochód" in first            # driving context present
    # protocol header flags the rule as session-scoped
    assert "do końca tej sesji" in first


def test_dictation_prefix_full_and_separate_from_text():
    data = _eval_module()
    d = data["dict"]
    assert d.endswith(")\n\n")                     # paste-prefix shape: closed paren + blank line
    assert "AskUserQuestion" in d
    assert "Cały ten wątek trzymaj w stylu głosowym." in d
