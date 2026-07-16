// orchestrator-terminal-preview.jsx — interactive terminal view + legacy
// SSE pane preview, plus a modal wrapper kept for backwards compatibility.
//
// `TerminalLiveView` is the primary export: a flex-1 React component that
// pre-warms the tmux slot via POST /api/orchestrator/sessions/<sid>/term/ensure
// then renders ttyd's iframe inline. Designed to fill its parent container —
// no fixed pixel heights — so it can serve both as the main chat-area view
// (via the chat ↔ terminal toggle) and as the body of a modal.
//
// Both modes share a server-side flag check on mount: when ttyd_enabled is
// false (default), the iframe path is unreachable and we fall back to the
// legacy <pre>+SSE view. That fallback is the documented rollback guarantee
// for the ttyd feature — keep it in this file indefinitely.

const { useState: _tpUseState, useEffect: _tpUseEffect, useRef: _tpUseRef, useCallback: _tpUseCallback } = React;

// Mobile-only xterm.js font size override. ttyd is spawned with
// fontSize=13 in its --client-option argv (desktop-friendly), but
// 13px @ ~7px char width on a 360-380px-wide phone gives ~50 cols
// which truncates long claude TUI lines. 11px on mobile yields ~60
// cols with the same iframe width — visibly denser without becoming
// unreadable. Injected via term.options.fontSize after iframe load.
const _MOBILE_XTERM_FONT_SIZE = 11;

// ── mobile soft-keyboard helpers ──────────────────────────────────
//
// On mobile (iOS Safari especially) the on-screen keyboard often
// won't surface when the user taps inside the ttyd iframe, and even
// when it does, common terminal keys (Esc, Tab, Ctrl, arrows) aren't
// on the keyboard. We render a small row of buttons above the iframe
// that dispatch synthetic KeyboardEvents to ttyd's xterm.js helper
// textarea. The "⌨" button is an explicit focus trigger that does
// nothing more than ta.focus() — for the iOS case where a user
// gesture is required to surface the keyboard.

function _findXtermHelperTextarea(iframeRef) {
  const iframe = iframeRef.current;
  if (!iframe || !iframe.contentWindow) return null;
  try {
    return iframe.contentWindow.document.querySelector('.xterm-helper-textarea');
  } catch (_e) {
    return null;
  }
}

function _sendKeyToIframe(iframeRef, descriptor) {
  const iframe = iframeRef.current;
  if (!iframe || !iframe.contentWindow) return;
  const win = iframe.contentWindow;
  const ta = _findXtermHelperTextarea(iframeRef);
  if (!ta) return;
  // Focus first so xterm.js routes the event to its key handler. The
  // burst of all three event types matches what a real key press
  // generates — xterm.js binds on keydown, but we send keyup too so
  // any held-state handlers don't get confused.
  ta.focus();
  const KE = win.KeyboardEvent || KeyboardEvent;
  for (const type of ['keydown', 'keypress', 'keyup']) {
    try {
      ta.dispatchEvent(new KE(type, { bubbles: true, cancelable: true, ...descriptor }));
    } catch (_e) { /* swallow */ }
  }
}

// Inject arbitrary text into the live tmux session. Prefers xterm's
// public `term.paste()` (handles bracketed-paste so multi-line content
// arrives as one paste, not N keystrokes), falls back to the raw
// coreService data path. Keeps focus in the terminal afterwards so the
// user can keep typing on the real keyboard.
// Signal that the most recent terminal input was voice-dictated (mic → paste)
// so orchestrator.jsx's on-voice read-aloud auto-speaks only dictated turns
// (a terminal turn never goes through the composer's from_voice latch).
function _markTerminalVoiceInput(sessionId) {
  try {
    window.dispatchEvent(new CustomEvent('hub:terminal-voice-input', { detail: { sessionId: sessionId || null } }));
  } catch (_e) { /* ignore */ }
}

// NOTE: terminal dictation (mic button / Cmd+M) pastes the RAW transcript with
// NO instruction prefix — the user wanted plain speech-to-text here. The
// spoken-style "voice protocol" prompt lives ONLY in the continuous-conversation
// mode (orchestrator-conversation.jsx), which builds it via window.HubVoicePrompts
// and sends it through the conv-send bridge below (pasted verbatim).

function _pasteToTerminal(iframeRef, text, opts) {
  // Returns true only when the text actually reached an xterm — the
  // conversation bridge relies on this to keep its in-flight counter honest
  // (a silent no-op on a not-ready terminal would leak a phantom turn that
  // gates every later utterance forever).
  const iframe = iframeRef.current;
  const win = iframe && iframe.contentWindow;
  if (!win || !text) return false;
  const term = win.term;
  try {
    if (term && typeof term.paste === 'function') {
      term.paste(text);
    } else {
      const cs = term && term._core && term._core.coreService;
      if (cs && typeof cs.triggerDataEvent === 'function') cs.triggerDataEvent(text, true);
      else return false;  // no xterm surface to deliver into
    }
    // Optional auto-submit (used by the mic): send a bare CR as a KEYSTROKE
    // AFTER a short delay. Sending it synchronously right after the bracketed
    // paste landed it in the SAME PTY read as the paste-end marker, and
    // claude's input then dropped the whole paste (UAT 2026-05-29: mic text
    // stopped reaching tmux). Deferring lets the paste flush + be ingested
    // first, then the \r cleanly submits. Re-resolve term inside the timeout
    // (the iframe may have re-rendered).
    if (opts && opts.submit) {
      setTimeout(() => {
        try {
          const w = iframeRef.current && iframeRef.current.contentWindow;
          const t2 = w && w.term;
          const cs2 = t2 && t2._core && t2._core.coreService;
          if (cs2 && typeof cs2.triggerDataEvent === 'function') cs2.triggerDataEvent('\r', true);
        } catch (_e) { /* swallow */ }
      }, 80);
    }
  } catch (_e) {
    return false;
  }
  try { if (term && typeof term.focus === 'function') term.focus(); } catch (_e) { /* swallow */ }
  return true;
}

// Write raw bytes straight to the PTY (no bracketed-paste wrapping). Used for
// tmux command chords — the C-Space prefix (NUL byte) followed by a command
// key — which must reach tmux as exact bytes. We deliberately don't go via
// synthetic KeyboardEvents: a plain-letter keydown wouldn't emit the byte
// (xterm defers printable chars to its input event), so the command key after
// the prefix would be lost.
function _sendRawToTerminal(iframeRef, data) {
  const iframe = iframeRef.current;
  const win = iframe && iframe.contentWindow;
  const term = win && win.term;
  if (!term || !data) return;
  try {
    const cs = term._core && term._core.coreService;
    if (cs && typeof cs.triggerDataEvent === 'function') cs.triggerDataEvent(data, true);
  } catch (_e) { /* swallow */ }
  try { if (typeof term.focus === 'function') term.focus(); } catch (_e) { /* swallow */ }
}

// Navigate the orchestrator to another session, keeping TERMINAL view.
// Terminal is the default view, so we just clear any 'chat' override the
// target session might carry in the localStorage map the orchestrator
// reads on activeId-change (key `hub-orchestrator-view-mode`); then
// routerReplace switches section + session (the hub's useRouter re-reads
// the path on router:change).
function _openSessionInTerminal(sid) {
  if (!sid) return;
  try {
    const raw = localStorage.getItem('hub-orchestrator-view-mode');
    const map = raw ? JSON.parse(raw) : {};
    delete map[sid];  // absence = default 'terminal'
    localStorage.setItem('hub-orchestrator-view-mode', JSON.stringify(map));
  } catch (_e) { /* quota / privacy mode — navigation still works */ }
  if (typeof window.routerReplace === 'function' && typeof window.buildPath === 'function') {
    window.routerReplace(window.buildPath({ section: 'orchestrator', sessionId: sid }));
  }
}

// Forward app-level keyboard shortcuts out of the focused ttyd iframe.
// When the terminal has focus, keydowns fire INSIDE the iframe document,
// so the parent window's shortcut handlers (Cmd+K command palette, Cmd+↑/↓
// session switch, Cmd+N new session) never see them. We intercept just
// those combos in the iframe (capture phase, before xterm's own keydown
// handler), swallow them so tmux doesn't get a stray key, and re-dispatch
// a synthetic keydown on the PARENT window so the app handlers fire. Plain
// Cmd/Ctrl shortcuts in the allow-set, PLUS the ⌥+⇥ session-switcher trigger
// (Alt+Tab) — the only Alt combo we forward, so the overlay can open while the
// ttyd terminal has focus. Other Alt combos always fall through to tmux.
const _FWD_SHORTCUT_KEYS = new Set([
  'k', 'K', 'n', 'N', 'u', 'U', 'm', 'M',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
]);
function _forwardAppShortcuts(win) {
  if (!win || win.__hubShortcutFwd) return;
  win.__hubShortcutFwd = true;
  win.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    // ⌥+⇥ (Alt+Tab) session-switcher trigger — forwarded even though Alt
    // normally falls through, so the overlay can open / cycle (⇧⇥ reverses)
    // with the terminal focused.
    const isSwitcher = e.altKey && !mod && e.key === 'Tab';
    if (!isSwitcher) {
      if (!mod || e.altKey) return;
      if (e.shiftKey || !_FWD_SHORTCUT_KEYS.has(e.key)) return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    try {
      const parentWin = win.parent || window.parent;
      if (!parentWin || parentWin === win) return;
      const KE = parentWin.KeyboardEvent || KeyboardEvent;
      parentWin.dispatchEvent(new KE('keydown', {
        key: e.key, code: e.code, keyCode: e.keyCode, which: e.which,
        metaKey: e.metaKey, ctrlKey: e.ctrlKey, altKey: e.altKey, shiftKey: e.shiftKey,
        bubbles: true, cancelable: true,
      }));
    } catch (_e) { /* cross-frame guard — same-origin so shouldn't fire */ }
  }, { capture: true });

  // Mirror the ⌥ (and ⌘/Ctrl) KEYUP to the parent too. Keydown-only forwarding
  // makes the session switcher's "release ⌥ to jump" fast path unreliable when
  // the ttyd terminal has focus (the modifier release fires inside the iframe).
  // A pure mirror (no preventDefault) — the terminal still sees its own modifier
  // release (a no-op for tmux), and the parent acts on it only while the
  // switcher overlay is open; otherwise it's a harmless no-op.
  win.addEventListener('keyup', (e) => {
    if (e.key !== 'Meta' && e.key !== 'Control' && e.key !== 'Alt') return;
    try {
      const parentWin = win.parent || window.parent;
      if (!parentWin || parentWin === win) return;
      const KE = parentWin.KeyboardEvent || KeyboardEvent;
      parentWin.dispatchEvent(new KE('keyup', {
        key: e.key, code: e.code, keyCode: e.keyCode, which: e.which,
        metaKey: e.metaKey, ctrlKey: e.ctrlKey, altKey: e.altKey, shiftKey: e.shiftKey,
        bubbles: true, cancelable: true,
      }));
    } catch (_e) { /* cross-frame guard */ }
  }, { capture: true });
}

// Detect REAL keyboard typing into the live terminal and signal the app. When
// the user types a printable character by hand (not a dictation paste — that
// goes via triggerDataEvent and emits NO keydown), they've taken keyboard
// control, so 'on-voice' read-aloud should stop auto-speaking subsequent turns.
// The voice latch in orchestrator.jsx is intentionally session-sticky (to also
// read async background-subagent results, which arrive with no typing), so
// without this a typed follow-up turn would still be spoken. Ctrl/Cmd/Alt
// combos are shortcuts (handled by _forwardAppShortcuts), not typing — ignored.
// Throttled: one signal per typing burst is enough, and clearing is idempotent.
function _detectKeyboardInput(win, sessionId) {
  if (!win || win.__hubTypingDetect) return;
  win.__hubTypingDetect = true;
  let last = 0;
  win.addEventListener('keydown', (e) => {
    if (!e.isTrusted) return;                              // ONLY real hardware typing — never synthetic send-key/soft-keyboard events (they'd wrongly drop a live voice turn's read-aloud)
    if (e.metaKey || e.ctrlKey || e.altKey) return;        // shortcut, not typing
    if (typeof e.key !== 'string' || e.key.length !== 1) return;  // printable char only
    const now = Date.now();
    if (now - last < 800) return;                          // throttle bursts
    last = now;
    try {
      window.dispatchEvent(new CustomEvent('hub:terminal-keyboard-input', {
        detail: { sessionId: sessionId || null },
      }));
    } catch (_e) { /* ignore */ }
  }, { capture: true });
}

// Intercept Shift+Enter inside the terminal iframe → insert a newline instead
// of submitting. A plain terminal can't distinguish Shift+Enter from Enter (both
// emit CR), so claude would SUBMIT. We send Meta+Enter (ESC + CR) instead —
// claude's "insert newline" (identical to Option+Enter). Capture-phase + stop
// propagation so xterm never also emits the bare CR.
function _interceptShiftEnter(win) {
  if (!win || win.__hubShiftEnter) return;
  win.__hubShiftEnter = true;
  win.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    try {
      const term = win.term || win.t || win.terminal;
      const cs = term && term._core && term._core.coreService;
      if (cs && typeof cs.triggerDataEvent === 'function') cs.triggerDataEvent('\x1b\r', true);
    } catch (_e) { /* swallow */ }
  }, { capture: true });
}

