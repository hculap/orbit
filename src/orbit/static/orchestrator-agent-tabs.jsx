// orchestrator-agent-tabs.jsx — the enhanced "active agents" tab strip.
//
// Replaces the old presentational AgentTabs in orchestrator.jsx with one that
// adds (desktop-first; mobile degrades gracefully):
//   1. right-click (long-press on touch) context menu: open-in-new-browser-tab,
//      new session, close-agent (close ALL its sessions → tab vanishes → jump
//      to the next tab / agents directory);
//   2. drag-and-drop reorder, persisted server-side (cross-device) via
//      /api/orchestrator/agent-tab-order;
//   3. long-hover flyout listing that agent's sessions (jump to a specific one);
//   4. a trailing "+" that opens a modal of agents WITHOUT an open session, to
//      activate their latest session or start a fresh one.
//
// Self-contained: it does its own fetches (create/close/list sessions, order
// GET/PUT) and navigation (window.buildPath / routerReplace / onSelect). Pure
// ordering/scope/close-target logic lives in window.HubAgentTabs (agent-tabs-
// order.js, unit-tested). Cross-file deps via window globals: Icon, Button,
// Modal, Spinner, useToast, apiUrl (components.jsx); HubAgentTabs (agent-tabs-
// order.js); useHubData, buildPath, routerReplace, HUB_INITIAL_DATA, HUB_BASE_PATH
// (app.jsx / router.jsx). Loads BEFORE orchestrator.jsx, which renders
// window.AgentTabs and feeds it groups/activeKey/onSelect/iconFor/compact/refresh.

const {
  useState: _atUseState, useEffect: _atUseEffect, useRef: _atUseRef,
  useCallback: _atUseCallback, useMemo: _atUseMemo,
} = React;

const _AT = () => window.HubAgentTabs || {};
const _api = (p) => (typeof apiUrl === 'function' ? apiUrl(p) : (window.HUB_BASE_PATH || '') + p);

// ── server-side order store: module-level cache + cross-instance pub/sub ──────
let _orderCache = null;          // null until first load
let _orderLoading = false;
let _orderSeq = 0;               // guards against out-of-order PUT responses
const _orderSubs = new Set();
function _notifyOrder(o) { _orderCache = o; _orderSubs.forEach((fn) => { try { fn(o); } catch (_e) {} }); }

async function _getOrder() {
  try {
    const r = await fetch(_api('/api/orchestrator/agent-tab-order'));
    if (!r.ok) return [];
    const j = await r.json();
    return Array.isArray(j && j.order) ? j.order : [];
  } catch (_e) { return []; }
}
async function _putOrder(keys) {
  try {
    const r = await fetch(_api('/api/orchestrator/agent-tab-order'), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order: keys }),
    });
    if (!r.ok) return null;
    const j = await r.json();
    return Array.isArray(j && j.order) ? j.order : null;
  } catch (_e) { return null; }
}

function useAgentTabOrder() {
  const [order, setOrder] = _atUseState(_orderCache || []);
  _atUseEffect(() => {
    let alive = true;
    const sub = (o) => { if (alive) setOrder(o); };
    _orderSubs.add(sub);
    if (_orderCache !== null) { setOrder(_orderCache); }
    else if (!_orderLoading) {
      _orderLoading = true;
      _getOrder().then((o) => { _orderLoading = false; _notifyOrder(o); });
    }
    return () => { alive = false; _orderSubs.delete(sub); };
  }, []);
  // Optimistic write. A monotonic seq makes only the LATEST save win, so two
  // quick drags can't have an out-of-order PUT response clobber the newer order.
  // Returns a promise → {ok} so the caller can toast on failure (we revert to
  // the authoritative server order when the PUT didn't stick).
  const save = _atUseCallback((keys) => {
    _notifyOrder(keys);  // optimistic, immediately reflected in every instance
    const seq = ++_orderSeq;
    return _putOrder(keys).then((o) => {
      if (seq !== _orderSeq) return { ok: true, stale: true };  // superseded by a newer save
      if (Array.isArray(o)) { _notifyOrder(o); return { ok: true }; }
      return _getOrder().then((auth) => {  // PUT failed → revert to server truth
        if (seq === _orderSeq) _notifyOrder(auth);
        return { ok: false };
      });
    });
  }, []);
  return { order, save };
}

// ── helpers ──────────────────────────────────────────────────────────────────
function _idleLabel(idleS) {
  if (typeof idleS !== 'number' || !isFinite(idleS) || idleS < 0) return '';
  const n = Math.floor(idleS);
  if (n < 60) return n + 's';
  if (n < 3600) return Math.floor(n / 60) + 'm';
  if (n < 86400) return Math.floor(n / 3600) + 'h';
  return Math.floor(n / 86400) + 'd';
}

