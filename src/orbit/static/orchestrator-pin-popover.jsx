// Pinned-messages popover for the Orchestrator header.
//
// Two layouts driven by the `compact` prop:
//
//   compact=false  → floating popover anchored to the trigger badge (desktop)
//   compact=true   → bottom-sheet slide-up with backdrop dim (mobile / PWA)
//
// Both let the user jump to a pinned message (smooth-scroll + brief outline
// flash on the bubble) and unpin inline. The compact variant renders 44+px
// touch targets, respects iOS safe-area-inset-bottom, animates with a 220ms
// translateY transition, and dismisses on backdrop tap or Esc.

const { useEffect: _ppUseEffect, useRef: _ppUseRef, useState: _ppUseState } = React;

// Trigger a temporary outline pulse on the target bubble. Uses inline style +
// setTimeout to avoid a CSS dependency.
function _flashBubble(el) {
  if (!el) return;
  const prevOutline = el.style.outline;
  const prevOffset = el.style.outlineOffset;
  const prevTransition = el.style.transition;
  el.style.transition = 'outline-color .4s ease, outline-width .4s ease';
  el.style.outline = '2px solid var(--accent)';
  el.style.outlineOffset = '4px';
  setTimeout(() => {
    el.style.outline = prevOutline || '';
    el.style.outlineOffset = prevOffset || '';
    setTimeout(() => { el.style.transition = prevTransition || ''; }, 400);
  }, 1200);
}

// Build the visible row list once per render — turn_idxs missing from the
// loaded `messages` array (e.g. legacy session before reload completes) are
// silently skipped rather than rendered as blank rows.
function _buildItems(activeSession, messages) {
  const turns = (activeSession && activeSession.pinned_turn_idxs) || [];
  return turns
    .map((turnIdx) => {
      const m = (messages || []).find((x) => x && x.turn_idx === turnIdx);
      return m ? { turnIdx, msg: m } : null;
    })
    .filter(Boolean);
}

function _previewOf(msg) {
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
  const text = blocks.map((b) => (b && (b.text || b.content)) || '').join(' ');
  return (typeof window.previewText === 'function')
    ? window.previewText(text, 80)
    : (text.length > 80 ? text.slice(0, 79) + '…' : text);
}

function _handleJump(msg, msgKey, onClose) {
  const k = typeof msgKey === 'function' ? msgKey(msg) : null;
  if (!k) return;
  const el = document.getElementById('msg-' + k);
  if (el) {
    try { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    catch (e) { el.scrollIntoView(); }
    _flashBubble(el);
  }
  onClose && onClose();
}

// PinRow — shared between desktop popover and mobile sheet. The padding +
// font sizes adapt to compact mode.
function PinRow({ turnIdx, msg, msgKey, togglePin, onClose, compact }) {
  const isAgent = msg.role === 'assistant' || msg.role === 'tool_result';
  const preview = _previewOf(msg) || '(brak treści)';
  const padV = compact ? 14 : 8;
  const fontTitle = compact ? 'var(--t-xs)' : 'var(--t-2xs)';
  const fontBody = compact ? 'var(--t-md)' : 'var(--t-cap)';
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      padding: padV + 'px 8px',
      borderTop: compact ? '1px solid var(--hairline)' : 'none',
    }}
      onMouseEnter={(e) => { if (!compact) e.currentTarget.style.background = 'var(--surface-2)'; }}
      onMouseLeave={(e) => { if (!compact) e.currentTarget.style.background = 'transparent'; }}>
      <button onClick={() => _handleJump(msg, msgKey, onClose)}
        title="Przejdź do wiadomości"
        style={{
          flex: 1, minWidth: 0, textAlign: 'left', background: 'transparent',
          border: 'none', color: 'var(--fg)', cursor: 'pointer',
          fontSize: fontBody, lineHeight: 1.4, padding: 0,
          display: 'flex', flexDirection: 'column', gap: 2,
          minHeight: compact ? 32 : 0,
        }}>
        <span className="mono" style={{
          fontSize: fontTitle, color: isAgent ? 'var(--accent)' : 'var(--fg-3)',
          textTransform: 'uppercase', letterSpacing: '0.06em',
        }}>
          {isAgent ? 'orchestrator' : 'you'} · #{turnIdx}
        </span>
        <span style={{
          color: 'var(--fg-2)', overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {preview}
        </span>
      </button>
      <button onClick={(e) => { e.stopPropagation(); togglePin && togglePin(msg); }}
        aria-label="Odepnij" title="Odepnij"
        style={{
          background: 'transparent', border: 'none', color: 'var(--fg-3)',
          cursor: 'pointer', padding: compact ? 12 : 4, flexShrink: 0,
          minWidth: compact ? 44 : 0, minHeight: compact ? 44 : 0,
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          borderRadius: 'var(--r-control)',
        }}>
        <Icon name="close" size={compact ? 18 : 14} stroke={1.8} />
      </button>
    </div>
  );
}

