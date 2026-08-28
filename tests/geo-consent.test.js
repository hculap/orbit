'use strict';
// Pure-logic unit tests for the browser half of the geolocation round trip.
// No-build CDN-React project → every DECISION lives in a JSX-free module the
// built-in runner can require() with zero deps:
//   node --test tests/geo-consent.test.js
//
// Two things are pinned here that nothing else can catch:
//   1. the consent LADDER — the order is a safety property, so each rung is
//      asserted against a payload that would take a different branch if the
//      one above it were dropped (opt-out beats 'auto', insecure beats
//      opt-out, no-api beats everything);
//   2. the STATUSES literals + roundCoords parity — the JS half of the two-way
//      drift guard against orchestrator_geo.STATUSES / _geo_coords.rebuild_coords.
//      The expected coordinates below are the VERBATIM output of the Python
//      function for the same input; if you change either rounding rule, this
//      file must be re-derived from Python, never the other way round.
//
// CI is pytest-only, so the Python-side drift guard is the load-bearing
// protection — run this one by hand after touching geo-consent.js.

const test = require('node:test');
const assert = require('node:assert/strict');
const g = require('../src/orbit/static/geo-consent.js');

// A well-formed `secrets.token_urlsafe(16)` id (22 chars).
const RID = 'AbCdEfGhIjKlMnOpQrStUv';

// ── STATUSES: the agent-facing contract ────────────────────────────────────
test('STATUSES: the 18 rows of the Python table, reason + retryable verbatim', () => {
  const expected = {
    fixed: ['browser_fix', false],
    cached: ['cached_fix', false],
    pending: ['awaiting_browser', true],
    denied: ['permission_denied', false],
    device_opt_out: ['device_opt_out', false],
    dismissed: ['user_dismissed', false],
    insecure_context: ['insecure_context', false],
    no_api: ['no_geolocation_api', false],
    unavailable: ['position_unavailable', true],
    timeout: ['browser_timeout', true],
    expired: ['request_expired', false],
    aborted: ['request_aborted', false],
    no_browser: ['no_browser_attached', false],
    not_found: ['unknown_request', false],
    rate_limited: ['too_many_requests', true],
    disabled: ['feature_disabled', false],
    geocode_disabled: ['geocode_disabled', false],
    geocode_failed: ['upstream_error', true],
  };
  assert.deepEqual(Object.keys(g.STATUSES).sort(), Object.keys(expected).sort());
  for (const [status, [reason, isRetryable]] of Object.entries(expected)) {
    assert.equal(g.STATUSES[status].reason, reason, `reason for ${status}`);
    assert.equal(g.STATUSES[status].retryable, isRetryable, `retryable for ${status}`);
  }
});

test('STATUSES: every row carries a Polish message, and a hint unless it succeeded', () => {
  for (const [status, row] of Object.entries(g.STATUSES)) {
    assert.equal(typeof row.message, 'string', `message type for ${status}`);
    assert.ok(row.message.length > 0, `empty message for ${status}`);
    assert.equal(typeof row.hint, 'string', `hint type for ${status}`);
    assert.equal(typeof row.retryable, 'boolean', `retryable type for ${status}`);
    // Only the outright success has nothing to advise.
    if (status !== 'fixed') assert.ok(row.hint.length > 0, `empty hint for ${status}`);
  }
});

test('describe: an unknown status degrades to not_found, never to undefined', () => {
  assert.deepEqual(g.describe('nonsense'), g.describe('not_found'));
  assert.deepEqual(g.describe(undefined), g.describe('not_found'));
  assert.equal(g.retryable('nonsense'), false);
  assert.equal(g.retryable('unavailable'), true);
  assert.equal(g.retryable('denied'), false);
});

test('exported literals the Python side is diffed against', () => {
  assert.equal(g.REQUEST_ID_RE.source, '^[A-Za-z0-9_-]{16,64}$');
  assert.deepEqual(g.ERROR_CODE_STATUS, { 1: 'denied', 2: 'unavailable', 3: 'timeout' });
  assert.deepEqual(g.ANSWER_STATUSES.slice().sort(), [
    'denied', 'device_opt_out', 'dismissed', 'fixed',
    'insecure_context', 'no_api', 'timeout', 'unavailable',
  ]);
  assert.deepEqual(g.ACCURACIES, ['fine', 'coarse']);
  assert.deepEqual(g.CONSENT_MODES, ['auto', 'ask']);
  assert.deepEqual(g.PRECISION_DP, { fine: 5, coarse: 2 });
  assert.equal(g.COARSE_ACCURACY_FLOOR_M, 1000);
  assert.equal(g.REASON_MAX_CHARS, 200);
  // Bottom rung of the timeout ladder: 20 s < 45 s poll window < 180 s TTL.
  assert.equal(g.POSITION_TIMEOUT_MS, 20000);
});