// Hold ⌥Option(/Alt) → momentary "select text" mode. onDown enters (tmux mouse
// off → native drag selection), onUp leaves. `blur` covers the case where focus
// leaves the iframe while Alt is held and the keyup never arrives, so we don't
// get stuck with mouse off. (The Option key is also Meta for the keyboard via
// macOptionIsMeta — entering select mode during a keyboard Meta combo is a
// harmless transient toggle; the dedup in _setSelect keeps it cheap.)
function _interceptAltSelect(win, onDown, onUp) {
  if (!win || win.__hubAltSel) return;
  win.__hubAltSel = true;
  win.addEventListener('keydown', (e) => {
    if (e.key === 'Alt' && !e.repeat) { try { onDown(); } catch (_e) {} }
  }, { capture: true });
  win.addEventListener('keyup', (e) => {
    if (e.key === 'Alt') { try { onUp(); } catch (_e) {} }
  }, { capture: true });
  win.addEventListener('blur', () => { try { onUp(); } catch (_e) {} });
}

// Copy-on-select for the terminal. tmux runs with mouse-mode ON, so a plain
// drag goes to tmux (no browser selection). With macOptionClickForcesSelection
// (Mac: ⌥+drag) / Shift+drag (other), xterm makes a LOCAL selection instead —
// we mirror that selection into the clipboard so the user can grab a URL/token
// without fighting tmux.
//
// Copy on mouseUP, NOT the async onSelectionChange+debounce the first cut used:
// clipboard.writeText needs a live user gesture + a focused document, and a
// deferred timer has neither (it "worked once" then silently failed — and
// claude's TUI redraws could clear the xterm selection before the timer ran).
// mouseup is a guaranteed gesture with the iframe focused and the selection
// already built. We read win.term FRESH each time so a ttyd reconnect that
// recreates the Terminal keeps working; the listener lives on the persistent
// document, bound once.
function _bindCopyOnSelect(win, onResult) {
  if (!win || win.__hubCopySel) return !!(win && win.__hubCopySel);
  const doc = win.document;
  if (!doc) return false;
  win.__hubCopySel = true;
  doc.addEventListener('mouseup', () => {
    let sel = '';
    try {
      const t = win.term || win.t || win.terminal;
      if (t && typeof t.getSelection === 'function') sel = t.getSelection();
    } catch (_e) { sel = ''; }
    if (!sel || !sel.trim()) return;
    try {
      // Reach the parent's shared util (same-origin iframe; the win.parent
      // pattern is already used in this file) so the iOS execCommand fallback
      // applies inside the terminal too.
      const HC = (win.parent && win.parent.HubClipboard)
        || (window.parent && window.parent.HubClipboard)
        || window.HubClipboard;
      if (HC && typeof HC.copyText === 'function') {
        HC.copyText(sel).then(
          (ok) => { if (typeof onResult === 'function') onResult(ok); },
          () => { if (typeof onResult === 'function') onResult(false); },
        );
      }
    } catch (_e) { if (typeof onResult === 'function') onResult(false); }
  });
  return true;
}

// Intercept Ctrl+V inside the terminal iframe. Default behavior sends
// Ctrl+V to claude (which reads the system clipboard and inserts an
// image inline). Instead: if the clipboard holds image/file blobs, we
// upload them to the session and paste the absolute path (same as
// Cmd+U, but the file comes from the clipboard). If it's plain text, we
// paste the text normally so Ctrl+V still works. Runs IN the iframe
// because the async Clipboard API needs the real keydown's transient
// activation — the iframe carries `allow="clipboard-read"` for this.
function _interceptCtrlV(win, onFiles) {
  if (!win || win.__hubCtrlV) return;
  win.__hubCtrlV = true;
  win.addEventListener('keydown', async (e) => {
    if (!e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (e.key !== 'v' && e.key !== 'V') return;
    // Swallow it — claude must NOT also paste (would double-insert).
    e.preventDefault();
    e.stopImmediatePropagation();
    const pasteText = (txt) => {
      if (!txt) return;
      try {
        const term = win.term;
        if (term && typeof term.paste === 'function') term.paste(txt);
        else if (term && term._core && term._core.coreService) term._core.coreService.triggerDataEvent(txt, true);
      } catch (_e) { /* swallow */ }
    };
    try {
      const items = await win.navigator.clipboard.read();
      const files = [];
      let text = null;
      for (const it of items) {
        for (const type of it.types) {
          if (type === 'text/html') continue;  // prefer text/plain / files
          if (type === 'text/plain') {
            if (text === null) { try { text = await (await it.getType(type)).text(); } catch (_e) {} }
            continue;
          }
          // image/* or any other binary type → treat as a file to upload.
          try {
            const blob = await it.getType(type);
            const ext = ((type.split('/')[1] || 'bin').split('+')[0]) || 'bin';
            files.push(new win.File([blob], `wklejka.${ext}`, { type }));
          } catch (_e) { /* skip this type */ }
        }
      }
      if (files.length) {
        if (typeof onFiles === 'function') onFiles(files);
      } else {
        pasteText(text);
      }
    } catch (_e) {
      // Clipboard read denied / unsupported — fall back to text paste so
      // Ctrl+V isn't dead.
      try { pasteText(await win.navigator.clipboard.readText()); } catch (__e) { /* swallow */ }
    }
  }, { capture: true });
}

// Intercept the `paste` event (Cmd+V on Mac, Ctrl+V on Win/Linux) when it
// carries FILES. Copying a file in Finder puts only a reference on the
// clipboard — its bytes are NOT in navigator.clipboard.read() (that just
// yields the filename as text), but the paste event's clipboardData.files
// DOES expose the real File with content. So a Finder-copied image/file
// uploads via Cmd+V here (the Ctrl+V keydown path still handles raw image
// data like screenshots). Plain-text pastes fall through to xterm.
function _interceptPasteFiles(win, onFiles) {
  if (!win || win.__hubPasteFiles) return;
  win.__hubPasteFiles = true;
  win.addEventListener('paste', (e) => {
    const dt = e.clipboardData;
    if (!dt) return;
    const files = [];
    if (dt.files && dt.files.length) {
      for (const f of dt.files) files.push(f);
    } else if (dt.items) {
      for (const it of dt.items) {
        if (it.kind === 'file') { const f = it.getAsFile(); if (f) files.push(f); }
      }
    }
    if (files.length) {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (typeof onFiles === 'function') onFiles(files);
    }
    // else: plain text — let xterm paste it normally.
  }, { capture: true });
}

// Drag a file from the OS onto the terminal → upload + paste path. The
// drop lands INSIDE the iframe, so we attach here. dragover must
// preventDefault to enable the drop (and to stop the browser navigating
// to the dropped file); drop reads dataTransfer.files (real bytes) and
// hands them to the uploader.
function _interceptDropFiles(win, onFiles, onDragState) {
  if (!win || win.__hubDropFiles) return;
  win.__hubDropFiles = true;
  const isFileDrag = (e) => {
    const t = e.dataTransfer && e.dataTransfer.types;
    return t && Array.prototype.indexOf.call(t, 'Files') !== -1;
  };
  const setDrag = (v) => { try { if (typeof onDragState === 'function') onDragState(v); } catch (_e) { /* swallow */ } };
  // dragover fires continuously while a file hovers; debounce-clear so the
  // overlay stays up during the hover and drops ~140ms after the drag
  // leaves (dragleave is unreliable across child elements).
  let clearTimer = null;
  win.addEventListener('dragover', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'copy'; } catch (_e) { /* swallow */ }
    setDrag(true);
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = setTimeout(() => { clearTimer = null; setDrag(false); }, 140);
  }, { capture: true });
  win.addEventListener('drop', (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (clearTimer) { clearTimeout(clearTimer); clearTimer = null; }
    setDrag(false);
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (files.length && typeof onFiles === 'function') onFiles(files);
  }, { capture: true });
}

// ── soft-key views ────────────────────────────────────────────────
// The first (accent) button cycles between these. Keeps the row short
// on a phone while exposing navigation keys, terminal control keys, and
// prompt-authoring actions without all of them fighting for width.

// Nav + special keys carry a `label` (text/glyph) + a key descriptor `d`
// (or `mod` for sticky modifiers). Actions carry an `icon` (app line-icon
// name, for a coherent monochrome look matching the mic) + an `action` or
// `paste` payload.

// tmux prefix on the box is C-Space (the NUL byte) — `set -g prefix C-Space`
// in ~/.tmux.conf. A chord key sends NUL + the command letter as raw PTY data
// (see _sendRawToTerminal). Command bindings (lowercase, from the live config):
// v → split-window -v, h → split-window -h, x → kill-pane, z → resize-pane -Z
// (zoom is a single TOGGLE, so one Z button covers in + out).
const _TMUX_PREFIX = '\x00';

// Tab is no longer listed here (nor in _SPECIAL_KEYS): it's now pinned
// next to the red-square Esc on the Akcje + Nawigacja views (see the
// soft-key row), so a per-view copy would just be a duplicate.
// Every array-driven key carries a STABLE `id`: the shortcut editor keys its
// sparse override map (hidden / remapped key) on these, and the React render
// uses them too — so adding/relabelling a button never breaks an override.
const _NAV_KEYS = [
  { id: 'arrow-up',    label: '↑', hint: 'W górę',  d: { key: 'ArrowUp',    code: 'ArrowUp',    keyCode: 38, which: 38 } },
  { id: 'arrow-down',  label: '↓', hint: 'W dół',   d: { key: 'ArrowDown',  code: 'ArrowDown',  keyCode: 40, which: 40 } },
  { id: 'arrow-left',  label: '←', hint: 'W lewo',  d: { key: 'ArrowLeft',  code: 'ArrowLeft',  keyCode: 37, which: 37 } },
  { id: 'arrow-right', label: '→', hint: 'W prawo', d: { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39, which: 39 } },
  { id: 'enter',       label: '⏎', hint: 'Enter',   d: { key: 'Enter',      code: 'Enter',      keyCode: 13, which: 13 } },
  // Line navigation — readline-style start/end of line (Ctrl+A / Ctrl+E), sent
  // the same way as the other Ctrl combos (synthetic ctrlKey event → xterm
  // emits \x01 / \x05). The box's tmux prefix is C-Space, so C-a is free and
  // passes straight through to the shell / claude input line.
  { id: 'line-start',  label: '⇤', hint: 'Początek linii (Ctrl+A)', d: { key: 'a', code: 'KeyA', keyCode: 65, which: 65, ctrlKey: true } },
  { id: 'line-end',    label: '⇥', hint: 'Koniec linii (Ctrl+E)',   d: { key: 'e', code: 'KeyE', keyCode: 69, which: 69, ctrlKey: true } },
  // tmux pane management (prefix → command, sent as raw bytes).
  { id: 'tmux-split-v', label: 'V', hint: 'tmux: split pionowy (prefix → v)',                raw: _TMUX_PREFIX + 'v' },
  { id: 'tmux-split-h', label: 'H', hint: 'tmux: split poziomy (prefix → h)',                raw: _TMUX_PREFIX + 'h' },
  { id: 'tmux-kill',    label: '✕', hint: 'tmux: zamknij pane (prefix → x)',                 raw: _TMUX_PREFIX + 'x' },
  { id: 'tmux-zoom',    label: 'Z', hint: 'tmux: zoom pane on/off (prefix → z, przełącznik)', raw: _TMUX_PREFIX + 'z' },
];

const _SPECIAL_KEYS = [
  { id: 'shift-tab',  label: 'ST',   hint: 'Shift+Tab',            d: { key: 'Tab',    code: 'Tab',    keyCode: 9,  which: 9, shiftKey: true } },
  { id: 'ctrl-s',     label: '^S',   hint: 'Ctrl+S',               d: { key: 's', code: 'KeyS',  keyCode: 83, which: 83, ctrlKey: true } },
  { id: 'ctrl-b',     label: '^B',   hint: 'Ctrl+B (tmux prefix)', d: { key: 'b', code: 'KeyB',  keyCode: 66, which: 66, ctrlKey: true } },
  { id: 'ctrl-c',     label: '^C',   hint: 'Ctrl+C',               d: { key: 'c', code: 'KeyC',  keyCode: 67, which: 67, ctrlKey: true } },
  { id: 'ctrl-space', label: '^␣',   hint: 'Ctrl+Space',           d: { key: ' ', code: 'Space', keyCode: 32, which: 32, ctrlKey: true } },
  { id: 'mod-ctrl',   label: 'Ctrl', hint: 'Ctrl — trzyma do następnego klawisza', mod: 'ctrlKey' },
  { id: 'mod-alt',    label: 'Alt',  hint: 'Alt — trzyma do następnego klawisza',  mod: 'altKey' },
  { id: 'mod-meta',   label: '⌘',    hint: 'Cmd / Meta — trzyma do następnego klawisza', mod: 'metaKey' },
  { id: 'ctrl-u',     label: 'Clr',  hint: 'Wyczyść input (Ctrl+U)', d: { key: 'u', code: 'KeyU', keyCode: 85, which: 85, ctrlKey: true } },
];

const _ACTION_KEYS = [
  { id: 'upload',  icon: 'attach',      hint: 'Dodaj plik / obraz',           action: 'upload' },
  { id: 'voice',   hint: 'Dyktuj (głos → tekst)',                             action: 'voice' },
  { id: 'clip',    icon: 'copy',        hint: 'Wklej ze schowka',             action: 'clipboard' },
  { id: 'ultra',   icon: 'bulb',        hint: 'Wklej „ultrathink”',           paste: 'ultrathink ' },
  { id: 'multi',   icon: 'bot',         hint: 'Wklej „multi agents”',         paste: 'multi agents ' },
  { id: 'plan',    icon: 'list-checks', hint: 'Wklej „plan mode”',            paste: 'plan mode ' },
  { id: 'web',     icon: 'globe',       hint: 'Wklej „web search”',           paste: 'web search ' },
  { id: 'ask',     icon: 'help',        hint: 'Wklej „ask user question”',    paste: 'ask user question ' },
  { id: 'compact', icon: 'archive',     hint: 'Compact (/compact)',           action: 'compact' },
];

