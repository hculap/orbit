// Per-session model selector for the Orchestrator header.
// IconButton (cpu) opens a modal/sheet with radio options:
//   • Domyślny (no `--model` flag — claude-cli picks its built-in default)
//   • Opus / Sonnet / Haiku (passed verbatim as `--model <alias>`)
// Selecting a row PATCHes /api/orchestrator/sessions/{id}/model and dismisses
// the popover. The active model is reflected by the icon's accent colour and
// a tiny mono badge (e.g. "OPUS") under the button so the user can see the
// current routing at a glance.

const {
  useEffect: _mmUseEffect,
  useRef: _mmUseRef,
  useState: _mmUseState,
} = React;

const _MODEL_OPTIONS = [
  { value: null, label: 'Domyślny', sub: 'co claude-cli ma ustawione' },
  { value: 'opus', label: 'Opus', sub: 'najgłębsze rozumowanie' },
  { value: 'sonnet', label: 'Sonnet', sub: 'najlepszy do kodu' },
  { value: 'haiku', label: 'Haiku', sub: 'tani i szybki' },
];

function _shortLabel(model) {
  if (!model) return null;
  if (model === 'opus') return 'OPUS';
  if (model === 'sonnet') return 'SNT';
  if (model === 'haiku') return 'HKU';
  return model.toUpperCase().slice(0, 4);
}

// Delegates to the global Popover (anchor-relative, fixed-positioned, with
// outside-click + Esc + touchstart dismiss). Header + children preserved.
function _DesktopPopover({ open, onClose, anchorRef, children }) {
  return (
    <window.Popover open={open} onClose={onClose} anchorRef={anchorRef} placement="bottom-end" minWidth={260} maxWidth={320}>
      <div role="dialog" aria-label="Wybierz model">
        <div className="mono" style={{
          fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase',
          letterSpacing: '0.08em', padding: '6px 8px',
        }}>Model dla tej sesji</div>
        {children}
      </div>
    </window.Popover>
  );
}

function _MobileSheet({ open, onClose, children }) {
  const [mounted, setMounted] = _mmUseState(false);
  const [enter, setEnter] = _mmUseState(false);
  _mmUseEffect(() => {
    if (open) {
      setMounted(true);
      const id = requestAnimationFrame(() => setEnter(true));
      return () => cancelAnimationFrame(id);
    }
    setEnter(false);
    const t = setTimeout(() => setMounted(false), 220);
    return () => clearTimeout(t);
  }, [open]);
  _mmUseEffect(() => {
    if (!open) return undefined;
    const onEsc = (e) => { if (e.key === 'Escape') onClose && onClose(); };
    document.addEventListener('keydown', onEsc);
    return () => document.removeEventListener('keydown', onEsc);
  }, [open, onClose]);
  _mmUseEffect(() => {
    if (!open) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [open]);
  if (!mounted) return null;
  return (
    <div role="dialog" aria-label="Wybierz model"
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
        <div style={{ padding: '6px 16px 10px' }}>
          <span style={{ fontSize: 'var(--t-h3)', fontWeight: 600, color: 'var(--fg)' }}>Model dla tej sesji</span>
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
          }}>Zamknij</button>
      </div>
    </div>
  );
}

