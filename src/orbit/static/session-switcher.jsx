// Desktop session switcher overlay — the macOS Cmd+Tab feel for sessions.
//
// Gesture: hold ⌥ (Option/Alt), tap ⇥ Tab to cycle through active sessions
// (⇧⇥ reverses; ↑/↓/←/→ also move), RELEASE ⌥ to jump (fast path). Esc/Enter/
// click are always-on fallbacks, so it can never get stuck if a modifier keyup
// is missed (no auto-commit timer — that was the footgun). We use ⌥⇥ rather
// than ⌘⇧ because ⌘⇧ collided with several other in-app shortcuts.
//
// Self-contained: owns the open state, the always-on key listeners, the pool
// fetch, the client MRU log, and the server flag gate. app.jsx just mounts
// <SessionSwitcher currentId={sessionId}/> inside DesktopHub (desktop-only).
// The branching logic (ordering / cycle / clamp) lives in the JSX-free,
// unit-tested window.HubSessionOrder (session-switcher-order.js).
//
// Flag: session_switcher_enabled (server, default True — see
// orchestrator_settings.py). OFF → no listeners attach and ⌥⇥ is a no-op; the
// ⌘←/→ agent-switch + ⌘↑/↓ session-cycle (orchestrator.jsx) are unaffected.

const { useState: _ssUseState, useEffect: _ssUseEffect, useRef: _ssUseRef, useMemo: _ssUseMemo, useCallback: _ssUseCallback } = React;

// activeElement that should swallow ⌥⇥ natively (e.g. Tab to indent / move
// focus in the composer) → we must NOT hijack the chord there. The focused ttyd
// terminal shows up as the <iframe> element (not a text field), so it still triggers.
function _ssIsTextEntry(el) {
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || el.isContentEditable === true;
}

