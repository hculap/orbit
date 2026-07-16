// app.jsx — DataProvider + responsive shell (mobile/desktop) + command palette + detail router.
// Loaded after components.jsx, sections.jsx, orchestrator.jsx, details.jsx.

const { useState, useEffect, useRef, useContext, createContext, useCallback, useMemo } = React;

// ─────────────────────────────────────────────────────────────
// Push-notification plumbing: IndexedDB ui-state mirror + service worker
// ─────────────────────────────────────────────────────────────
//
// The service worker (`/static/sw.js`) reads `ui-state` from this DB to decide
// whether to suppress notifications when the user is already looking at the
// orchestrator. Schema is intentionally minimal — a single keyed entry under
// store `state`, key `ui-state`, value `{ section, visible, lastFocusTs }`.

function openHubIdb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('hub', 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('state')) db.createObjectStore('state');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function writeUiState(section) {
  try {
    const db = await openHubIdb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction('state', 'readwrite');
      tx.objectStore('state').put({
        section,
        visible: document.visibilityState === 'visible',
        lastFocusTs: Date.now(),
      }, 'ui-state');
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } catch (e) {
    // IDB may be unavailable (private mode, quota, etc.) — SW falls back to
    // showing all notifications, which is the safer default.
  }
}

// Custom hook used by both MobileHub and DesktopHub. Wires up:
//   1. SW registration (idempotent — register() is safe to call repeatedly).
//   2. IDB ui-state mirror on every section change + visibility/focus events.
//   3. Notification-click messages from the SW → switch section + dispatch
//      `hub:open-session` for orchestrator.jsx to consume.
//   4. Initial `?session=<id>` query param read on mount → same dispatch.
function useOrchestratorPushBridge(section, setSection, setUpdateAvailable) {
  // Effect 1: register SW + listen for notification-click and update messages.
  //
  // The SW lives at the origin root (/sw.js) so its scope covers the
  // entire app (it handles BOTH push notifications and stale-while-
  // revalidate caching of static assets). Any older registration at
  // /static/sw.js from a previous build is unregistered first so we
  // don't leave an orphaned narrower-scoped SW behind.
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;
    const base = window.HUB_BASE_PATH || '';
    const swUrl = base + '/sw.js';
    // No explicit scope — defaults to the directory of the script (= /).
    (async () => {
      try {
        const regs = await navigator.serviceWorker.getRegistrations();
        for (const reg of regs) {
          // Legacy /static/-scoped registration from previous deploys.
          if (reg.scope && reg.scope.endsWith('/static/')) {
            try { await reg.unregister(); } catch (e) { /* best-effort */ }
          }
        }
      } catch (e) { /* getRegistrations may reject in private mode */ }
      try {
        await navigator.serviceWorker.register(swUrl);
      } catch (err) {
        console.warn('[sw] register failed', err);
      }
    })();

    const onMessage = (event) => {
      const msg = event.data;
      if (!msg) return;
      if (msg.type === 'notification-click' && msg.session_id) {
        setSection('orchestrator');
        window.dispatchEvent(new CustomEvent('hub:open-session', {
          detail: { session_id: msg.session_id },
        }));
      } else if (msg.type === 'update_available' && typeof setUpdateAvailable === 'function') {
        setUpdateAvailable(true);
      }
    };
    navigator.serviceWorker.addEventListener('message', onMessage);
    return () => navigator.serviceWorker.removeEventListener('message', onMessage);
    // setSection / setUpdateAvailable are stable (same closure for the lifetime of the shell).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Effect 2: mirror current section + visibility into IDB.
  useEffect(() => {
    writeUiState(section);
    const onVis = () => writeUiState(section);
    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('focus', onVis);
    window.addEventListener('blur', onVis);
    return () => {
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('focus', onVis);
      window.removeEventListener('blur', onVis);
    };
  }, [section]);

  // Effect 3: handle `?session=<id>` from a fresh window opened by the SW.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const sid = params.get('session');
    if (!sid) return;
    setSection('orchestrator');
    window.dispatchEvent(new CustomEvent('hub:open-session', {
      detail: { session_id: sid },
    }));
    // Strip the param so a reload doesn't keep re-firing it.
    const url = new URL(window.location.href);
    url.searchParams.delete('session');
    window.history.replaceState({}, '', url.pathname + (url.search || ''));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

// Toast banner shown when the service worker pulled in a newer build of
// the static assets. Click → reload the page → SW promotes its just-updated
// cache. Dismissable so a user mid-task isn't forced to refresh; the toast
// is also one reload away (the new bytes are already in the cache).
function UpdateToast({ visible, onDismiss }) {
  if (!visible) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: 'fixed',
        bottom: 20,
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 9999,
        background: 'var(--bg-2, #1a1a1a)',
        color: 'var(--fg, #fff)',
        border: '1px solid var(--hairline, #2a2a2a)',
        borderRadius: 'var(--r-control)',
        padding: '10px 14px',
        fontSize: 'var(--t-sm)',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
      }}
    >
      <span>Nowa wersja dashboardu jest gotowa.</span>
      <button
        type="button"
        onClick={() => window.location.reload()}
        style={{
          background: 'var(--accent, #3b82f6)',
          color: '#fff',
          border: 'none',
          borderRadius: 'var(--r-sm)',
          padding: '6px 12px',
          fontSize: 'var(--t-cap)',
          cursor: 'pointer',
          fontFamily: 'inherit',
        }}
      >
        Odśwież
      </button>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        style={{
          background: 'transparent',
          color: 'var(--fg-3, #888)',
          border: 'none',
          padding: 4,
          cursor: 'pointer',
          fontFamily: 'inherit',
          fontSize: 'var(--t-md)',
        }}
      >
        ×
      </button>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// DataProvider — single source of truth for areas/projects/apps/resources + live system polling
// ─────────────────────────────────────────────────────────────
const HubDataContext = createContext(null);

function mapLive(resp) {
  if (!resp) return null;
  const ts = resp.ts;
  const ts_ts = typeof ts === 'number' ? ts : Date.now() / 1000;
  const tail = resp.tailscale || {};
  return {
    ts: ts_ts,
    cpu: resp.cpu || { load_1m: 0, load_5m: 0, load_15m: 0, cpu_count: 0 },
    memory: resp.memory || { used_bytes: 0, total_bytes: 0, percent: 0, swap_used_bytes: 0, swap_total_bytes: 0, swap_percent: 0 },
    disk: resp.disk_root || { used_bytes: 0, total_bytes: 0, percent: 0 },
    services: resp.services || [],
    peers: tail.peers || [],
    peers_total: tail.peers_total || 0,
    peers_online: tail.peers_online || 0,
    self_ip: tail.self_ip || '',
    self_dns: tail.self_dns || '',
    // Carry the tmux-pool diagnostics through verbatim — SystemView reads
    // live.tmux directly. Without this passthrough the card fell back to
    // {} and rendered "0 aktywne · 0 stałych slotów · idle TTL 0s".
    tmux: resp.tmux || null,
  };
}

