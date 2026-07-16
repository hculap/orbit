// Unread indicator for the Orchestrator chat:
//   - Plays a short Web Audio sine "ding" when a new assistant message arrives
//   - Sets `● ` prefix on document.title
//   - Renders a red dot overlay on the favicon (canvas-painted data URL)
//   - Clears the badge ~1.5s after the user is on the orchestrator section
//     with the tab visible (Messenger semantics)
//
// Lives in its own sibling file to keep orchestrator.jsx under the 800-line
// cap. Published as a global hook via window.useUnreadIndicator.

const { useEffect: _useEffect, useRef: _useRef, useState: _useState, useCallback: _useCallback } = React;

// Module-level user-gesture flag. Chrome/iOS won't let us construct an
// AudioContext (or even resume one) until after the first input event.
// Trying anyway floods the console with "AudioContext was not allowed to
// start" warnings even though our try/catch swallows the actual exception.
// Listen once for any plausible gesture, flip the flag, detach.
let _hubUserGestured = false;
if (typeof window !== 'undefined' && !_hubUserGestured) {
  const _markGesture = () => {
    _hubUserGestured = true;
    document.removeEventListener('pointerdown', _markGesture, true);
    document.removeEventListener('keydown', _markGesture, true);
    document.removeEventListener('touchstart', _markGesture, true);
  };
  document.addEventListener('pointerdown', _markGesture, true);
  document.addEventListener('keydown', _markGesture, true);
  document.addEventListener('touchstart', _markGesture, true);
}

