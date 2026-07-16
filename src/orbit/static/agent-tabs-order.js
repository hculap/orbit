'use strict';
// Pure logic for the "active agents" tab strip (the row between the chat
// header and the window) + the ⌘←/→ agent-cycling shortcut.
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). All the
// branching the strip needs — grouping warm pool slots into one entry per
// agent, picking each agent's "top" (most-recently-active) session, the stable
// render order, and the prev/next cycle target — lives here and is unit-tested.
// The .jsx renderer + keyboard wiring is thin glue on top.
//
// `window` is referenced ONLY inside the publish guard at the very bottom, never
// at module load, so `require()` under Node never touches a missing global.
//
// Immutability: every function returns new arrays/objects; inputs are never
// mutated (the pool slots from the backend are treated as read-only).
(function () {
  // Canonical agent key, shared by BOTH the pool grouping and the chat's active
  // scope so the highlighted tab matches the session on screen. The stable
  // `lib_id` ("areas/Work", "projects/my-project") is the key when present;
  // otherwise we fall back to the human name prefixed with '@' so cwd-less
  // ("Global") and folder-rooted-without-lib sessions still group sanely.
  // INVARIANT: agentName/_agent_name_for collapse home/null cwd → "Global", so
  // every global session keys to '@Global' on both sides.
  function agentKey(libId, name) {
    const lid = (typeof libId === 'string') ? libId.trim() : '';
    if (lid) return lid;
    const nm = (typeof name === 'string' && name.trim()) ? name.trim() : 'Global';
    return '@' + nm;
  }

  const GLOBAL_KEY = '@Global';

  // Group the warm-pool slots into one tab per agent, ordered for stable
  // rendering + predictable ⌘←/→ cycling: Global first, then case-insensitive
  // alphabetical by name (tie-break on key). Each group exposes:
  //   { key, name, libId, count, topSessionId, minIdle, sessions:[{session_id,idle_s,title}] }
  // `topSessionId` is the most-recently-active session of that agent (smallest
  // idle_s) — what ⌘←/→ and a tab click jump to ("the one at the very top").
  //
  // `current` (optional) = { key, name, libId, sessionId } describes the chat's
  // active scope. It's folded in so the active agent always has a tab even
  // before its slot shows up in the pool poll (or in programmatic mode with no
  // pool at all); a synthesized group falls back to current.sessionId as its
  // top target.
  function groupAgents(slots, current) {
    const list = Array.isArray(slots) ? slots : [];
    const groups = new Map();

    const ensure = (key, name, libId) => {
      let g = groups.get(key);
      if (!g) {
        g = {
          key,
          name: (typeof name === 'string' && name.trim()) ? name.trim() : 'Global',
          libId: (typeof libId === 'string') ? libId : '',
          count: 0,
          topSessionId: null,
          minIdle: Infinity,
          topTs: -Infinity,
          sessions: [],
        };
        groups.set(key, g);
      }
      return g;
    };

    for (const s of list) {
      if (!s || !s.session_id) continue;
      const name = (typeof s.agent === 'string' && s.agent.trim()) ? s.agent.trim() : 'Global';
      const libId = (typeof s.lib_id === 'string') ? s.lib_id : '';
      const g = ensure(agentKey(libId, name), name, libId);
      const idle = (typeof s.idle_s === 'number' && isFinite(s.idle_s)) ? s.idle_s : Infinity;
      const ts = (typeof s.updated_at === 'number' && isFinite(s.updated_at)) ? s.updated_at : 0;
      g.sessions.push({
        session_id: s.session_id, idle_s: idle, updated_at: ts,
        title: (s.title || ''), persistent: !!s.persistent,
      });
      g.count += 1;
      // topSessionId = the agent's MOST RECENT session (greatest updated_at) so
      // opening a tab lands on the last session you used, even if it's a cooling
      // (yellow) slot — NOT the least-idle (warmest/green) one. Tie-break on
      // smaller idle. When updated_at is absent everywhere (ts=0), this reduces
      // to the old least-idle behaviour.
      if (g.topSessionId === null || ts > g.topTs || (ts === g.topTs && idle < g.minIdle)) {
        g.topTs = ts; g.minIdle = idle; g.topSessionId = s.session_id;
      }
    }

    // Fold in the active scope so its tab always exists + is targetable.
    if (current && current.key) {
      const g = ensure(current.key, current.name, current.libId);
      if (!g.topSessionId && current.sessionId) g.topSessionId = current.sessionId;
    }

    // Within each agent, order its sessions most-recent-first (greatest
    // updated_at, tie-break least idle) — the flyout / mobile in-menu list render
    // this verbatim. (All sessions in a group are open-pool slots, i.e. one tier,
    // so this is just the sidebar's recency order within that tier.)
    for (const g of groups.values()) {
      g.sessions.sort((a, b) => (b.updated_at - a.updated_at) || ((a.idle_s || Infinity) - (b.idle_s || Infinity)));
    }

    const arr = Array.from(groups.values());
    arr.sort((a, b) => {
      const ag = a.key === GLOBAL_KEY ? 0 : 1;
      const bg = b.key === GLOBAL_KEY ? 0 : 1;
      if (ag !== bg) return ag - bg;
      const an = a.name.toLowerCase();
      const bn = b.name.toLowerCase();
      if (an < bn) return -1;
      if (an > bn) return 1;
      if (a.key < b.key) return -1;
      if (a.key > b.key) return 1;
      return 0;
    });
    return arr;
  }

  // Prev/next agent target for ⌘←/→. Returns the session id to open (the next
  // agent's top session), or null when there's nothing to switch to. `dir`:
  // >= 0 → next (⌘→), < 0 → prev (⌘←). Wraps at both ends. An unknown
  // currentKey (active scope not yet grouped) starts the walk from index 0.
  function cycleAgentTarget(groups, currentKey, dir) {
    const arr = Array.isArray(groups) ? groups : [];
    const n = arr.length;
    if (n === 0) return null;
    let idx = arr.findIndex((g) => g && g.key === currentKey);
    if (idx === -1) idx = 0;
    const step = (dir >= 0) ? 1 : -1;
    const next = ((idx + step) % n + n) % n;
    const g = arr[next];
    return g ? (g.topSessionId || null) : null;
  }

  // Reorder grouped tabs to honour a user's saved drag-and-drop order. Keys in
  // `savedOrder` come first, in that exact sequence (only those actually present
  // among `groups`); any group NOT in savedOrder is appended afterwards in its
  // incoming (default Global-first-alpha) order. So a brand-new agent shows up
  // at the END until the user drags it, and a saved key for an agent that has no
  // tab right now is simply skipped (its slot is remembered for when it returns).
  // Pure: returns a NEW array, never mutates inputs.
  function applyOrder(groups, savedOrder) {
    const list = Array.isArray(groups) ? groups.slice() : [];
    const order = Array.isArray(savedOrder) ? savedOrder : [];
    if (!order.length) return list;
    const byKey = new Map();
    for (const g of list) { if (g && g.key && !byKey.has(g.key)) byKey.set(g.key, g); }
    const out = [];
    const used = new Set();
    for (const key of order) {
      if (byKey.has(key) && !used.has(key)) { out.push(byKey.get(key)); used.add(key); }
    }
    for (const g of list) {
      if (g && g.key && !used.has(g.key)) { out.push(g); used.add(g.key); }
    }
    return out;
  }

  // Where to land after CLOSING every session of one agent (its tab vanishes).
  //   * closing an agent you're NOT currently in → stay put ('none')
  //   * closing the agent you ARE in → jump to the neighbouring tab (next, else
  //     previous, else any surviving tab with an openable session); if no tab
  //     survives → the agents directory ('agents').
  // `groups` is the CURRENTLY DISPLAYED (already-ordered) list. Returns
  // { action:'none' } | { action:'session', id } | { action:'agents' }.
  function pickAgentCloseTarget(groups, closedKey, currentKey) {
    if (!closedKey || closedKey !== currentKey) return { action: 'none' };
    const list = Array.isArray(groups) ? groups : [];
    const idx = list.findIndex((g) => g && g.key === closedKey);
    const survivors = list.filter((g) => g && g.key && g.key !== closedKey);
    if (!survivors.length) return { action: 'agents' };
    // Prefer the neighbour that slides into the closed tab's slot (next), then
    // the previous one, then the first survivor that actually has a session.
    const ordered = [];
    if (idx >= 0) {
      for (let i = idx + 1; i < list.length; i++) ordered.push(list[i]);
      for (let i = idx - 1; i >= 0; i--) ordered.push(list[i]);
    }
    for (const g of survivors) ordered.push(g);
    for (const g of ordered) {
      if (g && g.key !== closedKey && g.topSessionId) return { action: 'session', id: g.topSessionId };
    }
    return { action: 'agents' };
  }

  // Plan a "close agent" action: which sessions to actually close, how many
  // keep-alive ones are spared, whether the tab will vanish, and where to land.
  //   * keep-alive (persistent) sessions are NEVER closed (spared)
  //   * the tab only vanishes when EVERY session was closable (no keep-alive left)
  //   * redirect is computed only when the tab vanishes (else stay put)
  // `ordered` = the displayed tab order; `activeKey` = the on-screen agent.
  // Returns { closableIds:[…], spared:number, willVanish:bool, decision }.
  function planAgentClose(group, ordered, activeKey) {
    const all = ((group && group.sessions) || []).filter((s) => s && s.session_id);
    const closableIds = all.filter((s) => !s.persistent).map((s) => s.session_id);
    const spared = all.length - closableIds.length;
    const willVanish = closableIds.length > 0 && closableIds.length === all.length;
    const decision = willVanish
      ? pickAgentCloseTarget(ordered, group && group.key, activeKey)
      : { action: 'none' };
    return { closableIds, spared, willVanish, decision };
  }

  // Resolve an agent's session scope from its tab key / lib_id, matching the
  // agents-directory convention: ''/global → cwd-less Global; 'areas/X' →
  // '~/Areas/X'; 'projects/X' → '~/Projects/X'. Anything else (a bare '@name'
  // folder-rooted-without-lib agent) degrades to Global. Used to build the
  // "new session" / "open latest" payloads for a tab.
  function scopeFromLibId(libId) {
    const lid = (typeof libId === 'string') ? libId.trim() : '';
    let m = /^areas\/(.+)$/.exec(lid);
    if (m) return { cwd: '~/Areas/' + m[1], libId: lid, isGlobal: false };
    m = /^projects\/(.+)$/.exec(lid);
    if (m) return { cwd: '~/Projects/' + m[1], libId: lid, isGlobal: false };
    return { cwd: null, libId: '', isGlobal: true };
  }

  // Move `fromKey` to sit at `toKey`'s slot within an ordered key list — the
  // array transform behind a drag-and-drop drop. Returns a NEW array; a no-op
  // (unknown key, or from===to) returns a copy unchanged. Pure.
  function moveKey(keys, fromKey, toKey) {
    const list = Array.isArray(keys) ? keys.slice() : [];
    if (fromKey === toKey) return list;
    const from = list.indexOf(fromKey);
    if (from === -1) return list;
    list.splice(from, 1);
    const to = list.indexOf(toKey);
    if (to === -1) { list.push(fromKey); return list; }
    list.splice(to, 0, fromKey);
    return list;
  }

  const api = {
    agentKey, groupAgents, cycleAgentTarget, GLOBAL_KEY,
    applyOrder, pickAgentCloseTarget, scopeFromLibId, moveKey, planAgentClose,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubAgentTabs = api;
})();
