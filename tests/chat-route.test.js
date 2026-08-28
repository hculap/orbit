'use strict';
// Pure-logic unit tests for the /chat overlay segment. No-build CDN-React
// project → the encode/decode pair lives in a JSX-free module testable with
// Node's built-in runner (zero deps):
//   node --test tests/chat-route.test.js
//
// The .jsx wiring (router branch, the derived-from-route modals in
// orchestrator.jsx, push-to-open / back-to-close) is covered by the manual
// smoke checklist — there is no jsdom/Playwright in this repo.

const test = require('node:test');
const assert = require('node:assert/strict');
const cr = require('../src/orbit/static/chat-route.js');

// router.jsx splits the path on '/' and passes parts.slice(1) — everything
// after the 'chat' / 'orchestrator' head. These tests feed parseChatTail the
// SAME already-split segment arrays.

test('parseChatTail: bare /chat', () => {
  assert.deepEqual(cr.parseChatTail([]), { sessionId: undefined, overlay: undefined });
  assert.deepEqual(cr.parseChatTail(undefined), { sessionId: undefined, overlay: undefined });
});

test('parseChatTail: /chat/<sid> (unchanged behaviour)', () => {
  assert.deepEqual(cr.parseChatTail(['abc123']), { sessionId: 'abc123', overlay: undefined });
});

test('parseChatTail: /chat/<sid>/<overlay>', () => {
  assert.deepEqual(cr.parseChatTail(['abc123', 'gallery']), { sessionId: 'abc123', overlay: 'gallery' });
  assert.deepEqual(cr.parseChatTail(['abc123', 'issues']), { sessionId: 'abc123', overlay: 'issues' });
});

// THE reserved-word guard. Without it sessionId would be 'gallery' and the
// orchestrator would fire hub:open-session with a garbage id.
test('parseChatTail: session-less /chat/<overlay> never becomes a session id', () => {
  assert.deepEqual(cr.parseChatTail(['gallery']), { sessionId: undefined, overlay: 'gallery' });
  assert.deepEqual(cr.parseChatTail(['issues']), { sessionId: undefined, overlay: 'issues' });
});

test('parseChatTail: an unknown trailing segment is ignored, not an overlay', () => {
  assert.deepEqual(cr.parseChatTail(['abc123', 'bogus']), { sessionId: 'abc123', overlay: undefined });
});

test('parseChatTail: a session id that merely LOOKS like a word still resolves', () => {
  assert.deepEqual(cr.parseChatTail(['galleries']), { sessionId: 'galleries', overlay: undefined });
});

test('buildChatTail: round-trips every parse case', () => {
  assert.equal(cr.buildChatTail({}), '');
  assert.equal(cr.buildChatTail(), '');
  assert.equal(cr.buildChatTail({ sessionId: 'abc' }), '/abc');
  assert.equal(cr.buildChatTail({ sessionId: 'abc', overlay: 'issues' }), '/abc/issues');
  assert.equal(cr.buildChatTail({ overlay: 'gallery' }), '/gallery');
});

test('buildChatTail: an unknown overlay can never reach the address bar', () => {
  assert.equal(cr.buildChatTail({ sessionId: 'abc', overlay: 'bogus' }), '/abc');
  assert.equal(cr.buildChatTail({ sessionId: 'abc', overlay: '' }), '/abc');
});

test('build → parse is a fixpoint for every valid combination', () => {
  const specs = [
    { sessionId: 'abc', overlay: 'gallery' },
    { sessionId: 'abc', overlay: 'issues' },
    { sessionId: 'abc' },
    { overlay: 'gallery' },
  ];
  for (const spec of specs) {
    const parts = cr.buildChatTail(spec).replace(/^\/+/, '').split('/').filter(Boolean);
    const got = cr.parseChatTail(parts);
    assert.equal(got.sessionId, spec.sessionId);
    assert.equal(got.overlay, spec.overlay);
  }
});

test('stripChatOverlay: drops the overlay so the PWA restores the SESSION', () => {
  assert.equal(cr.stripChatOverlay('/chat/abc/gallery'), '/chat/abc');
  assert.equal(cr.stripChatOverlay('/chat/abc/issues'), '/chat/abc');
  assert.equal(cr.stripChatOverlay('/orchestrator/abc/gallery'), '/orchestrator/abc');
  assert.equal(cr.stripChatOverlay('/chat/gallery'), '/chat');
});