// Order = frequency: commands (most-used icon actions) first, then the
// keyboard navigation keys, then the live session switcher (dynamic — no
// static keys, rendered specially), and finally the terminal control /
// special keys (least frequent).
const _SOFT_VIEWS = [
  { id: 'actions',  icon: 'sparkle',  label: 'Akcje',     keys: _ACTION_KEYS },
  { id: 'nav',      icon: 'terminal', label: 'Nawigacja', keys: _NAV_KEYS },
  { id: 'sessions', icon: 'inbox',    label: 'Sesje',     keys: [] },
  { id: 'special',  icon: 'cmd',      label: 'Specjalne', keys: _SPECIAL_KEYS },
];

// Fixed button footprint so every soft key is the same width — keeps the
// row visually uniform across all three views.
const _SOFT_BTN_W = 46;
const _SOFT_BTN_H = 38;

// ── shortcut customization (opt-in `terminal_shortcuts_enabled`) ──────
//
// HubShortcuts is published by orchestrator-shortcuts.jsx, which loads BEFORE
// this file — so this module-eval capture is reliably the object (or null if
// the shared module was removed during a rollback). Captured once → the
// `_useShortcutConfig` branch is stable for the process lifetime, so it never
// trips the rules-of-hooks even though it's behind a guard.
const _HubShortcuts = (typeof window !== 'undefined' && window.HubShortcuts) ? window.HubShortcuts : null;

function _useShortcutConfig() {
  if (_HubShortcuts && _HubShortcuts.useConfig) return _HubShortcuts.useConfig();
  return { enabled: false, overrides: {} };
}

// Editable catalog for the Settings shortcut editor (settings-view.jsx reads
// `window.TERMINAL_SHORTCUTS_CATALOG` at render time — it loads earlier, but
// this module has evaluated by mount). Only the array-driven buttons are
// editable: the pinned cycler / Esc / Tab and the dynamic session switcher are
// structural and intentionally excluded. Built from the same source arrays the
// toolbar renders, so labels/ids never drift.
function _shortcutKind(k) {
  if (k.mod) return 'mod';
  if (k.raw) return 'raw';
  if (k.action === 'voice') return 'voice';
  if (k.action) return 'action';
  if (k.paste) return 'paste';
  if (k.d) return 'key';
  return 'other';
}

function _buildShortcutsCatalog() {
  const groups = [
    { group: 'nav',     label: 'Nawigacja', keys: _NAV_KEYS },
    { group: 'special', label: 'Specjalne', keys: _SPECIAL_KEYS },
    { group: 'actions', label: 'Akcje',     keys: _ACTION_KEYS },
  ];
  return groups.map((g) => ({
    group: g.group,
    label: g.label,
    buttons: g.keys.filter((k) => k.id).map((k) => {
      const kind = _shortcutKind(k);
      return {
        id: k.id,
        group: g.group,
        label: k.label || '',
        hint: k.hint || k.label || k.id,
        icon: k.icon || null,
        kind,
        remappable: kind === 'key',          // only synthetic-key buttons remap
        defaultKey: k.d ? { ...k.d } : null,  // for the editor's "reset to default"
      };
    }),
  }));
}
window.TERMINAL_SHORTCUTS_CATALOG = _buildShortcutsCatalog();

// Map a legacy _NAV_KEYS/_SPECIAL_KEYS/_ACTION_KEYS entry to the unified button
// shape {id,kind,label,hint,icon,payload}, so the flag-off (static) render path
// uses the SAME renderer/dispatcher as the layout path — no divergence. Note:
// legacy `raw` is already-decoded bytes (e.g. '\x00v'); decodeEscapes is a
// no-op on a string with no backslash escapes, so it round-trips correctly.
function _legacyToButton(k) {
  if (k.mod) return { id: k.id, kind: 'modifier', label: k.label, hint: k.hint, payload: { modifier: k.mod } };
  if (k.raw) return { id: k.id, kind: 'send-raw', label: k.label, hint: k.hint, payload: { data: k.raw } };
  if (k.action === 'voice') return { id: k.id, kind: 'special', label: k.label, hint: k.hint, payload: { actionType: 'microphone' } };
  if (k.action === 'upload') return { id: k.id, kind: 'special', icon: k.icon, hint: k.hint, payload: { actionType: 'upload' } };
  if (k.action === 'clipboard') return { id: k.id, kind: 'special', icon: k.icon, hint: k.hint, payload: { actionType: 'clipboard-paste' } };
  if (k.action === 'compact') return { id: k.id, kind: 'slash-command', icon: k.icon, hint: k.hint, payload: { command: 'compact', submit: true } };
  if (k.paste) return { id: k.id, kind: 'paste-text', icon: k.icon, hint: k.hint, payload: { text: k.paste, submit: false } };
  return { id: k.id, kind: 'send-key', label: k.label, hint: k.hint, payload: k.d || {} };
}

function _MobileSoftKeyboard({ iframeRef, sessionId, onPickFiles }) {
  const [viewId, setViewId] = _tpUseState('actions');
  // Sticky modifiers: tap Ctrl/Alt to arm, it applies to the NEXT key
  // press then clears (one-shot). Armed state is shown underlined +
  // filled so it's obvious a modifier is pending.
  const [mods, setMods] = _tpUseState({ ctrlKey: false, altKey: false, metaKey: false });
  // Active interactive sessions for the "Sesje" switcher view. Fetched
  // (and polled) only while that view is open.
  const [poolSlots, setPoolSlots] = _tpUseState([]);
  // Live layout config { enabled, layout }. When the opt-in flag is off (or no
  // layout / HubShortcuts missing) the toolbar renders the raw default arrays.
  const shortcutCfg = _useShortcutConfig();
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  // DUAL PATH (rollback level 1): flag off (or layout/HubShortcuts missing) →
  // render today's static `_SOFT_VIEWS` arrays, mapped to the unified button
  // shape so ONE renderer covers both paths (functionally identical to today).
  // Flag on → render the user's persisted layout (views + typed buttons).
  const layoutMode = !!(shortcutCfg.enabled && _HubShortcuts && shortcutCfg.layout);
  const merged = layoutMode ? _HubShortcuts.applyLayout(shortcutCfg.layout, viewId) : null;
  const views = layoutMode
    ? merged.views
    : _SOFT_VIEWS.map((v) => ({ id: v.id, label: v.label, icon: v.icon }));
  const activeId = layoutMode ? merged.activeId : ((_SOFT_VIEWS.find((v) => v.id === viewId) || _SOFT_VIEWS[0]).id);
  const curView = views.find((v) => v.id === activeId) || views[0] || { id: '', label: '', icon: 'cmd' };
  const buttons = layoutMode
    ? merged.buttons
    : ((_SOFT_VIEWS.find((v) => v.id === activeId) || _SOFT_VIEWS[0]).keys || []).map(_legacyToButton);
  // Session switcher poll runs whenever the active view exposes one — in layout
  // mode that's a kind=special session-switcher button (it can live in any
  // view / be pinned); in static mode it's the dedicated 'sessions' view.
  const hasSessionSwitcher = layoutMode
    ? buttons.some((b) => b && b.kind === 'special' && b.payload && b.payload.actionType === 'session-switcher')
    : (activeId === 'sessions');

  // Stable manual refetch of the session pool — reused by the auto-poll
  // effect AND the manual "Odśwież sesje" button in renderSessionSwitcher.
  const refreshPool = _tpUseCallback(async () => {
    try {
      const res = await fetch(apiUrl('/api/orchestrator/pool'));
      if (!res.ok) return;
      const data = await res.json();
      setPoolSlots((data && data.slots) || []);
    } catch (_e) { /* swallow — switcher just stays as-is */ }
  }, []);

  _tpUseEffect(() => {
    if (!hasSessionSwitcher) return undefined;
    refreshPool();
    const id = setInterval(refreshPool, 4000);
    return () => { clearInterval(id); };
  }, [hasSessionSwitcher, refreshPool]);

  const cycleView = () => {
    const ids = views.map((v) => v.id);
    if (!ids.length) return;
    // Advance from the view actually being shown (activeId), not the raw
    // viewId state — which may be a stale default (e.g. 'actions') absent from
    // a custom view set, in which case applyLayout already fell back to ids[0].
    const cur = ids.indexOf(activeId);
    setViewId(ids[(cur + 1) % ids.length]);
  };

  // Single dispatch by button kind — shared by both render paths.
  const dispatchButton = (btn) => {
    const p = btn.payload || {};
    if (btn.kind === 'modifier') {
      setMods((m) => ({ ...m, [p.modifier]: !m[p.modifier] }));
      return;
    }
    if (btn.kind === 'send-raw') {
      _sendRawToTerminal(iframeRef, _HubShortcuts ? _HubShortcuts.decodeEscapes(p.data) : p.data);
      return;
    }
    if (btn.kind === 'send-key') {
      const desc = { ...p };
      if (mods.ctrlKey) desc.ctrlKey = true;
      if (mods.altKey) desc.altKey = true;
      if (mods.metaKey) desc.metaKey = true;
      _sendKeyToIframe(iframeRef, desc);
      if (mods.ctrlKey || mods.altKey || mods.metaKey) setMods({ ctrlKey: false, altKey: false, metaKey: false });
      return;
    }
    if (btn.kind === 'paste-text') {
      _pasteToTerminal(iframeRef, p.text || '', { submit: !!p.submit });
      return;
    }
    if (btn.kind === 'slash-command') {
      // Type the slash command, then (if submit) a real Enter. The 80ms delay
      // lets the slash-command UI register before the submit so it isn't
      // swallowed by claude's autocomplete (preserves the old /compact timing).
      _pasteToTerminal(iframeRef, '/' + (p.command || ''));
      if (p.submit) setTimeout(() => _sendKeyToIframe(iframeRef, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }), 80);
      return;
    }
    if (btn.kind === 'special') {
      if (p.actionType === 'upload') { if (typeof onPickFiles === 'function') onPickFiles(); return; }
      if (p.actionType === 'clipboard-paste') {
        navigator.clipboard.readText()
          .then((text) => { if (text) _pasteToTerminal(iframeRef, text); else if (toast) toast('Schowek jest pusty', 'warn'); })
          .catch(() => { if (toast) toast('Brak dostępu do schowka', 'err'); });
      }
      // microphone + session-switcher render their own widgets (see renderButton).
    }
  };

  // Stop the buttons from stealing focus from xterm's hidden textarea.
  // Without this, tapping any soft key (especially the view cycler, which
  // does no ta.focus() of its own) blurs the textarea → the mobile
  // keyboard dismisses → and since this whole row is gated on
  // keyboardOpen, it unmounts and the buttons vanish. preventDefault on
  // pointer/mouse-down cancels the focus default while STILL firing the
  // click, so the terminal keeps focus and the keyboard stays up.
  const noBlur = {
    onPointerDown: (e) => e.preventDefault(),
    onMouseDown: (e) => e.preventDefault(),
  };

  const baseBtn = {
    width: _SOFT_BTN_W, height: _SOFT_BTN_H, padding: 0,
    borderRadius: 'var(--r-sm)', cursor: 'pointer', flexShrink: 0, lineHeight: 1,
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  };
  // Key-cap style (nav + special views): bordered, monospace label.
  const keyStyle = (active) => ({
    ...baseBtn,
    fontSize: 'var(--t-sm)', fontFamily: 'JetBrains Mono, monospace',
    background: active ? 'var(--accent)' : 'var(--surface-2)',
    color: active ? 'var(--accent-fg)' : 'var(--fg)',
    border: '1px solid ' + (active ? 'var(--accent-line)' : 'var(--hairline)'),
    textDecoration: active ? 'underline' : 'none',
  });
  // Action / icon style: SAME bordered key-cap box as the text keys, just
  // with an Icon child instead of a label — so every button in the row
  // (keys, action icons, mic, stop) reads as the same component.
  const iconStyle = {
    ...baseBtn,
    background: 'var(--surface-2)', color: 'var(--fg)',
    border: '1px solid var(--hairline)',
  };
  const cyclerStyle = {
    ...baseBtn,
    background: 'var(--accent)', color: 'var(--accent-fg)',
    border: '1px solid var(--accent-line)',
  };

  // The live "Sesje" switcher: one button per active tmux session (current
  // highlighted), or a placeholder. Returned as an array so it can be inlined
  // wherever a session-switcher button sits.
  const renderSessionSwitcher = (key) => {
    // Manual refresh — first element, so it shows even when the list is empty.
    const refreshBtn = (
      <button
        key={key + '-refresh'}
        {...noBlur}
        title="Odśwież listę sesji"
        onClick={() => { refreshPool(); if (toast) toast('Odświeżono sesje', 'ok'); }}
        style={{ ...iconStyle, width: 'auto', minWidth: 40, padding: '0 8px' }}
      >
        <Icon name="refresh" size={16} color="var(--fg-2)" />
      </button>
    );
    if (poolSlots.length === 0) {
      return [
        refreshBtn,
        <span key={key + '-empty'} style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', padding: '0 8px', alignSelf: 'center', whiteSpace: 'nowrap' }}>brak aktywnych sesji</span>,
      ];
    }
    return [refreshBtn, ...poolSlots.map((s, idx) => {
      const isCur = s.session_id === sessionId;
      const label = s.agent || 'Global';
      return (
        <button
          key={key + '-' + s.session_id}
          {...noBlur}
          title={s.title || label}
          onClick={() => { if (!isCur) _openSessionInTerminal(s.session_id); }}
          style={{
            ...baseBtn, width: 'auto', minWidth: 46, padding: '0 10px', gap: 5, fontSize: 'var(--t-sm)',
            background: isCur ? 'var(--accent)' : 'var(--surface-2)',
            color: isCur ? 'var(--accent-fg)' : 'var(--fg)',
            border: '1px solid ' + (isCur ? 'var(--accent-line)' : 'var(--hairline)'),
          }}
        >{label}<span style={{ opacity: 0.55, fontSize: 'var(--t-xs)' }}>#{idx + 1}</span></button>
      );
    })];
  };

  // ONE renderer for every button kind (drives both the layout + static paths).
  const renderButton = (btn) => {
    const p = btn.payload || {};
    if (btn.kind === 'special' && p.actionType === 'session-switcher') {
      return renderSessionSwitcher(btn.id);
    }
    if (btn.kind === 'special' && p.actionType === 'microphone') {
      return window.MicButton ? (
        <span key={btn.id} {...noBlur} style={{ ...iconStyle, cursor: 'default' }}>
          <window.MicButton
            language="auto"
            onFinalTranscript={(t) => { const txt = (t || '').trim(); if (txt) { _markTerminalVoiceInput(sessionId); _pasteToTerminal(iframeRef, txt, { submit: true }); } }}
          />
        </span>
      ) : null;
    }
    // Optional per-button color overrides (validated to hex / var(--token) by
    // the backend). bgColor → button background; iconColor → icon/text color.
    if (btn.icon) {
      const st = btn.bgColor ? { ...iconStyle, background: btn.bgColor } : iconStyle;
      return (
        <button key={btn.id} {...noBlur} title={btn.hint || btn.label} onClick={() => dispatchButton(btn)} style={st}>
          <Icon name={btn.icon} size={18} color={btn.iconColor || 'var(--fg-2)'} />
        </button>
      );
    }
    const active = btn.kind === 'modifier' && !!mods[p.modifier];
    const label = _HubShortcuts ? _HubShortcuts.labelForButton(btn) : (btn.label || btn.hint || btn.id);
    let st = keyStyle(active);
    if (btn.bgColor || btn.iconColor) {
      st = { ...st, ...(btn.bgColor ? { background: btn.bgColor } : {}), ...(btn.iconColor ? { color: btn.iconColor } : {}) };
    }
    return (
      <button key={btn.id} {...noBlur} title={btn.hint || label} onClick={() => dispatchButton(btn)} style={st}>{label}</button>
    );
  };

  return (
    <div style={{
      display: 'flex', gap: 4, alignItems: 'center',
      // Extra breathing room below the keys: when the soft keyboard is
      // up this row sits directly on top of it, and a flush 4px looked
      // cramped. 10px bottom gives a clear gap from the keyboard edge.
      padding: '4px 2px 10px',
      overflowX: 'auto', flexShrink: 0, whiteSpace: 'nowrap',
      WebkitOverflowScrolling: 'touch',
    }}>
      {/* View cycler — the ONLY structural anchor (always rendered, never in
          the layout): it's the sole way to switch views, so it can't be
          deleted/bricked. Esc + Tab are now layout buttons (seeded pinned) so
          they're managed in the panel — but in flag-OFF mode the layout isn't
          used, so they're still rendered structurally there. */}
      <button
        {...noBlur}
        onClick={cycleView}
        title={'Widok: ' + (curView.label || '') + ' — kliknij, by zmienić'}
        style={cyclerStyle}
      ><Icon name={curView.icon || 'cmd'} size={18} color="var(--accent-fg)" /></button>

      {!layoutMode && (
        <button
          {...noBlur}
          title="Stop — Esc (zatrzymaj agenta)"
          onClick={() => _sendKeyToIframe(iframeRef, { key: 'Escape', code: 'Escape', keyCode: 27, which: 27 })}
          style={iconStyle}
        ><Icon name="square" size={18} color="var(--err)" /></button>
      )}

      {!layoutMode && (activeId === 'actions' || activeId === 'nav') && (
        <button
          {...noBlur}
          title="Tab — uzupełnianie (autocomplete)"
          onClick={() => _sendKeyToIframe(iframeRef, { key: 'Tab', code: 'Tab', keyCode: 9, which: 9 })}
          style={keyStyle(false)}
        >Tab</button>
      )}

      {/* Static-mode 'sessions' view keeps its dedicated switcher (in layout
          mode the switcher is a session-switcher button handled by renderButton). */}
      {!layoutMode && activeId === 'sessions' && renderSessionSwitcher('static-sessions')}

      {buttons.map((b) => renderButton(b))}
    </div>
  );
}

