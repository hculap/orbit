'use strict';
// Pure-logic unit tests for the Sync/Share browser URL helpers. No-build
// CDN-React project → the encode/decode pair lives in a JSX-free module
// testable with Node's built-in runner (zero deps):
//   node --test tests/share-route.test.js
//
// The .jsx wiring (router branch + ShareView/app.jsx props + Back behaviour)
// is covered by the manual smoke checklist in the PR description — there is
// no jsdom/Playwright in this repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const sr = require('../src/orbit/static/share-route.js');

// router.jsx splits the path on '/' and passes parts.slice(1) (everything
// after 'share'). These tests feed parseSharePath the SAME already-split
// segment arrays.

test('buildSharePath: bare root', () => {
  assert.equal(sr.buildSharePath({}), '/share');
  assert.equal(sr.buildSharePath(), '/share');
});

test('buildSharePath: folder only (internal slashes collapse to one segment)', () => {
  assert.equal(sr.buildSharePath({ sharePath: 'a/b' }), '/share/a%2Fb');
});

test('buildSharePath: folder + file', () => {
  assert.equal(sr.buildSharePath({ sharePath: 'a/b', shareFile: 'c.png' }), '/share/a%2Fb/f/c.png');
});

test('buildSharePath: file at root', () => {
  assert.equal(sr.buildSharePath({ shareFile: 'note.md' }), '/share/f/note.md');
});

test('parseSharePath: bare /share → {}', () => {
  assert.deepEqual(sr.parseSharePath([]), {});
  assert.deepEqual(sr.parseSharePath(null), {});
});

test('parseSharePath: folder only', () => {
  assert.deepEqual(sr.parseSharePath(['a%2Fb']), { sharePath: 'a/b' });
});

test('parseSharePath: folder + file', () => {
  assert.deepEqual(sr.parseSharePath(['a%2Fb', 'f', 'c.png']), { sharePath: 'a/b', shareFile: 'c.png' });
});

test('parseSharePath: file at root', () => {
  assert.deepEqual(sr.parseSharePath(['f', 'note.md']), { sharePath: '', shareFile: 'note.md' });
});

test('round-trip: nested dirs', () => {
  const built = sr.buildSharePath({ sharePath: 'docs/2026/q2' });
  assert.deepEqual(sr.parseSharePath(built.replace(/^\/share\/?/, '').split('/').filter(Boolean)), { sharePath: 'docs/2026/q2' });
});

test('round-trip: file name with spaces + unicode', () => {
  const spec = { sharePath: 'foldér', shareFile: 'hello world ąćż.txt' };
  const built = sr.buildSharePath(spec);
  const parts = built.replace(/^\/share\/?/, '').split('/').filter(Boolean);
  assert.deepEqual(sr.parseSharePath(parts), spec);
});

test('a real folder literally named "f" round-trips (slice-from-end marker)', () => {
  // /share/a/f — 'f' is the LAST segment, not the 2nd-to-last marker pair,
  // so it parses as a folder, not a file marker.
  assert.deepEqual(sr.parseSharePath(['a', 'f']), { sharePath: 'a/f' });
});

test('a folder named "f" containing a file still works', () => {
  // sharePath 'a/f' encodes to one segment 'a%2Ff', so the 'f' marker is
  // unambiguous: ['a%2Ff', 'f', 'doc.txt'] → folder 'a/f', file 'doc.txt'.
  const spec = { sharePath: 'a/f', shareFile: 'doc.txt' };
  const built = sr.buildSharePath(spec);
  const parts = built.replace(/^\/share\/?/, '').split('/').filter(Boolean);
  assert.deepEqual(sr.parseSharePath(parts), spec);
});
