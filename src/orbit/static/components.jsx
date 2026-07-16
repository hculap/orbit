// components.jsx — small shared UI primitives for the HUB
// Exports many small components to window so app.jsx and sections.jsx can use them.

const { useState, useEffect, useRef, useMemo, useCallback, createContext, useContext } = React;

// ─────────────────────────────────────────────────────────────
// Icon — minimal stroke icon set, 20px default
// ─────────────────────────────────────────────────────────────
function Icon({ name, size = 20, stroke = 1.5, color = "currentColor", style }) {
  // `s` is spread directly onto <svg> as ATTRIBUTES — so we must NOT mix
  // user-provided CSS (e.g. style={{transform:'rotate(15deg)'}}) into it,
  // since SVG's `transform` attribute rejects CSS units (`rotate(15deg)`)
  // while the CSS `transform` property requires them. Keep dims as
  // attributes; let `style` go through React's style prop (CSS path).
  const s = { width: size, height: size, style };
  const p = { fill: 'none', stroke: color, strokeWidth: stroke, strokeLinecap: 'round', strokeLinejoin: 'round' };
  switch (name) {
    case 'menu':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 7h16M4 12h16M4 17h16"/></svg>);
    case 'close':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M6 6l12 12M6 18L18 6"/></svg>);
    case 'search':    return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="11" cy="11" r="7"/><path {...p} d="M20 20l-3.5-3.5"/></svg>);
    case 'cmd':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 6h6v12H9zM6 9V6h3M6 15v3h3M18 9V6h-3M18 15v3h-3"/></svg>);
    case 'chevron-r': return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 6l6 6-6 6"/></svg>);
    case 'chevron-l': return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M15 6l-6 6 6 6"/></svg>);
    case 'chevron-d': return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M6 9l6 6 6-6"/></svg>);
    case 'arrow-up':  return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 19V5M5 12l7-7 7 7"/></svg>);
    case 'arrow-down':return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 5v14M5 12l7 7 7-7"/></svg>);
    case 'plus':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 5v14M5 12h14"/></svg>);
    case 'send':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 12l14-7-5 16-4-7-5-2z"/></svg>);
    case 'sparkle':   return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 4l1.6 4.4L18 10l-4.4 1.6L12 16l-1.6-4.4L6 10l4.4-1.6L12 4zM18 16l.8 2.2L21 19l-2.2.8L18 22l-.8-2.2L15 19l2.2-.8L18 16z"/></svg>);
    case 'help':      return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="9"/><path {...p} d="M9.3 9a2.7 2.7 0 0 1 5.2 1c0 1.8-2.5 2-2.5 3.5M12 17h.01"/></svg>);
    case 'home':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1v-9z"/></svg>);
    case 'folder':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>);
    case 'box':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M3 8l9-4 9 4-9 4-9-4zM3 8v8l9 4M21 8v8l-9 4"/></svg>);
    case 'globe':     return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="9"/><path {...p} d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>);
    case 'book':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2V5zM8 3v18"/></svg>);
    case 'share':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 4v12M7 9l5-5 5 5M5 16v3a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3"/></svg>);
    case 'cpu':       return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="6" y="6" width="12" height="12" rx="1.5"/><rect {...p} x="9" y="9" width="6" height="6"/><path {...p} d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/></svg>);
    case 'logs':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 5h14v4H5zM5 11h14v4H5zM5 17h10v2H5z"/></svg>);
    case 'bot':       return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="4" y="8" width="16" height="11" rx="2"/><path {...p} d="M8 14h.01M16 14h.01M12 4v4M9 22h6"/></svg>);
    case 'upload':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 16V4M7 9l5-5 5 5M5 18v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2"/></svg>);
    case 'download':  return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 4v12M7 11l5 5 5-5M5 18v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2"/></svg>);
    case 'trash':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 7h14M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/></svg>);
    case 'refresh':   return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 12a8 8 0 0 1 14-5.3M20 4v4h-4M20 12a8 8 0 0 1-14 5.3M4 20v-4h4"/></svg>);
    case 'filter':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 5h16l-6 8v6l-4-2v-4z"/></svg>);
    case 'check':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 12l5 5 9-11"/></svg>);
    case 'dots':      return (<svg {...s} viewBox="0 0 24 24"><circle cx="6" cy="12" r="1.5" fill="currentColor"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><circle cx="18" cy="12" r="1.5" fill="currentColor"/></svg>);
    case 'dots-v':    return (<svg {...s} viewBox="0 0 24 24"><circle cx="12" cy="6" r="1.5" fill="currentColor"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><circle cx="12" cy="18" r="1.5" fill="currentColor"/></svg>);
    case 'image':     return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="5" width="18" height="14" rx="2"/><circle {...p} cx="9" cy="11" r="2"/><path {...p} d="M21 17l-5-5-9 9"/></svg>);
    case 'file':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M6 3h8l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 3v4h4"/></svg>);
    case 'video':     return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="6" width="13" height="12" rx="2"/><path {...p} d="M16 10l5-3v10l-5-3"/></svg>);
    case 'pdf':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M6 3h8l4 4v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 3v4h4M8 14h2a1 1 0 0 0 0-2H8v4M14 12v4M14 12h2"/></svg>);
    case 'archive':   return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M3 7h18v3H3zM5 10v9a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-9M10 14h4"/></svg>);
    case 'cog':       return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="3"/><path {...p} d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>);
    case 'circle':    return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="9"/></svg>);
    case 'spinner':   return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 4a8 8 0 1 1-8 8" stroke={color}><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.9s" repeatCount="indefinite"/></path></svg>);
    case 'mic':       return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="9" y="3" width="6" height="12" rx="3"/><path {...p} d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>);
    case 'headphones': return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M3 14a9 9 0 0 1 18 0M3 14v4a2 2 0 0 0 2 2h2v-7H5a2 2 0 0 0-2 2zM21 14v4a2 2 0 0 1-2 2h-2v-7h2a2 2 0 0 1 2 2z"/></svg>);
    case 'mic-fill': return (<svg {...s} viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="12" rx="3" fill={color}/><path {...p} d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>);
    case 'attach':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M20 12l-8 8a5 5 0 0 1-7-7l9-9a3.5 3.5 0 0 1 5 5l-9 9a2 2 0 0 1-3-3l8-8"/></svg>);
    case 'eye':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle {...p} cx="12" cy="12" r="3"/></svg>);
    case 'pencil':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 20h4l10-10-4-4L4 16v4zM14 6l4 4"/></svg>);
    case 'panel-r':   return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="4" width="18" height="16" rx="2"/><path {...p} d="M15 4v16"/></svg>);
    case 'tasks':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 7l2 2 4-4M5 14l2 2 4-4M5 19h.01M14 7h6M14 12h6M14 17h6"/></svg>);
    case 'star':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 3l2.6 5.5 6 .8-4.4 4.2 1.1 6-5.3-2.9L6.7 19.5l1.1-6L3.4 9.3l6-.8L12 3z"/></svg>);
    case 'star-fill': return (<svg {...s} viewBox="0 0 24 24"><path d="M12 3l2.6 5.5 6 .8-4.4 4.2 1.1 6-5.3-2.9L6.7 19.5l1.1-6L3.4 9.3l6-.8L12 3z" fill={color} stroke={color} strokeWidth={stroke} strokeLinejoin="round"/></svg>);
    case 'bell':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M6 8a6 6 0 1 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9z"/><path {...p} d="M10 21a2 2 0 0 0 4 0"/></svg>);
    case 'bell-fill': return (<svg {...s} viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9z" fill={color} stroke="none"/><path d="M10 21a2 2 0 0 0 4 0" fill={color} stroke="none"/></svg>);
    case 'volume':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 9h4l5-4v14l-5-4H4z"/><path {...p} d="M16 8a5 5 0 0 1 0 8M19 5a9 9 0 0 1 0 14"/></svg>);
    case 'square':    return (<svg {...s} viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5" fill={color} stroke={color} strokeWidth={stroke}/></svg>);
    case 'copy':         return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="9" y="9" width="10" height="10" rx="2"/><path {...p} d="M5 15V5h10"/></svg>);
    case 'corner-up-left': return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 14l-4-4 4-4M5 10h11a4 4 0 0 1 4 4v6"/></svg>);
    case 'pin':          return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 17v5M9 3h6l-1 5a4 4 0 0 1 3 4H7a4 4 0 0 1 3-4L9 3z"/></svg>);
    case 'pin-fill':     return (<svg {...s} viewBox="0 0 24 24"><path d="M12 17v5M9 3h6l-1 5a4 4 0 0 1 3 4H7a4 4 0 0 1 3-4L9 3z" fill={color} stroke="none"/></svg>);
    case 'notepad':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 3v2h6V3M5 7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM9 11h6M9 15h6"/></svg>);
    case 'list-checks':  return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M3 6l1 1 2-2M3 12l1 1 2-2M3 18l1 1 2-2M10 6h11M10 12h11M10 18h11"/></svg>);
    case 'clock':        return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="9"/><path {...p} d="M12 7v5l3 2"/></svg>);
    case 'inbox':        return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M22 12h-6l-2 3h-4l-2-3H2M5 5l-3 7v5a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-5l-3-7z"/></svg>);
    case 'bulb':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 18h6M10 22h4M12 2a6 6 0 0 0-4 10.5c1 .9 1.5 1.9 1.5 3V15h5v-.5c0-1.1.5-2.1 1.5-3A6 6 0 0 0 12 2z"/></svg>);
    case 'terminal':     return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="4" width="18" height="16" rx="2"/><path {...p} d="M7 9l3 3-3 3M13 15h4"/></svg>);
    case 'maximize':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>);
    case 'minimize':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M8 3v3a2 2 0 0 1-2 2H3M16 3v3a2 2 0 0 0 2 2h3M21 16h-3a2 2 0 0 0-2 2v3M3 16h3a2 2 0 0 1 2 2v3"/></svg>);
    case 'pip':          return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="5" width="18" height="14" rx="2"/><rect {...p} x="12" y="11" width="7" height="5" rx="1"/></svg>);
    case 'rotate-ccw':   return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M3 3v6h6"/><path {...p} d="M3.5 14a9 9 0 1 0 2.6-8.4L3 9"/></svg>);
    case 'rotate-cw':    return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M21 3v6h-6"/><path {...p} d="M20.5 14a9 9 0 1 1-2.6-8.4L21 9"/></svg>);
    case 'play':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M8 5v14l11-7z"/></svg>);
    case 'pause':        return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 5v14M15 5v14"/></svg>);
    case 'stop-circle':  return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="9"/><rect {...p} x="9" y="9" width="6" height="6" rx="1"/></svg>);
    case 'skip':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 5l9 7-9 7zM19 5v14"/></svg>);
    case 'heart':        return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 20l-1.4-1.3C5.4 14 2 11 2 7.5A4.5 4.5 0 0 1 12 5a4.5 4.5 0 0 1 10 2.5c0 3.5-3.4 6.5-8.6 11.2L12 20z"/></svg>);
    case 'heart-fill':   return (<svg {...s} viewBox="0 0 24 24"><path d="M12 20l-1.4-1.3C5.4 14 2 11 2 7.5A4.5 4.5 0 0 1 12 5a4.5 4.5 0 0 1 10 2.5c0 3.5-3.4 6.5-8.6 11.2L12 20z" fill={color} stroke="none"/></svg>);
    case 'flag':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 21V4M5 4h13l-2.5 4L18 12H5"/></svg>);
    case 'bookmark':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/></svg>);
    case 'tag':          return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M11 3l9 1 1 9-8 8-9-9z"/><circle {...p} cx="15" cy="9" r="1.3"/></svg>);
    case 'link':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 15l6-6M10.5 6l1-1a4 4 0 0 1 6 6l-1 1M13.5 18l-1 1a4 4 0 0 1-6-6l1-1"/></svg>);
    case 'lock':         return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="5" y="11" width="14" height="9" rx="2"/><path {...p} d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>);
    case 'unlock':       return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="5" y="11" width="14" height="9" rx="2"/><path {...p} d="M8 11V8a4 4 0 0 1 7.9-1"/></svg>);
    case 'key':          return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="8" cy="8" r="4"/><path {...p} d="M11 11l9 9M17 17l2-2M15 19l2-2"/></svg>);
    case 'shield':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg>);
    case 'wifi':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M2 8.5a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0M8.5 15.5a6 6 0 0 1 7 0M12 19h.01"/></svg>);
    case 'cloud':        return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.6-1.5A3.5 3.5 0 0 1 18 18z"/></svg>);
    case 'database':     return (<svg {...s} viewBox="0 0 24 24"><ellipse {...p} cx="12" cy="6" rx="8" ry="3"/><path {...p} d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>);
    case 'server':       return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="4" width="18" height="7" rx="2"/><rect {...p} x="3" y="13" width="18" height="7" rx="2"/><path {...p} d="M7 7.5h.01M7 16.5h.01"/></svg>);
    case 'code':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M8 8l-4 4 4 4M16 8l4 4-4 4M13 5l-2 14"/></svg>);
    case 'git-branch':   return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="6" cy="6" r="2.5"/><circle {...p} cx="6" cy="18" r="2.5"/><circle {...p} cx="18" cy="7" r="2.5"/><path {...p} d="M6 8.5v7M18 9.5c0 3-3 4-6 4"/></svg>);
    case 'zap':          return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M13 3L4 14h7l-1 7 9-11h-7z"/></svg>);
    case 'flame':        return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 3c2 4 5 5 5 9a5 5 0 0 1-10 0c0-2 1-3.2 2-4 .2 1 1 1.8 2 2 0-2.8-1-5 1-7z"/></svg>);
    case 'rocket':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 2c3 2.5 5 5.5 5 9.5l-1.5 3.5h-7L7 11.5C7 7.5 9 4.5 12 2zM9 15l-2 2-1 4 4-1 2-2M12 11a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"/></svg>);
    case 'calendar':     return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="5" width="18" height="16" rx="2"/><path {...p} d="M3 9h18M8 3v4M16 3v4"/></svg>);
    case 'map-pin':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 21s-7-6-7-11a7 7 0 0 1 14 0c0 5-7 11-7 11z"/><circle {...p} cx="12" cy="10" r="2.5"/></svg>);
    case 'message':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4V5z"/></svg>);
    case 'at-sign':      return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="4"/><path {...p} d="M16 8v5.5a2.5 2.5 0 0 0 5 0V12A9 9 0 1 0 16.5 19"/></svg>);
    case 'hash':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 9h14M5 15h14M9 4l-2 16M17 4l-2 16"/></svg>);
    case 'sliders':      return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M6 15v4"/></svg>);
    case 'grid':         return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="4" y="4" width="7" height="7" rx="1"/><rect {...p} x="13" y="4" width="7" height="7" rx="1"/><rect {...p} x="4" y="13" width="7" height="7" rx="1"/><rect {...p} x="13" y="13" width="7" height="7" rx="1"/></svg>);
    case 'layers':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M12 3l9 5-9 5-9-5zM3 13l9 5 9-5M3 17l9 5 9-5"/></svg>);
    case 'save':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M5 3h11l3 3v15H5zM8 3v5h7M8 21v-7h8v7"/></svg>);
    case 'external':     return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>);
    case 'sun':          return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="4"/><path {...p} d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"/></svg>);
    case 'moon':         return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M21 13A8 8 0 1 1 11 3a6 6 0 0 0 10 10z"/></svg>);
    case 'coffee':       return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M4 8h13v5a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4zM17 9h2a2 2 0 0 1 0 4h-2M6 2v2M10 2v2M14 2v2"/></svg>);
    case 'music':        return (<svg {...s} viewBox="0 0 24 24"><path {...p} d="M9 18V5l11-2v13"/><circle {...p} cx="6" cy="18" r="3"/><circle {...p} cx="17" cy="16" r="3"/></svg>);
    case 'compass':      return (<svg {...s} viewBox="0 0 24 24"><circle {...p} cx="12" cy="12" r="9"/><path {...p} d="M16 8l-2 6-6 2 2-6z"/></svg>);
    case 'gift':         return (<svg {...s} viewBox="0 0 24 24"><rect {...p} x="3" y="8" width="18" height="4"/><path {...p} d="M5 12v9h14v-9M12 8v13M12 8C10.5 4 7 5 8 7c.7 1.4 4 1 4 1M12 8c1.5-4 5-3 4-1-.7 1.4-4 1-4 1"/></svg>);
    default: return null;
  }
}