// ── errorToStatus ──────────────────────────────────────────────────────────
test('errorToStatus: the three GeolocationPositionError codes', () => {
  assert.equal(g.errorToStatus(1), 'denied');
  assert.equal(g.errorToStatus(2), 'unavailable');
  assert.equal(g.errorToStatus(3), 'timeout');
});

test('errorToStatus: anything else is the retryable "unavailable", not a denial', () => {
  // Reporting a permission refusal that never happened is the worst possible
  // guess, so an unmapped code must land on the transient branch.
  for (const code of [undefined, null, 0, 4, 99, -1, 'x', {}]) {
    assert.equal(g.errorToStatus(code), 'unavailable', `code ${JSON.stringify(code)}`);
  }
});

// ── consent ladder ─────────────────────────────────────────────────────────
// Baseline = the happiest possible page: API present, https, no opt-out.
function opts(over) {
  return Object.assign({ hasApi: true, secure: true, optOut: false, mode: 'auto', autoGrantUntil: 0, permission: null, now: 1000 }, over);
}

test('consent: no geolocation API wins over every other rung', () => {
  // optOut/mode/snooze all set to values that would otherwise branch away.
  const d = g.decideConsent(opts({ hasApi: false, secure: false, optOut: true, mode: 'ask' }));
  assert.deepEqual(d, { action: 'answer', status: 'no_api', reason: 'no_geolocation_api' });
});

test('consent: an insecure origin beats the opt-out and both modes', () => {
  for (const mode of ['auto', 'ask']) {
    const d = g.decideConsent(opts({ secure: false, optOut: true, mode: mode }));
    assert.equal(d.action, 'answer', `mode ${mode}`);
    assert.equal(d.status, 'insecure_context');
    assert.equal(d.reason, 'insecure_context');
  }
});

test('consent: the per-device opt-out outranks mode "auto" (the kill switch)', () => {
  const d = g.decideConsent(opts({ optOut: true, mode: 'auto' }));
  assert.deepEqual(d, { action: 'answer', status: 'device_opt_out', reason: 'device_opt_out' });
});

test('consent: the per-device opt-out outranks a live snooze in "ask" mode', () => {
  const d = g.decideConsent(opts({ optOut: true, mode: 'ask', autoGrantUntil: 999999 }));
  assert.equal(d.status, 'device_opt_out');
});

test('consent: mode "auto" grants without any in-app modal', () => {
  // D1: the browser's OWN permission sheet is the only blocking gate; the
  // React glue still has to show the non-blocking toast.
  const d = g.decideConsent(opts({ mode: 'auto' }));
  assert.deepEqual(d, { action: 'grant', status: null, reason: 'consent_auto' });
});

test('consent: mode "auto" ignores the snooze window entirely', () => {
  // The snooze is an `ask`-mode affordance; in auto it must not change the
  // reason string the toast/telemetry reads.
  assert.equal(g.decideConsent(opts({ mode: 'auto', autoGrantUntil: 0 })).reason, 'consent_auto');
  assert.equal(g.decideConsent(opts({ mode: 'auto', autoGrantUntil: 999999 })).reason, 'consent_auto');
});

test('consent: an unrecognised mode degrades to "auto", never to an unknown branch', () => {
  for (const mode of [undefined, null, '', 'prompt', 'ASK', 42]) {
    assert.equal(g.decideConsent(opts({ mode: mode })).reason, 'consent_auto', `mode ${JSON.stringify(mode)}`);
  }
  assert.equal(g.normalizeMode('ask'), 'ask');
  assert.equal(g.normalizeMode('nonsense'), 'auto');
});

test('consent: mode "ask" with no snooze renders the modal', () => {
  const d = g.decideConsent(opts({ mode: 'ask' }));
  assert.deepEqual(d, { action: 'prompt', status: null, reason: 'consent_ask' });
});

test('consent: mode "ask" with a live snooze grants silently', () => {
  const d = g.decideConsent(opts({ mode: 'ask', autoGrantUntil: 1001 }));
  assert.deepEqual(d, { action: 'grant', status: null, reason: 'consent_snoozed' });
});

test('consent: an EXPIRED snooze falls through to the modal', () => {
  // Boundary: strictly-greater, so `until === now` is already stale.
  assert.equal(g.decideConsent(opts({ mode: 'ask', autoGrantUntil: 1000 })).action, 'prompt');
  assert.equal(g.decideConsent(opts({ mode: 'ask', autoGrantUntil: 999 })).action, 'prompt');
  assert.equal(g.decideConsent(opts({ mode: 'ask', autoGrantUntil: 1001 })).action, 'grant');
});

