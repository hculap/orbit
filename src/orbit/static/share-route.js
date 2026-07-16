// share-route.js — pure URL helpers for the Sync/Share browser route.
//
// Why a JSX-free plain script? The Share browser used to keep its whole
// navigation (current folder + open file) in React useState, so the browser
// Back button escaped /share entirely instead of stepping back one folder/
// file. The fix routes Share state through the URL — mirroring the library
// Files-tab pattern (router.jsx 4th-segment openFile). These two pure fns are
// the encode/decode pair; they live in a plain <script> (NOT type=text/babel)
// so router.jsx can call window.HubShareRoute AND node --test can require()
// them directly. Same dual-publish pattern as session-switcher-order.js.
//
//   /share                       → {}                       (Sync root)
//   /share/<dir>                 → {sharePath:'<dir>'}       (a folder)
//   /share/<dir>/f/<file>        → {sharePath, shareFile}    (file open)
//
// The whole relative path is percent-encoded into ONE segment (internal
// slashes survive — same trick as router.jsx:156). The literal '/f/' marker
// is the reserved 2nd-to-last segment; because we always slice from the END,
// a real folder literally named 'f' still round-trips.

(function () {
  // parts = the path segments AFTER 'share' (router passes parts.slice(1),
  // i.e. an already-split array, NOT a string).
  function parseSharePath(parts) {
    if (!Array.isArray(parts) || parts.length === 0) return {};
    const dec = (s) => { try { return decodeURIComponent(s); } catch (_e) { return s; } };
    if (parts.length >= 2 && parts[parts.length - 2] === 'f') {
      return {
        sharePath: dec(parts.slice(0, -2).join('/')),
        shareFile: dec(parts[parts.length - 1]),
      };
    }
    return { sharePath: dec(parts.join('/')) };
  }

  function buildSharePath(spec) {
    const s = spec || {};
    let p = '/share';
    if (s.sharePath) p += '/' + encodeURIComponent(s.sharePath);
    if (s.shareFile) p += '/f/' + encodeURIComponent(s.shareFile);
    return p;
  }

  const api = { parseSharePath, buildSharePath };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubShareRoute = api;
})();
