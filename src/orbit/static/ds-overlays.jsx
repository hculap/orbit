// ds-overlays.jsx — design-system v2 overlays + banners + dialog helpers.
//
// Loads after components.jsx / ds-layout / ds-forms, before consumers. Publishes
// to window. Provides the GLOBAL Popover (none existed — 5 hand-rolled copies),
// the promoted StatusBanner/Alert (~25 bespoke banner divs), and the
// ModalFooter / ConfirmModal helpers (~7 re-coded modal footers). Tokens only.

const { useState: _dsoUseState, useEffect: _dsoUseEffect, useRef: _dsoUseRef, useLayoutEffect: _dsoUseLayout } = React;
const _dsoLayout = _dsoUseLayout || _dsoUseEffect;

// ─────────────────────────────────────────────────────────────
// Popover — anchor-relative desktop dropdown. Outside-click + Esc dismiss,
// repositions on scroll/resize. Fixed-positioned at --z-dropdown.
// Replaces DesktopPopover (pin), _DesktopPopover (state/model modals),
// link-picker popover, the filter-chip dropdown.
// ─────────────────────────────────────────────────────────────
function Popover({ open, onClose, anchorRef, placement = 'bottom-end', minWidth = 180, maxWidth, maxHeight, children, style }) {
  const ref = _dsoUseRef(null);
  const [pos, setPos] = _dsoUseState(null);
  // Keep onClose in a ref so the dismiss effect's deps don't include it — callers
  // pass an inline arrow (onClose={() => setOpen(false)}) whose identity changes
  // every parent render, which would otherwise tear down + re-add the document
  // listeners on each render.
  const onCloseRef = _dsoUseRef(onClose); onCloseRef.current = onClose;

  _dsoLayout(() => {
    if (!open || !anchorRef || !anchorRef.current) { setPos(null); return; }
    const place = () => {
      const r = anchorRef.current.getBoundingClientRect();
      const top = r.bottom + 4;
      const right = placement === 'bottom-start' ? undefined : (window.innerWidth - r.right);
      const left = placement === 'bottom-start' ? r.left : undefined;
      setPos({ top, left, right });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open, anchorRef, placement]);

  _dsoUseEffect(() => {
    if (!open) return;
    const close = () => { if (onCloseRef.current) onCloseRef.current(); };
    const onDoc = (e) => {
      if (ref.current && ref.current.contains(e.target)) return;
      if (anchorRef && anchorRef.current && anchorRef.current.contains(e.target)) return;
      close();
    };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('touchstart', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('touchstart', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, anchorRef]);

  if (!open || !pos) return null;
  return (
    <div ref={ref} className="fade-in" style={{
      position: 'fixed', top: pos.top, left: pos.left, right: pos.right,
      zIndex: 'var(--z-dropdown)', minWidth, maxWidth,
      maxHeight: maxHeight || '70vh', overflowY: 'auto',
      background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
      borderRadius: 'var(--r-md)', boxShadow: 'var(--shadow-pop)', padding: 4,
      borderTop: '1px solid transparent',
      backgroundImage: 'var(--grad-cosmic-line)',
      backgroundRepeat: 'no-repeat', backgroundSize: '100% 1px', backgroundPosition: 'top',
      ...style,
    }}>{children}</div>
  );
}

// ─────────────────────────────────────────────────────────────
// StatusBanner / Alert — promoted from HubSettings.StatusBanner to global.
// Back-compat: legacy `{color, label, action}` (surface-2 bg + colored border +
// dot) still works. New: `variant='ok'|'warn'|'err'|'info'` derives the color
// family; `inline` uses the soft fill for inline message strips; `sub` adds a
// second line.
// ─────────────────────────────────────────────────────────────
const _BANNER = {
  ok:   { main: 'var(--ok)',   soft: 'var(--ok-soft)',   line: 'var(--ok-line)',   icon: 'check' },
  warn: { main: 'var(--warn)', soft: 'var(--warn-soft)', line: 'var(--warn-line)', icon: 'flag' },
  err:  { main: 'var(--err)',  soft: 'var(--err-soft)',  line: 'var(--err-line)',  icon: 'close' },
  info: { main: 'var(--info)', soft: 'var(--info-soft)', line: 'var(--info-line)', icon: 'help' },
};
function StatusBanner({ variant, color, label, sub, action, inline = false, icon, style }) {
  const v = variant ? _BANNER[variant] : null;
  const main = color || (v ? v.main : 'var(--fg-3)');
  const bg = inline && v ? v.soft : 'var(--surface-2)';
  const line = v ? v.line : main;
  const Ico = window.Icon;
  const glyphName = icon || (v ? v.icon : null);
  return (
    <div style={{
      display: 'flex', alignItems: sub ? 'flex-start' : 'center', gap: 10,
      padding: '8px 12px', background: bg, border: '1px solid ' + line,
      borderRadius: 'var(--r-control)', ...style,
    }}>
      {inline && glyphName && Ico
        ? <Ico name={glyphName} size={15} color={main} style={{ flexShrink: 0, marginTop: sub ? 2 : 0 }} />
        : <span style={{ width: 10, height: 10, borderRadius: 'var(--r-pill)', background: main, flexShrink: 0, marginTop: sub ? 4 : 0 }} />}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: sub ? 'normal' : 'nowrap', lineHeight: 1.45 }}>{label}</div>
        {sub && <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', marginTop: 2, lineHeight: 1.45 }}>{sub}</div>}
      </div>
      {action}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// ModalFooter — right-aligned Cancel/Confirm row for Modal/BottomSheet.
// Stops the 18px-pad / 14px-gap / justify-end footer being re-coded ~7x.
// ─────────────────────────────────────────────────────────────
function ModalFooter({ onCancel, onConfirm, confirmLabel = 'Confirm', cancelLabel = 'Cancel', variant = 'primary', busy = false, confirmDisabled = false, left, style }) {
  const Btn = window.Button;
  // Primary confirm reads as the signature cosmic CTA (gradient fill + glow);
  // danger/other variants keep their semantic Button styling untouched.
  const cosmicConfirmStyle = (variant === 'primary' && !(confirmDisabled || busy))
    ? { background: 'var(--grad-cosmic)', border: '1px solid transparent', boxShadow: 'var(--glow-accent)' }
    : undefined;
  return (
    <div style={{
      display: 'flex', alignItems: 'center',
      justifyContent: left ? 'space-between' : 'flex-end',
      gap: 8, padding: '14px 18px', borderTop: '1px solid var(--hairline)',
      backgroundImage: 'var(--grad-cosmic-line)',
      backgroundRepeat: 'no-repeat', backgroundSize: '100% 1px', backgroundPosition: 'top',
      ...style,
    }}>
      {left && <div>{left}</div>}
      <div style={{ display: 'flex', gap: 8 }}>
        {onCancel && (Btn ? <Btn variant="quiet" onClick={onCancel}>{cancelLabel}</Btn>
          : <button onClick={onCancel}>{cancelLabel}</button>)}
        {onConfirm && (Btn ? <Btn variant={variant} onClick={onConfirm} busy={busy} disabled={confirmDisabled} style={cosmicConfirmStyle}>{confirmLabel}</Btn>
          : <button onClick={onConfirm} disabled={confirmDisabled || busy}>{confirmLabel}</button>)}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// ConfirmModal — compound dialog: title + message (+ optional children, e.g. a
// rename Input) + Cancel/Confirm footer. Replaces ~6 near-identical confirm
// bodies across orchestrator / library / scheduler.
// ─────────────────────────────────────────────────────────────
function ConfirmModal({ open, onClose, title, message, children, confirmLabel = 'Confirm', cancelLabel = 'Cancel', onConfirm, variant = 'default', busy = false, confirmDisabled = false }) {
  const Modal = window.Modal;
  if (!Modal) return null;
  return (
    <Modal open={open} onClose={onClose} title={title} width={440}>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        {(message || children) && (
          <div style={{ padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 12 }}>
            {message && <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg-2)', lineHeight: 1.55 }}>{message}</div>}
            {children}
          </div>
        )}
        <ModalFooter
          onCancel={onClose}
          onConfirm={() => onConfirm && onConfirm()}
          confirmLabel={confirmLabel} cancelLabel={cancelLabel}
          variant={variant === 'danger' ? 'danger' : 'primary'}
          busy={busy} confirmDisabled={confirmDisabled}
        />
      </div>
    </Modal>
  );
}

// ─────────────────────────────────────────────────────────────
Object.assign(window, { Popover, StatusBanner, ModalFooter, ConfirmModal });
