'use strict';
// Pure-logic unit tests for the shared Escape stack. No-build CDN-React
// project → the LIFO registry lives in a JSX-free module testable with Node's
// built-in runner (zero deps):
//   node --test tests/overlay-stack.test.js
//
// The .jsx wiring (Modal's closeOnEscape effect, _GhFullscreen's isTop gate)
// is covered by the manual smoke checklist — there is no jsdom in this repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../src/orbit/static/overlay-stack.js');

test('nextId: unique per call', () => {
  S.reset();
  const seen = new Set();
  for (let i = 0; i < 100; i += 1) seen.add(S.nextId());
  assert.equal(seen.size, 100);
});

test('push/pop: size bookkeeping', () => {
  S.reset();
  assert.equal(S.size(), 0);
  const a = S.push(S.nextId());
  const b = S.push(S.nextId());
  assert.equal(S.size(), 2);
  S.pop(b);
  S.pop(a);
  assert.equal(S.size(), 0);
});

test('push returns the id it was given (so callers can inline it)', () => {
  S.reset();
  const id = S.nextId();
  assert.equal(S.push(id), id);
});

test('isTop: only the LAST registered overlay is top', () => {
  S.reset();
  const outer = S.push(S.nextId());
  assert.equal(S.isTop(outer), true);
  // A nested overlay mounts LATER — it must take over Escape.
  const inner = S.push(S.nextId());
  assert.equal(S.isTop(inner), true);
  assert.equal(S.isTop(outer), false);
  // …and hand it back when it unmounts.
  S.pop(inner);
  assert.equal(S.isTop(outer), true);
});

test('isTop: empty stack has no top', () => {
  S.reset();
  assert.equal(S.isTop('ovl-1'), false);
  assert.equal(S.isTop(undefined), false);
});

test('pop: removes a MIDDLE entry (React unmount order is not guaranteed LIFO)', () => {
  S.reset();
  const a = S.push(S.nextId());
  const b = S.push(S.nextId());
  const c = S.push(S.nextId());
  assert.equal(S.pop(b), true);
  assert.equal(S.size(), 2);
  // c is still on top, a still below it — order of the survivors is preserved.
  assert.equal(S.isTop(c), true);
  S.pop(c);
  assert.equal(S.isTop(a), true);
});

test('pop: unknown / falsy id is a no-op, never corrupts the stack', () => {
  S.reset();
  const a = S.push(S.nextId());
  assert.equal(S.pop('ovl-nope'), false);
  assert.equal(S.pop(undefined), false);
  assert.equal(S.pop(null), false);
  assert.equal(S.size(), 1);
  assert.equal(S.isTop(a), true);
});

test('push: falsy id is ignored (a caller with no stack stays inert)', () => {
  S.reset();
  S.push(null);
  S.push(undefined);
  S.push('');
  assert.equal(S.size(), 0);
});

test('reset: clears everything', () => {
  S.reset();
  S.push(S.nextId());
  S.push(S.nextId());
  S.reset();
  assert.equal(S.size(), 0);
});
