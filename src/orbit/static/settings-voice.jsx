// SettingsVoice — the "Głos i mowa" tab body of the redesigned Settings panel.
//
// Everything here is per-DEVICE (localStorage via the shell's single
// useSettings instance), so values arrive as props { settings, updateSettings }
// and we render ONE SettingGroup scope="device". Built on the frozen
// design-system primitives published by settings-primitives.jsx
// (window.HubSettings). No bundler / imports — components are read off window.
//
// Ports four legacy rows from the old flat settings-view.jsx, re-grouped:
//   • autoSendVoice toggle
//   • VoiceOutputRow      → voiceOutput + "Test głosu" (iOS audio unlock)
//   • VoiceEngineRow      → ttsEngine (4-way SettingSelect) + ttsVoice picker
//   • SttLanguageRow      → sttLanguage
//   • VoiceConversationRow→ voiceConvEcho + advanced tunables
// Plus surfaces a previously-hidden device flag: voiceConvBargeMinDurationMs.

const {
  useState: _svUseState,
  useEffect: _svUseEffect,
  useCallback: _svUseCallback,
} = React;

const _HS = window.HubSettings;
const {
  SettingGroup,
  SettingCard,
  ToggleRow,
  FieldRow,
  Segmented,
  SettingSelect,
  NumberField,
  AdvancedDisclosure,
} = _HS;

// STT (Whisper) language hint passed to /api/orchestrator/transcribe. Top 5
// cover ~95% of real usage; 'auto' lets Whisper detect.
const _STT_LANG_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'pl',   label: 'Polski' },
  { value: 'en',   label: 'English' },
  { value: 'de',   label: 'Deutsch' },
  { value: 'fr',   label: 'Français' },
  { value: 'es',   label: 'Español' },
];

const _TTS_ENGINE_OPTIONS = [
  { value: 'elevenlabs', label: 'ElevenLabs' },
  { value: 'openai',     label: 'OpenAI' },
  { value: 'gemini',     label: 'Gemini' },
  { value: 'browser',    label: 'Przeglądarka' },
];

// Per-engine descriptive copy (ported verbatim from the legacy VoiceEngineRow).
// `data` is the /tts/voices payload (or null while loading).
function _engineDesc(engine, data) {
  if (!data) return '…';
  if (!data.configured) return 'Brak klucza API skonfigurowanego po stronie serwera.';
  if (engine === 'elevenlabs') return 'Najszybsze: Flash v2.5, ~125ms TTFA. Płatne (free tier 10k znaków/mies).';
  if (engine === 'openai')     return 'Średnia szybkość, dobra jakość. Domyślnie tts-1, można zmienić w backendzie.';
  if (engine === 'gemini')     return 'Wolne (~2s TTFA, brak streamingu) ale free tier. Dobre jako fallback.';
  return 'Lokalny silnik przeglądarki — zero kosztów ale jakość bardzo zmienna.';
}

function _voiceOutputDesc(supported, value) {
  if (!supported) return 'Ta przeglądarka nie wspiera odtwarzania audio.';
  if (value === 'always')   return 'Każda odpowiedź czytana automatycznie.';
  if (value === 'on-voice') return 'Czyta tylko gdy ostatnia wiadomość przyszła z mikrofonu.';
  return 'Tylko ręcznie — kliknij 🔊 przy wiadomości.';
}

// ── voice-output mode + Test głosu (iOS audio-unlock gesture) ──
function _VoiceOutputField({ value, onChange }) {
  const useTtsHook = (typeof window !== 'undefined') ? window.useTts : null;
  const tts = useTtsHook ? useTtsHook() : null;
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function') ? window.useToast() : null;
  const supported = !!(tts && tts.supported);
  const desc = _voiceOutputDesc(supported, value);

  // Picking a non-manual mode is a user gesture — the perfect moment to unlock
  // iOS audio so later automatic speaks aren't blocked by the autoplay policy.
  const onPickMode = (next) => {
    onChange(next);
    if (supported && next !== 'manual' && tts && !tts.unlocked) {
      try { tts.unlock(); } catch (e) { /* ignore */ }
    }
  };

  const onTest = () => {
    if (!tts) return;
    try { tts.speakTest(); } catch (e) { /* ignore */ }
    if (toast) toast('Test głosu', 'info');
  };

  return (
    <FieldRow label="Czytanie odpowiedzi" desc={desc}>
      <div style={{
        display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
        flex: 1, minWidth: 0, opacity: supported ? 1 : 0.6,
      }}>
        <Segmented
          value={value}
          disabled={!supported}
          options={[
            { value: 'manual',   label: 'Ręcznie' },
            { value: 'on-voice', label: 'Po głosie' },
            { value: 'always',   label: 'Zawsze' },
          ]}
          onChange={onPickMode}
        />
        {supported && (
          window.Button
            ? <window.Button variant="ghost" size="sm" icon="volume" onClick={onTest}>Test głosu</window.Button>
            : <button onClick={onTest} style={{
                padding: '6px 10px', borderRadius: 'var(--r-sm)', fontSize: 'var(--t-cap)', fontFamily: 'inherit',
                background: 'var(--surface-2)', color: 'var(--fg-2)',
                border: '1px solid var(--hairline)', cursor: 'pointer',
              }}>Test głosu</button>
        )}
      </div>
    </FieldRow>
  );
}