function DataProvider({ children }) {
  const initial = window.HUB_INITIAL_DATA || {};
  // Areas + projects are mutable now: CRUD operations dispatch a
  // `library:reload` event and we refetch /api/data to replace these slices.
  // Apps / resources stay static — they don't have a CRUD UI yet.
  const [areas, setAreas] = useState(initial.areas || []);
  const [projects, setProjects] = useState(initial.projects || []);
  const [apps] = useState(initial.apps || []);
  const [resources] = useState(initial.resources || []);
  const [host] = useState(initial.host || { name: '', domain: '', region: '', ip: '' });
  const [system] = useState(initial.system || { hostname: '', uptime_s: 0 });
  const [live, setLive] = useState(null);
  const [loading, setLoading] = useState(false);
  const cpuHistRef = useRef([]);
  const memHistRef = useRef([]);
  const [, forceTick] = useState(0);

  const fetchLive = useCallback(async () => {
    try {
      setLoading(true);
      const res = await fetch(apiUrl('/api/system'), { headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      const mapped = mapLive(json);
      if (mapped) {
        cpuHistRef.current = [...cpuHistRef.current, (mapped.cpu.load_1m || 0) * 100].slice(-20);
        memHistRef.current = [...memHistRef.current, mapped.memory.percent || 0].slice(-20);
        setLive({ ...mapped, cpuHistory: cpuHistRef.current, memHistory: memHistRef.current });
      }
    } catch (err) {
      console.error('live poll failed:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchLive();
    let timer = null;
    const tick = () => {
      if (document.visibilityState === 'visible') fetchLive();
    };
    const start = () => {
      if (timer) return;
      timer = setInterval(tick, 5000);
    };
    const stop = () => {
      if (!timer) return;
      clearInterval(timer);
      timer = null;
    };
    const onVis = () => {
      if (document.visibilityState === 'visible') {
        fetchLive();
        start();
      } else {
        stop();
      }
    };
    start();
    document.addEventListener('visibilitychange', onVis);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [fetchLive]);

  const refreshLive = useCallback(() => fetchLive(), [fetchLive]);

  // refreshHub — pull /api/data and replace the areas + projects slices.
  // Mirrors the `share:reload` listener in sections.jsx ShareView (which
  // refetches its file listing when share/file-preview rename/delete fires).
  // `fresh: true` appends ?fresh=1 so the backend bypasses its 30s discovery
  // cache and re-scans the filesystem — used when the user opens a view that
  // must reflect a just-created project/area (e.g. the agent made one mid-chat).
  const refreshHub = useCallback(async (opts) => {
    const fresh = !!(opts && opts.fresh);
    try {
      const res = await fetch(apiUrl('/api/data' + (fresh ? '?fresh=1' : '')), { headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      if (Array.isArray(json.areas)) setAreas(json.areas);
      if (Array.isArray(json.projects)) setProjects(json.projects);
    } catch (err) {
      console.error('refreshHub failed:', err);
    }
  }, []);

  // Library mutations dispatch `library:reload` (mirroring `share:reload`
  // for the file browser). Listen at window-level so any descendant can
  // trigger a refresh without prop drilling.
  useEffect(() => {
    const onReload = () => { refreshHub(); };
    window.addEventListener('library:reload', onReload);
    return () => window.removeEventListener('library:reload', onReload);
  }, [refreshHub]);

  const value = useMemo(() => ({
    areas, projects, apps, resources, host, system, live, refreshLive, refreshHub, loading,
  }), [areas, projects, apps, resources, host, system, live, refreshLive, refreshHub, loading]);

  return React.createElement(HubDataContext.Provider, { value }, children);
}

function useHubData() {
  return useContext(HubDataContext);
}

// ─────────────────────────────────────────────────────────────
// Unread tracker — polls /api/orchestrator/sessions and derives
// a Map<agentKey, unreadSessionCount> + total. agentKey = "global"
// or "<kind>/<lib_id>". Counts SESSIONS with unread > 0 (not the
// total unread message count) so badges stay readable when one
// session has hundreds of unread turns. Sidebar total = sum of
// per-agent session counts (e.g. 2 agents × 2 sessions = 4).
// ─────────────────────────────────────────────────────────────
const UnreadContext = React.createContext({
  byAgent: new Map(), bySession: new Map(), total: 0, refresh: () => {},
});

function _agentKeyFor(session) {
  const cwd = session && session.cwd;
  const libId = session && session.lib_id;
  if (!cwd) return 'global';
  if (typeof libId === 'string' && libId) return libId;  // already "<kind>/<lib_id>"
  return 'global';  // session has cwd but no lib_id → treat as global-ish
}

function UnreadProvider({ children }) {
  const [byAgent, setByAgent] = useState(() => new Map());
  const [bySession, setBySession] = useState(() => new Map());

  const refresh = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/api/orchestrator/sessions'));
      if (!r.ok) return;
      const list = await r.json();
      if (!Array.isArray(list)) return;
      const agentMap = new Map();
      const sessionMap = new Map();
      for (const s of list) {
        const u = Number(s && s.unread_count) || 0;
        if (u <= 0) continue;
        const key = _agentKeyFor(s);
        // +1 per session — this map counts SESSIONS with unread, not msgs.
        agentMap.set(key, (agentMap.get(key) || 0) + 1);
        if (s && s.id) sessionMap.set(String(s.id), u);
      }
      setByAgent(agentMap);
      setBySession(sessionMap);
    } catch (_) { /* tolerate transient offline */ }
  }, []);

  useEffect(() => {
    refresh();
    const onFocus = () => refresh();
    const onSessionsReload = () => refresh();
    window.addEventListener('focus', onFocus);
    window.addEventListener('orchestrator:sessions-changed', onSessionsReload);
    const id = setInterval(refresh, 30000);
    return () => {
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('orchestrator:sessions-changed', onSessionsReload);
      clearInterval(id);
    };
  }, [refresh]);

  const total = useMemo(() => {
    let n = 0;
    for (const v of byAgent.values()) n += v;
    return n;
  }, [byAgent]);

  const value = useMemo(() => ({ byAgent, bySession, total, refresh }), [byAgent, bySession, total, refresh]);
  return React.createElement(UnreadContext.Provider, { value }, children);
}

function useUnreadCounts() {
  return useContext(UnreadContext);
}

window.useUnreadCounts = useUnreadCounts;

// ─────────────────────────────────────────────────────────────
// Section router
// ─────────────────────────────────────────────────────────────
const TasksViewStub = () => window.ComingSoonView ? <window.ComingSoonView name="Tasks" /> : null;
function ArchiveView() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;

  const load = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/api/library/archive'));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const d = await r.json();
      setData({ areas: d.areas || [], projects: d.projects || [] });
    } catch (e) {
      setError(e.message || 'failed to load');
      setData({ areas: [], projects: [] });
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const restore = async (kind, archived_name) => {
    if (!confirm(`Restore "${archived_name}" to ~/${kind === 'areas' ? 'Areas' : 'Projects'}/?`)) return;
    try {
      const r = await fetch(apiUrl(`/api/library/${kind}/${encodeURIComponent(archived_name)}/restore`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
      });
      if (!r.ok) throw new Error(`http ${r.status}`);
      toast && toast('Restored', 'ok');
      window.dispatchEvent(new CustomEvent('library:reload'));
      await load();
    } catch (e) {
      toast && toast('Restore failed: ' + (e.message || 'error'), 'err');
    }
  };

  if (data === null) {
    return <div className="mono" style={{ padding: 24, color: 'var(--fg-4)' }}>loading…</div>;
  }
  const groups = [
    { label: 'Areas', kind: 'areas', items: data.areas },
    { label: 'Projects', kind: 'projects', items: data.projects },
  ];
  const total = data.areas.length + data.projects.length;
  return (
    <div style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 18 }}>
      <div>
        <h2 style={{ fontSize: 18, fontWeight: 500, margin: 0 }}>Archive</h2>
        <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 4 }}>
          Soft-deleted items in ~/Areas/_archive/ + ~/Projects/_archive/. Restore moves them back.
        </div>
      </div>
      {error && (
        <StatusBanner variant="err" label={error} inline />
      )}
      {total === 0 && (
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(empty — no archived items)</div>
      )}
      {groups.map(g => g.items.length > 0 && (
        <div key={g.kind}>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>{g.label} · {g.items.length}</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {g.items.map(it => (
              <div key={it.archived_name} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '12px 14px', background: 'var(--surface-1)',
                border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)',
              }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 'var(--t-md)', fontWeight: 500 }}>{it.original_name}</div>
                  <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', marginTop: 2 }}>
                    archived {new Date(it.archived_at * 1000).toISOString().slice(0, 16).replace('T', ' ')}
                  </div>
                </div>
                <button onClick={() => restore(g.kind, it.archived_name)}
                  style={{
                    padding: '6px 12px', fontSize: 'var(--t-cap)', borderRadius: 'var(--r-sm)',
                    background: 'var(--accent-soft)', color: 'var(--accent)',
                    border: '1px solid var(--accent-line)', cursor: 'pointer', fontFamily: 'inherit',
                  }}>Restore</button>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function getSection(key) {
  switch (key) {
    // `agents` is the new home — directory of every agent (Global + Areas +
    // Projects). The legacy `orchestrator` case is preserved so deep-linked
    // /orchestrator and /orchestrator/<sid> URLs still resolve to the
    // single-session chat.
    case 'agents':       return window.AgentsDirectoryView;
    case 'orchestrator': return window.OrchestratorView;
    case 'tasks':        return window.TasksView || TasksViewStub;
    case 'inbox':        return window.InboxView || TasksViewStub;
    case 'ideas':        return window.IdeasView || TasksViewStub;
    case 'apps':         return window.AppsView;
    case 'share':        return window.ShareView;
    case 'system':       return window.SystemView;
    case 'logs':         return window.LogsView;
    case 'skills':       return window.SkillsDirectoryView;
    case 'scheduler':    return window.SchedulerDirectoryView;
    case 'secrets':      return window.SecretsView;
    case 'projects':     return window.ProjectsView;
    case 'areas':        return window.AreasView;
    case 'global':       return window.GlobalDetail;
    case 'resources':    return window.ResourcesView;
    case 'archive':      return ArchiveView;
    case 'settings':     return window.SettingsView;
    case 'design':       return window.DesignSystemView;
    default:             return window.AgentsDirectoryView;
  }
}

function fmtUptime(seconds) {
  if (!seconds || seconds < 0) return '—';
  if (seconds < 60) return Math.floor(seconds) + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h + 'h ' + m + 'm';
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d + 'd ' + h + 'h ' + m + 'm';
}

// ─────────────────────────────────────────────────────────────
// ResponsiveHub — switches between Mobile/Desktop based on viewport
// ─────────────────────────────────────────────────────────────
function ResponsiveHub() {
  const [isDesktop, setIsDesktop] = useState(() => (typeof window !== 'undefined' ? window.innerWidth >= 960 : true));

  useEffect(() => {
    let raf = null;
    const onResize = () => {
      if (raf) cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        setIsDesktop(window.innerWidth >= 960);
      });
    };
    window.addEventListener('resize', onResize);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
    };
  }, []);

  return isDesktop
    ? <DesktopHub />
    : <MobileHub />;
}

