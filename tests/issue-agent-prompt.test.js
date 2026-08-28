'use strict';
// Pure-logic unit tests for the two prompts the Issues panel pastes into the
// live agent session ("Rozwiąż" / "Dopracuj"):
//   node --test tests/issue-agent-prompt.test.js
//
// These assertions are the feature's contract, not decoration:
//   * ONE LINE — at 4+ lines claude's REPL collapses a bracketed paste into an
//     opaque "[Pasted text #N +M lines]" chip the user cannot read or edit
//     (measured against claude-cli 2.1.237);
//   * well under 8192 chars — POST /term/paste rejects longer text with a 400
//     that surfaces as an untranslated toast;
//   * the two prompts must be genuinely DIFFERENT in intent — "Dopracuj" that
//     starts editing code is the whole failure mode this wording guards.

const test = require('node:test');
const assert = require('node:assert/strict');
const P = require('../src/orbit/static/issue-agent-prompt.js');

const ISSUE = {
  number: 14,
  title: 'Oficjalne przykłady',
  url: 'https://github.com/your-github-user/orbit/issues/14',
};

// ── shape constraints (both prompts) ───────────────────────────────────────
for (const [mode, build] of [['solve', P.buildSolvePrompt], ['refine', P.buildRefinePrompt]]) {
  test(`${mode}: is exactly one line`, () => {
    assert.equal(build(ISSUE).indexOf('\n'), -1);
    assert.equal(build(ISSUE).indexOf('\r'), -1);
  });

  test(`${mode}: stays far below the 8192-char /term/paste cap`, () => {
    assert.ok(build(ISSUE).length < 1200, `length=${build(ISSUE).length}`);
  });

  test(`${mode}: carries the issue number, title and url`, () => {
    const t = build(ISSUE);
    assert.match(t, /#14\b/);
    assert.ok(t.includes('Oficjalne przykłady'));
    assert.ok(t.includes(ISSUE.url));
  });

  test(`${mode}: points at a command that expands the issue`, () => {
    assert.ok(build(ISSUE).includes('gh issue view 14 --comments'));
  });

  test(`${mode}: survives a payload with no url`, () => {
    const t = build({ number: 9, title: 'Bez url' });
    assert.match(t, /#9\b/);
    assert.ok(!t.includes('()'), 'empty parens would look like a bug');
  });
}

// ── intent: the two prompts must not be interchangeable ────────────────────
test('solve: asks for an implementation but forbids an unsupervised commit', () => {
  const t = P.buildSolvePrompt(ISSUE);
  assert.ok(t.startsWith('Rozwiąż issue #14'));
  assert.ok(t.includes('zaimplementuj'));
  assert.ok(t.includes('nie commituj'));
});

test('refine: forbids implementing and demands a write-back to GitHub', () => {
  const t = P.buildRefinePrompt(ISSUE);
  assert.ok(t.includes('NIE implementuj'));
  assert.ok(t.includes('nie zmieniaj kodu'));
  assert.ok(t.includes('kryteria akceptacji'));
  assert.ok(t.includes('gh issue edit 14 --body-file -'));
  assert.ok(!t.includes('zaimplementuj'), 'must not read as an implement order');
});

test('the two prompts are genuinely different text', () => {
  assert.notEqual(P.buildSolvePrompt(ISSUE), P.buildRefinePrompt(ISSUE));
});

// ── buildPrompt dispatch ───────────────────────────────────────────────────
test('buildPrompt: routes by mode and refuses anything unknown', () => {
  assert.equal(P.buildPrompt('solve', ISSUE), P.buildSolvePrompt(ISSUE));
  assert.equal(P.buildPrompt('refine', ISSUE), P.buildRefinePrompt(ISSUE));
  // Empty string, NOT a default prompt — pasting the wrong instruction is
  // worse than pasting nothing, and the caller skips a falsy payload.
  assert.equal(P.buildPrompt('nonsense', ISSUE), '');
  assert.equal(P.buildPrompt(undefined, ISSUE), '');
});

// ── title handling ─────────────────────────────────────────────────────────
test('clampTitle: collapses whitespace and caps the length', () => {
  assert.equal(P.clampTitle('  a   b \n c '), 'a b c');
  const long = P.clampTitle('x'.repeat(400));
  assert.equal(long.length, P.TITLE_MAX);
  assert.ok(long.endsWith('…'));
  assert.equal(P.clampTitle(null), '');
});

test('issueRef: a newline in the title can never break the one-line rule', () => {
  const t = P.buildSolvePrompt({ number: 1, title: 'a\nb', url: '' });
  assert.equal(t.indexOf('\n'), -1);
  assert.ok(t.includes('„a b”'));
});

test('issueRef: title-less issue still references the number', () => {
  assert.equal(P.issueRef({ number: 5 }), '#5');
});