// ── TTS engine (4-way, RWD select) + curated voice picker ──
// Fetches /api/orchestrator/tts/voices?engine= on mount + engine change. The
// voice select resets to backend default when engine changes (each engine has
// its own voice-id namespace; carrying over would break).
function _VoiceEngineField({ engine, voice, onChange }) {
  const [data, setData] = _svUseState(null);
  const [busy, setBusy] = _svUseState(false);

  const reload = _svUseCallback(async (e) => {
    setBusy(true);
    try {
      const url = window.apiUrl('/api/orchestrator/tts/voices?engine=' + encodeURIComponent(e));
      const r = await fetch(url);
      if (r.ok) setData(await r.json());
      else setData({ engine: e, voices: [], configured: false });
    } catch (err) {
      console.error('tts voices fetch failed:', err);
      setData({ engine: e, voices: [], configured: false });
    } finally {
      setBusy(false);
    }
  }, []);

  _svUseEffect(() => { reload(engine); }, [engine, reload]);

  const desc = _engineDesc(engine, data);
  const hasVoices = engine !== 'browser' && data && Array.isArray(data.voices) && data.voices.length > 0;
  const voiceOptions = hasVoices
    ? data.voices.map((v) => ({ value: v.id, label: v.name }))
    : [];
  const voiceValue = (voice || (data && data.default_voice) || '');

  return (
    <FieldRow label="Silnik TTS" desc={desc}>
      <SettingSelect
        value={engine}
        ariaLabel="Silnik TTS"
        options={_TTS_ENGINE_OPTIONS}
        onChange={(e) => onChange({ ttsEngine: e, ttsVoice: null })}
        minWidth={180}
      />
      {hasVoices && (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>Głos:</span>
          <SettingSelect
            value={voiceValue}
            ariaLabel="Głos TTS"
            options={voiceOptions}
            onChange={(v) => onChange({ ttsVoice: v })}
            disabled={busy}
            minWidth={180}
          />
        </span>
      )}
    </FieldRow>
  );
}

// ── STT language hint ──
function _SttLanguageField({ value, onChange }) {
  const v = value || 'auto';
  const desc = v === 'auto'
    ? 'Whisper sam wykrywa język z nagrania. Dla krótkich / szumowych klipów warto przypiąć konkretny.'
    : 'Whisper zakłada wybrany język — szybciej i dokładniej dla krótkich klipów.';
  return (
    <FieldRow label="Język rozpoznawania mowy" desc={desc}>
      <SettingSelect
        value={v}
        ariaLabel="Język rozpoznawania mowy"
        options={_STT_LANG_OPTIONS}
        onChange={onChange}
        minWidth={160}
      />
    </FieldRow>
  );
}

// ── float input for the AEC barge-in threshold ──
// NumberField is int-only; this RMS threshold is a float (0.02–0.12, step
// 0.005), so we render a small commit-on-change float input styled like the
// primitives' fields. Ported from the legacy VoiceConversationRow.
function _FloatField({ value, min, max, step, onCommit, suffix, width = 96 }) {
  // Commit on blur/Enter (not per-keystroke). A fully-controlled value + clamp on
  // every onChange would snap the input to `min` after the first character (e.g.
  // typing "0.045" clamps "0" → 0.02 and overwrites the draft), making any prefix
  // below min un-typeable. Mirror the NumberField primitive's draft/focused model.
  const [draft, setDraft] = React.useState(String(value));
  const [focused, setFocused] = React.useState(false);
  React.useEffect(() => { if (!focused) setDraft(String(value)); }, [value, focused]);
  const commit = () => {
    const n = parseFloat(draft);
    if (!Number.isFinite(n)) { setDraft(String(value)); return; }
    const clamped = Math.max(min, Math.min(max, n));
    if (clamped !== value) onCommit(clamped); else setDraft(String(value));
  };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <input
        type="number" inputMode="decimal" min={min} max={max} step={step}
        value={draft}
        onFocus={() => setFocused(true)}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { setFocused(false); commit(); }}
        onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
        style={{
          width, minHeight: 38, padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit',
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
          borderRadius: 'var(--r-sm)', color: 'var(--fg)', outline: 'none',
        }} />
      {suffix && <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{suffix}</span>}
    </span>
  );
}