function useUnreadIndicator({ messages, activeId, isActive }) {
  const [settings] = (window.useSettings || (() => [{ unreadSound: true }, () => {}]))();
  const [unread, setUnread] = _useState(false);
  const audioCtxRef = _useRef(null);
  const baseTitleRef = _useRef(typeof document !== 'undefined' ? document.title : '');
  const baseFaviconHrefRef = _useRef(null);
  const prevMsgCount = _useRef(null);
  const lastSessionId = _useRef(null);

  // Capture the original favicon href once at mount.
  _useEffect(() => {
    const link = document.querySelector("link[rel='icon']") || document.querySelector("link[rel~='icon']");
    if (link) baseFaviconHrefRef.current = link.href;
  }, []);

  // Short two-tone "ding" via Web Audio. Lazy-creates the AudioContext on
  // first use; resumes it if the browser suspended it (background tabs).
  // Silently no-ops if autoplay is still blocked.
  const playDing = _useCallback(() => {
    // Skip entirely until the user has interacted with the page — otherwise
    // creating/resuming the AudioContext fires a deprecation warning even
    // though it's wrapped in try/catch.
    if (!_hubUserGestured) return;
    try {
      if (!audioCtxRef.current) {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        audioCtxRef.current = new Ctx();
      }
      const ctx = audioCtxRef.current;
      if (ctx.state === 'suspended') ctx.resume();
      const t0 = ctx.currentTime;
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = 'sine';
      o.frequency.setValueAtTime(880, t0);              // A5
      o.frequency.setValueAtTime(1175, t0 + 0.08);      // D6 — bright bell-like ping
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(0.18, t0 + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.45);
      o.connect(g).connect(ctx.destination);
      o.start(t0);
      o.stop(t0 + 0.5);
    } catch (e) { /* autoplay blocked or audio unsupported */ }
  }, []);

  // Detect a NEW assistant message. Re-baselines on session switch so
  // simply selecting a different session doesn't fire a phantom ding.
  // After a switch we hold prevMsgCount=null so the FIRST messages-load
  // (loadMessages dumps the historical transcript in one batch — curr jumps
  // 0 → N) is treated as baselining, not a new arrival. Subsequent SSE
  // events grow the count by 1 from a known-real prev → ding.
  _useEffect(() => {
    if (lastSessionId.current !== activeId) {
      prevMsgCount.current = null;
      lastSessionId.current = activeId;
      return;
    }
    const prev = prevMsgCount.current;
    const curr = (messages && messages.length) || 0;
    prevMsgCount.current = curr;
    if (prev === null) return;
    if (curr <= prev) return;
    const last = messages[curr - 1];
    if (!last || last.role !== 'assistant') return;
    const hasVisible = Array.isArray(last.blocks) && last.blocks.some((b) => {
      if (!b) return false;
      if (b.kind === 'image') return true;
      const text = (b.text || b.content || '').trim();
      return text && (b.kind === 'markdown' || b.kind === 'text' || b.kind === 'code');
    });
    if (!hasVisible) return;
    if (settings.unreadSound !== false) playDing();
    setUnread(true);
  }, [messages, activeId, playDing, settings.unreadSound]);

  // Clear unread when user is actively viewing the orchestrator section.
  // Messenger-style: small grace window (1.5s) so the badge is briefly
  // visible even when the chat was already open.
  _useEffect(() => {
    if (!unread) return undefined;
    let timer = null;
    const isViewing = () => isActive && document.visibilityState === 'visible';
    const trySchedule = () => {
      if (timer) return;
      if (!isViewing()) return;
      timer = setTimeout(() => { setUnread(false); timer = null; }, 1500);
    };
    trySchedule();
    document.addEventListener('visibilitychange', trySchedule);
    window.addEventListener('focus', trySchedule);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', trySchedule);
      window.removeEventListener('focus', trySchedule);
    };
  }, [unread, isActive]);

  // Reflect unread to document.title.
  _useEffect(() => {
    const baseTitle = baseTitleRef.current || 'Orchestrator';
    document.title = unread ? '● ' + baseTitle : baseTitle;
  }, [unread]);

  // Presence heartbeat: while the user is on the orchestrator section AND the
  // tab is visible AND a session is open, POST to /api/orchestrator/presence
  // every 15s. On visibility-loss / unmount, send one final visible:false so
  // the runner can fire chat notification for the next tool-completed turn.
  _useEffect(() => {
    if (!activeId || !isActive) return undefined;
    let stopped = false;
    let timer = null;
    let clientId = null;
    try {
      clientId = window.sessionStorage.getItem('hub-presence-client-id');
      if (!clientId) {
        const cryptoObj = window.crypto || window.msCrypto;
        clientId = (cryptoObj && cryptoObj.randomUUID)
          ? cryptoObj.randomUUID()
          : 'c-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
        window.sessionStorage.setItem('hub-presence-client-id', clientId);
      }
    } catch (_) {
      clientId = 'c-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    }
    const apiUrl = (typeof window.apiUrl === 'function')
      ? window.apiUrl
      : (p) => (window.HUB_BASE_PATH || '') + p;
    const post = (visible) => {
      if (stopped) return;
      const url = apiUrl('/api/orchestrator/presence/' + encodeURIComponent(activeId));
      try {
        fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ client_id: clientId, visible }),
          keepalive: true,
        }).catch(() => { /* presence is best-effort */ });
      } catch (_) { /* presence is best-effort */ }
    };
    const tick = () => {
      if (stopped) return;
      if (document.visibilityState === 'visible') post(true);
    };
    tick();
    timer = setInterval(tick, 15000);
    const onVisibility = () => {
      if (document.visibilityState === 'visible') tick();
      else post(false);
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      if (timer) clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
      post(false);
    };
  }, [activeId, isActive]);

  // Reflect unread to favicon: paint base icon onto a 64x64 canvas, overlay
  // a red dot in the top-right corner if unread, then write the data URL
  // back to the existing <link rel="icon"> href. Skips if the base href
  // hasn't been captured (e.g. SSR or template missing the link).
  _useEffect(() => {
    const baseHref = baseFaviconHrefRef.current;
    if (!baseHref) return undefined;
    let cancelled = false;
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    const img = new Image();
    img.onload = () => {
      if (cancelled) return;
      ctx.clearRect(0, 0, 64, 64);
      ctx.drawImage(img, 0, 0, 64, 64);
      if (unread) {
        ctx.fillStyle = '#e35d5d';
        ctx.beginPath();
        ctx.arc(48, 16, 14, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#0e0f12';
        ctx.lineWidth = 4;
        ctx.stroke();
      }
      const link = document.querySelector("link[rel='icon']") || document.querySelector("link[rel~='icon']");
      if (link) link.href = canvas.toDataURL('image/png');
    };
    img.onerror = () => { /* ignore: keep base favicon */ };
    img.src = baseHref;
    return () => { cancelled = true; };
  }, [unread]);

  return { unread };
}

Object.assign(window, { useUnreadIndicator });
