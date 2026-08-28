'use strict';
// Pure-logic unit tests for git-worktree NAME validation shared by the
// new-session modal. No-build CDN-React project → the branching logic lives in
// a JSX-free module testable with Node's built-in runner (zero deps):
//   node --test tests/worktree-name.test.js
//
// The rules mirror claude-cli's own `--worktree <name>` validation (≤64 chars,
// [A-Za-z0-9._-] only, no `.`/`..`/`.git`) PLUS two of our own: no `/` (keeps
// the on-disk path predictable, since claude mangles `/` → `+`) and no leading
// `-` (would be read as a flag if it ever reached an argv boundary).
//
// `tests/test_orchestrator_worktree.py::test_name_regex_matches_frontend`
// cross-checks NAME_RE against the Python source so the two can't drift.

const test = require('node:test');
const assert = require('node:assert/strict');
const w = require('../src/orbit/static/worktree-name.js');

// ── validate: accepted names ───────────────────────────────────────────────
test('validate: accepts plain names', () => {
  for (const name of ['a', 'A1', 'feat-x', 'v1.2.3', 'a_b', 'Fix.Bug_2-b']) {
    assert.equal(w.validate(name).ok, true, `expected ${name} to be valid`);
  }
});

test('validate: accepts exactly 64 chars, rejects 65', () => {
  assert.equal(w.validate('a'.repeat(64)).ok, true);
  const long = w.validate('a'.repeat(65));
  assert.equal(long.ok, false);
  assert.equal(long.code, 'too-long');
});

test('validate: blank is OK and means "generate a random name"', () => {
  for (const blank of ['', '   ', null, undefined]) {
    const r = w.validate(blank);
    assert.equal(r.ok, true, `expected ${JSON.stringify(blank)} to be ok`);
    assert.equal(r.code, 'empty');
  }
});

// ── validate: rejected names ───────────────────────────────────────────────
test('validate: rejects a leading non-alphanumeric', () => {
  for (const name of ['-x', '.x', '_x']) {
    const r = w.validate(name);
    assert.equal(r.ok, false, `expected ${name} to be rejected`);
    assert.equal(r.code, 'bad-start');
  }
});

test('validate: slash gets its own code (the mistake users actually make)', () => {
  const r = w.validate('feat/x');
  assert.equal(r.ok, false);
  assert.equal(r.code, 'slash');
  assert.match(r.message, /\//);
});

test('validate: rejects Polish diacritics and spaces as bad-chars', () => {
  for (const name of ['ma-ły', 'zażółć', 'a b', 'a+b', 'a@b']) {
    const r = w.validate(name);
    assert.equal(r.ok, false, `expected ${name} to be rejected`);
    assert.equal(r.code, 'bad-chars');
  }
});

test('validate: rejects git-reserved names', () => {
  for (const name of ['.', '..', '.git', '.GIT']) {
    const r = w.validate(name);
    assert.equal(r.ok, false, `expected ${name} to be rejected`);
    // `.`/`..`/`.git` all start with a dot, so bad-start fires first for
    // those; the point is only that they never pass.
    assert.ok(['bad-start', 'reserved'].includes(r.code), `unexpected code ${r.code}`);
  }
});

test('validate: rejects a name already used by another worktree', () => {
  const r = w.validate('feat-x', { existing: ['main-wt', 'feat-x'] });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'duplicate');
});

test('validate: duplicate check ignores surrounding whitespace', () => {
  assert.equal(w.validate('  feat-x  ', { existing: ['feat-x'] }).code, 'duplicate');
});

test('validate: blank stays OK even when the suggested name collides', () => {
  // Nothing is typed yet — the server picks (and re-picks) the random name.
  assert.equal(w.validate('', { existing: ['anything'] }).ok, true);
});

test('validate: every rejection carries a non-empty human message', () => {
  for (const name of ['-x', 'feat/x', 'ma-ły', 'a'.repeat(65)]) {
    const r = w.validate(name);
    assert.equal(typeof r.message, 'string');
    assert.ok(r.message.length > 0, `empty message for ${name}`);
  }
});

// ── branchFor ──────────────────────────────────────────────────────────────
test('branchFor: mirrors claude-cli (worktree-<name>)', () => {
  assert.equal(w.branchFor('x'), 'worktree-x');
  assert.equal(w.branchFor('  feat-1  '), 'worktree-feat-1');
  assert.equal(w.branchFor(''), '');
  assert.equal(w.branchFor(null), '');
});

// ── effectiveName ──────────────────────────────────────────────────────────
test('effectiveName: trims, and falls back to the suggestion when blank', () => {
  assert.equal(w.effectiveName(' feat ', 'brave-otter'), 'feat');
  assert.equal(w.effectiveName('  ', 'brave-otter'), 'brave-otter');
  assert.equal(w.effectiveName('', 'brave-otter'), 'brave-otter');
  assert.equal(w.effectiveName(null, 'brave-otter'), 'brave-otter');
});

test('effectiveName: returns "" when both the input and the suggestion are blank', () => {
  // The server then generates one; the UI shows the literal "losowa" hint.
  assert.equal(w.effectiveName('', null), '');
  assert.equal(w.effectiveName(null, undefined), '');
});

// ── exported contract ──────────────────────────────────────────────────────
test('exports MAX_LEN and a NAME_RE the Python side can be diffed against', () => {
  assert.equal(w.MAX_LEN, 64);
  assert.ok(w.NAME_RE instanceof RegExp);
  assert.equal(w.NAME_RE.source, '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$');
});