// Desktop-only fullscreen toggle, floated over the terminal's top-right
// corner. Self-contained hover state so it sits dimmed/unobtrusive until
// pointed at (an iframe swallows parent hover, so a persistent affordance
// beats a hover-reveal here). The actual requestFullscreen/exitFullscreen
// runs in the parent, which owns the container ref + fullscreenchange
// listener and drives `isFs`.
function _TermFsButton({ isFs, onToggle }) {
  const [hover, setHover] = _tpUseState(false);
  return (
    <button
      onClick={onToggle}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={isFs ? 'Wyjdź z pełnego ekranu (Esc)' : 'Pełny ekran terminala'}
      aria-label={isFs ? 'Wyjdź z pełnego ekranu' : 'Pełny ekran terminala'}
      style={{
        position: 'absolute', top: isFs ? 12 : 16, right: isFs ? 12 : 22,
        zIndex: 25, width: 30, height: 30, padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-sm)', cursor: 'pointer',
        background: 'oklch(0.20 0.012 264 / 0.74)',
        border: '1px solid var(--hairline)',
        color: hover ? 'var(--fg)' : 'var(--fg-2)',
        opacity: hover ? 1 : 0.62,
        backdropFilter: 'blur(2px)',
        transition: 'opacity .15s, color .15s',
      }}
    >
      <Icon name={isFs ? 'minimize' : 'maximize'} size={16} color="currentColor" />
    </button>
  );
}

// Desktop-only "select text" toggle, floated next to the fullscreen button.
// Active = tmux mouse is OFF, so a drag does a native xterm selection that
// copy-on-select grabs (lets the user drag-copy a URL/token without tmux's
// mouse-mode intercepting it).
function _TermSelectButton({ active, isFs, compact, onToggle }) {
  const [hover, setHover] = _tpUseState(false);
  // Desktop sits to the LEFT of the fullscreen button; mobile (no fullscreen
  // button) takes the top-right corner itself.
  const right = compact ? 14 : (isFs ? 50 : 60);
  const top = compact ? 10 : (isFs ? 12 : 16);
  return (
    <button
      onClick={onToggle}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={active
        ? 'Tryb zaznaczania włączony — przeciągnij po tekście i puść = kopia. Kliknij, by wrócić do normalnej myszy.'
        : 'Zaznacz i skopiuj tekst (wyłącza mysz tmux; na desktopie wystarczy trzymać ⌥Option)'}
      aria-label="Tryb zaznaczania tekstu"
      style={{
        position: 'absolute', top, right,
        zIndex: 25, width: 30, height: 30, padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-sm)', cursor: 'pointer',
        background: active ? 'var(--accent)' : 'oklch(0.20 0.012 264 / 0.74)',
        border: '1px solid ' + (active ? 'var(--accent-line)' : 'var(--hairline)'),
        color: active ? 'var(--accent-fg)' : (hover ? 'var(--fg)' : 'var(--fg-2)'),
        opacity: active ? 1 : (hover ? 1 : 0.62),
        backdropFilter: 'blur(2px)',
        transition: 'opacity .15s, color .15s, background .15s',
      }}
    >
      <Icon name="copy" size={15} color="currentColor" />
    </button>
  );
}

// Mobile-only "reload terminal render" button. Takes the top-right corner that
// the select-to-copy button occupies on desktop (on phones, copying is covered
// by the last-message modal). Reconnects the ttyd client in place so tmux
// repaints the pane from scratch — "reload just this session" without reloading
// the whole page. Same position/size as the select button's mobile placement.
function _TermReloadButton({ onReload }) {
  const [hover, setHover] = _tpUseState(false);
  return (
    <button
      onMouseDown={(e) => { try { e.preventDefault(); } catch (_e) { /* keep terminal focus */ } }}
      onClick={onReload}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title="Odśwież render sesji — przeładuj terminal (pełne odrysowanie tmux)"
      aria-label="Odśwież render terminala"
      style={{
        position: 'absolute', top: 10, right: 14,
        zIndex: 25, width: 30, height: 30, padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-sm)', cursor: 'pointer',
        background: 'oklch(0.20 0.012 264 / 0.74)',
        border: '1px solid var(--hairline)',
        color: hover ? 'var(--fg)' : 'var(--fg-2)',
        opacity: hover ? 1 : 0.62,
        backdropFilter: 'blur(2px)',
        transition: 'opacity .15s, color .15s, background .15s',
      }}
    >
      <Icon name="refresh" size={15} color="currentColor" />
    </button>
  );
}

// Manual "read the last assistant message aloud" button — only rendered when
// the read_aloud_tmux_enabled server flag is on (the parent gates it). Floats
// to the LEFT of the select-text button. Click = speak; click while speaking =
// stop. Reads the chat-mirror message (clean markdown via the shared TTS
// pipeline), NOT the raw xterm buffer. See orchestrator.jsx onSpeakLastInTerminal.
function _TermSpeakButton({ tts, speaking, isFs, compact, onSpeakLast }) {
  const [hover, setHover] = _tpUseState(false);
  // Stacks one slot LEFT of select-text (select: compact?14 / isFs?50 / 60).
  const right = compact ? 50 : (isFs ? 86 : 96);
  const top = compact ? 10 : (isFs ? 12 : 16);
  const handleClick = () => {
    if (speaking) {
      if (tts && typeof tts.cancel === 'function') {
        try { tts.cancel(); } catch (_e) { /* best-effort stop */ }
      }
    } else if (typeof onSpeakLast === 'function') {
      onSpeakLast();
    }
  };
  return (
    <button
      onClick={handleClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={speaking ? 'Zatrzymaj czytanie' : 'Przeczytaj ostatnią wiadomość na głos'}
      aria-label={speaking ? 'Zatrzymaj czytanie' : 'Przeczytaj ostatnią wiadomość na głos'}
      style={{
        position: 'absolute', top, right,
        zIndex: 25, width: 30, height: 30, padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-sm)', cursor: 'pointer',
        background: speaking ? 'var(--accent)' : 'oklch(0.20 0.012 264 / 0.74)',
        border: '1px solid ' + (speaking ? 'var(--accent-line)' : 'var(--hairline)'),
        color: speaking ? 'var(--accent-fg)' : (hover ? 'var(--fg)' : 'var(--fg-2)'),
        opacity: speaking ? 1 : (hover ? 1 : 0.62),
        backdropFilter: 'blur(2px)',
        transition: 'opacity .15s, color .15s, background .15s',
      }}
    >
      <Icon name={speaking ? 'square' : 'volume'} size={15} color="currentColor" />
    </button>
  );
}

// Dictate button — floats in the top-right cluster, one slot LEFT of the
// speaker. Toggles the SAME voice capture as the soft-key/Cmd+M mic, so the
// user can dictate without opening the keyboard, on desktop AND mobile. Red +
// mic-fill while recording. onFinal (wired on the _voice hook) prefixes the
// transcript for voice mode + submits.
function _TermMicButton({ recording, isFs, compact, onToggle }) {
  const [hover, setHover] = _tpUseState(false);
  // One slot left of the speaker (speaker: compact?50 / isFs?86 / 96).
  const right = compact ? 86 : (isFs ? 122 : 132);
  const top = compact ? 10 : (isFs ? 12 : 16);
  return (
    <button
      onClick={onToggle}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={recording ? 'Zatrzymaj nagrywanie' : 'Dyktuj głosem (bez klawiatury)'}
      aria-label={recording ? 'Zatrzymaj nagrywanie' : 'Dyktuj głosem'}
      style={{
        position: 'absolute', top, right,
        zIndex: 25, width: 30, height: 30, padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-sm)', cursor: 'pointer',
        background: recording ? 'var(--err)' : 'oklch(0.20 0.012 264 / 0.74)',
        border: '1px solid ' + (recording ? 'var(--err)' : 'var(--hairline)'),
        color: recording ? '#fff' : (hover ? 'var(--fg)' : 'var(--fg-2)'),
        opacity: recording ? 1 : (hover ? 1 : 0.62),
        backdropFilter: 'blur(2px)',
        transition: 'opacity .15s, color .15s, background .15s',
      }}
    >
      <Icon name={recording ? 'mic-fill' : 'mic'} size={16} color="currentColor" />
    </button>
  );
}

