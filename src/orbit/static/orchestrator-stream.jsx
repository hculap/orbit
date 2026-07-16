// Resilient EventSource wrapper for the orchestrator SSE feed.
// - Translates incoming events into reducer dispatches (dispatchSseEvent)
// - Silent retries with exp backoff on transient drops (1s, 2s, 4s, max 8s, up to 6)
// - Polling fallback (every 3s on /status + /messages) after retries exhaust
// - Terminal events (done/error) tear down everything cleanly
//
// All closures live on the caller's refs so navigation/session-switch tearDown
// is symmetric with closeStream() in OrchestratorView.

const { useCallback: _useCallback } = React;

function useResilientStream({ dispatch, esRef, pollerRef, closeStream, refreshSessions }) {
  const dispatchSseEvent = _useCallback((kind, raw) => {
    let p = null;
    try { p = JSON.parse(raw); } catch (e) { p = { text: raw }; }
    switch (kind) {
      case 'spawning': dispatch({ type: 'SPAWNING_EVENT', payload: p }); return null;
      case 'init': dispatch({ type: 'INIT_EVENT', payload: p }); return null;
      case 'delta': dispatch({ type: 'APPEND_DELTA', text: p.text || '' }); return null;
      case 'tool_start': dispatch({ type: 'APPEND_BLOCK', block: {
        kind: 'tool_use', tool_use_id: p.tool_use_id, name: p.name, input: p.input_partial || {}, running: true,
      }}); return null;
      case 'tool_use': dispatch({
        type: 'UPDATE_BLOCK',
        match: (b) => b && b.kind === 'tool_use' && b.tool_use_id === p.tool_use_id,
        patch: { input: p.input || {}, name: p.name || undefined, ms: p.ms, running: false },
      }); return null;
      case 'tool_result': dispatch({ type: 'APPEND_BLOCK', block: {
        kind: 'tool_result', tool_use_id: p.tool_use_id,
        stdout: p.stdout != null ? p.stdout : p.text, is_error: !!p.is_error, ms: p.ms,
      }}); return null;
      case 'assistant_message': dispatch({ type: 'FINALIZE_TURN', message: {
        role: 'assistant', ts: p.ts || Date.now() / 1000, turn_idx: p.turn_idx,
        blocks: Array.isArray(p.blocks) ? p.blocks : [],
      }}); return null;
      case 'error':
        dispatch({ type: 'STREAM_ERROR', text: p.message || p.text || 'stream error' });
        return 'terminal';
      case 'done':
        return 'terminal';
      default: return null;
    }
  }, [dispatch]);

  const startPollMode = _useCallback((sessionId) => {
    if (pollerRef.current) { try { pollerRef.current(); } catch (e) {} pollerRef.current = null; }
    let stopped = false, timer = null;
    const tick = async () => {
      if (stopped) return;
      try {
        const base = '/api/orchestrator/sessions/' + encodeURIComponent(sessionId);
        const status = await apiSend(base + '/status', 'GET');
        const msgs = await apiSend(base + '/messages', 'GET');
        if (stopped) return;
        dispatch({ type: 'LOAD_MESSAGES', messages: (msgs && msgs.messages) || [] });
        if (status && !status.in_flight) {
          dispatch({ type: 'CLEAR_STREAMING' });
          dispatch({ type: 'STREAM_LIVE' });
          refreshSessions();
          stopped = true; pollerRef.current = null;
          return;
        }
      } catch (e) { /* keep polling */ }
      if (!stopped) timer = setTimeout(tick, 3000);
    };
    const cancel = () => { stopped = true; if (timer) { clearTimeout(timer); timer = null; } };
    pollerRef.current = cancel;
    tick();
    return cancel;
  }, [refreshSessions, dispatch, pollerRef]);

  // Resilient EventSource wrapper: silent retries (exp backoff) on transient
  // disconnects (browser auto-resumes via Last-Event-ID), poll fallback after
  // 6 failures, no error toasts during recoverable outages.
  const attachStream = _useCallback((sessionId) => {
    closeStream();
    if (!sessionId) return () => {};
    let retry = 0, retryTimer = null, stopped = false, es = null;
    const cleanup = () => {
      stopped = true;
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      if (es) { try { es.close(); } catch (e) {} }
      if (esRef.current === es) esRef.current = null;
      es = null;
    };
    const open = () => {
      if (stopped) return;
      // Capture this iteration's EventSource in closure. Handlers compare
      // `myEs !== es` to bail out if a later open() has already replaced
      // the current connection — otherwise stale handlers from a prior
      // iteration could fire after cleanup, see a nulled `es`, and silently
      // skip the retry increment, leaving subsequent retries dead.
      const myEs = new EventSource(apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/stream'));
      es = myEs;
      esRef.current = myEs;
      SSE_KINDS.forEach(kind => myEs.addEventListener(kind, (ev) => {
        if (stopped || myEs !== es) return;
        retry = 0;
        dispatch({ type: 'STREAM_LIVE' });
        if (dispatchSseEvent(kind, ev.data) === 'terminal') {
          cleanup();
          dispatch({ type: 'CLEAR_STREAMING' });
          refreshSessions();
        }
      }));
      myEs.onerror = () => {
        if (myEs !== es || myEs.readyState !== EventSource.CLOSED) return;
        try { myEs.close(); } catch (e) {}
        if (esRef.current === myEs) esRef.current = null;
        es = null;
        if (stopped) return;
        retry += 1;
        if (retry > 6) {
          dispatch({ type: 'STREAM_POLLING' });
          startPollMode(sessionId);
          return;
        }
        const delay = Math.min(1000 * Math.pow(2, retry - 1), 8000);
        dispatch({ type: 'STREAM_RETRYING', retry, delay });
        retryTimer = setTimeout(() => { retryTimer = null; open(); }, delay);
      };
    };
    open();
    return cleanup;
  }, [closeStream, refreshSessions, dispatchSseEvent, startPollMode, dispatch, esRef]);

  return { attachStream, startPollMode };
}

Object.assign(window, { useResilientStream });
