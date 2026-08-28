'use strict';
// Pure-logic unit tests for the Issues panel search (number / title / body).
// No-build CDN-React project → the matching logic lives in a JSX-free module
// testable with Node's built-in runner (zero deps):
//   node --test tests/issue-search.test.js
//
// The rules under test:
//   * "#14" and "14" are the same ask, and a bare number RANKS the exact
//     issue first without hiding the looser hits;
//   * title and body match case-insensitively, on a plain toLowerCase (NO
//     diacritics folding — see the module header for why NFD is worse than
//     nothing for Polish);
//   * filtering never mutates or reorders the caller's array in place.

const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../src/orbit/static/issue-search.js');

const ITEMS = [
  { number: 14, title: 'Oficjalne przykłady', body: 'Dodaj sekcję o mapach do README.' },
  { number: 140, title: 'Coś zupełnie innego', body: 'Nic tu nie ma.' },
  { number: 7, title: 'Zażółć gęślą jaźń', body: 'Test polskich znaków w opisie.' },
  { number: 3, title: 'Wspomina o 14 w tytule', body: '' },
  { number: 9, title: 'Bez opisu', body: null },
];

// ── normalize ──────────────────────────────────────────────────────────────
test('normalize: trims + lowercases, tolerates non-strings', () => {
  assert.equal(S.normalize('  MaPa  '), 'mapa');
  assert.equal(S.normalize('ŁÓDŹ'), 'łódź');
  for (const junk of [null, undefined, 42, {}]) assert.equal(S.normalize(junk), '');
});

// ── numberToken / exactNumber ──────────────────────────────────────────────
test('numberToken: hash is optional, anything else is not a number query', () => {
  assert.equal(S.numberToken('14'), '14');
  assert.equal(S.numberToken('#14'), '14');
  assert.equal(S.numberToken('#'), '');
  assert.equal(S.numberToken('14a'), '');
  assert.equal(S.numberToken('mapa'), '');
  assert.equal(S.numberToken(''), '');
});

test('exactNumber: null for anything that is not a bare number', () => {
  assert.equal(S.exactNumber('#14'), 14);
  assert.equal(S.exactNumber('14'), 14);
  assert.equal(S.exactNumber('mapa'), null);
});

// ── matchesIssue ───────────────────────────────────────────────────────────
test('matchesIssue: empty query matches everything', () => {
  assert.equal(S.matchesIssue(ITEMS[0], ''), true);
});

test('matchesIssue: by number, with or without the hash', () => {
  assert.equal(S.matchesIssue(ITEMS[0], '14'), true);
  assert.equal(S.matchesIssue(ITEMS[0], '#14'), true);
  assert.equal(S.matchesIssue(ITEMS[1], '14'), true, '#140 contains 14');
  assert.equal(S.matchesIssue(ITEMS[2], '14'), false);
});

test('matchesIssue: by title and by body, case-insensitively', () => {
  assert.equal(S.matchesIssue(ITEMS[0], 'oficjalne'), true);
  assert.equal(S.matchesIssue(ITEMS[0], 'readme'), true, 'body-only word');
  assert.equal(S.matchesIssue(ITEMS[0], 'mapach'), true);
  assert.equal(S.matchesIssue(ITEMS[0], 'nieobecne'), false);
});

test('matchesIssue: Polish casefolding works without NFD stripping', () => {
  assert.equal(S.matchesIssue(ITEMS[2], 'zażółć'), true);
  assert.equal(S.matchesIssue(ITEMS[2], 'ZAŻÓŁĆ'), true);
  // Documented non-goal: unaccented input does NOT match accented text.
  assert.equal(S.matchesIssue(ITEMS[2], 'zazolc'), false);
});

test('matchesIssue: a missing/!string body is not a crash', () => {
  assert.equal(S.matchesIssue(ITEMS[4], 'bez'), true);
  assert.equal(S.matchesIssue(ITEMS[4], 'opisu'), true);
  assert.equal(S.matchesIssue({}, 'cokolwiek'), false);
  assert.equal(S.matchesIssue(null, 'cokolwiek'), false);
});

test('matchesIssue: "#" alone never degenerates into match-everything', () => {
  // '#' strips to '' — a naive String(number).includes('') is true for every
  // row, which would silently show the unfiltered list.
  assert.equal(S.matchesIssue(ITEMS[1], '#'), false);
});

// ── filterIssues ───────────────────────────────────────────────────────────
test('filterIssues: blank query returns the list untouched', () => {
  assert.equal(S.filterIssues(ITEMS, '   '), ITEMS);
  assert.deepEqual(S.filterIssues(null, ''), []);
});

test('filterIssues: a number query finds number hits AND text hits', () => {
  const nums = S.filterIssues(ITEMS, '14').map((i) => i.number);
  assert.deepEqual(nums.slice().sort((a, b) => a - b), [3, 14, 140]);
});

test('filterIssues: exact number match is ranked first', () => {
  assert.equal(S.filterIssues(ITEMS, '14')[0].number, 14);
  assert.equal(S.filterIssues(ITEMS, '#14')[0].number, 14);
});

test('filterIssues: text query keeps the incoming (gh) order', () => {
  const hits = S.filterIssues(ITEMS, 'o');
  const idx = hits.map((h) => ITEMS.indexOf(h));
  assert.deepEqual(idx, idx.slice().sort((a, b) => a - b));
});

test('filterIssues: no match returns an empty array, not the whole list', () => {
  assert.deepEqual(S.filterIssues(ITEMS, 'zzz-nie-ma'), []);
});

test('filterIssues: never mutates or reorders the source array', () => {
  const before = ITEMS.map((i) => i.number);
  S.filterIssues(ITEMS, '14');
  assert.deepEqual(ITEMS.map((i) => i.number), before);
});

test('filterIssues: query and state filter compose (state is upstream)', () => {
  // The panel filters by state SERVER-side and then searches the result, so
  // searching a closed-only list can only ever return closed rows.
  const closed = ITEMS.filter((i) => i.number === 14);
  assert.deepEqual(S.filterIssues(closed, 'oficjalne').map((i) => i.number), [14]);
});
