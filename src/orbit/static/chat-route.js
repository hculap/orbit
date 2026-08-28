'use strict';
// chat-route.js — the overlay segment of the chat URL:
//   /chat/<sid>          … the session (as before)
//   /chat/<sid>/gallery  … + the artifact gallery modal over it
//   /chat/<sid>/issues   … + the project/area Issues modal over it
//
// The gallery and the Issues panel open as MODALS layered over the live
// session (the ttyd terminal must stay attached underneath), but they are
// still real ROUTES: the URL is the single source of truth, so browser /
// Android Back closes the modal and a deep link opens it. Keeping a
// useState alongside the URL would drift, and the drift only ever shows up
// on Back — the exact case this adds.
//
// Why a path segment and not `?overlay=`: parseRoute strips `?` and `#`
// before doing anything, useRouter's `path` state is seeded from
// `location.pathname` alone (so a query survives neither a popstate re-read
// nor the next routerReplace(buildPath(...)) that every session select
// fires), a query disables the PWA lastPath restore, and the service worker
// caches the app shell per-query-string. A suffix costs one local router
// change and matches two existing house patterns (/settings/<tab>,
// /share/<dir>/f/<file>).
//
// JSX-free + dependency-free so `node --test` can require() it — parseRoute /
// buildPath live in a Babel file and are not requireable from node. Same
// shape as share-route.js, which owns the /share encoding the same way.
// `window` is touched ONLY inside the publish guard at the very bottom.
(function () {
  // Reserved words in the segments after /chat. The guard is MANDATORY, not
  // polish: without it `/chat/gallery` parses as sessionId='gallery' and the
  // orchestrator fires hub:open-session with a garbage id. That URL is
  // reachable — the gallery button is gated on the ACTIVE session (restored
  // from localStorage), not on the URL session id, so it renders on bare /chat.
  const OVERLAYS = ['gallery', 'issues'];

  function isChatOverlay(seg) {
    return typeof seg === 'string' && OVERLAYS.indexOf(seg) >= 0;
  }

  // `parts` = the path segments AFTER the 'chat' / 'orchestrator' head.
  // The overlay is recognised in EITHER position (last segment), so both
  // /chat/<sid>/gallery and the session-less /chat/gallery resolve sanely.
  // An unknown trailing segment is simply ignored (same tolerance
  // /settings/<tab> has) rather than being mistaken for a session id.
  function parseChatTail(parts) {
    const list = Array.isArray(parts) ? parts.filter(Boolean) : [];
    const tail = list.length ? list[list.length - 1] : null;
    const first = list.length ? list[0] : null;
    return {
      sessionId: (first && !isChatOverlay(first)) ? first : undefined,
      overlay: isChatOverlay(tail) ? tail : undefined,
    };
  }

  // Inverse of parseChatTail. Returns '' when there is nothing to append, so
  // buildPath can concatenate unconditionally. An unknown overlay value is
  // dropped here too — a bad value can never reach the address bar.
  function buildChatTail(spec) {
    const s = spec || {};
    let out = '';
    if (s.sessionId) out += '/' + s.sessionId;
    if (isChatOverlay(s.overlay)) out += '/' + s.overlay;
    return out;
  }

  // Drop a trailing overlay segment from a chat path. Used by the router's
  // lastPath bookkeeping: the PWA cold-start restore must reopen the SESSION,
  // not a modal that has NO history entry behind it (Back would exit the app
  // instead of closing the modal). Any non-chat path is returned untouched.
  function stripChatOverlay(path) {
    if (typeof path !== 'string' || !path) return path;
    const clean = path.split('?')[0].split('#')[0];
    const parts = clean.replace(/^\/+/, '').split('/').filter(Boolean);
    if (parts.length < 2) return path;
    if (parts[0] !== 'chat' && parts[0] !== 'orchestrator') return path;
    if (!isChatOverlay(parts[parts.length - 1])) return path;
    return '/' + parts.slice(0, -1).join('/');
  }

  // Which overlay must SURVIVE when the app reflects the selected session back
  // into the address bar (orchestrator.jsx `onSelectSession` → routerReplace →
  // buildPath). `route` is a parsed route (window.parseRoute's output, so the
  // BASE_PATH is already stripped and /orchestrator is already normalised),
  // `sessionId` is the session being reflected.
  //
  // This exists as its own function because getting it wrong is invisible until
  // a cold load: that replace also runs on the hub:open-session replay, where
  // the selected id equals the URL's id, and returning undefined there strips
  // the segment about one fetch after first paint — the modal flashes open and
  // vanishes, killing every deep link / shared link / F5-with-modal-open.
  //
  // The rule is deliberately narrow: carry it ONLY when the URL already names
  // the same session. Selecting a DIFFERENT session is a real navigation and
  // must drop an overlay scoped to the one we are leaving.
  function overlayToCarry(route, sessionId) {
    if (!route || route.section !== 'orchestrator') return undefined;
    if (!sessionId || route.sessionId !== sessionId) return undefined;
    return isChatOverlay(route.overlay) ? route.overlay : undefined;
  }

  const api = { OVERLAYS, isChatOverlay, parseChatTail, buildChatTail, stripChatOverlay, overlayToCarry };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubChatRoute = api;
})();
