'use strict';

// Unit tests for static/hub-clipboard.js (Node built-in runner — no jsdom/babel),
// mirroring tests/session-switcher-order.test.js. Run: node --test tests/hub-clipboard.test.js
const test = require('node:test');
const assert = require('node:assert/strict');

// window === global so the module's `window.HubClipboard = api` publish lands
// on global, and getSelection/createRange resolve off it in the fallback.
global.window = global;

const { copyText, tidyTerminalText } = require('../src/orbit/static/hub-clipboard.js');

function resetEnv() {
  delete global.navigator;
  delete global.document;
  global.getSelection = undefined;
}

// A minimal fake textarea + document/window for the execCommand fallback path.
function installFakeDom(execResult) {
  const fakeTextarea = {
    value: '',
    style: {},
    setAttribute() {},
    setSelectionRange() {},
    select() {},
  };
  let execCalled = null;
  global.document = {
    body: {
      appendChild() {},
      removeChild() {},
    },
    createElement() { return fakeTextarea; },
    createRange() {
      return { selectNodeContents() {} };
    },
    execCommand(cmd) { execCalled = cmd; return execResult; },
  };
  global.getSelection = () => ({ removeAllRanges() {}, addRange() {} });
  return { getExecCalled: () => execCalled };
}

test('happy path: writeText resolves → true', async () => {
  resetEnv();
  let calledWith = null;
  global.navigator = {
    userAgent: 'desktop',
    clipboard: { writeText(s) { calledWith = s; return Promise.resolve(); } },
  };
  const ok = await copyText('hello');
  assert.equal(ok, true);
  assert.equal(calledWith, 'hello');
});

test('iOS rejection → execCommand fallback fires and copies (regression guard)', async () => {
  resetEnv();
  global.navigator = {
    userAgent: 'iPhone',
    clipboard: { writeText() { return Promise.reject(new Error('NotAllowed')); } },
  };
  const dom = installFakeDom(true);
  const ok = await copyText('payload');
  assert.equal(ok, true);
  assert.equal(dom.getExecCalled(), 'copy');
});

test('writeText rejects AND execCommand returns false → false', async () => {
  resetEnv();
  global.navigator = {
    userAgent: 'iPhone',
    clipboard: { writeText() { return Promise.reject(new Error('NotAllowed')); } },
  };
  installFakeDom(false);
  const ok = await copyText('payload');
  assert.equal(ok, false);
});

test('no navigator.clipboard → straight to execCommand path', async () => {
  resetEnv();
  global.navigator = { userAgent: 'desktop' };
  const dom = installFakeDom(true);
  const ok = await copyText('x');
  assert.equal(ok, true);
  assert.equal(dom.getExecCalled(), 'copy');
});

test('empty / null / undefined → false and clipboard untouched', async () => {
  resetEnv();
  let writeCalled = false;
  global.navigator = {
    userAgent: 'desktop',
    clipboard: { writeText() { writeCalled = true; return Promise.resolve(); } },
  };
  assert.equal(await copyText(''), false);
  assert.equal(await copyText(null), false);
  assert.equal(await copyText(undefined), false);
  assert.equal(writeCalled, false);
});

test('never throws even when DOM stubs throw', async () => {
  resetEnv();
  global.navigator = {
    userAgent: 'iPhone',
    clipboard: { writeText() { return Promise.reject(new Error('NotAllowed')); } },
  };
  global.document = {
    body: { appendChild() {}, removeChild() {} },
    createElement() { throw new Error('boom'); },
    execCommand() { return true; },
  };
  const ok = await copyText('x');
  assert.equal(ok, false); // swallowed → false, no throw
});

// ── tidyTerminalText ───────────────────────────────────────────────
//
// A terminal grid has no "end of line" — every row is `cols` cells wide and
// whatever painted it (tmux redraw, claude's Ink TUI) pads the remainder with
// REAL space characters. xterm.js's own trimRight only drops never-written
// (null) cells, so a copied selection carries those spaces to the clipboard
// and lands in LinkedIn/Slack as ragged trailing whitespace on every line.

test('tidy: strips trailing spaces/tabs per line, keeps indentation', () => {
  assert.equal(
    tidyTerminalText('foo   \n  bar\t\t\n   baz'),
    'foo\n  bar\n   baz',
  );
});

test('tidy: keeps interior blank lines but empties padded ones', () => {
  assert.equal(
    tidyTerminalText('a    \n      \nb    '),
    'a\n\nb',
  );
});

