// ds-layout.jsx — design-system v2 layout helpers + simple atoms.
//
// Part of the reusable UI library introduced on feature/design-system. Loads
// right after components.jsx and BEFORE settings-primitives.jsx and every
// consumer (no bundler — order is set in templates/index.html). Every component
// publishes to window so sibling JSX files pick it up. Styling uses the tokens
// in tokens.css v2 — no hardcoded colors/spacing/radii here.
//
// These are the "~1900 inline style blocks reinvent this" helpers from the
// audit: Stack/Inline (flex), PageScaffold (page padding + nav clearance),
// Field/ListRow (form + data rows), EmptyState/Spinner/Avatar/SectionLabel/
// Divider (atoms). See docs/design-system/03-design-system-spec.md.

const { useState: _dslUseState } = React;

// ─────────────────────────────────────────────────────────────
// Stack — vertical flex with token gap. The flex-column the codebase
// reaches for constantly (`display:flex; flexDirection:column; gap:N`).
// ─────────────────────────────────────────────────────────────
function Stack({ gap = 12, align, justify, flex, children, className, style, onClick }) {
  return (
    <div onClick={onClick} className={className} style={{
      display: 'flex', flexDirection: 'column', gap,
      alignItems: align, justifyContent: justify,
      ...(flex != null ? { flex, minHeight: 0 } : null),
      ...style,
    }}>{children}</div>
  );
}

// ─────────────────────────────────────────────────────────────
// Inline — horizontal flex with gap; wraps optionally.
// ─────────────────────────────────────────────────────────────
function Inline({ gap = 8, align = 'center', justify, wrap = false, flex, children, className, style, onClick }) {
  return (
    <div onClick={onClick} className={className} style={{
      display: 'flex', flexDirection: 'row', gap,
      alignItems: align, justifyContent: justify,
      flexWrap: wrap ? 'wrap' : 'nowrap',
      ...(flex != null ? { flex, minWidth: 0 } : null),
      ...style,
    }}>{children}</div>
  );
}

// ─────────────────────────────────────────────────────────────
// Divider — a hairline rule (horizontal default, vertical opt-in).
// ─────────────────────────────────────────────────────────────
function Divider({ vertical = false, strong = false, style }) {
  const c = strong ? 'var(--hairline-strong)' : 'var(--hairline)';
  return <div aria-hidden="true" style={vertical
    ? { width: 1, alignSelf: 'stretch', background: c, flexShrink: 0, ...style }
    : { height: 1, width: '100%', background: c, ...style }} />;
}

// ─────────────────────────────────────────────────────────────
// SectionLabel / Eyebrow — uppercase mono label. Standardizes the ~25 inline
// "uppercase mono fg-3" headers (nav groups, intra-card section headers).
// ─────────────────────────────────────────────────────────────
function SectionLabel({ children, spacing = 'default', color = 'var(--fg-3)', style }) {
  return (
    <div className="mono" style={{
      fontSize: 'var(--t-2xs)', color, textTransform: 'uppercase',
      letterSpacing: spacing === 'tight' ? '0.06em' : '0.08em',
      fontWeight: 500, ...style,
    }}>{children}</div>
  );
}

