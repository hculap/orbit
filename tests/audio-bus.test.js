'use strict';
// Pure-logic unit tests for the cross-subsystem audio bus (issue #89).
// No-build CDN-React project → the bus is a JSX-free module testable with
// Node's built-in runner (zero deps):
//   node --test tests/audio-bus.test.js
//
// The .jsx wiring (media-player / TTS / conversation registration) is covered
// by the manual smoke checklist in the PR description — there is no jsdom here.

const test = require('node:test');
const assert = require('node:assert/strict');
const busModule = require('../src/orbit/static/audio-bus.js');

// Each test uses a fresh bus instance for isolation (the default export is a
// process-wide singleton that also exposes its factory).
const makeBus = () => busModule.createAudioBus();

const spyPause = () => {
  const fn = () => { fn.calls += 1; };
  fn.calls = 0;
  return fn;
};

test('requestActive pauses every OTHER source, not the requester', () => {
  const bus = makeBus();
  const a = spyPause();
  const b = spyPause();
  bus.register('A', { kind: 'tts', pause: a });
  bus.register('B', { kind: 'media', pause: b });

  bus.requestActive('A');
  assert.equal(a.calls, 0, 'requester is never paused');
  assert.equal(b.calls, 1, 'the other source is paused once');
  assert.equal(bus.getActive(), 'A');
});

test('requestActive flips active and pauses the previous owner', () => {
  const bus = makeBus();
  const a = spyPause();
  const b = spyPause();
  bus.register('A', { pause: a });
  bus.register('B', { pause: b });

  bus.requestActive('A');
  bus.requestActive('B');
  assert.equal(a.calls, 1, 'previous owner A paused on handoff');
  assert.equal(bus.getActive(), 'B');
});

test('re-requesting the same active id is idempotent (pauses nobody)', () => {
  const bus = makeBus();
  const a = spyPause();
  const b = spyPause();
  bus.register('A', { pause: a });
  bus.register('B', { pause: b });

  bus.requestActive('A');
  const bBefore = b.calls;
  bus.requestActive('A'); // second self-request
  assert.equal(a.calls, 0, 'self never paused');
  assert.equal(b.calls, bBefore, 'no extra pause on idempotent re-request');
  assert.equal(bus.getActive(), 'A');
});

test('release clears active only for the active id; non-active release is a no-op', () => {
  const bus = makeBus();
  bus.register('A', { pause: spyPause() });
  bus.register('B', { pause: spyPause() });

  bus.requestActive('B');
  bus.release('A'); // A is not active → no-op
  assert.equal(bus.getActive(), 'B');

  bus.release('B'); // active → clears
  assert.equal(bus.getActive(), null);
});

test('a source pause that throws does not break the handoff', () => {
  const bus = makeBus();
  const boom = () => { throw new Error('nope'); };
  const b = spyPause();
  bus.register('A', { pause: boom });
  bus.register('B', { pause: b });

  assert.doesNotThrow(() => bus.requestActive('C'));
  assert.equal(b.calls, 1, 'B still paused even though A threw');
  assert.equal(bus.getActive(), 'C');
});

test('unregister (returned fn) removes the source so it is never paused again', () => {
  const bus = makeBus();
  const a = spyPause();
  const un = bus.register('A', { pause: a });
  bus.register('B', { pause: spyPause() });

  un();
  bus.requestActive('B');
  assert.equal(a.calls, 0, 'unregistered source not paused');
});

test('unregister clears active when the active source is removed', () => {
  const bus = makeBus();
  bus.register('A', { pause: spyPause() });
  bus.requestActive('A');
  assert.equal(bus.getActive(), 'A');

  bus.unregister('A'); // method form
  assert.equal(bus.getActive(), null, 'active cleared when active source removed');
});

test('reentrant release() inside a paused source does not desync active (#89)', () => {
  // Repro of the bus desync: a paused source's callback re-enters the bus by
  // releasing the channel mid-handoff. Before the re-affirm guard this left
  // getActive()===null while the new owner was audibly playing.
  const bus = makeBus();
  bus.register('conv', {
    kind: 'conv',
    // Simulates conv.pause → cancel() → release('tts') firing while we are
    // still inside requestActive('tts').
    pause: () => { bus.release('tts'); },
  });
  bus.register('tts', { kind: 'tts', pause: spyPause() });

  bus.requestActive('tts');
  assert.equal(bus.getActive(), 'tts', 'active stays the new owner despite reentrant release');
});

test('singleton is published with both module.exports and createAudioBus factory', () => {
  assert.equal(typeof busModule.requestActive, 'function');
  assert.equal(typeof busModule.register, 'function');
  assert.equal(typeof busModule.release, 'function');
  assert.equal(typeof busModule.getActive, 'function');
  assert.equal(typeof busModule.unregister, 'function');
  assert.equal(typeof busModule.createAudioBus, 'function');
});
