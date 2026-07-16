// State modals (Plan + Todos) for the Orchestrator header.
// Two buttons lazy-fetch GET /api/orchestrator/sessions/{id}/state and render
// either a desktop popover (compact=false) or mobile bottom sheet (compact=true).
// Each button hides (null) when its section is empty. Plan badge = 1, Todos
// badge = count of active (pending+in_progress) entries.

const { useEffect: _smUseEffect, useRef: _smUseRef, useState: _smUseState, useMemo: _smUseMemo } = React;

// Data hook — single source of truth for /state. Called ONCE in the parent
// (StateModalButtons) and the result is passed down to children as props.
// Earlier we called this hook in every Button + Viewer (4 instances),
// firing 4 fetches per mount; now it's 1 fetch on session-load + 1 reload
// per modal open. State is reset to null on sessionId change so badges
// don't flash the previous session's count while the new fetch is in
// flight (was a 50–200ms UX flicker on every session switch).
function useSessionState({ sessionId }) {
  const [state, setState] = _smUseState(null);
  const [loading, setLoading] = _smUseState(false);
  const [error, setError] = _smUseState(null);
  const reqIdRef = _smUseRef(0);

  const reload = _smUseRef(null);
  reload.current = async () => {
    if (!sessionId) return;
    const myId = ++reqIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const url = window.apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/state');
      const r = await fetch(url);
      if (!r.ok) throw new Error('GET /state → ' + r.status);
      const data = await r.json();
      if (myId !== reqIdRef.current) return;
      setState(data);
    } catch (e) {
      if (myId !== reqIdRef.current) return;
      console.error('useSessionState fetch failed:', e);
      setError(e && e.message ? e.message : 'Nie udało się pobrać stanu sesji');
    } finally {
      if (myId === reqIdRef.current) setLoading(false);
    }
  };
  _smUseEffect(() => {
    // Reset BEFORE firing the new fetch so the old session's badge counts
    // disappear instantly (no stale flicker).
    setState(null);
    setError(null);
    if (sessionId) reload.current();
  }, [sessionId]);
  return { state, loading, error, reload: () => reload.current() };
}

// Visual-only checkbox glyph for todos.
function _todoGlyph(status) {
  if (status === 'completed') return { glyph: '☑', color: 'var(--fg-2)' };
  if (status === 'in_progress') return { glyph: '◐', color: 'var(--accent)' };
  return { glyph: '☐', color: 'var(--fg-3)' };
}

function TodoRow({ todo, compact }) {
  const { glyph, color } = _todoGlyph(todo.status);
  const completed = todo.status === 'completed';
  const padV = compact ? 12 : 8;
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: 10,
      padding: padV + 'px 8px',
      borderTop: compact ? '1px solid var(--hairline)' : 'none',
    }}>
      <span aria-hidden="true" style={{
        flexShrink: 0, fontSize: 22, lineHeight: '22px', color, width: 22,
        textAlign: 'center', userSelect: 'none',
      }}>{glyph}</span>
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
        <span style={{
          fontWeight: 500, color: 'var(--fg)', fontSize: compact ? 'var(--t-md)' : 'var(--t-sm)',
          textDecoration: completed ? 'line-through' : 'none',
          opacity: completed ? 0.7 : 1,
          wordBreak: 'break-word',
        }}>{todo.subject || '(bez tytułu)'}</span>
        {todo.description && (
          <span className="mono" style={{
            fontSize: 'var(--t-xs)', color: 'var(--fg-3)', wordBreak: 'break-word',
          }}>{todo.description}</span>
        )}
      </div>
    </div>
  );
}

// Shared shells — desktop popover + mobile bottom sheet (mirror PinPopover shape).
// Delegates to the global Popover (anchor-relative, fixed-positioned, outside-
// click + Esc + touchstart dismiss). Title/subtitle header + children preserved.
function _DesktopShell({ open, onClose, anchorRef, title, subtitle, maxHeight = 480, children }) {
  return (
    <window.Popover open={open} onClose={onClose} anchorRef={anchorRef} placement="bottom-end" minWidth={320} maxWidth={480} maxHeight={maxHeight}>
      <div role="dialog" aria-label={title}>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
          padding: '6px 8px',
        }}>
          <span className="mono" style={{
            fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase',
            letterSpacing: '0.08em',
          }}>{title}</span>
          {subtitle && (
            <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>{subtitle}</span>
          )}
        </div>
        {children}
      </div>
    </window.Popover>
  );
}

