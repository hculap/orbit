'use strict';
// Contract test for window.encodePath (defined in static/components.jsx).
// The no-build CDN-React project can't import the .jsx, so the 1-line impl is
// inlined here to lock the behaviour: per-segment encodeURIComponent so names
// with #/?/&/+/% navigate correctly while '/' separators survive. Issue #84.
//
//   node --test tests/share-encode-path.test.js

const test = require('node:test');
const assert = require('node:assert/strict');

// Must stay byte-for-byte identical to components.jsx `encodePath`.
function encodePath(rel) {
  return String(rel || '').split('/').map(encodeURIComponent).join('/');
}

test('encodes & as %26 (would otherwise start a query string)', () => {
  assert.equal(encodePath('Q&A/sub'), 'Q%26A/sub');
});

test('encodes # as %23 (would otherwise start a URL fragment)', () => {
  assert.equal(encodePath('H#tag'), 'H%23tag');
});

test('encodes spaces as %20', () => {
  assert.equal(encodePath('a b/c'), 'a%20b/c');
});

test('encodes + and % literally', () => {
  assert.equal(encodePath('a+b/c%d'), 'a%2Bb/c%25d');
});

test('preserves / separators between segments', () => {
  assert.equal(encodePath('one/two/three').split('/').length, 3);
});

test('handles unicode without mangling separators', () => {
  assert.equal(encodePath('A B/Zażółć/deep'), 'A%20B/Za%C5%BC%C3%B3%C5%82%C4%87/deep');
});

test('null/undefined/empty become empty string', () => {
  assert.equal(encodePath(null), '');
  assert.equal(encodePath(undefined), '');
  assert.equal(encodePath(''), '');
});
