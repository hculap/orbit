'use strict';
// Pure logic for the desktop session switcher (⌘+⇧ hold-to-cycle overlay).
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). All branching
// logic the overlay needs — MRU ordering, the cycle state machine, the client
// activation log, the idle label — lives here and is unit-tested. The .jsx
// overlay is thin rendering + keyboard wiring on top of these functions.
//
// `window` is referenced ONLY inside the publish guard at the very bottom, never
// at module load, so `require()` under Node never touches a missing global.
//
// Immutability: every function returns new arrays/objects; inputs are never
// mutated (the pool slots from the backend are treated as read-only).
(function () {
  // Cyclic index — for ⇧-tap / arrow cycling (wraps past both ends).
  function wrapIndex(i, len) {
    if (!len || len <= 0) return 0;
    return ((i % len) + len) % len;
  }

  // Non-cyclic clamp — for re-pinning the selection after the pool shrinks
  // (a session was evicted mid-overlay) so we never point past the new end.
  function clampIndex(i, len) {
    if (!len || len <= 0) return 0;
    if (i < 0) return 0;
    if (i > len - 1) return len - 1;
    return i;
  }

  function advance(index, len) { return wrapIndex(index + 1, len); }
  function reverse(index, len) { return wrapIndex(index - 1, len); }

  // Order the warm-pool slots the way a Cmd+Tab switcher should:
  //   [ current session, then most-recently-used order, then idle-asc remainder ]
  // deduped by session_id. The current session shows first (as context) but is
  // NOT the default landing target — see defaultIndex(). MRU comes from a
  // client-side activation log because the server sorts the pool by uptime and
  // merely *viewing* a warm slot does not bump its server-side idle clock.
  function orderSessions(slots, mruIds, currentId) {
    const list = Array.isArray(slots) ? slots : [];
    const mru = Array.isArray(mruIds) ? mruIds : [];

    const byId = new Map();
    for (const s of list) {
      if (s && s.session_id && !byId.has(s.session_id)) byId.set(s.session_id, s);
    }

    const out = [];
    const used = new Set();

    if (currentId && byId.has(currentId)) {
      out.push(byId.get(currentId));
      used.add(currentId);
    }

    for (const id of mru) {
      if (used.has(id)) continue;
      if (byId.has(id)) { out.push(byId.get(id)); used.add(id); }
    }

    const remaining = list.filter((s) => s && s.session_id && !used.has(s.session_id));
    remaining.sort((a, b) => (a.idle_s || 0) - (b.idle_s || 0));
    for (const s of remaining) {
      if (!used.has(s.session_id)) { out.push(s); used.add(s.session_id); }
    }
    return out;
  }

  // The default highlighted row = the PREVIOUS session (index 1), so a single
  // ⌘+⇧ tap-and-release flips to the last session — the core Cmd+Tab win. With
  // only the current session (or none), clamp to 0.
  // INVARIANT: this assumes orderSessions keeps the current session at index 0;
  // if that ordering ever changes, this index-1 default must change with it.
  function defaultIndex(ordered) {
    return (Array.isArray(ordered) && ordered.length > 1) ? 1 : 0;
  }

  // Client MRU log: move (or insert) `id` to the front, deduped. Immutable.
  function pushActivation(mruIds, id) {
    const mru = Array.isArray(mruIds) ? mruIds : [];
    if (!id) return mru.slice();
    return [id].concat(mru.filter((x) => x !== id));
  }

  // Compact age label: 12s / 2m / 1h / 3d. Non-finite/negative → ''.
  function humanizeIdle(idleS) {
    if (typeof idleS !== 'number' || !isFinite(idleS) || idleS < 0) return '';
    const n = Math.floor(idleS);
    if (n < 60) return n + 's';
    if (n < 3600) return Math.floor(n / 60) + 'm';
    if (n < 86400) return Math.floor(n / 3600) + 'h';
    return Math.floor(n / 86400) + 'd';
  }

  const api = {
    wrapIndex, clampIndex, advance, reverse,
    orderSessions, defaultIndex, pushActivation, humanizeIdle,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubSessionOrder = api;
})();