function SessionSwitcher({ currentId }) {
  const O = window.HubSessionOrder; // pure logic (always loaded before this file)
  const [enabled, setEnabled] = _ssUseState(false);
  const [open, setOpen] = _ssUseState(false);
  const [slots, setSlots] = _ssUseState([]);
  const [loading, setLoading] = _ssUseState(false);
  const [index, setIndex] = _ssUseState(0);

  // Client MRU log — every navigation pushes the new session id to the front,
  // so the PREVIOUS session lands at the default highlight (the Cmd+Tab win).
  // The server pool is sorted by uptime, and merely viewing a warm slot does
  // not bump its idle clock, so recency must be tracked client-side.
  const mruRef = _ssUseRef([]);
  const prevFocusRef = _ssUseRef(null);
  const altHeldRef = _ssUseRef(false);    // was ⌥/Alt down when we opened / since?
  const confirmedRef = _ssUseRef(false);  // user has cycled at least once this open
  const boxRef = _ssUseRef(null);

  const ordered = _ssUseMemo(
    () => (O ? O.orderSessions(slots, mruRef.current, currentId) : []),
    [O, slots, currentId, open],
  );

  // Refs mirror state so the once-bound window listeners read fresh values.
  const openRef = _ssUseRef(open);        openRef.current = open;
  const indexRef = _ssUseRef(index);      indexRef.current = index;
  const orderedRef = _ssUseRef(ordered);  orderedRef.current = ordered;
  const currentIdRef = _ssUseRef(currentId); currentIdRef.current = currentId;

  // ── server flag (mount + live on Settings toggle) ──────────────────────
  _ssUseEffect(() => {
    let cancelled = false;
    const read = () => {
      fetch(window.apiUrl('/api/orchestrator/settings'))
        .then((r) => (r.ok ? r.json() : null))
        .then((cfg) => { if (!cancelled && cfg) setEnabled(cfg.session_switcher_enabled === true); })
        .catch(() => { /* blip → leave prior value; never poison to a stale enabled */ });
    };
    read();
    window.addEventListener('hub:session-switcher-change', read);
    return () => { cancelled = true; window.removeEventListener('hub:session-switcher-change', read); };
  }, []);

  // ── MRU log: seed + follow every route change ──────────────────────────
  _ssUseEffect(() => {
    if (!enabled || !O) return undefined;
    const onRoute = () => {
      try {
        const r = window.parseRoute(location.pathname);
        if (r && r.sessionId) mruRef.current = O.pushActivation(mruRef.current, r.sessionId);
      } catch (_e) { /* tolerate */ }
    };
    onRoute();
    window.addEventListener('router:change', onRoute);
    return () => window.removeEventListener('router:change', onRoute);
  }, [enabled, O]);

  const close = _ssUseCallback(() => {
    setOpen(false);
    altHeldRef.current = false;
    confirmedRef.current = false;
    const el = prevFocusRef.current;
    prevFocusRef.current = null;
    // Restore focus to wherever it was (the terminal iframe, a button, …).
    if (el && typeof el.focus === 'function') { try { el.focus(); } catch (_e) {} }
  }, []);

  const commit = _ssUseCallback(() => {
    const list = orderedRef.current || [];
    const target = list[O ? O.clampIndex(indexRef.current, list.length) : indexRef.current];
    close();
    if (target && target.session_id && target.session_id !== currentIdRef.current) {
      window.dispatchEvent(new CustomEvent('hub:open-session', { detail: { session_id: target.session_id } }));
    }
  }, [O, close]);

  // ── pool fetch while open (one-shot + light refresh, no permanent poll) ──
  _ssUseEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    const load = (first) => {
      if (first) setLoading(true);
      fetch(window.apiUrl('/api/orchestrator/pool'))
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (cancelled || !data) { if (first) setLoading(false); return; }
          const next = Array.isArray(data.slots) ? data.slots : [];
          setSlots(next);
          if (first) {
            const ord = O ? O.orderSessions(next, mruRef.current, currentIdRef.current) : next;
            setIndex(O ? O.defaultIndex(ord) : 0);
            setLoading(false);
          } else {
            // a slot may have been evicted mid-overlay → re-clamp selection
            const ord = O ? O.orderSessions(next, mruRef.current, currentIdRef.current) : next;
            setIndex((i) => (O ? O.clampIndex(i, ord.length) : i));
          }
        })
        .catch(() => { if (first) setLoading(false); });
    };
    load(true);
    const id = setInterval(() => load(false), 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [open, O]);

  // Steal focus into the overlay so subsequent keys (incl. across the ttyd
  // iframe boundary) reach our window listener and ⌘-keyup is observable here.
  _ssUseEffect(() => {
    if (open && boxRef.current) { try { boxRef.current.focus(); } catch (_e) {} }
  }, [open]);

  // Keep the highlighted row in view when the list scrolls.
  _ssUseEffect(() => {
    if (!open || !boxRef.current) return;
    const row = boxRef.current.querySelector('[data-ss-row="' + index + '"]');
    if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
  }, [open, index]);

  // ── the single always-on keyboard surface (capture phase) ──────────────
  _ssUseEffect(() => {
    if (!enabled || !O) return undefined;

    const onKeyDown = (e) => {
      if (e.altKey) altHeldRef.current = true;

      if (!openRef.current) {
        // OPEN: hold ⌥, tap ⇥ Tab. Skip when a text field is focused so native
        // Tab (indent / focus move) keeps working; the focused terminal iframe is
        // the <iframe> element (not a text field) so it still opens.
        if (e.altKey && e.key === 'Tab' && !e.repeat && !_ssIsTextEntry(document.activeElement)) {
          e.preventDefault();
          e.stopImmediatePropagation();
          prevFocusRef.current = document.activeElement;
          altHeldRef.current = true;
          confirmedRef.current = false;
          setOpen(true);
        }
        return;
      }

      const len = (orderedRef.current || []).length;
      const k = e.key;
      const consume = () => { e.preventDefault(); e.stopImmediatePropagation(); };

      if (k === 'Tab') {                               // ⇥ next · ⇧⇥ prev
        consume(); confirmedRef.current = true;
        setIndex((i) => (e.shiftKey ? O.reverse(i, len) : O.advance(i, len))); return;
      }
      if (k === 'ArrowDown' || k === 'ArrowRight') {
        consume(); confirmedRef.current = true; setIndex((i) => O.advance(i, len)); return;
      }
      if (k === 'ArrowUp' || k === 'ArrowLeft') {
        consume(); confirmedRef.current = true; setIndex((i) => O.reverse(i, len)); return;
      }
      if (k === 'Enter') { consume(); commit(); return; }
      if (k === 'Escape') { consume(); close(); return; }

      // Abort: an ⌥+<letter> combo we caught while open but before the user has
      // confirmed any cycle. Close WITHOUT preventDefault so the combo passes through.
      if (!confirmedRef.current && e.altKey && typeof k === 'string' && k.length === 1) {
        close();
        return;
      }
      // anything else: leave it alone
    };

    const onKeyUp = (e) => {
      if (openRef.current && altHeldRef.current && e.key === 'Alt') {
        e.preventDefault();
        commit(); // release-to-commit fast path
        return;
      }
      if (!e.altKey) altHeldRef.current = false;
    };

    window.addEventListener('keydown', onKeyDown, { capture: true });
    window.addEventListener('keyup', onKeyUp, { capture: true });
    return () => {
      window.removeEventListener('keydown', onKeyDown, { capture: true });
      window.removeEventListener('keyup', onKeyUp, { capture: true });
    };
  }, [enabled, O, commit, close]);

  if (!enabled || !open || !window.Modal) return null;

  const safeIdx = O.clampIndex(index, ordered.length);
  return (
    <window.Modal open={open} onClose={close} title={null} width={520}>
      <div ref={boxRef} tabIndex={-1} style={{ outline: 'none', display: 'flex', flexDirection: 'column', maxHeight: '70vh' }}>
        <div style={{
          padding: '12px 16px', borderBottom: '1px solid var(--hairline)',
          display: 'flex', alignItems: 'center', gap: 10,
        }}>
          <span style={{ fontSize: 'var(--t-md)', fontWeight: 600 }}>Przełącz sesję</span>
          <span style={{ flex: 1 }} />
          <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{ordered.length} aktywnych</span>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: 6 }} className="scroll-hide">
          {loading && ordered.length === 0 && (
            <div style={{ padding: 22, textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>Ładowanie…</div>
          )}
          {!loading && ordered.length === 0 && (
            <div style={{ padding: 22, textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>
              Brak aktywnych sesji · <span className="kbd">⌘</span> <span className="kbd">N</span> rozpocznij nową
            </div>
          )}
          {ordered.map((s, i) => {
            const isSel = i === safeIdx;
            const isCur = s.session_id === currentId;
            const idle = O.humanizeIdle(s.idle_s);
            return (
              <button
                key={s.session_id}
                data-ss-row={i}
                onClick={() => { setIndex(i); commit(); }}
                onMouseEnter={() => setIndex(i)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10, width: '100%', textAlign: 'left',
                  padding: '9px 12px', borderRadius: 'var(--r-control)', fontFamily: 'inherit', cursor: 'pointer',
                  border: '1px solid ' + (isSel ? 'var(--accent-line)' : 'transparent'),
                  background: isSel ? 'var(--surface-2)' : 'transparent',
                  color: 'var(--fg)',
                  borderLeft: '3px solid ' + (isSel ? 'var(--accent)' : 'transparent'),
                }}
              >
                <span style={{ width: 12, fontSize: 'var(--t-cap)', color: isCur ? 'var(--accent)' : 'var(--fg-4)' }}>{isCur ? '●' : (isSel ? '▸' : '')}</span>
                <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, whiteSpace: 'nowrap' }}>{s.agent || 'Global'}</span>
                <span style={{
                  flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', color: 'var(--fg-3)',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{(s.title && s.title.trim()) ? s.title : '—'}</span>
                {s.persistent && <span title="trwała (keep-alive)" style={{ fontSize: 'var(--t-cap)' }}>📌</span>}
                {s.cooling && <span title="stygnie" style={{ fontSize: 'var(--t-cap)' }}>⏳</span>}
                {idle && <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)', whiteSpace: 'nowrap' }}>{idle}</span>}
              </button>
            );
          })}
        </div>

        <div style={{
          borderTop: '1px solid var(--hairline)', padding: '9px 14px',
          display: 'flex', flexWrap: 'wrap', gap: '6px 14px',
          fontSize: 'var(--t-xs)', color: 'var(--fg-4)', alignItems: 'center',
        }}>
          {[
            { keys: ['⇥'], label: 'następna' },
            { keys: ['↑', '↓'], label: 'wybierz' },
            { keys: ['⏎'], label: 'otwórz' },
            { keys: ['puść ⌥'], label: 'skok' },
            { keys: ['esc'], label: 'anuluj' },
          ].map((sc, i) => (
            <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              {sc.keys.map((kk, j) => <span key={j} className="kbd">{kk}</span>)}
              <span style={{ marginLeft: 2 }}>{sc.label}</span>
            </span>
          ))}
        </div>
      </div>
    </window.Modal>
  );
}

window.SessionSwitcher = SessionSwitcher;