test('stripChatOverlay: idempotent, and leaves everything else untouched', () => {
  assert.equal(cr.stripChatOverlay(cr.stripChatOverlay('/chat/abc/gallery')), '/chat/abc');
  assert.equal(cr.stripChatOverlay('/chat/abc'), '/chat/abc');
  assert.equal(cr.stripChatOverlay('/chat'), '/chat');
  assert.equal(cr.stripChatOverlay('/projects/x/issues'), '/projects/x/issues');
  assert.equal(cr.stripChatOverlay('/settings/gallery'), '/settings/gallery');
  assert.equal(cr.stripChatOverlay('/'), '/');
  assert.equal(cr.stripChatOverlay(''), '');
  assert.equal(cr.stripChatOverlay(null), null);
});

// ── overlayToCarry ────────────────────────────────────────────────────────
// Regression guard for the cold-deep-link bug: onSelectSession reflects the
// selected session into the URL with routerReplace(buildPath(...)), and that
// replace ALSO runs on the hub:open-session replay of a deep link (activeId is
// still null there, so the "same session" early-return can't fire). Rebuilding
// without the overlay stripped /chat/<sid>/gallery back to /chat/<sid> about
// one fetch after first paint — the modal flashed open and vanished.

test('overlayToCarry: keeps the overlay when the URL already names this session', () => {
  const route = { section: 'orchestrator', sessionId: 'abc', overlay: 'gallery' };
  assert.equal(cr.overlayToCarry(route, 'abc'), 'gallery');
  assert.equal(cr.overlayToCarry({ ...route, overlay: 'issues' }, 'abc'), 'issues');
});

test('overlayToCarry: the carried value survives a buildChatTail round-trip', () => {
  const route = { section: 'orchestrator', sessionId: 'abc', overlay: 'issues' };
  const overlay = cr.overlayToCarry(route, 'abc');
  // This is exactly what onSelectSession feeds buildPath.
  assert.equal('/chat' + cr.buildChatTail({ sessionId: 'abc', overlay }), '/chat/abc/issues');
});

test('overlayToCarry: drops it when switching to a DIFFERENT session', () => {
  const route = { section: 'orchestrator', sessionId: 'abc', overlay: 'gallery' };
  assert.equal(cr.overlayToCarry(route, 'other'), undefined);
  assert.equal('/chat' + cr.buildChatTail({ sessionId: 'other', overlay: cr.overlayToCarry(route, 'other') }), '/chat/other');
});

test('overlayToCarry: nothing to carry from a session-less or overlay-less URL', () => {
  assert.equal(cr.overlayToCarry({ section: 'orchestrator', sessionId: 'abc' }, 'abc'), undefined);
  // Bare /chat/gallery names no session, so a real id is never "the same".
  assert.equal(cr.overlayToCarry({ section: 'orchestrator', overlay: 'gallery' }, 'abc'), undefined);
});

test('overlayToCarry: ignores non-chat routes, junk overlays and missing input', () => {
  assert.equal(cr.overlayToCarry({ section: 'projects', sessionId: 'abc', overlay: 'issues' }, 'abc'), undefined);
  assert.equal(cr.overlayToCarry({ section: 'orchestrator', sessionId: 'abc', overlay: 'bogus' }, 'abc'), undefined);
  assert.equal(cr.overlayToCarry(null, 'abc'), undefined);
  assert.equal(cr.overlayToCarry({ section: 'orchestrator', sessionId: 'abc', overlay: 'gallery' }, null), undefined);
});

test('isChatOverlay: the reserved-word set', () => {
  assert.equal(cr.isChatOverlay('gallery'), true);
  assert.equal(cr.isChatOverlay('issues'), true);
  assert.equal(cr.isChatOverlay('overview'), false);
  assert.equal(cr.isChatOverlay(undefined), false);
  assert.deepEqual(cr.OVERLAYS.slice().sort(), ['gallery', 'issues']);
});