// ─────────────────────────────────────────────────────────────
// StatusDot
// ─────────────────────────────────────────────────────────────
function StatusDot({ status = 'ok', size = 8 }) {
  // `brand` is an optional cosmic "live"/brand pulse (plasma core) — keeps every
  // semantic status (ok/warn/err) on its own colour for real health signalling.
  if (status === 'brand') {
    return <span className="orbit-core" style={{ width: size, height: size, flexShrink: 0, display: 'inline-block' }} />;
  }
  const color = status === 'ok' ? 'var(--ok)' : status === 'warn' ? 'var(--warn)' : status === 'err' ? 'var(--err)' : 'var(--fg-4)';
  return <span style={{ width: size, height: size, borderRadius: 'var(--r-pill)', background: color, flexShrink: 0, display: 'inline-block' }} />;
}

// ─────────────────────────────────────────────────────────────
// Chip
// ─────────────────────────────────────────────────────────────
// `variant` (v2) maps to the surface/status token families; `accent` (legacy
// boolean) still works and is equivalent to variant='accent'. `size='xs'` is
// the dense tag used in cards/rows.
const CHIP_VARIANTS = {
  default: { bg: 'var(--surface-2)',  fg: 'var(--fg-2)',  bd: 'var(--hairline)' },
  accent:  { bg: 'var(--grad-cosmic-soft)', fg: 'var(--accent)', bd: 'var(--accent-line)' },
  ok:      { bg: 'var(--ok-soft)',    fg: 'var(--ok)',    bd: 'var(--ok-line)' },
  warn:    { bg: 'var(--warn-soft)',  fg: 'var(--warn)',  bd: 'var(--warn-line)' },
  danger:  { bg: 'var(--err-soft)',   fg: 'var(--err)',   bd: 'var(--err-line)' },
  muted:   { bg: 'transparent',       fg: 'var(--fg-3)',  bd: 'var(--hairline)' },
};
function Chip({ children, icon, accent = false, variant, mono = false, size = 'sm', onClick, style }) {
  const v = CHIP_VARIANTS[variant || (accent ? 'accent' : 'default')] || CHIP_VARIANTS.default;
  const xs = size === 'xs';
  return (
    <span onClick={onClick} className={mono ? 'mono' : ''} style={{
      display: 'inline-flex', alignItems: 'center', gap: xs ? 4 : 6,
      padding: xs ? '2px 7px' : '4px 9px', borderRadius: 'var(--r-pill)',
      fontSize: xs ? 'var(--t-2xs)' : 'var(--t-cap)', lineHeight: '14px',
      background: v.bg, color: v.fg, border: '1px solid ' + v.bd,
      cursor: onClick ? 'pointer' : 'default',
      whiteSpace: 'nowrap',
      ...style,
    }}>
      {icon}{children}
    </span>
  );
}

