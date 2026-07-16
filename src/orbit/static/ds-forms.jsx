// ds-forms.jsx — design-system v2 form controls + tabs + badge.
//
// Loads after components.jsx / ds-layout.jsx, before settings-primitives.jsx and
// all consumers. Publishes to window. These are the most-reinvented primitives
// from the audit: 84 raw <input>, 26 <textarea>, 22 <select>, ~15 tab strips,
// 4 bespoke segmented controls. Convention: text controls call
// `onChange(value)` (the cleaner DS API) — NOT onChange(event) — matching the
// existing SearchInput / SettingSelect / NumberField. Styling = tokens only.

const { useState: _dsfUseState, useRef: _dsfUseRef } = React;

// Shared cosmic focus ring: violet border + soft glow on focus. Paint-only —
// derived entirely from the cosmic vars (--accent / --glow-soft).
const _dsfFocusRing = (focused, error) => focused
  ? { borderColor: error ? 'var(--err-line)' : 'var(--accent)', boxShadow: 'var(--glow-soft)' }
  : null;

// ─────────────────────────────────────────────────────────────
// Input — single-line text/search/password. THE most-needed missing primitive
// (replaces ~14 bespoke input-style factories + 84 raw <input>).
// `onCommit(value)` fires on blur + Enter for commit-on-blur flows.
// ─────────────────────────────────────────────────────────────
const _INPUT_H = { sm: 'var(--control-h-sm)', md: 'var(--control-h)' };
function Input({
  value, onChange, onCommit, type = 'text', placeholder, disabled = false,
  error = false, mono = false, size = 'md', autoFocus, onKeyDown, onBlur, onFocus, icon, trailing,
  inputMode, name, id, ariaLabel, maxLength, autoComplete, autoCorrect, spellCheck, min, max, step, style, inputStyle,
}) {
  const Ico = window.Icon;
  const [_focused, _setFocused] = _dsfUseState(false);
  const commit = onCommit ? () => onCommit(value) : undefined;
  const control = (
    <input
      type={type} value={value == null ? '' : value} name={name} id={id}
      aria-label={ariaLabel} inputMode={inputMode} autoFocus={autoFocus}
      placeholder={placeholder} disabled={disabled}
      maxLength={maxLength} autoComplete={autoComplete} autoCorrect={autoCorrect}
      spellCheck={spellCheck != null ? spellCheck : (mono ? false : undefined)} min={min} max={max} step={step}
      className={mono ? 'mono' : ''}
      onChange={(e) => onChange && onChange(e.target.value)}
      onBlur={(e) => { _setFocused(false); if (commit) commit(); if (onBlur) onBlur(e); }}
      onFocus={(e) => { _setFocused(true); if (onFocus) onFocus(e); }}
      onKeyDown={(e) => { if (e.key === 'Enter' && commit) commit(); if (onKeyDown) onKeyDown(e); }}
      style={{
        flex: 1, minWidth: 0, background: 'transparent', border: 'none', outline: 'none',
        color: 'var(--fg)', fontFamily: 'inherit',
        fontSize: mono ? 'var(--t-code)' : 'var(--t-sm)',
        ...inputStyle,
      }}
    />
  );
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      height: _INPUT_H[size] || _INPUT_H.md, padding: '0 10px',
      background: 'var(--surface-2)',
      border: '1px solid ' + (error ? 'var(--err-line)' : 'var(--hairline)'),
      borderRadius: 'var(--r-control)',
      opacity: disabled ? 0.55 : 1,
      transition: 'border-color var(--dur-fast), box-shadow var(--dur-fast)',
      ...style,
      ..._dsfFocusRing(_focused, error),
    }}>
      {icon && Ico && <Ico name={icon} size={15} color="var(--fg-3)" />}
      {control}
      {trailing}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Textarea — multi-line, mono by default (JetBrains Mono / --t-code), matches