// Delegates to the global Popover (anchor-relative, fixed-positioned, outside-
// click + Esc + touchstart dismiss). Pinned-message menu content preserved.
function DesktopPopover({ open, onClose, anchorRef, items, msgKey, togglePin }) {
  const safeItems = Array.isArray(items) ? items : [];
  return (
    <window.Popover open={open} onClose={onClose} anchorRef={anchorRef} placement="bottom-end" minWidth={280} maxWidth={360} maxHeight={360}>
      <div role="dialog" aria-label="Przypięte wiadomości">
        <div className="mono" style={{
          fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase',
          letterSpacing: '0.08em', padding: '6px 8px',
        }}>
          Przypięte ({safeItems.length})
        </div>
        {safeItems.length === 0 && (
          <EmptyState message="Brak przypiętych wiadomości." padded />
        )}
        {safeItems.map(({ turnIdx, msg }) => (
          <PinRow key={turnIdx} turnIdx={turnIdx} msg={msg}
            msgKey={msgKey} togglePin={togglePin} onClose={onClose} compact={false} />
        ))}
      </div>
    </window.Popover>
  );
}

function PinSheet({ open, onClose, items, msgKey, togglePin }) {
  const safeItems = Array.isArray(items) ? items : [];
  // Two-stage mount so the slide-up transition has a frame to register the
  // "from" position before flipping to the "to" position. `mounted` controls
  // whether we render the DOM at all (so we can wait out the close animation
  // before unmounting); `enter` toggles the translateY between 100% and 0.
  const [mounted, setMounted] = _ppUseState(false);
  const [enter, setEnter] = _ppUseState(false);
  _ppUseEffect(() => {
    if (open) {
      setMounted(true);
      // Defer the enter flip to the next frame so the browser sees the
      // initial translateY(100%) before the transition target is applied.
      const id = requestAnimationFrame(() => setEnter(true));
      return () => cancelAnimationFrame(id);
    }
    setEnter(false);
    const t = setTimeout(() => setMounted(false), 220);
    return () => clearTimeout(t);
  }, [open]);
  _ppUseEffect(() => {
    if (!open) return undefined;
    const onEsc = (e) => { if (e.key === 'Escape') onClose && onClose(); };
    document.addEventListener('keydown', onEsc);
    return () => document.removeEventListener('keydown', onEsc);
  }, [open, onClose]);
  // Lock body scroll while the sheet is up so iOS rubber-banding doesn't
  // fight the inner scroll container.
  _ppUseEffect(() => {
    if (!open) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [open]);
  if (!mounted) return null;
  return (
    <div role="dialog" aria-label="Przypięte wiadomości"
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        display: 'flex', flexDirection: 'column', justifyContent: 'flex-end',
        pointerEvents: 'auto',
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
        <div style={{
          display: 'flex', justifyContent: 'center', padding: '10px 0 6px',
        }}>
          <span aria-hidden="true" style={{
            width: 36, height: 4, borderRadius: 2,
            background: 'var(--hairline-strong)',
          }} />
        </div>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '6px 16px 8px',
        }}>
          <span style={{ fontSize: 'var(--t-h3)', fontWeight: 600, color: 'var(--fg)' }}>
            Przypięte
          </span>
          <span className="mono" style={{
            fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
            textTransform: 'uppercase', letterSpacing: '0.06em',
          }}>
            {safeItems.length} {safeItems.length === 1 ? 'msg' : 'msgs'}
          </span>
        </div>
        <div style={{ overflowY: 'auto', WebkitOverflowScrolling: 'touch' }}>
          {safeItems.length === 0 && (
            <EmptyState message="Brak przypiętych wiadomości." centered padded />
          )}
          {safeItems.map(({ turnIdx, msg }) => (
            <PinRow key={turnIdx} turnIdx={turnIdx} msg={msg}
              msgKey={msgKey} togglePin={togglePin} onClose={onClose} compact={true} />
          ))}
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

function PinPopover({
  open, onClose, anchorRef,
  activeSession, messages, msgKey, togglePin, compact,
}) {
  const items = _buildItems(activeSession, messages);
  if (compact) {
    return (
      <PinSheet open={!!open} onClose={onClose}
        items={items} msgKey={msgKey} togglePin={togglePin} />
    );
  }
  return (
    <DesktopPopover open={!!open} onClose={onClose} anchorRef={anchorRef}
      items={items} msgKey={msgKey} togglePin={togglePin} />
  );
}

Object.assign(window, { PinPopover });
