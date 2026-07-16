'use strict';

// Unit tests for static/hub-clipboard.js (Node built-in runner — no jsdom/babel),
// mirroring tests/session-switcher-order.test.js. Run: node --test tests/hub-clipboard.test.js
const test = require('node:test');
const assert = require('node:assert/strict');

// window === global so the module's `window.HubClipboard = api` publish lands
// on global, and getSelection/createRange resolve off it in the fallback.
global.window = global;

const { copyText } = require('../src/orbit/static/hub-clipboard.js');

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
