// router.jsx — tiny URL router for the hub.
//
// Why hand-rolled? The dashboard has no build step (CDN React + babel-
// standalone). React-router-dom from a CDN works but ships ~30 kB of
// abstractions we don't need. ~150 LOC + History API is enough.
//
// Public surface:
//   useRouter()                    → { path, push, replace, route }
//   parseRoute(path)               → { section, detail?, tab?, sessionId? }
//   buildPath({section, ...})      → string (relative, no BASE_PATH)
//   addBase(p) / stripBase(p)      → BASE_PATH-aware
//
// Single source of truth: window.location. State sync direction:
//   URL → state on initial mount + popstate (browser back/forward).
//   State → URL on user navigation (push/replace).
// Cross-instance sync is event-based: every push/replace dispatches a
// `router:change` window event so sibling useRouter() instances re-read.
//
// localStorage `lastPath` is written on every navigation. On cold start,
// if the user landed on `/` (no deep link) AND a recent lastPath exists,
// we replaceState into it — that's the "iOS PWA reopens to where I was"
// behaviour the user asked for.

const { useState: _routerUseState, useEffect: _routerUseEffect, useCallback: _routerUseCallback, useMemo: _routerUseMemo } = React;

const _LAST_PATH_KEY = 'hub:lastPath';
const _LAST_PATH_TTL_MS = 7 * 24 * 60 * 60 * 1000;  // 7 days

// Sections that exist as standalone top-level routes. Everything else is
// treated as a typo / 404 fallback (we route to '/').
const _ROUTE_SECTIONS = new Set([
  // 'chat' is the canonical chat URL prefix; 'orchestrator' is kept as a
  // back-compat alias so old bookmarks / notifications still resolve.
  // parseRoute normalises both to `section: 'orchestrator'`.
  'agents', 'chat', 'orchestrator',
  'tasks', 'inbox', 'ideas', 'apps', 'share',
  'projects', 'areas', 'resources', 'archive', 'global',
  'skills', 'scheduler', 'secrets',
  'system', 'logs', 'settings', 'design',
]);

// Library detail tabs. Used to validate the third path segment for
// /areas/<id>/<tab> URLs and to gate which tab the LibraryDetail mounts.
const _LIBRARY_TABS = new Set(['overview', 'agent', 'gallery', 'secrets', 'files', 'gitignore', 'branches', 'prs', 'issues']);

function _baseFromWindow() {
  return (typeof window !== 'undefined' && window.HUB_BASE_PATH) || '';
}

function addBase(p) {
  const base = _baseFromWindow();
  if (!p) return base || '/';
  if (!base) return p;
  // Avoid double-prefixing if caller already passed a base-prefixed path.
  if (p === base || p.startsWith(base + '/')) return p;
  return base + p;
}

function stripBase(p) {
  const base = _baseFromWindow();
  if (!base) return p || '/';
  if (p === base) return '/';
  if (p && p.startsWith(base + '/')) return p.slice(base.length);
  return p || '/';
}

