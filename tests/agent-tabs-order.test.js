'use strict';
// Pure-logic unit tests for the "active agents" tab strip + ⌘←/→ agent cycle.
// No-build CDN-React project → the branching logic lives in a JSX-free module
// testable with Node's built-in runner (zero deps):
//   node --test tests/agent-tabs-order.test.js
//
// The .jsx strip (rendering / keyboard wiring) is covered by the manual smoke
// checklist in the PR description — there is no jsdom in this repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const o = require('../src/orbit/static/agent-tabs-order.js');

const slot = (id, agent, lib_id, idle_s) => ({ session_id: id, agent, lib_id, idle_s, title: '' });

// ── agentKey: lib_id wins, else '@'+name, global collapses ─────────────────
test('agentKey: prefers lib_id, falls back to @name, defaults to @Global', () => {
  assert.equal(o.agentKey('areas/Work', 'Work'), 'areas/Work');
  assert.equal(o.agentKey('', 'Global'), '@Global');
  assert.equal(o.agentKey(null, 'Foo'), '@Foo');
  assert.equal(o.agentKey('   ', '  '), '@Global');
  assert.equal(o.agentKey(undefined, undefined), '@Global');
});

// ── groupAgents: one entry per agent, top = most-recent (min idle) ─────────
test('groupAgents: groups by agent, picks min-idle session as topSessionId', () => {
  const slots = [
    slot('g1', 'Global', '', 30),
    slot('g2', 'Global', '', 5),
    slot('p1', 'Work', 'areas/Work', 12),
  ];
  const groups = o.groupAgents(slots, null);
  assert.equal(groups.length, 2);
  // Global first, then alpha.
  assert.deepEqual(groups.map((g) => g.key), ['@Global', 'areas/Work']);
  const global = groups[0];
  assert.equal(global.count, 2);
  assert.equal(global.topSessionId, 'g2'); // idle 5 < 30
  assert.equal(groups[1].topSessionId, 'p1');
});

test('groupAgents: stable order — Global pinned first, then case-insensitive alpha', () => {
  const slots = [
    slot('z', 'Health', 'areas/Health', 1),
    slot('d', 'dom', 'areas/dom', 1),
    slot('g', 'Global', '', 1),
    slot('b', 'Brain', 'projects/my-project', 1),
  ];
  const groups = o.groupAgents(slots, null);
  assert.deepEqual(groups.map((g) => g.name), ['Global', 'Brain', 'dom', 'Health']);
});

test('groupAgents: folds in the active scope even with an empty pool', () => {
  const groups = o.groupAgents([], { key: '@Global', name: 'Global', libId: '', sessionId: 'active1' });
  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, '@Global');
  assert.equal(groups[0].topSessionId, 'active1'); // fallback to current session
});

test('groupAgents: active scope already warm keeps the pool top, not current id', () => {
  const slots = [slot('warm', 'Work', 'areas/Work', 3)];
  const groups = o.groupAgents(slots, { key: 'areas/Work', name: 'Work', libId: 'areas/Work', sessionId: 'cold' });
  assert.equal(groups.length, 1);
  assert.equal(groups[0].topSessionId, 'warm'); // pool's most-recent wins over current fallback
});

test('groupAgents: cwd-less Global and folder-rooted no-lib stay separate', () => {
  const slots = [
    slot('a', 'Global', '', 1),
    slot('b', 'Foo', '', 1), // folder-rooted, no lib_id → @Foo, not @Global
  ];
  const groups = o.groupAgents(slots, null);
  assert.deepEqual(groups.map((g) => g.key).sort(), ['@Foo', '@Global']);
});

test('groupAgents: drops malformed slots, tolerates non-numeric idle', () => {
  const slots = [null, { agent: 'X' }, slot('ok', 'Global', '', 'nope')];
  const groups = o.groupAgents(slots, null);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].topSessionId, 'ok'); // idle coerced to Infinity, still the only one
});