function _MobileSheet({ open, onClose, title, subtitle, children }) {
  const [mounted, setMounted] = _smUseState(false);
  const [enter, setEnter] = _smUseState(false);
  _smUseEffect(() => {
    if (open) {
      setMounted(true);
      const id = requestAnimationFrame(() => setEnter(true));
      return () => cancelAnimationFrame(id);
    }
    setEnter(false);
    const t = setTimeout(() => setMounted(false), 220);
    return () => clearTimeout(t);
  }, [open]);
  _smUseEffect(() => {
    if (!open) return undefined;
    const onEsc = (e) => { if (e.key === 'Escape') onClose && onClose(); };
    document.addEventListener('keydown', onEsc);
    return () => document.removeEventListener('keydown', onEsc);
  }, [open, onClose]);
  _smUseEffect(() => {
    if (!open) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [open]);
  if (!mounted) return null;
  return (
    <div role="dialog" aria-label={title}
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        display: 'flex', flexDirection: 'column', justifyContent: 'flex-end',
      }}>
      <div onClick={() => onClose && onClose()}
        style={{
          position: 'absolute', inset: 0,
          background: enter ? 'rgba(0,0,0,0.5)' : 'rgba(0,0,0,0)',
          transition: 'background .22s ease',
        }} />
      <div style={{
        position: 'relative',
        background: 'var(--surface-1)',
        borderTop: '1px solid var(--hairline)',
        borderTopLeftRadius: 16, borderTopRightRadius: 16,
        boxShadow: '0 -12px 32px rgba(0,0,0,0.36)',
        maxHeight: '75vh', display: 'flex', flexDirection: 'column',
        transform: enter ? 'translateY(0)' : 'translateY(100%)',
        transition: 'transform .22s cubic-bezier(.2,.7,.2,1)',
        paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)',
      }}>
        <div style={{ display: 'flex', justifyContent: 'center', padding: '10px 0 6px' }}>
          <span aria-hidden="true" style={{
            width: 36, height: 4, borderRadius: 2,
            background: 'var(--hairline-strong)',
          }} />
        </div>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '6px 16px 8px',
        }}>
          <span style={{ fontSize: 'var(--t-h3)', fontWeight: 600, color: 'var(--fg)' }}>{title}</span>
          {subtitle && (
            <span className="mono" style={{
              fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
              textTransform: 'uppercase', letterSpacing: '0.06em',
            }}>{subtitle}</span>
          )}
        </div>
        <div style={{ overflowY: 'auto', WebkitOverflowScrolling: 'touch', flex: 1 }}>
          {children}
        </div>
        <button onClick={() => onClose && onClose()}
          style={{
            margin: '8px 16px 4px', padding: '12px',
            background: 'var(--surface-2)', color: 'var(--fg)',
            border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
            fontSize: 'var(--t-md)', fontFamily: 'inherit', cursor: 'pointer',
            minHeight: 44,
          }}>
          Zamknij
        </button>
      </div>
    </div>
  );
}

function TodosViewer({ open, onClose, anchorRef, compact, state, loading, error }) {
  const todos = (state && Array.isArray(state.todos)) ? state.todos : [];
  const active = todos.filter(t => t.status !== 'completed').length;
  const subtitle = todos.length > 0 ? (active + '/' + todos.length + ' aktywne') : null;
  const body = (
    <>
      {loading && todos.length === 0 && <div style={{ padding: 12 }}><Spinner label="Ładowanie…" inline /></div>}
      {error && <div style={{ padding: 12 }}><StatusBanner variant="err" label={error} inline /></div>}
      {!loading && !error && todos.length === 0 && (
        <div style={{ padding: compact ? '24px 16px' : 12, fontSize: compact ? 'var(--t-md)' : 'var(--t-cap)', color: 'var(--fg-3)', textAlign: compact ? 'center' : 'left' }}>
          Brak aktywnych todos.
        </div>
      )}
      {todos.map((t, i) => <TodoRow key={i} todo={t} compact={compact} />)}
    </>
  );
  if (compact) {
    return <_MobileSheet open={!!open} onClose={onClose} title="Todos" subtitle={subtitle}>{body}</_MobileSheet>;
  }
  return (
    <_DesktopShell open={!!open} onClose={onClose} anchorRef={anchorRef}
      title={'Todos' + (todos.length ? ' (' + todos.length + ')' : '')} subtitle={subtitle}>
      {body}
    </_DesktopShell>
  );
}

function _basename(p) {
  if (!p) return 'plan';
  const parts = String(p).split('/');
  return parts[parts.length - 1] || 'plan';
}

