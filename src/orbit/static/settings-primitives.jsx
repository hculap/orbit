// Settings design-system primitives + the per-device `useSettings` store.
//
// This is the SHARED CONTRACT for the tabbed Settings redesign: the per-tab
// modules (settings-notifications / -voice / -server / -terminal) and the
// shell (settings-view) all build on the atoms published here. Loaded BEFORE
// every settings-* module in index.html.
//
// Two scopes of state live in Settings and must stay visually distinct:
//   • per-DEVICE  — localStorage via `useSettings` (this hook)
//   • per-SERVER  — ~/.orchestrator/settings.json via `useServerSettings`
//   • server SECRETS — ~/.env via the secrets API (handled in the notif tab)
// `ScopeChip` labels each group so the user always knows the blast radius.

const _SETTINGS_KEY = 'hub-settings';

// Per-device defaults. `voiceConvBargeMinDurationMs` is consumed by
// orchestrator-conversation.jsx (AEC barge-in) and is now surfaced in the
// Głos tab alongside its sibling threshold.
const _SETTINGS_DEFAULTS = {
  unreadSound: true, autoSendVoice: false, voiceOutput: 'manual',
  ttsEngine: 'elevenlabs', ttsVoice: null,
  showToolActions: true,
  // STT (Whisper) language hint. 'pl' is the user's default; 'auto' lets
  // Whisper detect, other ISO 639-1 codes pin a language.
  sttLanguage: 'pl',
  // Continuous voice conversation (Tryb rozmowy). 'mute' = half-duplex (mic
  // off during TTS); 'aec' = AEC loopback for hands-free barge-in.
  voiceConvEcho: 'mute',
  voiceConvSilenceMs: 1200,
  // AEC barge-in: RMS must exceed threshold for >= minDuration ms to count as
  // user speech during playback.
  voiceConvBargeInThreshold: 0.045,
  voiceConvBargeMinDurationMs: 120,
};

const { useState: _spUseState, useEffect: _spUseEffect, useCallback: _spUseCallback, useRef: _spUseRef } = React;

function _readSettings() {
  try {
    const raw = localStorage.getItem(_SETTINGS_KEY);
    return raw ? { ..._SETTINGS_DEFAULTS, ...JSON.parse(raw) } : { ..._SETTINGS_DEFAULTS };
  } catch (e) { return { ..._SETTINGS_DEFAULTS }; }
}

// Per-device settings store. Cross-component sync via the `hub:settings-change`
// window event so any consumer (orchestrator-unread / -conversation / etc.)
// updates immediately when a toggle changes. External modules read this via
// `window.useSettings` — keep the contract identical.
function useSettings() {
  const [settings, setSettings] = _spUseState(_readSettings);
  const update = _spUseCallback((patch) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      try { localStorage.setItem(_SETTINGS_KEY, JSON.stringify(next)); } catch (e) { /* quota / private mode */ }
      window.dispatchEvent(new CustomEvent('hub:settings-change', { detail: next }));
      return next;
    });
  }, []);
  _spUseEffect(() => {
    const onChange = (e) => { if (e && e.detail) setSettings(e.detail); };
    window.addEventListener('hub:settings-change', onChange);
    return () => window.removeEventListener('hub:settings-change', onChange);
  }, []);
  return [settings, update];
}

