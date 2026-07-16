// Terminal shortcut layout EDITOR — the full views+buttons manager body.
//
// Ported from the old flat settings-view.jsx (TerminalShortcutsCard + its two
// modals). This module owns ONLY the editor body (the views list, per-view
// expand, per-button rows, the add/save/reset chrome, and the Button/View edit
// modals). The enable flag + the summary card live in the Terminal tab
// (settings-terminal.jsx), which opens this in a drawer.
//
// Render/helpers come from window.HubShortcuts (orchestrator-shortcuts.jsx):
//   refresh / saveLayout / resetLayout         — server store
//   labelForButton / kindLabel / labelForDescriptor / decodeEscapes
//   paletteMatch / descriptorFromEvent / descriptorFromParts / KEY_PALETTE
//
// RWD: per-button + per-view rows keep ONLY [chip][label/hint][Toggle] inline;
// move / pin / edit / delete all live in a KebabMenu so a 360px viewport never
// crushes the label. Mini buttons are 36px (touch minimum).

const { useState: _seUseState, useEffect: _seUseEffect } = React;
const _seToggle = (window.HubSettings && window.HubSettings.Toggle) || window.Toggle;
const _seSegmented = (window.HubSettings && window.HubSettings.Segmented) || window.Segmented;

const _scBtnGhost = {
  padding: '8px 14px', borderRadius: 'var(--r-control)', fontSize: 'var(--t-sm)', cursor: 'pointer',
  background: 'var(--surface-2)', color: 'var(--fg)', border: '1px solid var(--hairline)',
  fontFamily: 'inherit',
};
function _scBtnPrimary(disabled) {
  return {
    padding: '8px 14px', borderRadius: 'var(--r-control)', fontSize: 'var(--t-sm)', fontFamily: 'inherit',
    cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.5 : 1,
    background: 'var(--accent)', color: 'var(--accent-fg)', border: '1px solid var(--accent-line)',
  };
}

// Key-order-insensitive serialization for the dirty check (array order — which
// IS significant for reorder — is preserved; only object key order is normalized).
function _stableStringify(obj) {
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return '[' + obj.map(_stableStringify).join(',') + ']';
  return '{' + Object.keys(obj).sort().map((k) => JSON.stringify(k) + ':' + _stableStringify(obj[k])).join(',') + '}';
}

