// orchestrator-read-aloud.jsx — per-session passive TTS channel.
//
// Backend `orchestrator_read_aloud.py` tails the session JSONL and emits a
// 'speak' frame on a dedicated SSE stream (NOT the shared /events channel) when
// each assistant turn completes. This is the ONLY auto-speak path for turns the
// user types straight into the ttyd terminal (no dashboard runner → no
// assistant_message SSE → the composer auto-speak effect never fires).
//
// The connection is opened ONLY when enabled (server flag on + this device's
// voiceMode !== 'manual' + terminal view + a session is active); its mere
// existence arms the backend watcher (ref-counted), and closing it disarms.
//
// Reconnect is RESILIENT: EventSource.onerror can't read the HTTP status, so a
// transient outage (deploy restart, laptop sleep, Tailscale blip) looks the
// same as a flag-off 404. We therefore reconnect FOREVER with capped backoff
// (auto-recovering when the server returns), notifying the user ONCE via
// onGiveUp after a sustained outage. What stops retries for good is the
// `enabled` gate: when the flag is toggled off or the mode goes manual, the
// effect tears the connection down.
//
// De-dup: the local `seen` Set keys off e.lastEventId (the hub's monotonic
// per-session seq) to drop a replayed frame; the cross-path turn de-dup (vs the
// composer auto-speak path) is handled downstream in orchestrator.jsx by
// spokenKeysRef + the speakBlocks same-text window.

const { useEffect: _raUseEffect, useRef: _raUseRef } = React;

// Notify the user once after this many consecutive reconnect failures (but keep
// retrying — recovery is automatic when the endpoint comes back).
const _RA_SOFT_LIMIT = 8;

function useReadAloud({ sessionId, enabled, onSpeak, onGiveUp }) {
  // Keep latest callbacks without re-opening the EventSource on every render.
  const cbRef = _raUseRef({ onSpeak, onGiveUp });
  cbRef.current = { onSpeak, onGiveUp };

  _raUseEffect(() => {
    if (!enabled || !sessionId) return undefined;
    let closed = false;
    let es = null;
    let reopenTimer = null;
    let fails = 0;
    let notified = false;
    const seen = new Set();

    const onSpeakEvt = (e) => {
      // A real 'speak' frame proves the channel works → reset the failure
      // budget. (Deliberately NOT reset on onopen: a 200 that immediately EOFs
      // would otherwise zero the budget and produce a tight reconnect loop.)
      fails = 0;
      notified = false;
      if (e.lastEventId) {
        if (seen.has(e.lastEventId)) return;  // drop a replayed frame
        seen.add(e.lastEventId);
      }
      let data = null;
      try { data = JSON.parse(e.data); } catch (_e) { return; }
      const cb = cbRef.current.onSpeak;
      if (cb && data && data.text != null && data.key != null) {
        cb({ text: data.text, key: data.key, end_flow: data.end_flow });
      }
    };

    const scheduleRetry = () => {
      if (closed) return;
      fails += 1;
      // Notify ONCE on a sustained outage, then keep reconnecting forever so a
      // deploy restart / sleep / network blip auto-recovers.
      if (fails === _RA_SOFT_LIMIT && !notified) {
        notified = true;
        const gu = cbRef.current.onGiveUp;
        if (gu) { try { gu(); } catch (_e) { /* ignore */ } }
      }
      const delay = Math.min(1000 * Math.pow(2, Math.min(fails - 1, 4)), 15000);
      reopenTimer = setTimeout(open, delay);
    };

    function open() {
      if (closed) return;
      let src;
      try {
        src = new EventSource(window.apiUrl(
          '/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/read-aloud'
        ));
      } catch (_e) {
        scheduleRetry();  // ctor failure (bad URL) → bounded retry, not silent death
        return;
      }
      es = src;
      es.addEventListener('speak', onSpeakEvt);
      es.onerror = () => {
        try { es.close(); } catch (_e) { /* ignore */ }
        if (closed) return;
        scheduleRetry();
      };
    }

    open();
    return () => {
      closed = true;
      if (reopenTimer) clearTimeout(reopenTimer);
      if (es) { try { es.close(); } catch (_e) { /* ignore */ } }
    };
  }, [sessionId, enabled]);
}

Object.assign(window, { useReadAloud });