function PlanViewer({ open, onClose, anchorRef, compact, state, loading, error }) {
  const plan = state && state.plan;
  const filename = plan ? _basename(plan.path) : 'plan';
  const mtime = plan && plan.mtime ? window.relTime(Math.floor(plan.mtime)) : null;
  const subtitle = mtime ? ('zmodyfikowano ' + mtime) : null;
  const html = _smUseMemo(() => {
    if (!plan || plan.content == null) return null;
    if (typeof window.safeMd === 'function') return window.safeMd(plan.content);
    return null;
  }, [plan && plan.content]);

  const muted = { fontSize: compact ? 'var(--t-md)' : 'var(--t-cap)', color: 'var(--fg-3)', textAlign: 'center', padding: '16px 0' };
  const body = (
    <div style={{ padding: compact ? '4px 16px 12px' : '4px 8px 8px' }}>
      {loading && !plan && <Spinner label="Ładowanie…" inline />}
      {error && <StatusBanner variant="err" label={error} inline />}
      {!loading && !error && !plan && <div style={muted}>Brak planu dla tej sesji.</div>}
      {plan && plan.content == null && <div style={muted}>Plik plan został usunięty: {plan.path}</div>}
      {plan && plan.content != null && html && (
        <div className="md-render" style={{ fontSize: 'var(--t-sm)', lineHeight: 1.6, color: 'var(--fg)' }}
          dangerouslySetInnerHTML={{ __html: html }} />
      )}
      {plan && plan.content != null && !html && (
        <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 'var(--t-cap)', color: 'var(--fg-2)', margin: 0 }}>{plan.content}</pre>
      )}
    </div>
  );

  if (compact) {
    return <_MobileSheet open={!!open} onClose={onClose} title={filename} subtitle={subtitle}>{body}</_MobileSheet>;
  }
  return (
    <_DesktopShell open={!!open} onClose={onClose} anchorRef={anchorRef}
      title={filename} subtitle={subtitle} maxHeight={560}>
      {body}
    </_DesktopShell>
  );
}

// Header buttons — IconButton + count badge, each owns its own open state.
function _Badge({ count }) {
  return (
    <span aria-hidden="true" style={{
      position: 'absolute', top: 2, right: 2,
      minWidth: 14, height: 14, padding: '0 3px',
      borderRadius: 7, background: 'var(--accent)', color: 'var(--bg)',
      fontSize: 9, fontWeight: 700, lineHeight: '14px', textAlign: 'center',
      pointerEvents: 'none',
    }}>{count}</span>
  );
}

// Refetch /state when this button's modal opens (false→true), so users see
// the latest todos/plan even if claude updated them in the chat since the
// last load. Uses a ref to detect the edge.
function _useReloadOnOpen(open, reload) {
  const prevOpenRef = _smUseRef(false);
  _smUseEffect(() => {
    if (open && !prevOpenRef.current) {
      try { reload(); } catch (e) { /* ignore */ }
    }
    prevOpenRef.current = !!open;
  }, [open, reload]);
}

function PlanButton({ compact, state, loading, error, reload }) {
  const [open, setOpen] = _smUseState(false);
  const anchorRef = _smUseRef(null);
  _useReloadOnOpen(open, reload);
  const plan = state && state.plan;
  if (!plan) return null;
  return (
    <div ref={anchorRef} style={{ position: 'relative', display: 'inline-flex' }}>
      <window.IconButton icon="notepad" size={32}
        label={open ? 'Schowaj plan' : 'Pokaż plan'}
        onClick={() => setOpen(o => !o)}
        style={{ color: open ? 'var(--accent)' : undefined }} />
      <_Badge count={1} />
      <PlanViewer open={open} onClose={() => setOpen(false)}
        anchorRef={anchorRef} compact={compact}
        state={state} loading={loading} error={error} />
    </div>
  );
}

function TodosButton({ compact, state, loading, error, reload }) {
  const [open, setOpen] = _smUseState(false);
  const anchorRef = _smUseRef(null);
  _useReloadOnOpen(open, reload);
  const todos = (state && Array.isArray(state.todos)) ? state.todos : [];
  if (todos.length === 0) return null;
  const active = todos.filter(t => t.status !== 'completed').length;
  return (
    <div ref={anchorRef} style={{ position: 'relative', display: 'inline-flex' }}>
      <window.IconButton icon="list-checks" size={32}
        label={open ? 'Schowaj todos' : 'Pokaż todos (' + active + ')'}
        onClick={() => setOpen(o => !o)}
        style={{ color: open ? 'var(--accent)' : undefined }} />
      {active > 0 && <_Badge count={active} />}
      <TodosViewer open={open} onClose={() => setOpen(false)}
        anchorRef={anchorRef} compact={compact}
        state={state} loading={loading} error={error} />
    </div>
  );
}

function StateModalButtons({ compact, sessionId }) {
  // Single hook instance per session — children consume `state` as props.
  // Previously each button + each viewer hooked independently (4 fetches
  // per mount); now it's one shared fetch + per-modal `reload()` on open.
  const { state, loading, error, reload } = useSessionState({ sessionId });
  if (!sessionId) return null;
  return (
    <>
      <PlanButton compact={compact} state={state} loading={loading} error={error} reload={reload} />
      <TodosButton compact={compact} state={state} loading={loading} error={error} reload={reload} />
    </>
  );
}

Object.assign(window, { useSessionState, TodosViewer, PlanViewer, StateModalButtons });