// ─────────────────────────────────────────────────────────────
// Hub route — URL is single source of truth for section + library detail
// ─────────────────────────────────────────────────────────────
// Sections (orchestrator, areas, projects, …) and library detail (areas/
// projects/resources items) live in the URL. Non-library detail kinds
// (folder / log / service) stay as local state since they don't have a
// stable identity worth bookmarking. `useHubRoute` exposes a single API
// that hides the split.
function _resolveLibraryDetailFromRoute(route, hubData) {
  if (!route || !route.detail) return null;
  const { kind, lib_id, name } = route.detail;
  const list = kind === 'area'     ? (hubData && hubData.areas)
            : kind === 'project'  ? (hubData && hubData.projects)
            : kind === 'resource' ? (hubData && hubData.resources)
            : null;
  const found = (Array.isArray(list) ? list : []).find((it) => it && (it.lib_id === lib_id || it.name === lib_id));
  return { kind, payload: found || { name, lib_id } };
}

function useHubRoute() {
  const data = useHubData();
  const { path, push, replace, route } = window.useRouter();
  const [localDetail, setLocalDetail] = useState(null);

  const libraryDetail = useMemo(() => _resolveLibraryDetailFromRoute(route, data), [route, data && data.areas, data && data.projects, data && data.resources]);
  const detail = libraryDetail || localDetail;
  // Bare `/` (parseRoute returns section: null) lands on the Agents directory
  // now that the sidebar's "Orchestrator" entry has become "Agents".
  const section = route.section || 'agents';
  const tab = route.tab || 'overview';
  const sessionId = route.sessionId || null;
  // /skills/<name> → render <SkillsDetailView name={…}/> in place of the
  // directory. Mirrors the orchestrator's optional sessionId pattern.
  const skillName = route.skillName || null;
  // /scheduler/<job_id> → same pattern as /skills/<name>.
  const jobId = route.jobId || null;
  // Files-tab deep-link: the open file's relative path, decoded from the URL.
  const openFile = (route.detail && route.detail.openFile) || null;
  // Share browser: current folder + open file, both decoded from the URL so
  // browser Back steps back one folder/file instead of escaping /share.
  const sharePath = route.section === 'share' ? (route.sharePath || '') : '';
  const shareFile = route.section === 'share' ? (route.shareFile || null) : null;

  // Closing detail: clear non-library local state OR drop the detail segment
  // from URL. Opening detail: library kinds → URL push; everything else →
  // local state.
  const setDetail = useCallback((d) => {
    if (!d) {
      setLocalDetail(null);
      if (route.detail) push(window.buildPath({ section }));
      return;
    }
    const k = d.kind;
    if (k === 'area' || k === 'project' || k === 'resource') {
      const sec = k === 'area' ? 'areas' : (k === 'project' ? 'projects' : 'resources');
      const payload = d.payload || {};
      const det = { kind: k, lib_id: payload.lib_id || payload.name, name: payload.name };
      push(window.buildPath({ section: sec, detail: det }));
      // also clear any stale non-library overlay
      if (localDetail) setLocalDetail(null);
    } else {
      setLocalDetail(d);
    }
  }, [push, route, section, localDetail]);

  const setSection = useCallback((next) => {
    if (!next || next === section) return;
    setLocalDetail(null);
    push(window.buildPath({ section: next }));
  }, [push, section]);

  // Tab switches within a library detail use replaceState so the back button
  // isn't polluted with one entry per tab click.
  const setTab = useCallback((nextTab) => {
    if (!route.detail) return;
    if (nextTab === tab) return;
    replace(window.buildPath({ section, detail: route.detail, tab: nextTab }));
  }, [replace, route, section, tab]);

  const setSessionId = useCallback((sid) => {
    push(window.buildPath({ section: 'orchestrator', sessionId: sid || undefined }));
  }, [push]);

  // Files-tab open-file sync. Opening a file PUSHES a history entry so Back
  // returns to the previously-viewed file (not out of the Files view); the
  // URL→state effect in LibraryDetail reopens whatever file the popped URL
  // names. Closing a file (falsy `rel`) REPLACES — closing shouldn't leave a
  // dangling "no file open" entry that Back would then have to step through.
  const setOpenFile = useCallback((rel) => {
    if (!route.detail) return;
    if (rel) {
      const det = { ...route.detail, openFile: rel };
      push(window.buildPath({ section, detail: det, tab: 'files' }));
    } else {
      const det = { ...route.detail, openFile: undefined };
      replace(window.buildPath({ section, detail: det, tab: 'files' }));
    }
  }, [push, replace, route, section]);

  // Share browser navigation. Folder change PUSHES so Back pops a level.
  // Opening a file PUSHES (Back returns to the listing); closing a file
  // (falsy fileRel) REPLACES so no dangling "no file open" entry is left —
  // same rationale as setOpenFile above.
  const setSharePath = useCallback((rel) => {
    push(window.buildPath({ section: 'share', sharePath: rel || undefined }));
  }, [push]);
  const setShareFile = useCallback((rel, fileRel) => {
    if (fileRel) {
      push(window.buildPath({ section: 'share', sharePath: rel || undefined, shareFile: fileRel }));
    } else {
      replace(window.buildPath({ section: 'share', sharePath: rel || undefined }));
    }
  }, [push, replace]);

  // One-shot cold-start redirect on bare `/`.
  window.useColdStartRedirect(replace, path);

  return { section, detail, tab, sessionId, skillName, jobId, openFile, sharePath, shareFile, setSection, setDetail, setTab, setSessionId, setOpenFile, setSharePath, setShareFile, route };
}

// useLibraryOpenDetailListener — wires the global `library:open-detail`
// event (dispatched by linked-item chips inside LibraryDetail) back into
// the hub's `setDetail` state. The event payload is `{kind, name, lib_id}`;
// we resolve it against the freshest hub data so the new detail receives
// the full item object (with linked_*, is_repo flags, etc).
function useLibraryOpenDetailListener(setDetail) {
  const data = useHubData();
  // Stable refs to the latest item arrays so the event handler always
  // sees current data without re-binding on every render.
  const areasRef = useRef(data && data.areas);
  const projectsRef = useRef(data && data.projects);
  useEffect(() => { areasRef.current = data && data.areas; }, [data && data.areas]);
  useEffect(() => { projectsRef.current = data && data.projects; }, [data && data.projects]);

  useEffect(() => {
    const onOpen = (ev) => {
      const d = ev && ev.detail;
      if (!d || !d.kind || !d.name) return;
      const list = d.kind === 'area' ? areasRef.current : projectsRef.current;
      const found = (Array.isArray(list) ? list : []).find((it) => {
        if (!it) return false;
        if (d.lib_id && (it.lib_id === d.lib_id)) return true;
        return it.name === d.name;
      });
      if (found) {
        setDetail({ kind: d.kind, payload: found });
      } else {
        // Fall back to a stub payload; LibraryDetail tolerates missing
        // metadata (it'll fetch /main + /git on its own).
        setDetail({ kind: d.kind, payload: { name: d.name, lib_id: d.lib_id || d.name } });
      }
    };
    window.addEventListener('library:open-detail', onOpen);
    return () => window.removeEventListener('library:open-detail', onOpen);
  }, [setDetail]);
}