// ── server-settings store (deduped from the old Server/Ttyd/AutoTitles rows) ──
// GET /api/orchestrator/settings on mount; `patch(delta)` is optimistic. Two
// concurrency hazards are guarded explicitly (the server echoes the FULL
// settings object from both GET and PATCH, not the changed delta):
//   1. A patch merges back ONLY the keys it wrote — so a patch to one key never
//      clobbers a concurrent in-flight edit to a different key.
//   2. A `dirtyKeys` ref records keys the user has written this mount (added at
//      patch START). The mount GET applies server values for every key EXCEPT
//      dirty ones, so a stale GET that resolves after a patch can't revert the
//      just-saved value.
// ONE instance is hoisted in the shell (settings-view.jsx) and passed to the
// Serwer + Terminal tabs as a prop — one GET per Settings-open, no per-tab
// refetch, no shared module-level cache.
function useServerSettings() {
  const [cfg, setCfg] = _spUseState({});
  const [bounds, setBounds] = _spUseState({});
  const [loaded, setLoaded] = _spUseState(false);
  const [busy, setBusy] = _spUseState(false);
  const dirtyRef = _spUseRef(null);
  if (dirtyRef.current === null) dirtyRef.current = new Set();

  _spUseEffect(() => {
    let cancelled = false;
    const doFetch = async () => {
      try {
        const r = await fetch(window.apiUrl('/api/orchestrator/settings'));
        if (!r.ok) throw new Error('GET /settings → ' + r.status);
        const data = await r.json();
        if (cancelled || !data || typeof data !== 'object') return;
        const { _bounds, ...rest } = data;
        // Apply every fresh key EXCEPT ones the user is actively writing, so an
        // in-flight patch's value isn't reverted by this (possibly stale) GET.
        setCfg((prev) => {
          const next = { ...prev };
          for (const k of Object.keys(rest)) if (!dirtyRef.current.has(k)) next[k] = rest[k];
          return next;
        });
        if (_bounds && typeof _bounds === 'object') setBounds((prev) => ({ ...prev, ..._bounds }));
      } catch (e) { /* keep defaults; backend may be momentarily down */ }
      finally { if (!cancelled) setLoaded(true); }
    };
    doFetch();
    // Cross-instance sync: another component's patch() broadcasts this so every
    // live useServerSettings re-reads — e.g. the always-mounted orchestrator
    // view picks up a flag toggled in the Settings tab without a reload (it
    // would otherwise never refetch and the change wouldn't take effect live).
    const onChange = () => { doFetch(); };
    window.addEventListener('hub:server-settings-change', onChange);
    return () => {
      cancelled = true;
      window.removeEventListener('hub:server-settings-change', onChange);
    };
  }, []);

  const patch = _spUseCallback(async (delta) => {
    const keys = Object.keys(delta);
    keys.forEach((k) => dirtyRef.current.add(k));  // protect from a stale in-flight GET, immediately
    let prevSnapshot = null;
    setCfg((prev) => { prevSnapshot = prev; return { ...prev, ...delta }; });
    setBusy(true);
    try {
      const r = await fetch(window.apiUrl('/api/orchestrator/settings'), {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(delta),
      });
      if (!r.ok) throw new Error('PATCH /settings → ' + r.status);
      const data = await r.json();
      // Merge back ONLY our keys (server-coerced/clamped), never the full object,
      // so we don't overwrite a different key written by a concurrent patch.
      if (data && typeof data === 'object') {
        setCfg((prev) => {
          const next = { ...prev };
          for (const k of keys) if (k in data) next[k] = data[k];
          return next;
        });
        if (data._bounds && typeof data._bounds === 'object') setBounds((prev) => ({ ...prev, ...data._bounds }));
      }
      // Tell other live useServerSettings instances to re-read (this instance
      // already applied the change optimistically + via the merge above), so a
      // flag flip in Settings takes effect in the orchestrator view immediately.
      try { window.dispatchEvent(new Event('hub:server-settings-change')); } catch (_e) { /* ignore */ }
      return true;
    } catch (e) {
      console.error('server settings patch failed:', e);
      // Revert ONLY the keys we wrote; they now match the server again, so let a
      // future GET correct them (un-dirty).
      if (prevSnapshot) {
        setCfg((prev) => {
          const out = { ...prev };
          for (const k of keys) out[k] = prevSnapshot[k];
          return out;
        });
      }
      keys.forEach((k) => dirtyRef.current.delete(k));
      return false;
    } finally { setBusy(false); }
  }, []);

  return { cfg, bounds, loaded, busy, patch };
}

function _useToast() {
  return (typeof window !== 'undefined' && typeof window.useToast === 'function') ? window.useToast() : null;
}

// ── byte helper (prompt editors) ──
function _sByteLength(str) {
  if (!str) return 0;
  if (typeof TextEncoder !== 'undefined') {
    try { return new TextEncoder().encode(str).length; } catch (_) { /* fallthrough */ }
  }
  return str.length;
}

// ─────────────────────────────────────────────────────────────
// UI ATOMS
// ─────────────────────────────────────────────────────────────