function _sessionTitle(s) {
  const t = (s && s.title ? String(s.title) : '').trim();
  return t || 'untitled session';
}

// Fetch the agent profile (model + system prompt) for a non-global scope so a
// new session inherits it — mirrors agents-directory.jsx createSession.
async function _agentProfile(scope) {
  if (!scope || scope.isGlobal) return {};
  const m = /^(areas|projects)\/(.+)$/.exec(scope.libId || '');
  if (!m) return {};
  const kind = m[1];
  const enc = String(m[2]).split('/').map(encodeURIComponent).join('/');
  try {
    const r = await fetch(_api(`/api/library/${kind}/${enc}/agent`));
    if (r.ok) { const j = await r.json(); return (j && j.agent) || {}; }
  } catch (_e) { /* defaults */ }
  return {};
}

async function _createSessionForScope(scope) {
  const payload = scope.isGlobal ? {} : { cwd: scope.cwd, lib_id: scope.libId };
  if (!scope.isGlobal) {
    const profile = await _agentProfile(scope);
    if (profile.model) payload.model = profile.model;
    if (profile.system_prompt) payload.extra_system_prompt = profile.system_prompt;
  }
  const r = await fetch(_api('/api/orchestrator/sessions'), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j || !j.id) throw new Error((j && (j.error || j.detail)) || `http ${r.status}`);
  return j.id;
}

async function _latestSessionForScope(scope) {
  const url = scope.isGlobal
    ? '/api/orchestrator/sessions?cwd=__global__'
    : `/api/orchestrator/sessions?cwd=${encodeURIComponent(scope.cwd)}`;
  try {
    const r = await fetch(_api(url));
    if (!r.ok) return null;
    const list = await r.json().catch(() => []);
    // Pick the MOST RECENT session by last message (updated_at). The list
    // endpoint returns pinned-first, but "open the last session" means newest,
    // not whichever happens to be pinned.
    if (Array.isArray(list) && list.length) {
      let top = null;
      for (const s of list) {
        if (s && s.id && (!top || (Number(s.updated_at) || 0) > (Number(top.updated_at) || 0))) top = s;
      }
      if (top) return top.id;
    }
  } catch (_e) { /* fall through */ }
  return null;
}

// ── context-menu chrome (shared by the tab menu + the per-session row menu) ──
function _MenuItem({ it }) {
  return (
    <button role="menuitem" disabled={it.disabled} onClick={it.onClick} title={it.hint || undefined}
      style={{
        display: 'flex', alignItems: 'center', gap: 10, width: '100%',
        padding: '9px 10px', minHeight: 40, border: 'none', background: 'transparent',
        borderRadius: 'var(--r-control)', cursor: it.disabled ? 'default' : 'pointer',
        fontFamily: 'inherit', fontSize: 'var(--t-sm)', textAlign: 'left',
        opacity: it.disabled ? 0.45 : 1,
        color: it.danger ? 'var(--danger, #f87171)' : 'var(--fg)',
      }}
      onMouseEnter={(e) => { if (!it.disabled) e.currentTarget.style.background = 'var(--surface-2)'; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}>
      <Icon name={it.icon} size={14} color={it.danger ? 'var(--danger, #f87171)' : 'var(--fg-2)'} />
      <span>{it.label}</span>
    </button>
  );
}

// Fixed-position, viewport-clamped menu that dismisses on outside-click /
// Escape / scroll / resize. Renders `items` (via _MenuItem) then `children`.
function _FloatingMenu({ x, y, onClose, items, children, minWidth = 210, maxWidth = 300 }) {
  const ref = _atUseRef(null);
  const [pos, setPos] = _atUseState({ left: x, top: y });
  _atUseEffect(() => {
    const el = ref.current; if (!el) return;
    const r = el.getBoundingClientRect(); const pad = 8;
    let left = x, top = y;
    if (left + r.width + pad > window.innerWidth) left = Math.max(pad, window.innerWidth - r.width - pad);
    if (top + r.height + pad > window.innerHeight) top = Math.max(pad, window.innerHeight - r.height - pad);
    setPos({ left, top });
  }, [x, y]);
  _atUseEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    const onScroll = () => onClose();
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onScroll, true);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', onScroll, true);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [onClose]);
  return (
    <>
      <div onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }}
        style={{ position: 'fixed', inset: 0, zIndex: 9998 }} />
      <div ref={ref} role="menu" style={{
        position: 'fixed', left: pos.left, top: pos.top, zIndex: 9999,
        minWidth, maxWidth, padding: 4,
        background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
        borderRadius: 'var(--r-control)', boxShadow: 'var(--shadow-2, 0 8px 28px rgba(0,0,0,.4))',
        maxHeight: '70vh', overflowY: 'auto',
      }}>
        {(items || []).map((it, i) => <_MenuItem key={i} it={it} />)}
        {children}
      </div>
    </>
  );
}