// "Pokaż ostatnią wiadomość" — mobile-only floating button. The terminal/
// transcript copy badly on phones (#88, related #83), so this opens a modal
// with the last assistant turn as natively-selectable, copyable text. Pure
// display, no feature flag (mirrors the unflagged select-text button). Floats
// one slot LEFT of the mic button. See orchestrator.jsx onShowLastInTerminal.
function _TermLastMsgButton({ isFs, compact, onShowLast }) {
  const [hover, setHover] = _tpUseState(false);
  // One slot left of the mic (mic: compact?86 / isFs?122 / 132).
  const right = compact ? 122 : (isFs ? 158 : 168);
  const top = compact ? 10 : (isFs ? 12 : 16);
  return (
    <button
      onClick={() => { if (typeof onShowLast === 'function') onShowLast(); }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title="Pokaż ostatnią wiadomość"
      aria-label="Pokaż ostatnią wiadomość"
      style={{
        position: 'absolute', top, right,
        zIndex: 25, width: 30, height: 30, padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-sm)', cursor: 'pointer',
        background: 'oklch(0.20 0.012 264 / 0.74)',
        border: '1px solid var(--hairline)',
        color: hover ? 'var(--fg)' : 'var(--fg-2)',
        opacity: hover ? 1 : 0.62,
        backdropFilter: 'blur(2px)',
        transition: 'opacity .15s, color .15s, background .15s',
      }}
    >
      <Icon name="message" size={15} color="currentColor" />
    </button>
  );
}

// Floating "scroll to bottom" button for the terminal.
//
// Scrolling the tmux pane puts it in COPY-MODE (the `[N/M]` indicator) — tmux
// redraws the pane in place, so the browser xterm's own scrollbar never moves
// and a DOM-scroll listener sees nothing. We therefore ask the BACKEND whether
// the pane is in copy-mode (`GET …/copy-mode` → `tmux #{pane_in_mode}`) and show
// the FAB while it is; a click POSTs `…/copy-mode` which runs `send-keys -X
// cancel` to exit copy-mode and snap back to the live tail. We poll on an
// interval (paused when the tab is hidden) and also re-check immediately on a
// wheel-up so the button appears promptly.
function _TermScrollFab({ iframeRef, phase, iframeLoaded, compact, keyboardOpen, sessionId }) {
  const [visible, setVisible] = _tpUseState(false);

  _tpUseEffect(() => {
    if (phase !== 'ready' || !sessionId) { setVisible(false); return undefined; }
    let cancelled = false;
    let timer = 0;
    let wheelTimer = 0;
    let inFlight = false;
    const modeUrl = apiUrl(
      '/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/copy-mode'
    );

    const checkOnce = async () => {
      if (cancelled || inFlight) return;
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      inFlight = true;
      try {
        const r = await fetch(modeUrl);
        if (!cancelled && r.ok) {
          const d = await r.json().catch(() => ({}));
          setVisible(!!(d && d.in_mode));
        }
      } catch (_e) { /* offline / transient — keep last state */ }
      finally { inFlight = false; }
    };

    const loop = () => { checkOnce(); timer = setTimeout(loop, 2000); };
    loop();

    // Snappier appearance: a wheel-up over the terminal triggers a re-check
    // (debounced so tmux has entered copy-mode before we ask).
    let doc = null;
    const onWheel = (e) => {
      if (!e || e.deltaY >= 0 || wheelTimer) return;
      wheelTimer = setTimeout(() => { wheelTimer = 0; checkOnce(); }, 160);
    };
    try {
      const win = iframeRef.current && iframeRef.current.contentWindow;
      doc = win && win.document;
      if (doc) doc.addEventListener('wheel', onWheel, { passive: true, capture: true });
    } catch (_e) { /* cross-frame DOM not ready yet */ }

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (wheelTimer) clearTimeout(wheelTimer);
      try { if (doc) doc.removeEventListener('wheel', onWheel, { capture: true }); } catch (_e) {}
    };
  }, [phase, sessionId, iframeLoaded, iframeRef]);

  if (phase !== 'ready') return null;

  const onClick = async () => {
    setVisible(false);
    try {
      await fetch(
        apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/copy-mode'),
        { method: 'POST' },
      );
    } catch (_e) { /* swallow — the view-cycler still works */ }
    // Refocus the pane so the user can keep typing immediately.
    try {
      const win = iframeRef.current && iframeRef.current.contentWindow;
      const t = win && (win.term || win.t || win.terminal);
      if (t && typeof t.focus === 'function') t.focus();
    } catch (_e) { /* swallow */ }
  };

  // Lower-right of the iframe. On mobile lift the FAB above the soft-keyboard
  // toolbar (in normal flow at the column bottom) when it's open; the desktop
  // fullscreen button lives top-right, so there's no collision down here.
  const bottom = compact
    ? (keyboardOpen
        ? 'calc(env(safe-area-inset-bottom, 0px) + 64px)'
        : 'calc(env(safe-area-inset-bottom, 0px) + 16px)')
    : '18px';

  return (
    <button
      onClick={onClick}
      aria-label="Przewiń terminal na dół"
      title="Przewiń terminal na dół"
      style={{
        position: 'absolute',
        bottom,
        right: compact ? 16 : 18,
        width: 40, height: 40, borderRadius: 'var(--r-xl)',
        background: 'var(--surface-1)',
        color: 'var(--accent)',
        border: '1px solid var(--accent-line)',
        boxShadow: '0 6px 18px rgba(0,0,0,0.32)',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        cursor: 'pointer', padding: 0, zIndex: 25,
        opacity: visible ? 1 : 0,
        transform: visible ? 'translateY(0)' : 'translateY(8px)',
        pointerEvents: visible ? 'auto' : 'none',
        transition: 'opacity .18s ease, transform .18s ease',
      }}>
      <Icon name="chevron-d" size={20} stroke={2} />
    </button>
  );
}

// ── interactive ttyd-backed live view (NO modal wrapping) ─────────
//
// Two-phase load: POST /term/ensure to pre-warm the tmux slot, THEN
// render the iframe. Without the ensure step a chat whose slot has
// evicted (idle TTL) or never spawned would see ttyd's cryptic
// "can't find session" message because tmux exits immediately on
// attach to a non-existent session.
//
// Uses flex: 1 + minHeight: 0 so the iframe fills whatever the parent
// gives it. Caller wraps in a sized container (modal sets explicit
// 540 px; inline chat-view toggle gives it the full remaining height).

