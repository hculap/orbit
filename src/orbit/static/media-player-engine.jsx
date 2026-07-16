// media-player-engine.jsx — the app-lifetime media engine.
//
// Owns exactly TWO persistent HTMLMediaElements (one <audio>, one <video>)
// created imperatively (document.createElement, NOT JSX) so React's reconciler
// never fights the manual DOM moves used to "adopt" the <video> into a visible
// surface. Both live in a hidden fixed "garage" rendered by MediaPlayerProvider,
// which wraps ResponsiveHub ABOVE the section router AND the Mobile/Desktop shell
// swap — so neither navigation nor the 960px breakpoint flip ever unmounts them.
// Audio therefore keeps playing while the user walks around the whole app; the
// dock is a pure controller, never a host.
//
// Discrete state (current track, playing, speed, autoplay, pip, fs, keyboardOpen,
// blocked, queue) lives in a useReducer and flows via React context
// (window.useMediaPlayer). High-frequency position/duration/buffered live in a
// tiny external store (window.usePlayerProgress / useSyncExternalStore) so only
// the scrubber re-renders ~4Hz, never the whole tree.
//
// Cross-file callers (the artifact Play handlers, which have no context handle in
// the gallery render path) drive AUTO-handoff-on-Play through the imperative
// facade window.HubPlayer. State persists to its OWN localStorage namespace
// (hub.media.*), never the useSettings store.
//
// Deps via window globals (no bundler): useToast (components.jsx). Loads after
// components.jsx, before orchestrator-artifacts.jsx + app.jsx.

const { useReducer: _mpUseReducer, useRef: _mpRef, useEffect: _mpEffect, useMemo: _mpMemo, useContext: _mpCtxHook, createContext: _mpCreateCtx } = React;

// ── constants ────────────────────────────────────────────────────────
const MEDIA_SPEED_STEPS = [0.75, 1, 1.25, 1.5, 1.75, 2];
const _MP_PREFS_KEY = 'hub.media.prefs';
const _MP_POS_KEY = 'hub.media.pos';
const _MP_NOW_KEY = 'hub.media.now';
const _MP_MAX_POS = 200;
const _MP_FLUSH_MS = 4000;
const _MP_POS_TTL_MS = 60 * 24 * 60 * 60 * 1000; // 60 days
const _MP_MIN_RESUME = 2; // don't resume within first/last 2s
const _MP_SKIP_DEFAULT = 5; // seconds for the rewind/forward buttons
const _MP_SKIP_MIN = 1;
const _MP_SKIP_MAX = 120;

function _mpClampSkip(n) {
  const v = Math.round(Number(n));
  if (!Number.isFinite(v)) return _MP_SKIP_DEFAULT;
  return Math.min(_MP_SKIP_MAX, Math.max(_MP_SKIP_MIN, v));
}

// ── pure helpers ─────────────────────────────────────────────────────
function _mpScopeKey(scope) {
  if (scope && scope.sessionId) return 's:' + scope.sessionId;
  return 'l:' + ((scope && scope.libId) || '__global__');
}

// Stable per-(type,scope,id) key — matches _artScopeQs identity so session- and
// lib-scoped copies of the same id get distinct resume slots (correct: they may
// genuinely diverge).
function makeMediaTrackKey(type, scope, id) {
  return type + '|' + _mpScopeKey(scope) + '|' + id;
}

function _mpBufferedEnd(el) {
  try {
    return el.buffered && el.buffered.length ? el.buffered.end(el.buffered.length - 1) : 0;
  } catch (e) {
    return 0;
  }
}

function _mpClampIndex(i, len) {
  const n = Number.isFinite(i) ? Math.floor(i) : 0;
  return Math.min(Math.max(0, n), Math.max(0, len - 1));
}

// ── localStorage (own namespace; every access guarded → silent degrade) ──
function _mpReadPrefs() {
  const fallback = { speed: 1, autoplayNext: false, skipSeconds: _MP_SKIP_DEFAULT };
  try {
    const raw = localStorage.getItem(_MP_PREFS_KEY);
    if (!raw) return fallback;
    const p = JSON.parse(raw);
    const speed = MEDIA_SPEED_STEPS.indexOf(p && p.speed) >= 0 ? p.speed : 1;
    const skipSeconds = (p && p.skipSeconds != null) ? _mpClampSkip(p.skipSeconds) : _MP_SKIP_DEFAULT;
    return { speed, autoplayNext: !!(p && p.autoplayNext), skipSeconds };
  } catch (e) {
    return fallback;
  }
}

function _mpWritePrefs(speed, autoplayNext, skipSeconds) {
  try {
    localStorage.setItem(_MP_PREFS_KEY, JSON.stringify({ version: 1, speed, autoplayNext, skipSeconds }));
  } catch (e) {
    /* quota / private mode — prefs are best-effort */
  }
}