// ── tab context menu (right-click / long-press on a TAB) ─────────────────────
function _TabContextMenu({ group, scope, x, y, compact, onClose, onSelect, actions }) {
  const sessions = (group && group.sessions) || [];
  // Only non-keep-alive sessions actually get closed; count + disabled reflect it.
  const closableCount = sessions.filter((s) => s && !s.persistent).length;
  const items = [
    { icon: 'external', label: 'Otwórz w nowej karcie',
      disabled: !group.topSessionId, onClick: () => { onClose(); actions.openInNewTab(group); } },
    { icon: 'plus', label: 'Nowa sesja w tym agencie',
      onClick: () => { onClose(); actions.newSession(scope); } },
    { icon: 'close',
      label: `Zamknij agenta${closableCount ? ` (${closableCount})` : ''}`,
      hint: closableCount === 0 ? 'wszystkie sesje keep-alive' : undefined,
      disabled: closableCount === 0,
      danger: true, onClick: () => { onClose(); actions.closeAgent(group); } },
  ];
  // On touch (compact) there is no hover flyout, so the agent's sessions live in
  // the menu (tap to open).
  const footer = (compact && sessions.length > 0) ? (
    <>
      <div style={{ height: 1, background: 'var(--hairline)', margin: '4px 6px' }} />
      <div className="mono" style={{ padding: '4px 10px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Sesje</div>
      {sessions.map((s) => (
        <button key={s.session_id} role="menuitem"
          onClick={() => { onClose(); onSelect(s.session_id); }}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, width: '100%',
            padding: '8px 10px', minHeight: 40, border: 'none', background: 'transparent',
            borderRadius: 'var(--r-control)', cursor: 'pointer',
            fontFamily: 'inherit', fontSize: 'var(--t-sm)', textAlign: 'left', color: 'var(--fg)',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--surface-2)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}>
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{_sessionTitle(s)}</span>
          {_idleLabel(s.idle_s) && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>{_idleLabel(s.idle_s)}</span>}
        </button>
      ))}
    </>
  ) : null;
  return <_FloatingMenu x={x} y={y} onClose={onClose} items={items} minWidth={210} maxWidth={280}>{footer}</_FloatingMenu>;
}

// ── per-session row context menu (right-click a session in the flyout) ───────
// Mirrors the left-panel SessionContextMenu: open-in-new-tab / rename / close /
// delete, with close+delete disabled for keep-alive sessions.
function _SessionRowMenu({ session, x, y, onClose, onOpenInNewTab, actions }) {
  const persistent = !!(session && session.persistent);
  const sid = session && session.session_id;
  const keepHint = persistent ? 'Najpierw wyłącz keep-alive' : undefined;
  const a = actions || {};
  const items = [
    { icon: 'external', label: 'Otwórz w nowej karcie',
      disabled: !sid, onClick: () => { onClose(); if (onOpenInNewTab) onOpenInNewTab(sid); } },
    { icon: 'pencil', label: 'Zmień nazwę',
      disabled: !a.rename, onClick: () => { onClose(); if (a.rename) a.rename(sid); } },
    { icon: 'close', label: 'Zamknij sesję',
      disabled: persistent || !a.close, hint: keepHint, onClick: () => { onClose(); if (a.close) a.close(sid); } },
    { icon: 'trash', label: 'Usuń', danger: true,
      disabled: persistent || !a.delete, hint: keepHint, onClick: () => { onClose(); if (a.delete) a.delete(sid); } },
  ];
  return <_FloatingMenu x={x} y={y} onClose={onClose} items={items} minWidth={200} />;
}