function TerminalLiveView({ sessionId, compact, keyboardOpen, onStatusChange, tts, onSpeakLast, onShowLast, featureFlagEnabled, isSpeaking, conversationActive }) {
  // phase: 'ensuring' (POST /ensure pending) | 'ready' (iframe mounted)
  //        | 'ensure_err' (acquire failed)   | 'iframe_err' (iframe onError)
  const [phase, setPhase] = _tpUseState('ensuring');
  const [errMsg, setErrMsg] = _tpUseState(null);
  const [spawned, setSpawned] = _tpUseState(false);   // true = was cold spawn
  const [elapsedMs, setElapsedMs] = _tpUseState(0);
  const [iframeLoaded, setIframeLoaded] = _tpUseState(false);
  const [uploading, setUploading] = _tpUseState(0);  // count of files in-flight (0 = idle)
  const [dragging, setDragging] = _tpUseState(false); // file dragged over the terminal
  const iframeRef = _tpUseRef(null);
  // Desktop fullscreen: we fullscreen the WHOLE TerminalLiveView root
  // (iframe + overlays) via the native Fullscreen API. The ResizeObserver
  // already watching the iframe (see the refit effect) refits xterm to the
  // new size automatically, so no manual resize plumbing is needed.
  const rootRef = _tpUseRef(null);
  const [isFs, setIsFs] = _tpUseState(false);
  // "Select text" mode (desktop): turns tmux mouse OFF so a plain drag does a
  // native xterm selection (copy-on-select then grabs exactly that) instead of
  // the drag being captured by tmux's mouse-mode/copy-mode (which copies a
  // `[N/M]`-style fragment via OSC52). selectModeRef lets the unmount cleanup
  // restore mouse-on without reading stale state.
  const [selectMode, setSelectMode] = _tpUseState(false);
  // selectModeRef is the source of truth for the dedup + cleanup; _setSelect and
  // the session-reset effect keep it in lockstep with selectMode (no render-time
  // sync, so the dedup in _setSelect stays authoritative).
  const selectModeRef = _tpUseRef(false);
  // File input lives HERE, not inside the keyboard-gated soft-key row:
  // opening the native file dialog blurs the page → the mobile keyboard
  // closes → keyboardOpen flips false → the soft-key row unmounts. If the
  // <input> lived there it would unmount mid-pick and onChange would never
  // fire (the upload silently did nothing). Mounted at this level it
  // survives, so the selected file is uploaded + its path pasted.
  const fileInputRef = _tpUseRef(null);
  const _tlToast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  // Upload a list of File objects to the session and paste their absolute
  // paths into tmux. Shared by the file picker (Cmd+U / 📎) and the Ctrl+V
  // clipboard-paste interceptor.
  const uploadFiles = async (files) => {
    const list = Array.from(files || []);
    if (!list.length || !sessionId) return;
    // Show the "wysyłanie…" pill for the whole POST — the upload + paste
    // can take a few seconds (large files / cold network) and otherwise
    // nothing visibly happens between picking the file and the path
    // landing in tmux.
    setUploading(list.length);
    try {
      const fd = new FormData();
      list.forEach((f) => fd.append('files', f));
      // stage_only=1 → save + return path WITHOUT queueing the file into
      // the chat runner's pending-attachments list.
      const url = apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/uploads?stage_only=1');
      const res = await fetch(url, { method: 'POST', body: fd });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const paths = ((data && data.saved) || []).map((s) => s.server_path).filter(Boolean);
      if (paths.length) {
        // Paste the absolute paths so claude (running in the tmux session)
        // can Read them — works for both files and images.
        _pasteToTerminal(iframeRef, paths.join(' ') + ' ');
        if (_tlToast) _tlToast(paths.length === 1 ? 'Plik dołączony' : (paths.length + ' plików dołączonych'), 'ok');
      }
    } catch (_e) {
      if (_tlToast) _tlToast('Upload nie powiódł się', 'err');
    } finally {
      setUploading(0);
    }
  };
  // Stable ref so the iframe's Ctrl+V handler (attached once on load) always
  // calls the current uploader without re-attaching.
  const uploadFilesRef = _tpUseRef(uploadFiles);
  uploadFilesRef.current = uploadFiles;

  const onFilesChosen = (e) => {
    const files = Array.from((e.target && e.target.files) || []);
    if (e.target) e.target.value = '';
    uploadFiles(files);
  };

  // Voice → tmux: Cmd+M toggles recording; the final transcript is pasted
  // into the live session. Mirrors the soft-key mic, but works on desktop
  // terminal view (which has no soft-key row). Conditional-call mirrors
  // MicButton — window.useVoiceCapture is stable within a session.
  const _voice = (typeof window !== 'undefined' && window.useVoiceCapture)
    ? window.useVoiceCapture({
        onFinal: (t) => { const txt = (t || '').trim(); if (txt) { _markTerminalVoiceInput(sessionId); _pasteToTerminal(iframeRef, txt, { submit: true }); } },
        onPartial: () => {},
        onError: (m) => { if (_tlToast) _tlToast(m || 'Nagrywanie nie powiodło się', 'err'); },
        // chunkMs:0 → final-only: NO mid-recording requestData(). On Chrome/webm
        // each requestData() segment carries its own header, and concatenating
        // them is an invalid container Whisper reads as empty → Groq 400 "audio
        // too short", so on desktop nothing pasted (onFinal got ''). We don't
        // show live partials here (onPartial is a no-op), so the partial POSTs
        // were pure waste anyway. Mirrors the conversation-mode fix.
        language: 'auto', silenceMs: 1500, chunkMs: 0, maxRecordingMs: 5 * 60 * 1000,
      })
    : { state: 'idle', start: () => {}, stop: () => {} };
  const _voiceRef = _tpUseRef(_voice);
  _voiceRef.current = _voice;
  const _recording = _voice.state === 'recording';

  // Conversation modal owns the ONLY live mic while it's open — a second
  // useVoiceCapture (this view's _voice) recording in parallel would mean two
  // MediaRecorder stacks and duplicate sends. The modal overlay already
  // covers the visible mic buttons; this ref covers the keyboard path too.
  const _convActiveRef = _tpUseRef(!!conversationActive);
  _convActiveRef.current = !!conversationActive;

  // Cmd+U (upload) / Cmd+M (mic) shortcuts dispatched from OrchestratorView
  // (and forwarded out of the iframe). Mounted only in terminal view, so
  // the chat composer's listener handles them in chat view.
  _tpUseEffect(() => {
    const onUpload = () => { if (fileInputRef.current) fileInputRef.current.click(); };
    const onMic = () => {
      if (_convActiveRef.current) return;  // modal mic owns capture
      const v = _voiceRef.current;
      if (!v) return;
      if (v.state === 'recording') v.stop(); else v.start();
    };
    window.addEventListener('hub:shortcut-upload', onUpload);
    window.addEventListener('hub:shortcut-mic', onMic);
    return () => {
      window.removeEventListener('hub:shortcut-upload', onUpload);
      window.removeEventListener('hub:shortcut-mic', onMic);
    };
  }, []);

  // Conversation-mode send bridge: the modal (orchestrator.jsx) dispatches
  // text already carrying its own voice-protocol prompt (built via
  // window.HubVoicePrompts) — paste VERBATIM + submit. This is the ONLY voice
  // path that carries the spoken-style prompt; plain mic/Cmd+M dictation above
  // is raw. Server-side /term/paste isn't used here because the iframe xterm is
  // live whenever the modal is usable.
  _tpUseEffect(() => {
    const onConvSend = (e) => {
      const d = e && e.detail;
      const text = d && d.text;
      if (!text) return;
      // dispatchEvent runs listeners synchronously, so mutating detail.ok is
      // a same-tick result channel: the conversation's sender throws on !ok
      // (terminal not ready) instead of leaking a phantom in-flight turn.
      const ok = _pasteToTerminal(iframeRef, text, { submit: true });
      if (d && typeof d === 'object') d.ok = !!ok;
    };
    window.addEventListener('hub:conversation-terminal-send', onConvSend);
    return () => window.removeEventListener('hub:conversation-terminal-send', onConvSend);
  }, []);

  // Keep `isFs` in sync with the live fullscreen element so the icon +
  // full-bleed layout track OS-driven exits (Esc, or another element
  // grabbing fullscreen) too — not just our own button. Webkit-prefixed
  // names cover Safari, which still ships the unprefixed API only partly.
  _tpUseEffect(() => {
    const onFsChange = () => {
      const cur = document.fullscreenElement || document.webkitFullscreenElement || null;
      setIsFs(!!cur && cur === rootRef.current);
    };
    document.addEventListener('fullscreenchange', onFsChange);
    document.addEventListener('webkitfullscreenchange', onFsChange);
    return () => {
      document.removeEventListener('fullscreenchange', onFsChange);
      document.removeEventListener('webkitfullscreenchange', onFsChange);
    };
  }, []);

  const toggleFullscreen = () => {
    const el = rootRef.current;
    if (!el) return;
    const cur = document.fullscreenElement || document.webkitFullscreenElement || null;
    // Only treat this as "exit" when WE own fullscreen. If some other
    // element holds it, fall through and request fullscreen for our root
    // (the browser swaps it over) — "fullscreen my terminal" is the intent.
    if (cur === el) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) { try { exit.call(document); } catch (_e) { /* swallow */ } }
      return;
    }
    const req = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!req) { if (_tlToast) _tlToast('Pełny ekran nieobsługiwany w tej przeglądarce', 'warn'); return; }
    try {
      const r = req.call(el);
      if (r && typeof r.catch === 'function') r.catch(() => { if (_tlToast) _tlToast('Pełny ekran niedostępny', 'err'); });
    } catch (_e) {
      if (_tlToast) _tlToast('Pełny ekran niedostępny', 'err');
    }
  };

  // Enter/leave "select text" mode for the CURRENT session: POST tmux mouse off
  // (so a drag is a native xterm selection that copy-on-select grabs) / on. Used
  // by the toolbar button AND the Option-hold below. selectModeRef is set eagerly
  // so the unmount cleanup restores mouse-on even before the next render.
  const _setSelect = (on) => {
    if (selectModeRef.current === on) return;  // dedup — avoid POST spam on key-repeat / no-ops
    selectModeRef.current = on;
    setSelectMode(on);
    try {
      fetch(apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/mouse'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: !on }),
      }).catch(() => {});
    } catch (_e) { /* swallow */ }
  };

  const toggleSelectMode = () => {
    const next = !selectModeRef.current;
    _setSelect(next);
    if (_tlToast) {
      _tlToast(next
        ? 'Zaznaczanie: przeciągnij po tekście i puść = kopia. Skrót: trzymaj ⌥Option. Kliknij, by wrócić.'
        : 'Mysz tmux z powrotem włączona', 'ok');
    }
    try { const w = iframeRef.current && iframeRef.current.contentWindow; const t = w && (w.term || w.t); if (t && t.focus) t.focus(); } catch (_e) { /* swallow */ }
  };

  // Reset on session change; restore mouse-on for a session we leave with mouse
  // still off (so a revisit isn't stuck without tmux mouse/scroll).
  _tpUseEffect(() => {
    setSelectMode(false);
    selectModeRef.current = false;
    return () => {
      if (!selectModeRef.current) return;
      try {
        fetch(apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/mouse'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: true }),
        }).catch(() => {});
      } catch (_e) { /* swallow */ }
    };
  }, [sessionId]);

  // Fit/scroll cascade — keeps ttyd's xterm.js viewport sized to whatever
  // the iframe element currently occupies in the parent flex layout.
  // UAT 2026-05-27: ttyd's FitAddon ran once during xterm.js init at
  // whatever the iframe size was AT THAT MOMENT — often 0 or a default
  // 80x24 because the parent flex hadn't finished computing the
  // iframe's height yet. After that, FitAddon only refits when the
  // iframe's OWN window.resize fires (which only happens when the
  // browser viewport changes), so claude's input prompt + footer
  // stayed clipped below the iframe edge until the user resized the
  // browser window manually.
  //
  // Two-part fix, both keyed on the iframe element:
  //   1. ResizeObserver on the <iframe> DOM node → any parent-driven
  //      size change dispatches a synthetic resize INTO the iframe's
  //      contentWindow, which ttyd's listener catches and refits.
  //   2. A small burst of timed dispatches during the first ~1.5 s
  //      after iframe load to cover the layout race where the iframe
  //      gets its final dimensions AFTER ttyd's xterm.js has already
  //      done its initial (wrong) FitAddon pass.
  _tpUseEffect(() => {
    if (phase !== 'ready') return undefined;
    const el = iframeRef.current;
    if (!el) return undefined;

    const refit = () => {
      const win = el.contentWindow;
      if (!win) return;
      // Build the resize Event in the IFRAME's window so ttyd sees a
      // same-context event. Falls back to parent Event if the iframe's
      // Event constructor isn't accessible (shouldn't happen with
      // same-origin proxy, but cheap insurance).
      try {
        const EventCtor = win.Event || Event;
        win.dispatchEvent(new EventCtor('resize'));
      } catch (_e) { /* swallow */ }
    };

    // Burst of initial fits during the iframe-still-settling window.
    const timers = [
      setTimeout(refit, 200),
      setTimeout(refit, 600),
      setTimeout(refit, 1500),
    ];

    // Continuous refit on any parent-driven size change.
    let ro = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(refit);
      ro.observe(el);
    }
    return () => {
      timers.forEach(clearTimeout);
      if (ro) ro.disconnect();
    };
    // Re-run on `isFs`: the ResizeObserver below already catches the
    // fullscreen size jump, but entering/exiting fullscreen is a dramatic,
    // instant resize — exactly the case where FitAddon's one-shot timing
    // can miss (same reason the load path fires a burst). Re-running tears
    // the observer down, re-observes the iframe, and replays the timed
    // burst so the grid snaps to the new size cleanly.
  }, [phase, isFs]);

  // Pre-warm the slot. ttyd is cheap to spawn but tmux+claude can take
  // 10-20 s on a cold start — we tick a counter so the user has visual
  // feedback that something IS happening.
  _tpUseEffect(() => {
    if (!sessionId) return undefined;
    // Don't resurrect a session the user just closed — terminal mode would
    // otherwise cold-spawn it right back. (Re-opening it via the list clears the
    // guard, so an intentional reopen still works.)
    const _guard = window.HubSessionListOrder && window.HubSessionListOrder.closedGuard;
    if (_guard && _guard.isClosed(sessionId)) {
      setPhase('ensure_err');
      setErrMsg('Sesja zamknięta — otwórz ją z listy, aby wznowić.');
      return undefined;
    }
    setPhase('ensuring');
    setErrMsg(null);
    setSpawned(false);
    setElapsedMs(0);
    setIframeLoaded(false);
    const startedAt = Date.now();
    const tick = setInterval(() => setElapsedMs(Date.now() - startedAt), 250);
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(
          apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/term/ensure'),
          { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
        );
        if (cancelled) return;
        if (!r.ok) {
          let detail = '';
          try { detail = (await r.json()).detail || ''; } catch (_e) { /* ignore */ }
          throw new Error(detail || `HTTP ${r.status}`);
        }
        const body = await r.json().catch(() => ({}));
        if (cancelled) return;
        setSpawned(body && body.spawned === true);
        setPhase('ready');
      } catch (e) {
        if (cancelled) return;
        setErrMsg((e && e.message) || 'spawn failed');
        setPhase('ensure_err');
      } finally {
        clearInterval(tick);
      }
    })();
    return () => { cancelled = true; clearInterval(tick); };
  }, [sessionId]);

  const iframeUrl = apiUrl(
    '/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/term/'
  );
  // Mobile "reload render" action: reconnect the ttyd client in-place so tmux
  // repaints the whole pane from scratch (fixes garbled/half-rendered output
  // after a rotate/resume/scrollback glitch) — like reloading just this
  // session, not the whole page. contentWindow.location.reload() keeps the
  // iframe DOM node (and its ResizeObserver/refs) intact, and onLoad re-runs
  // the mobile font/scroll patch and re-sets iframeLoaded.
  const reloadTerminal = _tpUseCallback(() => {
    const f = iframeRef.current;
    if (!f) return;
    setIframeLoaded(false);
    try {
      if (f.contentWindow) f.contentWindow.location.reload();
      else f.src = iframeUrl;
    } catch (_e) {
      try { f.src = iframeUrl; } catch (_e2) { /* cross-origin guard — swallow */ }
    }
  }, [iframeUrl]);
  const elapsedS = (elapsedMs / 1000).toFixed(1);

  const statusColor =
    phase === 'ready' && iframeLoaded ? 'var(--ok)'
    : (phase === 'ensure_err' || phase === 'iframe_err') ? 'var(--err)'
    : 'var(--fg-3)';
  const statusLabel = (() => {
    if (phase === 'ensuring') return `warmowanie sesji… ${elapsedS}s`;
    if (phase === 'ensure_err') return `błąd spawn — ${errMsg}`;
    if (phase === 'iframe_err') return `błąd iframe — ${errMsg || 'load failed'}`;
    if (phase === 'ready' && !iframeLoaded) return 'ładowanie terminala…';
    return spawned ? 'live (interactive, świeży spawn)' : 'live (interactive, slot warm)';
  })();

  // On desktop the inline usage hands this status up to the chat header
  // (next to "N msg · ago · branch") instead of rendering its own status
  // row. Push the label + the ttyd/tmux detail whenever they change; clear
  // on unmount so the header doesn't keep stale terminal status in chat.
  const _statusDetail = `ttyd / tmux -L hd-orch / hd-${(sessionId || '').slice(0, 8)}`;
  _tpUseEffect(() => {
    if (typeof onStatusChange === 'function') onStatusChange({ label: statusLabel, detail: _statusDetail });
  }, [statusLabel, _statusDetail, onStatusChange]);
  _tpUseEffect(() => () => {
    if (typeof onStatusChange === 'function') onStatusChange(null);
  }, [onStatusChange]);

  return (
    <div ref={rootRef} style={{
      display: 'flex', flexDirection: 'column', minHeight: 0,
      flex: 1, gap: (compact || isFs) ? 0 : 8, position: 'relative',
      // Mobile: zero padding on all sides so the iframe spans the
      // full viewport width AND touches the chat header at the top
      // (status text moved up into the header subtitle). Fullscreen gets
      // the same full-bleed treatment + a dark backdrop so the iframe
      // fills the whole screen edge-to-edge.
      padding: (compact || isFs) ? 0 : '8px 14px 14px',
      background: isFs ? '#0e0f12' : undefined,
    }}>
      {/* Persistent file input — see fileInputRef note above. Visually
          hidden but NOT display:none (mobile Safari won't open the picker
          for a display:none input clicked programmatically). No `accept`
          so iOS offers both Photo Library and Files. */}
      <input
        ref={fileInputRef} type="file" multiple
        onChange={onFilesChosen}
        style={{ position: 'absolute', width: 1, height: 1, opacity: 0, overflow: 'hidden', left: -9999, pointerEvents: 'none' }}
      />
      {/* Drag-and-drop overlay — shown while a file hovers over the
          terminal. pointerEvents:none so the drag/drop still reaches the
          iframe handler underneath (which does the actual upload). */}
      {dragging && phase === 'ready' && (
        <div style={{
          position: 'absolute', inset: 0, zIndex: 30, pointerEvents: 'none',
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12,
          background: 'oklch(0.6 0.13 264 / 0.18)',
          border: '2px dashed var(--accent)',
          borderRadius: compact ? 0 : 'var(--r-sm)',
          backdropFilter: 'blur(1px)',
          color: 'var(--accent)', fontSize: 'var(--t-body)', fontWeight: 500,
          fontFamily: 'JetBrains Mono, ui-monospace, monospace',
        }}>
          <Icon name="upload" size={40} color="var(--accent)" />
          <span>Upuść plik, aby wysłać do sesji</span>
        </div>
      )}
      {/* Voice recording indicator (Cmd+M) — tap to stop. */}
      {_recording && (
        <button
          onClick={() => { const v = _voiceRef.current; if (v) v.stop(); }}
          title="Nagrywanie — kliknij, by zakończyć"
          style={{
            position: 'absolute', bottom: 'calc(14px + env(safe-area-inset-bottom))', left: '50%', transform: 'translateX(-50%)',
            zIndex: 20, display: 'inline-flex', alignItems: 'center', gap: 7,
            padding: '6px 12px', borderRadius: 'var(--r-pill)', cursor: 'pointer',
            background: 'var(--err-bg)', color: 'var(--err)',
            border: '1px solid oklch(0.70 0.18 25 / 0.45)',
            fontSize: 'var(--t-cap)', fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          }}
        >
          <span style={{ width: 8, height: 8, borderRadius: 4, background: 'var(--err)' }} />
          nagrywanie…
        </button>
      )}
      {/* Upload-in-progress indicator — covers the POST + paste latency. */}
      {uploading > 0 && !_recording && (
        <div
          title="Wysyłanie pliku do sesji…"
          style={{
            position: 'absolute', bottom: 'calc(14px + env(safe-area-inset-bottom))', left: '50%', transform: 'translateX(-50%)',
            zIndex: 20, display: 'inline-flex', alignItems: 'center', gap: 7,
            padding: '6px 12px', borderRadius: 'var(--r-pill)',
            background: 'var(--accent-soft)', color: 'var(--accent)',
            border: '1px solid var(--accent-line)',
            fontSize: 'var(--t-cap)', fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          }}
        >
          <Icon name="spinner" size={13} color="var(--accent)" />
          {uploading === 1 ? 'wysyłanie pliku…' : `wysyłanie ${uploading} plików…`}
        </div>
      )}
      {/* In-panel status row — only when the parent ISN'T hosting the
          status in the header (i.e. the modal usage, which passes no
          onStatusChange). The inline desktop chat view pushes it up. */}
      {!compact && !onStatusChange && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flexShrink: 0,
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: 4,
            background: statusColor,
            boxShadow: phase === 'ready' && iframeLoaded ? `0 0 6px ${statusColor}` : 'none',
          }} />
          <span>{statusLabel}</span>
          <span style={{ marginLeft: 'auto', fontSize: 'var(--t-2xs)' }}>ttyd / tmux -L hd-orch / hd-{(sessionId || '').slice(0, 8)}</span>
        </div>
      )}
      {phase === 'ensuring' && (
        <div style={{
          flex: 1, minHeight: 0,
          border: compact ? 'none' : '1px solid var(--hairline)',
          borderRadius: compact ? 0 : 'var(--r-sm)',
          background: '#0b0d11', color: '#d5dbe5',
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          gap: 12, fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          padding: 16, textAlign: 'center',
        }}>
          <div style={{ color: 'var(--fg-2)', fontSize: 'var(--t-md)' }}>uruchamianie sesji…</div>
          <div style={{ color: 'var(--accent)', fontSize: 22, fontWeight: 500, fontVariantNumeric: 'tabular-nums' }}>
            {elapsedS}s
          </div>
          <div style={{ color: 'var(--fg-3)', fontSize: 'var(--t-xs)', maxWidth: 260, lineHeight: 1.4 }}>
            pierwsze otwarcie po idle TTL trwa ~10-20s
          </div>
        </div>
      )}
      {phase === 'ensure_err' && (
        <div style={{
          flex: 1, minHeight: 0,
          border: '1px solid var(--err)', borderRadius: 'var(--r-sm)',
          background: 'oklch(0.18 0.05 25 / 0.30)', color: 'var(--fg)',
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          gap: 12, padding: 24, textAlign: 'center', fontSize: 'var(--t-sm)',
        }}>
          <div style={{ fontWeight: 500 }}>Nie udało się odpalić sesji</div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', maxWidth: 600 }}>{errMsg}</div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
            Sprawdź czy ttyd_enabled jest włączone i czy claude potrafi się uruchomić w cwd tej sesji.
          </div>
        </div>
      )}
      {phase === 'ready' && (
        <iframe
          ref={iframeRef}
          src={iframeUrl}
          onLoad={() => {
            setIframeLoaded(true);
            // ttyd's xterm.js doesn't auto-scroll to the bottom of the
            // buffer on initial paint — UAT 2026-05-27: user landed
            // mid-history with claude's input prompt hidden below the
            // viewport. Same-origin proxy means we CAN reach into the
            // iframe and force a scrollToBottom() on whatever Terminal
            // ttyd exposed globally. We poll a few times because the
            // Terminal instance is created AFTER the WS connects, not
            // at DOMContentLoaded — there's a 100-500ms window before
            // it appears on window.
            const win = iframeRef.current && iframeRef.current.contentWindow;
            if (!win) return;
            // Autoplay unlock: the user's gestures (typing, clicking) land INSIDE
            // this same-origin ttyd iframe, so the PARENT document — where TTS
            // actually plays — never gets user-activation, and auto read-aloud is
            // silently blocked / hangs (the manual 🔊 works because its click is a
            // parent gesture). Unlock TTS from the first real gesture inside the
            // terminal; one-shot, listeners die with the iframe document.
            try {
              const idoc = win.document;
              const _unlockFromTerminal = () => {
                if (tts && typeof tts.unlock === 'function') {
                  try { tts.unlock(); } catch (_e) { /* ignore */ }
                }
                try {
                  idoc.removeEventListener('pointerdown', _unlockFromTerminal, true);
                  idoc.removeEventListener('keydown', _unlockFromTerminal, true);
                } catch (_e) { /* ignore */ }
              };
              idoc.addEventListener('pointerdown', _unlockFromTerminal, true);
              idoc.addEventListener('keydown', _unlockFromTerminal, true);
            } catch (_e) { /* iframe not ready / cross-origin */ }
            // App shortcuts (Cmd+K / Cmd+↑↓ / Cmd+N) must work even when the
            // terminal iframe holds focus — forward them to the parent.
            _forwardAppShortcuts(win);
            // Typing by hand into ttyd → drop the on-voice read-aloud latch.
            _detectKeyboardInput(win, sessionId);
            // Shift+Enter → newline (Meta+Enter) instead of submit.
            _interceptShiftEnter(win);
            // Hold ⌥Option → momentary select-text mode (tmux mouse off).
            _interceptAltSelect(win, () => _setSelect(true), () => _setSelect(false));
            // ⌥/Shift+drag selection → auto-copy to clipboard (on mouseup).
            _bindCopyOnSelect(win, (ok) => {
              if (!_tlToast) return;
              if (ok) _tlToast('Skopiowano do schowka', 'ok');
              else _tlToast('Nie udało się skopiować — kliknij w terminal i zaznacz jeszcze raz', 'warn');
            });
            // Ctrl+V with raw image data (screenshots) → upload + paste path.
            _interceptCtrlV(win, (files) => { if (uploadFilesRef.current) uploadFilesRef.current(files); });
            // Cmd+V (paste event) with FILES — incl. Finder-copied files,
            // whose bytes only the paste event exposes — → upload + path.
            _interceptPasteFiles(win, (files) => { if (uploadFilesRef.current) uploadFilesRef.current(files); });
            // Drag a file from the OS onto the terminal → upload + path,
            // with a "drop here" overlay shown in the parent while dragging.
            _interceptDropFiles(win, (files) => { if (uploadFilesRef.current) uploadFilesRef.current(files); }, setDragging);
            // Inject CSS that strips ttyd's default body / xterm
            // container padding. Without this the iframe's right
            // edge has a visible gap because ttyd's stylesheet
            // pads .xterm-rows-area / body — left edge happens to
            // line up because xterm centers its character grid in
            // the leftover space. Same-origin proxy means we can
            // write into the iframe document directly.
            try {
              const doc = win.document;
              if (doc && doc.head && !doc.querySelector('[data-hub-fullbleed]')) {
                const style = doc.createElement('style');
                style.setAttribute('data-hub-fullbleed', '1');
                // Three real culprits:
                //  1. ttyd shows a scrollbar on .xterm-viewport that
                //     reserves ~15px on the right. Hide it: still
                //     scrollable via touch/wheel, just no rail.
                //  2. xterm.js sizes the character grid to whole cells
                //     so any sub-cell remainder paints as the iframe
                //     body background (white-ish on some themes) and
                //     looks like padding. Force every wrapper to the
                //     terminal's own background colour so the gap is
                //     invisible.
                //  3. .xterm-screen is centered inside .xterm by
                //     ttyd's default CSS via implicit margin-auto —
                //     pin it left so the gap (if any) sits on the
                //     right edge consistently with the rest of the UI.
                style.textContent = (
                  'html, body { margin: 0 !important; padding: 0 !important; background: #0e0f12 !important; }'
                  + ' #terminal-container, .terminal, .terminal.xterm, .xterm { padding: 0 !important; margin: 0 !important; background: #0e0f12 !important; width: 100% !important; }'
                  + ' .xterm-viewport { padding: 0 !important; margin: 0 !important; background: #0e0f12 !important; width: 100% !important; right: 0 !important; overflow-y: auto !important; scrollbar-width: none !important; }'
                  + ' .xterm-viewport::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }'
                  + ' .xterm-screen { padding: 0 !important; margin: 0 !important; background: #0e0f12 !important; }'
                );
                doc.head.appendChild(style);
              }
            } catch (_e) { /* swallow */ }
            const tryNames = ['term', 't', 'terminal'];
            const findTerm = () => {
              for (const n of tryNames) {
                const candidate = win[n];
                if (candidate && typeof candidate.scrollToBottom === 'function') {
                  return candidate;
                }
              }
              return null;
            };
            // Two-phase loop:
            //   PHASE A — wait for window.term to exist (xterm.js init
            //   races the WS handshake; term appears within 200-500ms).
            //   PHASE B — re-apply the mobile font override REPEATEDLY
            //   for ~6 seconds. Why: ttyd sends a "config" frame on
            //   WS connect that contains the fontSize from its argv
            //   (--client-option fontSize=13 in our case). That frame
            //   arrives AFTER our first override attempt and resets
            //   the option back to 13. Polling defeats this race —
            //   we just keep re-applying until the override sticks.
            //
            // Both phases reuse the same setTimeout chain to stay
            // out of useEffect cleanup territory; closure-captured
            // `cancelled` would be nice but iframe onLoad is a
            // one-shot event so a stale callback is harmless.
            const applyMobileFont = (t) => {
              // Try v5 proxy first, fall back to v4 setOption — different
              // xterm.js versions expose different APIs and we don't know
              // which ttyd bundled.
              let ok = false;
              if (t.options) {
                try { t.options.fontSize = _MOBILE_XTERM_FONT_SIZE; ok = true; } catch (_e) {}
              }
              if (!ok && typeof t.setOption === 'function') {
                try { t.setOption('fontSize', _MOBILE_XTERM_FONT_SIZE); ok = true; } catch (_e) {}
              }
              // Dispatch resize so FitAddon refits cols/rows under the
              // new cell metrics.
              try {
                const EventCtor = win.Event || Event;
                win.dispatchEvent(new EventCtor('resize'));
              } catch (_e) {}
              return ok;
            };
            // Kill the right-edge gap at its ROOT, once and forever.
            //
            // ttyd 1.7.4 bundles xterm.js whose FitAddon computes:
            //   availableWidth = parentWidth - padding
            //                  - (scrollback===0 ? 0 : viewport.scrollBarWidth)
            // and Viewport sets, ONCE in its constructor and never again:
            //   this.scrollBarWidth =
            //       viewportEl.offsetWidth - scrollArea.offsetWidth || 15
            //
            // The `|| 15` is the trap: our CSS hides the scrollbar, so the
            // measured difference is 0, so scrollBarWidth becomes `0 || 15`
            // = 15. FitAddon therefore subtracts 15px on EVERY refit, no
            // matter what CSS we inject — the rendered grid is permanently
            // ~2 cols short on the right (UAT 2026-05-28: 430px iframe,
            // 412px grid). The previous JS `term.resize()` poll fought
            // this every tick and produced the visible periodic "jump".
            //
            // Since scrollBarWidth is assigned exactly once (verified
            // against the bundled source — syncScrollArea does NOT
            // recompute it), we patch it to 0 a single time after the
            // Viewport exists. Every subsequent FitAddon pass then
            // subtracts 0 and computes the FULL width, and stays there —
            // no polling, no fighting, no jump, scrollback preserved.
            const patchScrollbar = (t) => {
              // FitAddon reads `term._core.viewport.scrollBarWidth`.
              // `_core` / `viewport` are stable property names in this
              // bundle; fall back to `_viewport` defensively in case a
              // future ttyd renames it. Idempotent (just assigns 0).
              try {
                const core = t._core;
                if (!core) return false;
                const vp = core.viewport || core._viewport;
                if (vp && vp.scrollBarWidth !== 0) {
                  vp.scrollBarWidth = 0;
                }
                return true;
              } catch (_e) {
                return false;
              }
            };

            // Touch-swipe → scroll on mobile.
            //
            // ttyd's xterm.js only scrolls the viewport from touch when
            // tmux mouse-mode is OFF. Our terminal always runs with
            // mouse-mode ON (`enable-mouse-events`: tmux + claude TUI),
            // so xterm's touch handler is a no-op and a swipe does
            // nothing (verified against the bundled source: the
            // touchstart/touchmove listeners both early-`return` only
            // when `!areMouseEventsActive`). Desktop still scrolls
            // because the WHEEL handler IS universal — it translates a
            // wheel into arrow keys on the alt-screen (TUI), scrolls the
            // viewport when there's scrollback, or forwards a mouse-wheel
            // to tmux when the app requested wheel events.
            //
            // So we make mobile behave exactly like desktop: convert a
            // finger swipe into REAL WheelEvents — same constructor,
            // dispatched at the SAME element desktop wheel hits — and let
            // xterm's own (unmodified) wheel handler take whichever branch
            // it would on desktop. We do NOT call triggerDataEvent / the
            // arrow path ourselves: the app may instead want mouse-wheel
            // events (s.wheel branch), and only a real wheel hits ALL the
            // listeners xterm registered (the scroll-translation one AND
            // the mouse-service forwarder).
            //
            // Two details that broke the earlier attempt:
            //   1. The wheel/touch listeners live on `this.element` = the
            //      `.xterm` root div (verified: `t=this.element` in the
            //      bundle's mouse-gesture setup). We now dispatch ON that
            //      element and listen on the iframe `document` (capture),
            //      which survives xterm re-creating its DOM on reconnect.
            //   2. The mouse-forwarder encodes the cell from the event's
            //      clientX/clientY — a wheel with no coords maps to an
            //      out-of-range cell and tmux/claude ignores it. We now
            //      copy the live touch coordinates onto every wheel.
            //
            // We only hijack once the finger moves past a small threshold,
            // so a plain tap still falls through to xterm (focus / cursor /
            // soft keyboard). preventDefault on the move suppresses native
            // scroll AND the synthesized mouse-click, so a swipe never
            // lands a stray tmux click. One wheel "notch" (±1 cell) is
            // dispatched per cell of finger travel — magnitude-independent
            // branches (mouse-forward = one tick/event) then scroll
            // proportionally, and the alt-screen arrow translation (which
            // floors sub-line deltas to zero) always registers.
            const enableTouchScroll = (t) => {
              try {
                const doc = win.document;
                if (doc.__hubTouchScroll) return true;  // attach once per doc
                doc.__hubTouchScroll = true;

                const THRESH = 8;            // px before a swipe counts as scroll
                const MAX_NOTCH = 16;        // clamp a flick to a sane burst
                let startY = 0, lastY = 0, accum = 0, active = false;

                const cellH = () => {
                  const screenEl = doc.querySelector('.xterm-screen');
                  const rows = (win.term && win.term.rows) || t.rows || 24;
                  const h = screenEl ? (screenEl.offsetHeight || 0) : 0;
                  return (h > 0 && rows > 0) ? (h / rows) : 18;
                };

                const onStart = (e) => {
                  if (!e.touches || e.touches.length !== 1) return;
                  startY = lastY = e.touches[0].clientY;
                  accum = 0;
                  active = false;
                  // Do NOT stop propagation — let xterm see the touchstart
                  // so a tap still focuses / positions the cursor.
                };
                const onMove = (e) => {
                  if (!e.touches || e.touches.length !== 1) return;
                  const touch = e.touches[0];
                  const y = touch.clientY;
                  if (!active) {
                    if (Math.abs(y - startY) < THRESH) return;  // still maybe a tap
                    active = true;
                    lastY = y;
                  }
                  // finger up → positive delta → scroll DOWN (newer content)
                  accum += (lastY - y);
                  lastY = y;
                  const ch = cellH();
                  const lines = Math.trunc(accum / ch);
                  if (lines !== 0) {
                    accum -= lines * ch;
                    const xtermEl = doc.querySelector('.xterm');
                    if (xtermEl) {
                      const dir = lines < 0 ? -1 : 1;
                      const notches = Math.min(Math.abs(lines), MAX_NOTCH);
                      const WheelCtor = win.WheelEvent || WheelEvent;
                      for (let i = 0; i < notches; i++) {
                        try {
                          xtermEl.dispatchEvent(new WheelCtor('wheel', {
                            deltaY: dir * ch,
                            deltaMode: 0,
                            clientX: touch.clientX,
                            clientY: touch.clientY,
                            bubbles: true,
                            cancelable: true,
                          }));
                        } catch (_e) { /* swallow */ }
                      }
                    }
                  }
                  // Suppress native scroll + the synthesized mouse-click
                  // so we don't double-scroll or land a tmux click.
                  e.preventDefault();
                  e.stopImmediatePropagation();
                };
                const onEnd = () => { active = false; accum = 0; };

                doc.addEventListener('touchstart', onStart, { capture: true, passive: true });
                doc.addEventListener('touchmove', onMove, { capture: true, passive: false });
                doc.addEventListener('touchend', onEnd, { capture: true, passive: true });
                doc.addEventListener('touchcancel', onEnd, { capture: true, passive: true });
                return true;
              } catch (_e) {
                return false;
              }
            };
            let attempts = 0;
            let scrolled = false;
            let patched = false;
            let touchBound = false;
            const tick = () => {
              const t = findTerm();
              if (t) {
                // Patch the scrollbar reservation BEFORE the font reapply
                // so the resize it dispatches refits at full width.
                if (!patched) patched = patchScrollbar(t);
                if (compact && !touchBound) touchBound = enableTouchScroll(t);
                if (compact) applyMobileFont(t);
                if (!scrolled) {
                  try { t.scrollToBottom(); scrolled = true; } catch (_e) {}
                }
                attempts += 1;
                // Keep re-applying the font on mobile for ~6s to outlast
                // ttyd's config-frame reset. On desktop one pass is
                // enough — just bail after the scroll. The scrollbar
                // patch is sticky so it doesn't need the repeat loop.
                if (compact && attempts < 30) {
                  setTimeout(tick, 200);
                }
                return;
              }
              attempts += 1;
              if (attempts < 30) setTimeout(tick, 150);  // wait for window.term
            };
            setTimeout(tick, 150);
          }}
          onError={() => { setErrMsg('iframe load failed'); setPhase('iframe_err'); }}
          // `allow-same-origin` is required: ttyd's xterm.js opens a
          // same-origin WebSocket to /api/orchestrator/sessions/<sid>/term/ws,
          // and without this flag the iframe is treated as a null origin
          // and the WS handshake fails. We do NOT add `allow-forms` /
          // `allow-popups` — ttyd has no such surface.
          sandbox="allow-scripts allow-same-origin"
          allow="clipboard-read; clipboard-write"
          title={`orchestrator terminal ${(sessionId || '').slice(0, 8)}`}
          style={{
            flex: 1, minHeight: 0, width: '100%',
            // Drop the chrome on mobile for full-bleed (every pixel matters
            // on a phone) and in fullscreen. Desktop windowed keeps the
            // subtle hairline border so the terminal reads as a panel
            // inside the chat surface.
            border: (compact || isFs) ? 'none' : '1px solid var(--hairline)',
            borderRadius: (compact || isFs) ? 0 : 'var(--r-sm)',
            // Match the ttyd theme background exactly (see _TTYD_THEME in
            // orchestrator_ttyd.py). Pure #000 mismatched the xterm
            // interior and painted a visible stripe on the right where
            // FitAddon's sub-cell remainder leaks through.
            background: '#0e0f12',
          }}
        />
      )}
      {phase === 'iframe_err' && (
        <div style={{
          flex: 1, minHeight: 0,
          border: '1px solid var(--err)', borderRadius: 'var(--r-sm)',
          background: 'oklch(0.18 0.05 25 / 0.30)', color: 'var(--fg)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 'var(--t-sm)',
        }}>
          iframe nie załadował się — {errMsg}
        </div>
      )}
      {compact && phase === 'ready' && keyboardOpen && (
        <_MobileSoftKeyboard
          iframeRef={iframeRef}
          sessionId={sessionId}
          onPickFiles={() => { if (fileInputRef.current) fileInputRef.current.click(); }}
        />
      )}
      {/* Top-right corner button. Desktop: select-to-copy toggle (turns tmux
          mouse off so a drag is a clean selection; ⌥Option does it momentarily).
          Mobile: that copy affordance is hidden — copying is handled by the
          "ostatnia wiadomość" modal — and the corner instead carries a reload
          button that repaints the tmux pane from scratch (more useful on a
          phone, where you can't just refresh the whole page cheaply). */}
      {phase === 'ready' && !compact && (
        <_TermSelectButton active={selectMode} isFs={isFs} compact={compact} onToggle={toggleSelectMode} />
      )}
      {phase === 'ready' && compact && (
        <_TermReloadButton onReload={reloadTerminal} />
      )}
      {phase === 'ready' && featureFlagEnabled && (
        <_TermSpeakButton tts={tts} speaking={isSpeaking} isFs={isFs} compact={compact} onSpeakLast={onSpeakLast} />
      )}
      {phase === 'ready' && (typeof window !== 'undefined' && window.useVoiceCapture) && (
        <_TermMicButton recording={_recording} isFs={isFs} compact={compact}
          onToggle={() => { if (_voiceRef.current.state === 'recording') _voiceRef.current.stop(); else _voiceRef.current.start(); }} />
      )}
      {compact && phase === 'ready' && (
        <_TermLastMsgButton isFs={isFs} compact={compact} onShowLast={onShowLast} />
      )}
      {!compact && phase === 'ready' && (
        <_TermFsButton isFs={isFs} onToggle={toggleFullscreen} />
      )}
      {/* Scroll-to-bottom FAB — appears when scrolled up into the terminal
          scrollback, snaps back to the live tail on click. */}
      <_TermScrollFab
        iframeRef={iframeRef}
        phase={phase}
        iframeLoaded={iframeLoaded}
        compact={compact}
        keyboardOpen={keyboardOpen}
        sessionId={sessionId}
      />
    </div>
  );
}