test('groupAgents: does not mutate its inputs', () => {
  const slots = [slot('g1', 'Global', '', 5)];
  const snap = JSON.stringify(slots);
  o.groupAgents(slots, { key: '@Global', name: 'Global', libId: '', sessionId: 'g1' });
  assert.equal(JSON.stringify(slots), snap);
});

test('groupAgents: empty / undefined input → empty list', () => {
  assert.deepEqual(o.groupAgents([], null), []);
  assert.deepEqual(o.groupAgents(undefined, undefined), []);
});

// ── cycleAgentTarget: prev/next agent's top session, wrapping ──────────────
test('cycleAgentTarget: next + prev wrap across agents', () => {
  const groups = [
    { key: '@Global', topSessionId: 'g' },
    { key: 'areas/Work', topSessionId: 'p' },
    { key: 'projects/x', topSessionId: 'x' },
  ];
  assert.equal(o.cycleAgentTarget(groups, '@Global', 1), 'p');     // → next
  assert.equal(o.cycleAgentTarget(groups, 'projects/x', 1), 'g');  // → wraps to first
  assert.equal(o.cycleAgentTarget(groups, '@Global', -1), 'x');    // ← wraps to last
  assert.equal(o.cycleAgentTarget(groups, 'areas/Work', -1), 'g');
});

test('cycleAgentTarget: unknown currentKey starts walk from index 0', () => {
  const groups = [{ key: 'a', topSessionId: 'A' }, { key: 'b', topSessionId: 'B' }];
  assert.equal(o.cycleAgentTarget(groups, 'ghost', 1), 'B');  // idx 0 → next = 1
  assert.equal(o.cycleAgentTarget(groups, 'ghost', -1), 'B'); // idx 0 → prev wraps to last
});

test('cycleAgentTarget: single group cycles back to itself', () => {
  const groups = [{ key: 'a', topSessionId: 'A' }];
  assert.equal(o.cycleAgentTarget(groups, 'a', 1), 'A');
  assert.equal(o.cycleAgentTarget(groups, 'a', -1), 'A');
});

test('cycleAgentTarget: empty groups → null', () => {
  assert.equal(o.cycleAgentTarget([], 'a', 1), null);
  assert.equal(o.cycleAgentTarget(undefined, 'a', 1), null);
});

test('groupAgents: with no updated_at, sessions still order by idle asc (legacy)', () => {
  const slots = [
    slot('g-old', 'Global', '', 90),
    slot('g-new', 'Global', '', 3),
    slot('g-mid', 'Global', '', 30),
  ];
  const groups = o.groupAgents(slots, null);
  assert.deepEqual(groups[0].sessions.map((s) => s.session_id), ['g-new', 'g-mid', 'g-old']);
});

test('groupAgents: topSessionId = MOST RECENT (updated_at), not least-idle', () => {
  // The most recent session is COOLING (high idle); a warmer (low idle) one is
  // older. The default target must be the recent one, not the warm one.
  const slots = [
    { session_id: 'warm-old', agent: 'Global', lib_id: '', idle_s: 2, updated_at: 1000, title: '' },
    { session_id: 'cool-new', agent: 'Global', lib_id: '', idle_s: 900, updated_at: 5000, title: '' },
  ];
  const groups = o.groupAgents(slots, null);
  assert.equal(groups[0].topSessionId, 'cool-new');
  assert.deepEqual(groups[0].sessions.map((s) => s.session_id), ['cool-new', 'warm-old']);
});

test('groupAgents: sessions carry the persistent (keep-alive) flag', () => {
  const slots = [
    { session_id: 'a', agent: 'X', lib_id: 'areas/X', idle_s: 1, persistent: true, title: '' },
    { session_id: 'b', agent: 'X', lib_id: 'areas/X', idle_s: 2, persistent: false, title: '' },
  ];
  const g = o.groupAgents(slots, null)[0];
  const byId = Object.fromEntries(g.sessions.map((s) => [s.session_id, s.persistent]));
  assert.equal(byId.a, true);
  assert.equal(byId.b, false);
});