// ── long-hover sessions flyout (desktop) ─────────────────────────────────────
function _TabSessionsFlyout({ group, rect, onSelect, onRowContextMenu, onEnter, onLeave }) {
  const sessions = (group && group.sessions) || [];
  if (!sessions.length || !rect) return null;
  const left = Math.min(rect.left, window.innerWidth - 300);
  const top = rect.bottom + 4;
  return (
    <div role="menu" onMouseEnter={onEnter} onMouseLeave={onLeave}
      style={{
        position: 'fixed', left: Math.max(8, left), top, zIndex: 9990,
        minWidth: 220, maxWidth: 300, padding: 4,
        background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
        borderRadius: 'var(--r-control)', boxShadow: 'var(--shadow-2, 0 8px 28px rgba(0,0,0,.4))',
        maxHeight: '60vh', overflowY: 'auto',
      }}>
      <div className="mono" style={{ padding: '4px 10px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {group.name} · {sessions.length} {sessions.length === 1 ? 'sesja' : 'sesji'}
      </div>
      {sessions.map((s) => (
        <button key={s.session_id} role="menuitem" onClick={() => onSelect(s.session_id)}
          title="Prawy klik: opcje sesji"
          onContextMenu={onRowContextMenu ? ((e) => { e.preventDefault(); onRowContextMenu(s, e); }) : undefined}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, width: '100%',
            padding: '8px 10px', border: 'none', background: 'transparent',
            borderRadius: 'var(--r-control)', cursor: 'pointer',
            fontFamily: 'inherit', fontSize: 'var(--t-sm)', textAlign: 'left', color: 'var(--fg)',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--surface-2)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}>
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{_sessionTitle(s)}</span>
          {_idleLabel(s.idle_s) && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', flexShrink: 0 }}>{_idleLabel(s.idle_s)}</span>}
        </button>
      ))}
    </div>
  );
}

// ── "+" add-agent modal: agents without an open session ──────────────────────
function _AddAgentModal({ open, onClose, activeKeys, onActivate, onNew }) {
  const hubHook = window.useHubData || (() => null);
  const hub = hubHook() || window.HUB_INITIAL_DATA || {};
  const [busyKey, setBusyKey] = _atUseState(null);
  const [query, setQuery] = _atUseState('');
  const searchRef = _atUseRef(null);

  // Reset + autofocus the search the moment the modal opens, so the user can
  // type a name straight away.
  _atUseEffect(() => {
    if (!open) return;
    setQuery('');
    const id = setTimeout(() => { if (searchRef.current) searchRef.current.focus(); }, 30);
    return () => clearTimeout(id);
  }, [open]);

  // Escape closes from anywhere in the modal, not only while the search input
  // has focus (the shared Modal has no Escape handler of its own).
  _atUseEffect(() => {
    if (!open) return;
    const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); onClose(); } };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, onClose]);

  const candidates = _atUseMemo(() => {
    const AT = _AT();
    const keyOf = AT.agentKey || ((lib, name) => lib || ('@' + (name || 'Global')));
    const out = [];
    if (!activeKeys.has('@Global')) {
      out.push({ key: '@Global', name: 'Global', icon: '🤖', libId: '' });
    }
    const add = (item, kind) => {
      const libId = `${kind === 'Area' ? 'areas' : 'projects'}/${item.lib_id || item.name}`;
      const key = keyOf(libId, item.label || item.name);
      if (activeKeys.has(key)) return;
      out.push({ key, name: item.label || item.name, icon: item.icon || (kind === 'Area' ? '📂' : '📦'), libId });
    };
    for (const a of (hub.areas || [])) add(a, 'Area');
    for (const p of (hub.projects || [])) add(p, 'Project');
    return out;
  }, [hub, activeKeys]);

  if (!open) return null;
  const scopeOf = (c) => (_AT().scopeFromLibId ? _AT().scopeFromLibId(c.libId) : { cwd: null, libId: '', isGlobal: true });
  const run = (fn, key) => async () => {
    if (busyKey) return;
    setBusyKey(key);
    try { await fn(); onClose(); } finally { setBusyKey(null); }
  };
  const q = query.trim().toLowerCase();
  const filtered = q ? candidates.filter((c) => c.name.toLowerCase().includes(q)) : candidates;

  // Enter opens the top match's latest session; Shift+Enter starts a fresh one
  // (mirrors clicking the row vs the "+"). Escape closes.
  const onSearchKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return; }
    if (e.key !== 'Enter') return;
    // Only act on Enter once the user has typed something — otherwise a bare
    // Enter right after opening would pick whatever is first (Global), which the
    // user didn't choose. Type to disambiguate; the top match is highlighted.
    if (!q) return;
    const c = filtered[0];
    if (!c || busyKey) return;
    e.preventDefault();
    const scope = scopeOf(c);
    run(() => (e.shiftKey ? onNew(scope) : onActivate(scope)), c.key)();
  };

  return (
    <Modal open={open} onClose={onClose} title="Otwórz agenta" width={460}>
      <div style={{ marginBottom: 10 }}>
        <input
          ref={searchRef} value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onSearchKey}
          placeholder="Szukaj agenta…  (Enter = ostatnia sesja · Shift+Enter = nowa)"
          style={{
            width: '100%', boxSizing: 'border-box', padding: '10px 12px',
            background: 'var(--surface-2)', border: '1px solid var(--hairline)',
            borderRadius: 'var(--r-control)', color: 'var(--fg)',
            fontFamily: 'inherit', fontSize: 'var(--t-sm)', outline: 'none',
          }}
        />
      </div>
      {candidates.length === 0 ? (
        <div style={{ padding: 18, fontSize: 'var(--t-sm)', color: 'var(--fg-3)', textAlign: 'center' }}>
          Wszyscy agenci mają już otwartą sesję.
        </div>
      ) : filtered.length === 0 ? (
        <div style={{ padding: 18, fontSize: 'var(--t-sm)', color: 'var(--fg-3)', textAlign: 'center' }}>
          Brak agenta pasującego do „{query}".
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: '60vh', overflowY: 'auto' }}>
          {filtered.map((c, i) => {
            const scope = scopeOf(c);
            const busy = busyKey === c.key;
            const isTop = !!q && i === 0;  // the row Enter will open
            return (
              <div key={c.key} style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
                borderRadius: 'var(--r-control)',
                border: '1px solid ' + (isTop ? 'var(--accent-line)' : 'var(--hairline)'),
                background: isTop ? 'var(--accent-soft)' : 'transparent',
              }}>
                <span style={{ fontSize: 18, flexShrink: 0 }}>{c.icon}</span>
                <button onClick={run(() => onActivate(scope), c.key)} disabled={!!busyKey}
                  title="Otwórz ostatnią sesję (lub utwórz nową)"
                  style={{
                    flex: 1, textAlign: 'left', background: 'transparent', border: 'none',
                    cursor: busyKey ? 'default' : 'pointer', fontFamily: 'inherit',
                    fontSize: 'var(--t-sm)', color: 'var(--fg)', overflow: 'hidden',
                    textOverflow: 'ellipsis', whiteSpace: 'nowrap', minHeight: 32,
                  }}>
                  {c.name}
                </button>
                {busy ? <Spinner inline size={13} />
                  : <IconButton icon="plus" label="Nowa sesja" size={30}
                      onClick={run(() => onNew(scope), c.key)} />}
              </div>
            );
          })}
        </div>
      )}
    </Modal>
  );
}

