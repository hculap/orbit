// orchestrator-geolocation.jsx — the BROWSER half of the geolocation round trip.
//
// An agent asks the server for a fix; the server pushes a `geo_request` down
// the EXISTING per-session SSE hub (the same channel artifact toasts ride) and
// blocks on /wait. This hook is what answers. It owns no EventSource of its
// own: orchestrator.jsx feeds it the two events from usePersistentEvents
// (orchestrator-artifacts.jsx), so the ask arrives in terminal mode, behind
// display:none, and across section switches — everywhere that channel reaches.
//
// Every DECISION lives in geo-consent.js (pure, node-testable, mirrors the
// Python STATUSES table); this file is glue: queue → consent → navigator →
// POST answer. Consent modes (per-device, `geoConsentMode`):
//   • 'auto' (default) — no in-app modal at all. The BROWSER's own permission
//     sheet is the only blocking gate, and it remembers its answer per origin.
//     Honesty is then non-negotiable: every fix taken raises a non-blocking
//     toast naming the agent, with a one-tap "Wyłącz".
//   • 'ask' — the ConfirmModal path, with a 15-minute snooze. Kept fully
//     working: it is a real user choice AND the L1.5 rollback for 'auto'.
// `geoOptOut` outranks both, and no-API / insecure-origin are answered before
// any permission state is consulted (see HubGeo.decideConsent).
//
// Cross-file deps via window globals (no bundler): HubGeo (geo-consent.js,
// plain script), ConfirmModal (ds-overlays), useToast/apiUrl (components),
// useSettings (settings-primitives). All load before this file.

const {
  useState: _geoUseState,
  useEffect: _geoUseEffect,
  useRef: _geoUseRef,
  useCallback: _geoUseCallback,
} = React;

const _GEO = window.HubGeo;

// Requests already answered/cancelled by THIS tab. A /pending re-poll right
// after an answer can still list an ask the server hasn't reconciled yet; this
// stops it re-prompting. Bounded — see the prune in `remember`.
const _GEO_HANDLED_MAX = 128;

function _geoApiUrl(path) {
  return window.apiUrl('/api/orchestrator/geo' + path);
}

// Turning the device kill switch on has to reach the SERVER too: `geoOptOut`
// is localStorage, but the server's in-memory fix answers the next agent
// straight from cache for up to 15 minutes — with no browser involved, no
// consent check and no toast. "Wyłącz" that leaves the coordinates readable is
// not a kill switch. Fire-and-forget: the route is un-gated and idempotent.
function _geoOptOutNow(updateSettings) {
  updateSettings({ geoOptOut: true });
  fetch(_geoApiUrl('/forget'), { method: 'POST' })
    .catch(() => { /* offline: the fix dies with the process anyway */ });
}

// getCurrentPosition as a promise that NEVER rejects: every failure comes back
// as a typed status the server understands. A callback API that throws
// synchronously (some embedded webviews) must not leave the agent hanging for
// the full 180 s TTL either, hence the try/catch around the call itself.
function _geoCurrentPosition(options) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) { resolve({ ok: false, status: 'no_api' }); return; }
    let settled = false;
    const done = (value) => { if (!settled) { settled = true; resolve(value); } };
    try {
      navigator.geolocation.getCurrentPosition(
        (position) => done({ ok: true, position }),
        (err) => done({ ok: false, status: _GEO.errorToStatus(err && err.code) }),
        options,
      );
    } catch (e) {
      done({ ok: false, status: 'unavailable' });
    }
  });
}

