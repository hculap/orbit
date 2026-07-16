// orchestrator-mic-button.jsx — composer microphone toggle.
//
// Consumes window.useVoiceCapture (published by orchestrator-voice.jsx) so the
// button stays a thin shell over recording/finalizing state. Renders a raw
// <button> (instead of <IconButton>) because we need a `disabled` prop and a
// dynamic background that pulses with the live mic level.
//
// Public API:
//   <MicButton onTranscript={(text) => …}
//              disabled={boolean}
//              currentInput={string}
//              language={'pl'|…} />
//
// Both partial and final transcripts are delivered to onTranscript with the
// full updated text — overlapping audio chunks make each call a complete view
// of the transcript so far, so the consumer can simply replace the textarea
// value on every callback.

const { useMemo: _miUseMemo } = React;

function MicButton({ onTranscript, onFinalTranscript, onRecordingStart, disabled, currentInput, language = 'auto' }) {
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  // The hook is published to window by orchestrator-voice.jsx. Script load
  // order in index.html guarantees it exists by the time this component
  // renders, but guard for early-render edge cases anyway.
  const useVoiceCapture = (typeof window !== 'undefined' && window.useVoiceCapture) || null;

  // Stable callbacks — recreate only when the input prop actually changes.
  const handlePartial = (text) => {
    if (typeof onTranscript === 'function') onTranscript(text);
  };
  const handleFinal = (text) => {
    if (typeof onFinalTranscript === 'function') onFinalTranscript(text);
    else if (typeof onTranscript === 'function') onTranscript(text);
  };
  const handleError = (msg) => {
    if (toast) toast(msg || 'Voice capture failed', 'err');
  };

  // useVoiceCapture is a real hook — must be called unconditionally on every
  // render. If it's missing (script load race), short-circuit with a no-op
  // shape so the rules of hooks aren't violated downstream.
  const hookResult = useVoiceCapture
    ? useVoiceCapture({
        onPartial: handlePartial,
        onFinal: handleFinal,
        onError: handleError,
        language,
        silenceMs: 1500,
        // chunkMs:0 → final-only POST on stop (one valid container). The
        // mid-recording requestData() partials produced an invalid multi-header
        // webm that Whisper reads as empty (Groq 400 "audio too short"), so on
        // Chrome/webm devices (Android, desktop) BOTH the live preview AND the
        // final transcript came back blank; Safari/mp4 only dodged it because it
        // no-ops requestData(). The final text still reaches consumers via
        // handleFinal (→ onFinalTranscript, or onTranscript fallback), so the
        // composer/soft-key keep working — they just lose the (already-broken)
        // live preview. Mirrors the conversation-mode + terminal-mic fix.
        chunkMs: 0,
        maxRecordingMs: 5 * 60 * 1000,
      })
    : { state: 'idle', level: 0, lastError: null, start: () => {}, stop: () => {} };

  const { state, level, lastError, start, stop } = hookResult;
  const recording = state === 'recording';
  const finalizing = state === 'finalizing';
  const hasError = state === 'error' || !!lastError;

  const onClick = () => {
    if (disabled || finalizing) return;
    if (recording) stop();
    else {
      // Notify before kicking off getUserMedia so callers (e.g. TTS) can
      // cancel any in-flight playback synchronously inside the user gesture.
      try { if (typeof onRecordingStart === 'function') onRecordingStart(); } catch (e) { /* swallow */ }
      start();
    }
  };

  // Visual state ------------------------------------------------------------
  const SIZE = 32;
  const iconName = recording ? 'mic-fill' : (finalizing ? 'spinner' : 'mic');
  const iconColor = recording ? 'var(--err)' : (hasError ? 'var(--err)' : 'var(--fg-2)');

  // Pulse the background opacity with the mic level (0..1) while recording.
  const lvl = Math.max(0, Math.min(1, Number(level) || 0));
  const pulseAlpha = 0.15 + lvl * 0.4;
  const bg = recording
    ? `oklch(0.70 0.18 25 / ${pulseAlpha.toFixed(3)})`
    : 'transparent';

  const title = recording
    ? 'Zatrzymaj nagrywanie'
    : (finalizing ? 'Finalizowanie…' : 'Nagraj wiadomość głosową (klik = stop, lub auto-stop na 1.5s ciszy)');

  const aria = recording
    ? 'Zatrzymaj nagrywanie'
    : (finalizing ? 'Finalizowanie nagrania' : 'Nagraj wiadomość głosową');

  const showErrorDot = hasError && !recording && !finalizing;

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!!disabled && !recording}
      title={hasError && lastError ? `${title} — ${lastError}` : title}
      aria-label={aria}
      aria-pressed={recording}
      style={{
        position: 'relative',
        width: SIZE, height: SIZE, borderRadius: 'var(--r-control)',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        border: '1px solid ' + (recording ? 'oklch(0.70 0.18 25 / 0.45)' : 'transparent'),
        background: bg,
        color: iconColor,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled && !recording ? 0.5 : 1,
        transition: 'background .12s, border-color .12s, color .12s',
        flexShrink: 0,
        padding: 0,
      }}
    >
      <Icon name={iconName} size={18} color={iconColor} />
      {showErrorDot && (
        <span
          aria-hidden="true"
          style={{
            position: 'absolute',
            top: 4, right: 4,
            width: 6, height: 6, borderRadius: 'var(--r-pill)',
            background: 'var(--err)',
            boxShadow: '0 0 0 1px var(--surface-1)',
          }}
        />
      )}
    </button>
  );
}

// Publish to window so orchestrator-attachments.jsx can use it as a bare global.
Object.assign(window, { MicButton });