// ── layout edit helpers (immutable; operate on the inner {views:[...]}) ──
function _uid() { return 'b-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
function _moveIdx(arr, idx, dir) {
  const j = idx + dir;
  if (j < 0 || j >= arr.length) return arr;
  const next = arr.slice();
  const [it] = next.splice(idx, 1);
  next.splice(j, 0, it);
  return next;
}
const _KIND_OPTIONS = [
  { value: 'send-key', label: 'Klawisz' },
  { value: 'send-raw', label: 'Raw' },
  { value: 'paste-text', label: 'Tekst' },
  { value: 'slash-command', label: '/Komenda' },
  { value: 'special', label: 'Akcja' },
];
const _SPECIAL_ACTIONS = [
  { value: 'upload', label: 'Plik / upload' },
  { value: 'microphone', label: 'Mikrofon' },
  { value: 'clipboard-paste', label: 'Wklej ze schowka' },
  { value: 'session-switcher', label: 'Przełącznik sesji' },
];
// '' = no icon (text-only). The rest are names from the components.jsx Icon set
// (monochrome line-icons) — kept broad so most buttons can get a fitting glyph.
// The picker shows the first `_ICON_COLLAPSED_COUNT` by default and reveals the
// rest behind a "więcej ikon" toggle.
const _ICON_CHOICES = [
  '', 'terminal', 'cmd', 'send', 'check', 'close', 'plus', 'pencil',
  'arrow-up', 'chevron-d', 'chevron-l', 'chevron-r', 'corner-up-left',
  'home', 'search', 'filter', 'menu', 'maximize', 'minimize', 'panel-r', 'refresh',
  'square', 'circle', 'star', 'star-fill', 'pin', 'pin-fill', 'bell', 'bell-fill', 'eye',
  'bulb', 'sparkle', 'cog', 'help', 'clock', 'copy', 'attach', 'upload', 'download', 'share',
  'trash', 'file', 'pdf', 'notepad', 'folder', 'box', 'archive', 'inbox', 'book',
  'tasks', 'list-checks', 'logs', 'globe', 'mic', 'mic-fill', 'headphones', 'volume',
  'video', 'image', 'bot', 'cpu',
  // extended set — revealed by "więcej ikon"
  'play', 'pause', 'stop-circle', 'skip', 'heart', 'heart-fill', 'flag', 'bookmark',
  'tag', 'link', 'lock', 'unlock', 'key', 'shield', 'wifi', 'cloud', 'database',
  'server', 'code', 'git-branch', 'zap', 'flame', 'rocket', 'calendar', 'map-pin',
  'message', 'at-sign', 'hash', 'sliders', 'grid', 'layers', 'save', 'external',
  'sun', 'moon', 'coffee', 'music', 'compass', 'gift',
];
const _ICON_COLLAPSED_COUNT = 30;

// Preset colors for the icon / background pickers. '' = unset (use the default
// design-system color). `var(--accent)` rides the theme; the rest are fixed hex.
const _COLOR_PRESETS = [
  'var(--accent)', '#ef4444', '#f59e0b', '#eab308', '#22c55e',
  '#06b6d4', '#3b82f6', '#a855f7', '#ec4899', '#94a3b8',
];
function _defaultPayload(kind) {
  if (kind === 'send-raw') return { data: '' };
  if (kind === 'paste-text') return { text: '', submit: false };
  if (kind === 'slash-command') return { command: '', submit: true };
  if (kind === 'special') return { actionType: 'upload' };
  return {};  // send-key descriptor (captured) / modifier
}
// Mirror the backend validators so Save can't pass something the server will
// silently drop: _COMMAND_OK == _COMMAND_RE; _RAW_BAD_ESCAPE matches a backslash
// that does NOT start a valid escape (\n \t \r \0 \\ or \xHH) — same set the
// backend _decode_raw accepts.
const _COMMAND_OK = /^[A-Za-z0-9:_-]+$/;
const _RAW_BAD_ESCAPE = /\\(?!x[0-9a-fA-F]{2}|[ntr0\\])/;
const _MODIFIERS = ['ctrlKey', 'altKey', 'metaKey'];
function _buttonValid(btn) {
  const p = btn.payload || {};
  if (btn.kind === 'send-key') return !!(p.key || p.code || p.keyCode);
  if (btn.kind === 'send-raw') return !!(p.data && p.data.trim()) && !_RAW_BAD_ESCAPE.test(p.data);
  if (btn.kind === 'paste-text') return !!(p.text && p.text.length);
  if (btn.kind === 'slash-command') return _COMMAND_OK.test((p.command || '').replace(/^\/+/, ''));
  if (btn.kind === 'special') return _SPECIAL_ACTIONS.some((a) => a.value === p.actionType);
  if (btn.kind === 'modifier') return _MODIFIERS.includes(p.modifier);  // seeded sticky Ctrl/Alt/Cmd — editable label/icon/pin
  return false;
}
function _countButtons(layout) {
  const views = (layout && Array.isArray(layout.views)) ? layout.views : [];
  return views.reduce((n, v) => n + (Array.isArray(v.buttons) ? v.buttons.length : 0), 0);
}

const _scRowStyle = (hidden) => ({
  display: 'flex', alignItems: 'center', gap: 8, padding: '7px 9px',
  borderRadius: 'var(--r-control)', background: 'var(--surface-2)', border: '1px solid var(--hairline)',
  opacity: hidden ? 0.5 : 1,
});

// Compact wrapping icon picker (empty = "text only"). Collapsed to the first
// _ICON_COLLAPSED_COUNT glyphs with a "więcej ikon" toggle; auto-expands when the
// current value lives in the extended set so the selection stays visible.
function _IconPicker({ value, onChange }) {
  const cur = value || '';
  const hiddenSelected = !!cur && _ICON_CHOICES.indexOf(cur) >= _ICON_COLLAPSED_COUNT;
  const [showAll, setShowAll] = _seUseState(false);
  const expanded = showAll || hiddenSelected;
  const list = expanded ? _ICON_CHOICES : _ICON_CHOICES.slice(0, _ICON_COLLAPSED_COUNT);
  const toggleStyle = {
    height: 36, padding: '0 10px', borderRadius: 'var(--r-sm)', cursor: 'pointer', flexShrink: 0,
    display: 'inline-flex', alignItems: 'center', gap: 4, fontFamily: 'inherit', fontSize: 'var(--t-xs)',
    background: 'var(--surface-1)', border: '1px dashed var(--hairline)', color: 'var(--fg-3)',
  };
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {list.map((name) => {
        const active = cur === name;
        return (
          <button key={name || 'none'} type="button" onClick={() => onChange(name)} title={name || 'bez ikony'} style={{
            width: 36, height: 36, borderRadius: 'var(--r-sm)', cursor: 'pointer', flexShrink: 0,
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            background: active ? 'var(--accent)' : 'var(--surface-1)',
            border: '1px solid ' + (active ? 'var(--accent-line)' : 'var(--hairline)'),
          }}>{name ? <Icon name={name} size={16} color={active ? 'var(--accent-fg)' : 'var(--fg-2)'} /> : <span className="mono" style={{ fontSize: 'var(--t-xs)', color: active ? 'var(--accent-fg)' : 'var(--fg-3)' }}>Aa</span>}</button>
        );
      })}
      {!expanded && (
        <button type="button" onClick={() => setShowAll(true)} title="Więcej ikon" style={toggleStyle}>
          +{_ICON_CHOICES.length - _ICON_COLLAPSED_COUNT} <Icon name="chevron-d" size={13} color="var(--fg-3)" />
        </button>
      )}
      {showAll && !hiddenSelected && (
        <button type="button" onClick={() => setShowAll(false)} title="Zwiń" style={toggleStyle}>
          Mniej <Icon name="chevron-l" size={13} color="var(--fg-3)" />
        </button>
      )}
    </div>
  );
}