function _ModelRow({ option, selected, busy, compact, onPick }) {
  const radius = 999;
  const sizeOuter = compact ? 22 : 18;
  const sizeInner = compact ? 12 : 9;
  return (
    <button onClick={() => onPick(option.value)}
      disabled={busy}
      style={{
        display: 'flex', alignItems: 'center', gap: 12,
        width: '100%', textAlign: 'left',
        padding: compact ? '14px 16px' : '10px 10px',
        background: selected ? 'var(--accent-soft)' : 'transparent',
        border: 'none', borderRadius: 'var(--r-control)',
        color: 'var(--fg)', cursor: busy ? 'progress' : 'pointer',
        opacity: busy ? 0.7 : 1,
        fontFamily: 'inherit',
        minHeight: compact ? 56 : 0,
      }}
      onMouseEnter={(e) => { if (!compact && !selected && !busy) e.currentTarget.style.background = 'var(--surface-2)'; }}
      onMouseLeave={(e) => { if (!compact && !selected && !busy) e.currentTarget.style.background = 'transparent'; }}>
      <span aria-hidden="true" style={{
        width: sizeOuter, height: sizeOuter, borderRadius: radius,
        border: '2px solid ' + (selected ? 'var(--accent)' : 'var(--fg-3)'),
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        flexShrink: 0,
      }}>
        {selected && (
          <span style={{
            width: sizeInner, height: sizeInner, borderRadius: radius,
            background: 'var(--accent)',
          }} />
        )}
      </span>
      <span style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
        <span style={{ fontSize: compact ? 'var(--t-body)' : 'var(--t-sm)', fontWeight: 500 }}>{option.label}</span>
        <span style={{ fontSize: compact ? 'var(--t-cap)' : 'var(--t-xs)', color: 'var(--fg-3)' }}>{option.sub}</span>
      </span>
    </button>
  );
}

function _ModelOptionList({ current, busy, compact, onPick }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, padding: compact ? '0 8px 8px' : '0 0 4px' }}>
      {_MODEL_OPTIONS.map((opt) => (
        <_ModelRow key={String(opt.value)}
          option={opt} selected={current === opt.value}
          busy={busy} compact={compact} onPick={onPick} />
      ))}
    </div>
  );
}

// ModelViewer — popover/sheet (no trigger). Owns the PATCH lifecycle so any
// caller (header IconButton, kebab item) can drive it via open/onClose props.
function ModelViewer({ open, onClose, anchorRef, compact, sessionId, model, onChanged }) {
  const [busy, setBusy] = _mmUseState(false);
  const current = model || null;

  const onPick = async (next) => {
    if (busy || !sessionId) return;
    if (next === current) { onClose && onClose(); return; }
    setBusy(true);
    try {
      const url = window.apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/model');
      const r = await fetch(url, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: next }),
      });
      if (!r.ok) {
        const detail = await r.text().catch(() => '');
        throw new Error('PATCH /model → ' + r.status + (detail ? ': ' + detail : ''));
      }
      const data = await r.json().catch(() => ({}));
      onChanged && onChanged(data && data.model != null ? data.model : null);
      onClose && onClose();
    } catch (e) {
      console.error('model PATCH failed:', e);
      try { window.alert && window.alert('Nie udało się zmienić modelu: ' + (e && e.message ? e.message : 'unknown')); }
      catch (_) { /* noop */ }
    } finally {
      setBusy(false);
    }
  };

  const list = <_ModelOptionList current={current} busy={busy} compact={compact} onPick={onPick} />;
  if (compact) {
    return <_MobileSheet open={!!open} onClose={onClose}>{list}</_MobileSheet>;
  }
  return (
    <_DesktopPopover open={!!open} onClose={onClose} anchorRef={anchorRef}>{list}</_DesktopPopover>
  );
}

function ModelButton({ compact, sessionId, model, onChanged }) {
  const [open, setOpen] = _mmUseState(false);
  const anchorRef = _mmUseRef(null);
  if (!sessionId) return null;
  const current = model || null;
  const short = _shortLabel(current);

  return (
    <div ref={anchorRef} style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
      <window.IconButton icon="cpu" size={32}
        label={current ? ('Model: ' + current) : 'Model: domyślny'}
        onClick={() => setOpen((o) => !o)}
        style={{ color: current ? 'var(--accent)' : undefined }} />
      {short && (
        <span aria-hidden="true" className="mono" style={{
          position: 'absolute', bottom: -2, right: -2,
          padding: '0 4px', borderRadius: 4,
          background: 'var(--accent)', color: 'var(--bg)',
          fontSize: 9, fontWeight: 700, lineHeight: '12px',
          letterSpacing: '0.04em', pointerEvents: 'none',
        }}>{short}</span>
      )}
      <ModelViewer
        open={open} onClose={() => setOpen(false)} anchorRef={anchorRef}
        compact={compact} sessionId={sessionId} model={current} onChanged={onChanged} />
    </div>
  );
}

Object.assign(window, { ModelButton, ModelViewer });
