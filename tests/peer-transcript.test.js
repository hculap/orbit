'use strict';
// Guards for the peer-message ("wiadomość od agenta") wiring in the chat
// transcript. No-build CDN-React project → the two files under test are JSX and
// cannot be imported, so this suite mixes:
//   1. behavioural tests for the JSX-free pure helpers (extracted + eval'd)
//   2. source-scan guards for the two branches that are pure JSX
// Run: node --test tests/peer-transcript.test.js
//
// Background: the backend emits role:"peer" rows (another session's words,
// relayed by the a2a bus). They must NOT be attributed to the local agent, must
// not be filtered out when the peer body is empty, and must not offer a Reply
// affordance (the reply endpoint only accepts assistant rows — clicking it
// clears the composer and then 400s, destroying the typed text).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const STATIC = path.join(__dirname, '..', 'src', 'orbit', 'static');
const TRANSCRIPT = fs.readFileSync(path.join(STATIC, 'orchestrator-transcript.jsx'), 'utf8');
const ACTIONS = fs.readFileSync(path.join(STATIC, 'orchestrator-message-actions.jsx'), 'utf8');

// Pull a top-level `function NAME(...) { ... }` out of a source file, relying on
// the repo's consistent formatting (closing brace at column 0).
function extractFn(src, name) {
  const start = src.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, `function ${name} not found`);
  const end = src.indexOf('\n}\n', start);
  assert.notEqual(end, -1, `end of ${name} not found`);
  return src.slice(start, end + 3);
}

const helpers = (function loadHelpers() {
  const src = extractFn(TRANSCRIPT, 'blockHasContent')
    + '\n' + extractFn(TRANSCRIPT, 'messageHasVisibleContent')
    + '\nreturn { blockHasContent, messageHasVisibleContent };';
  // eslint-disable-next-line no-new-func
  return new Function(src)();
})();

// ── messageHasVisibleContent: peer rows survive the transcript filter ───────
test('messageHasVisibleContent: peer row with an empty body is kept', () => {
  const msg = { role: 'peer', peer: { from: 'areas/dom' }, blocks: [] };
  assert.equal(helpers.messageHasVisibleContent(msg), true);
});

test('messageHasVisibleContent: peer row with a text block is kept', () => {
  const msg = { role: 'peer', blocks: [{ kind: 'text', text: 'hej' }] };
  assert.equal(helpers.messageHasVisibleContent(msg), true);
});

test('messageHasVisibleContent: unchanged for user / assistant', () => {
  assert.equal(helpers.messageHasVisibleContent({ role: 'user', blocks: [] }), true);
  assert.equal(helpers.messageHasVisibleContent({ role: 'assistant', blocks: [] }), false);
  assert.equal(
    helpers.messageHasVisibleContent({ role: 'assistant', blocks: [{ kind: 'text', text: 'x' }] }),
    true,
  );
  assert.equal(helpers.messageHasVisibleContent(null), false);
});

// ── MessageBubbleImpl dispatches the peer role away from the agent branch ───
test('MessageBubbleImpl dispatches role "peer" to window.PeerMessage', () => {
  const body = TRANSCRIPT.slice(TRANSCRIPT.indexOf('function MessageBubbleImpl('));
  assert.match(body, /msg\.role === 'peer'/, 'no peer branch in MessageBubbleImpl');
  assert.match(body, /<PeerBubble/, 'the peer branch must render PeerBubble');
  // PeerBubble is the (guarded) hand-off to the real card in orchestrator.jsx.
  assert.match(
    TRANSCRIPT.slice(TRANSCRIPT.indexOf('function PeerBubble(')),
    /window\.PeerMessage/,
    'PeerBubble does not hand off to window.PeerMessage',
  );
  // The dispatch must happen before the user branch (and therefore before the
  // assistant fallthrough that would label the peer as the local agent).
  const peerAt = body.indexOf("msg.role === 'peer'");
  const userAt = body.indexOf('if (isUser) {');
  assert.ok(peerAt !== -1 && peerAt < userAt, 'peer dispatch must precede the user branch');
});

// ── the Reply affordance is never offered on a peer row ────────────────────
test('MessageActionRow does not offer replyTo for role "peer"', () => {
  const body = ACTIONS.slice(ACTIONS.indexOf('function MessageActionRow('));
  assert.match(body, /role === 'peer'/, 'MessageActionRow does not know about the peer role');
  const renderLine = body.split('\n').find(l => l.includes("typeof safeActions.replyTo === 'function'"));
  assert.ok(renderLine, 'replyTo affordance disappeared');
  assert.match(renderLine, /!isPeer/, 'the replyTo render must be gated on !isPeer');
});