test('consent: a snooze survives a missing Permissions API but not a denial', () => {
  // Safari does not expose geolocation to permissions.query → null must NOT
  // cancel the snooze, or the checkbox would silently never work on iOS.
  const live = { mode: 'ask', autoGrantUntil: 9999 };
  assert.equal(g.decideConsent(opts(Object.assign({ permission: null }, live))).action, 'grant');
  assert.equal(g.decideConsent(opts(Object.assign({ permission: 'granted' }, live))).action, 'grant');
  assert.equal(g.decideConsent(opts(Object.assign({ permission: 'prompt' }, live))).action, 'grant');
  assert.equal(g.decideConsent(opts(Object.assign({ permission: 'denied' }, live))).action, 'prompt');
});

test('consent: a missing options object is treated as a page with no API', () => {
  assert.equal(g.decideConsent().status, 'no_api');
  assert.equal(g.decideConsent({}).status, 'no_api');
});

// ── auto-grant window ──────────────────────────────────────────────────────
test('autoGrantLive: only a future, finite, positive expiry counts', () => {
  assert.equal(g.autoGrantLive(2000, 1000), true);
  assert.equal(g.autoGrantLive(1000, 1000), false);
  assert.equal(g.autoGrantLive(500, 1000), false);
  for (const bad of [0, -1, null, undefined, NaN, Infinity, 'soon', {}]) {
    assert.equal(g.autoGrantLive(bad, 1000), false, `until ${JSON.stringify(bad)}`);
  }
});

test('setAutoGrant: returns the new expiry even when storage is unavailable', () => {
  // Node has no localStorage; the same path covers Safari private mode, where
  // the caller's in-memory state must still be correct.
  assert.equal(g.setAutoGrant(15, 1000), 1000 + 15 * 60 * 1000);
  assert.equal(g.setAutoGrant(1, 0), 60000);
  assert.equal(g.setAutoGrant(undefined, 0), g.AUTO_GRANT_MINUTES * 60 * 1000);
  assert.equal(g.readAutoGrant(), 0);
  assert.doesNotThrow(() => g.clearAutoGrant());
});

test('setAutoGrant + autoGrantLive agree on the 15-minute window', () => {
  const at = g.setAutoGrant(g.AUTO_GRANT_MINUTES, 1000);
  assert.equal(g.autoGrantLive(at, 1000 + 14 * 60 * 1000), true);
  assert.equal(g.autoGrantLive(at, 1000 + 15 * 60 * 1000), false);
});

// ── roundCoords: parity with orchestrator_geo_coords.rebuild_coords ────────
test('roundCoords: fine → 5 dp, byte-for-byte with the Python output', () => {
  const fix = g.roundCoords({
    latitude: 52.2296756, longitude: 21.0122287, accuracy: 18.44,
    altitude: 110.53, heading: 12.34, speed: 1.25,
  }, 'fine');
  assert.deepEqual(fix, {
    lat: 52.22968, lon: 21.01223, accuracy_m: 18.4,
    altitude_m: 110.53, heading: 12.34, speed_mps: 1.25, precision: 'fine',
  });
});

test('roundCoords: coarse → 2 dp AND the 1000 m accuracy floor', () => {
  const fix = g.roundCoords({ latitude: 52.2296756, longitude: 21.0122287, accuracy: 18.44 }, 'coarse');
  assert.deepEqual(fix, {
    lat: 52.23, lon: 21.01, accuracy_m: 1000,
    altitude_m: null, heading: null, speed_mps: null, precision: 'coarse',
  });
  // A genuinely worse reading is NOT improved by the floor.
  const wide = g.roundCoords({ latitude: -33.8688197, longitude: 151.2092955, accuracy: 2500 }, 'coarse');
  assert.equal(wide.accuracy_m, 2500);
  assert.equal(wide.lat, -33.87);
  assert.equal(wide.lon, 151.21);
});

test('roundCoords: southern/eastern hemisphere parity', () => {
  const fix = g.roundCoords({ latitude: -33.8688197, longitude: 151.2092955, accuracy: 5 }, 'fine');
  assert.equal(fix.lat, -33.86882);
  assert.equal(fix.lon, 151.2093);
  assert.equal(fix.accuracy_m, 5);
});