// ─────────────────────────────────────────────────────────────
// Card — base surface used everywhere
// ─────────────────────────────────────────────────────────────
// `variant` (v2): 'section' (default, --r-lg, 16px) is the page-level card;
// 'inset' (--r-md, 14×16) matches the old SettingCard so it can delegate here.
// `gap` turns the card into a flex column with token spacing between children.
function Card({ children, padding, gap, hover = false, onClick, style, variant = 'section' }) {
  const [h, setH] = useState(false);
  const inset = variant === 'inset';
  const pad = padding != null ? padding : (inset ? '14px 16px' : 16);
  return (
    <div
      onClick={onClick}
      onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      style={{
        background: hover && h ? 'var(--surface-2)' : 'var(--surface-1)',
        border: '1px solid var(--hairline)',
        borderRadius: inset ? 'var(--r-md)' : 'var(--r-lg)',
        padding: pad,
        ...(gap != null ? { display: 'flex', flexDirection: 'column', gap } : null),
        cursor: onClick ? 'pointer' : 'default',
        // Interactive cards (hover/onClick) get a soft cosmic glow on hover; resting cards stay calm/flat.
        boxShadow: (hover || onClick) && h ? 'var(--glow-soft)' : 'none',
        transition: 'background var(--dur-base), transform var(--dur-base), border-color var(--dur-base), box-shadow var(--dur-base)',
        ...style,
      }}
    >
      {children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// ProgressBar
// ─────────────────────────────────────────────────────────────
function ProgressBar({ value, max = 100, color = 'var(--accent)', height = 6, label, sub }) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  // Default (accent) fill gets the cosmic gradient; explicit colors (status hues) pass through.
  const fill = color === 'var(--accent)' ? 'var(--grad-cosmic)' : color;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {(label || sub) && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', fontSize: 'var(--t-cap)' }}>
          {label && <span style={{ color: 'var(--fg-2)', fontWeight: 500 }}>{label}</span>}
          {sub && <span className="mono" style={{ color: 'var(--fg-3)', fontSize: 'var(--t-xs)' }}>{sub}</span>}
        </div>
      )}
      <div style={{ height, background: 'var(--surface-3)', borderRadius: 'var(--r-pill)', overflow: 'hidden' }}>
        <div style={{
          width: pct + '%', height: '100%',
          background: fill,
          borderRadius: 'var(--r-pill)',
          transition: 'width .6s cubic-bezier(.2,.7,.3,1)',
        }} />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Sparkline — tiny svg line chart
// ─────────────────────────────────────────────────────────────
function Sparkline({ data, width = 100, height = 24, color = 'var(--accent)', filled = true }) {
  if (!data || data.length < 2) return <div style={{ width, height }} />;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const stepX = width / (data.length - 1);
  const points = data.map((v, i) => [i * stepX, height - ((v - min) / range) * (height - 2) - 1]);
  const line = points.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  const area = line + ` L${width},${height} L0,${height} Z`;
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      {filled && <path d={area} fill={color} fillOpacity="0.12"/>}
      <path d={line} fill="none" stroke={color} strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round"/>
    </svg>
  );
}

// ─────────────────────────────────────────────────────────────
// SectionHeader — used by every section's top
// ─────────────────────────────────────────────────────────────
function SectionHeader({ eyebrow, title, count, actions, style }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 16, ...style }}>
      <div>
        {eyebrow && <div className="mono orbit-gradient-text" style={{ fontSize: 'var(--t-xs)', letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 6 }}>{eyebrow}</div>}
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <h1 style={{ margin: 0, fontSize: 'var(--t-h1)', fontWeight: 500, letterSpacing: '-0.02em' }}>{title}</h1>
          {count != null && <span className="mono" style={{ color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>{count}</span>}
        </div>
      </div>
      {actions && <div style={{ display: 'flex', gap: 8 }}>{actions}</div>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// IconButton — square ghost button
// ─────────────────────────────────────────────────────────────
function IconButton({ icon, label, onClick, active = false, size = 36, style, disabled = false, compact = false }) {
  const [h, setH] = useState(false);
  // Keyboard-focus also flips the tooltip so screen-reader / tab-nav users
  // get the same affordance as mouse users.
  const [focused, setFocused] = useState(false);
  const [showTip, setShowTip] = useState(false);
  const tipTimerRef = useRef(null);
  // Custom tooltip: appears 280ms after hover/focus to avoid flicker on
  // pass-through mouse moves; pointer-events: none so it never blocks the
  // button itself. Mobile users don't see it (no hover) — they get the bigger
  // kebab labels instead.
  useEffect(() => {
    const wantsTip = (h || focused) && !!label;
    if (wantsTip) {
      tipTimerRef.current = setTimeout(() => setShowTip(true), 280);
    } else {
      setShowTip(false);
    }
    return () => {
      if (tipTimerRef.current) {
        clearTimeout(tipTimerRef.current);
        tipTimerRef.current = null;
      }
    };
  }, [h, focused, label]);
  return (
    <button
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      aria-label={label}
      style={{
        position: 'relative',
        width: size, height: size, borderRadius: 'var(--r-control)',
        ...(compact ? { minWidth: 'var(--control-h-touch)', minHeight: 'var(--control-h-touch)' } : null),
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        border: '1px solid ' + (active ? 'var(--accent-line)' : 'transparent'),
        background: active ? 'var(--accent-soft)' : (h && !disabled ? 'var(--surface-2)' : 'transparent'),
        color: active ? 'var(--accent)' : 'var(--fg-2)',
        boxShadow: active ? 'var(--glow-soft)' : 'none',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
        transition: 'background var(--dur-fast), color var(--dur-fast), box-shadow var(--dur-fast)',
        flexShrink: 0,
        ...style,
      }}
    >
      <Icon name={icon} size={18} />
      {showTip && label && (
        <span aria-hidden="true" className="mono" style={{
          position: 'absolute',
          top: 'calc(100% + 6px)', left: '50%',
          transform: 'translateX(-50%)',
          padding: '4px 8px', borderRadius: 'var(--r-sm)',
          background: 'var(--surface-1)', color: 'var(--fg)',
          border: '1px solid var(--hairline-strong)',
          fontSize: 'var(--t-xs)', lineHeight: 1.2, whiteSpace: 'nowrap',
          pointerEvents: 'none', zIndex: 60,
          boxShadow: '0 4px 12px rgba(0,0,0,0.32)',
        }}>{label}</span>
      )}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────
// Button — text button, primary / ghost
// ─────────────────────────────────────────────────────────────
// When `href` is passed, Button renders a real <a> (a genuine, ⌘-clickable
// link) instead of a <button> — same look, correct semantics. Avoids the
// invalid <button>-inside-<a> nesting that broke modifier-clicks.
//
// `disabled` / `busy` (design-system v2): the documented root cause of
// _ActionButton + the defensive `window.Button ? … : <button>` fallbacks being
// reinvented. `busy` shows a spinner and acts disabled. A disabled link drops
// its href (renders a non-navigating <button disabled>) so it can't be ⌘-clicked.
function Button({ children, onClick, href, target, rel, variant = 'ghost', icon, size = 'md', style, type = 'button', disabled = false, busy = false }) {
  const [h, setH] = useState(false);
  const off = disabled || busy;
  const sizes = { sm: { p: '6px 10px', f: 12, h: 28 }, md: { p: '8px 14px', f: 13, h: 34 }, lg: { p: '11px 18px', f: 14, h: 42 } };
  const sz = sizes[size];
  const variants = {
    // primary → the signature cosmic CTA (.cosmic-btn supplies gradient fill + glow);
    // the bg/bd below are a non-class fallback only.
    primary: { bg: 'var(--grad-cosmic)', fg: 'var(--accent-fg)', bd: 'transparent' },
    ghost:   { bg: h && !off ? 'var(--surface-2)' : 'var(--surface-1)', fg: 'var(--fg)', bd: 'var(--hairline)' },
    quiet:   { bg: h && !off ? 'var(--surface-2)' : 'transparent', fg: 'var(--fg-2)', bd: 'transparent' },
    danger:  { bg: h && !off ? 'var(--err-soft)' : 'transparent', fg: 'var(--err)', bd: 'var(--err-line)' },
  };
  const v = variants[variant];
  const cls = variant === 'primary' && !off ? 'cosmic-btn' : undefined;
  const sharedStyle = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
    padding: sz.p, height: sz.h, fontSize: sz.f, fontWeight: 500,
    // When the .cosmic-btn class is active, let it own background/border/glow + the
    // hover sweep (inline `background` shorthand would wipe its background-image).
    ...(cls ? { color: v.fg } : { background: v.bg, color: v.fg, border: '1px solid ' + v.bd, transition: 'background var(--dur-fast)' }),
    borderRadius: 'var(--r-control)', cursor: off ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap',
    boxSizing: 'border-box', textDecoration: 'none',
    opacity: off ? 0.5 : 1,
    ...style,
  };
  const guardedClick = off ? (e) => { e.preventDefault(); e.stopPropagation(); } : onClick;
  const inner = (
    <React.Fragment>
      {busy ? <Icon name="spinner" size={14} /> : (icon && <Icon name={icon} size={14} stroke={1.8} />)}
      {children}
    </React.Fragment>
  );
  if (href && !off) {
    return (
      <a href={href} target={target} rel={rel} onClick={onClick} className={cls}
        onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
        style={sharedStyle}>
        {inner}
      </a>
    );
  }
  return (
    <button type={type} onClick={guardedClick} disabled={off} aria-busy={busy || undefined} className={cls}
      onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      style={sharedStyle}
    >
      {inner}
    </button>
  );
}

// ─────────────────────────────────────────────────────────────
// SearchInput
// ─────────────────────────────────────────────────────────────
function SearchInput({ value, onChange, placeholder = 'Search…', shortcut, autoFocus, style }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      background: 'var(--surface-1)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', padding: '0 10px', height: 36,
      ...style,
    }}>
      <Icon name="search" size={15} color="var(--fg-3)" />
      <input
        autoFocus={autoFocus}
        value={value} onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        style={{
          flex: 1, background: 'transparent', border: 'none', outline: 'none',
          color: 'var(--fg)', fontFamily: 'inherit', fontSize: 'var(--t-sm)', minWidth: 0,
        }}
      />
      {shortcut && (
        <span className="kbd" style={{ marginLeft: 4 }}>{shortcut}</span>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// File icon by ext
// ─────────────────────────────────────────────────────────────
function FileIconBox({ ext, name, isFolder, size = 40, accent }) {
  const map = {
    folder:  { icon: 'folder', tint: 'oklch(0.78 0.10 240 / 0.18)', fg: 'oklch(0.82 0.10 240)' },
    image:   { icon: 'image',  tint: 'var(--ok-soft)', fg: 'oklch(0.82 0.12 155)' },
    pdf:     { icon: 'pdf',    tint: 'oklch(0.70 0.18 25 / 0.18)',  fg: 'oklch(0.78 0.14 25)' },
    video:   { icon: 'video',  tint: 'oklch(0.72 0.18 295 / 0.18)', fg: 'oklch(0.80 0.14 295)' },
    text:    { icon: 'file',   tint: 'var(--surface-3)',             fg: 'var(--fg-2)' },
    archive: { icon: 'archive',tint: 'var(--warn-soft)',  fg: 'oklch(0.85 0.12 80)' },
    other:   { icon: 'file',   tint: 'var(--surface-3)',             fg: 'var(--fg-3)' },
  };
  let kind = 'other';
  if (isFolder) kind = 'folder';
  else if (/\.(png|jpe?g|gif|webp|svg|heic)$/i.test(name)) kind = 'image';
  else if (/\.pdf$/i.test(name)) kind = 'pdf';
  else if (/\.(mp4|mov|webm|avi|mkv)$/i.test(name)) kind = 'video';
  else if (/\.(zip|tar|gz|7z|rar)$/i.test(name)) kind = 'archive';
  else if (/\.(md|txt|json|ya?ml|toml|conf|log|js|ts|tsx|jsx|py|rs|go|sh|html|css)$/i.test(name)) kind = 'text';
  const m = map[kind];
  return (
    <div style={{
      width: size, height: size, borderRadius: 'var(--r-control)',
      background: m.tint, color: m.fg,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      flexShrink: 0,
    }}>
      <Icon name={m.icon} size={size > 32 ? 20 : 16} stroke={1.6}/>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Toast / haptic
// ─────────────────────────────────────────────────────────────
const ToastCtx = createContext(null);
function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const push = useCallback((msg, kind = 'info') => {
    const id = Math.random().toString(36).slice(2);
    setToasts(t => [...t, { id, msg, kind }]);
    if (navigator.vibrate) try { navigator.vibrate(kind === 'err' ? [40, 30, 40] : 12); } catch (e) {}
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 2600);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div style={{
        position: 'fixed', left: 0, right: 0, bottom: 16,
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8,
        pointerEvents: 'none', zIndex: 100,
      }}>
        {toasts.map(t => (
          <div key={t.id} className="fade-in" style={{
            pointerEvents: 'auto',
            background: t.kind === 'err' ? 'oklch(0.32 0.10 25)' : 'var(--surface-2)',
            color: 'var(--fg)',
            border: '1px solid ' + (t.kind === 'err' ? 'var(--err)' : 'var(--hairline-strong)'),
            padding: '10px 14px', borderRadius: 'var(--r-pill)',
            fontSize: 'var(--t-sm)', fontWeight: 500,
            boxShadow: 'var(--shadow-2)',
            display: 'flex', alignItems: 'center', gap: 8,
          }}>
            {t.kind === 'ok' && <Icon name="check" size={14} color="var(--ok)" />}
            {t.kind === 'err' && <Icon name="close" size={14} color="var(--err)" />}
            {t.msg}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
function useToast() { return useContext(ToastCtx); }

// ─────────────────────────────────────────────────────────────
// Bottom sheet — mobile modal
// ─────────────────────────────────────────────────────────────
function BottomSheet({ open, onClose, title, children, height = '70%' }) {
  // Skip the DOM entirely when closed. We previously relied on a translateY(110%)
  // transform but that broke when the parent's intrinsic height couldn't be
  // determined yet, leaving the sheet stuck on screen at boot.
  if (!open) return null;
  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.55)',
          opacity: 1, pointerEvents: 'auto',
          transition: 'opacity .22s', zIndex: 30,
        }}
      />
      <div className="fade-in" style={{
        position: 'absolute', left: 0, right: 0, bottom: 0,
        background: 'var(--surface-1)', borderTopLeftRadius: 18, borderTopRightRadius: 18,
        borderTop: '1px solid var(--hairline-strong)',
        height, zIndex: 31,
        display: 'flex', flexDirection: 'column',
        boxShadow: '0 -16px 40px rgba(0,0,0,0.5)',
      }}>
        <div style={{ display: 'flex', justifyContent: 'center', padding: 'calc(8px + env(safe-area-inset-top)) 0 4px' }}>
          <span style={{ width: 36, height: 4, borderRadius: 'var(--r-pill)', background: 'var(--hairline-strong)' }} />
        </div>
        {title && (
          <div style={{ padding: '4px 16px 12px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid var(--hairline)' }}>
            <span style={{ fontWeight: 500, fontSize: 'var(--t-body)' }}>{title}</span>
            <IconButton icon="close" label="Close" size={28} onClick={onClose} />
          </div>
        )}
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px 16px calc(16px + env(safe-area-inset-bottom))' }} className="scroll-hide">
          {children}
        </div>
      </div>
    </>
  );
}

// ─────────────────────────────────────────────────────────────
// Modal (desktop)
// ─────────────────────────────────────────────────────────────
// KebabMenu — vertical-dots button that opens an absolute-positioned dropdown
// of {icon, label, onClick, disabled?, danger?} items. Closes on outside click.
function KebabMenu({ items, icon = 'dots-v', size = 32, label = 'More actions' }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('touchstart', onDoc);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('touchstart', onDoc);
    };
  }, [open]);
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <IconButton icon={icon} label={label} size={size} onClick={() => setOpen(o => !o)} />
      {open && (
        <div className="fade-in" style={{
          position: 'absolute', top: 'calc(100% + 4px)', right: 0, zIndex: 30,
          background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
          borderRadius: 'var(--r-md)', boxShadow: 'var(--shadow-2)', padding: 4, minWidth: 180,
        }}>
          {items.map((it, i) => it ? (
            <button key={i} onClick={() => { setOpen(false); it.onClick(); }} disabled={it.disabled}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                padding: '8px 10px', borderRadius: 'var(--r-sm)', background: 'transparent',
                border: 'none', cursor: it.disabled ? 'not-allowed' : 'pointer',
                color: it.danger ? 'var(--err)' : 'var(--fg)', fontFamily: 'inherit',
                fontSize: 'var(--t-sm)', opacity: it.disabled ? 0.4 : 1, textAlign: 'left',
              }}
              onMouseEnter={e => { if (!it.disabled) e.currentTarget.style.background = 'var(--surface-2)'; }}
              onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}
            >
              <Icon name={it.icon} size={14} color={it.danger ? 'var(--err)' : 'var(--fg-2)'} />
              <span>{it.label}</span>
            </button>
          ) : null)}
        </div>
      )}
    </div>
  );
}

