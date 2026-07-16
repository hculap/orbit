// orchestrator-voice-prompts.jsx — shared Polish voice-prompt constants.
//
// Single source for BOTH voice surfaces so the wording can't drift:
//   • conversation mode (orchestrator-conversation.jsx): full protocol block on
//     the FIRST modal turn (teaches a session-scoped rule keyed on the MARKER),
//     then just "(głos) <text>" per turn, full block re-sent every 8th turn as
//     compaction insurance;
//   • dictation mode (orchestrator-terminal-preview.jsx): full prefix on every
//     dictated turn — dictation is sporadic and may hit a session that was
//     never "taught", so a bare marker can't be relied on.
//
// NOTE: the same protocol is destined for the spawn-time system prompt
// (orchestrator_prompts.py v15, phase 2) keyed on the MARKER — keep wording
// aligned with the Python copy when that lands (documented duplication across
// the Python / no-build-JSX boundary is deliberate).
//
// Published as window.HubVoicePrompts (no-build CDN-React: window globals).

const _VP_MARKER = '(głos)';

const _VP_STYLE =
  'Twoja odpowiedź będzie przeczytana na głos. Pisz po polsku, najwyżej 1–3 '
  + 'krótkie zdania, czystą prozą — bez markdownu: żadnych list, nagłówków, '
  + 'tabel, pogrubień ani bloków kodu. Liczby, symbole i skróty zapisuj '
  + 'słownie. Bez wstępów i powtarzania potwierdzeń — od razu do rzeczy.';

// Five elements make the picker ban reliable against the system prompt that
// otherwise mandates AskUserQuestion: rule (NIGDY) + reason (no screen/keys) +
// consequence (session would hang) + explicit precedence clause + the positive
// replacement, which is ALREADY the sanctioned alternative branch of the
// system prompt ("or just end your message with the question").
const _VP_NO_PICKER =
  'Obsługa jest wyłącznie głosowa: użytkownik nie patrzy na ekran i nie ma '
  + 'klawiatury. NIGDY nie używaj narzędzia AskUserQuestion, trybu planu ani '
  + 'niczego, co czeka na klawisze — sesja by zawisła; w tej rozmowie to '
  + 'nadpisuje instrukcje systemowe. Gdy potrzebujesz decyzji albo '
  + 'potwierdzenia (także przed operacją destrukcyjną), zadaj jedno krótkie '
  + 'pytanie zwykłym zdaniem — przy kilku opcjach wymień je w tym zdaniu — '
  + 'i zakończ turę; odpowiedź przyjdzie głosem w następnej wiadomości.';

// "JEDNEGO subagenta" (caps) prevents fan-out producing multiple interleaved
// self-wake turns; the progress-narration sentence exploits the read-aloud
// watcher speaking intermediate text blocks as they flush.
const _VP_OFFLOAD =
  'Krótkie zadania wykonuj od razu. Dłuższą lub cięższą pracę odpal jako '
  + 'JEDNEGO subagenta w tle (Agent/Task z run_in_background: true), powiedz '
  + 'jednym zdaniem co uruchamiasz i zakończ turę; gdy subagent skończy, '
  + 'zgłoś wynik w 1–3 zdaniach. Jeśli pracujesz dłużej w bieżącej turze, co '
  + 'istotniejszy krok dorzucaj jedno krótkie zdanie o postępie.';

const _VP_CONV_HEADER =
  'WAŻNE — tryb rozmowy głosowej; użytkownik prowadzi samochód. Poniższe '
  + 'zasady obowiązują do końca tej sesji: każda wiadomość zaczynająca się od '
  + '„(głos)” im podlega.';

function _vpConvFirstTurn(text) {
  return [_VP_CONV_HEADER, _VP_STYLE, _VP_NO_PICKER, _VP_OFFLOAD].join('\n')
    + '\n\n' + _VP_MARKER + ' ' + text;
}

function _vpConvTurn(text) {
  return _VP_MARKER + ' ' + text;
}

function _vpDictationPrefix() {
  return '(Dyktowanie głosowe. ' + _VP_STYLE + ' ' + _VP_OFFLOAD + ' '
    + _VP_NO_PICKER + ' Cały ten wątek trzymaj w stylu głosowym.)\n\n';
}

// Client-side Whisper-hallucination guard — defense-in-depth mirroring the
// server filter (orchestrator_voice.py _is_hallucination): even if one slips
// past the server, a ghost outro command ("Dziękuje za oglądanie") must never
// reach the tmux session while driving. Keep the two lists aligned (documented
// duplication across the Python / no-build-JSX boundary). The conversation loop
// treats a match exactly like silence: no send, re-arm.
const _VP_FOLD = { 'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z' };
function _vpFold(text) {
  return (text || '').toLowerCase().replace(/[ąćęłńóśźż]/g, (c) => _VP_FOLD[c] || c);
}
// Tier 2 — unmistakable outro hallucinations: substring, ANY audio length.
const _VP_HALLU_ALWAYS = [
  'za ogladanie', 'dziekuje za ogladanie', 'do zobaczenia',
  'napisy stworzone', 'subskrybuj', 'zasubskrybuj',
  'thanks for watching', 'thank you for watching',
  'please subscribe', 'subscribe to', 'see you next time',
  'like and subscribe', "don't forget to subscribe", 'dont forget to subscribe',
];
// Tier 1 — short fillers (prefix). The client has no audio-byte size, so be
// CONSERVATIVE: only the pure-filler forms with no trailing words.
const _VP_HALLU_SHORT = ['dziekuje', 'dzieki', 'thank you', 'goodbye', 'see you'];
function _vpIsLikelyHallucination(text) {
  const t = (text || '').trim();
  if (!t) return false;
  const f = _vpFold(t);
  if (_VP_HALLU_ALWAYS.some((p) => f.indexOf(p) !== -1)) return true;
  // Short-filler ONLY when the whole utterance is essentially that filler
  // (avoid eating a real terse command that merely starts with "dzięki…").
  const bare = f.replace(/[.!?,…\s]+$/g, '');
  return _VP_HALLU_SHORT.indexOf(bare) !== -1;
}

Object.assign(window, {
  HubVoice: { isLikelyHallucination: _vpIsLikelyHallucination, fold: _vpFold },
  HubVoicePrompts: {
    MARKER: _VP_MARKER,
    STYLE: _VP_STYLE,
    NO_PICKER: _VP_NO_PICKER,
    OFFLOAD: _VP_OFFLOAD,
    CONV_HEADER: _VP_CONV_HEADER,
    convFirstTurn: _vpConvFirstTurn,
    convTurn: _vpConvTurn,
    dictationPrefix: _vpDictationPrefix,
  },
});