// ── planAgentClose: keep-alive exempt, tab-vanish + redirect ─────────────────
const gWith = (key, sessions, topSessionId) => ({ key, name: key, libId: key, topSessionId: topSessionId || (key + '-top'), sessions });

test('planAgentClose: closes only non-keep-alive sessions, counts spared', () => {
  const g = gWith('areas/X', [
    { session_id: 's1', persistent: false },
    { session_id: 's2', persistent: true },
    { session_id: 's3', persistent: false },
  ]);
  const plan = o.planAgentClose(g, [g], 'areas/X');
  assert.deepEqual(plan.closableIds, ['s1', 's3']);
  assert.equal(plan.spared, 1);
  assert.equal(plan.willVanish, false);          // a keep-alive session remains
  assert.deepEqual(plan.decision, { action: 'none' }); // tab stays → no redirect
});

test('planAgentClose: all closable → tab vanishes → redirect computed', () => {
  const a = gWith('areas/A', [{ session_id: 'a1', persistent: false }]);
  const b = gWith('areas/B', [{ session_id: 'b1', persistent: false }]);
  const plan = o.planAgentClose(a, [a, b], 'areas/A');  // closing the active agent
  assert.deepEqual(plan.closableIds, ['a1']);
  assert.equal(plan.spared, 0);
  assert.equal(plan.willVanish, true);
  assert.deepEqual(plan.decision, { action: 'session', id: 'areas/B-top' }); // jump to next tab's top session
});

test('planAgentClose: all keep-alive → nothing closable, never vanishes', () => {
  const g = gWith('areas/X', [{ session_id: 's1', persistent: true }, { session_id: 's2', persistent: true }]);
  const plan = o.planAgentClose(g, [g], 'areas/X');
  assert.deepEqual(plan.closableIds, []);
  assert.equal(plan.spared, 2);
  assert.equal(plan.willVanish, false);
  assert.deepEqual(plan.decision, { action: 'none' });
});

test('planAgentClose: closing a non-active agent stays put even when it vanishes', () => {
  const a = gWith('areas/A', [{ session_id: 'a1', persistent: false }]);
  const b = gWith('areas/B', [{ session_id: 'b1', persistent: false }]);
  const plan = o.planAgentClose(b, [a, b], 'areas/A');  // active is A, closing B
  assert.equal(plan.willVanish, true);
  assert.deepEqual(plan.decision, { action: 'none' });  // not the on-screen agent
});

// ── applyOrder: honour saved drag-and-drop order, append new agents ──────────
const grp = (key, topSessionId) => ({ key, name: key, libId: key.startsWith('@') ? '' : key, topSessionId: topSessionId || (key + '-top'), sessions: [] });

test('applyOrder: empty saved order → unchanged (default order)', () => {
  const g = [grp('@Global'), grp('areas/Finance'), grp('projects/x')];
  assert.deepEqual(o.applyOrder(g, []).map((x) => x.key), ['@Global', 'areas/Finance', 'projects/x']);
  assert.deepEqual(o.applyOrder(g, null).map((x) => x.key), ['@Global', 'areas/Finance', 'projects/x']);
});

test('applyOrder: saved keys first in saved sequence, rest appended in default order', () => {
  const g = [grp('@Global'), grp('areas/Finance'), grp('projects/x'), grp('projects/y')];
  const out = o.applyOrder(g, ['projects/x', '@Global']);
  assert.deepEqual(out.map((x) => x.key), ['projects/x', '@Global', 'areas/Finance', 'projects/y']);
});

test('applyOrder: saved key for an absent agent is skipped', () => {
  const g = [grp('@Global'), grp('areas/Finance')];
  const out = o.applyOrder(g, ['projects/gone', 'areas/Finance', '@Global']);
  assert.deepEqual(out.map((x) => x.key), ['areas/Finance', '@Global']);
});

test('applyOrder: brand-new agent (not in saved order) appends at the end', () => {
  const g = [grp('@Global'), grp('areas/Finance'), grp('projects/new')];
  const out = o.applyOrder(g, ['areas/Finance', '@Global']);
  assert.deepEqual(out.map((x) => x.key), ['areas/Finance', '@Global', 'projects/new']);
});

