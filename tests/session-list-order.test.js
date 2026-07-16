'use strict';
// Pure-logic unit tests for the session-sidebar ordering + "older than a week"
// fold. No-build CDN-React project → the branching logic lives in a JSX-free
// module testable with Node's built-in runner (zero deps):
//   node --test tests/session-list-order.test.js
//
// The .jsx SessionList renderer (rows + divider) is covered by the manual smoke
// checklist — there is no jsdom in this repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const o = require('../src/orbit/static/session-list-order.js');

const DAY = 24 * 60 * 60;
// A stable "now" so age math is deterministic.
const NOW = 1_000_000_000;
const ago = (days) => NOW - days * DAY;

// session summary stub (only the fields the helper reads).
const sess = (id, daysAgo, extra) => ({ id, updated_at: ago(daysAgo), ...(extra || {}) });
const poolOf = (...ids) => {
  const m = {};
  for (const id of ids) m[id] = 'hot';
  return m;
};

// ── isOpen ─────────────────────────────────────────────────────────────────
test('isOpen: true only when the session has a pool status', () => {
  assert.equal(o.isOpen(sess('a', 0), poolOf('a')), true);
  assert.equal(o.isOpen(sess('a', 0), poolOf('b')), false);
  assert.equal(o.isOpen(sess('a', 0), {}), false);
  assert.equal(o.isOpen(sess('a', 0), null), false);
  assert.equal(o.isOpen(null, poolOf('a')), false);
});

// ── sortSessions: open → pinned → recency ───────────────────────────────────
test('sortSessions: open sessions float to the very top regardless of age', () => {
  const list = [
    sess('recent', 0),                 // fresh, not open
    sess('open-old', 30),              // OPEN but ancient
    sess('mid', 2),
  ];
  const out = o.sortSessions(list, poolOf('open-old'));
  assert.deepEqual(out.map((s) => s.id), ['open-old', 'recent', 'mid']);
});

test('sortSessions: tier order is open > pinned > rest, each by updated desc', () => {
  const list = [
    sess('rest-new', 1),
    sess('pinned-old', 20, { pinned: true }),
    sess('open', 10),
    sess('rest-old', 40),
    sess('pinned-new', 0, { pinned: true }),
  ];
  const out = o.sortSessions(list, poolOf('open'));
  assert.deepEqual(out.map((s) => s.id),
    ['open', 'pinned-new', 'pinned-old', 'rest-new', 'rest-old']);
});

test('sortSessions: within a tier, newest last-message first', () => {
  const out = o.sortSessions([sess('a', 5), sess('b', 1), sess('c', 3)], {});
  assert.deepEqual(out.map((s) => s.id), ['b', 'c', 'a']);
});

test('sortSessions: does not mutate the input array', () => {
  const list = [sess('a', 5), sess('b', 1)];
  const copy = list.slice();
  o.sortSessions(list, {});
  assert.deepEqual(list, copy);
});

test('sortSessions: missing/garbage updated_at sorts to the bottom of its tier', () => {
  const out = o.sortSessions(
    [{ id: 'bad' }, sess('good', 2), { id: 'nan', updated_at: 'xyz' }], {});
  assert.equal(out[0].id, 'good');
});

// ── partition: the weekly fold ──────────────────────────────────────────────
test('partition: stale (>1w, not open/pinned/active) sessions fold into hidden', () => {
  const list = [sess('fresh', 1), sess('stale', 10), sess('edge-in', 6), sess('edge-out', 8)];
  const { visible, hidden } = o.partition(list, {}, NOW, {});
  assert.deepEqual(visible.map((s) => s.id), ['fresh', 'edge-in']);
  assert.deepEqual(hidden.map((s) => s.id), ['edge-out', 'stale']);
});

test('partition: open / pinned / active stay visible even when ancient', () => {
  const list = [
    sess('open-old', 50),
    sess('pinned-old', 60, { pinned: true }),
    sess('active-old', 40),
    sess('plain-old', 30),
  ];
  const { visible, hidden } = o.partition(list, poolOf('open-old'), NOW, { activeId: 'active-old' });
  // open first, then pinned, then the active straggler; only plain-old folds.
  assert.deepEqual(visible.map((s) => s.id), ['open-old', 'pinned-old', 'active-old']);
  assert.deepEqual(hidden.map((s) => s.id), ['plain-old']);
});