// Input's surface/border. Replaces 26 raw <textarea> (composers, editors).
// ─────────────────────────────────────────────────────────────
function Textarea({
  value, onChange, rows = 4, placeholder, disabled = false, error = false,
  mono = true, resize = 'vertical', minHeight, onKeyDown, onPaste, onDrop, onBlur, onFocus,
  autoFocus, name, id, ariaLabel, maxLength, autoComplete, spellCheck, style, inputStyle,
}) {
  const [_focused, _setFocused] = _dsfUseState(false);
  return (
    <textarea
      value={value == null ? '' : value} rows={rows} name={name} id={id}
      aria-label={ariaLabel} placeholder={placeholder} disabled={disabled}
      autoFocus={autoFocus} maxLength={maxLength} autoComplete={autoComplete}
      spellCheck={spellCheck != null ? spellCheck : (mono ? false : undefined)}
      className={mono ? 'mono' : ''}
      onChange={(e) => onChange && onChange(e.target.value)}
      onKeyDown={onKeyDown} onPaste={onPaste} onDrop={onDrop}
      onBlur={(e) => { _setFocused(false); if (onBlur) onBlur(e); }}
      onFocus={(e) => { _setFocused(true); if (onFocus) onFocus(e); }}
      style={{
        width: '100%', boxSizing: 'border-box', padding: '10px 12px',
        background: 'var(--surface-2)',
        border: '1px solid ' + (error ? 'var(--err-line)' : 'var(--hairline)'),
        borderRadius: 'var(--r-control)', color: 'var(--fg)', outline: 'none',
        fontFamily: mono ? 'var(--font-mono)' : 'inherit',
        fontSize: mono ? 'var(--t-code)' : 'var(--t-sm)', lineHeight: 1.6,
        resize, minHeight, opacity: disabled ? 0.55 : 1,
        transition: 'border-color var(--dur-fast), box-shadow var(--dur-fast)',
        // Textarea has no wrapper, so style already lands on the element; inputStyle
        // is accepted as an alias (merged last) so call sites that route text props
        // there — mirroring Input's API — still apply.
        ...style, ...inputStyle,
        ..._dsfFocusRing(_focused, error),
      }}
    />
  );
}

// ─────────────────────────────────────────────────────────────
// Select — styled native <select> (the generalized SettingSelect). Native arrow
// kept (works on all platforms); our surface/border/height applied.
// Replaces 22 raw <select>. options = [{value,label}].
// ─────────────────────────────────────────────────────────────
function Select({ value, options = [], onChange, placeholder, disabled = false, mono = false, size = 'md', minWidth = 140, ariaLabel, name, id, style }) {
  const [_focused, _setFocused] = _dsfUseState(false);
  return (
    <select
      value={value == null ? '' : value} disabled={disabled} aria-label={ariaLabel}
      name={name} id={id}
      onChange={(e) => onChange && onChange(e.target.value)}
      onFocus={() => _setFocused(true)} onBlur={() => _setFocused(false)}
      className={mono ? 'mono' : ''}
      style={{
        background: 'var(--surface-2)', color: 'var(--fg)',
        border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)',
        padding: '0 10px', height: _INPUT_H[size] || _INPUT_H.md,
        fontSize: mono ? 'var(--t-code)' : 'var(--t-sm)', fontFamily: 'inherit',
        outline: 'none', minWidth, maxWidth: '100%', opacity: disabled ? 0.55 : 1,
        transition: 'border-color var(--dur-fast), box-shadow var(--dur-fast)',
        ...style,
        ..._dsfFocusRing(_focused, false),
      }}>
      {placeholder != null && <option value="" disabled>{placeholder}</option>}
      {options.map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
    </select>
  );
}