// ─────────────────────────────────────────────────────────────
// Spinner — the one loading affordance: a rotating Icon + optional caption.
// Replaces ~15 ad-hoc "loading… / ładuję…" idioms.
// ─────────────────────────────────────────────────────────────
function Spinner({ label, size = 16, inline = false, color = 'var(--fg-3)', style }) {
  const Ico = window.Icon;
  const glyph = Ico
    ? <span className="hub-spin" style={{ display: 'inline-flex' }}><Ico name="spinner" size={size} color={color} /></span>
    : null;
  if (inline) {
    return <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color, ...style }}>{glyph}{label && <span className="mono" style={{ fontSize: 'var(--t-xs)' }}>{label}</span>}</span>;
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10, padding: 16, color, ...style }}>
      {glyph}
      {label && <span className="mono" style={{ fontSize: 'var(--t-sm)' }}>{label}</span>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// EmptyState — zero-state: optional icon + message + sub + action.
// Lighter than ComingSoonView. Replaces ~40 bare empty <div>s.
// ─────────────────────────────────────────────────────────────
function EmptyState({ icon, message, sub, action, centered = true, padded = true, style }) {
  const Ico = window.Icon;
  return (
    <div style={{
      display: 'flex', flexDirection: 'column',
      alignItems: centered ? 'center' : 'flex-start',
      textAlign: centered ? 'center' : 'left',
      gap: 8, color: 'var(--fg-3)',
      padding: padded ? '40px 24px' : '14px 0',
      ...style,
    }}>
      {icon && Ico && (
        <div style={{
          width: 44, height: 44, borderRadius: 'var(--r-widget)',
          background: 'var(--grad-cosmic-soft)', border: '1px solid var(--accent-line)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--accent)', marginBottom: 2,
          boxShadow: 'var(--glow-soft)',
        }}><Ico name={icon} size={20} /></div>
      )}
      {message && <div style={{ fontSize: 'var(--t-md)', fontWeight: 500, color: 'var(--fg-2)' }}>{message}</div>}
      {sub && <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', lineHeight: 1.5, maxWidth: 420 }}>{sub}</div>}
      {action && <div style={{ marginTop: 6 }}>{action}</div>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Avatar — rounded monogram/emoji/icon square or circle. accent-soft bg +
// matching line border. Covers the sidebar brand monogram + agent/skill avatars.
// ─────────────────────────────────────────────────────────────
const _AV_SIZE = { sm: 28, md: 40, lg: 56 };
const _AV_COLOR = {
  accent: { bg: 'var(--accent-soft)', bd: 'var(--accent-line)', fg: 'var(--accent)' },
  ok:     { bg: 'var(--ok-soft)',     bd: 'var(--ok-line)',     fg: 'var(--ok)' },
  warn:   { bg: 'var(--warn-soft)',   bd: 'var(--warn-line)',   fg: 'var(--warn)' },
  err:    { bg: 'var(--err-soft)',    bd: 'var(--err-line)',    fg: 'var(--err)' },
  info:   { bg: 'var(--info-soft)',   bd: 'var(--info-line)',   fg: 'var(--info)' },
  muted:  { bg: 'var(--surface-2)',   bd: 'var(--hairline)',    fg: 'var(--fg-2)' },
};
function Avatar({ icon, initials, emoji, size = 'md', shape = 'square', color = 'accent', statusDot, ring = false, style }) {
  const px = typeof size === 'number' ? size : (_AV_SIZE[size] || 40);
  const c = _AV_COLOR[color] || _AV_COLOR.accent;
  const Ico = window.Icon;
  return (
    <div style={{ position: 'relative', flexShrink: 0, width: px, height: px, ...style }}>
      <div
        className={ring ? ('orbit-gradient-border' + (ring === 'animated' ? ' is-animated' : '')) : undefined}
        style={{
        width: px, height: px,
        borderRadius: shape === 'circle' ? 'var(--r-pill)' : 'var(--r-md)',
        color: c.fg,
        ...(ring
          ? { '--gb-bg': c.bg, border: '1px solid transparent', boxShadow: 'var(--glow-soft)' }
          : { background: c.bg, border: '1px solid ' + c.bd }),
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: Math.round(px * 0.42), fontWeight: 500, overflow: 'hidden',
      }}>
        {emoji ? <span style={{ fontSize: Math.round(px * 0.5) }}>{emoji}</span>
          : icon && Ico ? <Ico name={icon} size={Math.round(px * 0.5)} />
          : <span style={{ letterSpacing: '-0.02em' }}>{(initials || '').slice(0, 2)}</span>}
      </div>
      {statusDot && (
        <span style={{
          position: 'absolute', right: -1, bottom: -1,
          width: Math.max(8, Math.round(px * 0.22)), height: Math.max(8, Math.round(px * 0.22)),
          borderRadius: 'var(--r-pill)', background: statusDot === true ? 'var(--ok)' : statusDot,
          border: '2px solid var(--surface-1)',
        }} />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Field — form-field layout atom: label + control + optional hint/error.
// Promotes/unifies HubSettings.FieldRow to global; fixes the 13-vs-14 label drift.
// ─────────────────────────────────────────────────────────────
function Field({ label, hint, error, required, layout = 'col', htmlFor, children, style }) {
  const labelEl = label && (
    <label htmlFor={htmlFor} style={{ fontSize: 'var(--t-md)', fontWeight: 500, color: 'var(--fg)', display: 'block' }}>
      {label}{required && <span style={{ color: 'var(--err)', marginLeft: 4 }}>*</span>}
    </label>
  );
  const meta = (error || hint) && (
    <div className="mono" style={{ fontSize: 'var(--t-xs)', color: error ? 'var(--err)' : 'var(--fg-3)', marginTop: 2, lineHeight: 1.45 }}>
      {error || hint}
    </div>
  );
  if (layout === 'row') {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', ...style }}>
        <div style={{ flex: '1 1 220px', minWidth: 0 }}>{labelEl}{meta}</div>
        <div>{children}</div>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--field-gap)', ...style }}>
      {labelEl}
      {children}
      {meta}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// ListRow — the canonical data row: leading + (title + mono sub) + spacer +
// trailing, optional top divider, hover/active states, compact tap-target.
// Replaces ~30 structurally-identical rows (services/peers/tmux/sessions/files…).
// ─────────────────────────────────────────────────────────────
function ListRow({ leading, title, sub, trailing, onClick, divider = false, compact = false, active = false, className, style }) {
  const [h, setH] = _dslUseState(false);
  const interactive = !!onClick;
  return (
    <div
      onClick={onClick}
      onMouseEnter={interactive ? () => setH(true) : undefined}
      onMouseLeave={interactive ? () => setH(false) : undefined}
      className={className}
      style={{
        display: 'flex', alignItems: 'center', gap: 12,
        padding: '10px 14px',
        minHeight: compact ? 'var(--control-h-touch)' : undefined,
        borderTop: divider ? '1px solid var(--hairline)' : undefined,
        ...(active ? {
          borderLeft: '3px solid transparent',
          borderImageSource: 'var(--grad-cosmic)',
          borderImageSlice: 1,
          borderImageWidth: '0 0 0 3px',
          paddingLeft: 11, // 14 − 3px indicator, so content doesn't shift vs inactive rows
        } : null),
        background: active ? 'var(--accent-soft)' : (h ? 'var(--surface-2)' : 'transparent'),
        boxShadow: active ? 'var(--glow-soft)' : undefined,
        cursor: interactive ? 'pointer' : 'default',
        transition: 'background var(--dur-fast)',
        ...style,
      }}>
      {leading != null && <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center' }}>{leading}</div>}
      <div style={{ flex: 1, minWidth: 0 }}>
        {title != null && <div style={{ fontSize: 'var(--t-md)', color: active ? 'var(--accent)' : 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</div>}
        {sub != null && <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{sub}</div>}
      </div>
      {trailing != null && <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', gap: 8 }}>{trailing}</div>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// PageScaffold — outer scroll wrapper that owns the page side padding
// (--page-pad / --page-pad-compact) and the mobile bottom-nav clearance
// (--nav-clearance). Kills the `compact?16:28` / `compact?100:28` literal (~26x).
// ─────────────────────────────────────────────────────────────
function PageScaffold({ compact = false, noPadding = false, scroll = true, children, className, style }) {
  const pad = compact ? 'var(--page-pad-compact)' : 'var(--page-pad)';
  return (
    <div
      className={(scroll ? 'scroll-hide ' : '') + (className || '')}
      style={{
        ...(scroll ? { height: '100%', overflowY: 'auto' } : null),
        ...(noPadding ? null : {
          padding: pad,
          paddingBottom: compact ? 'var(--nav-clearance)' : 'var(--page-pad)',
        }),
        ...style,
      }}>
      {children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
Object.assign(window, {
  Stack, Inline, Divider, SectionLabel, Spinner,
  EmptyState, Avatar, Field, ListRow, PageScaffold,
});