test('partition: hidden is always a contiguous tail of ordered', () => {
  const list = [sess('a', 0), sess('b', 9), sess('c', 1), sess('d', 20)];
  const { ordered, visible, hidden } = o.partition(list, {}, NOW, {});
  assert.deepEqual(ordered.map((s) => s.id), visible.concat(hidden).map((s) => s.id));
});

test('partition: custom maxAgeS boundary is exclusive (== boundary folds)', () => {
  const list = [sess('exact', 7), sess('under', 6)];
  const { visible, hidden } = o.partition(list, {}, NOW, { maxAgeS: 7 * DAY });
  assert.deepEqual(visible.map((s) => s.id), ['under']);
  assert.deepEqual(hidden.map((s) => s.id), ['exact']);
});

test('partition: everything recent → nothing hidden', () => {
  const { hidden } = o.partition([sess('a', 0), sess('b', 2)], {}, NOW, {});
  assert.equal(hidden.length, 0);
});

test('partition: empty / non-array input is safe', () => {
  assert.deepEqual(o.partition(null, {}, NOW, {}), { ordered: [], visible: [], hidden: [] });
  assert.deepEqual(o.partition(undefined, null, NOW, null).visible, []);
});

test('partition: minVisible floor keeps the newest row visible when ALL are stale', () => {
  // No open/pinned/active escape hatch (mirrors the Agent-panel reuse) → without
  // the floor every row would fold into a bare "show more" divider.
  const list = [sess('a', 10), sess('b', 20), sess('c', 30)];
  const { visible, hidden } = o.partition(list, {}, NOW, {});
  assert.deepEqual(visible.map((s) => s.id), ['a']);          // newest stays up
  assert.deepEqual(hidden.map((s) => s.id), ['b', 'c']);
});

test('partition: minVisible promotes from the front of hidden (stays contiguous)', () => {
  const list = [sess('new', 8), sess('mid', 9), sess('old', 40)];
  const { ordered, visible, hidden } = o.partition(list, {}, NOW, { minVisible: 2 });
  assert.deepEqual(visible.map((s) => s.id), ['new', 'mid']);
  assert.deepEqual(hidden.map((s) => s.id), ['old']);
  assert.deepEqual(ordered.map((s) => s.id), visible.concat(hidden).map((s) => s.id));
});

test('partition: minVisible=0 allows a fully-folded list (bare divider opt-in)', () => {
  const { visible, hidden } = o.partition([sess('a', 10), sess('b', 20)], {}, NOW, { minVisible: 0 });
  assert.equal(visible.length, 0);
  assert.equal(hidden.length, 2);
});

test('partition: minVisible never duplicates when enough are already visible', () => {
  const list = [sess('fresh', 1), sess('stale', 30)];
  const { visible, hidden } = o.partition(list, {}, NOW, { minVisible: 1 });
  assert.deepEqual(visible.map((s) => s.id), ['fresh']);
  assert.deepEqual(hidden.map((s) => s.id), ['stale']);
});

// ── membershipKey: stable across reorder/timestamp churn ─────────────────────
test('membershipKey: changes on add/remove, NOT on reorder or timestamp bump', () => {
  const a = o.membershipKey([sess('x', 1), sess('y', 2)]);
  const reordered = o.membershipKey([sess('y', 9), sess('x', 0)]); // swapped + bumped
  assert.equal(a, reordered);
  const added = o.membershipKey([sess('x', 1), sess('y', 2), sess('z', 0)]);
  assert.notEqual(a, added);
});

test('WEEK_S is exactly 7 days in seconds', () => {
  assert.equal(o.WEEK_S, 7 * 24 * 60 * 60);
});

// ── closedGuard: mark / isClosed / clear ─────────────────────────────────────
test('closedGuard: mark then isClosed; clear lifts it', () => {
  const g = o.closedGuard;
  assert.equal(g.isClosed('s1'), false);
  g.mark('s1');
  assert.equal(g.isClosed('s1'), true);
  g.clear('s1');
  assert.equal(g.isClosed('s1'), false);
});

test('closedGuard: falsy ids are no-ops', () => {
  const g = o.closedGuard;
  g.mark('');
  g.mark(null);
  assert.equal(g.isClosed(''), false);
  assert.equal(g.isClosed(undefined), false);
});

