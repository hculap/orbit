'use strict';
// Pure-logic unit tests for the "kontekst NN%" badge sizing. No-build
// CDN-React project → the rule lives in a JSX-free module testable with Node's
// built-in runner (zero deps):
//   node --test tests/model-context.test.js
//
// Regression guard: a hardcoded model version has mis-scaled this badge twice
// — first `opus-4-7`-only (a 597k Opus 4.8 session read 100% red instead of
// ~60%), then `opus-4`-prefix-only (broke the day claude-opus-5 shipped). The
// contract below is therefore inverted: 1M is the DEFAULT and 200k is the
// exception list (Haiku, pre-4.6 Opus/Sonnet, legacy version-first ids).

const test = require('node:test');
const assert = require('node:assert/strict');
const mc = require('../src/orbit/static/model-context.js');

// ── 1M-context models ──────────────────────────────────────────────────────
test('contextWindow: current Opus/Sonnet/Fable ids are 1M', () => {
  for (const id of [
    'claude-opus-5',
    'claude-opus-4-8',
    'claude-opus-4-7',
    'claude-opus-4-6',
    'claude-sonnet-5',
    'claude-sonnet-4-6',
    'claude-fable-5',
    'claude-mythos-5',
  ]) {
    assert.equal(mc.contextWindow(id), mc.WIN_1M, id);
  }
});

test('contextWindow: a future version in a 1M family stays 1M', () => {
  // The whole point of the family rule — no edit needed when these ship.
  for (const id of ['claude-opus-6', 'claude-sonnet-6', 'claude-fable-6', 'claude-opus-5-1']) {
    assert.equal(mc.contextWindow(id), mc.WIN_1M, id);
  }
});

test('contextWindow: an explicit [1m] suffix wins outright', () => {
  assert.equal(mc.contextWindow('claude-opus-5[1m]'), mc.WIN_1M);
  // Even for a family that would otherwise be sized at 200k.
  assert.equal(mc.contextWindow('claude-sonnet-4-5[1m]'), mc.WIN_1M);
});

// ── 200k-context models ────────────────────────────────────────────────────
test('contextWindow: Haiku is 200k through v4', () => {
  assert.equal(mc.contextWindow('claude-haiku-4-5-20251001'), mc.WIN_200K);
  assert.equal(mc.contextWindow('claude-haiku-3-5'), mc.WIN_200K);
});

test('contextWindow: pre-4.6 Opus/Sonnet are 200k', () => {
  assert.equal(mc.contextWindow('claude-opus-4-5'), mc.WIN_200K);
  assert.equal(mc.contextWindow('claude-sonnet-4-5'), mc.WIN_200K);
  assert.equal(mc.contextWindow('claude-opus-4'), mc.WIN_200K);
});

test('contextWindow: legacy version-first ids do not read their date as a version', () => {
  // `claude-3-5-sonnet-20241022` would otherwise match `sonnet-20241022`.
  assert.equal(mc.contextWindow('claude-3-5-sonnet-20241022'), mc.WIN_200K);
  assert.equal(mc.contextWindow('claude-3-7-sonnet-20250219'), mc.WIN_200K);
});

test('contextWindow: unknown / missing ids default to 1M', () => {
  // Claude ships 1M everywhere except the exceptions above, so an id we can't
  // parse is assumed 1M — a badge that reads low beats a false 100% red.
  for (const id of ['', '<synthetic>', 'opus', 'claude-next', null, undefined, 42, {}]) {
    assert.equal(mc.contextWindow(id), mc.WIN_1M, String(id));
  }
});

// ── formatTokens ───────────────────────────────────────────────────────────
test('formatTokens: k / M scaling', () => {
  assert.equal(mc.formatTokens(0), '0');
  assert.equal(mc.formatTokens(-5), '0');
  assert.equal(mc.formatTokens(900), '900');
  assert.equal(mc.formatTokens(1500), '2k');
  assert.equal(mc.formatTokens(304367), '304k');
  assert.equal(mc.formatTokens(1000000), '1.0M');
  assert.equal(mc.formatTokens(12000000), '12M');
});

// ── contextUsage ───────────────────────────────────────────────────────────
test('contextUsage: null when there is nothing to render', () => {
  assert.equal(mc.contextUsage(0, 'claude-opus-5'), null);
  assert.equal(mc.contextUsage(null, 'claude-opus-5'), null);
  assert.equal(mc.contextUsage('nope', 'claude-opus-5'), null);
});

test('contextUsage: the regression case reads 30%, not 100%', () => {
  // Real session from the bug report: 304 367 tokens on claude-opus-5.
  const u = mc.contextUsage(304367, 'claude-opus-5');
  assert.equal(u.window, mc.WIN_1M);
  assert.equal(u.pct, 30);
  assert.equal(u.color, 'var(--fg-3)');
  assert.match(u.title, /^304k \/ 1\.0M tokens · claude-opus-5$/);
});

test('contextUsage: colour tiers at 60 / 80', () => {
  // The tiers compare the ROUNDED pct, so 594k → 59% (muted) but 599k → 60%.
  assert.equal(mc.contextUsage(594000, 'claude-opus-5').color, 'var(--fg-3)');
  assert.equal(mc.contextUsage(600000, 'claude-opus-5').color, 'var(--accent)');
  assert.equal(mc.contextUsage(794000, 'claude-opus-5').color, 'var(--accent)');
  assert.equal(mc.contextUsage(800000, 'claude-opus-5').color, 'var(--err)');
});

test('contextUsage: pct is clamped at 100', () => {
  assert.equal(mc.contextUsage(5000000, 'claude-opus-5').pct, 100);
});

test('contextUsage: a 200k model still fills up fast', () => {
  const u = mc.contextUsage(180000, 'claude-haiku-4-5-20251001');
  assert.equal(u.window, mc.WIN_200K);
  assert.equal(u.pct, 90);
  assert.equal(u.color, 'var(--err)');
});
