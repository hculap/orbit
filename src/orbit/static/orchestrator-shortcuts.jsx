// orchestrator-shortcuts.jsx — shared helpers + store for the mobile terminal
// soft-keyboard SHORTCUT EDITOR ("Remap + show/hide").
//
// Loaded BEFORE settings-view.jsx and orchestrator-terminal-preview.jsx so both
// can reach `window.HubShortcuts` at render time:
//   • the toolbar (terminal-preview)  → merges overrides over its default keys
//   • the editor  (settings-view)     → reads/writes overrides + the catalog
//
// Pure presentation/merge helpers + a tiny cached fetch/save store talking to
// GET/PUT /api/orchestrator/terminal-shortcuts. The button DEFINITIONS live in
// the toolbar file (which publishes an editable catalog to window); this module
// holds NO button data — only functions that operate on (keys, overrides).
//
// Override shape (sparse diff vs. defaults), keyed by stable button id:
//   { "<id>": { hidden?: true, key?: <KeyboardEvent descriptor> } }
// `hidden` applies to any button; `key` only remaps buttons that send a
// synthetic key (those carrying a `d` descriptor).

(function () {
  const { useState, useEffect } = React;

  const CHANGE_EVENT = 'hub:terminal-shortcuts-change';

  function _api(path) {
    if (typeof window.apiUrl === 'function') return window.apiUrl(path);
    return (window.HUB_BASE_PATH || '') + path;
  }

  // ── label / glyph rendering ──────────────────────────────────────
  // Map a descriptor (or remapped key) to the short glyph shown on the key cap.
  const _CODE_GLYPH = {
    ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→',
    Enter: '⏎', Tab: 'Tab', Escape: 'Esc', Space: '␣',
    Backspace: '⌫', Delete: '⌦',
    Home: '⇤', End: '⇥', PageUp: 'PgUp', PageDown: 'PgDn',
  };

  function _baseGlyph(desc) {
    if (!desc) return '';
    if (desc.code && _CODE_GLYPH[desc.code]) return _CODE_GLYPH[desc.code];
    if (desc.key && _CODE_GLYPH[desc.key]) return _CODE_GLYPH[desc.key];
    if (desc.code && /^Key[A-Z]$/.test(desc.code)) return desc.code.slice(3);
    if (desc.code && /^Digit[0-9]$/.test(desc.code)) return desc.code.slice(5);
    const key = desc.key || '';
    if (key.length === 1) return key.toUpperCase();
    return key || desc.code || '';
  }

  // "^A", "⌥X", "⇧Tab", "↑" … falls back to the default label/glyph when the
  // descriptor yields nothing printable.
  function labelForDescriptor(desc, fallback) {
    if (!desc) return fallback || '';
    let prefix = '';
    if (desc.ctrlKey) prefix += '^';
    if (desc.altKey) prefix += '⌥';
    if (desc.metaKey) prefix += '⌘';
    if (desc.shiftKey) prefix += '⇧';
    const out = prefix + _baseGlyph(desc);
    return out || fallback || '';
  }

  // ── descriptor construction (capture + builder) ──────────────────
  const _MOD_KEYS = new Set(['Control', 'Alt', 'Shift', 'Meta']);

  // Build a descriptor from a real keydown (desktop press-to-capture). Returns
  // null for a bare modifier press so the capture zone waits for a real key.
  function descriptorFromEvent(e) {
    if (!e || _MOD_KEYS.has(e.key)) return null;
    const d = { key: e.key, code: e.code, keyCode: e.keyCode, which: e.keyCode };
    if (e.ctrlKey) d.ctrlKey = true;
    if (e.altKey) d.altKey = true;
    if (e.shiftKey) d.shiftKey = true;
    if (e.metaKey) d.metaKey = true;
    return d;
  }

  // Compose a base key (from the palette) with a modifier map — the touch-
  // friendly builder path (no physical keyboard on a phone). Only true
  // modifiers are kept so labels + equality stay clean.
  function descriptorFromParts(base, mods) {
    if (!base) return null;
    const d = { ...base };
    delete d.label;
    for (const m of ['ctrlKey', 'altKey', 'shiftKey', 'metaKey']) {
      if (mods && mods[m]) d[m] = true; else delete d[m];
    }
    return d;
  }

  // Common keys for the builder <select>. Letters + digits generated; named
  // keys appended. `keyCode` mirrors the legacy values xterm.js reads.
  function _buildPalette() {
    const out = [];
    for (let i = 0; i < 26; i++) {
      const ch = String.fromCharCode(65 + i); // 'A'..'Z'
      out.push({ label: ch, key: ch.toLowerCase(), code: 'Key' + ch, keyCode: 65 + i, which: 65 + i });
    }
    for (let i = 0; i < 10; i++) {
      out.push({ label: String(i), key: String(i), code: 'Digit' + i, keyCode: 48 + i, which: 48 + i });
    }
    const named = [
      { label: '↑ Up', key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
      { label: '↓ Down', key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
      { label: '← Left', key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
      { label: '→ Right', key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 },
      { label: '⏎ Enter', key: 'Enter', code: 'Enter', keyCode: 13 },
      { label: 'Tab', key: 'Tab', code: 'Tab', keyCode: 9 },
      { label: 'Esc', key: 'Escape', code: 'Escape', keyCode: 27 },
      { label: '␣ Space', key: ' ', code: 'Space', keyCode: 32 },
      { label: '⌫ Backspace', key: 'Backspace', code: 'Backspace', keyCode: 8 },
      { label: '⌦ Delete', key: 'Delete', code: 'Delete', keyCode: 46 },
    ];
    for (const n of named) out.push({ ...n, which: n.keyCode });
    return out;
  }
  const KEY_PALETTE = _buildPalette();

  // Match a descriptor back to its palette base (so the builder pre-selects the
  // right option when editing an existing remap).
  function paletteMatch(desc) {
    if (!desc) return null;
    // Prefer an exact `code` match. Only fall back to keyCode when the
    // descriptor carries no code, so a captured key whose code is absent from
    // the palette (e.g. NumpadEnter, keyCode 13) doesn't mis-resolve to an
    // unrelated same-keyCode entry and then get rewritten on a modifier toggle.
    if (desc.code) return KEY_PALETTE.find((p) => p.code === desc.code) || null;
    return KEY_PALETTE.find((p) => desc.keyCode && p.keyCode === desc.keyCode) || null;
  }

  // ── send-raw escape decoding (parity with the backend _decode_raw) ─
  // Turns the escaped on-disk text into the real bytes sent to the PTY:
  // \\n \\t \\r \\\\ and \\xHH (incl \\x00 tmux prefix, \\x1b ESC). Server already
  // dropped malformed escapes at save time, but stay defensive — NEVER throw
  // inside a click handler; on a bad escape emit the backslash literally.
  const _SIMPLE = { n: '\n', t: '\t', r: '\r', '0': '\x00', '\\': '\\' };
  function decodeEscapes(s) {
    if (typeof s !== 'string' || s.indexOf('\\') === -1) return s || '';
    let out = '';
    for (let i = 0; i < s.length; i++) {
      if (s[i] !== '\\') { out += s[i]; continue; }
      const nxt = s[i + 1];
      if (nxt === 'x') {
        const hex = s.slice(i + 2, i + 4);
        if (/^[0-9a-fA-F]{2}$/.test(hex)) { out += String.fromCharCode(parseInt(hex, 16)); i += 3; continue; }
        out += '\\'; continue;  // malformed → literal backslash
      }
      if (nxt != null && Object.prototype.hasOwnProperty.call(_SIMPLE, nxt)) { out += _SIMPLE[nxt]; i += 1; continue; }
      out += '\\';  // unknown / trailing → literal
    }
    return out;
  }

  // ── kind/label helpers ───────────────────────────────────────────
  const _KIND_LABELS = {
    'send-key': 'Klawisz', 'send-raw': 'Sekwencja raw', 'paste-text': 'Wklej tekst',
    'slash-command': 'Komenda /', 'special': 'Akcja', 'modifier': 'Modyfikator',
  };
  function kindLabel(kind) { return _KIND_LABELS[kind] || kind || ''; }

  // The glyph/text shown on a button cap: explicit label wins; else derive a
  // meaningful cap from the kind/payload — NEVER fall back to the raw uid (a
  // label-less, icon-less button must still read sensibly).
  function labelForButton(btn) {
    if (!btn) return '';
    if (btn.label) return btn.label;
    const p = btn.payload || {};
    if (btn.kind === 'send-key') return labelForDescriptor(p, btn.hint || '·');
    if (btn.kind === 'slash-command') return '/' + (p.command || '');
    if (btn.kind === 'paste-text') return (p.text || '').trim().slice(0, 8) || btn.hint || '⤶';
    if (btn.kind === 'send-raw') return btn.hint || 'raw';
    if (btn.kind === 'modifier') return p.modifier ? p.modifier.replace(/Key$/, '') : 'mod';
    return btn.hint || kindLabel(btn.kind) || '·';
  }

  // ── layout render-merge (used by the toolbar) ────────────────────
  // Immutable. Returns the visible views (for the cycler) + the active view's
  // visible buttons, pinned-first (stable within each group).
  function _visibleViews(layout) {
    const vs = (layout && layout.layout && Array.isArray(layout.layout.views)) ? layout.layout.views : [];
    return vs.filter((v) => v && v.id && !v.hidden);
  }
  function applyLayout(layout, viewId) {
    const raw = _visibleViews(layout);
    if (!raw.length) return { views: [], buttons: [], activeId: null };
    const active = raw.find((v) => v.id === viewId) || raw[0];
    const visible = (Array.isArray(active.buttons) ? active.buttons : []).filter((b) => b && b.id && !b.hidden);
    const buttons = visible.filter((b) => b.pinned).concat(visible.filter((b) => !b.pinned));
    return {
      views: raw.map((v) => ({ id: v.id, label: v.label || v.id, icon: v.icon || null })),
      buttons,
      activeId: active.id,
    };
  }

  // ── cached fetch / save store ────────────────────────────────────
  let _cache = null;        // { enabled, layout }
  let _inflight = null;

  function _normalize(data) {
    const layout = (data && data.layout && data.layout.layout && Array.isArray(data.layout.layout.views))
      ? data.layout : null;
    return { enabled: !!(data && data.enabled), layout };
  }

  // One GET. Returns a normalized config on success, or null on failure — so a
  // transient blip (cold-start, network) is NEVER cached as a poisoned
  // "disabled" state that would silence the toolbar with no retry.
  async function _readServer() {
    try {
      const r = await fetch(_api('/api/orchestrator/terminal-shortcuts'));
      if (r.ok) return _normalize(await r.json());
    } catch (_e) { /* offline / warming → caller falls back + retries next time */ }
    return null;
  }

  async function fetchConfig(force) {
    if (_cache && !force) return _cache;
    // `force` bypasses the in-flight dedup too, so a forced re-read (refresh
    // after a flag toggle) can't be silently downgraded to an older pending
    // non-forced fetch carrying pre-change state.
    if (_inflight && !force) return _inflight;
    const p = (async () => {
      const next = await _readServer();
      if (next) _cache = next;             // cache ONLY a real success
      if (_inflight === p) _inflight = null; // don't clobber a newer fetch's slot
      return next || _cache || { enabled: false, layout: null };
    })();
    _inflight = p;
    return p;
  }

  // Full-state replace of the layout. Throws on HTTP failure so the caller can
  // toast + revert.
  async function saveLayout(layout) {
    const r = await fetch(_api('/api/orchestrator/terminal-shortcuts'), {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ layout: layout || {} }),
    });
    if (!r.ok) throw new Error('PUT terminal-shortcuts → ' + r.status);
    _cache = _normalize(await r.json());
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: _cache }));
    return _cache;
  }

  // Reseed the canonical default layout server-side, then broadcast.
  async function resetLayout() {
    const r = await fetch(_api('/api/orchestrator/terminal-shortcuts/reset'), { method: 'POST' });
    if (!r.ok) throw new Error('POST terminal-shortcuts/reset → ' + r.status);
    _cache = _normalize(await r.json());
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: _cache }));
    return _cache;
  }

  // Authoritative fresh read (bypasses the in-flight dedup via _readServer) +
  // broadcast — used after the enable flag is toggled via the /settings PATCH
  // (which doesn't touch this store) and on the editor mount, so a live toolbar
  // re-syncs even against an out-of-band change from another device.
  async function refresh() {
    const next = await _readServer();
    const cfg = next || _cache || { enabled: false, layout: null };
    if (next) _cache = next;
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: cfg }));
    return cfg;
  }

  // React hook: live {enabled, layout}; re-renders on save/refresh.
  function useConfig() {
    const [cfg, setCfg] = useState(_cache || { enabled: false, layout: null });
    useEffect(() => {
      let alive = true;
      fetchConfig(false).then((c) => { if (alive) setCfg(c); });
      const onChange = (e) => { if (e && e.detail) setCfg(e.detail); };
      window.addEventListener(CHANGE_EVENT, onChange);
      return () => { alive = false; window.removeEventListener(CHANGE_EVENT, onChange); };
    }, []);
    return cfg;
  }

  window.HubShortcuts = {
    CHANGE_EVENT,
    KEY_PALETTE,
    labelForDescriptor,
    descriptorFromEvent,
    descriptorFromParts,
    paletteMatch,
    decodeEscapes,
    kindLabel,
    labelForButton,
    applyLayout,
    fetchConfig,
    saveLayout,
    resetLayout,
    refresh,
    useConfig,
  };
})();