// ── "now playing" persistence (survive page reload / server refresh) ──
// Stores the current track + queue snapshot so a reload re-shows the player
// (paused — no autoplay; the position itself comes from hub.media.pos). Cleared
// only on explicit stop().
function _mpValidTrack(t) {
  return !!(t && typeof t === 'object' && t.id && t.type && t.url && t.key);
}

function _mpReadNow() {
  try {
    const raw = localStorage.getItem(_MP_NOW_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p || !_mpValidTrack(p.track)) return null;
    const t = p.track;
    const rawItems = (p.queue && Array.isArray(p.queue.items)) ? p.queue.items.filter(_mpValidTrack) : [];
    const items = rawItems.length ? rawItems : [t];
    const idx = (p.queue && Number.isInteger(p.queue.index)) ? p.queue.index : 0;
    return { track: t, queue: { items, index: Math.min(Math.max(0, idx), items.length - 1) } };
  } catch (e) {
    return null;
  }
}

function _mpWriteNow(track, queue) {
  if (!_mpValidTrack(track)) { try { localStorage.removeItem(_MP_NOW_KEY); } catch (e) { /* ignore */ } return; }
  const q = queue && Array.isArray(queue.items) && queue.items.length ? queue : { items: [track], index: 0 };
  try {
    localStorage.setItem(_MP_NOW_KEY, JSON.stringify({ version: 1, track, queue: q }));
  } catch (e) {
    // Quota: fall back to track-only (drop the queue) so the player still restores.
    try { localStorage.setItem(_MP_NOW_KEY, JSON.stringify({ version: 1, track, queue: { items: [track], index: 0 } })); } catch (e2) { /* ignore */ }
  }
}

function _mpClearNow() { try { localStorage.removeItem(_MP_NOW_KEY); } catch (e) { /* ignore */ } }

function _mpReadPosMap() {
  try {
    const raw = localStorage.getItem(_MP_POS_KEY);
    if (!raw) return {};
    const p = JSON.parse(raw);
    return p && p.byTrack && typeof p.byTrack === 'object' ? p.byTrack : {};
  } catch (e) {
    return {};
  }
}

// Immutable write: rebuild a fresh object, GC oldest entries by ts, emergency
// shrink on quota. Returns nothing; never throws.
function _mpWritePosMap(map) {
  const prune = (m, keep) => {
    const keys = Object.keys(m);
    const sorted = keys
      .map((k) => [k, (m[k] && m[k].ts) || 0])
      .sort((a, b) => b[1] - a[1])
      .slice(0, keep);
    const next = {};
    sorted.forEach(([k]) => { next[k] = m[k]; });
    return next;
  };
  let out = map;
  if (Object.keys(out).length > _MP_MAX_POS) out = prune(out, Math.floor(_MP_MAX_POS / 2));
  try {
    localStorage.setItem(_MP_POS_KEY, JSON.stringify({ version: 1, byTrack: out }));
  } catch (e) {
    try {
      localStorage.setItem(_MP_POS_KEY, JSON.stringify({ version: 1, byTrack: prune(out, 50) }));
    } catch (e2) {
      /* give up — in-session continuity is unaffected (element never unmounts) */
    }
  }
}

function _mpGcOldPositions() {
  const map = _mpReadPosMap();
  const now = Date.now();
  const keys = Object.keys(map);
  let changed = false;
  const next = {};
  keys.forEach((k) => {
    const e = map[k];
    if (e && now - (e.ts || 0) < _MP_POS_TTL_MS) next[k] = e;
    else changed = true;
  });
  if (changed) _mpWritePosMap(next);
}

// ── external progress store (scrubber-only re-render) ─────────────────
const _mpProgress = (() => {
  let snap = { position: 0, duration: 0, buffered: 0 };
  const subs = new Set();
  return {
    get: () => snap,
    set: (next) => {
      // Coalesce identical snapshots so useSyncExternalStore stays stable.
      if (next.position === snap.position && next.duration === snap.duration && next.buffered === snap.buffered) return;
      snap = next;
      subs.forEach((fn) => { try { fn(); } catch (e) { /* ignore */ } });
    },
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
  };
})();

function usePlayerProgress() {
  return React.useSyncExternalStore(_mpProgress.subscribe, _mpProgress.get, _mpProgress.get);
}

// ── reducer (immutable transitions) ──────────────────────────────────
function _mpInit() {
  const prefs = _mpReadPrefs();
  return {
    track: null,
    queue: { items: [], index: 0 },
    isPlaying: false,
    speed: prefs.speed,
    autoplayNext: prefs.autoplayNext,
    skipSeconds: prefs.skipSeconds,
    pip: false,
    fs: false,
    keyboardOpen: false,
    blocked: false,
    error: null,
  };
}