test('applyOrder: does not mutate input', () => {
  const g = [grp('a'), grp('b')];
  const copy = g.slice();
  o.applyOrder(g, ['b', 'a']);
  assert.deepEqual(g, copy);
});

// ── pickAgentCloseTarget ────────────────────────────────────────────────────
test('pickAgentCloseTarget: closing a non-current agent → none', () => {
  const g = [grp('a'), grp('b'), grp('c')];
  assert.deepEqual(o.pickAgentCloseTarget(g, 'b', 'a'), { action: 'none' });
});

test('pickAgentCloseTarget: closing current → next tab (slides into the slot)', () => {
  const g = [grp('a'), grp('b'), grp('c')];
  assert.deepEqual(o.pickAgentCloseTarget(g, 'b', 'b'), { action: 'session', id: 'c-top' });
});

test('pickAgentCloseTarget: closing the last current → previous tab', () => {
  const g = [grp('a'), grp('b'), grp('c')];
  assert.deepEqual(o.pickAgentCloseTarget(g, 'c', 'c'), { action: 'session', id: 'b-top' });
});

test('pickAgentCloseTarget: closing the only tab → agents directory', () => {
  const g = [grp('a')];
  assert.deepEqual(o.pickAgentCloseTarget(g, 'a', 'a'), { action: 'agents' });
});

test('pickAgentCloseTarget: skips survivors with no openable session', () => {
  const g = [grp('a'), { key: 'b', topSessionId: null }, grp('c')];
  assert.deepEqual(o.pickAgentCloseTarget(g, 'a', 'a'), { action: 'session', id: 'c-top' });
});

// ── scopeFromLibId ──────────────────────────────────────────────────────────
test('scopeFromLibId: areas/projects/global mapping', () => {
  assert.deepEqual(o.scopeFromLibId('areas/Finance'), { cwd: '~/Areas/Finance', libId: 'areas/Finance', isGlobal: false });
  assert.deepEqual(o.scopeFromLibId('projects/my-project'), { cwd: '~/Projects/my-project', libId: 'projects/my-project', isGlobal: false });
  assert.deepEqual(o.scopeFromLibId('projects/Group/Nested'), { cwd: '~/Projects/Group/Nested', libId: 'projects/Group/Nested', isGlobal: false });
  assert.deepEqual(o.scopeFromLibId(''), { cwd: null, libId: '', isGlobal: true });
  assert.deepEqual(o.scopeFromLibId('@Foo'), { cwd: null, libId: '', isGlobal: true });
  assert.deepEqual(o.scopeFromLibId(null), { cwd: null, libId: '', isGlobal: true });
});

// ── moveKey: drag-and-drop array transform ──────────────────────────────────
test('moveKey: drop-onto-target inserts the dragged key at the target slot', () => {
  // drop 'a' onto 'c' → 'a' takes c's place, c shifts right
  assert.deepEqual(o.moveKey(['a', 'b', 'c', 'd'], 'a', 'c'), ['b', 'a', 'c', 'd']);
  // drop 'd' onto 'b' → 'd' takes b's place
  assert.deepEqual(o.moveKey(['a', 'b', 'c', 'd'], 'd', 'b'), ['a', 'd', 'b', 'c']);
});

test('moveKey: from===to or unknown key → unchanged copy', () => {
  assert.deepEqual(o.moveKey(['a', 'b'], 'a', 'a'), ['a', 'b']);
  assert.deepEqual(o.moveKey(['a', 'b'], 'z', 'a'), ['a', 'b']);
});

test('moveKey: unknown target → push to end', () => {
  assert.deepEqual(o.moveKey(['a', 'b', 'c'], 'a', 'zzz'), ['b', 'c', 'a']);
});

test('moveKey: does not mutate input', () => {
  const k = ['a', 'b', 'c'];
  const copy = k.slice();
  o.moveKey(k, 'a', 'c');
  assert.deepEqual(k, copy);
});
