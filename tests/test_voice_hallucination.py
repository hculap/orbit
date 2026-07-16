"""Whisper hallucination filter — regression for the 2026-06-13 in-car miss.

The driver got a ghost command 'Dziękuje za oglądanie' (YouTube-outro
hallucination on road noise) forwarded to claude because the filter listed
'dziękuję' (with ę) and the transcript had 'dziękuje' (no ę), and the outro
was longer than the 32 KB small-audio gate. These pin the two-tier +
diacritic-folded behavior.
"""
from __future__ import annotations

from orbit.orchestrator_voice import _fold, _is_hallucination

_BIG = 200 * 1024   # well past the small-audio gate (multi-second cabin noise)
_SMALL = 10 * 1024  # under the 32 KB gate


def test_fold_strips_polish_diacritics():
    assert _fold("Dziękuję") == "dziekuje"
    assert _fold("OglĄdanie ŻŹĆŁŃÓŚĘĄ") == "ogladanie zzclnosea"


def test_the_exact_in_car_miss_is_dropped_at_any_size():
    # The real transcript that slipped through (no ę, long audio).
    assert _is_hallucination("Dziękuje za oglądanie.", _BIG) is True
    assert _is_hallucination("dziękuję za oglądanie", _SMALL) is True


def test_outro_phrases_drop_regardless_of_audio_size():
    for t in ("Thanks for watching!", "Please subscribe", "Subskrybuj kanał",
              "Do zobaczenia", "Napisy stworzone przez społeczność Amara.org"):
        assert _is_hallucination(t, _BIG) is True, t


def test_short_fillers_drop_only_on_small_audio():
    assert _is_hallucination("Dziękuję.", _SMALL) is True
    # On a long, clearly-voiced clip a bare filler is more likely real → keep.
    assert _is_hallucination("Tak.", _BIG) is False
    assert _is_hallucination("Tak.", _SMALL) is True


def test_real_commands_pass_through():
    for t in ("Policz pliki w katalogu i podaj wynik.",
              "Tak, zrób to teraz",          # 'tak,' != prefix 'tak.'
              "Dodaj obsługę napisów do gry",  # not 'napisy stworzone'
              "Sprawdź dysk na serwerze"):
        assert _is_hallucination(t, _SMALL) is False, t
        assert _is_hallucination(t, _BIG) is False, t


def test_empty_is_not_hallucination():
    assert _is_hallucination("", _SMALL) is False
    assert _is_hallucination("   ", _SMALL) is False