test('roundCoords: accepts the server-side key spelling too', () => {
  // The /pending replay and our own cached fixes speak lat/lon/accuracy_m.
  const fix = g.roundCoords({ lat: 52.2296756, lon: 21.0122287, accuracy_m: 18.44, altitude_m: 110.53 }, 'fine');
  assert.equal(fix.lat, 52.22968);
  assert.equal(fix.lon, 21.01223);
  assert.equal(fix.altitude_m, 110.53);
});

test('roundCoords: an unusable reading is null, never a fix at (0, 0)', () => {
  const base = { latitude: 52.2, longitude: 21.0, accuracy: 10 };
  const bad = [
    null, undefined, 'x', 42,
    {},
    Object.assign({}, base, { latitude: 91 }),
    Object.assign({}, base, { latitude: -91 }),
    Object.assign({}, base, { longitude: 181 }),
    Object.assign({}, base, { longitude: -181 }),
    Object.assign({}, base, { latitude: NaN }),
    Object.assign({}, base, { longitude: Infinity }),
    // A string coordinate must not be coerced — same rule as Python's finite().
    Object.assign({}, base, { latitude: '52.2' }),
    Object.assign({}, base, { longitude: '21.0' }),
    // Nor a bool: Number(true) === 1 would become a fix off Africa.
    Object.assign({}, base, { latitude: true }),
    Object.assign({}, base, { accuracy: 0 }),
    Object.assign({}, base, { accuracy: -5 }),
    Object.assign({}, base, { accuracy: NaN }),
    { latitude: 52.2, longitude: 21.0 },
  ];
  for (const raw of bad) {
    assert.equal(g.roundCoords(raw, 'fine'), null, `expected null for ${JSON.stringify(raw)}`);
  }
});

test('roundCoords: garbage extras become null instead of costing a good fix', () => {
  const fix = g.roundCoords({
    latitude: 52.2, longitude: 21.0, accuracy: 10,
    altitude: -20000, heading: 400, speed: -3,
  }, 'fine');
  assert.equal(fix.lat, 52.2);
  assert.equal(fix.altitude_m, null);
  assert.equal(fix.heading, null);
  assert.equal(fix.speed_mps, null);
});

test('roundCoords: extras at the physical bounds are kept', () => {
  const fix = g.roundCoords({
    latitude: 52.2, longitude: 21.0, accuracy: 10,
    altitude: -12000, heading: 0, speed: 0,
  }, 'fine');
  assert.equal(fix.altitude_m, -12000);
  assert.equal(fix.heading, 0);
  assert.equal(fix.speed_mps, 0);
});

test('roundCoords: an unknown precision degrades to fine (never full precision)', () => {
  for (const precision of [undefined, null, '', 'exact', 'COARSE']) {
    const fix = g.roundCoords({ latitude: 52.2296756, longitude: 21.0122287, accuracy: 18.44 }, precision);
    assert.equal(fix.precision, 'fine', `precision ${JSON.stringify(precision)}`);
    assert.equal(fix.lat, 52.22968);
  }
  assert.equal(g.normalizeAccuracy('coarse'), 'coarse');
  assert.equal(g.normalizeAccuracy('nonsense'), 'fine');
});

// ── positionOptions ────────────────────────────────────────────────────────
test('positionOptions: maximumAge mirrors --max-age in milliseconds', () => {
  assert.equal(g.positionOptions({ max_age_s: 300 }).maximumAge, 300000);
  assert.equal(g.positionOptions({ max_age_s: 1 }).maximumAge, 1000);
  assert.equal(g.positionOptions({ max_age_s: 1.5 }).maximumAge, 1500);
  assert.equal(g.positionOptions({ max_age_s: '300' }).maximumAge, 300000);
});

test('positionOptions: "--max-age 0" forces a genuinely fresh reading', () => {
  for (const maxAge of [0, -1, undefined, null, NaN, 'soon', {}]) {
    assert.equal(g.positionOptions({ max_age_s: maxAge }).maximumAge, 0, `max_age_s ${JSON.stringify(maxAge)}`);
  }
  assert.equal(g.positionOptions().maximumAge, 0);
});

test('positionOptions: enableHighAccuracy tracks the accuracy the agent asked for', () => {
  assert.equal(g.positionOptions({ accuracy: 'fine' }).enableHighAccuracy, true);
  assert.equal(g.positionOptions({ accuracy: 'coarse' }).enableHighAccuracy, false);
  assert.equal(g.positionOptions({ accuracy: 'nonsense' }).enableHighAccuracy, true);
  assert.equal(g.positionOptions({}).timeout, 20000);
});