test('tidy: trims leading + trailing whitespace-only lines', () => {
  assert.equal(tidyTerminalText('   \n\nhello   \n   \n  \n'), 'hello');
});

test('tidy: normalizes CRLF without leaving a stray CR', () => {
  assert.equal(tidyTerminalText('a  \r\nb  \r\n'), 'a\nb');
});

test('tidy: non-string / empty input is safe', () => {
  assert.equal(tidyTerminalText(''), '');
  assert.equal(tidyTerminalText(null), '');
  assert.equal(tidyTerminalText(undefined), '');
  assert.equal(tidyTerminalText('     '), '');
});

test('tidy: leaves already-clean text byte-identical', () => {
  const clean = 'line one\n\n  indented\nlast';
  assert.equal(tidyTerminalText(clean), clean);
});

test('tidy: strips NBSP padding too (trailing + whitespace-only rows)', () => {
  assert.equal(tidyTerminalText('a  \n \nb'), 'a\n\nb');
});

// ── left margin ────────────────────────────────────────────────────
//
// claude's TUI paints a 1-space left gutter, so EVERY copied line arrives
// indented by one. Classic dedent (strip the common minimum) handles the
// uniform case and preserves relative indentation, so a copied code block
// keeps its shape.

test('margin: uniform gutter removed, relative indentation preserved', () => {
  assert.equal(tidyTerminalText(' a\n   b\n c'), 'a\n  b\nc');
});

test('margin: a stray column-0 line does not defeat the 1-space gutter', () => {
  // The real UAT paste (2026-07-30): ~34 gutter lines, one flush-left.
  assert.equal(tidyTerminalText(' a\nb\n c\n d\n e'), 'a\nb\nc\nd\ne');
});

test('margin: a RAGGED gutter is peeled even when many lines are flush', () => {
  // Second UAT paste (2026-07-30): claude's TUI painted only 4 of 6 rows with
  // its gutter. A ratio test can't separate this from content — the tell is
  // the width: exactly one space is a gutter, never an indentation level.
  assert.equal(tidyTerminalText('a\nb\n c\n d\n e\n f'), 'a\nb\nc\nd\ne\nf');
});

test('margin: one lone gutter line among flush prose still gets peeled', () => {
  assert.equal(tidyTerminalText('a\nb\n c\nd'), 'a\nb\nc\nd');
});

test('margin: deeper indentation is content and survives a ragged gutter', () => {
  // 2+ spaces is a real indentation level (a code block among prose) — only
  // the exactly-one-space rows are gutter.
  assert.equal(tidyTerminalText('a\n b\n    code\nc'), 'a\nb\n    code\nc');
});

test('margin: a raw unified diff keeps its context-line markers', () => {
  // Context lines start with exactly one space, which is also the gutter
  // signature — here the space MEANS "unchanged", so the hunk header vetoes
  // the peel outright.
  const diff = '@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n ctx2';
  assert.equal(tidyTerminalText(diff), diff);
});

test('margin: a ---/+++ file-header diff is recognized without a hunk line', () => {
  const diff = '--- a/x.js\n+++ b/x.js\n ctx\n-old\n+new';
  assert.equal(tidyTerminalText(diff), diff);
});

test('margin: a diff WITH the TUI gutter still dedents uniformly', () => {
  // Every row indented ⇒ the always-safe common-minimum path, which the diff
  // veto must not block: relative structure (and the markers) survive.
  assert.equal(
    tidyTerminalText(' @@ -1,2 +1,2 @@\n  ctx\n -old\n +new'),
    '@@ -1,2 +1,2 @@\n ctx\n-old\n+new',
  );
});

test('margin: blank lines are ignored when measuring the gutter', () => {
  assert.equal(tidyTerminalText(' a\n\n b'), 'a\n\nb');
});

test('margin: real code indentation is never flattened', () => {
  const code = 'def f():\n    return 1';
  assert.equal(tidyTerminalText(code), code);
});

test('margin: mostly-indented block with a flush line stays untouched', () => {
  // 4/5 lines indented — over the ratio, but a 4-space step is code, not a
  // gutter, so the stray-line rescue must NOT fire.
  const code = '    a\n    b\n    c\n    d\nx';
  assert.equal(tidyTerminalText(code), code);
});

test('margin: a leading tab is real indentation, not a gutter', () => {
  const code = '\ta\n\tb\n\tc';
  assert.equal(tidyTerminalText(code), code);
});