// Toggle — 44x26 visual switch wrapped in a >=44px transparent hit target so
// it satisfies the WCAG/touch minimum without changing the visual size.
function Toggle({ checked, onChange, disabled }) {
  return (
    <button
      onClick={() => { if (!disabled) onChange(!checked); }}
      role="switch" aria-checked={!!checked} disabled={!!disabled}
      style={{
        flexShrink: 0, border: 'none', background: 'transparent', padding: 9,
        margin: -9, cursor: disabled ? 'not-allowed' : 'pointer',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        opacity: disabled ? 0.5 : 1, minWidth: 44, minHeight: 44,
      }}>
      <span style={{
        width: 44, height: 26, borderRadius: 13, display: 'block',
        background: checked ? 'var(--accent)' : 'var(--surface-3)',
        border: '1px solid ' + (checked ? 'var(--accent-line)' : 'var(--hairline-strong)'),
        position: 'relative', transition: 'background .15s',
      }}>
        <span style={{
          position: 'absolute', top: 2, left: checked ? 20 : 2,
          width: 20, height: 20, borderRadius: '50%',
          background: checked ? 'var(--accent-fg)' : 'var(--fg-2)',
          transition: 'left .15s, background .15s',
        }} />
      </span>
    </button>
  );
}

// Segmented / SettingSelect / StatusBanner now DELEGATE to the global canonical
// primitives published by ds-forms.jsx / ds-overlays.jsx (design-system v2 —
// the "promote to global + keep a thin HubSettings alias" reconciliation). They
// load before this file (see index.html), so the globals are present here.
//
// CRITICAL — do NOT reintroduce a same-named `function Segmented()/StatusBanner()`
// wrapper. There is no bundler: every file runs in the GLOBAL scope, so a top-level
// function declaration is (a) hoisted to the very top of this file and (b) leaks
// onto `window`. A wrapper named `Segmented` therefore overwrites the canonical
// `window.Segmented` BEFORE the `const _dsSegmented = window.Segmented` capture
// below even executes — so the capture grabs the wrapper itself and the wrapper
// delegates to ITSELF: infinite render recursion that freezes any page rendering
// it (this is exactly the bug that hung /tasks). `SettingSelect` is safe only
// because its name differs from the `Select` it captures. The aliases are plain
// pass-throughs, so just point the HubSettings contract straight at the captured
// canonical refs — no wrapper component needed.
const _dsSegmented = window.Segmented;
const _dsSelect = window.Select;
const _dsStatusBanner = window.StatusBanner;

// SettingSelect — delegates to the global Select; keeps the 160px default width
// the settings tabs rely on. options = [{value, label}]. (Name differs from
// `Select`, so it is safe as a function declaration — see the note above.)
function SettingSelect(props) { return React.createElement(_dsSelect, { minWidth: 160, ...props }); }

// NumberField — commit-on-blur (or Enter), not per-keystroke, to avoid the
// "typing 12 sends 1 then 12" double-PATCH race. The committed value flows back
// via the `value` prop; until then the local typed draft shows. inputMode
// numeric for the mobile numeric keypad.
function NumberField({ value, min, max, onCommit, disabled, suffix, width = 96, step = 1 }) {
  const [draft, setDraft] = _spUseState(String(value));
  const [focused, setFocused] = _spUseState(false);
  const isFloat = !Number.isInteger(step);
  _spUseEffect(() => { if (!focused) setDraft(String(value)); }, [value, focused]);
  const commit = () => {
    const n = isFloat ? parseFloat(draft) : parseInt(draft, 10);
    if (Number.isFinite(n) && (min == null || n >= min) && (max == null || n <= max) && n !== value) onCommit(n);
    else setDraft(String(value));  // revert invalid / unchanged
  };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <input
        type="number" inputMode={isFloat ? 'decimal' : 'numeric'} min={min} max={max} step={step} value={draft}
        disabled={!!disabled}
        onFocus={() => setFocused(true)}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { setFocused(false); commit(); }}
        onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
        style={{
          width, minHeight: 38, padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit',
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
          borderRadius: 'var(--r-sm)', color: 'var(--fg)', outline: 'none',
          opacity: disabled ? 0.5 : 1,
        }} />
      {suffix && <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{suffix}</span>}
    </span>
  );
}

// SettingCard — the standard surface-1 box. Delegates to the global Card
// (variant='inset' is byte-identical to the old SettingCard: 14×16 padding,
// --r-md, surface-1/hairline, flex-column gap 12). Alias kept for the contract.
function SettingCard({ children, style }) {
  return <window.Card variant="inset" gap={12} style={style}>{children}</window.Card>;
}

// SettingRow — label + description on the left, a control node on the right.
// Delegates to the global Field(layout='row') (same flex/space-between/wrap shell
// + label/hint typography; Field renders a semantic <label>). disabled dims it.
function SettingRow({ label, desc, control, disabled }) {
  return (
    <window.Field label={label} hint={desc} layout="row" style={disabled ? { opacity: 0.6 } : undefined}>
      {control}
    </window.Field>
  );
}