// Color picker for icon / background: a "Domyślny" (unset) chip, preset swatches,
// and a native custom-color well. onChange('') clears the override.
function _ColorPicker({ value, onChange }) {
  const cur = value || '';
  const isPreset = _COLOR_PRESETS.indexOf(cur) !== -1;
  const customHex = (cur && !isPreset && /^#[0-9a-fA-F]{3,8}$/.test(cur)) ? cur : '';
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
      <button type="button" onClick={() => onChange('')} title="Domyślny (bez koloru)" style={{
        height: 28, padding: '0 10px', borderRadius: 'var(--r-sm)', cursor: 'pointer', fontFamily: 'inherit', fontSize: 'var(--t-xs)',
        background: !cur ? 'var(--accent)' : 'var(--surface-1)',
        color: !cur ? 'var(--accent-fg)' : 'var(--fg-3)',
        border: '1px solid ' + (!cur ? 'var(--accent-line)' : 'var(--hairline)'),
      }}>Domyślny</button>
      {_COLOR_PRESETS.map((c) => (
        <button key={c} type="button" onClick={() => onChange(c)} title={c} style={{
          width: 28, height: 28, borderRadius: 'var(--r-sm)', cursor: 'pointer', flexShrink: 0, padding: 0,
          background: c, border: '1px solid var(--hairline)',
          boxShadow: cur === c ? '0 0 0 2px var(--accent-line)' : 'none',
        }} />
      ))}
      <label title="Własny kolor" style={{
        width: 28, height: 28, borderRadius: 'var(--r-sm)', cursor: 'pointer', flexShrink: 0, position: 'relative',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        background: customHex || 'var(--surface-1)',
        border: '1px solid ' + (customHex ? 'var(--accent-line)' : 'var(--hairline)'),
        boxShadow: customHex ? '0 0 0 2px var(--accent-line)' : 'none',
      }}>
        {!customHex && <Icon name="pencil" size={13} color="var(--fg-3)" />}
        <input type="color" value={customHex || '#888888'} onChange={(e) => onChange(e.target.value)}
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', opacity: 0, cursor: 'pointer', border: 'none', padding: 0 }} />
      </label>
    </div>
  );
}