// ─────────────────────────────────────────────────────────────
// Mobile shell
// ─────────────────────────────────────────────────────────────
function MobileHub() {
  const data = useHubData();
  const { section, detail, tab, skillName, jobId, openFile, sharePath, shareFile, setSection, setDetail, setTab, setOpenFile, setSharePath, setShareFile } = useHubRoute();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [previewFile, setPreviewFile] = useState(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  // True when the soft keyboard is up. Detected by comparing
  // visualViewport.height against window.innerHeight — any difference
  // > 150 px is almost certainly a keyboard (iOS keyboards are 250-330 px,
  // Android 220-260 px; the only sub-150 px viewport-shrink causes are
  // address-bar collapse which we DON'T want to treat as keyboard up).
  // When this flips on, the bottom nav hides and the terminal shows its
  // soft-keys row — exactly inverted from the keyboard-down layout.
  const [keyboardOpen, setKeyboardOpen] = useState(false);
  // The keyboard signal is forwarded into the media engine (so the dock hides
  // while the tmux soft-keyboard toolbar is up, audio still playing) via the
  // STABLE imperative facade window.HubPlayer.reportKeyboard — NOT useMediaPlayer()
  // context, which would re-render the whole shell on every play/pause/PiP/blocked
  // discrete state change. The facade is a no-op stub until the engine mounts.
  useOrchestratorPushBridge(section, setSection, setUpdateAvailable);
  useLibraryOpenDetailListener(setDetail);

  const Section = getSection(section);
  const workspace = SECTIONS.filter(s => s.group === 'workspace');
  const library = SECTIONS.filter(s => s.group === 'library');
  const system = SECTIONS.filter(s => s.group === 'system');
  // Mobile bottom nav: an explicit, ordered five — Inbox is the featured
  // center action. (Everything else stays reachable via the drawer.)
  const mobileTabs = ['agents', 'tasks', 'inbox', 'apps', 'share']
    .map((k) => SECTIONS.find((s) => s.key === k))
    .filter(Boolean);
  const _unreadCtxMobile = useUnreadCounts();
  const _totalUnreadMobile = _unreadCtxMobile?.total || 0;

  // URL → preview reconciliation: Back/forward/deep-link reopen the right
  // Sync file (or close it) without an imperative click. FilePreview needs
  // only {name, path}, so no extra fetch is required to reopen from the URL.
  useEffect(() => {
    if (shareFile) setPreviewFile({ name: shareFile.split('/').pop(), path: shareFile });
    else if (section === 'share') setPreviewFile(null);
  }, [shareFile, section]);

  const sectionProps = {
    compact: true,
    isActive: section === 'orchestrator',
    keyboardOpen,
    sharePath,
    shareFile,
    onPreview: (f) => setPreviewFile(f),
    onNavigate: (rel) => setSharePath(rel),
    onOpenFilePath: (dir, fileRel, fileObj) => {
      setShareFile(dir, fileRel);
      setPreviewFile(fileObj || { name: fileRel.split('/').pop(), path: fileRel });
    },
    onOpenProject: (p) => setDetail({ kind: 'project', payload: p }),
    onOpenArea: (a) => setDetail({ kind: 'area', payload: a }),
    onOpenLog: (l) => setDetail({ kind: 'log', payload: l }),
    onOpenService: (s) => setDetail({ kind: 'service', payload: s }),
  };

  const onRefresh = async () => {
    await Promise.all([data.refreshLive(), new Promise(r => setTimeout(r, 400))]);
  };

  const sectionLabel = SECTIONS.find(s => s.key === section)?.label || '';
  const onOpenDrawer = () => setDrawerOpen(true);

  // visualViewport sync — when the iOS / Android soft keyboard appears
  // it shrinks `visualViewport.height` BELOW window.innerHeight. We
  // mirror that into the CSS custom property `--vvh` and use it as the
  // shell height instead of `100vh`. Result: the mobile shell shrinks
  // when the keyboard slides in, the iframe terminal area gets a
  // smaller `flex: 1` allocation, and its existing ResizeObserver
  // dispatches a resize INTO the iframe so ttyd/FitAddon refits and
  // claude redraws with the input prompt above the keyboard — exactly
  // like a native mobile app handles keyboard-aware content.
  useEffect(() => {
    // Cache the last written value so no-op writes don't trigger a
    // pointless style invalidation on :root (which propagates to every
    // element consuming var(--vvh) — the entire fixed shell). Mobile
    // Safari fires three events in quick succession during orientation
    // flip; without the cache that's 3× redundant setProperty.
    let lastH = null;
    let lastKbd = false;
    const sync = () => {
      const vv = window.visualViewport;
      const h = (vv && vv.height) || window.innerHeight;
      const next = `${h}px`;
      if (next !== lastH) {
        lastH = next;
        document.documentElement.style.setProperty('--vvh', next);
      }
      // Keyboard-up heuristic: visualViewport.height shrinks by the
      // keyboard's height when it slides in. Sub-150 px deltas are
      // address-bar collapse / pinch-zoom artifacts we ignore.
      const kbdNow = vv ? (window.innerHeight - vv.height) > 150 : false;
      if (kbdNow !== lastKbd) {
        lastKbd = kbdNow;
        setKeyboardOpen(kbdNow);
        if (window.HubPlayer && window.HubPlayer.reportKeyboard) window.HubPlayer.reportKeyboard(kbdNow);
      }
    };
    sync();
    const vv = window.visualViewport;
    if (vv) {
      // visualViewport.resize covers keyboard show/hide AND orientation
      // change (it's a superset of window.resize on browsers that have
      // it). Specifically NOT subscribing to vv.scroll — it fires at
      // refresh rate during pinch-zoom and address-bar collapse, and
      // we only care about height changes, not viewport pan.
      vv.addEventListener('resize', sync);
      return () => { vv.removeEventListener('resize', sync); };
    }
    // Fallback for ancient browsers without visualViewport.
    window.addEventListener('resize', sync);
    window.addEventListener('orientationchange', sync);
    return () => {
      window.removeEventListener('resize', sync);
      window.removeEventListener('orientationchange', sync);
    };
  }, []);

  // Title + subtitle + back handler resolution for non-chat sections.
  // Chat section paints its own header (orchestrator.HeaderActionsCompact);
  // every other branch below renders MobileSectionHeader at the top of
  // its own subtree.
  //
  // Memoized so MobileSectionHeader (which receives `detailBack` as
  // `onBack`) doesn't see a fresh function reference on every
  // MobileHub re-render. Without memoization the header re-renders on
  // every drawer / palette / presence tick — cheap but pointless.
  const detailTitle = useMemo(
    () => (
      detail ? (detail.payload?.label || detail.payload?.name || sectionLabel)
        : (section === 'skills' && skillName ? skillName
           : (section === 'scheduler' && jobId ? jobId : sectionLabel))
    ),
    [detail, section, skillName, jobId, sectionLabel],
  );
  const detailBack = useMemo(() => {
    if (detail) return () => setDetail(null);
    if (section === 'skills' && skillName) {
      return () => { try { window.history.pushState(null, '', (window.HUB_BASE_PATH || '') + '/skills'); window.dispatchEvent(new Event('router:change')); } catch (_) { /* ignore */ } };
    }
    if (section === 'scheduler' && jobId) {
      return () => { try { window.history.pushState(null, '', (window.HUB_BASE_PATH || '') + '/scheduler'); window.dispatchEvent(new Event('router:change')); } catch (_) { /* ignore */ } };
    }
    return null;
  }, [detail, section, skillName, jobId, setDetail]);

  return (
    <div style={{
      // Anchor the shell to the visual viewport (keyboard-aware). Old
      // pattern was `position: absolute, inset: 0` which made the shell
      // exactly the layout viewport size — iOS' keyboard overlays
      // without shrinking that, so iframe content went under the
      // keyboard. Using fixed + var(--vvh) lets the shell respect the
      // visual viewport that visualViewport.height reports.
      position: 'fixed', top: 0, left: 0, right: 0,
      height: 'var(--vvh, 100dvh)',
      display: 'flex', flexDirection: 'column',
      background: 'var(--bg)', overflow: 'hidden',
    }}>
      {/* body — keep OrchestratorView mounted always so the SSE
          connection survives navigation; other sections still
          mount/unmount. Library detail (areas/projects/resources)
          renders inline on top of the current section just like desktop
          — no BottomSheet modal.

          Each branch below renders its own MobileSectionHeader at the
          top — the legacy "global" mobile top bar was removed because
          it duplicated info (hostname / uptime) already present in the
          drawer header. Net win: one header per page, more vertical
          space for content (especially the chat transcript on small
          phones).

          Pull-to-refresh wraps ONLY the list/section pages. The chat
          surface (orchestrator) and library detail manage their own
          scroll, and wrapping them in a PullToRefresh's `overflowY:
          auto` ancestor caused iOS to scroll that wrapper when the
          keyboard opened on the composer textarea — dragging the chat
          header off-screen. With the chat outside the wrapper, iOS
          scrolls the transcript's own scroll container (which is below
          the header) and the header stays put. */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        {window.OrchestratorView && (
          <div style={{
            display: section === 'orchestrator' && !detail ? 'flex' : 'none',
            flex: 1, minHeight: 0, flexDirection: 'column', height: '100%',
          }}>
            {/* Chat brings its own header via HeaderActionsCompact; we
                only forward the drawer-open callback so the hamburger
                can render INSIDE that header. */}
            <window.OrchestratorView {...sectionProps} onOpenDrawer={onOpenDrawer} />
          </div>
        )}
        {detail && (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', height: '100%' }}
               key={'detail:' + (detail.kind) + ':' + (detail.payload?.lib_id || detail.payload?.name || '')}>
            {/* Library detail kinds (project/area/resource) show the drawer
                HAMBURGER here (like the Global meta-area) and carry their own
                back arrow inside LibraryDetail's header. Other detail kinds
                (folder/log/service) keep the back-arrow topbar. */}
            {(detail.kind === 'area' || detail.kind === 'project' || detail.kind === 'resource')
              ? <MobileSectionHeader onOpenDrawer={onOpenDrawer} />
              : <MobileSectionHeader title={detailTitle} onBack={detailBack} />}
            <DetailRouter detail={detail} compact tab={tab} onChangeTab={setTab} openFile={openFile} onOpenFile={setOpenFile} onBack={() => setDetail(null)} onPreview={(f) => setPreviewFile(f)}/>
          </div>
        )}
        {!detail && section === 'skills' && skillName && window.SkillsDetailView && (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }} key={'skill:' + skillName}>
            <MobileSectionHeader title={detailTitle} onBack={detailBack} />
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }} className="scroll-hide">
              <window.SkillsDetailView name={skillName} compact />
            </div>
          </div>
        )}
        {!detail && section === 'scheduler' && jobId && window.SchedulerDetailView && (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }} key={'job:' + jobId}>
            <MobileSectionHeader title={detailTitle} onBack={detailBack} />
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }} className="scroll-hide">
              <window.SchedulerDetailView jobId={jobId} compact />
            </div>
          </div>
        )}
        {!detail && section !== 'orchestrator' && !(section === 'skills' && skillName) && !(section === 'scheduler' && jobId) && Section && (
          <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <MobileSectionHeader title={sectionLabel} onOpenDrawer={onOpenDrawer} />
            <PullToRefresh onRefresh={onRefresh} style={{ flex: 1 }}>
              <Section {...sectionProps} key={section}/>
            </PullToRefresh>
          </div>
        )}
      </div>

      {/* Persistent media dock — sits above the nav, in flow. Self-hides via
          context keyboardOpen (audio keeps playing in the engine garage) and
          returns null when nothing is playing. */}
      {window.MediaDock && <window.MediaDock />}

      {/* bottom nav — hidden when keyboard is up so it doesn't sit ON
          TOP of the keyboard (visualViewport-aware shell already
          shrinks above the keyboard; the nav was the only thing
          pinning the bottom inset). When hidden, the safe-area-bottom
          home-indicator inset doesn't matter either — the iframe /
          composer below sits flush against the keyboard edge. */}
      {!keyboardOpen && (
        <div style={{
          flexShrink: 0,
          background: 'var(--bg)',
          borderTop: '1px solid var(--hairline)',
          // Home-indicator inset added on top of the 12 px design gap (was 22 px).
          // calc() composes properly across iOS versions; max() of env() can be
          // flaky on older Safari.
          padding: '4px calc(8px + env(safe-area-inset-right)) calc(7px + env(safe-area-inset-bottom)) calc(8px + env(safe-area-inset-left))',
          display: 'grid',
          gridTemplateColumns: `repeat(${mobileTabs.length}, 1fr)`,
          gap: 4,
          // Let the featured (Inbox) circle pop above the bar's top edge.
          overflow: 'visible',
        }}>
          {mobileTabs.map(s => (
            <BottomTab key={s.key} s={s} active={section === s.key} onClick={() => setSection(s.key)} featured={s.key === 'inbox'} />
          ))}
        </div>
      )}

      <Drawer open={drawerOpen} onClose={() => setDrawerOpen(false)} section={section} setSection={setSection} workspace={workspace} library={library} system={system} totalUnread={_totalUnreadMobile} onOpenSearch={() => setPaletteOpen(true)} />

      <BottomSheet open={!!previewFile} onClose={() => { if (section === 'share') setShareFile(sharePath || '', null); else setPreviewFile(null); }} title={previewFile?.name} height="78%">
        {previewFile && window.FilePreview && <window.FilePreview file={previewFile} />}
      </BottomSheet>

      <BottomSheet open={paletteOpen} onClose={() => setPaletteOpen(false)} title="Search anything" height="60%">
        <CommandPalette onPick={(s) => { setSection(s); setPaletteOpen(false); }}/>
      </BottomSheet>
      <UpdateToast visible={updateAvailable} onDismiss={() => setUpdateAvailable(false)} />
    </div>
  );
}