// ── continuous voice conversation (Tryb rozmowy) ──
function _VoiceConversationField({ settings, updateSettings }) {
  const echo = settings.voiceConvEcho || 'aec';
  const silenceMs = Number.isFinite(settings.voiceConvSilenceMs) ? settings.voiceConvSilenceMs : 1200;
  const bargeT = Number.isFinite(settings.voiceConvBargeInThreshold) ? settings.voiceConvBargeInThreshold : 0.045;
  const bargeMinMs = Number.isFinite(settings.voiceConvBargeMinDurationMs) ? settings.voiceConvBargeMinDurationMs : 120;

  const echoDesc = echo === 'aec'
    ? 'Mikrofon słucha cały czas; AEC + RTCPeerConnection loopback filtruje głos AI ze sprzężenia. Pozwala wpaść w słowo. Test w aucie — jak źle, przełącz na "Wycisz mic".'
    : 'Mikrofon wyłączony gdy AI mówi. Stabilne, zero echa. Przerwanie tylko przyciskiem "Przerwij".';

  return (
    <SettingCard>
      <FieldRow label="Tryb rozmowy (auto-głos)" desc={echoDesc}>
        <Segmented
          value={echo}
          options={[
            { value: 'aec',  label: 'AEC loopback' },
            { value: 'mute', label: 'Wycisz mic' },
          ]}
          onChange={(v) => updateSettings({ voiceConvEcho: v })}
        />
      </FieldRow>

      <AdvancedDisclosure label="Zaawansowane (tryb rozmowy)">
        <FieldRow
          label="Cisza przed auto-wysłaniem"
          desc="Ile ciszy (ms) po Twojej wypowiedzi zanim wiadomość pójdzie sama.">
          <NumberField
            value={silenceMs}
            min={400} max={3000}
            suffix="ms"
            onCommit={(n) => updateSettings({ voiceConvSilenceMs: n })}
          />
        </FieldRow>

        {echo === 'aec' && (
          <FieldRow
            label="Próg przerwania (RMS)"
            desc="Głośność (RMS) Twojego głosu, która przerywa odczyt AI (tryb AEC).">
            <_FloatField
              value={bargeT}
              min={0.02} max={0.12} step={0.005}
              onCommit={(n) => updateSettings({ voiceConvBargeInThreshold: n })}
            />
          </FieldRow>
        )}

        {echo === 'aec' && (
          <FieldRow
            label="Min. czas przerwania"
            desc="Jak długo głos musi przekroczyć próg, aby przerwać odczyt (tryb AEC).">
            <NumberField
              value={bargeMinMs}
              min={40} max={1000}
              suffix="ms"
              onCommit={(n) => updateSettings({ voiceConvBargeMinDurationMs: n })}
            />
          </FieldRow>
        )}
      </AdvancedDisclosure>
    </SettingCard>
  );
}

// ── tab body ──
function SettingsVoice({ settings, updateSettings }) {
  return (
    <SettingGroup scope="device" title="Głos i mowa">
      <ToggleRow
        label="Auto-wyślij po nagraniu"
        desc="Po zatrzymaniu mikrofonu wiadomość pójdzie sama."
        value={!!settings.autoSendVoice}
        onChange={(v) => updateSettings({ autoSendVoice: v })}
      />

      <SettingCard>
        <_VoiceOutputField
          value={settings.voiceOutput || 'manual'}
          onChange={(v) => updateSettings({ voiceOutput: v })}
        />
        <_VoiceEngineField
          engine={settings.ttsEngine || 'elevenlabs'}
          voice={settings.ttsVoice}
          onChange={updateSettings}
        />
      </SettingCard>

      <SettingCard>
        <_SttLanguageField
          value={settings.sttLanguage}
          onChange={(v) => updateSettings({ sttLanguage: v })}
        />
      </SettingCard>

      <_VoiceConversationField settings={settings} updateSettings={updateSettings} />
    </SettingGroup>
  );
}

window.SettingsVoice = SettingsVoice;
