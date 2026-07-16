// Push notifications toggle for the orchestrator header.
//
// Owns: stable per-device UUID in localStorage, current subscription state,
// and the toggle flow (request permission → fetch VAPID key → subscribe →
// POST to backend; or unsubscribe + DELETE on the way back).
//
// Extracted from orchestrator.jsx to keep that file under the 800-line cap
// (mirrors the orchestrator-stream.jsx split).

const { useState: _useStateNotif, useEffect: _useEffectNotif, useCallback: _useCallbackNotif } = React;

// Stable per-device UUID. Generated lazily, persisted forever — used as the
// key the backend stores subscriptions under. crypto.randomUUID is supported
// in all current evergreen browsers; fallback is a deterministic placeholder
// so older browsers still send *something* unique-ish (the backend treats
// the id as opaque).
function getDeviceId() {
  let id = null;
  try { id = localStorage.getItem('hub-device-id'); } catch (e) { id = null; }
  if (!id) {
    id = (crypto && crypto.randomUUID && crypto.randomUUID()) ||
         '00000000-0000-4000-8000-000000000000';
    try { localStorage.setItem('hub-device-id', id); } catch (e) { /* private mode */ }
  }
  return id;
}

// VAPID public keys are URL-safe base64; PushManager.subscribe needs raw bytes.
function urlBase64ToUint8Array(b64url) {
  const padding = '='.repeat((4 - b64url.length % 4) % 4);
  const b64 = (b64url + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

// status:
//   'unsupported' — browser missing SW / PushManager / Notification
//   'denied'      — permission explicitly denied
//   'idle'        — supported, not subscribed
//   'subscribing' — in-flight (subscribe or unsubscribe)
//   'subscribed'  — active push subscription registered with backend
//   'error'       — last toggle failed (UI shows toast; treat like idle for next click)
function useNotifications(toast) {
  const [status, setStatus] = _useStateNotif('idle');

  _useEffectNotif(() => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      setStatus('unsupported');
      return;
    }
    if (Notification.permission === 'denied') {
      setStatus('denied');
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (!cancelled) setStatus(sub ? 'subscribed' : 'idle');
      } catch (e) {
        if (!cancelled) setStatus('idle');
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const toggle = _useCallbackNotif(async () => {
    if (status === 'unsupported') {
      if (toast) toast('Push notifications not supported in this browser', 'err');
      return;
    }
    if (status === 'denied') {
      if (toast) toast('Notifications blocked. Enable in browser settings.', 'err');
      return;
    }
    if (status === 'subscribing') return;

    if (status === 'subscribed') {
      setStatus('subscribing');
      try {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (sub) await sub.unsubscribe();
        await fetch(
          apiUrl('/api/notifications/subscribe/' + encodeURIComponent(getDeviceId())),
          { method: 'DELETE' },
        );
        setStatus('idle');
        if (toast) toast('Notifications off', 'ok');
      } catch (e) {
        console.error('unsubscribe failed', e);
        setStatus('error');
        if (toast) toast('Unsubscribe failed: ' + (e && e.message ? e.message : 'unknown'), 'err');
      }
      return;
    }

    // idle / error → subscribe
    setStatus('subscribing');
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        setStatus(perm === 'denied' ? 'denied' : 'idle');
        if (toast) toast('Permission ' + perm, 'err');
        return;
      }
      const r = await fetch(apiUrl('/api/notifications/vapid-key'));
      if (!r.ok) throw new Error('vapid-key ' + r.status);
      const { public_key } = await r.json();
      if (!public_key) throw new Error('VAPID key missing');
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key),
      });
      const subJson = sub.toJSON();
      const post = await fetch(apiUrl('/api/notifications/subscribe'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: getDeviceId(),
          subscription: { endpoint: subJson.endpoint, keys: subJson.keys },
        }),
      });
      if (!post.ok) throw new Error('subscribe ' + post.status);
      setStatus('subscribed');
      if (toast) toast('Notifications on', 'ok');
    } catch (e) {
      console.error('subscribe failed', e);
      setStatus('error');
      if (toast) toast('Subscribe failed: ' + (e && e.message ? e.message : 'unknown'), 'err');
    }
  }, [status, toast]);

  return { status, toggle };
}

// Listen for the window-level `hub:open-session` event app.jsx dispatches when
// a notification is clicked (or `?session=<id>` was on the URL). If the target
// session is already in our state, just select it; otherwise refresh the list
// first (the session may be brand-new) and then select.
function useOpenSessionListener({ sessions, onSelectSession, refreshSessions }) {
  _useEffectNotif(() => {
    const onOpenSession = (e) => {
      const sid = e && e.detail && e.detail.session_id;
      if (!sid) return;
      const have = (sessions || []).some(s => s.id === sid);
      if (have) {
        onSelectSession(sid);
      } else {
        refreshSessions().then(() => onSelectSession(sid)).catch(() => {});
      }
    };
    window.addEventListener('hub:open-session', onOpenSession);
    return () => window.removeEventListener('hub:open-session', onOpenSession);
  }, [onSelectSession, refreshSessions, sessions]);
}

Object.assign(window, { useNotifications, getDeviceId, urlBase64ToUint8Array, useOpenSessionListener });