function _mpReducer(state, action) {
  switch (action.type) {
    case 'PLAY_TRACK':
      return { ...state, track: action.track, queue: action.queue, isPlaying: true, blocked: false, error: null };
    case 'RESTORE':
      // Re-show a reloaded track WITHOUT playing it (no autoplay — the user clicks
      // start; the position is restored from hub.media.pos on the first play).
      return { ...state, track: action.track, queue: action.queue, isPlaying: false, blocked: false, error: null };
    case 'SET_QUEUE':
      // Queue mutated (enqueue / remove / reorder) without changing the current
      // track or play state — just re-publish the new queue snapshot.
      return { ...state, queue: action.queue };
    case 'SET_PLAYING':
      return state.isPlaying === action.value ? state : { ...state, isPlaying: action.value };
    case 'SET_SPEED':
      return state.speed === action.value ? state : { ...state, speed: action.value };
    case 'SET_AUTOPLAY':
      return state.autoplayNext === action.value ? state : { ...state, autoplayNext: action.value };
    case 'SET_SKIP':
      return state.skipSeconds === action.value ? state : { ...state, skipSeconds: action.value };
    case 'SET_PIP':
      return state.pip === action.value ? state : { ...state, pip: action.value };
    case 'SET_FS':
      return state.fs === action.value ? state : { ...state, fs: action.value };
    case 'SET_KBD':
      return state.keyboardOpen === action.value ? state : { ...state, keyboardOpen: action.value };
    case 'SET_BLOCKED':
      return state.blocked === action.value ? state : { ...state, blocked: action.value };
    case 'SET_ERROR':
      return { ...state, error: action.value };
    case 'CLEAR':
      // Pure: keep prefs + keyboard, zero the rest. (Must NOT call _mpInit() here
      // — it reads localStorage, and a reducer must stay side-effect free.)
      return {
        track: null, queue: { items: [], index: 0 }, isPlaying: false,
        pip: false, fs: false, blocked: false, error: null,
        speed: state.speed, autoplayNext: state.autoplayNext, skipSeconds: state.skipSeconds, keyboardOpen: state.keyboardOpen,
      };
    default:
      return state;
  }
}

const _MediaCtx = _mpCreateCtx(null);

// Synchronous no-op stub so any handler that reads window.HubPlayer before the
// provider's mount effect runs never crashes (overwritten with the real facade).
if (!window.HubPlayer) {
  window.HubPlayer = {
    play() {}, toggle() {}, pause() {}, resume() {}, seek() {}, seekBy() {},
    setSpeed() {}, cycleSpeed() {}, setAutoplayNext() {}, toggleAutoplayNext() {},
    next() {}, prev() {}, togglePip() {}, enterFullscreen() {}, stop() {},
    enqueue() {}, removeAt() {}, reorder() {}, jumpTo() {},
    isCurrent() { return false; }, adoptVideo() {}, releaseVideo() {},
    reportKeyboard() {}, dismissBlocked() {}, getState() { return null; },
  };
}

