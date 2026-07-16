'use strict';
// Pure-logic unit tests for the desktop session switcher (⌘+⇧ hold-to-cycle).
// No-build CDN-React project → the switcher's branching logic lives in a
// JSX-free module testable with Node's built-in runner (zero deps):
//   node --test tests/session-switcher-order.test.js
//
// The .jsx overlay (rendering / keyboard wiring) is covered by the manual
// smoke checklist in the PR description — there is no jsdom in this repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const o = require('../src/orbit/static/session-switcher-order.js');

const slot = (id, idle_s, extra) => Object.assign({ session_id: id, idle_s, agent: id.toUpperCase(), title: '' }, extra || {});

// ── orderSessions: current → MRU → idle-asc fallback, deduped ──────────────
test('orderSessions: current first, then MRU order, then idle-asc remainder', () => {
  const slots = [slot('a', 50), slot('b', 10), slot('c', 5), slot('d', 99)];
  const out = o.orderSessions(slots, ['b', 'a'], 'c');
  assert.deepEqual(out.map((s) => s.session_id), ['c', 'b', 'a', 'd']);
});

test('orderSessions: no current / no MRU → pure idle-ascending', () => {
  const slots = [slot('a', 50), slot('b', 10), slot('d', 99)];
  const out = o.orderSessions(slots, [], null);
  assert.deepEqual(out.map((s) => s.session_id), ['b', 'a', 'd']);
});

test('orderSessions: dedupes ids appearing in both MRU and slots, and a current also in MRU', () => {
  const slots = [slot('a', 50), slot('b', 10), slot('c', 5)];
  const out = o.orderSessions(slots, ['c', 'b', 'c'], 'c');
  assert.deepEqual(out.map((s) => s.session_id), ['c', 'b', 'a']);
  // each id exactly once
  assert.equal(new Set(out.map((s) => s.session_id)).size, out.length);
});

test('orderSessions: drops malformed slots and a currentId not in the pool', () => {
  const slots = [slot('a', 50), null, { idle_s: 1 }, slot('b', 10)];
  const out = o.orderSessions(slots, [], 'ghost');
  assert.deepEqual(out.map((s) => s.session_id), ['b', 'a']);
});

test('orderSessions: does not mutate its inputs', () => {
  const slots = [slot('a', 50), slot('b', 10)];
  const mru = ['b'];
  const snapshotSlots = JSON.stringify(slots);
  o.orderSessions(slots, mru, 'a');
  assert.equal(JSON.stringify(slots), snapshotSlots);
  assert.deepEqual(mru, ['b']);
});

test('orderSessions: empty input → empty list', () => {
  assert.deepEqual(o.orderSessions([], [], null), []);
  assert.deepEqual(o.orderSessions(undefined, undefined, undefined), []);
});

// ── defaultIndex: land on PREVIOUS session, never on current ───────────────
test('defaultIndex: previous session (index 1) when >1, else 0', () => {
  assert.equal(o.defaultIndex([slot('a', 1), slot('b', 2), slot('c', 3)]), 1);
  assert.equal(o.defaultIndex([slot('a', 1)]), 0);
  assert.equal(o.defaultIndex([]), 0);
});

test('defaultIndex: current (index 0) is never the default target when >1 session', () => {
  const out = o.orderSessions([slot('a', 5), slot('b', 5)], ['b'], 'a');
  const currentIdx = out.findIndex((s) => s.session_id === 'a');
  assert.equal(currentIdx, 0);
  assert.notEqual(o.defaultIndex(out), currentIdx);
});

// ── wrapIndex: cyclic (for ⇧ tap / arrow cycling) ─────────────────────────
test('wrapIndex: wraps both directions, 0 on empty', () => {
  assert.equal(o.wrapIndex(-1, 4), 3);
  assert.equal(o.wrapIndex(4, 4), 0);
  assert.equal(o.wrapIndex(2, 4), 2);
  assert.equal(o.wrapIndex(5, 4), 1);
  assert.equal(o.wrapIndex(0, 0), 0);
});

// ── clampIndex: non-cyclic (for re-clamp after a session is evicted) ───────
test('clampIndex: clamps into range, 0 on empty', () => {
  assert.equal(o.clampIndex(5, 4), 3);
  assert.equal(o.clampIndex(-1, 4), 0);
  assert.equal(o.clampIndex(2, 4), 2);
  assert.equal(o.clampIndex(0, 0), 0);
});

// ── advance / reverse: the cycle state machine ────────────────────────────
test('advance: forward with wrap (⇧ tap / ↓)', () => {
  assert.equal(o.advance(1, 4), 2);
  assert.equal(o.advance(3, 4), 0);
  assert.equal(o.advance(0, 1), 0);
  assert.equal(o.advance(0, 0), 0);
});

test('reverse: backward with wrap (↑)', () => {
  assert.equal(o.reverse(0, 4), 3);
  assert.equal(o.reverse(2, 4), 1);
  assert.equal(o.reverse(0, 1), 0);
});

// ── pushActivation: the client MRU log (immutable) ────────────────────────
test('pushActivation: moves existing id to front', () => {
  assert.deepEqual(o.pushActivation(['a', 'b', 'c'], 'b'), ['b', 'a', 'c']);
});

test('pushActivation: prepends a new id', () => {
  assert.deepEqual(o.pushActivation(['a'], 'c'), ['c', 'a']);
  assert.deepEqual(o.pushActivation([], 'x'), ['x']);
});

test('pushActivation: returns a NEW array, never mutates input', () => {
  const mru = ['a', 'b'];
  const next = o.pushActivation(mru, 'b');
  assert.notEqual(next, mru);
  assert.deepEqual(mru, ['a', 'b']);
});

test('pushActivation: falsy id is a no-op (returns equal contents)', () => {
  assert.deepEqual(o.pushActivation(['a', 'b'], null), ['a', 'b']);
  assert.deepEqual(o.pushActivation(['a', 'b'], ''), ['a', 'b']);
});

// ── humanizeIdle: compact age label ───────────────────────────────────────
test('humanizeIdle: s / m / h / d buckets', () => {
  assert.equal(o.humanizeIdle(0), '0s');
  assert.equal(o.humanizeIdle(12), '12s');
  assert.equal(o.humanizeIdle(125), '2m');
  assert.equal(o.humanizeIdle(3600), '1h');
  assert.equal(o.humanizeIdle(90000), '1d');
});

test('humanizeIdle: non-finite / negative → empty string', () => {
  assert.equal(o.humanizeIdle(-5), '');
  assert.equal(o.humanizeIdle(NaN), '');
  assert.equal(o.humanizeIdle(undefined), '');
});