// ── modal preview (wraps either TerminalLiveView or legacy SSE view) ──
//
// Kept for callers that still want a popup view (e.g. external callers
// or future surfaces). The main orchestrator panel now uses the inline
// toggle instead; the kebab modal-trigger was removed in the toggle
// rollout. This component remains exported so a follow-up can revive
// the modal without re-implementing it.

function TerminalPreviewModal({ open, onClose, sessionId }) {
  const [mode, setMode] = _tpUseState(null); // 'iframe' | 'sse' | null while loading

  _tpUseEffect(() => {
    if (!open) { setMode(null); return undefined; }
    let cancelled = false;
    fetch(apiUrl('/api/orchestrator/settings'))
      .then((r) => r.json())
      .then((s) => { if (!cancelled) setMode(s && s.ttyd_enabled ? 'iframe' : 'sse'); })
      .catch(() => { if (!cancelled) setMode('sse'); });
    return () => { cancelled = true; };
  }, [open]);

  if (!open || !window.Modal) return null;
  if (mode === null) {
    return (
      <window.Modal open={open} onClose={onClose} title="Terminal…" width={920}>
        <div style={{ padding: 18, fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>…</div>
      </window.Modal>
    );
  }
  if (mode === 'iframe') {
    return (
      <window.Modal open={open} onClose={onClose} title={`Terminal — ${(sessionId || '').slice(0, 8)}`} width={960}>
        <div style={{ display: 'flex', flexDirection: 'column', height: 540 }}>
          <TerminalLiveView sessionId={sessionId} />
        </div>
      </window.Modal>
    );
  }
  return <_SseView open={open} onClose={onClose} sessionId={sessionId} />;
}

// ── legacy SSE-polled <pre> fallback view ─────────────────────────
//
// Lifted verbatim from the pre-ttyd implementation. Streams snapshots
// from GET /api/orchestrator/sessions/<sid>/pane (SSE, event
// "pane_snapshot"). Read-only — claude's TUI colors and cursor state
// are lost, but the content (banners, input box, "auto mode on" marker,
// assistant scrollback) is fully visible. Kept as the permanent fallback
// for ttyd_enabled=false (plan rollback Level 1).

function _SseView({ open, onClose, sessionId }) {
  const [text, setText] = _tpUseState('');
  const [status, setStatus] = _tpUseState('connecting'); // connecting | live | error | done
  const [statusDetail, setStatusDetail] = _tpUseState('');
  const preRef = _tpUseRef(null);

  _tpUseEffect(() => {
    if (!open || !sessionId) return undefined;
    setText('');
    setStatus('connecting');
    setStatusDetail('');
    const es = new EventSource(apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/pane'));
    es.addEventListener('pane_snapshot', (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (typeof payload.text === 'string') {
          setText(payload.text);
          setStatus('live');
        }
      } catch (_e) { /* ignore malformed */ }
    });
    es.addEventListener('error', (ev) => {
      let detail = '';
      try {
        if (ev && ev.data) detail = (JSON.parse(ev.data).message) || '';
      } catch (_e) { /* ignore */ }
      setStatus('error');
      setStatusDetail(detail || 'stream disconnected');
      try { es.close(); } catch (_e) {}
    });
    es.addEventListener('done', (ev) => {
      let reason = '';
      try { if (ev && ev.data) reason = (JSON.parse(ev.data).reason) || ''; } catch (_e) {}
      setStatus('done');
      setStatusDetail(reason || 'stream closed');
      try { es.close(); } catch (_e) {}
    });
    return () => { try { es.close(); } catch (_e) {} };
  }, [open, sessionId]);

  _tpUseEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [text]);

  const statusColor = status === 'live'
    ? 'var(--ok)'
    : status === 'error'
      ? 'var(--err)'
      : 'var(--fg-3)';
  const statusLabel = status === 'connecting'
    ? 'łączenie…'
    : status === 'live'
      ? 'live (read-only)'
      : status === 'done'
        ? `zakończono${statusDetail ? ' — ' + statusDetail : ''}`
        : `błąd${statusDetail ? ' — ' + statusDetail : ''}`;

  return (
    <window.Modal open={open} onClose={onClose} title={`Podgląd terminala — ${(sessionId || '').slice(0, 8)}`} width={920}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '8px 14px 14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
          <span style={{
            width: 8, height: 8, borderRadius: 4,
            background: statusColor,
            boxShadow: status === 'live' ? `0 0 6px ${statusColor}` : 'none',
          }} />
          <span>{statusLabel}</span>
          <span style={{ marginLeft: 'auto', fontSize: 'var(--t-2xs)' }}>tmux -L hd-orch / hd-{(sessionId || '').slice(0, 8)}</span>
        </div>
        <pre
          ref={preRef}
          style={{
            margin: 0,
            padding: 12,
            background: '#0b0d11',
            color: '#d5dbe5',
            border: '1px solid var(--hairline)',
            borderRadius: 'var(--r-sm)',
            fontFamily: 'JetBrains Mono, ui-monospace, monospace',
            fontSize: 'var(--t-xs)',
            lineHeight: 1.35,
            height: 540,
            overflow: 'auto',
            whiteSpace: 'pre',
            wordBreak: 'normal',
          }}
        >
          {text || (status === 'connecting' ? '…' : '(brak danych — sesja jeszcze nie utworzyła tmux slotu?)')}
        </pre>
      </div>
    </window.Modal>
  );
}

Object.assign(window, { TerminalLiveView, TerminalPreviewModal });