function BottomTab({ s, active, onClick, unread = 0, featured = false }) {
  // Anchor, not <button>, so ⌘/Ctrl/middle-click (and "open in new tab" from
  // a long-press menu on mobile) work; plain tap is intercepted for SPA nav.
  const href = window.sectionHref ? window.sectionHref(s.key) : undefined;
  const handle = (e) => { if (window.isModifiedClick && window.isModifiedClick(e)) return; e.preventDefault(); onClick && onClick(); };
  // Featured center action (Inbox): a raised accent pill with an
  // inverted-colour icon, slightly larger than the flanking tabs.
  if (featured) {
    // Same outer height as the other tabs (so the bar doesn't grow); the
    // accent circle is absolutely positioned to POP above the bar's top
    // edge. The bar sets overflow:visible so it isn't clipped.
    return (
      <a href={href} onClick={handle} style={{
        background: 'transparent', border: 'none', cursor: 'pointer', textDecoration: 'none',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'flex-end',
        padding: '0 0 2px', gap: 3, height: 40, minHeight: 40,
        position: 'relative', overflow: 'visible',
        color: active ? 'var(--accent)' : 'var(--fg-2)',
      }}>
        <span className="orbit-glow-strong" style={{
          position: 'absolute', top: -20, left: '50%', transform: 'translateX(-50%)',
          width: 46, height: 46, borderRadius: 16,
          background: 'var(--grad-cosmic)', color: 'var(--accent-fg)',
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          border: '2px solid var(--bg)',
        }}>
          <Icon name={s.icon} size={24} stroke={1.9} color="var(--accent-fg)"/>
        </span>
        <span style={{ fontSize: 'var(--t-2xs)', fontWeight: 600, letterSpacing: 0.1 }}>{s.label}</span>
      </a>
    );
  }
  return (
    <a href={href} onClick={handle} style={{
      background: 'transparent', border: 'none', cursor: 'pointer', textDecoration: 'none',
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
      padding: '4px 0 2px', gap: 2, minHeight: 40,
      color: active ? 'var(--accent)' : 'var(--fg-3)',
      position: 'relative',
    }}>
      <span style={{ position: 'relative', display: 'inline-flex' }}>
        <Icon name={s.icon} size={20} stroke={active ? 1.8 : 1.5}/>
      </span>
      <span className={active ? 'orbit-gradient-text' : undefined} style={{ fontSize: 'var(--t-2xs)', fontWeight: 500, letterSpacing: 0.1 }}>{s.label}</span>
      {/* Active tab: cosmic gradient indicator with a soft glow. */}
      {active && <span className="orbit-glow-soft" style={{ position: 'absolute', top: -1, left: '30%', right: '30%', height: 2, background: 'var(--grad-cosmic)', borderRadius: 'var(--r-pill)' }}/>}
    </a>
  );
}

// MobileSectionHeader — slim 44 px bar with [hamburger or back] + [title] +
// optional right actions. Replaces the page-chrome top bar that used to
// live in MobileHub and duplicated info already present in the drawer
// header. Each non-chat section is wrapped with one of these so the page
// has a single visible header (matches the chat panel pattern).
function MobileSectionHeader({ title, subtitle, onOpenDrawer, onBack, rightAction }) {
  // Dev-time guard: at least one of the two navigation actions MUST be
  // provided, otherwise the leading IconButton renders with
  // onClick={undefined} — a visible button that taps silently. Warn so
  // a future caller catches the bug at first render instead of
  // shipping a dead control.
  if (!onBack && typeof onOpenDrawer !== 'function') {
    // eslint-disable-next-line no-console
    console.warn('MobileSectionHeader: neither onBack nor onOpenDrawer was provided; the leading icon button will be a no-op.');
  }
  return (
    <div style={{
      flexShrink: 0,
      // Reserve space below the iOS notch / status bar in PWA standalone
      // mode — same insets we used to pay in the old top bar.
      padding: 'calc(8px + env(safe-area-inset-top)) calc(8px + env(safe-area-inset-right)) 8px calc(8px + env(safe-area-inset-left))',
      display: 'flex', alignItems: 'center', gap: 6,
      background: 'var(--bg)', borderBottom: '1px solid var(--hairline)',
      minHeight: 36,
    }}>
      {onBack
        ? <IconButton icon="chevron-l" label="Back" size={32} onClick={onBack} />
        : <IconButton icon="menu" label="Menu" size={32} onClick={onOpenDrawer} />}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 'var(--t-body)', fontWeight: 500, lineHeight: 1.2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{title}</div>
        {subtitle && (
          <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{subtitle}</div>
        )}
      </div>
      {rightAction || null}
    </div>
  );
}