// ── the strip ────────────────────────────────────────────────────────────────
function AgentTabs({ groups, activeKey, onSelect, iconFor, compact, refresh, refreshPool, runningById, applyRemovalDecision, sessionActions }) {
  const toast = (typeof useToast === 'function') ? useToast() : null;
  const { order, save } = useAgentTabOrder();
  const [hoverKey, setHoverKey] = _atUseState(null);
  const [menu, setMenu] = _atUseState(null);        // {group, scope, x, y}
  const [rowMenu, setRowMenu] = _atUseState(null);  // {session, x, y} — per-session row menu
  const [flyout, setFlyout] = _atUseState(null);    // {key, rect}
  const [dragKey, setDragKey] = _atUseState(null);
  const [dropKey, setDropKey] = _atUseState(null);
  const [addOpen, setAddOpen] = _atUseState(false);

  const hoverTimer = _atUseRef(null);
  const flyoutCloseTimer = _atUseRef(null);
  const longPressTimer = _atUseRef(null);
  const longPressFired = _atUseRef(false);  // suppress the synthetic click after a long-press
  const rowMenuOpen = _atUseRef(false);     // freeze the flyout while a row context menu is up

  const closeMenu = _atUseCallback(() => setMenu(null), []);
  const closeAdd = _atUseCallback(() => setAddOpen(false), []);  // stable ref → modal's Escape effect doesn't re-subscribe each render

  // Ctrl/⌘+Shift+N (dispatched from orchestrator.jsx's keyboard handler) opens
  // the add-agent modal.
  _atUseEffect(() => {
    const onOpen = () => setAddOpen(true);
    window.addEventListener('hub:open-agent-modal', onOpen);
    return () => window.removeEventListener('hub:open-agent-modal', onOpen);
  }, []);

  const ordered = _atUseMemo(() => {
    const AT = _AT();
    return AT.applyOrder ? AT.applyOrder(groups || [], order) : (groups || []);
  }, [groups, order]);

  const scopeFor = _atUseCallback((g) => {
    const AT = _AT();
    return AT.scopeFromLibId ? AT.scopeFromLibId(g.libId) : { cwd: null, libId: '', isGlobal: true };
  }, []);

  // Refresh BOTH the session list AND the live pool (the tab strip is driven by
  // poolSessions). Without the pool refresh a just-closed tab lingers up to ~8s
  // until the next poll. Both props are optional.
  const refreshSoon = _atUseCallback(() => {
    if (typeof refresh === 'function') { try { refresh(); } catch (_e) {} }
    if (typeof refreshPool === 'function') { try { refreshPool(); } catch (_e) {} }
  }, [refresh, refreshPool]);

  // ── actions ──
  const openSessionInNewTab = _atUseCallback((sid) => {
    if (!sid) return;
    const path = window.buildPath ? window.buildPath({ section: 'orchestrator', sessionId: sid }) : null;
    if (!path) return;
    window.open((window.HUB_BASE_PATH || '') + path, '_blank');
  }, []);
  const openInNewTab = _atUseCallback((g) => { openSessionInNewTab(g && g.topSessionId); }, [openSessionInNewTab]);

  const newSession = _atUseCallback(async (scope) => {
    try {
      const id = await _createSessionForScope(scope);
      if (typeof onSelect === 'function') onSelect(id);
      refreshSoon();
      toast && toast('Nowa sesja', 'ok');
    } catch (e) { toast && toast('Nie udało się utworzyć sesji: ' + (e.message || e), 'err'); }
  }, [onSelect, refreshSoon, toast]);

  const activateLatest = _atUseCallback(async (scope) => {
    try {
      const id = (await _latestSessionForScope(scope)) || (await _createSessionForScope(scope));
      if (typeof onSelect === 'function') onSelect(id);
      refreshSoon();
    } catch (e) { toast && toast('Nie udało się otworzyć agenta: ' + (e.message || e), 'err'); }
  }, [onSelect, refreshSoon, toast]);

  const closeAgent = _atUseCallback(async (g) => {
    const AT = _AT();
    const plan = AT.planAgentClose
      ? AT.planAgentClose(g, ordered, activeKey)
      : (() => {  // fallback must still spare keep-alive sessions
          const all = (g.sessions || []).filter((s) => s && s.session_id);
          const closableIds = all.filter((s) => !s.persistent).map((s) => s.session_id);
          return { closableIds, spared: all.length - closableIds.length, willVanish: false, decision: { action: 'none' } };
        })();
    const { closableIds, spared, decision } = plan;
    if (closableIds.length === 0) {
      toast && toast('Wszystkie sesje agenta są keep-alive — nic nie zamknięto', 'ok');
      return;
    }
    // Mark each closed so the terminal doesn't re-spawn one of them on the way
    // out (terminal mode auto-warms the displayed session).
    const _guard = window.HubSessionListOrder && window.HubSessionListOrder.closedGuard;
    if (_guard) closableIds.forEach((id) => _guard.mark(id));
    // kill=true → actually END each REPL (free the RAM), keeping the transcript.
    const results = await Promise.allSettled(closableIds.map((id) =>
      fetch(_api('/api/orchestrator/sessions/' + encodeURIComponent(id) + '/close?kill=true'), { method: 'POST' })));
    const failed = results.filter((r) => r.status === 'rejected' || !(r.value && r.value.ok)).length;
    if (failed === closableIds.length) {
      toast && toast('Nie udało się zamknąć agenta', 'err');
      refreshSoon();
      return;
    }
    const closed = closableIds.length - failed;
    toast && toast(`Zamknięto agenta (${closed})${spared ? ` · ${spared} keep-alive zachowano` : ''}`, 'ok');
    // Route navigation through the SAME teardown the session-close path uses so
    // the 'agents' branch also tears down the killed active session's stream +
    // clears activeId (not just routerReplace). Fallback hand-rolls it.
    if (typeof applyRemovalDecision === 'function') {
      applyRemovalDecision(decision);
    } else if (decision.action === 'session' && decision.id) {
      if (typeof onSelect === 'function') onSelect(decision.id);
    } else if (decision.action === 'agents') {
      if (window.routerReplace && window.buildPath) window.routerReplace(window.buildPath({ section: 'agents' }));
    }
    refreshSoon();
  }, [ordered, activeKey, onSelect, refreshSoon, toast, applyRemovalDecision]);

  const menuActions = _atUseMemo(() => ({ openInNewTab, newSession, closeAgent }), [openInNewTab, newSession, closeAgent]);

  // ── hover-intent flyout (desktop only) ──
  const clearHoverTimers = () => {
    if (hoverTimer.current) { clearTimeout(hoverTimer.current); hoverTimer.current = null; }
    if (flyoutCloseTimer.current) { clearTimeout(flyoutCloseTimer.current); flyoutCloseTimer.current = null; }
  };
  const onTabEnter = (g, e) => {
    setHoverKey(g.key);
    if (compact) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (flyoutCloseTimer.current) { clearTimeout(flyoutCloseTimer.current); flyoutCloseTimer.current = null; }
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(() => {
      if ((g.sessions || []).length > 0) setFlyout({ key: g.key, rect });
    }, 450);
  };
  const onTabLeave = (g) => {
    setHoverKey((k) => (k === g.key ? null : k));
    if (compact) return;
    if (hoverTimer.current) { clearTimeout(hoverTimer.current); hoverTimer.current = null; }
    flyoutCloseTimer.current = setTimeout(() => setFlyout(null), 220);
  };
  _atUseEffect(() => () => clearHoverTimers(), []);

  // ── long-press (touch) → context menu ──
  const onTabTouchStart = (g, e) => {
    if (longPressTimer.current) clearTimeout(longPressTimer.current);
    longPressFired.current = false;
    const t = e.touches && e.touches[0];
    const x = t ? t.clientX : 0, y = t ? t.clientY : 0;
    longPressTimer.current = setTimeout(() => {
      longPressTimer.current = null;
      longPressFired.current = true;  // makes the trailing synthetic click a no-op
      setFlyout(null);
      setMenu({ group: g, scope: scopeFor(g), x, y });
    }, 500);
  };
  const cancelLongPress = () => { if (longPressTimer.current) { clearTimeout(longPressTimer.current); longPressTimer.current = null; } };
  // After a long-press the browser still dispatches a synthetic click on the
  // original tab — swallow it so the menu doesn't also switch the session.
  const onTabClick = (g) => {
    if (longPressFired.current) { longPressFired.current = false; return; }
    if (g.topSessionId && typeof onSelect === 'function') onSelect(g.topSessionId);
  };

  // ── drag-and-drop reorder (desktop only) ──
  const onDrop = (targetKey) => {
    const from = dragKey;
    setDragKey(null); setDropKey(null);
    if (!from || from === targetKey) return;
    const AT = _AT();
    const keys = ordered.map((g) => g.key);
    const next = AT.moveKey ? AT.moveKey(keys, from, targetKey) : keys;
    Promise.resolve(save(next)).then((r) => {
      if (r && r.ok === false) toast && toast('Nie zapisano kolejności tabów', 'err');
    });
  };

  if (!groups || groups.length === 0) return null;
  const flyoutGroup = flyout ? ordered.find((g) => g.key === flyout.key) : null;

  return (
    <div role="tablist" aria-label="Aktywni agenci" className="scroll-hide"
      style={{
        display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0,
        padding: compact ? '4px 8px' : '6px 12px',
        borderBottom: '1px solid var(--hairline)',
        overflowX: 'auto', overflowY: 'hidden', background: 'var(--surface-1)',
      }}>
      {ordered.map((g) => {
        const active = g.key === activeKey;
        const hovered = !active && g.key === hoverKey;
        const isDrop = dropKey === g.key && dragKey && dragKey !== g.key;
        const icon = (typeof iconFor === 'function' ? iconFor(g.libId) : '') || '';
        // Gently pulse the tab while ANY of the agent's sessions has an in-flight
        // turn; stops when they all finish. Checks the pool sessions AND the
        // group's top/active session (so a synthesized current-scope group with
        // no pool slots still pulses). Pulse is suppressed on the tab being
        // dragged so the drag-dim stays visible.
        const busy = !!(runningById && (
          (g.sessions || []).some((s) => s && runningById.has(s.session_id))
          || (g.topSessionId && runningById.has(g.topSessionId))
        ));
        const pulse = busy && dragKey !== g.key;
        return (
          <button
            key={g.key} role="tab" aria-selected={active}
            className={pulse ? 'hub-think-tab' : undefined}
            draggable={!compact}
            title={g.name + (g.count > 1 ? ` · ${g.count} sesji` : '') + (busy ? ' · tura w toku' : '') + ' — prawy klik: opcje'}
            onClick={() => onTabClick(g)}
            onContextMenu={(e) => { e.preventDefault(); setFlyout(null); setMenu({ group: g, scope: scopeFor(g), x: e.clientX, y: e.clientY }); }}
            onMouseEnter={(e) => onTabEnter(g, e)}
            onMouseLeave={() => onTabLeave(g)}
            onTouchStart={(e) => onTabTouchStart(g, e)}
            onTouchEnd={cancelLongPress}
            onTouchMove={cancelLongPress}
            onTouchCancel={cancelLongPress}
            onDragStart={(e) => { if (compact) return; setDragKey(g.key); try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', g.key); } catch (_e) {} }}
            onDragOver={(e) => { if (dragKey) { e.preventDefault(); if (dropKey !== g.key) setDropKey(g.key); } }}
            onDragLeave={() => setDropKey((k) => (k === g.key ? null : k))}
            onDrop={(e) => { e.preventDefault(); onDrop(g.key); }}
            onDragEnd={() => { setDragKey(null); setDropKey(null); }}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              flexShrink: 0, maxWidth: 200, cursor: 'pointer',
              padding: compact ? '8px 12px' : '5px 10px', minHeight: compact ? 40 : undefined,
              borderRadius: 'var(--r-control)',
              fontFamily: 'inherit', fontSize: 'var(--t-sm)',
              fontWeight: active ? 600 : 500,
              color: active ? 'var(--accent)' : 'var(--fg-3)',
              background: active ? 'var(--accent-soft)' : (hovered ? 'var(--surface-2)' : 'transparent'),
              border: '1px solid ' + (isDrop ? 'var(--accent)' : (active ? 'var(--accent-line)' : 'transparent')),
              opacity: dragKey === g.key ? 0.5 : 1,
              // pan-x keeps the strip horizontally scrollable while long-press
              // signals intent; the callout/select suppressions stop the native
              // iOS long-press menu from racing our context menu.
              touchAction: 'pan-x', WebkitTouchCallout: 'none', userSelect: 'none',
              transition: 'background 0.12s ease, color 0.12s ease, opacity 0.12s ease',
            }}>
            <span style={{ fontSize: 14, lineHeight: 1, flexShrink: 0 }}>
              {icon ? icon : <Icon name="bot" size={13} stroke={1.8} />}
            </span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{g.name}</span>
            {g.count > 1 && (
              <span className="mono" style={{
                flexShrink: 0, fontSize: 'var(--t-2xs)', padding: '0 5px',
                borderRadius: 999, lineHeight: '15px',
                background: active ? 'var(--accent-line)' : 'var(--surface-3)',
                color: active ? 'var(--accent)' : 'var(--fg-4)',
              }}>{g.count}</span>
            )}
          </button>
        );
      })}

      {/* trailing "+" → add-agent modal */}
      <button type="button" title="Otwórz innego agenta" aria-label="Otwórz innego agenta"
        onClick={() => setAddOpen(true)}
        style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          flexShrink: 0, width: compact ? 40 : 28, height: compact ? 40 : 28,
          marginLeft: 2, cursor: 'pointer',
          padding: 0, borderRadius: 'var(--r-control)', color: 'var(--fg-3)',
          background: 'transparent', border: '1px solid var(--hairline)',
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--surface-2)'; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}>
        <Icon name="plus" size={15} />
      </button>

      {menu && (
        <_TabContextMenu group={menu.group} scope={menu.scope} x={menu.x} y={menu.y} compact={compact}
          onClose={closeMenu} onSelect={onSelect} actions={menuActions} />
      )}
      {flyoutGroup && (
        <_TabSessionsFlyout group={flyoutGroup} rect={flyout.rect} onSelect={(id) => { setFlyout(null); onSelect(id); }}
          onRowContextMenu={(s, e) => {
            // Keep the flyout (session list) VISIBLE behind the row menu — just
            // freeze it: pin the close timer so the mouse moving onto the menu
            // doesn't dismiss it.
            rowMenuOpen.current = true;
            if (flyoutCloseTimer.current) { clearTimeout(flyoutCloseTimer.current); flyoutCloseTimer.current = null; }
            setRowMenu({ session: s, x: e.clientX, y: e.clientY });
          }}
          onEnter={() => { if (flyoutCloseTimer.current) { clearTimeout(flyoutCloseTimer.current); flyoutCloseTimer.current = null; } }}
          onLeave={() => { if (rowMenuOpen.current) return; flyoutCloseTimer.current = setTimeout(() => setFlyout(null), 200); }} />
      )}
      {rowMenu && (
        <_SessionRowMenu session={rowMenu.session} x={rowMenu.x} y={rowMenu.y}
          onClose={() => { rowMenuOpen.current = false; setRowMenu(null); setFlyout(null); }}
          onOpenInNewTab={openSessionInNewTab} actions={sessionActions} />
      )}
      <_AddAgentModal open={addOpen} onClose={closeAdd}
        activeKeys={new Set((groups || []).map((g) => g.key))}
        onActivate={activateLatest} onNew={newSession} />
    </div>
  );
}

Object.assign(window, { AgentTabs });