// ToggleRow — the most common row: a labeled boolean inside its own card.
function ToggleRow({ label, desc, value, onChange, disabled }) {
  return (
    <SettingCard>
      <SettingRow label={label} desc={desc} disabled={disabled}
        control={<Toggle checked={!!value} onChange={onChange} disabled={disabled} />} />
    </SettingCard>
  );
}

// FieldRow — label/description stacked ABOVE a wide control (select / segmented
// / number cluster). Used when the control is too wide to sit inline.
function FieldRow({ label, desc, children }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div>
        <div style={{ fontSize: 'var(--t-md)', fontWeight: 500, color: 'var(--fg)' }}>{label}</div>
        {desc && <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2, lineHeight: 1.45 }}>{desc}</div>}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>{children}</div>
    </div>
  );
}

const _SCOPE_META = {
  device: { label: 'To urządzenie', color: 'var(--info)', icon: 'cpu' },
  server: { label: 'Cały serwer', color: 'var(--warn)', icon: 'globe' },
  secrets: { label: 'Sekrety serwera', color: 'var(--warn)', icon: 'cog' },
};

// ScopeChip — a small pill stating the blast radius of a group's settings.
// Replaces the old, misleading single "local to this device" header.
function ScopeChip({ scope }) {
  const meta = _SCOPE_META[scope] || _SCOPE_META.device;
  return (
    <span className="mono" style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '2px 8px', borderRadius: 'var(--r-pill)', fontSize: 'var(--t-2xs)',
      color: meta.color, border: '1px solid ' + meta.color, background: 'transparent',
      whiteSpace: 'nowrap', flexShrink: 0,
    }}>
      {window.Icon ? <window.Icon name={meta.icon} size={11} color={meta.color} /> : null}
      {meta.label}
    </span>
  );
}

// SettingGroup — a titled cluster of cards (the "boxes" grouping). Header shows
// the title, an optional ScopeChip, and an optional description.
function SettingGroup({ title, desc, scope, children, style }) {
  return (
    <section style={{ display: 'flex', flexDirection: 'column', gap: 8, ...style }}>
      {(title || scope) && (
        <header style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', margin: '4px 0 2px' }}>
          {title && <h3 style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg)', margin: 0, letterSpacing: '-0.01em' }}>{title}</h3>}
          {scope && <ScopeChip scope={scope} />}
        </header>
      )}
      {desc && <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: -2, marginBottom: 2, lineHeight: 1.5 }}>{desc}</div>}
      {children}
    </section>
  );
}

// AdvancedDisclosure — collapsible "▸ Zaawansowane" section for rollback levers
// / niche tunables that should not crowd the default view. Uses native
// <details> so it works without extra state and is keyboard-accessible.
function AdvancedDisclosure({ label = 'Zaawansowane', children }) {
  return (
    <details style={{ marginTop: 2 }}>
      <summary style={{
        cursor: 'pointer', userSelect: 'none', listStyle: 'none',
        display: 'inline-flex', alignItems: 'center', gap: 6,
        fontSize: 'var(--t-cap)', color: 'var(--fg-3)', padding: '6px 2px',
      }}>
        {window.Icon ? <window.Icon name="chevron-r" size={13} color="var(--fg-3)" /> : '▸'}
        {label}
      </summary>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 8 }}>
        {children}
      </div>
    </details>
  );
}

// StatusBanner — the legacy {color, label, action} API is preserved by the
// global ds-overlays implementation, so the HubSettings alias is just the
// captured canonical ref (`_dsStatusBanner`). See the no-wrapper note above.

// Published contract. `useSettings` + `Toggle` + `Segmented` keep their legacy
// bare-window names for any external consumer; everything new is namespaced
// under window.HubSettings. Segmented/StatusBanner alias straight to the
// captured canonical primitives (no same-named wrapper — see the note above).
window.HubSettings = {
  useSettings, useServerSettings, _SETTINGS_DEFAULTS, _sByteLength, _useToast,
  Toggle, Segmented: _dsSegmented, SettingSelect, NumberField,
  SettingCard, SettingRow, ToggleRow, FieldRow,
  ScopeChip, SettingGroup, AdvancedDisclosure, StatusBanner: _dsStatusBanner,
};
Object.assign(window, { useSettings, Toggle, Segmented: _dsSegmented });