// Live permission state ('granted' | 'prompt' | 'denied'), or null where the
// Permissions API doesn't cover geolocation (Safari). Null is NOT "denied" —
// decideConsent only treats an explicit 'denied' as a snooze-breaker.
// Published on window because the Settings device group shows the same line.
function useGeoPermission() {
  const [state, setState] = _geoUseState(null);
  _geoUseEffect(() => {
    if (!navigator.permissions || !navigator.permissions.query) return undefined;
    let status = null;
    let cancelled = false;
    const onChange = () => { if (!cancelled && status) setState(status.state); };
    navigator.permissions.query({ name: 'geolocation' }).then((result) => {
      if (cancelled) return;
      status = result;
      setState(result.state);
      result.addEventListener('change', onChange);
    }).catch(() => { /* unsupported name → stay null, the ladder copes */ });
    return () => {
      cancelled = true;
      if (status) { try { status.removeEventListener('change', onChange); } catch (e) { /* ignore */ } }
    };
  }, []);
  return state;
}

// ─────────────────────────────────────────────────────────────
// Consent modal ('ask' mode only). The TITLE is fixed and never
// agent-controlled; the agent's `reason` renders as a React TEXT child, never
// as HTML.
// ─────────────────────────────────────────────────────────────
function _GeoConsentModal({ request, busy, onConfirm, onDismiss }) {
  const [snooze, setSnooze] = _geoUseState(false);
  const ConfirmModal = window.ConfirmModal;
  // Reset the checkbox per request so a snooze can never carry over silently.
  _geoUseEffect(() => { setSnooze(false); }, [request && request.request_id]);
  if (!ConfirmModal || !request) return null;
  const label = _GEO.agentLabel(request);
  const freshness = request.max_age_s > 0
    ? 'Zaakceptuje odczyt do ' + _GEO.formatAge(request.max_age_s) + ' wstecz.'
    : 'Wymaga świeżego pomiaru.';
  return (
    <ConfirmModal
      open
      onClose={onDismiss}
      title="Agent prosi o Twoją lokalizację"
      message={
        <>
          <strong style={{ color: 'var(--fg)' }}>{label}</strong> prosi o lokalizację tego urządzenia.
          {request.reason ? <> Powód: <em>{request.reason}</em></> : null}
        </>
      }
      confirmLabel="Udostępnij"
      cancelLabel="Odmów"
      onConfirm={() => onConfirm({ snooze })}
      busy={busy}
    >
      <label style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: 'pointer' }}>
        <input
          type="checkbox"
          checked={snooze}
          onChange={(e) => setSnooze(e.target.checked)}
          style={{ width: 18, height: 18, marginTop: 1, flexShrink: 0, accentColor: 'var(--accent)' }}
        />
        <span style={{ minWidth: 0 }}>
          <span style={{ fontSize: 'var(--t-md)', color: 'var(--fg)' }}>
            Nie pytaj ponownie przez {_GEO.AUTO_GRANT_MINUTES} minut
          </span>
          <span className="mono" style={{
            display: 'block', fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
            marginTop: 2, lineHeight: 1.45,
          }}>
            Dotyczy tylko tego urządzenia. {freshness}
          </span>
        </span>
      </label>
    </ConfirmModal>
  );
}

