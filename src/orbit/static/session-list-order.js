'use strict';
// Pure logic for the orchestrator session LIST (the left sidebar / mobile
// "Sesje" sheet) — ordering + the "older than a week" fold.
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). The .jsx
// `SessionList` renderer is thin glue on top of these functions.
//
// `window` is referenced ONLY inside the publish guard at the very bottom, never
// at module load, so `require()` under Node never touches a missing global.
//
// Immutability: every function returns NEW arrays/objects; inputs are never
// mutated (the session summaries from the backend are treated as read-only).
(function () {
  // Sessions whose last activity is older than this fold under the "show more"
  // divider (matches relTime()'s 7-day "Nd ago" → date boundary in
  // components.jsx). `updated_at` is epoch SECONDS, so this is in seconds too.
  const WEEK_S = 7 * 24 * 60 * 60;

  // A session is "open" when it currently holds a warm tmux slot — i.e. it
  // appears in the /api/orchestrator/pool snapshot (poolStatusById[id] is
  // 'hot' or 'cooling'). These are the live REPLs the user expects pinned to
  // the very top.
  function isOpen(session, poolStatusById) {
    return !!(session && session.id && poolStatusById && poolStatusById[session.id]);
  }

  function _updatedAt(session) {
    const n = Number(session && session.updated_at);
    return isFinite(n) ? n : 0;
  }

  // Render rank for the primary sort:
  //   0 = open (live tmux)      — always at the very top
  //   1 = pinned (not open)     — explicit "keep prominent" signal floats next
  //   2 = everything else       — ordered purely by recency
  // Within a rank, newest-last-message first.
  function _rank(session, poolStatusById) {
    if (isOpen(session, poolStatusById)) return 0;
    if (session && session.pinned) return 1;
    return 2;
  }

  // Order the sessions the way the sidebar should show them:
  //   [ open (tmux), then pinned, then the rest ] — each tier by updated_at desc.
  // Returns a NEW array; the backend's pinned-first ordering is overridden here
  // because "open at the top" is the stronger signal the user asked for.
  function sortSessions(sessions, poolStatusById) {
    const list = Array.isArray(sessions) ? sessions.slice() : [];
    // Stable decorate-sort-undecorate so equal keys keep input order.
    return list
      .map((s, i) => ({ s, i, r: _rank(s, poolStatusById), t: _updatedAt(s) }))
      .sort((a, b) => (a.r - b.r) || (b.t - a.t) || (a.i - b.i))
      .map((d) => d.s);
  }

  // Split the ordered list into what shows immediately vs. what folds under the
  // "show more" divider. A session stays VISIBLE when it is open (live tmux),
  // pinned, the currently-active session, or its last message is within
  // `maxAgeS` (default a week). Everything else (stale, closed, not pinned, not
  // active) goes to `hidden`.
  //
  // Because the sort already puts open+pinned first and the stale remainder
  // last, `hidden` is always a contiguous tail of `ordered` — the divider sits
  // at a single clean boundary.
  //
  // opts: { maxAgeS?: number, activeId?: string|null, minVisible?: number }
  // nowS: current time in epoch SECONDS (caller passes Date.now()/1000).
  function partition(sessions, poolStatusById, nowS, opts) {
    const o = opts || {};
    const maxAge = (typeof o.maxAgeS === 'number' && isFinite(o.maxAgeS)) ? o.maxAgeS : WEEK_S;
    const activeId = o.activeId || null;
    // Always keep at least this many newest rows above the fold so the list is
    // never just a bare "show more" divider — matters on surfaces with no other
    // escape hatch (the Agent panel passes no pool/active state, so an agent
    // untouched for a week would otherwise fold away entirely). Default 1.
    const minVisible = (typeof o.minVisible === 'number' && o.minVisible >= 0)
      ? Math.floor(o.minVisible) : 1;
    const now = (typeof nowS === 'number' && isFinite(nowS)) ? nowS : 0;
    const ordered = sortSessions(sessions, poolStatusById);

    const keepVisible = (s) => {
      if (isOpen(s, poolStatusById)) return true;          // live tmux
      if (s && s.pinned) return true;                      // pinned float
      // The active session stays visible even when stale — a localized recency
      // quirk at the divider boundary is the accepted cost of always being able
      // to see the session you're in.
      if (s && activeId && s.id === activeId) return true; // open in the chat now
      return (now - _updatedAt(s)) < maxAge;               // recent enough
    };

    const visible = [];
    const hidden = [];
    for (const s of ordered) (keepVisible(s) ? visible : hidden).push(s);
    // Promote the newest hidden rows (front of `hidden`, since it's the
    // recency-sorted stale tail) until the floor is met — keeps both blocks a
    // clean contiguous split of `ordered`.
    while (visible.length < minVisible && hidden.length > 0) {
      visible.push(hidden.shift());
    }
    return { ordered, visible, hidden };
  }

  // Stable membership signature — changes only when the SET of session ids
  // changes (not on reorder / timestamp bumps from a poll), so the sidebar can
  // reset the "show more" expansion when the agent scope actually changes
  // without collapsing on every refresh.
  function membershipKey(sessions) {
    const ids = [];
    for (const s of (Array.isArray(sessions) ? sessions : [])) {
      if (s && s.id) ids.push(String(s.id));
    }
    ids.sort();
    return ids.join(',');
  }

  // Where to send the user after closing / deleting a session. The rule:
  //   * the removed session is NOT the one on screen → stay put ('none')
  //   * it IS on screen → jump to the next OPEN session in the SAME agent
  //     (most recent first); else an OPEN session in ANOTHER agent; else, when
  //     nothing is open anywhere, fall back to the agents directory ('agents').
  // "Open" = has a live warm slot (present in poolStatusById). The removed
  // session is always excluded (its own slot is freed by the close/delete).
  //
  // opts: { closedId, activeId, sessions:[{id,cwd,updated_at,archived}],
  //         poolStatusById:{id->'hot'|'cooling'}, agentCwd:string|null }
  // Returns { action: 'none' } | { action: 'session', id } | { action: 'agents' }.
  function pickRedirectTarget(opts) {
    const o = opts || {};
    const closedId = o.closedId || null;
    const activeId = o.activeId || null;
    // Removing a session other than the one displayed → don't navigate at all.
    if (!closedId || closedId !== activeId) return { action: 'none' };

    const sessions = Array.isArray(o.sessions) ? o.sessions : [];
    const pool = o.poolStatusById || {};
    const agentCwd = o.agentCwd || null;
    const byRecency = (a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0);
    const open = sessions.filter((s) =>
      s && s.id && s.id !== closedId && !s.archived && !!pool[s.id]);

    const sameAgent = open.filter((s) => (s.cwd || null) === agentCwd).sort(byRecency);
    if (sameAgent.length) return { action: 'session', id: sameAgent[0].id };

    const otherAgent = open.filter((s) => (s.cwd || null) !== agentCwd).sort(byRecency);
    if (otherAgent.length) return { action: 'session', id: otherAgent[0].id };

    return { action: 'agents' };
  }

  // ── recently-closed guard ───────────────────────────────────────────────
  // Terminal mode (the default chat surface) keeps the DISPLAYED session warm by
  // POSTing /term/ensure, which COLD-SPAWNS the tmux slot if it's gone. So a
  // session the user just closed gets instantly re-spawned by the terminal
  // ("I close it and it comes back"). This guard records explicitly-closed
  // session ids for a short window; the terminal skips its ensure for them, so a
  // close actually sticks. Re-opening a session (onSelect) clears its mark.
  const CLOSED_TTL_MS = 20000;
  const _closedAt = (typeof window !== 'undefined' && window.__hubClosedAt)
    ? window.__hubClosedAt
    : new Map();
  if (typeof window !== 'undefined') window.__hubClosedAt = _closedAt;
  const _nowMs = () => ((typeof Date !== 'undefined' && Date.now) ? Date.now() : 0);
  const closedGuard = {
    mark(id) { if (id) _closedAt.set(String(id), _nowMs()); },
    clear(id) { if (id) _closedAt.delete(String(id)); },
    isClosed(id) {
      if (!id) return false;
      const t = _closedAt.get(String(id));
      if (t == null) return false;
      if (_nowMs() - t > CLOSED_TTL_MS) { _closedAt.delete(String(id)); return false; }
      return true;
    },
  };

  const api = {
    WEEK_S, isOpen, sortSessions, partition, membershipKey, pickRedirectTarget,
    closedGuard,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubSessionListOrder = api;
})();