function Drawer({ open, onClose, section, setSection, workspace, library, system, totalUnread = 0, onOpenSearch }) {
  if (!open) return null;
  const pick = (s) => { setSection(s.key); onClose(); };
  return (
    <>
      <div onClick={onClose} style={{
        position: 'absolute', inset: 0, background: 'rgba(0,0,0,0.5)',
        opacity: 1, pointerEvents: 'auto',
        transition: 'opacity .22s', zIndex: 50,
      }}/>
      <aside className="fade-in" style={{
        position: 'absolute', top: 0, bottom: 0, left: 0, width: 280,
        background: 'var(--surface-1)', borderRight: '1px solid var(--hairline-strong)',
        zIndex: 51, display: 'flex', flexDirection: 'column',
        boxShadow: '8px 0 32px rgba(0,0,0,0.5)',
      }}>
        <DrawerHeader />
        {onOpenSearch && (
          <button onClick={() => { onOpenSearch(); onClose(); }}
            style={{
              margin: '10px 12px 0',
              padding: '10px 12px', borderRadius: 'var(--r-control)',
              background: 'var(--surface-2)', color: 'var(--fg-3)',
              border: '1px solid var(--hairline)',
              display: 'flex', alignItems: 'center', gap: 8,
              cursor: 'pointer', fontFamily: 'inherit', fontSize: 'var(--t-sm)',
              textAlign: 'left',
            }}>
            <Icon name="search" size={16} />
            <span>Search projects, apps, sections…</span>
          </button>
        )}
        <div style={{ padding: '6px 8px', flex: 1, overflowY: 'auto' }} className="scroll-hide">
          <DrawerGroup label="Workspace">
            {workspace.map(s => <DrawerItem key={s.key} s={s} active={section === s.key} onClick={() => pick(s)} unread={s.key === 'agents' ? totalUnread : 0}/>)}
          </DrawerGroup>
          <DrawerGroup label="Library">
            {library.map(s => <DrawerItem key={s.key} s={s} active={section === s.key} onClick={() => pick(s)}/>)}
          </DrawerGroup>
          <DrawerGroup label="System">
            {system.map(s => <DrawerItem key={s.key} s={s} active={section === s.key} onClick={() => pick(s)}/>)}
          </DrawerGroup>
        </div>
        <DrawerFooter />
      </aside>
    </>
  );
}