// ── provider ─────────────────────────────────────────────────────────
function MediaPlayerProvider({ children }) {
  const [state, dispatch] = _mpUseReducer(_mpReducer, undefined, _mpInit);

  const stateRef = _mpRef(state);
  stateRef.current = state;

  const garageRef = _mpRef(null);
  const audioRef = _mpRef(null);
  const videoRef = _mpRef(null);
  const loadSeqRef = _mpRef(0);
  const pendingSeekRef = _mpRef(null);
  const posAccRef = _mpRef(null); // { key, t, d }
  const lastFlushRef = _mpRef(0);
  const videoOwnerRef = _mpRef(null);
  const errSkipRef = _mpRef(0);
  // Synchronous source of truth for speed/autoplay (state lags within a tick, so
  // writePrefs from state could persist a stale value). Seeded from initial prefs.
  const prefsRef = _mpRef(null);
  if (prefsRef.current === null) prefsRef.current = { speed: state.speed, autoplayNext: state.autoplayNext, skipSeconds: state.skipSeconds };
  // Synchronous "what's loaded" — control/identity decisions (same-track check,
  // activeEl, isCurrent, queue walking) must NOT read stateRef (it lags within a
  // tick, so e.g. stop()+play() in one tick would mis-detect "same" and skip the
  // src reload). These refs update synchronously inside play()/stop().
  const curTrackRef = _mpRef(null);
  const curQueueRef = _mpRef({ items: [], index: 0 });

  const _toast = (typeof useToast === 'function') ? useToast() : null;
  const toastRef = _mpRef(_toast);
  toastRef.current = _toast;

  // Imperative facade — built ONCE; every closure reads live values via refs.
  const api = _mpMemo(() => {
    const elFor = (type) => (type === 'video' ? videoRef.current : audioRef.current);
    const otherEl = (type) => (type === 'video' ? audioRef.current : videoRef.current);
    const activeEl = () => {
      const t = curTrackRef.current;
      return t ? elFor(t.type) : null;
    };
    const toast = (msg, kind) => { if (toastRef.current) toastRef.current(msg, kind || 'err'); };

    function flushPos(opts) {
      const acc = posAccRef.current;
      if (!acc || !acc.key) return;
      const map = _mpReadPosMap();
      const t = opts && opts.reset ? 0 : acc.t;
      const next = { ...map, [acc.key]: { t, d: acc.d, ts: Date.now() } };
      _mpWritePosMap(next);
      lastFlushRef.current = Date.now();
    }

    function setMediaSession(track) {
      if (!('mediaSession' in navigator)) return;
      try {
        const MM = window.MediaMetadata;
        if (MM) {
          navigator.mediaSession.metadata = new MM({
            title: track.title || 'Media',
            artist: 'Orbit',
            artwork: track.poster ? [{ src: track.poster, sizes: '256x256', type: 'image/png' }] : [],
          });
        }
        const set = (a, h) => { try { navigator.mediaSession.setActionHandler(a, h); } catch (e) { /* unsupported action */ } };
        set('play', () => resume());
        set('pause', () => pause());
        set('previoustrack', () => prev());
        set('nexttrack', () => next());
        set('seekbackward', (d) => seekBy(-((d && d.seekOffset) || prefsRef.current.skipSeconds)));
        set('seekforward', (d) => seekBy((d && d.seekOffset) || prefsRef.current.skipSeconds));
        set('seekto', (d) => { if (d && typeof d.seekTime === 'number') seek(d.seekTime); });
      } catch (e) {
        /* mediaSession best-effort */
      }
    }

    function clearMediaSession() {
      if (!('mediaSession' in navigator)) return;
      try {
        navigator.mediaSession.metadata = null;
        navigator.mediaSession.playbackState = 'none';
        ['play', 'pause', 'previoustrack', 'nexttrack', 'seekbackward', 'seekforward', 'seekto'].forEach((a) => {
          try { navigator.mediaSession.setActionHandler(a, null); } catch (e) { /* ignore */ }
        });
      } catch (e) {
        /* ignore */
      }
    }

    function findNext(queue, current, dir) {
      if (!queue || !Array.isArray(queue.items)) return null;
      const step = dir < 0 ? -1 : 1;
      for (let i = queue.index + step; i >= 0 && i < queue.items.length; i += step) {
        const cand = queue.items[i];
        if (cand && cand.type === current.type && cand.key !== current.key) return { track: cand, index: i };
      }
      return null;
    }

    function play(track, opts) {
      if (!track || !track.url || !track.type) return;
      const el = elFor(track.type);
      if (!el) return;
      // Cross-subsystem handoff: starting a clip pauses TTS + conversation voice.
      if (window.HubAudioBus) window.HubAudioBus.requestActive('media');
      const cur = curTrackRef.current;
      const same = cur && cur.key === track.key;
      const seq = ++loadSeqRef.current;
      // Reset the error-skip budget on every NORMAL play (user click, end-of-track
      // autoplay-next), but NOT when _onError advances after a failed load — else
      // the < 3 storm bound never accumulates and a dead-URL queue walks in full.
      if (!(opts && opts.fromError)) errSkipRef.current = 0;

      // Single audible stream invariant: pause + clear the OTHER kind on handoff.
      const o = otherEl(track.type);
      if (o) {
        try { o.pause(); if (o.getAttribute('src')) { o.removeAttribute('src'); o.load(); } } catch (e) { /* ignore */ }
      }

      const queue = opts && Array.isArray(opts.queue) && opts.queue.length
        ? { items: opts.queue.slice(), index: _mpClampIndex(opts.index, opts.queue.length) }
        : { items: [track], index: 0 };

      if (!same) {
        flushPos(); // persist outgoing track's position before switching
        if (el.getAttribute('src') !== track.url) {
          try { el.src = track.url; el.load(); } catch (e) { /* ignore */ }
        }
        const saved = _mpReadPosMap()[track.key];
        pendingSeekRef.current = { seq, t: saved ? saved.t : 0 };
        posAccRef.current = { key: track.key, t: saved ? saved.t : 0, d: saved ? saved.d : 0 };
      }

      curTrackRef.current = track;
      curQueueRef.current = queue;
      _mpWriteNow(track, queue); // persist so a reload re-shows this track
      try { el.playbackRate = prefsRef.current.speed; } catch (e) { /* ignore */ }
      dispatch({ type: 'PLAY_TRACK', track, queue });
      setMediaSession(track);
      const p = el.play();
      if (p && p.catch) {
        p.catch((err) => {
          if (err && err.name === 'NotAllowedError') dispatch({ type: 'SET_BLOCKED', value: true });
          // AbortError (rapid src swap) is benign.
        });
      }
    }

    function pause() { const el = activeEl(); if (el) { try { el.pause(); } catch (e) { /* ignore */ } } }
    function resume() {
      const el = activeEl();
      if (!el) return;
      // Resuming a clip also reclaims the audible channel from TTS / conversation.
      if (window.HubAudioBus) window.HubAudioBus.requestActive('media');
      const p = el.play();
      if (p && p.catch) p.catch((err) => { if (err && err.name === 'NotAllowedError') dispatch({ type: 'SET_BLOCKED', value: true }); });
    }
    function toggle() { const el = activeEl(); if (!el) return; if (el.paused) resume(); else pause(); }

    function seek(seconds) {
      const el = activeEl();
      if (!el) return;
      const d = el.duration;
      const t = Math.max(0, Number(seconds) || 0);
      try { el.currentTime = Number.isFinite(d) ? Math.min(t, d - 0.05) : t; } catch (e) { /* ignore */ }
      const cur = el.currentTime || 0;
      _mpProgress.set({ position: cur, duration: Number.isFinite(d) ? d : 0, buffered: _mpBufferedEnd(el) });
      // Keep the resume accumulator in sync so a seek-then-reload (even while
      // paused, before any timeupdate fires) restores the seeked position.
      const trk = curTrackRef.current;
      if (trk) posAccRef.current = { key: trk.key, t: cur, d: Number.isFinite(d) ? d : 0 };
    }
    function seekBy(delta) { const el = activeEl(); if (el) seek((el.currentTime || 0) + delta); }

    function setSpeed(rate) {
      const r = MEDIA_SPEED_STEPS.indexOf(rate) >= 0 ? rate : 1;
      const el = activeEl();
      if (el) { try { el.playbackRate = r; } catch (e) { /* ignore */ } }
      prefsRef.current = { ...prefsRef.current, speed: r };
      dispatch({ type: 'SET_SPEED', value: r });
      _mpWritePrefs(r, prefsRef.current.autoplayNext, prefsRef.current.skipSeconds);
    }
    function cycleSpeed() {
      const i = MEDIA_SPEED_STEPS.indexOf(prefsRef.current.speed);
      setSpeed(MEDIA_SPEED_STEPS[(i + 1) % MEDIA_SPEED_STEPS.length]);
    }

    function setAutoplayNext(value) {
      const v = !!value;
      prefsRef.current = { ...prefsRef.current, autoplayNext: v };
      dispatch({ type: 'SET_AUTOPLAY', value: v });
      _mpWritePrefs(prefsRef.current.speed, v, prefsRef.current.skipSeconds);
    }
    function toggleAutoplayNext() { setAutoplayNext(!prefsRef.current.autoplayNext); }

    function setSkipSeconds(n) {
      const s = _mpClampSkip(n);
      prefsRef.current = { ...prefsRef.current, skipSeconds: s };
      dispatch({ type: 'SET_SKIP', value: s });
      _mpWritePrefs(prefsRef.current.speed, prefsRef.current.autoplayNext, s);
    }
    function skipBack() { seekBy(-prefsRef.current.skipSeconds); }
    function skipForward() { seekBy(prefsRef.current.skipSeconds); }

    function next() {
      const trk = curTrackRef.current;
      const q = curQueueRef.current;
      if (!trk) return;
      const nx = findNext(q, trk, 1);
      if (nx) play(nx.track, { queue: q.items, index: nx.index });
    }
    function prev() {
      const trk = curTrackRef.current;
      const q = curQueueRef.current;
      if (!trk) return;
      const el = activeEl();
      // Spotify-style: if past 3s, restart current; else go to previous.
      if (el && (el.currentTime || 0) > 3) { seek(0); return; }
      const pv = findNext(q, trk, -1);
      if (pv) play(pv.track, { queue: q.items, index: pv.index });
      else seek(0);
    }

    function togglePip() {
      const v = videoRef.current;
      if (!v) return;
      if (document.pictureInPictureElement) { document.exitPictureInPicture().catch(() => {}); return; }
      if (v.requestPictureInPicture && document.pictureInPictureEnabled && v.disablePictureInPicture !== true) {
        v.requestPictureInPicture().catch(() => toast('PiP niedostępne', 'err'));
      } else if (typeof v.webkitSetPresentationMode === 'function') {
        try { v.webkitSetPresentationMode('picture-in-picture'); } catch (e) { toast('PiP niedostępne', 'err'); }
      } else {
        toast('PiP niewspierane', 'err');
      }
    }

    function enterFullscreen() {
      const v = videoRef.current;
      if (!v) return;
      try {
        if (v.requestFullscreen) v.requestFullscreen().catch(() => {});
        else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();
        else if (v.webkitRequestFullscreen) v.webkitRequestFullscreen();
        else toast('Pełny ekran niewspierany', 'err');
      } catch (e) {
        toast('Pełny ekran niedostępny', 'err');
      }
    }

    function stop() {
      flushPos();
      [audioRef.current, videoRef.current].forEach((el) => {
        if (!el) return;
        try { el.pause(); if (el.getAttribute('src')) { el.removeAttribute('src'); el.load(); } } catch (e) { /* ignore */ }
      });
      clearMediaSession();
      _mpClearNow();
      posAccRef.current = null;
      curTrackRef.current = null;
      curQueueRef.current = { items: [], index: 0 };
      dispatch({ type: 'CLEAR' });
      _mpProgress.set({ position: 0, duration: 0, buffered: 0 });
      if (window.HubAudioBus) window.HubAudioBus.release('media');
    }

    function isCurrent(id, scope) {
      const t = curTrackRef.current;
      return !!t && t.id === id && _mpScopeKey(t.scope) === _mpScopeKey(scope);
    }

    // ── user-built playback queue ─────────────────────────────────────────
    // Persist a mutated queue (current track unchanged) + re-publish to React.
    function _commitQueue(q) {
      curQueueRef.current = q;
      const t = curTrackRef.current;
      if (t) _mpWriteNow(t, q);
      dispatch({ type: 'SET_QUEUE', queue: q });
    }
    // Append a track to the queue. If nothing is playing, start it now. De-dupes
    // by key — re-adding an already-queued track is a no-op (with a hint).
    function enqueue(track) {
      if (!track || !track.url || !track.type) return;
      if (!curTrackRef.current) { play(track, { queue: [track], index: 0 }); return; }
      const q = curQueueRef.current || { items: [], index: 0 };
      if (q.items.some((it) => it && it.key === track.key)) {
        if (typeof toast === 'function') toast('Już w kolejce', 'info');
        return;
      }
      _commitQueue({ items: q.items.concat([track]), index: q.index });
      if (typeof toast === 'function') toast('Dodano do kolejki', 'ok');
    }
    // Remove the queue item at index i. Removing the CURRENT track hops to the
    // next same-type track (or prev, or stops if it was the only one).
    function removeAt(i) {
      const q = curQueueRef.current || { items: [], index: 0 };
      if (i < 0 || i >= q.items.length) return;
      if (i === q.index) {
        const trk = curTrackRef.current;
        const hop = findNext(q, trk, 1) || findNext(q, trk, -1);
        const items = q.items.slice(); items.splice(i, 1);
        if (!items.length || !hop) { stop(); return; }
        const newIdx = items.findIndex((it) => it && it.key === hop.track.key);
        play(hop.track, { queue: items, index: newIdx < 0 ? 0 : newIdx });
        return;
      }
      const items = q.items.slice(); items.splice(i, 1);
      _commitQueue({ items, index: i < q.index ? q.index - 1 : q.index });
    }
    // Move a queue item (▲▼ / drag). The index follows the playing track so
    // reordering never interrupts playback.
    function reorder(from, to) {
      const q = curQueueRef.current || { items: [], index: 0 };
      const n = q.items.length;
      if (from < 0 || from >= n || to < 0 || to >= n || from === to) return;
      const curKey = q.items[q.index] && q.items[q.index].key;
      const items = q.items.slice();
      const [moved] = items.splice(from, 1);
      items.splice(to, 0, moved);
      const index = curKey ? Math.max(0, items.findIndex((it) => it && it.key === curKey)) : q.index;
      _commitQueue({ items, index });
    }
    // Jump to + play the queue item at index i.
    function jumpTo(i) {
      const q = curQueueRef.current || { items: [], index: 0 };
      if (i < 0 || i >= q.items.length || !q.items[i]) return;
      play(q.items[i], { queue: q.items, index: i });
    }

    // Move the singleton <video> into a visible, pixel-stable slot (modal). In-
    // document re-parenting does NOT interrupt playback; src/load() are never
    // touched here (the single most fragile invariant). Owner-token guarded so a
    // second claimant can't steal the node.
    function adoptVideo(slotEl, token) {
      const v = videoRef.current;
      if (!v || !slotEl) return;
      if (videoOwnerRef.current && videoOwnerRef.current !== token) return;
      videoOwnerRef.current = token;
      v.style.position = 'absolute';
      v.style.inset = '0';
      v.style.width = '100%';
      v.style.height = '100%';
      v.style.opacity = '1';
      v.style.objectFit = 'contain';
      v.controls = true;
      if (v.parentNode !== slotEl) slotEl.appendChild(v);
    }
    function releaseVideo(token) {
      if (videoOwnerRef.current !== token) return;
      videoOwnerRef.current = null;
      const v = videoRef.current;
      if (!v) return;
      v.controls = false;
      v.style.position = '';
      v.style.inset = '';
      v.style.width = '1px';
      v.style.height = '1px';
      v.style.opacity = '0';
      if (garageRef.current && v.parentNode !== garageRef.current) garageRef.current.appendChild(v);
    }

    function reportKeyboard(value) { dispatch({ type: 'SET_KBD', value: !!value }); }
    function dismissBlocked() { dispatch({ type: 'SET_BLOCKED', value: false }); resume(); }
    function getState() { return stateRef.current; }

    // Re-show the last-played track after a page reload (no autoplay). Seeds the
    // scrubber from the saved position, sets src (preload metadata) so a later
    // play() resumes instantly. Called once from the provider mount effect.
    function restoreNow() {
      const saved = _mpReadNow();
      if (!saved || !saved.track) return;
      const track = saved.track;
      const el = elFor(track.type);
      if (!el) return;
      curTrackRef.current = track;
      curQueueRef.current = saved.queue;
      const pos = _mpReadPosMap()[track.key];
      const t = pos ? pos.t : 0;
      const d = pos ? pos.d : 0;
      const seq = ++loadSeqRef.current;
      pendingSeekRef.current = { seq, t };
      posAccRef.current = { key: track.key, t, d };
      _mpProgress.set({ position: t, duration: Number.isFinite(d) ? d : 0, buffered: 0 });
      try { if (el.getAttribute('src') !== track.url) { el.src = track.url; el.load(); } } catch (e) { /* ignore */ }
      try { el.playbackRate = prefsRef.current.speed; } catch (e) { /* ignore */ }
      dispatch({ type: 'RESTORE', track, queue: saved.queue });
      setMediaSession(track);
      if ('mediaSession' in navigator) { try { navigator.mediaSession.playbackState = 'paused'; } catch (e) { /* ignore */ } }
    }

    // ── native element event handlers (bound once to both elements) ──
    function _onTimeUpdate(e) {
      const el = e.target;
      const d = el.duration;
      _mpProgress.set({ position: el.currentTime || 0, duration: Number.isFinite(d) ? d : 0, buffered: _mpBufferedEnd(el) });
      const t = curTrackRef.current;
      if (!t) return;
      posAccRef.current = { key: t.key, t: el.currentTime || 0, d: Number.isFinite(d) ? d : 0 };
      if (Date.now() - lastFlushRef.current > _MP_FLUSH_MS) flushPos();
    }
    function _onLoaded(e) {
      const el = e.target;
      errSkipRef.current = 0;
      const ps = pendingSeekRef.current;
      if (ps && ps.seq === loadSeqRef.current) {
        const d = el.duration;
        const t = ps.t;
        if (Number.isFinite(d) && t > _MP_MIN_RESUME && t < d - _MP_MIN_RESUME) {
          try { el.currentTime = Math.min(t, d - 0.25); } catch (e2) { /* InvalidState — skip */ }
        }
        pendingSeekRef.current = null;
      }
      try { el.playbackRate = prefsRef.current.speed; } catch (e2) { /* ignore */ }
      const d2 = el.duration;
      _mpProgress.set({ position: el.currentTime || 0, duration: Number.isFinite(d2) ? d2 : 0, buffered: _mpBufferedEnd(el) });
    }
    function _onPlay() { dispatch({ type: 'SET_PLAYING', value: true }); if ('mediaSession' in navigator) { try { navigator.mediaSession.playbackState = 'playing'; } catch (e) { /* ignore */ } } }
    function _onPause(e) {
      dispatch({ type: 'SET_PLAYING', value: false });
      if (e && e.target === activeEl()) flushPos();
      if ('mediaSession' in navigator) { try { navigator.mediaSession.playbackState = 'paused'; } catch (er) { /* ignore */ } }
      // Media is no longer the audible owner once paused. release() only clears
      // `active` if media held it; a bus-pause is immediately followed by the new
      // source's requestActive(), so the ordering is safe.
      if (window.HubAudioBus) window.HubAudioBus.release('media');
    }
    function _onEnded() {
      flushPos({ reset: true });
      const trk = curTrackRef.current;
      const q = curQueueRef.current;
      if (prefsRef.current.autoplayNext && trk) {
        const nx = findNext(q, trk, 1);
        if (nx) { play(nx.track, { queue: q.items, index: nx.index }); return; }
      }
      dispatch({ type: 'SET_PLAYING', value: false });
      if ('mediaSession' in navigator) { try { navigator.mediaSession.playbackState = 'none'; } catch (e) { /* ignore */ } }
    }
    function _onError(e) {
      // Only react to failures of the ACTIVE element. play()/stop() clear the
      // other (or both) elements via removeAttribute('src')+load(); any error
      // those surface must not trigger a spurious skip or a toast on Close.
      if (e && e.target !== activeEl()) return;
      const trk = curTrackRef.current;
      const q = curQueueRef.current;
      // Bounded skip so a run of 404s can't storm; otherwise stop + toast once.
      if (prefsRef.current.autoplayNext && trk && errSkipRef.current < 3) {
        errSkipRef.current += 1;
        const nx = findNext(q, trk, 1);
        if (nx) { play(nx.track, { queue: q.items, index: nx.index, fromError: true }); return; }
      }
      toast('Nie można odtworzyć mediów', 'err');
      dispatch({ type: 'SET_PLAYING', value: false });
    }
    function _onPipEnter() { dispatch({ type: 'SET_PIP', value: true }); }
    function _onPipLeave() { dispatch({ type: 'SET_PIP', value: false }); }

    return {
      play, toggle, pause, resume, seek, seekBy, setSpeed, cycleSpeed,
      setAutoplayNext, toggleAutoplayNext, setSkipSeconds, skipBack, skipForward,
      next, prev, togglePip, enterFullscreen,
      enqueue, removeAt, reorder, jumpTo,
      stop, isCurrent, adoptVideo, releaseVideo, reportKeyboard, dismissBlocked, getState, restoreNow,
      _onTimeUpdate, _onLoaded, _onPlay, _onPause, _onEnded, _onError, _onPipEnter, _onPipLeave,
      _flush: () => flushPos(),
    };
  }, []);

  // Create the two persistent elements + bind listeners exactly once.
  _mpEffect(() => {
    const audio = document.createElement('audio');
    audio.preload = 'metadata';
    const video = document.createElement('video');
    video.preload = 'metadata';
    video.playsInline = true;
    video.setAttribute('playsinline', '');
    video.style.width = '1px';
    video.style.height = '1px';
    video.style.opacity = '0';
    video.style.background = '#000';
    audioRef.current = audio;
    videoRef.current = video;
    const g = garageRef.current;
    if (g) { g.appendChild(audio); g.appendChild(video); }

    const evts = [
      ['timeupdate', api._onTimeUpdate], ['loadedmetadata', api._onLoaded], ['durationchange', api._onLoaded],
      ['play', api._onPlay], ['pause', api._onPause], ['ended', api._onEnded], ['error', api._onError],
    ];
    const bind = (el) => evts.forEach(([n, h]) => el.addEventListener(n, h));
    const unbind = (el) => evts.forEach(([n, h]) => el.removeEventListener(n, h));
    bind(audio);
    bind(video);
    video.addEventListener('enterpictureinpicture', api._onPipEnter);
    video.addEventListener('leavepictureinpicture', api._onPipLeave);

    const onFs = () => dispatch({ type: 'SET_FS', value: !!document.fullscreenElement });
    document.addEventListener('fullscreenchange', onFs);
    const onVis = () => { if (document.visibilityState === 'hidden') api._flush(); };
    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('pagehide', api._flush);

    _mpGcOldPositions();
    window.HubPlayer = api;
    // Register on the cross-subsystem audio bus so TTS / conversation can pause us.
    const _busUnreg = window.HubAudioBus && window.HubAudioBus.register('media', {
      kind: 'media',
      pause: () => { try { api.pause(); } catch (e) { /* ignore */ } },
    });
    api.restoreNow(); // re-show the last-played track (paused) after a reload

    return () => {
      unbind(audio);
      unbind(video);
      video.removeEventListener('enterpictureinpicture', api._onPipEnter);
      video.removeEventListener('leavepictureinpicture', api._onPipLeave);
      document.removeEventListener('fullscreenchange', onFs);
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('pagehide', api._flush);
      _busUnreg && _busUnreg();
      // Remove the created elements too, so a re-run (e.g. StrictMode double-invoke
      // if ever enabled) starts clean instead of orphaning a pair in the garage.
      try { audio.remove(); video.remove(); } catch (e) { /* ignore */ }
    };
  }, []);

  const value = _mpMemo(() => ({ ...state, ...api }), [state, api]);

  return (
    <_MediaCtx.Provider value={value}>
      <div ref={garageRef} aria-hidden="true" style={{ position: 'fixed', right: 0, bottom: 0, width: 1, height: 1, overflow: 'hidden', opacity: 0, pointerEvents: 'none', zIndex: -1 }} />
      {children}
    </_MediaCtx.Provider>
  );
}

function useMediaPlayer() { return _mpCtxHook(_MediaCtx); }

Object.assign(window, {
  MediaPlayerProvider, useMediaPlayer, usePlayerProgress,
  makeMediaTrackKey, MEDIA_SPEED_STEPS,
});