// ── answerPayload ──────────────────────────────────────────────────────────
test('answerPayload: coords ride along with "fixed" and with nothing else', () => {
  const coords = { lat: 52.22968, lon: 21.01223, accuracy_m: 18.4, precision: 'fine' };
  const fixed = g.answerPayload({ status: 'fixed', coords: coords, ts_ms: 1785938591000, sessionId: 's1' });
  assert.deepEqual(fixed, {
    status: 'fixed', session_id: 's1', reason: 'browser_fix',
    coords: coords, ts_ms: 1785938591000,
  });
  const denied = g.answerPayload({ status: 'denied', coords: coords, sessionId: 's1' });
  assert.equal(denied.coords, undefined);
  assert.equal(denied.ts_ms, undefined);
  assert.equal(denied.reason, 'permission_denied');
});

test('answerPayload: only ever emits a status the server accepts', () => {
  for (const status of ['nonsense', 'cached', 'expired', undefined, 42]) {
    const body = g.answerPayload({ status: status });
    assert.ok(g.ANSWER_STATUSES.includes(body.status), `leaked status ${body.status}`);
    assert.equal(body.status, 'unavailable');
  }
  for (const status of g.ANSWER_STATUSES) {
    assert.equal(g.answerPayload({ status: status }).status, status);
  }
});

test('answerPayload: an explicit reason overrides the table but stays capped', () => {
  const body = g.answerPayload({ status: 'denied', reason: 'x'.repeat(500) });
  assert.equal(body.reason.length, g.REASON_MAX_CHARS);
  assert.equal(g.answerPayload({ status: 'denied', reason: 'device_opt_out' }).reason, 'device_opt_out');
});

// ── normalizeRequest ───────────────────────────────────────────────────────
test('normalizeRequest: rejects anything without a well-formed request_id', () => {
  for (const raw of [null, undefined, 'x', 42, {}, { request_id: '' }, { request_id: 'short' },
                     { request_id: 'a'.repeat(65) }, { request_id: 'has spaces here!!' },
                     { request_id: 'a/b'.padEnd(20, 'c') }, { request_id: 123456789012345678 }]) {
    assert.equal(g.normalizeRequest(raw), null, `expected null for ${JSON.stringify(raw)}`);
  }
});

test('normalizeRequest: rebuilds an SSE frame field by field', () => {
  const req = g.normalizeRequest({
    request_id: RID, reason: 'x'.repeat(500), accuracy: 'nonsense',
    max_age_s: '300.4', expires_at_ms: -1, agent: { title: 'Dom', lib_id: 'areas/dom', evil: 1 },
    extra: 'dropped',
  });
  assert.deepEqual(Object.keys(req).sort(), ['accuracy', 'agent', 'expires_at_ms', 'max_age_s', 'reason', 'request_id']);
  assert.equal(req.reason.length, g.REASON_MAX_CHARS);
  assert.equal(req.accuracy, 'fine');
  assert.equal(req.max_age_s, 300);
  assert.equal(req.expires_at_ms, 0);
  assert.deepEqual(req.agent, { lib_id: 'areas/dom', title: 'Dom' });
});

test('agentLabel: never renders an empty attribution in the toast', () => {
  assert.equal(g.agentLabel({ agent: { title: 'Dom', lib_id: 'areas/dom' } }), 'Dom');
  assert.equal(g.agentLabel({ agent: { title: '', lib_id: 'areas/dom' } }), 'areas/dom');
  assert.equal(g.agentLabel({ agent: {} }), 'Agent');
  assert.equal(g.agentLabel(null), 'Agent');
});

// ── formatting ─────────────────────────────────────────────────────────────
test('formatAge / formatAccuracy: human units, empty string for nonsense', () => {
  assert.equal(g.formatAge(45), '45 s');
  assert.equal(g.formatAge(90), '2 min');
  assert.equal(g.formatAge(7200), '2 h');
  assert.equal(g.formatAccuracy(18.4), '~18 m');
  assert.equal(g.formatAccuracy(2500), '~2.5 km');
  for (const bad of [-1, NaN, 'x', undefined]) {
    assert.equal(g.formatAge(bad), '', `age ${JSON.stringify(bad)}`);
    assert.equal(g.formatAccuracy(bad), '', `accuracy ${JSON.stringify(bad)}`);
  }
  assert.equal(g.formatAge(0), '0 s');
  assert.equal(g.formatAccuracy(0), '');
  // `Number(null) === 0`, so an absent age formats as "0 s" while an absent
  // accuracy formats as "" (0 m is not a plausible reading, 0 s is). Callers
  // pass `age_s` only when the server sent one, so this never reaches the UI —
  // pinned so a refactor of _num() cannot start claiming a fresh fix.
  assert.equal(g.formatAge(null), '0 s');
  assert.equal(g.formatAccuracy(null), '');
});