// ─────────────────────────────────────────────────────────────
// useGeolocationRequests — one instance, mounted by OrchestratorView.
// Returns { prompt, modal, handleRequest, handleCancelled, busy }.
// `enabled:false` (server flag off) makes every path inert: nothing is
// queued, so no modal and no navigator call can ever happen — that is the
// documented level-1 rollback.
// ─────────────────────────────────────────────────────────────
function useGeolocationRequests({ sessionId, enabled }) {
  const [queue, setQueue] = _geoUseState([]);
  const [prompt, setPrompt] = _geoUseState(null);
  const [busy, setBusy] = _geoUseState(false);
  const [settings, updateSettings] = useSettings();
  const permission = useGeoPermission();
  const toast = useToast();
  const handledRef = _geoUseRef(null);
  if (handledRef.current === null) handledRef.current = new Set();
  const runningRef = _geoUseRef(false);
  // Read inside async callbacks that outlive the render they started in.
  const sessionRef = _geoUseRef(sessionId);
  sessionRef.current = sessionId;

  const remember = _geoUseCallback((id) => {
    // Bounded: dropping the whole set is safe because a re-listed request is
    // either already expired (dropped below) or genuinely still outstanding.
    if (handledRef.current.size >= _GEO_HANDLED_MAX) handledRef.current.clear();
    handledRef.current.add(id);
  }, []);

  const finish = _geoUseCallback((id) => {
    remember(id);
    runningRef.current = false;
    setQueue((prev) => prev.filter((q) => q.request_id !== id));
  }, [remember]);

  const handleRequest = _geoUseCallback((raw) => {
    // Flag off ⇒ nothing is even QUEUED (level-1 rollback): a request parked in
    // the queue would otherwise fire the moment the flag came back, long after
    // the agent gave up. Dead-code defence: geo-consent.js is a plain <script>
    // loaded before this file, so _GEO is always there — but nothing may enter
    // the queue without the validator, or the pump would drive on unvalidated
    // server JSON.
    if (!enabled) return;
    const req = _GEO && _GEO.normalizeRequest(raw);
    if (!req || handledRef.current.has(req.request_id)) return;
    setQueue((prev) => (prev.some((q) => q.request_id === req.request_id) ? prev : prev.concat([req])));
  }, [enabled]);

  // The agent withdrew the ask (another device answered / TTL / SIGINT). Drop
  // it silently — answering a cancelled request would only ever lose a race.
  const handleCancelled = _geoUseCallback((raw) => {
    const id = raw && typeof raw.request_id === 'string' ? raw.request_id : '';
    if (!_GEO || !_GEO.REQUEST_ID_RE.test(id)) return;
    remember(id);
    runningRef.current = false;
    setQueue((prev) => prev.filter((q) => q.request_id !== id));
    setPrompt((prev) => (prev && prev.request_id === id ? null : prev));
  }, [remember]);

  // POST the answer. A failed POST is not retried: the server resolves the ask
  // as `expired` on its own TTL, which is a TYPED outcome the agent handles —
  // retrying here would race that and could double-answer.
  const answer = _geoUseCallback(async (req, status, extra) => {
    const body = _GEO.answerPayload({
      status,
      sessionId: sessionRef.current,
      coords: extra && extra.coords,
      ts_ms: extra && extra.ts_ms,
    });
    try {
      await fetch(_geoApiUrl('/requests/' + encodeURIComponent(req.request_id) + '/answer'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (e) { /* offline → the server expires the ask; nothing to retry into */ }
    finish(req.request_id);
  }, [finish]);

  // The honesty toast (mandatory in 'auto' mode, shown in both). Non-blocking,
  // never steals focus; the "Wyłącz" link flips the per-device kill switch.
  const notifyTaken = _geoUseCallback((req, coords) => {
    if (!toast) return;
    const accuracy = _GEO.formatAccuracy(coords && coords.accuracy_m);
    toast(
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span>📍 Agent pobrał Twoją lokalizację</span>
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
          {_GEO.agentLabel(req)}{accuracy ? ' · ' + accuracy : ''}
        </span>
        <a
          role="button" tabIndex={0}
          onClick={() => _geoOptOutNow(updateSettings)}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') _geoOptOutNow(updateSettings); }}
          style={{ color: 'var(--accent)', cursor: 'pointer' }}>
          Wyłącz
        </a>
      </span>,
      'ok',
    );
  }, [toast, updateSettings]);

  const takeFix = _geoUseCallback(async (req) => {
    setBusy(true);
    try {
      const result = await _geoCurrentPosition(_GEO.positionOptions(req));
      if (!result.ok) { await answer(req, result.status); return; }
      const coords = _GEO.roundCoords(result.position && result.position.coords, req.accuracy);
      // A position object the allowlist can't rebuild is a broken sensor, not
      // a location — report it as such instead of shipping a garbage fix.
      if (!coords) { await answer(req, 'unavailable'); return; }
      await answer(req, 'fixed', { coords, ts_ms: result.position.timestamp });
      notifyTaken(req, coords);
    } finally {
      setBusy(false);
    }
  }, [answer, notifyTaken]);

  // ── the pump: one request at a time, head of the queue ──
  _geoUseEffect(() => {
    if (!enabled || prompt || busy || runningRef.current || !queue.length) return;
    const head = queue[0];
    runningRef.current = true;
    // Already dead on arrival (reconnect poll racing the TTL): drop it. The
    // server has resolved it as `expired`; an answer now would 404 anyway.
    if (head.expires_at_ms && Date.now() >= head.expires_at_ms) { finish(head.request_id); return; }
    const decision = _GEO.decideConsent({
      hasApi: !!(navigator && navigator.geolocation),
      secure: window.isSecureContext !== false,
      optOut: settings.geoOptOut === true,
      mode: settings.geoConsentMode,
      autoGrantUntil: _GEO.readAutoGrant(),
      permission,
      now: Date.now(),
    });
    if (decision.action === 'prompt') { runningRef.current = false; setPrompt(head); return; }
    if (decision.action === 'answer') {
      if (decision.status === 'insecure_context' && toast) {
        toast('Geolokalizacja wymaga https://your-dashboard.example/', 'err');
      }
      answer(head, decision.status);
      return;
    }
    takeFix(head);
  }, [enabled, prompt, busy, queue, settings.geoOptOut, settings.geoConsentMode, permission, answer, takeFix, finish, toast]);

  // ── reconnect guarantee ──
  // A hand-built EventSource sends no Last-Event-ID, so a two-second blip
  // silently drops the geo_request frame. Poll the authoritative list on
  // (re)connect, on tab-visible, and once on mount.
  _geoUseEffect(() => {
    if (!enabled || !sessionId) return undefined;
    let cancelled = false;
    const poll = () => {
      fetch(_geoApiUrl('/requests/pending?session_id=' + encodeURIComponent(sessionId)))
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (cancelled || !data || !Array.isArray(data.requests)) return;
          data.requests.forEach(handleRequest);
        })
        .catch(() => { /* offline; the next reconnect/visibility tick retries */ });
    };
    const onReconnect = (e) => {
      if (!e || !e.detail || e.detail.sessionId === sessionId) poll();
    };
    const onVisible = () => { if (document.visibilityState === 'visible') poll(); };
    window.addEventListener('hub:geo-reconnect', onReconnect);
    document.addEventListener('visibilitychange', onVisible);
    poll();
    return () => {
      cancelled = true;
      window.removeEventListener('hub:geo-reconnect', onReconnect);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [enabled, sessionId, handleRequest]);

  // Switching sessions abandons the previous session's asks: they were pushed
  // to a channel we no longer watch, and the server expires them. Flipping the
  // flag off drops them for the same reason — the kill switch must empty the
  // queue, not merely pause it.
  _geoUseEffect(() => {
    runningRef.current = false;
    setQueue([]);
    setPrompt(null);
  }, [sessionId, enabled]);

  const onConfirmPrompt = _geoUseCallback(async (opts) => {
    if (!prompt) return;
    const req = prompt;
    runningRef.current = true;   // claim the slot BEFORE the pump can see prompt:null
    setPrompt(null);
    if (opts && opts.snooze) _GEO.setAutoGrant(_GEO.AUTO_GRANT_MINUTES);
    await takeFix(req);
  }, [prompt, takeFix]);

  // Cancel / backdrop = `dismissed`, deliberately distinct from `denied`: the
  // agent must not report a permission refusal that never happened.
  const onDismissPrompt = _geoUseCallback(async () => {
    if (!prompt) return;
    const req = prompt;
    runningRef.current = true;
    setPrompt(null);
    await answer(req, 'dismissed');
  }, [prompt, answer]);

  const modal = (enabled && prompt)
    ? <_GeoConsentModal request={prompt} busy={busy} onConfirm={onConfirmPrompt} onDismiss={onDismissPrompt} />
    : null;

  return { prompt, modal, handleRequest, handleCancelled, busy };
}

Object.assign(window, { useGeolocationRequests, useGeoPermission });