function Modal({ open, onClose, title, children, width = 520, height }) {
  if (!open) return null;
  return (
    <div onClick={onClose} style={{
      // `fixed` (not `absolute`) so the backdrop always covers the FULL viewport
      // — including when a Modal is opened from inside another Modal (the editor
      // → button-edit case), where `absolute` would clip the overlay to the
      // parent modal's box. `.fade-in` settles to transform:none so it creates
      // no containing block that would trap this fixed overlay.
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(2px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 40, padding: 20,
    }}>
      <div onClick={e => e.stopPropagation()} className="fade-in" style={{
        background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
        borderRadius: 'var(--r-lg)', width, maxWidth: '100%', maxHeight: '90%',
        // Optional explicit box height. The body is `flex:1` (flex-basis 0), so a
        // tall child inside an overflow:auto body does NOT grow the box — callers
        // that want a tall modal (e.g. the shortcut editor) must size the box.
        ...(height ? { height } : {}),
        boxShadow: 'var(--shadow-2)', display: 'flex', flexDirection: 'column', overflow: 'hidden',
      }}>
        {title && (
          <div style={{ padding: '14px 18px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid var(--hairline)' }}>
            <span style={{ fontWeight: 500, fontSize: 'var(--t-body)' }}>{title}</span>
            <IconButton icon="close" label="Close" size={28} onClick={onClose} />
          </div>
        )}
        <div style={{ flex: 1, overflowY: 'auto' }}>{children}</div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// PullToRefresh — wraps a scrollable; adds top bouncy refresh
// ─────────────────────────────────────────────────────────────
function PullToRefresh({ children, onRefresh, style }) {
  const [pull, setPull] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const startY = useRef(null);
  const ref = useRef(null);

  const onTouchStart = e => {
    if (ref.current && ref.current.scrollTop <= 0) {
      startY.current = e.touches[0].clientY;
    } else { startY.current = null; }
  };
  const onTouchMove = e => {
    if (startY.current == null || refreshing) return;
    const dy = e.touches[0].clientY - startY.current;
    if (dy > 0) {
      setPull(Math.min(80, dy * 0.55));
    }
  };
  const onTouchEnd = async () => {
    if (pull > 50 && !refreshing) {
      setRefreshing(true); setPull(40);
      await Promise.resolve(onRefresh && onRefresh());
      setTimeout(() => { setRefreshing(false); setPull(0); }, 600);
    } else { setPull(0); }
    startY.current = null;
  };

  return (
    // `min-height: 0` is required for `flex: 1` to actually shrink in a column
    // flex container — without it the box grows to its intrinsic content size
    // and pushes siblings (the bottom-nav) up the screen.
    <div style={{ position: 'relative', flex: 1, minHeight: 0, overflow: 'hidden', ...style }}>
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, height: 60,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        opacity: pull / 50, transform: `translateY(${pull - 40}px)`, zIndex: 1,
        color: 'var(--fg-3)', fontSize: 'var(--t-xs)', gap: 6,
      }} className="mono">
        <Icon name="refresh" size={14} style={{ transform: `rotate(${pull * 4}deg)`, transition: refreshing ? 'transform .8s linear' : '' }}/>
        {refreshing ? 'refreshing…' : pull > 50 ? 'release to refresh' : 'pull to refresh'}
      </div>
      <div
        ref={ref}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove} onTouchEnd={onTouchEnd}
        className="scroll-hide"
        style={{
          height: '100%', overflowY: 'auto',
          transform: `translateY(${pull}px)`,
          transition: pull === 0 || refreshing ? 'transform .25s cubic-bezier(.2,.7,.3,1)' : '',
        }}
      >
        {children}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Section list — top tabs / sections registry
// ─────────────────────────────────────────────────────────────
// `group` drives the sidebar / drawer headers — three buckets:
//   workspace — daily tools (chat, todo, apps, file sync)
//   library   — long-lived assets, organised PARA-style
//   system    — host status + diagnostics + global settings
// `mobile` still controls which entries appear in the bottom-nav.
// `primary` is kept for any legacy consumer that hasn't migrated to `group`.
const SECTIONS = [
  // Workspace
  // `agents` replaces the old `orchestrator` sidebar entry (PR: feat/library-agents).
  // `/orchestrator` and `/orchestrator/<sid>` URLs still work — they're the
  // Global agent's chat — they're just no longer linked from the sidebar.
  { key: 'agents',       label: 'Agents',       icon: 'bot',     group: 'workspace', primary: true,  mobile: true },
  { key: 'inbox',        label: 'Inbox',        icon: 'inbox',   group: 'workspace', primary: true,  mobile: true },
  { key: 'ideas',        label: 'Ideas',        icon: 'bulb',    group: 'workspace', primary: true,  mobile: false },
  { key: 'tasks',        label: 'Tasks',        icon: 'tasks',   group: 'workspace', primary: true,  mobile: true },
  { key: 'apps',         label: 'Apps',         icon: 'globe',   group: 'workspace', primary: true },
  { key: 'share',        label: 'Sync',         icon: 'share',   group: 'workspace', primary: true,  mobile: true },
  // Library (PARA)
  { key: 'projects',     label: 'Projects',     icon: 'box',     group: 'library',   primary: false, mobile: true },
  { key: 'areas',        label: 'Areas',        icon: 'home',    group: 'library',   primary: false },
  { key: 'resources',    label: 'Resources',    icon: 'book',    group: 'library',   primary: false },
  { key: 'archive',      label: 'Archive',      icon: 'archive', group: 'library',   primary: false },
  // System — hardware status (relabeled "Server" so the group header isn't
  // duplicated by the item) + log explorer + global settings.
  { key: 'system',       label: 'Server',       icon: 'cpu',     group: 'system',    primary: true,  mobile: true },
  { key: 'skills',       label: 'Skills',       icon: 'sparkle', group: 'system',    primary: false, mobile: true },
  { key: 'scheduler',    label: 'Scheduler',    icon: 'clock',   group: 'system',    primary: false, mobile: true },
  { key: 'secrets',      label: 'Credentials',  icon: 'eye',     group: 'system',    primary: false, mobile: false },
  { key: 'logs',         label: 'Logs',         icon: 'logs',    group: 'system',    primary: true },
  { key: 'settings',     label: 'Settings',     icon: 'cog',     group: 'system',    primary: false },
  // Design-system living styleguide (feature/design-system) — /design.
  { key: 'design',       label: 'Design',       icon: 'layers',  group: 'system',    primary: false },
];

function ComingSoonView({ name }) {
  return (
    <div style={{
      padding: '60px 24px', display: 'flex', flexDirection: 'column',
      alignItems: 'center', gap: 12, color: 'var(--fg-3)',
    }}>
      <div className="orbit-gradient-border orbit-glow-soft" style={{
        // gradient-border owns `background` (padding-box + border-box); --gb-bg is the
        // inner surface, so we DON'T set inline `background` (it would wipe the gradient).
        '--gb-bg': 'var(--accent-soft)',
        width: 56, height: 56, borderRadius: 'var(--r-lg)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'var(--accent)',
      }}><Icon name="sparkle" size={24} /></div>
      <div style={{ fontSize: 18, fontWeight: 500, color: 'var(--fg)' }}>{name}</div>
      <div style={{ fontSize: 'var(--t-sm)' }}>coming soon</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Live-app utilities (added for production port)
// ─────────────────────────────────────────────────────────────
function fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const x = Number(n);
  if (x < 1024) return x + ' B';
  if (x < 1024 * 1024) return Math.round(x / 1024) + ' KB';
  if (x < 1024 * 1024 * 1024) return Math.round(x / (1024 * 1024)) + ' MB';
  if (x < 1024 * 1024 * 1024 * 1024) return (x / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  return (x / (1024 * 1024 * 1024 * 1024)).toFixed(2) + ' TB';
}

function relTime(ts) {
  if (!ts) return '—';
  const now = Math.floor(Date.now() / 1000);
  const diff = now - Number(ts);
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + 'd ago';
  const d = new Date(Number(ts) * 1000);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return yyyy + '-' + mm + '-' + dd;
}

function apiUrl(path) {
  return (window.HUB_BASE_PATH || '') + path;
}

// Encode a relative path for use in a URL: per-segment encodeURIComponent so
// names containing #/?/&/+/% navigate correctly while keeping '/' separators.
function encodePath(rel) {
  return String(rel || '').split('/').map(encodeURIComponent).join('/');
}

// ─────────────────────────────────────────────────────────────
Object.assign(window, {
  Icon, StatusDot, Chip, Card, ProgressBar, Sparkline,
  SectionHeader, IconButton, Button, SearchInput, FileIconBox,
  ToastProvider, useToast, BottomSheet, Modal, KebabMenu, PullToRefresh, SECTIONS,
  ComingSoonView,
  fmtBytes, relTime, apiUrl, encodePath,
});
