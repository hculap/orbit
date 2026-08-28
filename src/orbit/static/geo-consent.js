'use strict';
// Pure consent + payload logic for the server-initiated geolocation round trip.
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). The React
// half (orchestrator-geolocation.jsx) is thin glue: it owns the EventSource
// callbacks, the modal and the fetch — every DECISION is made here.
//
// `window` / `localStorage` are touched ONLY inside guarded helpers (never at
// module load), so require() under Node never trips on a missing global.
//
// STATUSES is a VERBATIM copy of orchestrator_geo.STATUSES — the Python table
// is the single source of truth for the agent-facing contract and a drift test
// diffs the two literals from both sides. Do not "improve" the copy here; fix
// the Python row and re-copy, or the agent and the browser start disagreeing
// about what `retryable` means.
//
// The ordering of decideConsent() is itself a safety property: the two answers
// the browser CANNOT act on (no API, insecure origin) are settled before any
// permission state is consulted, and the per-device kill switch outranks both
// consent modes — so no configuration can produce a prompt on a page that
// physically cannot take a fix.
(function () {
  // ── the contract table (mirror of orchestrator_geo.STATUSES) ──────
  const STATUSES = {
    // — success —
    fixed: {
      reason: 'browser_fix',
      retryable: false,
      message: 'Pobrano lokalizację z przeglądarki użytkownika.',
      hint: '',
    },
    cached: {
      reason: 'cached_fix',
      retryable: false,
      message: 'Zwrócono ostatnio znaną lokalizację z pamięci serwera.',
      hint: 'Podaj wiek odczytu (age_s). --max-age 0 wymusza świeży pomiar.',
    },
    // — in flight (never terminal) —
    pending: {
      reason: 'awaiting_browser',
      retryable: true,
      message: 'Prośba trafiła do przeglądarki — czekam na odpowiedź.',
      hint: 'Odpytuj /wait tym samym request_id aż do odpowiedzi terminalnej.',
    },
    // — refusals (never retry in the same turn) —
    denied: {
      reason: 'permission_denied',
      retryable: false,
      message: 'Użytkownik odrzucił prośbę o lokalizację.',
      hint: 'Nie pytaj ponownie w tej turze. Poproś o lokalizację tekstem.',
    },
    device_opt_out: {
      reason: 'device_opt_out',
      retryable: false,
      message: 'Na tym urządzeniu geolokalizacja jest wyłączona w ustawieniach.',
      hint: 'Nie ponawiaj. Powiedz, że można to włączyć w Ustawienia → Serwer.',
    },
    dismissed: {
      reason: 'user_dismissed',
      retryable: false,
      message: 'Użytkownik zamknął prośbę bez decyzji.',
      hint: 'Nie ponawiaj automatycznie — zapytaj w czacie, czy podać lokalizację.',
    },
    insecure_context: {
      reason: 'insecure_context',
      retryable: false,
      message: 'Przeglądarka działa po http:// — API geolokalizacji jest zablokowane.',
      hint: 'Poproś użytkownika, żeby otworzył https://your-dashboard.example/.',
    },
    no_api: {
      reason: 'no_geolocation_api',
      retryable: false,
      message: 'Przeglądarka nie udostępnia API geolokalizacji.',
      hint: 'Nie ponawiaj — poproś o lokalizację tekstem.',
    },
    // — transient device/environment failures (ONE more try is reasonable) —
    unavailable: {
      reason: 'position_unavailable',
      retryable: true,
      message: 'Urządzenie nie ustaliło pozycji (brak GPS/WiFi).',
      hint: 'Spróbuj RAZ z --accuracy coarse.',
    },
    timeout: {
      reason: 'browser_timeout',
      retryable: true,
      message: 'Przeglądarka nie zdążyła ustalić pozycji.',
      hint: 'Jedna próba z --accuracy coarse --timeout 30.',
    },
    // — server-side outcomes —
    expired: {
      reason: 'request_expired',
      retryable: false,
      message: 'Nikt nie odpowiedział na prośbę o lokalizację.',
      hint: 'Użytkownik nie zareagował — zapytaj o lokalizację w czacie.',
    },
    aborted: {
      reason: 'request_aborted',
      retryable: false,
      message: 'Prośba o lokalizację została anulowana.',
      hint: 'Ponów tylko jeśli użytkownik znów o to poprosi.',
    },
    no_browser: {
      reason: 'no_browser_attached',
      retryable: false,
      message: 'Żadna przeglądarka nie jest podłączona do dashboardu.',
      hint: 'Otwórz https://your-dashboard.example/ i poproś ponownie, albo poproś użytkownika o lokalizację tekstem.',
    },
    not_found: {
      reason: 'unknown_request',
      retryable: false,
      message: 'Taka prośba o lokalizację nie istnieje (wygasła lub anulowana).',
      hint: 'Zacznij od nowa — utwórz świeżą prośbę.',
    },
    rate_limited: {
      reason: 'too_many_requests',
      retryable: true,
      message: 'Za dużo próśb o lokalizację naraz.',
      hint: 'Odczekaj retry_after_s sekund i spróbuj raz jeszcze.',
    },
    disabled: {
      reason: 'feature_disabled',
      retryable: false,
      message: 'Geolokalizacja jest wyłączona na serwerze.',
      hint: 'Włącz w Ustawienia → Serwer.',
    },
    // — geocoding (same table on purpose; the browser never answers these) —
    geocode_disabled: {
      reason: 'geocode_disabled',
      retryable: false,
      message: 'Reverse-geocoding jest wyłączony na serwerze.',
      hint: 'Podaj surowe współrzędne albo włącz w Ustawienia → Serwer.',
    },
    geocode_failed: {
      reason: 'upstream_error',
      retryable: true,
      message: 'Serwis OSM nie odpowiedział poprawnie.',
      hint: 'Spróbuj raz później albo podaj surowe współrzędne.',
    },
  };

  // Statuses a BROWSER may answer with (mirror of geo_mod.ANSWER_STATUSES).
  const ANSWER_STATUSES = [
    'fixed', 'denied', 'device_opt_out', 'dismissed',
    'unavailable', 'no_api', 'insecure_context', 'timeout',
  ];

  // GeolocationPositionError.code → our status. Mirrored in Python
  // (orchestrator_geo.ERROR_CODE_STATUS) and drift-guarded from both sides.
  const ERROR_CODE_STATUS = { 1: 'denied', 2: 'unavailable', 3: 'timeout' };

  // `secrets.token_urlsafe(16)` → 22 chars; the range matches the Python regex
  // byte for byte so a request id rejected there is rejected here too.
  const REQUEST_ID_RE = /^[A-Za-z0-9_-]{16,64}$/;

  const ACCURACIES = ['fine', 'coarse'];
  const CONSENT_MODES = ['auto', 'ask'];
  const REASON_MAX_CHARS = 200;
  // 5 dp ≈ 1 m, 2 dp ≈ 1 km — same constants as orchestrator_geo_coords.
  const PRECISION_DP = { fine: 5, coarse: 2 };
  const COARSE_ACCURACY_FLOOR_M = 1000;
  // The bottom rung of the timeout ladder: 20 s here < 45 s poll window <
  // 180 s request TTL. Raising it past the poll window would make a slow fix
  // land after the agent has already re-polled — harmless, but it would look
  // like a hang, so keep the gap.
  const POSITION_TIMEOUT_MS = 20000;
  // "Nie pytaj ponownie przez 15 minut" (the `ask`-mode snooze only).
  const AUTO_GRANT_KEY = 'hub-geo-auto-until';
  const AUTO_GRANT_MINUTES = 15;

  function describe(status) {
    const row = STATUSES[status] || STATUSES.not_found;
    return { reason: row.reason, retryable: row.retryable, message: row.message, hint: row.hint };
  }

  function retryable(status) {
    return !!describe(status).retryable;
  }

  function errorToStatus(code) {
    return ERROR_CODE_STATUS[code] || 'unavailable';
  }

  // ── consent ladder ────────────────────────────────────────────────
  //
  // Returns { action, status, reason } where action is one of:
  //   'answer' — settle the request right now with `status`, no UI, no
  //              navigator call (the environment or the user already decided)
  //   'grant'  — call getCurrentPosition immediately (mode 'auto', or a live
  //              snooze); the BROWSER's own permission sheet is the gate
  //   'prompt' — render the in-app ConfirmModal first (mode 'ask')
  //
  // `permission` is the navigator.permissions.query state when the browser has
  // the Permissions API and null when it hasn't — Safari does not expose
  // geolocation there, so a snooze must not require a literal 'granted' or it
  // would silently never apply on iOS. Only an explicit 'denied' cancels it.
  function decideConsent(opts) {
    const o = opts || {};
    if (!o.hasApi) return { action: 'answer', status: 'no_api', reason: STATUSES.no_api.reason };
    if (!o.secure) return { action: 'answer', status: 'insecure_context', reason: STATUSES.insecure_context.reason };
    if (o.optOut) return { action: 'answer', status: 'device_opt_out', reason: STATUSES.device_opt_out.reason };
    if (normalizeMode(o.mode) === 'auto') return { action: 'grant', status: null, reason: 'consent_auto' };
    if (autoGrantLive(o.autoGrantUntil, o.now) && o.permission !== 'denied') {
      return { action: 'grant', status: null, reason: 'consent_snoozed' };
    }
    return { action: 'prompt', status: null, reason: 'consent_ask' };
  }

  function normalizeMode(raw) {
    return CONSENT_MODES.indexOf(raw) !== -1 ? raw : 'auto';
  }

  function normalizeAccuracy(raw) {
    return ACCURACIES.indexOf(raw) !== -1 ? raw : 'fine';
  }

  function autoGrantLive(until, now) {
    const at = Number(until);
    if (!Number.isFinite(at) || at <= 0) return false;
    return at > (Number.isFinite(now) ? now : Date.now());
  }

  // ── the 15-minute snooze (localStorage; absent storage is not an error) ──
  function _storage() {
    try {
      return (typeof localStorage !== 'undefined' && localStorage) ? localStorage : null;
    } catch (e) { return null; }  // Safari private mode throws on access
  }

  function readAutoGrant() {
    const store = _storage();
    if (!store) return 0;
    try {
      const at = Number(store.getItem(AUTO_GRANT_KEY));
      return Number.isFinite(at) && at > 0 ? at : 0;
    } catch (e) { return 0; }
  }

  // Returns the new expiry (epoch ms) even when persisting failed, so the
  // caller's in-memory state stays correct in private-mode browsers.
  function setAutoGrant(minutes, now) {
    const mins = Number.isFinite(Number(minutes)) ? Number(minutes) : AUTO_GRANT_MINUTES;
    const at = (Number.isFinite(now) ? now : Date.now()) + mins * 60 * 1000;
    const store = _storage();
    if (store) { try { store.setItem(AUTO_GRANT_KEY, String(at)); } catch (e) { /* quota / private mode */ } }
    return at;
  }

  function clearAutoGrant() {
    const store = _storage();
    if (store) { try { store.removeItem(AUTO_GRANT_KEY); } catch (e) { /* ignore */ } }
  }

  // ── payload shaping ───────────────────────────────────────────────
  function _num(raw) {
    if (raw === null || raw === undefined || typeof raw === 'boolean' || typeof raw === 'string') return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  function _round(value, dp) {
    // Half-away-from-zero, unlike Python's half-to-even. A tie at 1e-5 degrees
    // from a real sensor is a fantasy, and the SERVER re-rounds authoritatively
    // anyway — this pass exists so a `coarse` fix never puts full precision on
    // the wire in the first place.
    return Number(value.toFixed(dp));
  }

  function _optional(raw, lo, hi) {
    const value = _num(raw);
    if (value === null || value < lo || value > hi) return null;
    return value;
  }

  // Rebuild a fix from a GeolocationCoordinates (or any coords-shaped object),
  // key by key, at the precision the agent asked for. Returns null when the
  // reading is unusable — a broken sensor must not become a fix at (0, 0).
  function roundCoords(raw, precision) {
    if (!raw || typeof raw !== 'object') return null;
    const lat = _num(raw.latitude !== undefined ? raw.latitude : raw.lat);
    const lon = _num(raw.longitude !== undefined ? raw.longitude : raw.lon);
    let accuracy = _num(raw.accuracy !== undefined ? raw.accuracy : raw.accuracy_m);
    if (lat === null || lat < -90 || lat > 90) return null;
    if (lon === null || lon < -180 || lon > 180) return null;
    if (accuracy === null || accuracy <= 0) return null;
    const mode = normalizeAccuracy(precision);
    const dp = PRECISION_DP[mode];
    if (mode === 'coarse') accuracy = Math.max(accuracy, COARSE_ACCURACY_FLOOR_M);
    return {
      lat: _round(lat, dp),
      lon: _round(lon, dp),
      accuracy_m: _round(accuracy, 1),
      // Physical sanity bounds, matching orchestrator_geo_coords: below the
      // Dead Sea / above the Kármán line, a bearing outside a circle or a
      // speed past escape velocity is a broken sensor, not a location.
      altitude_m: _optional(raw.altitude !== undefined ? raw.altitude : raw.altitude_m, -12000, 100000),
      heading: _optional(raw.heading, 0, 360),
      speed_mps: _optional(raw.speed !== undefined ? raw.speed : raw.speed_mps, 0, 10000),
      precision: mode,
    };
  }

  // getCurrentPosition options for one request. `maximumAge` mirrors the
  // agent's --max-age so the platform may answer from its own cache exactly as
  // far back as the agent said it would accept.
  function positionOptions(request) {
    const req = request || {};
    const maxAge = Number(req.max_age_s);
    return {
      enableHighAccuracy: normalizeAccuracy(req.accuracy) === 'fine',
      timeout: POSITION_TIMEOUT_MS,
      maximumAge: Number.isFinite(maxAge) && maxAge > 0 ? Math.round(maxAge * 1000) : 0,
    };
  }

  // The body of POST /geo/requests/{id}/answer. Only ever emits statuses the
  // server accepts; `coords` is present for 'fixed' and for nothing else.
  function answerPayload(opts) {
    const o = opts || {};
    const status = ANSWER_STATUSES.indexOf(o.status) !== -1 ? o.status : 'unavailable';
    const body = { status: status, session_id: o.sessionId || undefined };
    const row = STATUSES[status];
    if (o.reason) body.reason = String(o.reason).slice(0, REASON_MAX_CHARS);
    else if (row) body.reason = row.reason;
    if (status === 'fixed' && o.coords) {
      body.coords = o.coords;
      body.ts_ms = Number.isFinite(Number(o.ts_ms)) ? Number(o.ts_ms) : Date.now();
    }
    return body;
  }

  // An inbound geo_request frame (SSE push or the /pending reconnect poll) is
  // server-shaped but still parsed JSON — validate before it can drive a modal
  // or a navigator call. Returns null for anything unusable.
  function normalizeRequest(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const id = typeof raw.request_id === 'string' ? raw.request_id : '';
    if (!REQUEST_ID_RE.test(id)) return null;
    const maxAge = Number(raw.max_age_s);
    const expires = Number(raw.expires_at_ms);
    const agent = (raw.agent && typeof raw.agent === 'object') ? raw.agent : {};
    return {
      request_id: id,
      reason: typeof raw.reason === 'string' ? raw.reason.slice(0, REASON_MAX_CHARS) : '',
      accuracy: normalizeAccuracy(raw.accuracy),
      max_age_s: Number.isFinite(maxAge) && maxAge >= 0 ? Math.round(maxAge) : 0,
      expires_at_ms: Number.isFinite(expires) && expires > 0 ? expires : 0,
      agent: {
        lib_id: typeof agent.lib_id === 'string' ? agent.lib_id.slice(0, 120) : '',
        title: typeof agent.title === 'string' ? agent.title.slice(0, 120) : '',
      },
    };
  }

  // What to call the asking agent in UI copy. Never trusts it to be non-empty.
  function agentLabel(request) {
    const agent = (request && request.agent) || {};
    return agent.title || agent.lib_id || 'Agent';
  }

  // ── formatting ────────────────────────────────────────────────────
  function formatAge(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) return '';
    if (s < 60) return Math.round(s) + ' s';
    if (s < 3600) return Math.round(s / 60) + ' min';
    return Math.round(s / 360) / 10 + ' h';
  }

  function formatAccuracy(metres) {
    const m = Number(metres);
    if (!Number.isFinite(m) || m <= 0) return '';
    if (m < 1000) return '~' + Math.round(m) + ' m';
    return '~' + (Math.round(m / 100) / 10) + ' km';
  }

  const api = {
    STATUSES, ANSWER_STATUSES, ERROR_CODE_STATUS, REQUEST_ID_RE,
    ACCURACIES, CONSENT_MODES, PRECISION_DP, COARSE_ACCURACY_FLOOR_M,
    REASON_MAX_CHARS, POSITION_TIMEOUT_MS, AUTO_GRANT_KEY, AUTO_GRANT_MINUTES,
    describe, retryable, errorToStatus,
    decideConsent, normalizeMode, normalizeAccuracy, autoGrantLive,
    readAutoGrant, setAutoGrant, clearAutoGrant,
    roundCoords, positionOptions, answerPayload, normalizeRequest, agentLabel,
    formatAge, formatAccuracy,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubGeo = api;
})();