// Parse `/areas/Foo/files` or `/orchestrator/<sid>` etc. Falls back to
// `{section: 'orchestrator'}` (the home/default) for anything unrecognized.
function parseRoute(rawPath) {
  const path = stripBase((rawPath || '/').split('?')[0].split('#')[0]);
  if (!path || path === '/' || path === '') {
    // `/` is the cold-start landing — caller decides whether to redirect
    // via lastPath or stay home. We tag it so the caller can branch.
    return { section: null, isHome: true, raw: '/' };
  }
  const parts = path.replace(/^\/+/, '').split('/').filter(Boolean);
  const head = parts[0];

  // /<section>
  if (!_ROUTE_SECTIONS.has(head)) {
    return { section: null, isHome: true, raw: path };
  }

  // Library kinds carry detail + optional tab + optional open-file path.
  if (head === 'areas' || head === 'projects' || head === 'resources') {
    if (parts.length === 1) return { section: head, raw: path };
    const kind = head === 'areas' ? 'area' : (head === 'projects' ? 'project' : 'resource');
    // Tab is the LAST segment when it's a known tab name; if the SECOND-to-last
    // is a known tab then the last segment is an open-file path (the Files tab
    // deep-link — buildPath percent-encodes the file's internal slashes so it
    // stays a single segment). Otherwise the trailing segments are all lib_id
    // (e.g. nested project paths).
    const last = parts[parts.length - 1];
    const secondLast = parts.length >= 2 ? parts[parts.length - 2] : null;
    let libId, tab, openFile;
    if (parts.length >= 3 && _LIBRARY_TABS.has(last)) {
      tab = last;
      libId = parts.slice(1, -1).join('/');
    } else if (parts.length >= 4 && secondLast && _LIBRARY_TABS.has(secondLast)) {
      tab = secondLast;
      libId = parts.slice(1, -2).join('/');
      try { openFile = decodeURIComponent(last); } catch (_e) { openFile = last; }
    } else {
      tab = 'overview';
      libId = parts.slice(1).join('/');
    }
    const detail = { kind, lib_id: libId, name: libId.split('/').pop() };
    if (openFile) detail.openFile = openFile;
    return { section: head, detail, tab, raw: path };
  }

  // /skills/<name> — single segment after the section is the skill name.
  // Skill names can contain dots, dashes, etc. but no slashes (validated by
  // backend's safe_skill_name), so we don't have to handle nested paths.
  if (head === 'skills' && parts.length >= 2) {
    return { section: 'skills', skillName: decodeURIComponent(parts[1]), raw: path };
  }

  // /scheduler/<job_id> — mirrors the /skills/<name> pattern.
  if (head === 'scheduler' && parts.length >= 2) {
    return { section: 'scheduler', jobId: decodeURIComponent(parts[1]), raw: path };
  }

  // Orchestrator: optional session id segment.
  if ((head === 'chat' || head === 'orchestrator') && parts.length >= 2) {
    // Both /chat/<sid> and /orchestrator/<sid> normalise to the same
    // section so existing checks (`section === 'orchestrator'`) keep
    // working without a mass rename.
    return { section: 'orchestrator', sessionId: parts[1], raw: path };
  }
  if (head === 'chat') {
    // Bare /chat is the global agent's home — same as bare /orchestrator.
    return { section: 'orchestrator', raw: path };
  }

  // /share/<dir>/f/<file> — the Sync browser encodes its current folder +
  // open file into the URL so browser Back steps back one folder/file instead
  // of escaping the whole section. Delegated to the JSX-free HubShareRoute
  // helper (loaded as a plain <script> before this module).
  if (head === 'share' && parts.length >= 2) {
    const sr = (typeof window !== 'undefined' && window.HubShareRoute)
      ? window.HubShareRoute.parseSharePath(parts.slice(1))
      : {};
    return { section: 'share', sharePath: sr.sharePath, shareFile: sr.shareFile, raw: path };
  }

  // Plain section: /tasks, /apps, /share, /system, /logs, /settings, /archive
  return { section: head, raw: path };
}

function buildPath(spec) {
  if (!spec) return '/';
  const { section, detail, tab, sessionId, skillName, jobId } = spec;
  if (!section) return '/';
  // Always emit /chat (the new pretty form). /orchestrator parses the same
  // way so old links still work, but new pushes use /chat.
  if (section === 'orchestrator' && sessionId) return `/chat/${sessionId}`;
  if (section === 'orchestrator') return '/chat';
  if (section === 'skills' && skillName) return `/skills/${encodeURIComponent(skillName)}`;
  if (section === 'scheduler' && jobId) return `/scheduler/${encodeURIComponent(jobId)}`;
  if ((section === 'areas' || section === 'projects' || section === 'resources') && detail) {
    const id = detail.lib_id || detail.name || '';
    if (!id) return `/${section}`;
    let p = `/${section}/${id}`;
    if (tab && tab !== 'overview' && _LIBRARY_TABS.has(tab)) p += `/${tab}`;
    // Files tab deep-links the open file as a single slash-encoded segment.
    if (tab === 'files' && detail.openFile) p += `/${encodeURIComponent(detail.openFile)}`;
    return p;
  }
  // Share browser: HubShareRoute owns the /share/<dir>/f/<file> encoding.
  if (section === 'share') {
    return (typeof window !== 'undefined' && window.HubShareRoute)
      ? window.HubShareRoute.buildSharePath(spec)
      : '/share';
  }
  return `/${section}`;
}