function DrawerHeader() {
  const { host, system } = useHubData();
  const name = host?.name || system?.hostname || 'host';
  const domain = host?.domain || '';
  const uptimeStr = fmtUptime(system?.uptime_s);
  return (
    <div style={{ padding: 'calc(20px + env(safe-area-inset-top)) 18px 16px', borderBottom: '1px solid var(--hairline)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <OrbitMark size={32} />
        <div>
          <div className="orbit-gradient-text is-animated" style={{ fontSize: 'var(--t-h3)', fontWeight: 700, letterSpacing: '-0.015em' }}>Orbit</div>
          <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{name}{domain ? ' · ' + domain : ''}</div>
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 14, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} className="mono">
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><span className="live-dot" style={{ width: 5, height: 5 }}/> healthy</span>
        <span>·</span>
        <span>up {uptimeStr}</span>
      </div>
    </div>
  );
}

function DrawerGroup({ label, children }) {
  return (
    <div style={{ marginTop: 14 }}>
      <div className="mono" style={{ padding: '4px 12px 6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{label}</div>
      {children}
    </div>
  );
}

function DrawerItem({ s, active, onClick, unread = 0 }) {
  // Anchor, not <button>, so ⌘/Ctrl/middle-click opens a new tab.
  const href = window.sectionHref ? window.sectionHref(s.key) : undefined;
  const handle = (e) => { if (window.isModifiedClick && window.isModifiedClick(e)) return; e.preventDefault(); onClick && onClick(); };
  return (
    <a href={href} onClick={handle} style={{
      display: 'flex', alignItems: 'center', gap: 12, width: '100%', boxSizing: 'border-box',
      padding: '10px 12px', borderRadius: 'var(--r-control)', background: active ? 'var(--accent-soft)' : 'transparent',
      color: active ? 'var(--accent)' : 'var(--fg-2)', border: 'none', cursor: 'pointer',
      fontFamily: 'inherit', fontSize: 'var(--t-md)', fontWeight: 500, textDecoration: 'none',
    }}>
      <span style={{ position: 'relative', display: 'inline-flex' }}>
        <Icon name={s.icon} size={18}/>
      </span>
      <span style={{ flex: 1, textAlign: 'left' }}>{s.label}</span>
      {s.badge && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--accent)', padding: '2px 5px', border: '1px solid var(--accent-line)', borderRadius: 4, letterSpacing: 0.1 }}>{s.badge}</span>}
    </a>
  );
}

function DrawerFooter() {
  const { live } = useHubData();
  const total = live?.peers_total ?? 0;
  const online = live?.peers_online ?? 0;
  return (
    <div style={{ padding: '12px 16px calc(12px + env(safe-area-inset-bottom))', borderTop: '1px solid var(--hairline)', display: 'flex', alignItems: 'center', gap: 10, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} className="mono">
      <Icon name="globe" size={14}/>
      <span>tailnet · {online}/{total} online</span>
      <span style={{ flex: 1 }}/>
      <Icon name="dots" size={14}/>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Desktop shell
// ─────────────────────────────────────────────────────────────
function DesktopHub() {
  const { section, detail, tab, skillName, jobId, openFile, sessionId, sharePath, shareFile, setSection, setDetail, setTab, setOpenFile, setSharePath, setShareFile } = useHubRoute();
  const [previewFile, setPreviewFile] = useState(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [updateAvailable, setUpdateAvailable] = useState(false);
  useOrchestratorPushBridge(section, setSection, setUpdateAvailable);
  useLibraryOpenDetailListener(setDetail);

  const Section = getSection(section);

  // URL → preview reconciliation (see MobileHub for the rationale).
  useEffect(() => {
    if (shareFile) setPreviewFile({ name: shareFile.split('/').pop(), path: shareFile });
    else if (section === 'share') setPreviewFile(null);
  }, [shareFile, section]);

  const sectionProps = {
    isActive: section === 'orchestrator' && !detail,
    sharePath,
    shareFile,
    onPreview: (f) => setPreviewFile(f),
    onNavigate: (rel) => setSharePath(rel),
    onOpenFilePath: (dir, fileRel, fileObj) => {
      setShareFile(dir, fileRel);
      setPreviewFile(fileObj || { name: fileRel.split('/').pop(), path: fileRel });
    },
    onOpenProject: (p) => setDetail({ kind: 'project', payload: p }),
    onOpenArea: (a) => setDetail({ kind: 'area', payload: a }),
    onOpenLog: (l) => setDetail({ kind: 'log', payload: l }),
    onOpenService: (s) => setDetail({ kind: 'service', payload: s }),
  };

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); setPaletteOpen(o => !o); }
      if (e.key === 'Escape') setPaletteOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const _unreadCtx = useUnreadCounts();
  const _unreadFor = (key) => key === 'agents' ? (_unreadCtx?.total || 0) : 0;

  return (
    <div style={{ position: 'absolute', inset: 0, display: 'flex', background: 'var(--bg)', overflow: 'hidden' }}>
      <aside style={{
        width: 240, flexShrink: 0,
        background: 'var(--bg)', borderRight: '1px solid var(--hairline)',
        display: 'flex', flexDirection: 'column',
      }}>
        <DesktopSidebarHeader />
        <div style={{ flex: 1, padding: '8px 10px', overflowY: 'auto' }} className="scroll-hide">
          <SidebarGroup label="Workspace">
            {SECTIONS.filter(s => s.group === 'workspace').map(s => <SidebarItem key={s.key} s={s} active={section === s.key} onClick={() => setSection(s.key)} unread={_unreadFor(s.key)}/>)}
          </SidebarGroup>
          <SidebarGroup label="Library">
            {SECTIONS.filter(s => s.group === 'library').map(s => <SidebarItem key={s.key} s={s} active={section === s.key} onClick={() => setSection(s.key)} unread={_unreadFor(s.key)}/>)}
          </SidebarGroup>
          <SidebarGroup label="System">
            {SECTIONS.filter(s => s.group === 'system').map(s => <SidebarItem key={s.key} s={s} active={section === s.key} onClick={() => setSection(s.key)} unread={_unreadFor(s.key)}/>)}
          </SidebarGroup>
        </div>
        <SidebarStatus />
      </aside>

      <main style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
        <DesktopTopBar onPalette={() => setPaletteOpen(true)} />
        {/* OrchestratorView stays mounted across section changes so the SSE
            connection isn't torn down when the user navigates elsewhere.
            Other sections still mount/unmount via the keyed wrapper below. */}
        {window.OrchestratorView && (
          <div style={{
            display: (section === 'orchestrator' && !detail) ? 'flex' : 'none',
            flex: 1, minHeight: 0, flexDirection: 'column', overflowY: 'auto',
          }} className="scroll-hide">
            <window.OrchestratorView {...sectionProps}/>
          </div>
        )}
        {(section !== 'orchestrator' || detail) && (
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }} className="scroll-hide" key={section + (detail ? ':' + detail.kind : (skillName ? ':skill:' + skillName : (jobId ? ':job:' + jobId : '')))}>
            {detail
              ? <DetailRouter detail={detail} tab={tab} onChangeTab={setTab} openFile={openFile} onOpenFile={setOpenFile} onBack={() => setDetail(null)} onPreview={(f) => setPreviewFile(f)}/>
              : (section === 'skills' && skillName && window.SkillsDetailView)
                ? <window.SkillsDetailView name={skillName} />
                : (section === 'scheduler' && jobId && window.SchedulerDetailView)
                  ? <window.SchedulerDetailView jobId={jobId} />
                  : (Section && <Section {...sectionProps}/>)
            }
          </div>
        )}
        <Modal open={!!previewFile} onClose={() => { if (section === 'share') setShareFile(sharePath || '', null); else setPreviewFile(null); }} title={previewFile?.name} width={580}>
          {previewFile && window.FilePreview && <window.FilePreview file={previewFile} />}
        </Modal>
        <Modal open={paletteOpen} onClose={() => setPaletteOpen(false)} title={null} width={560}>
          <CommandPalette onPick={(s) => { setSection(s); setPaletteOpen(false); }} desktop/>
        </Modal>
        {/* Global ⌥+⇥ session switcher (desktop-only mount). Self-gated on the
            session_switcher_enabled flag; renders/listens only when enabled. */}
        {window.SessionSwitcher && <window.SessionSwitcher currentId={sessionId} />}
        {/* Persistent media dock — last flex child of <main>; keyboardOpen stays
            false on desktop, so it shows whenever something is playing.
            atScreenBottom: it's the viewport-bottom element here, so it owns the
            home-indicator inset (mobile mount sits above the nav and doesn't). */}
        {window.MediaDock && <window.MediaDock atScreenBottom />}
      </main>
      <UpdateToast visible={updateAvailable} onDismiss={() => setUpdateAvailable(false)} />
    </div>
  );
}

// Orbit brand mark: an open violet orbit ring + a satellite riding it + a
// glowing core. Rendered flat (no <defs> gradients/filters) so it stays crisp
// inline at small sizes and can appear multiple times without SVG id clashes.
// The full gradient/glow expression lives in the icon/favicon assets.
function OrbitMark({ size = 28, glow = true }) {
  // The in-app brand mark is the real app icon (the vibrant Gemini render),
  // served as a small pre-rounded PNG so the header matches the home-screen
  // icon 1:1. Base-path aware so it loads under any nginx subpath. v3 wraps it
  // in a soft cosmic glow + a slow-revolving gradient halo so the brand reads
  // as "alive" / orbital without re-drawing the mark.
  const src = (window.HUB_BASE_PATH || '') + '/static/orbit-mark.png';
  const radius = Math.round(size * 0.2235);
  return (
    <span style={{ position: 'relative', display: 'inline-flex', width: size, height: size, flexShrink: 0 }}>
      {glow && (
        <span
          aria-hidden="true"
          className="orbit-ring-spin"
          style={{
            position: 'absolute', inset: -Math.max(2, Math.round(size * 0.12)),
            borderRadius: '50%', pointerEvents: 'none', opacity: 0.55,
            background: 'conic-gradient(from 0deg, transparent 0deg, var(--cosmic-blue) 90deg, var(--cosmic-violet) 180deg, var(--cosmic-magenta) 270deg, transparent 360deg)',
            WebkitMask: 'radial-gradient(farthest-side, transparent calc(100% - 1.5px), #000 calc(100% - 1.5px))',
            mask: 'radial-gradient(farthest-side, transparent calc(100% - 1.5px), #000 calc(100% - 1.5px))',
          }}
        />
      )}
      <img
        src={src} width={size} height={size} alt="" aria-hidden="true"
        style={{
          position: 'relative', display: 'block', flexShrink: 0, width: size, height: size,
          borderRadius: radius, boxShadow: glow ? 'var(--glow-soft)' : undefined,
        }}
      />
    </span>
  );
}
window.OrbitMark = OrbitMark;

function DesktopSidebarHeader() {
  const { host, system } = useHubData();
  const name = host?.name || system?.hostname || 'host';
  return (
    <div style={{ padding: '18px 16px 14px', borderBottom: '1px solid var(--hairline)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <OrbitMark size={28} />
        <div style={{ minWidth: 0 }}>
          <div className="orbit-gradient-text is-animated" style={{ fontSize: 'var(--t-h3)', fontWeight: 700, letterSpacing: '-0.015em' }}>Orbit</div>
          <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{name}</div>
        </div>
      </div>
    </div>
  );
}

function SidebarGroup({ label, children }) {
  return (
    <div style={{ marginTop: 12 }}>
      <div className="mono" style={{ padding: '4px 10px 6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{label}</div>
      {children}
    </div>
  );
}

function SidebarItem({ s, active, onClick, unread = 0 }) {
  const [h, setH] = useState(false);
  // Real anchor (not a <button>) so ⌘/Ctrl/middle-click opens the section in
  // a new tab; a plain left-click is intercepted for SPA routing.
  const href = window.sectionHref ? window.sectionHref(s.key) : undefined;
  const handle = (e) => { if (window.isModifiedClick && window.isModifiedClick(e)) return; e.preventDefault(); onClick && onClick(); };
  return (
    <a
      href={href}
      onClick={handle}
      onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      className={active ? 'orbit-glow-soft' : undefined}
      style={{
        position: 'relative',
        display: 'flex', alignItems: 'center', gap: 10, width: '100%',
        padding: '7px 10px', borderRadius: 7, boxSizing: 'border-box',
        background: active ? 'var(--accent-soft)' : (h ? 'var(--surface-1)' : 'transparent'),
        color: active ? 'var(--accent)' : 'var(--fg-2)',
        border: 'none', cursor: 'pointer', fontFamily: 'inherit', fontSize: 'var(--t-sm)', fontWeight: 500,
        textDecoration: 'none', marginBottom: 1,
      }}
    >
      {/* Active row gets a cosmic gradient left-indicator bar. */}
      {active && (
        <span aria-hidden="true" style={{
          position: 'absolute', left: 0, top: 6, bottom: 6, width: 3,
          background: 'var(--grad-cosmic)', borderRadius: 'var(--r-pill)',
        }}/>
      )}
      <span style={{ position: 'relative', display: 'inline-flex' }}>
        <Icon name={s.icon} size={16}/>
      </span>
      <span className={active ? 'orbit-gradient-text' : undefined} style={{ flex: 1, textAlign: 'left' }}>{s.label}</span>
      {s.badge && <span className="mono" style={{ fontSize: 9, color: 'var(--accent)', padding: '1px 4px', border: '1px solid var(--accent-line)', borderRadius: 3, letterSpacing: 0.1 }}>{s.badge}</span>}
    </a>
  );
}

function SidebarStatus() {
  const { live } = useHubData();
  const cpuPct = live ? Math.min(100, Math.round((live.cpu.load_1m || 0) * 100 / Math.max(1, live.cpu.cpu_count || 1))) : 0;
  const memPct = Math.round(live?.memory?.percent || 0);
  const diskPct = Math.round(live?.disk?.percent || 0);
  const cpuVal = live ? (live.cpu.load_1m || 0).toFixed(2) : '—';
  const memVal = live ? fmtBytes(live.memory.used_bytes || 0) : '—';
  const diskVal = live ? fmtBytes(live.disk.used_bytes || 0) : '—';
  const total = live?.peers_total ?? 0;
  const online = live?.peers_online ?? 0;
  return (
    <div style={{ padding: '12px 14px', borderTop: '1px solid var(--hairline)' }}>
      <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>STATUS</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <MiniMetric label="cpu" value={cpuVal} pct={cpuPct}/>
        <MiniMetric label="ram" value={memVal} pct={memPct}/>
        <MiniMetric label="disk" value={diskVal} pct={diskPct}/>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 12, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
        <span className="live-dot" style={{ width: 6, height: 6 }}/>
        <span className="mono">tailnet · {online}/{total} online</span>
      </div>
    </div>
  );
}

function MiniMetric({ label, value, pct }) {
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 3, fontSize: 'var(--t-xs)' }}>
        <span style={{ color: 'var(--fg-3)' }}>{label}</span>
        <span className="mono" style={{ color: 'var(--fg-2)', fontSize: 'var(--t-2xs)' }}>{value}</span>
      </div>
      <div style={{ height: 3, background: 'var(--surface-3)', borderRadius: 'var(--r-pill)' }}>
        <div style={{ width: Math.min(100, Math.max(0, pct)) + '%', height: '100%', background: 'var(--accent)', borderRadius: 'var(--r-pill)' }}/>
      </div>
    </div>
  );
}

function DesktopTopBar({ onPalette }) {
  const { host, system } = useHubData();
  const domain = host?.domain || host?.name || system?.hostname || '';
  const region = host?.region || '';
  const uptimeStr = fmtUptime(system?.uptime_s);
  return (
    <div style={{
      flexShrink: 0, padding: '12px 24px',
      borderBottom: '1px solid transparent',
      borderImageSource: 'var(--grad-cosmic-line)',
      borderImageSlice: 1,
      borderImageWidth: '0 0 1px 0',
      display: 'grid', gridTemplateColumns: '1fr auto 1fr', alignItems: 'center', gap: 16,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 'var(--t-cap)', color: 'var(--fg-3)', minWidth: 0 }} className="mono">
        <span style={{ color: 'var(--fg-2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{domain}</span>
        <span>·</span>
        <span style={{ whiteSpace: 'nowrap' }}>uptime {uptimeStr}</span>
        {region && <><span>·</span><span style={{ whiteSpace: 'nowrap' }}>{region}</span></>}
      </div>
      <button onClick={onPalette} style={{
        width: 360, maxWidth: '100%', display: 'flex', alignItems: 'center', gap: 8,
        padding: '7px 12px', borderRadius: 'var(--r-control)',
        background: 'var(--surface-1)', border: '1px solid var(--hairline)',
        color: 'var(--fg-3)', fontSize: 'var(--t-sm)', cursor: 'pointer', fontFamily: 'inherit',
      }}>
        <Icon name="search" size={14} />
        <span style={{ flex: 1, textAlign: 'left' }}>Search</span>
        <span className="kbd">⌘K</span>
      </button>
      <div style={{ justifySelf: 'end' }}>
        <Chip mono><span className="live-dot" style={{ width: 6, height: 6, marginRight: 4 }}/>healthy</Chip>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Command palette
// ─────────────────────────────────────────────────────────────
function CommandPalette({ onPick, desktop }) {
  const { projects, apps } = useHubData();
  const [q, setQ] = useState('');

  const items = useMemo(() => ([
    ...SECTIONS.map(s => ({ kind: 'section', label: s.label, icon: s.icon, sub: 'go to · ' + s.label.toLowerCase(), key: s.key })),
    ...(projects || []).map(p => ({ kind: 'project', label: p.label || p.name, icon: 'box', sub: p.rel_path || p.path || '', key: 'projects' })),
    ...(apps || []).map(a => ({ kind: 'app', label: a.label || a.name, icon: 'globe', sub: a.path || a.url || '', key: 'apps' })),
  ]), [projects, apps]);

  const ql = q.trim().toLowerCase();
  const filtered = items.filter(i => !ql || i.label.toLowerCase().includes(ql) || (i.sub || '').toLowerCase().includes(ql));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', maxHeight: desktop ? '70vh' : '100%' }}>
      <div style={{ padding: desktop ? 14 : 0, borderBottom: desktop ? '1px solid var(--hairline)' : 'none' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: desktop ? '0' : '0 0 10px' }}>
          <Icon name="search" size={16} color="var(--fg-3)"/>
          <input autoFocus value={q} onChange={e => setQ(e.target.value)} placeholder="Search projects, apps, sections…"
            style={{ flex: 1, background: 'transparent', border: 'none', outline: 'none', color: 'var(--fg)', fontSize: 'var(--t-body)', fontFamily: 'inherit' }}/>
          {desktop && <span className="kbd">esc</span>}
        </div>
      </div>
      <div style={{ flex: 1, overflowY: 'auto', padding: 6 }} className="scroll-hide">
        {['section', 'project', 'app'].map(group => {
          const groupItems = filtered.filter(i => i.kind === group);
          if (!groupItems.length) return null;
          const labels = { section: 'Navigate', project: 'Projects', app: 'Apps' };
          return (
            <div key={group} style={{ marginTop: 4 }}>
              <div className="mono" style={{ padding: '8px 12px 4px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{labels[group]}</div>
              {groupItems.slice(0, 8).map((i, k) => (
                <button key={i.kind + ':' + i.label + ':' + k} onClick={() => onPick(i.key)} style={{
                  display: 'flex', alignItems: 'center', gap: 12, width: '100%',
                  padding: '9px 12px', borderRadius: 7, background: 'transparent',
                  border: 'none', cursor: 'pointer', fontFamily: 'inherit',
                  color: 'var(--fg)',
                }}
                onMouseEnter={e => e.currentTarget.style.background = 'var(--surface-2)'}
                onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
                >
                  <Icon name={i.icon} size={16} color="var(--fg-3)"/>
                  <span style={{ fontSize: 'var(--t-md)', fontWeight: 500 }}>{i.label}</span>
                  <span style={{ flex: 1 }}/>
                  <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{i.sub}</span>
                </button>
              ))}
            </div>
          );
        })}
        {!filtered.length && <div style={{ padding: 24, textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>No matches</div>}
      </div>
      {desktop && (
        <div style={{
          borderTop: '1px solid var(--hairline)', padding: '10px 14px',
          display: 'flex', flexWrap: 'wrap', gap: '8px 16px',
          fontSize: 'var(--t-xs)', color: 'var(--fg-4)', alignItems: 'center',
        }}>
          {[
            { keys: ['⌘', 'K'], label: 'Szukaj' },
            { keys: ['Ctrl', 'N'], label: 'Nowa sesja' },
            { keys: ['⌘', '⇧'], label: 'Przełącznik sesji' },
            { keys: ['⌘', '←', '→'], label: 'Zmień sesję' },
            { keys: ['⌘', 'U'], label: 'Wyślij plik' },
            { keys: ['⌘', 'M'], label: 'Nagrywanie' },
          ].map((sc, i) => (
            <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              {sc.keys.map((kk, j) => <span key={j} className="kbd">{kk}</span>)}
              <span style={{ marginLeft: 2 }}>{sc.label}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Detail Router
// ─────────────────────────────────────────────────────────────
function DetailRouter({ detail, compact, tab, onChangeTab, openFile, onOpenFile, onBack, onPreview }) {
  if (!detail) return null;
  switch (detail.kind) {
    // Library detail (Phase 1) replaces the legacy ProjectDetail stub in
    // details.jsx — both areas and projects share the same component.
    case 'area':    return window.LibraryDetail
      ? <window.LibraryDetail kind="area" item={detail.payload} compact={compact} tab={tab} onChangeTab={onChangeTab} openFile={openFile} onOpenFile={onOpenFile} onBack={onBack}/>
      : null;
    case 'project': return window.LibraryDetail
      ? <window.LibraryDetail kind="project" item={detail.payload} compact={compact} tab={tab} onChangeTab={onChangeTab} openFile={openFile} onOpenFile={onOpenFile} onBack={onBack}/>
      : (window.ProjectDetail
          ? <window.ProjectDetail project={detail.payload} compact={compact} onBack={onBack}/>
          : null);
    case 'folder':  return window.ShareFolderDetail
      ? <window.ShareFolderDetail folder={detail.payload} compact={compact} onBack={onBack} onPreview={onPreview}/>
      : null;
    case 'log':     return window.LogDetail
      ? <window.LogDetail line={detail.payload} compact={compact} onBack={onBack}/>
      : null;
    case 'service': return window.ServiceDetail
      ? <window.ServiceDetail service={detail.payload} compact={compact} onBack={onBack}/>
      : null;
    default: return null;
  }
}

// ─────────────────────────────────────────────────────────────
// Exports + mount
// ─────────────────────────────────────────────────────────────
Object.assign(window, { DataProvider, useHubData, MobileHub, DesktopHub, ResponsiveHub, DetailRouter, CommandPalette });

const _rootEl = document.getElementById('root');
if (_rootEl) {
  // MediaPlayerProvider wraps ResponsiveHub (above the section router AND the
  // Mobile/Desktop shell swap) so its persistent <audio>/<video> survive
  // navigation. Inside ToastProvider so the engine can surface errors. Degrades
  // to a bare ResponsiveHub if the engine module is absent.
  const _Shell = window.MediaPlayerProvider
    ? <window.MediaPlayerProvider><ResponsiveHub /></window.MediaPlayerProvider>
    : <ResponsiveHub />;
  ReactDOM.createRoot(_rootEl).render(
    <DataProvider>
      <UnreadProvider>
        <ToastProvider>
          {_Shell}
        </ToastProvider>
      </UnreadProvider>
    </DataProvider>
  );

  // Fade out + remove the boot splash (index.html, a #root sibling) once React
  // has committed its first paint. Double-rAF guarantees the app is on screen
  // behind the splash before we start the cross-fade, so there's no flash of
  // empty shell. If app.jsx never reaches here (compile error) the splash
  // stays up — a useful "still booting / broken" signal rather than a blank.
  (function hideOrbitSplash() {
    const el = document.getElementById('orbit-splash');
    if (!el) return;
    let started = false;
    const start = () => {
      if (started) return;
      started = true;
      // Drive the fade with INLINE styles — they beat the stylesheet rule
      // unconditionally, so the splash always clears even if class-based CSS
      // or the .is-hiding rule is somehow shadowed. Then hard-remove.
      el.classList.add('is-hiding');
      el.style.transition = 'opacity 0.5s ease';
      el.style.opacity = '0';
      el.style.pointerEvents = 'none';
      setTimeout(() => {
        try { el.style.display = 'none'; el.remove(); } catch (_) {}
      }, 600);
    };
    // Primary: cross-fade once the app's first paint commits.
    requestAnimationFrame(() => requestAnimationFrame(start));
    // Fallbacks: rAF is throttled when the tab is backgrounded or under a
    // headless compositor — guarantee the splash always clears.
    setTimeout(start, 1200);
    setTimeout(start, 3000);
  })();
}