// Add/edit a button of any KIND. Desktop Modal; kind-specific fields below the
// KIND picker, then shared metadata (label, icon, pin).
function _ButtonEditModal({ open, value, onClose, onSave }) {
  const HS = window.HubShortcuts;
  const [btn, setBtn] = _seUseState(null);
  const captureRef = React.useRef(null);
  _seUseEffect(() => {
    if (!open) return;
    setBtn(value
      ? JSON.parse(JSON.stringify(value))
      : { id: _uid(), kind: 'send-key', label: '', hint: '', icon: '', pinned: false, payload: {} });
    const t = setTimeout(() => { try { if (captureRef.current) captureRef.current.focus(); } catch (e) {} }, 60);
    return () => clearTimeout(t);
  }, [open, value]);
  if (!open || !btn || !HS) return null;

  const p = btn.payload || {};
  const set = (patch) => setBtn((b) => ({ ...b, ...patch }));
  const setPayload = (patch) => setBtn((b) => ({ ...b, payload: { ...b.payload, ...patch } }));
  const setKind = (kind) => setBtn((b) => ({ ...b, kind, payload: _defaultPayload(kind) }));

  // send-key capture (reuses the v1 capture UX)
  const desc = btn.kind === 'send-key' ? p : null;
  const base = desc ? HS.paletteMatch(desc) : null;
  const mods = { ctrlKey: !!(desc && desc.ctrlKey), altKey: !!(desc && desc.altKey), shiftKey: !!(desc && desc.shiftKey), metaKey: !!(desc && desc.metaKey) };
  const baseForMods = base || (desc ? { key: desc.key, code: desc.code, keyCode: desc.keyCode, which: desc.which } : null);
  const onKeyDown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose(); return; }
    const d = HS.descriptorFromEvent(e);
    if (d) { e.preventDefault(); e.stopPropagation(); set({ payload: d }); }
  };
  const rawDecoded = btn.kind === 'send-raw' ? HS.decodeEscapes(p.data || '') : '';
  const rawHex = rawDecoded ? Array.from(rawDecoded).map((c) => c.charCodeAt(0).toString(16).padStart(2, '0')).join(' ') : '';
  const fieldStyle = { padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit', background: 'var(--surface-2)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-sm)', color: 'var(--fg)', width: '100%', boxSizing: 'border-box' };
  const lblStyle = { display: 'flex', flexDirection: 'column', gap: 4 };
  const capStyle = { fontSize: 'var(--t-xs)', color: 'var(--fg-3)' };
  const valid = _buttonValid(btn);

  return (
    <window.Modal open={open} onClose={onClose} title={value ? 'Edytuj przycisk' : 'Nowy przycisk'} width={460}>
      <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <label style={lblStyle}>
          <span className="mono" style={capStyle}>Typ</span>
          {btn.kind === 'modifier'
            ? <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', padding: '6px 0' }}>Modyfikator sticky ({(btn.payload && btn.payload.modifier) || ''}) — edytuj etykietę / ikonę / przypięcie</span>
            : <_seSegmented value={btn.kind} options={_KIND_OPTIONS} onChange={setKind} />}
        </label>

        {btn.kind === 'send-key' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'center' }}>
              <div className="mono" style={{ fontSize: 'var(--t-h2)', fontWeight: 600, padding: '8px 16px', borderRadius: 'var(--r-control)', background: 'var(--surface-2)', border: '1px solid var(--hairline-strong)', minWidth: 80, textAlign: 'center' }}>{HS.labelForDescriptor(desc, '—') || '—'}</div>
            </div>
            <div ref={captureRef} tabIndex={0} onKeyDown={onKeyDown} style={{ outline: 'none', textAlign: 'center', padding: 9, borderRadius: 'var(--r-control)', border: '1px dashed var(--hairline-strong)', color: 'var(--fg-3)', fontSize: 'var(--t-cap)', cursor: 'text' }}>Kliknij i naciśnij klawisz (desktop) — albo zbuduj poniżej ↓</div>
            <Select
              value={base ? base.code : ''}
              onChange={(val) => { const b = HS.KEY_PALETTE.find((x) => x.code === val); if (b) set({ payload: HS.descriptorFromParts(b, mods) }); }}
              options={[...(!base ? [{ value: '', label: '(przechwycony klawisz)' }] : []), ...HS.KEY_PALETTE.map((k) => ({ value: k.code, label: k.label }))]}
            />
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {[['ctrlKey', 'Ctrl'], ['altKey', 'Alt'], ['shiftKey', 'Shift'], ['metaKey', '⌘']].map(([m, lbl]) => (
                <button key={m} disabled={!baseForMods} onClick={() => baseForMods && set({ payload: HS.descriptorFromParts(baseForMods, { ...mods, [m]: !mods[m] }) })} style={{
                  padding: '6px 12px', borderRadius: 'var(--r-pill)', fontSize: 'var(--t-cap)', fontFamily: 'inherit', cursor: baseForMods ? 'pointer' : 'not-allowed',
                  background: mods[m] ? 'var(--accent)' : 'var(--surface-2)', color: mods[m] ? 'var(--accent-fg)' : 'var(--fg)',
                  border: '1px solid ' + (mods[m] ? 'var(--accent-line)' : 'var(--hairline)'),
                }}>{lbl}</button>
              ))}
            </div>
          </div>
        )}

        {btn.kind === 'send-raw' && (
          <label style={lblStyle}>
            <span className="mono" style={capStyle}>Sekwencja (escapes: \n, \t, \x1b ESC, \x00 prefiks tmux C-Space)</span>
            <Textarea value={p.data || ''} onChange={(val) => setPayload({ data: val })} rows={2} maxLength={500} placeholder="np. \x00v  (tmux: split pionowy)" mono style={{ resize: 'vertical' }} />
            {rawHex && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>bajty: {rawHex}</span>}
          </label>
        )}

        {btn.kind === 'paste-text' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <label style={lblStyle}><span className="mono" style={capStyle}>Tekst do wklejenia</span>
              <Input value={p.text || ''} maxLength={2000} onChange={(val) => setPayload({ text: val })} placeholder="np. ultrathink " /></label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10 }}><_seToggle checked={!!p.submit} onChange={(v) => setPayload({ submit: v })} /><span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>Wyślij od razu (Enter)</span></label>
          </div>
        )}

        {btn.kind === 'slash-command' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <label style={lblStyle}><span className="mono" style={capStyle}>Komenda (bez /)</span>
              <Input value={p.command || ''} maxLength={64} onChange={(val) => setPayload({ command: val.replace(/^\/+/, '') })} placeholder="np. compact" /></label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10 }}><_seToggle checked={p.submit !== false} onChange={(v) => setPayload({ submit: v })} /><span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>Wyślij od razu (Enter)</span></label>
          </div>
        )}

        {btn.kind === 'special' && (
          <label style={lblStyle}><span className="mono" style={capStyle}>Akcja</span>
            <Select value={p.actionType || ''} onChange={(val) => setPayload({ actionType: val })} options={_SPECIAL_ACTIONS} /></label>
        )}

        <label style={lblStyle}><span className="mono" style={capStyle}>Etykieta (tekst na przycisku — puste = ikona / auto)</span>
          <Input value={btn.label || ''} maxLength={40} onChange={(val) => set({ label: val })} /></label>
        <label style={lblStyle}><span className="mono" style={capStyle}>Ikona</span><_IconPicker value={btn.icon} onChange={(name) => set({ icon: name })} /></label>
        <label style={lblStyle}><span className="mono" style={capStyle}>Kolor ikony (domyślnie nieustawiony)</span>
          <_ColorPicker value={btn.iconColor} onChange={(c) => setBtn((b) => { const n = { ...b }; if (c) n.iconColor = c; else delete n.iconColor; return n; })} /></label>
        <label style={lblStyle}><span className="mono" style={capStyle}>Tło przycisku (domyślnie nieustawione)</span>
          <_ColorPicker value={btn.bgColor} onChange={(c) => setBtn((b) => { const n = { ...b }; if (c) n.bgColor = c; else delete n.bgColor; return n; })} /></label>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="mono" style={capStyle}>Podgląd</span>
          <span style={{
            minWidth: 40, height: 40, padding: '0 8px', borderRadius: 'var(--r-sm)',
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            background: btn.bgColor || 'var(--surface-2)', border: '1px solid var(--hairline)',
            color: btn.iconColor || 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'JetBrains Mono, monospace',
          }}>
            {btn.icon
              ? <Icon name={btn.icon} size={18} color={btn.iconColor || 'var(--fg-2)'} />
              : (btn.label || (HS.labelForButton ? HS.labelForButton(btn) : '·'))}
          </span>
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 10 }}><_seToggle checked={!!btn.pinned} onChange={(v) => setBtn((b) => { const n = { ...b }; if (v) n.pinned = true; else delete n.pinned; return n; })} /><span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>Przypnij na początek</span></label>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
          <button onClick={onClose} style={_scBtnGhost}>Anuluj</button>
          <Button variant="primary" disabled={!valid} onClick={() => { onSave(btn); onClose(); }}>Zapisz</Button>
        </div>
      </div>
    </window.Modal>
  );
}