// localStorage helpers — never throw.
function _readLastPath() {
  try {
    const raw = localStorage.getItem(_LAST_PATH_KEY);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    if (!obj || typeof obj.path !== 'string') return null;
    if (typeof obj.ts !== 'number' || (Date.now() - obj.ts) > _LAST_PATH_TTL_MS) return null;
    if (obj.path === '/' || !obj.path) return null;
    return obj.path;
  } catch (_) { return null; }
}
function _writeLastPath(path) {
  try {
    if (!path || path === '/') return;
    localStorage.setItem(_LAST_PATH_KEY, JSON.stringify({ path, ts: Date.now() }));
  } catch (_) { /* ignore quota / private mode */ }
}

// Public hook — single subscription to `popstate`, broadcasts the current
// path and exposes push/replace.
function useRouter() {
  const [path, setPath] = _routerUseState(() =>
    stripBase(typeof window !== 'undefined' ? window.location.pathname : '/'),
  );

  _routerUseEffect(() => {
    const onPop = () => setPath(stripBase(window.location.pathname));
    window.addEventListener('popstate', onPop);
    window.addEventListener('router:change', onPop);
    return () => {
      window.removeEventListener('popstate', onPop);
      window.removeEventListener('router:change', onPop);
    };
  }, []);

  const push = _routerUseCallback((next) => {
    if (!next) return;
    if (next === path) return;
    try { window.history.pushState(null, '', addBase(next)); } catch (_) { /* ignore */ }
    setPath(next);
    window.dispatchEvent(new Event('router:change'));
    _writeLastPath(next);
  }, [path]);

  const replace = _routerUseCallback((next) => {
    if (!next) return;
    if (next === path) {
      // Even if path didn't change, a fresh write keeps the lastPath ts current.
      _writeLastPath(next);
      return;
    }
    try { window.history.replaceState(null, '', addBase(next)); } catch (_) { /* ignore */ }
    setPath(next);
    window.dispatchEvent(new Event('router:change'));
    _writeLastPath(next);
  }, [path]);

  const route = _routerUseMemo(() => parseRoute(path), [path]);

  return { path, push, replace, route };
}

// Cold-start redirect. Call once on mount: if the user landed on `/`
// (no deep link) AND lastPath is fresh, replace into it. Otherwise no-op.
function useColdStartRedirect(replace, currentPath) {
  _routerUseEffect(() => {
    if (currentPath !== '/') return;  // user has a real deep link
    // Don't clobber explicit query params (e.g. legacy ?session=<sid>
    // from the SW notification handler).
    if (typeof window !== 'undefined' && window.location && window.location.search) return;
    const last = _readLastPath();
    if (!last || last === '/') return;
    replace(last);
    // run-once on mount — currentPath/replace deps would re-fire on every
    // nav, but we only want to consider lastPath at the very first mount
    // before any user action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

// Imperative replace — for callers that don't have a useRouter() handle in
// scope (e.g. orchestrator's onSelectSession, dispatched from a deeply
// nested useCallback). Performs the same triplet `useRouter().replace`
// does — replaceState + _writeLastPath — so lastPath stays current.
// Live `useRouter()` instances won't see the change in their React state
// until the next popstate, which is harmless because they re-parse from
// `window.location` on every pop and only push/replace from user actions.
function routerReplace(next) {
  if (!next) return;
  try { window.history.replaceState(null, '', addBase(next)); } catch (_) { /* ignore */ }
  _writeLastPath(next);
  window.dispatchEvent(new Event('router:change'));
}

// True when a click on an <a> should be left to the browser's native
// handling (open in new tab/window, middle-click, modifier-click, already
// prevented). Nav anchors call this to decide whether to intercept the
// click for SPA routing or let the URL open normally. Without this the
// nav was <button>-only, so ⌘/Ctrl/middle-click had no href to act on.
function isModifiedClick(e) {
  return !!(
    e.defaultPrevented ||
    e.button !== 0 ||         // 1 = middle, 2 = right
    e.metaKey ||              // ⌘ on macOS
    e.ctrlKey ||             // Ctrl (new tab on Win/Linux)
    e.shiftKey ||            // new window
    e.altKey                  // download
  );
}

// Absolute (BASE_PATH-aware) URL for a top-level section key. Used as the
// `href` on nav anchors so they're real, ⌘-clickable links that ALSO drive
// SPA routing on a plain click.
function sectionHref(key) {
  try { return addBase(buildPath({ section: key })); } catch (_) { return addBase('/' + key); }
}

Object.assign(window, {
  useRouter, parseRoute, buildPath, addBase, stripBase, useColdStartRedirect,
  routerReplace, isModifiedClick, sectionHref,
});