// ─────────────────────────────────────────────────────────────
// Segmented — pick one of N (the global promotion of HubSettings.Segmented).
// Wraps on narrow screens. options = [{value,label}]. Identical API so the
// settings atom can delegate here. Use for 2-3 options; >3 prefer Select.
// ─────────────────────────────────────────────────────────────
function Segmented({ value, options = [], onChange, disabled = false, size = 'sm', style }) {
  const h = size === 'md' ? 'var(--control-h)' : 'var(--control-h-sm)';
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', background: 'var(--surface-3)',
      border: '1px solid var(--hairline-strong)', borderRadius: 'var(--r-control)',
      padding: 2, gap: 2, opacity: disabled ? 0.5 : 1, ...style,
    }}>
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button key={opt.value} onClick={() => { if (!disabled) onChange && onChange(opt.value); }} disabled={disabled}
            style={{
              flex: '1 1 auto', minHeight: h, padding: '6px 12px', borderRadius: 'var(--r-sm)', border: 'none',
              background: active ? 'var(--grad-cosmic)' : 'transparent',
              color: active ? 'var(--accent-fg)' : 'var(--fg-2)',
              boxShadow: active ? 'var(--glow-soft)' : 'none',
              fontSize: 'var(--t-sm)', fontFamily: 'inherit', cursor: disabled ? 'not-allowed' : 'pointer',
              transition: 'background var(--dur-fast), color var(--dur-fast), box-shadow var(--dur-fast)', whiteSpace: 'nowrap',
            }}>
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Tabs — ONE tab primitive, two variants. Replaces ~15 hand-rolled tab strips.
//   variant='underline' — 2px accent border-bottom, flush with a hairline rule
//   variant='pills'     — accent-soft active chip in a surface-2 track
// tabs = [{id,label,icon,count}].
// ─────────────────────────────────────────────────────────────
function Tabs({ tabs = [], active, onChange, variant = 'underline', sticky = false, size = 'md', style }) {
  const Ico = window.Icon;
  const fs = size === 'sm' ? 'var(--t-sm)' : 'var(--t-md)';
  const stickyStyle = sticky ? { position: 'sticky', top: 0, zIndex: 'var(--z-sticky)', background: 'var(--bg)' } : null;

  if (variant === 'pills') {
    return (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, background: 'var(--surface-2)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)', padding: 3, ...stickyStyle, ...style }}>
        {tabs.map((t) => {
          const on = t.id === active;
          return (
            <button key={t.id} onClick={() => onChange && onChange(t.id)}
              className={on ? 'orbit-glow-soft' : ''} style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '6px 12px', borderRadius: 'var(--r-sm)', border: 'none',
              background: on ? 'var(--accent-soft)' : 'transparent',
              color: on ? 'var(--accent)' : 'var(--fg-2)',
              fontSize: fs, fontFamily: 'inherit', fontWeight: on ? 500 : 400,
              cursor: 'pointer', whiteSpace: 'nowrap', transition: 'background var(--dur-fast), color var(--dur-fast), box-shadow var(--dur-fast)',
            }}>
              {t.icon && Ico && <Ico name={t.icon} size={15} />}
              <span>{t.label}</span>
              {t.count != null && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: on ? 'var(--accent)' : 'var(--fg-3)' }}>{t.count}</span>}
            </button>
          );
        })}
      </div>
    );
  }

  // underline (default)
  return (
    <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--hairline)', ...stickyStyle, ...style }}>
      {tabs.map((t) => {
        const on = t.id === active;
        return (
          <button key={t.id} onClick={() => onChange && onChange(t.id)} style={{
            position: 'relative',
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '9px 12px', marginBottom: -1, background: 'transparent', border: 'none',
            borderBottom: '2px solid transparent',
            color: on ? 'var(--fg)' : 'var(--fg-3)',
            fontSize: fs, fontFamily: 'inherit', fontWeight: on ? 500 : 400,
            cursor: 'pointer', whiteSpace: 'nowrap', transition: 'color var(--dur-fast)',
          }}>
            {t.icon && Ico && <Ico name={t.icon} size={15} />}
            {t.label}
            {t.count != null && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{t.count}</span>}
            {on && <span aria-hidden="true" style={{
              position: 'absolute', left: 0, right: 0, bottom: -1, height: 2,
              background: 'var(--grad-cosmic)', borderRadius: 'var(--r-pill)',
              boxShadow: 'var(--glow-soft)',
            }} />}
          </button>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Badge — small count / dot / label indicator. Distinct from Chip by its
// absolute-overlay mode (notification dot/count on an icon button).
// ─────────────────────────────────────────────────────────────
const _BADGE_COLOR = {
  accent: { bg: 'var(--grad-cosmic)', fg: 'var(--accent-fg)', glow: 'var(--glow-soft)' },
  err:    { bg: 'var(--err)',    fg: '#fff' },
  ok:     { bg: 'var(--ok)',     fg: 'var(--accent-fg)' },
  muted:  { bg: 'var(--surface-3)', fg: 'var(--fg-2)' },
};
function Badge({ variant = 'count', value, color = 'accent', max = 99, absolute = false, style }) {
  const c = _BADGE_COLOR[color] || _BADGE_COLOR.accent;
  const abs = absolute ? { position: 'absolute', top: -4, right: -4, zIndex: 1 } : {};
  if (variant === 'dot') {
    return <span style={{ width: 8, height: 8, borderRadius: 'var(--r-pill)', background: c.bg, boxShadow: c.glow || undefined, display: 'inline-block', flexShrink: 0, ...abs, ...style }} />;
  }
  if (variant === 'count') {
    const n = typeof value === 'number' ? (value > max ? max + '+' : String(value)) : value;
    if (value === 0 || value == null || value === '') return null;
    return (
      <span className="mono" style={{
        minWidth: 16, height: 16, padding: '0 5px', borderRadius: 'var(--r-pill)',
        background: c.bg, color: c.fg, boxShadow: c.glow || undefined, fontSize: 'var(--t-2xs)', fontWeight: 600,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        lineHeight: 1, ...abs, ...style,
      }}>{n}</span>
    );
  }
  // label
  return (
    <span className="mono" style={{
      padding: '2px 7px', borderRadius: 'var(--r-control)', background: c.bg, color: c.fg, boxShadow: c.glow || undefined,
      fontSize: 'var(--t-2xs)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em',
      whiteSpace: 'nowrap', ...abs, ...style,
    }}>{value}</span>
  );
}

// ─────────────────────────────────────────────────────────────
Object.assign(window, { Input, Textarea, Select, Segmented, Tabs, Badge });