// Edit a view's label + icon.
function _ViewEditModal({ open, view, onClose, onSave }) {
  const [draft, setDraft] = _seUseState(null);
  _seUseEffect(() => { if (open && view) setDraft({ label: view.label || '', icon: view.icon || '' }); }, [open, view]);
  if (!open || !view || !draft) return null;
  const fieldStyle = { padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit', background: 'var(--surface-2)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-sm)', color: 'var(--fg)', width: '100%', boxSizing: 'border-box' };
  return (
    <window.Modal open={open} onClose={onClose} title="Edytuj widok" width={420}>
      <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}><span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>Nazwa</span>
          <Input value={draft.label} maxLength={40} onChange={(val) => setDraft((d) => ({ ...d, label: val }))} /></label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}><span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>Ikona</span>
          <_IconPicker value={draft.icon} onChange={(name) => setDraft((d) => ({ ...d, icon: name }))} /></label>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button onClick={onClose} style={_scBtnGhost}>Anuluj</button>
          <Button variant="primary" onClick={() => { onSave({ label: draft.label, icon: draft.icon || null }); onClose(); }}>Zapisz</Button>
        </div>
      </div>
    </window.Modal>
  );
}

// The editor body. Loads its own layout via HS.refresh() on mount. Calls
// onDirtyChange(bool) whenever the unsaved-changes state flips so the host
// (the drawer / Terminal tab) can warn before close if it wants to.
function SettingsShortcutsEditor({ onDirtyChange }) {
  const HS = window.HubShortcuts;
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [saved, setSaved] = _seUseState({ views: [] });
  const [draft, setDraft] = _seUseState({ views: [] });
  const [loaded, setLoaded] = _seUseState(false);
  const [busy, setBusy] = _seUseState(false);
  const [expanded, setExpanded] = _seUseState(null);
  const [editing, setEditing] = _seUseState(null);       // {viewId, button|null}
  const [editingView, setEditingView] = _seUseState(null);

  _seUseEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        if (!HS) throw new Error('HubShortcuts unavailable');
        const cfg = await HS.refresh();  // authoritative read + broadcast (re-syncs a live toolbar)
        if (cancelled) return;
        const inner = (cfg.layout && cfg.layout.layout) ? cfg.layout.layout : { views: [] };
        setSaved(inner); setDraft(inner);
      } catch (e) { console.error('load shortcut layout failed:', e); }
      finally { if (!cancelled) setLoaded(true); }
    })();
    return () => { cancelled = true; };
  }, []);

  const dirty = _stableStringify(saved) !== _stableStringify(draft);
  // Surface dirtiness to the host whenever it flips.
  _seUseEffect(() => { if (typeof onDirtyChange === 'function') onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  const views = Array.isArray(draft.views) ? draft.views : [];
  const setViews = (next) => setDraft({ views: next });
  const patchView = (vid, patch) => setViews(views.map((v) => (v.id === vid ? { ...v, ...patch } : v)));
  const moveView = (idx, dir) => setViews(_moveIdx(views, idx, dir));
  const addView = () => setViews(views.concat([{ id: _uid(), label: 'Nowy widok', icon: 'cmd', buttons: [] }]));
  const removeView = (vid) => setViews(views.filter((v) => v.id !== vid));
  const _editButtons = (vid, fn) => setViews(views.map((v) => (v.id === vid ? { ...v, buttons: fn(Array.isArray(v.buttons) ? v.buttons : []) } : v)));
  const moveButton = (vid, idx, dir) => _editButtons(vid, (bs) => _moveIdx(bs, idx, dir));
  const removeButton = (vid, bid) => _editButtons(vid, (bs) => bs.filter((b) => b.id !== bid));
  // Prune the flag when toggling OFF (don't write `false`): the backend omits
  // false flags, so an explicit `hidden:false`/`pinned:false` in the draft would
  // diverge from the sanitized `saved` and read as permanently dirty.
  const _toggleFlag = (vid, bid, flag) => _editButtons(vid, (bs) => bs.map((b) => {
    if (b.id !== bid) return b;
    const n = { ...b };
    if (n[flag]) delete n[flag]; else n[flag] = true;
    return n;
  }));
  const toggleHideButton = (vid, bid) => _toggleFlag(vid, bid, 'hidden');
  const togglePinButton = (vid, bid) => _toggleFlag(vid, bid, 'pinned');
  const upsertButton = (vid, btn) => _editButtons(vid, (bs) => {
    const i = bs.findIndex((b) => b.id === btn.id);
    if (i === -1) return bs.concat([btn]);
    const next = bs.slice(); next[i] = btn; return next;
  });

  const save = async () => {
    if (!HS) return;
    setBusy(true);
    try {
      const before = _countButtons(draft);
      const cfg = await HS.saveLayout(draft);
      const inner = (cfg.layout && cfg.layout.layout) ? cfg.layout.layout : draft;
      setSaved(inner); setDraft(inner);
      // The server re-sanitizes; surface what actually happened instead of a
      // misleading success toast. Two rejection shapes:
      //  • empty/buttonless layout → backend reseeds the full DEFAULT_LAYOUT
      //    (before === 0 but the server returns the seed) — not what the user saved.
      //  • partial drop → some buttons rejected for a bad value (before > after).
      const after = _countButtons(inner);
      const dropped = before - after;
      if (before === 0 && after > 0) {
        if (toast) toast('Pusty układ odrzucono — przywrócono domyślne', 'err');
      } else if (dropped > 0) {
        if (toast) toast('Zapisano — ale ' + dropped + ' przycisk(ów) odrzucono (błędna wartość)', 'err');
      } else if (toast) { toast('Układ zapisany', 'ok'); }
    } catch (e) {
      console.error('save layout failed:', e);
      if (toast) toast('Zapis nie powiódł się', 'err');
    } finally { setBusy(false); }
  };

  const resetAll = async () => {
    if (!HS) return;
    if (typeof window.confirm === 'function'
      && !window.confirm('Przywrócić domyślny układ? Utracisz wszystkie zmiany (zapisane i niezapisane).')) return;
    setBusy(true);
    try {
      const cfg = await HS.resetLayout();
      const inner = (cfg.layout && cfg.layout.layout) ? cfg.layout.layout : { views: [] };
      setSaved(inner); setDraft(inner);
      if (toast) toast('Przywrócono domyślne', 'ok');
    } catch (e) {
      console.error('reset layout failed:', e);
      if (toast) toast('Reset nie powiódł się', 'err');
    } finally { setBusy(false); }
  };

  // 36px mini-buttons (raised from 30px per the RWD audit — touch minimum).
  const miniBtn = { width: 36, height: 36, flexShrink: 0, borderRadius: 'var(--r-sm)', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', background: 'var(--surface-1)', border: '1px solid var(--hairline)' };
  const chip = { width: 32, height: 28, flexShrink: 0, borderRadius: 'var(--r-sm)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', background: 'var(--surface-3)', border: '1px solid var(--hairline)', fontSize: 'var(--t-cap)', color: 'var(--fg)' };

  return (
    <div style={{
      padding: '14px 16px', background: 'var(--surface-1)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
      opacity: !loaded || busy ? 0.7 : 1, display: 'flex', flexDirection: 'column', gap: 14,
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {views.length === 0 && (
          <EmptyState message="Brak widoków" sub="Dodaj pierwszy poniżej." />
        )}
        {views.map((v, vi) => {
          const isOpen = expanded === v.id;
          const btns = Array.isArray(v.buttons) ? v.buttons : [];
          return (
            <div key={v.id} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={_scRowStyle(v.hidden)}>
                <button onClick={() => setExpanded(isOpen ? null : v.id)} title={isOpen ? 'Zwiń' : 'Rozwiń'} style={miniBtn}>
                  <Icon name={isOpen ? 'chevron-d' : 'chevron-r'} size={15} color="var(--fg-2)" />
                </button>
                <span style={chip}><Icon name={v.icon || 'cmd'} size={16} color="var(--fg-2)" /></span>
                <span style={{ flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v.label || v.id} <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>· {btns.length}</span></span>
                <_seToggle checked={!v.hidden} onChange={() => patchView(v.id, { hidden: !v.hidden })} />
                <KebabMenu items={[
                  { icon: 'arrow-up', label: 'W górę', disabled: vi === 0, onClick: () => moveView(vi, -1) },
                  { icon: 'chevron-d', label: 'W dół', disabled: vi === views.length - 1, onClick: () => moveView(vi, 1) },
                  { icon: 'pencil', label: 'Edytuj widok', onClick: () => setEditingView(v) },
                  { icon: 'trash', label: 'Usuń widok', danger: true, onClick: () => removeView(v.id) },
                ]} />
              </div>
              {isOpen && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5, paddingLeft: 14 }}>
                  {btns.map((b, bi) => (
                    <div key={b.id} style={_scRowStyle(b.hidden)}>
                      <span style={b.bgColor ? { ...chip, background: b.bgColor } : chip}>{b.icon ? <Icon name={b.icon} size={15} color={b.iconColor || 'var(--fg-2)'} /> : (HS.labelForButton(b) || '·')}</span>
                      <span style={{ flex: 1, minWidth: 0, fontSize: 'var(--t-cap)', color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.hint || HS.labelForButton(b) || b.id} <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>· {HS.kindLabel(b.kind)}</span></span>
                      <_seToggle checked={!b.hidden} onChange={() => toggleHideButton(v.id, b.id)} />
                      <KebabMenu items={[
                        { icon: b.pinned ? 'pin-fill' : 'pin', label: b.pinned ? 'Odepnij' : 'Przypnij', onClick: () => togglePinButton(v.id, b.id) },
                        { icon: 'arrow-up', label: 'W górę', disabled: bi === 0, onClick: () => moveButton(v.id, bi, -1) },
                        { icon: 'chevron-d', label: 'W dół', disabled: bi === btns.length - 1, onClick: () => moveButton(v.id, bi, 1) },
                        { icon: 'pencil', label: 'Edytuj', onClick: () => setEditing({ viewId: v.id, button: b }) },
                        { icon: 'trash', label: 'Usuń', danger: true, onClick: () => removeButton(v.id, b.id) },
                      ]} />
                    </div>
                  ))}
                  <button onClick={() => setEditing({ viewId: v.id, button: null })} style={{ ..._scBtnGhost, alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 12px' }}>
                    <Icon name="plus" size={13} color="var(--fg-2)" /> Dodaj przycisk
                  </button>
                </div>
              )}
            </div>
          );
        })}

        <button onClick={addView} disabled={views.length >= 16} style={{ ..._scBtnGhost, alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 6, opacity: views.length >= 16 ? 0.5 : 1 }}>
          <Icon name="plus" size={13} color="var(--fg-2)" /> Dodaj widok
        </button>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginTop: 4 }}>
          <span className="mono" style={{ fontSize: 'var(--t-xs)', color: dirty ? 'var(--accent)' : 'var(--fg-3)' }}>{dirty ? 'Niezapisane zmiany' : 'Zapisane'}</span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={resetAll} disabled={busy} style={_scBtnGhost}>Przywróć domyślne</button>
            <Button variant="primary" disabled={!dirty || busy} busy={busy} onClick={save}>Zapisz</Button>
          </div>
        </div>
      </div>

      <_ButtonEditModal
        open={!!editing}
        value={editing ? editing.button : null}
        onClose={() => setEditing(null)}
        onSave={(btn) => { if (editing) upsertButton(editing.viewId, btn); }}
      />
      <_ViewEditModal
        open={!!editingView}
        view={editingView}
        onClose={() => setEditingView(null)}
        onSave={(patch) => { if (editingView) patchView(editingView.id, patch); }}
      />
    </div>
  );
}

window.SettingsShortcutsEditor = SettingsShortcutsEditor;