// ── pickRedirectTarget: post-close / post-delete navigation ──────────────────
const rsess = (id, cwd, daysAgo, extra) =>
  ({ id, cwd, updated_at: ago(daysAgo), ...(extra || {}) });

test('pickRedirectTarget: removing a NON-displayed session → stay put (none)', () => {
  const sessions = [rsess('a', '/x', 0), rsess('b', '/x', 1)];
  const d = o.pickRedirectTarget({
    closedId: 'b', activeId: 'a', sessions, poolStatusById: { a: 'hot', b: 'hot' }, agentCwd: '/x',
  });
  assert.deepEqual(d, { action: 'none' });
});

test('pickRedirectTarget: closing displayed → next OPEN in same agent (most recent)', () => {
  const sessions = [
    rsess('active', '/agentA', 0),
    rsess('a-old', '/agentA', 5),   // open, same agent, older
    rsess('a-new', '/agentA', 1),   // open, same agent, newer ← winner
    rsess('a-closed', '/agentA', 0), // NOT open (no pool entry)
    rsess('b1', '/agentB', 0),      // open, other agent
  ];
  const pool = { active: 'hot', 'a-old': 'cooling', 'a-new': 'hot', b1: 'hot' };
  const d = o.pickRedirectTarget({ closedId: 'active', activeId: 'active', sessions, poolStatusById: pool, agentCwd: '/agentA' });
  assert.deepEqual(d, { action: 'session', id: 'a-new' });
});

test('pickRedirectTarget: no open in same agent → most recent open in ANOTHER agent', () => {
  const sessions = [
    rsess('active', '/agentA', 0),
    rsess('a-closed', '/agentA', 0), // same agent but NOT open
    rsess('b-old', '/agentB', 9),    // open other agent, older
    rsess('c-new', '/agentC', 2),    // open other agent, newer ← winner
  ];
  const pool = { active: 'hot', 'b-old': 'hot', 'c-new': 'cooling' };
  const d = o.pickRedirectTarget({ closedId: 'active', activeId: 'active', sessions, poolStatusById: pool, agentCwd: '/agentA' });
  assert.deepEqual(d, { action: 'session', id: 'c-new' });
});

test('pickRedirectTarget: no open session anywhere → agents directory', () => {
  const sessions = [rsess('active', '/agentA', 0), rsess('b', '/agentB', 1)];
  const d = o.pickRedirectTarget({ closedId: 'active', activeId: 'active', sessions, poolStatusById: { active: 'hot' }, agentCwd: '/agentA' });
  assert.deepEqual(d, { action: 'agents' });
});

test('pickRedirectTarget: the removed session is never its own redirect target', () => {
  // Pool still lists the closed session as open (poll lag) — must be excluded.
  const sessions = [rsess('active', '/agentA', 0)];
  const d = o.pickRedirectTarget({ closedId: 'active', activeId: 'active', sessions, poolStatusById: { active: 'hot' }, agentCwd: '/agentA' });
  assert.deepEqual(d, { action: 'agents' });
});

test('pickRedirectTarget: archived sessions are not redirect targets', () => {
  const sessions = [rsess('active', '/a', 0), rsess('arch', '/a', 1, { archived: true })];
  const d = o.pickRedirectTarget({ closedId: 'active', activeId: 'active', sessions, poolStatusById: { active: 'hot', arch: 'hot' }, agentCwd: '/a' });
  assert.deepEqual(d, { action: 'agents' });
});

test('pickRedirectTarget: Global agent (agentCwd null) matches cwd=null sessions', () => {
  const sessions = [
    rsess('active', null, 0),
    rsess('g2', null, 1),       // open global ← same-agent winner
    rsess('p1', '/proj', 0),    // open project
  ];
  const pool = { active: 'hot', g2: 'hot', p1: 'hot' };
  const d = o.pickRedirectTarget({ closedId: 'active', activeId: 'active', sessions, poolStatusById: pool, agentCwd: null });
  assert.deepEqual(d, { action: 'session', id: 'g2' });
});

test('pickRedirectTarget: missing/empty inputs are safe → none', () => {
  assert.deepEqual(o.pickRedirectTarget({}), { action: 'none' });
  assert.deepEqual(o.pickRedirectTarget(), { action: 'none' });
  assert.deepEqual(o.pickRedirectTarget({ closedId: 'x', activeId: 'x' }), { action: 'agents' });
});
