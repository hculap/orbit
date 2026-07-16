// Continuous voice conversation mode — fullscreen "Tryb rozmowy" overlay
// for hands-free chat (designed for in-car use). One tap to enter; mic +
// VAD + STT + send + TTS + barge-in run in a loop until the user taps X.
//
// Pipeline reused, not reimplemented:
//   - Mic + VAD + STT chunked POST: window.useVoiceCapture (orchestrator-voice.jsx)
//   - TTS chunking + voice picker: window.stripMarkdownForSpeech +
//     window.chunkForSpeech (orchestrator-tts.jsx)
//   - AEC reference path for `<audio>` TTS: window.createAecLoopback
//     (orchestrator-aec-loopback.jsx) — see that file for the WebRTC trick.
//
// State machine (driven by stateRef so callbacks fired outside React
// commits — VAD timer, SSE handlers — see consistent values):
//
//   idle ─tap─▶ armed ─prime─▶ listening ─VAD─▶ transcribing ─text─▶
//     sending ─POST─▶ thinking ─finalize─▶ speaking ─tts.ended─▶
//     cooldown ─250ms─▶ listening (loop)
//
//     speaking ─barge─▶ interrupting ─cancel─▶ cooldown ─▶ listening
//     * ─X─▶ exit ─cleanup─▶ idle
//
// Published as window.useConversation + window.ConversationModal.
//
// BUILD VERSION marker — bump when shipping fixes so the user can verify
// in the Safari Web Inspector console which bundle is actually loaded
// (PWA service workers cache aggressively on iOS). Look for this string
// in the console at modal load time.
const _CONV_BUILD = 'conv-2026-05-04-webrtc-stream';
if (typeof window !== 'undefined') {
  // eslint-disable-next-line no-console
  console.info('[conv] loaded build', _CONV_BUILD);
}

const {
  useState: _cUseState,
  useEffect: _cUseEffect,
  useRef: _cUseRef,
  useCallback: _cUseCallback,
  useMemo: _cUseMemo,
} = React;

const _CONV_STATE = Object.freeze({
  IDLE: 'idle',
  ARMED: 'armed',
  LISTENING: 'listening',
  TRANSCRIBING: 'transcribing',
  SENDING: 'sending',
  THINKING: 'thinking',
  SPEAKING: 'speaking',
  INTERRUPTING: 'interrupting',
  COOLDOWN: 'cooldown',
  ERROR: 'error',
});

const _CONV_STATE_LABELS = {
  idle: '',
  armed: 'Uruchamiam mikrofon...',
  listening: 'Słucham...',
  transcribing: 'Rozumiem...',
  sending: 'Wysyłam...',
  thinking: 'Myślę...',
  speaking: 'Mówię...',
  interrupting: 'Przerywam...',
  cooldown: '...',
  error: 'Błąd',
};

// Grace window after TTS ends before the mic re-arms. Covers AEC tail in the
// speaker buffer + the speechSynthesis.cancel() async settle on Safari. 250ms
// is short enough to feel snappy and long enough to avoid self-trigger.
const _CONV_COOLDOWN_MS = 250;

// ── terminalMode (ttyd / driving) tunables ─────────────────────────────
// Keep-alive while claude thinks silently: first cue, then cadence.
const _CONV_KEEPALIVE_FIRST_MS = 12_000;
const _CONV_KEEPALIVE_EVERY_MS = 20_000;
// Release the mic after this long in THINKING so the driver can keep talking
// while claude grinds (the send-gate still protects against pasting into a
// picker — a queued utterance only sends once the turn proves generating).
const _CONV_RELEASE_MS = 50_000;
// No block AND no end_flow this long after a send → likely an interactive
// picker / permission prompt the voice loop cannot answer. Spoken fallback.
const _CONV_PICKER_FALLBACK_MS = 165_000;
// Re-send the full voice-protocol block every N turns (compaction insurance).
const _CONV_HINT_REFRESH_TURNS = 8;
// Send-gate stall window: a turn that HAS produced blocks counts as "proven
// generating" only while a block arrived this recently — after prose, claude
// can still park on a permission prompt / picker, where our paste+\r would
// confirm the highlighted option. Stale activity → queue the utterance.
const _CONV_SEND_STALL_MS = 12_000;
// Queued (send-gated) utterances older than this are dropped at flush time —
// auto-pasting a stale command minutes later is worse than asking again.
const _CONV_PENDING_SEND_TTL_MS = 5 * 60_000;
// Phantom in-flight decay: a missed end_flow (phone locked through the turn
// boundary — speak frames don't replay) would otherwise hold the send-gate
// forever. After this long with zero turn activity, reset the counter and
// tell the driver to glance at the screen when convenient.
const _CONV_INFLIGHT_DECAY_MS = 10 * 60_000;
// Drain-poll: the mic re-arms only after this many CONSECUTIVE "not speaking"
// reads 250ms apart — a single false can be the gap between turn A's drained
// controller and turn B's first block.
const _CONV_DRAIN_POLL_MS = 250;
const _CONV_DRAIN_POLL_QUIET_READS = 2;
// SPEAKING watchdog: cancel playback after this much time with NO activity
// (activity = any feedSpeak). Async narration can legally exceed any fixed
// per-state ceiling, so the watchdog is inactivity-based.
const _CONV_SPEAK_IDLE_MAX_MS = 240_000;
// Mic capture cap while driving — a noisy-cabin recording that never
// auto-stops must not sit unsent for a minute.
const _CONV_TERMINAL_MAX_RECORDING_MS = 20_000;

// ~1s near-silent WAV (quiet 100 Hz hum at ~2% amplitude) for the Now Playing
// anchor: a looping, NOT-muted <audio> that holds the page's media session so
// steering-wheel keys reach us instead of Spotify during long LISTENING gaps.
// Muted/zero audio fails iOS "is audible" heuristics — hence the faint hum at
// element volume ~0.04 (inaudible over cabin noise).
function _convSilentLoopUrl() {
  const rate = 8000;
  const n = rate;  // 1s
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const wstr = (off, s) => { for (let i = 0; i < s.length; i += 1) v.setUint8(off + i, s.charCodeAt(i)); };
  wstr(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); wstr(8, 'WAVE');
  wstr(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  wstr(36, 'data'); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i += 1) {
    v.setInt16(44 + i * 2, Math.sin(2 * Math.PI * 100 * (i / rate)) * 0.02 * 32767, true);
  }
  return URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
}
// Ignore mic energy for the first 200ms after entering 'speaking' — TTS
// onset ramp can spike RMS even with AEC engaged.
const _CONV_BARGE_ARM_GRACE_MS = 200;
const _CONV_BARGE_TICK_MS = 50;

// Build the TTS streaming URL — same shape as orchestrator-tts.jsx's
// internal _buildRemoteUrl, duplicated here to keep the conversation hook
// self-contained.
function _convTtsUrl(text, engine, voice) {
  const base = (typeof window !== 'undefined' && typeof window.HUB_BASE_PATH === 'string')
    ? window.HUB_BASE_PATH : '';
  const params = new URLSearchParams({ text, engine });
  if (voice) params.set('voice', voice);
  return base + '/api/orchestrator/tts?' + params.toString();
}

// Web Audio playback. iOS PWA standalone-mode rejects HTMLAudioElement
// `.play()` calls that happen outside a user-gesture window — even with a
// shared element + src swap, iOS still blocks the second-onwards play
// when the gesture is more than ~50 ms in the past. Conversation TTS fires
// 5-15 s after the modal-open click, so HTMLAudioElement cannot work.
//
// Web Audio API does NOT have the per-call gesture requirement: once the
// AudioContext has been resumed inside ANY user gesture, subsequent
// AudioBufferSourceNode.start() calls play freely. We resume() the
// context inside `start()` (modal-open click handler) and then play each
// chunk via fetch → decodeAudioData → BufferSource.start().
//
// Trade-off: have to download the full chunk before playing (no streaming
// like with `<audio>`), so TTFA per chunk is ~250 ms ElevenLabs Flash +
// transfer time. For Polish prose chunks (typically 30-100 chars) the
// chunks are small (~50-150 KB MP3). The N+1 lookahead pattern from the
// HTMLAudioElement version is preserved here — fetch chunk N+1 while
// playing chunk N.
async function _startConvPlayback(chunks, engine, voice, audioCtx, destination, onDone) {
  let cancelled = false;
  let currentSource = null;
  let nextBufferPromise = null;
  const queue = Array.isArray(chunks) ? [...chunks] : [];

  // Decode a single chunk URL to an AudioBuffer.
  const fetchAndDecode = async (url) => {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error('fetch ' + r.status);
    const buf = await r.arrayBuffer();
    // Safari only supports the callback-style decodeAudioData; modern
    // browsers also accept it. Wrap to a Promise either way.
    return new Promise((resolve, reject) => {
      try {
        const p = audioCtx.decodeAudioData(buf, resolve, reject);
        if (p && typeof p.then === 'function') p.then(resolve).catch(reject);
      } catch (e) { reject(e); }
    });
  };

  const playAt = async (idx) => {
    if (cancelled) return;
    if (idx >= queue.length) {
      try { if (onDone) onDone(); } catch (e) { /* swallow */ }
      return;
    }
    let buffer;
    try {
      // Use the pre-fetched lookahead if it matches this idx; otherwise
      // fetch fresh.
      if (nextBufferPromise && nextBufferPromise.idx === idx) {
        buffer = await nextBufferPromise.promise;
      } else {
        buffer = await fetchAndDecode(_convTtsUrl(queue[idx], engine, voice));
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('[conv] WebAudio chunk', idx, 'failed:', e && e.message);
      // Skip to next chunk so one bad URL doesn't kill the whole reply.
      nextBufferPromise = null;
      playAt(idx + 1);
      return;
    }
    if (cancelled) return;

    // Pre-fetch chunk idx+1 while current plays.
    if (idx + 1 < queue.length) {
      const nextIdx = idx + 1;
      nextBufferPromise = {
        idx: nextIdx,
        promise: fetchAndDecode(_convTtsUrl(queue[nextIdx], engine, voice)).catch(() => null),
      };
    } else {
      nextBufferPromise = null;
    }

    try {
      const source = audioCtx.createBufferSource();
      source.buffer = buffer;
      source.connect(destination);
      source.onended = () => {
        if (currentSource === source) currentSource = null;
        playAt(idx + 1);
      };
      currentSource = source;
      source.start(0);
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn('[conv] BufferSource.start failed:', e && e.message);
      playAt(idx + 1);
    }
  };

  // Kick off async — caller doesn't await.
  playAt(0);

  return {
    cancel: () => {
      cancelled = true;
      if (currentSource) {
        try { currentSource.stop(); } catch (e) { /* already stopped */ }
        try { currentSource.disconnect(); } catch (e) {}
        currentSource = null;
      }
      nextBufferPromise = null;
    },
  };
}

// Dedicated barge-in monitor: a fresh getUserMedia stream + AnalyserNode
// active only during 'speaking' (when voiceConvEcho === 'aec'). Doesn't
// touch useVoiceCapture's lifecycle — fully independent. Returns a `close`
// fn for teardown.
async function _startBargeMonitor({ thresholdRms, minDurationMs, onBarge }) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) throw new Error('AudioContext unsupported');
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1, sampleRate: 16000 },
  });
  const ctx = new Ctx();
  if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) {} }
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0;
  source.connect(analyser);

  const armedAt = performance.now();
  let accumMs = 0;
  let cancelled = false;
  const buf = new Uint8Array(analyser.fftSize);

  const tick = () => {
    if (cancelled) return;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i += 1) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    const elapsed = performance.now() - armedAt;
    if (elapsed < _CONV_BARGE_ARM_GRACE_MS) {
      // Anti-onset-ramp grace.
      accumMs = 0;
    } else if (rms > thresholdRms) {
      accumMs += _CONV_BARGE_TICK_MS;
      if (accumMs >= minDurationMs) {
        cancelled = true;
        try { onBarge(rms); } catch (e) { /* swallow */ }
        return;
      }
    } else {
      accumMs = 0;
    }
  };
  const timer = setInterval(tick, _CONV_BARGE_TICK_MS);

  return {
    close: () => {
      cancelled = true;
      try { clearInterval(timer); } catch (e) {}
      try { source.disconnect(); } catch (e) {}
      try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
      try { ctx.close(); } catch (e) {}
    },
  };
}

function useConversation(opts) {
  const o = opts || {};
  const send = o.send;
  const onCancelStream = o.onCancelStream;
  const isStreaming = !!o.isStreaming;
  const messages = _cUseMemo(() => Array.isArray(o.messages) ? o.messages : [], [o.messages]);
  // Conversation mode forces 'pl' when the user has 'auto' selected —
  // car-mode background noise + short utterances drift Whisper to English
  // far too often when left in auto-detect. Explicit non-'auto' values
  // (e.g. 'en' if they really want English) pass through.
  const language = (o.language && o.language !== 'auto') ? o.language : 'pl';
  const echoMode = o.echoMode || 'mute';
  const silenceMs = Number.isFinite(o.silenceMs) ? o.silenceMs : 1200;
  const bargeThreshold = Number.isFinite(o.bargeThreshold) ? o.bargeThreshold : 0.045;
  const bargeMinDurationMs = Number.isFinite(o.bargeMinDurationMs) ? o.bargeMinDurationMs : 120;
  const ttsEngine = o.ttsEngine || 'elevenlabs';
  const ttsVoice = o.ttsVoice || null;
  const ttsUnlock = o.ttsUnlock;
  const cancelTtsExternal = o.cancelTtsExternal;
  const speakBlocksFallback = o.speakBlocksFallback;
  const ttsSupported = !!o.ttsSupported;
  const markAssistantSpoken = o.markAssistantSpoken;
  const _msgKey = o.msgKey || ((m) => (m && m.idx != null) ? (String(m.idx) + ':' + m.role) : null);

  // ── terminalMode (ttyd / driving) opts — all consumed via render-updated
  // refs so the stable feedSpeak/feedTurnEnd callbacks never go stale. ──
  const terminalModeRef = _cUseRef(false);
  terminalModeRef.current = !!o.terminalMode;
  const sendToTerminalRef = _cUseRef(null);
  sendToTerminalRef.current = o.sendToTerminal || null;
  const getTtsSpeakingRef = _cUseRef(null);
  getTtsSpeakingRef.current = o.getTtsSpeaking || null;
  // Speak a SELF-GENERATED phrase through the same flow-key queue as turn
  // blocks (streamChunk + endStream) — the only safe audio path: it can never
  // play into an open mic because every caller routes through the same
  // SPEAKING → drain → COOLDOWN machinery as real turn audio.
  const speakTextRef = _cUseRef(null);
  speakTextRef.current = o.speakText || null;
  const readAloudAvailableRef = _cUseRef(true);
  readAloudAvailableRef.current = o.readAloudAvailable !== false;
  const cancelTtsExternalRef = _cUseRef(null);
  cancelTtsExternalRef.current = cancelTtsExternal || null;

  const [active, setActive] = _cUseState(false);
  const [state, _setState] = _cUseState(_CONV_STATE.IDLE);
  const [error, setError] = _cUseState(null);
  const [lastUserText, setLastUserText] = _cUseState('');

  const stateRef = _cUseRef(_CONV_STATE.IDLE);
  const setState = _cUseCallback((s) => {
    stateRef.current = s;
    _setState(s);
  }, []);

  // Refs for resources that live across renders.
  const loopbackRef = _cUseRef(null);
  const playbackRef = _cUseRef(null);
  const bargeMonRef = _cUseRef(null);
  const cooldownRef = _cUseRef(null);
  const lastSpokenKeyRef = _cUseRef(null);
  const browserUtterRef = _cUseRef(null);
  // Shared AudioContext for chunk playback. Created (and resumed) inside
  // start() so it lives within the user-gesture window — Web Audio
  // .start() then plays freely on iOS PWA without per-call gesture.
  const audioCtxRef = _cUseRef(null);
  // MediaStreamDestination + HTMLAudioElement routing. iOS PWA-critical:
  // routing Web Audio through a MediaStream and then to an <audio> with
  // srcObject (a) bypasses the autoplay gesture restriction (iOS treats
  // it like an incoming WebRTC call — allowed) AND (b) ignores the silent
  // switch on the side ringer (the silent switch mutes Web Audio output
  // but does NOT mute HTMLAudioElement playback).
  const audioDestRef = _cUseRef(null);
  const audioOutRef = _cUseRef(null);
  // Voice-mode hint is sent ONLY on the first turn of a modal session —
  // claude has it in context for subsequent turns and we avoid bloating
  // every prompt + reducing the chance the model gets stuck repeating
  // meta-responses ("Jasne, słyszę cię") instead of answering the query.
  const firstTurnRef = _cUseRef(true);

  // ── terminalMode loop state ──
  const activeRef = _cUseRef(false);
  const vcRef = _cUseRef(null);
  // Turns we sent that have not yet seen their end_flow. feedTurnEnd re-arms
  // the mic only at 0 — a stale end_flow from an earlier async turn must not
  // yank the machine out of THINKING for a newer send.
  const inFlightRef = _cUseRef(0);
  // Send-gate: proof the in-flight turn is GENERATING (≥1 block since the
  // send). Without it the turn may be parked on an interactive picker /
  // permission prompt — pasting + \r would answer the picker (hard user rule:
  // never auto-press keys). Utterances captured then are queued instead.
  const hadSpeakSinceSendRef = _cUseRef(false);
  const pendingSendsRef = _cUseRef([]);
  // Deferred play closures: an async-result block landed while the user was
  // mid-sentence — audio waits its turn, flushed after onFinal.
  const pendingAudioRef = _cUseRef([]);
  // Barge: drop the rest of the CURRENT turn's blocks. Set only when the
  // barged turn's end_flow has NOT yet arrived (JSONL turns are sequential,
  // so the next feedTurnEnd is necessarily the barged turn's → clears it).
  const suppressRef = _cUseRef(false);
  // Set while the browser-engine path is intentionally handing this reply off to
  // the tts hook. tts.speakBlocks claims the bus ('tts'), which would otherwise
  // pause our own 'conv' source and cancel the speech we just started. The conv
  // pause callback no-ops while this is true (we ARE the source driving tts).
  const _delegatingToTtsRef = _cUseRef(false);
  const drainPollRef = _cUseRef(null);
  // Forward ref: _startDrainPoll (defined first) flushes gated sends at drain
  // completion; _flushPendingSends is defined later in the chain.
  const _flushPendingSendsRef = _cUseRef(null);
  // 'auto' → after drain go THINKING (turn in flight) or LISTENING; 'listen'
  // → force LISTENING (the release path's whole point is freeing the mic
  // while a turn still runs).
  const drainTargetRef = _cUseRef('auto');
  const turnCountRef = _cUseRef(0);
  const keepAliveTimerRef = _cUseRef(null);
  const pickerTimerRef = _cUseRef(null);
  const lastSpeakActivityRef = _cUseRef(0);
  const lastSendAtRef = _cUseRef(0);
  // True while handleFinal runs synchronously inside vc's onFinal: the vc
  // state still reads 'finalizing' there, but the capture is DONE — a system
  // phrase spoken from these paths must PLAY, not defer into pendingAudioRef
  // (no future onFinal would ever flush it → permanent TRANSCRIBING wedge).
  const inFinalRef = _cUseRef(false);
  const errorCountRef = _cUseRef(0);
  const wakeLockRef = _cUseRef(null);
  const anchorRef = _cUseRef(null);
  const anchorUrlRef = _cUseRef(null);

  // Stable handler refs so the vc hook doesn't re-instantiate on each render.
  const handleFinalRef = _cUseRef(null);
  const handleErrorRef = _cUseRef(null);

  // useVoiceCapture is always allocated; resources only acquired on start().
  // chunkMs=0 disables mid-recording requestData() so the final POST is a
  // single contiguous webm container (one header), not a concatenation of
  // multiple requestData chunks. Whisper rejects the multi-header blob as
  // "audio too short" — see voice.jsx note for context.
  const vc = window.useVoiceCapture({
    onFinal: (t) => { if (handleFinalRef.current) handleFinalRef.current(t); },
    onError: (m) => { if (handleErrorRef.current) handleErrorRef.current(m); },
    onPartial: () => { /* not used in conversation mode */ },
    language,
    silenceMs,
    chunkMs: 0,
    // Driving: a noisy-cabin recording that never auto-stops must not sit
    // unsent for a minute; 20s is plenty for one spoken command.
    maxRecordingMs: o.terminalMode ? _CONV_TERMINAL_MAX_RECORDING_MS : 60_000,
    // NO preferredDeviceId: in-car test showed an exact-device pin makes iOS
    // hijack the output route to the phone speaker, while DEFAULT constraints
    // already use the built-in mic AND keep A2DP in the car (see the disabled
    // pinning-effect comment below).
  });
  vcRef.current = vc;

  // Mirror vc.state into a ref so callbacks scheduled via setTimeout (which
  // capture stale closures) can read the LATEST vc.state — not the snapshot
  // from the render that scheduled them. Without this, the cooldown re-arm
  // got stuck reading vc.state='finalizing' from the closure long after vc
  // had actually settled to 'idle'.
  const vcStateRef = _cUseRef(vc.state);
  _cUseEffect(() => { vcStateRef.current = vc.state; }, [vc.state]);

  // Schedule re-arm by transitioning to LISTENING after the grace; the
  // useEffect below actually drives vc.start() once vc reports 'idle'. We
  // don't poll vc.state here because the timer's closure can't see fresh
  // state without ref-mirroring (see vcStateRef).
  const _scheduleListen = _cUseCallback(() => {
    if (cooldownRef.current) clearTimeout(cooldownRef.current);
    cooldownRef.current = setTimeout(() => {
      cooldownRef.current = null;
      if (stateRef.current !== _CONV_STATE.COOLDOWN) return;
      setState(_CONV_STATE.LISTENING);
    }, _CONV_COOLDOWN_MS);
  }, [setState]);

  // ════ terminalMode (ttyd / driving) core ════════════════════════════
  // All callbacks below are STABLE (deps on setState/_scheduleListen only,
  // everything else via render-updated refs) so handleSpeakEvent can hold
  // them through a conversationRef without identity churn.

  const _stopDrainPoll = _cUseCallback(() => {
    if (drainPollRef.current) { clearInterval(drainPollRef.current); drainPollRef.current = null; }
  }, []);

  // Wait for the shared TTS queue to fully drain (N consecutive quiet reads —
  // a single false can be the gap between turn A's finished controller and
  // turn B's first block), then route: forced 'listen', else THINKING when a
  // sent turn is still in flight, else COOLDOWN → LISTENING. The mic NEVER
  // arms except through this path, so audio can't play into an open mic.
  const _startDrainPoll = _cUseCallback(() => {
    if (drainPollRef.current) return;  // idempotent (A's and B's end_flow may both call)
    let quiet = 0;
    drainPollRef.current = setInterval(() => {
      if (stateRef.current !== _CONV_STATE.SPEAKING) { _stopDrainPoll(); return; }
      const speaking = !!(getTtsSpeakingRef.current && getTtsSpeakingRef.current());
      if (speaking) { quiet = 0; return; }
      quiet += 1;
      if (quiet < _CONV_DRAIN_POLL_QUIET_READS) return;
      _stopDrainPoll();
      const forceListen = drainTargetRef.current === 'listen';
      drainTargetRef.current = 'auto';
      if (!forceListen && inFlightRef.current > 0) {
        setState(_CONV_STATE.THINKING);  // a sent turn still runs → keep mic shut
        return;
      }
      // end_flow commonly lands while SPEAKING (the tail still drains) — this
      // completion is where the turn truly ends, so gated sends flush HERE.
      if (inFlightRef.current === 0 && _flushPendingSendsRef.current && _flushPendingSendsRef.current()) return;
      setState(_CONV_STATE.COOLDOWN);
      _scheduleListen();
    }, _CONV_DRAIN_POLL_MS);
  }, [setState, _scheduleListen, _stopDrainPoll]);

  const _clearThinkingTimers = _cUseCallback(() => {
    if (keepAliveTimerRef.current) { clearTimeout(keepAliveTimerRef.current); keepAliveTimerRef.current = null; }
  }, []);

  // Route ONE piece of audio (a turn block OR a self-spoken system phrase)
  // per the state matrix. `play` is a closure that queues the audio on the
  // shared flow key. Returns 'played' | 'deferred'.
  const _routeAudio = _cUseCallback((play) => {
    const st = stateRef.current;
    if (st === _CONV_STATE.LISTENING || st === _CONV_STATE.TRANSCRIBING) {
      const vcs = vcRef.current ? vcRef.current.state : 'idle';
      const speaking = vcRef.current && typeof vcRef.current.hasSpeech === 'function' && vcRef.current.hasSpeech();
      if (vcs === 'recording' && !speaking) {
        // Mic open but the user is silent — nothing of value recorded.
        // Discard the capture (no POST, no onFinal) and play immediately.
        try { vcRef.current.abort(); } catch (e) { /* ignore */ }
        try { play(); } catch (e) { /* ignore */ }
        setState(_CONV_STATE.SPEAKING);
        return 'played';
      }
      if (vcs === 'recording' || (vcs === 'finalizing' && !inFinalRef.current)) {
        // User mid-sentence (or utterance still finalizing) — defer: the
        // captured audio predates this TTS so the STT stays clean; audio
        // flushes right after onFinal. Bounded by maxRecordingMs. NOT taken
        // from inside onFinal itself (inFinalRef) — that flush already ran.
        pendingAudioRef.current.push(play);
        return 'deferred';
      }
      // Mic not actually open yet (gap before vc.start fires) → invalidate
      // any IN-FLIGHT start first (abort bumps the epoch, so a getUserMedia
      // that resolves mid-playback drops its tracks instead of recording the
      // TTS), then play.
      if (vcs === 'idle' && vcRef.current && typeof vcRef.current.abort === 'function') {
        try { vcRef.current.abort(); } catch (e) { /* ignore */ }
      }
      try { play(); } catch (e) { /* ignore */ }
      setState(_CONV_STATE.SPEAKING);
      return 'played';
    }
    if (st === _CONV_STATE.COOLDOWN) {
      if (cooldownRef.current) { clearTimeout(cooldownRef.current); cooldownRef.current = null; }
      try { play(); } catch (e) { /* ignore */ }
      setState(_CONV_STATE.SPEAKING);
      return 'played';
    }
    if (st === _CONV_STATE.SPEAKING || st === _CONV_STATE.INTERRUPTING) {
      try { play(); } catch (e) { /* ignore */ }  // tts queues (append/reopen)
      return 'played';
    }
    if (st === _CONV_STATE.ERROR) {
      errorCountRef.current = 0;  // an arriving block = recovery signal
      setError(null);
      try { play(); } catch (e) { /* ignore */ }
      setState(_CONV_STATE.SPEAKING);
      return 'played';
    }
    // THINKING / SENDING / ARMED / anything unlisted → default arm: speak.
    _clearThinkingTimers();
    try { play(); } catch (e) { /* ignore */ }
    setState(_CONV_STATE.SPEAKING);
    return 'played';
  }, [setState, _clearThinkingTimers]);

  // Self-spoken phrase (status / error / release / resume). Routed through
  // the SAME matrix + drain machinery as turn audio — the one hard invariant
  // that keeps system phrases out of an open mic. forceListen: after the
  // phrase drains go to LISTENING even though a turn is still in flight
  // (the release path).
  const _speakSystem = _cUseCallback((text, opts2) => {
    const speak = speakTextRef.current;
    if (!speak) return;
    if (opts2 && opts2.forceListen) drainTargetRef.current = 'listen';
    _routeAudio(() => speak(text));
    if (stateRef.current === _CONV_STATE.SPEAKING) _startDrainPoll();
  }, [_routeAudio, _startDrainPoll]);

  // Picker fallback, re-armed on EVERY activity while a turn is in flight —
  // claude routinely narrates prose and THEN parks on a picker/permission
  // prompt, so a timer cleared on the first block would never warn for the
  // common mid-turn case.
  const _armPickerTimer = _cUseCallback(() => {
    if (pickerTimerRef.current) clearTimeout(pickerTimerRef.current);
    pickerTimerRef.current = setTimeout(() => {
      pickerTimerRef.current = null;
      if (!activeRef.current || inFlightRef.current <= 0) return;
      _speakSystem('Sesja czeka na wybór na ekranie. Zajmę się tym, gdy się zatrzymasz.', { forceListen: true });
    }, _CONV_PICKER_FALLBACK_MS);
  }, [_speakSystem]);

  // Block of a turn arrived (from handleSpeakEvent). suppress = barged turn.
  const feedSpeak = _cUseCallback((play) => {
    if (!terminalModeRef.current || !activeRef.current) { try { play(); } catch (e) { /* ignore */ } return; }
    if (suppressRef.current) return;  // rest of a barged turn → drop
    lastSpeakActivityRef.current = performance.now();
    hadSpeakSinceSendRef.current = true;  // send-gate: the turn is generating
    if (inFlightRef.current > 0) _armPickerTimer();  // re-arm: prose-THEN-picker is the common case
    else if (pickerTimerRef.current) { clearTimeout(pickerTimerRef.current); pickerTimerRef.current = null; }
    _clearThinkingTimers();
    _routeAudio(play);
  }, [_routeAudio, _clearThinkingTimers, _armPickerTimer]);

  // Flush queued sends (utterances held by the send-gate) once it's safe.
  // Goes straight to the sender (not handleFinal — that one gates on the mic
  // state and a flushed send can fire from COOLDOWN/THINKING). Entries carry
  // timestamps: auto-pasting a command queued MINUTES ago (picker resolved by
  // hand much later) is worse than asking again, so stale ones are dropped.
  const _sendVoiceTextRef = _cUseRef(null);
  const _flushPendingSends = _cUseCallback(() => {
    if (!pendingSendsRef.current.length) return false;
    const now = performance.now();
    const fresh = pendingSendsRef.current
      .filter((p) => now - p.at < _CONV_PENDING_SEND_TTL_MS)
      .map((p) => p.text);
    pendingSendsRef.current = [];
    if (!fresh.length) return false;
    // Announce — eyes-free, the driver must know a held command is going out.
    if (speakTextRef.current) { try { speakTextRef.current('Wysyłam zaległe polecenie.'); } catch (e) { /* ignore */ } }
    if (_sendVoiceTextRef.current) _sendVoiceTextRef.current(fresh.join('\n'));
    return true;
  }, []);
  _flushPendingSendsRef.current = _flushPendingSends;

  // end_flow arrived. finalize = closure ending the flow-key stream.
  const feedTurnEnd = _cUseCallback((finalize) => {
    if (!terminalModeRef.current || !activeRef.current) { try { finalize(); } catch (e) { /* ignore */ } return; }
    suppressRef.current = false;  // the barged turn is bounded by its own end
    inFlightRef.current = Math.max(0, inFlightRef.current - 1);
    if (pickerTimerRef.current) { clearTimeout(pickerTimerRef.current); pickerTimerRef.current = null; }
    const st = stateRef.current;
    if ((st === _CONV_STATE.LISTENING || st === _CONV_STATE.TRANSCRIBING)
        && pendingAudioRef.current.length) {
      pendingAudioRef.current.push(finalize);  // keep ordering with deferred plays
      return;
    }
    try { finalize(); } catch (e) { /* ignore */ }
    if (st === _CONV_STATE.SPEAKING) { _startDrainPoll(); return; }
    if (st === _CONV_STATE.THINKING) {
      // Zero-prose turn (tool-only) or all audio already drained: route via a
      // brief SPEAKING so the standard drain machinery decides what's next.
      if (inFlightRef.current === 0 && _flushPendingSends()) return;
      if (inFlightRef.current === 0) { setState(_CONV_STATE.COOLDOWN); _scheduleListen(); }
      // else: another sent turn still runs → stay THINKING (timers re-arm via effect)
      return;
    }
    // LISTENING/COOLDOWN etc. with no pending audio → nothing else to do; a
    // queued send may now be safe.
    if (inFlightRef.current === 0) _flushPendingSends();
  }, [setState, _scheduleListen, _startDrainPoll, _flushPendingSends]);

  // Flush deferred audio (plays + the queued finalize marker) after the
  // user's utterance resolved. Ends in SPEAKING + drain-poll.
  const _flushPendingAudio = _cUseCallback(() => {
    const items = pendingAudioRef.current;
    if (!items.length) return false;
    pendingAudioRef.current = [];
    items.forEach((fn) => { try { fn(); } catch (e) { /* ignore */ } });
    setState(_CONV_STATE.SPEAKING);
    _startDrainPoll();
    return true;
  }, [setState, _startDrainPoll]);

  const _acquireWakeLock = _cUseCallback(() => {
    try {
      if (!navigator.wakeLock || !navigator.wakeLock.request) return;
      navigator.wakeLock.request('screen')
        .then((s) => { wakeLockRef.current = s; })
        .catch(() => { /* iOS may refuse outside gesture — visibility re-tries */ });
    } catch (e) { /* unsupported */ }
  }, []);

  const _releaseWakeLock = _cUseCallback(() => {
    const s = wakeLockRef.current;
    wakeLockRef.current = null;
    if (s) { try { s.release(); } catch (e) { /* ignore */ } }
  }, []);

  // Now Playing anchor + steering-wheel keys. pause/nexttrack → barge (only
  // meaningful in SPEAKING — see barge()); play → recovery re-arm from ERROR.
  // iOS sometimes pauses the anchor element itself instead of (or besides)
  // firing the action handler → the element listener resumes it.
  const _stopAnchor = _cUseCallback(() => {
    const a = anchorRef.current;
    anchorRef.current = null;
    if (a) {
      try { a.pause(); } catch (e) { /* ignore */ }
      try { a.removeAttribute('src'); a.load(); } catch (e) { /* ignore */ }
    }
    if (anchorUrlRef.current) { try { URL.revokeObjectURL(anchorUrlRef.current); } catch (e) { /* ignore */ } anchorUrlRef.current = null; }
    if ('mediaSession' in navigator) {
      try { navigator.mediaSession.metadata = null; navigator.mediaSession.playbackState = 'none'; } catch (e) { /* ignore */ }
      ['play', 'pause', 'nexttrack', 'previoustrack'].forEach((ac) => {
        try { navigator.mediaSession.setActionHandler(ac, null); } catch (e) { /* ignore */ }
      });
    }
  }, []);

  const bargeRef = _cUseRef(null);  // forward ref — barge() defined below
  const _startAnchor = _cUseCallback(() => {
    if (anchorRef.current) return;
    try {
      const url = _convSilentLoopUrl();
      anchorUrlRef.current = url;
      const a = new Audio(url);
      a.loop = true;
      a.volume = 0.04;
      const p = a.play();
      if (p && typeof p.catch === 'function') p.catch(() => { /* gesture race — best-effort */ });
      a.addEventListener('pause', () => {
        // System paused the element (interruption / steering key). Resume so
        // the page keeps Now Playing — barging is the action handler's job.
        if (activeRef.current && anchorRef.current === a) {
          setTimeout(() => {
            if (activeRef.current && anchorRef.current === a) { try { a.play(); } catch (e) { /* ignore */ } }
          }, 300);
        }
      });
      anchorRef.current = a;
      if ('mediaSession' in navigator) {
        try {
          const MM = window.MediaMetadata;
          if (MM) navigator.mediaSession.metadata = new MM({ title: 'Tryb rozmowy — Hub', artist: 'Claude' });
          navigator.mediaSession.playbackState = 'playing';
        } catch (e) { /* ignore */ }
        const set = (ac, h) => { try { navigator.mediaSession.setActionHandler(ac, h); } catch (e) { /* ignore */ } };
        const reassert = () => {
          try { if (anchorRef.current) anchorRef.current.play(); } catch (e) { /* ignore */ }
          try { navigator.mediaSession.playbackState = 'playing'; } catch (e) { /* ignore */ }
        };
        set('pause', () => { if (bargeRef.current) bargeRef.current(); reassert(); });
        set('nexttrack', () => { if (bargeRef.current) bargeRef.current(); reassert(); });
        set('play', () => {
          reassert();
          if (stateRef.current === _CONV_STATE.ERROR) {
            errorCountRef.current = 0;
            setError(null);
            setState(_CONV_STATE.COOLDOWN);
            _scheduleListen();
          }
        });
      }
    } catch (e) { /* anchor is best-effort */ }
  }, [setState, _scheduleListen]);
  // ════ end terminalMode core ════════════════════════════════════════

  // VAD finalisation: text from STT → send through orchestrator. Empty text
  // means silence-only (warmup never saw speech) — re-arm without sending.
  // First turn only: prepend a short voice-mode hint so claude knows the
  // session is hands-free and replies in plain prose. Subsequent turns
  // skip the prefix to keep context lean and avoid the model latching
  // onto the prefix as the actual question.
  // terminalMode send: build the prompt (full protocol block on turn 1 and
  // every Nth turn as compaction insurance, bare "(głos)" marker otherwise),
  // paste+submit into tmux, bump the in-flight counter, anchor the picker
  // fallback at THIS send, then play any deferred audio or go THINKING.
  const _sendVoiceText = _cUseCallback((cleaned) => {
    const vp = window.HubVoicePrompts;
    turnCountRef.current += 1;
    const full = vp && ((turnCountRef.current - 1) % _CONV_HINT_REFRESH_TURNS === 0);
    const toSend = vp ? (full ? vp.convFirstTurn(cleaned) : vp.convTurn(cleaned)) : cleaned;
    const sender = sendToTerminalRef.current;
    if (!sender) {
      _speakSystem('Nie mogę wysłać do terminala.', { forceListen: true });
      return;
    }
    try {
      sender(toSend);
    } catch (e) {
      _speakSystem('Nie udało się wysłać, powtórz proszę.', { forceListen: true });
      return;
    }
    inFlightRef.current += 1;
    hadSpeakSinceSendRef.current = false;
    lastSendAtRef.current = performance.now();
    lastSpeakActivityRef.current = performance.now();  // stall-gate anchor for this send
    _armPickerTimer();
    if (_flushPendingAudio()) return;  // deferred async audio plays now; drain routes back to THINKING
    setState(_CONV_STATE.THINKING);
  }, [setState, _speakSystem, _flushPendingAudio, _armPickerTimer]);
  _sendVoiceTextRef.current = _sendVoiceText;

  const handleFinal = _cUseCallback((text) => {
    const cleaned = (text || '').trim();
    if (stateRef.current !== _CONV_STATE.LISTENING && stateRef.current !== _CONV_STATE.TRANSCRIBING) return;
    if (terminalModeRef.current) {
      // Mark "inside onFinal": the vc state still reads 'finalizing' here, but
      // the capture is DONE — _routeAudio must PLAY system phrases from these
      // paths, not defer them into a queue no future onFinal would flush.
      inFinalRef.current = true;
      try {
        errorCountRef.current = 0;  // a successful capture resets the error spiral
        // Treat too-short OR a Whisper outro-hallucination (the server filter
        // is primary; this is the belt-and-suspenders so a single miss can't
        // paste a ghost command into tmux) exactly like silence: no send.
        const hallu = !!(window.HubVoice && window.HubVoice.isLikelyHallucination
          && window.HubVoice.isLikelyHallucination(cleaned));
        if (!cleaned || cleaned.length < 4 || hallu) {
          if (_flushPendingAudio()) return;
          setState(_CONV_STATE.COOLDOWN);
          _scheduleListen();
          return;
        }
        setLastUserText(cleaned);
        // Send-gate (hard user rule: never auto-press keys): paste+Enter must
        // not reach a picker / permission prompt. The turn counts as safely
        // generating only with RECENT block activity — claude routinely
        // narrates prose and THEN parks on a prompt, so stale activity
        // (> _CONV_SEND_STALL_MS) re-gates even after blocks arrived.
        const stalled = (performance.now() - lastSpeakActivityRef.current) > _CONV_SEND_STALL_MS;
        if (inFlightRef.current > 0 && (!hadSpeakSinceSendRef.current || stalled)) {
          pendingSendsRef.current.push({ text: cleaned, at: performance.now() });
          _speakSystem('Sesja jeszcze pracuje i nie mogę bezpiecznie wysłać — wyślę, gdy skończy.', { forceListen: true });
          return;
        }
        _sendVoiceText(cleaned);
        return;
      } finally {
        inFinalRef.current = false;
      }
    }
    if (!cleaned) {
      setState(_CONV_STATE.COOLDOWN);
      _scheduleListen();
      return;
    }
    setLastUserText(cleaned);
    setState(_CONV_STATE.SENDING);
    let toSend = cleaned;
    if (firstTurnRef.current) {
      firstTurnRef.current = false;
      toSend = (
        '<voice-mode>Rozmowa głosowa — krótkie zdania prozą, bez markdownu, '
        + 'list, tabel ani kodu. Pisz tak, by dało się płynnie przeczytać '
        + 'na głos.</voice-mode>\n\n'
        + cleaned
      );
    }
    try {
      send(toSend, { from_voice_conv: true });
    } catch (e) {
      setError('Wysyłka nie powiodła się');
      setState(_CONV_STATE.ERROR);
    }
  }, [send, _scheduleListen, setState, _sendVoiceText, _speakSystem, _flushPendingAudio]);

  const handleError = _cUseCallback((msg) => {
    const m = typeof msg === 'string' ? msg : 'Błąd mikrofonu';
    setError(m);
    if (terminalModeRef.current && activeRef.current) {
      // Eyes-free: every error is SPOKEN, and the loop self-recovers — the
      // driver can't read the modal. Two retries, then a stable ERROR the
      // steering-wheel play button (or an arriving block) can recover from.
      // Deferred async audio must not be stranded by a failed capture — play
      // it first (the error phrase then queues behind it on the same flow).
      _flushPendingAudio();
      errorCountRef.current += 1;
      const micErr = /mikrofon/i.test(m);
      if (errorCountRef.current <= 2) {
        _speakSystem(
          micErr ? 'Nie mam dostępu do mikrofonu. Próbuję jeszcze raz.'
                 : 'Nie zrozumiałem, powtórz proszę.',
          { forceListen: true },
        );
        return;
      }
      _speakSystem(micErr ? 'Mikrofon nie działa. Sprawdź telefon, gdy się zatrzymasz.'
                          : 'Transkrypcja nie działa. Sprawdź telefon, gdy się zatrzymasz.');
      setState(_CONV_STATE.ERROR);
      return;
    }
    setState(_CONV_STATE.ERROR);
  }, [setState, _speakSystem, _flushPendingAudio]);

  handleFinalRef.current = handleFinal;
  handleErrorRef.current = handleError;

  // Cancel any active TTS (loopback playback, browser SpeechSynthesis,
  // external tts hook) and clean up barge monitor.
  const _cancelPlayback = _cUseCallback(() => {
    if (playbackRef.current) {
      try { playbackRef.current.cancel(); } catch (e) {}
      playbackRef.current = null;
    }
    if (browserUtterRef.current) {
      try { window.speechSynthesis.cancel(); } catch (e) {}
      browserUtterRef.current = null;
    }
    if (cancelTtsExternal) { try { cancelTtsExternal(); } catch (e) {} }
    if (bargeMonRef.current) {
      try { bargeMonRef.current.close(); } catch (e) {}
      bargeMonRef.current = null;
    }
    if (window.HubAudioBus) window.HubAudioBus.release('conv');
  }, [cancelTtsExternal]);

  // Register conversation voice on the cross-subsystem audio bus. The remote
  // engine path (_startConvPlayback → AudioContext/BufferSource) does NOT go
  // through tts.streamChunk, so it needs its OWN registration; _cancelPlayback
  // stops both the remote playback and the browser-engine external TTS. Gate the
  // bus-pause to the SPEAKING state so _cancelPlayback's mic side-effects don't
  // fire when conversation isn't the audio owner.
  const _cancelPlaybackRef = _cUseRef(_cancelPlayback);
  _cUseEffect(() => { _cancelPlaybackRef.current = _cancelPlayback; }, [_cancelPlayback]);
  _cUseEffect(() => {
    if (!window.HubAudioBus) return undefined;
    const un = window.HubAudioBus.register('conv', {
      kind: 'conv',
      pause: () => {
        // Ignore the bus pause we ourselves triggered by delegating this reply
        // to the tts hook (browser engine) — otherwise tts.requestActive('tts')
        // would cancel the speech we just started.
        if (_delegatingToTtsRef.current) return;
        if (stateRef.current === _CONV_STATE.SPEAKING) {
          try { _cancelPlaybackRef.current && _cancelPlaybackRef.current(); } catch (e) { /* ignore */ }
        }
      },
    });
    return () => { try { un && un(); } catch (e) { /* ignore */ } };
  }, []);

  // Pull speakable prose from an assistant message, mirroring orchestrator's
  // _speechBlocksFor (orchestrator.jsx:1143-1148). Falls back to m.text for
  // assistant_message events (FINALIZE_TURN dispatches put prose directly on
  // the message rather than into a blocks array). Without this fallback, the
  // conversation hook saw an empty string, dispatched COOLDOWN, and the loop
  // sat in 'thinking' until the user re-triggered — exactly the symptom in
  // the 2nd-turn console trace from 2026-05-04.
  const _speechTextFor = _cUseCallback((m) => {
    if (!m) return '';
    const blocks = Array.isArray(m.blocks) ? m.blocks : [];
    const fromBlocks = blocks
      .filter((b) => b && (b.kind === 'markdown' || b.kind === 'text') && (b.text || b.content))
      .map((b) => (b.text != null ? b.text : (b.content || '')))
      .join('\n\n');
    if (fromBlocks && fromBlocks.trim()) return fromBlocks;
    if (typeof m.text === 'string' && m.text.trim()) return m.text;
    return '';
  }, []);

  // Build playback chunks via the existing strip+chunk helpers.
  const _buildSpeechChunks = _cUseCallback((rawText) => {
    if (!rawText) return [];
    const stripped = (window.stripMarkdownForSpeech ? window.stripMarkdownForSpeech(rawText) : rawText) || '';
    if (!stripped.trim()) return [];
    const chunked = window.chunkForSpeech ? window.chunkForSpeech(stripped) : [stripped];
    return chunked.filter(Boolean);
  }, []);

  // Start TTS for an assistant message. Routes through loopback (aec mode)
  // or external tts.speakBlocks (mute mode / browser engine). Returns true
  // when TTS was actually queued so the caller can decide whether to mark
  // this turn as "spoken" — see speaking effect below for the race this
  // gates against.
  const _startSpeaking = _cUseCallback(async (assistantMsg, key) => {
    const rawText = _speechTextFor(assistantMsg);
    const chunks = _buildSpeechChunks(rawText);
    if (!chunks.length) {
      // Common case: an early `assistant_message` event delivered ONLY a
      // thinking / tool_use block (no prose). The prose lands shortly after
      // via `structured_blocks`, which the reducer merges into the same
      // message. Stay in THINKING and DON'T mark this key as spoken — that
      // way the merge-driven re-fire will pick up the prose. The watchdog
      // (isStreaming=false + still THINKING after 2500ms) catches the case
      // where prose never arrives.
      return;
    }
    // Cancel any leftover playback (and its barge monitor) before starting
    // a new one. Without this, two assistant messages with different
    // turn_idx (e.g. multi-emit structured_blocks per logical turn) would
    // each spawn an Audio chain that plays in parallel — orphaned chunks
    // pile up and the bargeMonRef leaks a getUserMedia track per emission.
    _cancelPlayback();
    lastSpokenKeyRef.current = key;
    // Claim this key in the parent's spokenKeys set so the regular
    // auto-speak effect (voiceOutput='always') won't re-speak the same
    // turn through tts.speakBlocks when the modal closes.
    if (markAssistantSpoken) { try { markAssistantSpoken(key); } catch (e) {} }
    setState(_CONV_STATE.SPEAKING);
    // Claim the audible channel so a starting reply pauses any media clip.
    if (window.HubAudioBus) window.HubAudioBus.requestActive('conv');

    const onPlaybackDone = () => {
      playbackRef.current = null;
      browserUtterRef.current = null;
      if (bargeMonRef.current) {
        try { bargeMonRef.current.close(); } catch (e) {}
        bargeMonRef.current = null;
      }
      if (window.HubAudioBus) window.HubAudioBus.release('conv');
      if (stateRef.current !== _CONV_STATE.SPEAKING) return;
      setState(_CONV_STATE.COOLDOWN);
      _scheduleListen();
    };

    // Browser engine: route through the existing tts.speakBlocks fallback.
    // No loopback (Web Speech API isn't routable through Web Audio).
    if (ttsEngine === 'browser' || !ttsSupported) {
      if (speakBlocksFallback) {
        // tts.speakBlocks synchronously claims the bus ('tts'); flag the
        // in-flight delegation so our own conv pause callback no-ops during it
        // (it would otherwise cancel this very read). Cleared once the
        // synchronous claim has returned.
        _delegatingToTtsRef.current = true;
        try {
          speakBlocksFallback(rawText, { onDone: onPlaybackDone });
        } catch (e) {
          onPlaybackDone();
        } finally {
          _delegatingToTtsRef.current = false;
        }
      } else {
        onPlaybackDone();
      }
      // Browser engine = always mute mode (mic stays off until cooldown).
      return;
    }

    // Pick playback path. Default: route through our shared
    // MediaStreamDestination → <audio>.srcObject (iOS PWA + silent-switch
    // bypass). For AEC mode, route through the loopback's dest instead
    // so AEC sees the playback as remote and can cancel it from the mic.
    let destination;
    let ctx = audioCtxRef.current;
    if (echoMode === 'aec' && loopbackRef.current && loopbackRef.current.dest) {
      ctx = loopbackRef.current.audioCtx || ctx;
      destination = loopbackRef.current.dest;
    } else if (ctx && audioDestRef.current) {
      destination = audioDestRef.current;
    } else if (ctx) {
      destination = ctx.destination;  // last-resort fallback
    }

    if (!ctx || !destination) {
      // No AudioContext — Web Audio unsupported. Bail with cooldown so
      // the loop isn't stuck in SPEAKING; user sees the text response.
      // eslint-disable-next-line no-console
      console.warn('[conv] no AudioContext — cannot play TTS, falling through to cooldown');
      onPlaybackDone();
      return;
    }

    // Resume context if it slipped to suspended (iOS does this when the
    // PWA was backgrounded, even for already-resumed contexts). MUST be
    // awaited — otherwise BufferSource.start(0) inside _startConvPlayback
    // can fire on a still-suspended context, silently producing no audio.
    // _startBargeMonitor in this same file already awaits its resume()
    // for the same reason; this call site was missing the await.
    if (ctx.state === 'suspended') {
      try { await ctx.resume(); } catch (e) { /* iOS sometimes rejects; proceed and let start() fail audibly */ }
    }

    playbackRef.current = await _startConvPlayback(
      chunks,
      ttsEngine,
      ttsVoice,
      ctx,
      destination,
      onPlaybackDone,
    );

    // Spin up the barge-in monitor only in aec mode (mute mode can't barge
    // by voice — the mic is silent during speaking).
    if (echoMode === 'aec') {
      try {
        const monitor = await _startBargeMonitor({
          thresholdRms: bargeThreshold,
          minDurationMs: bargeMinDurationMs,
          onBarge: () => {
            if (stateRef.current !== _CONV_STATE.SPEAKING) return;
            setState(_CONV_STATE.INTERRUPTING);
            _cancelPlayback();
            setState(_CONV_STATE.COOLDOWN);
            _scheduleListen();
          },
        });
        // Guard against late assignment after teardown (modal closed during
        // the await): if we're no longer SPEAKING, close the monitor we
        // just created instead of leaking its getUserMedia track.
        if (stateRef.current !== _CONV_STATE.SPEAKING) {
          try { monitor.close(); } catch (e) {}
        } else {
          // Close any prior monitor from a stale invocation before assignment.
          if (bargeMonRef.current) {
            try { bargeMonRef.current.close(); } catch (e) {}
          }
          bargeMonRef.current = monitor;
        }
      } catch (e) {
        // Monitor failure is non-fatal — user can still tap "Przerwij".
      }
    }
  }, [
    _speechTextFor, _buildSpeechChunks, ttsEngine, ttsVoice, ttsSupported,
    echoMode, bargeThreshold, bargeMinDurationMs, speakBlocksFallback,
    _scheduleListen, _cancelPlayback, setState, markAssistantSpoken,
  ]);

  // Watch incoming assistant messages — kick off speaking when one arrives
  // post-thinking. Gated by lastSpokenKeyRef to avoid double-speaking when
  // structured_blocks finalisations re-fire.
  // Also fires when isStreaming flips to false: 'done' SSE events don't
  // necessarily change `messages`, but they CAN signal that the new
  // assistant has been finalised (and was already in messages at the time
  // of THINKING entry but lastSpokenKeyRef matched it). Re-running the
  // walk-back lets us catch trailing-finalise cases.
  _cUseEffect(() => {
    if (!active) return;
    if (terminalModeRef.current) return;  // ttyd turns never land in state.messages
    if (stateRef.current !== _CONV_STATE.THINKING) return;
    if (!messages.length) return;
    let lastAssistantIdx = -1;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (m && m.role === 'assistant') { lastAssistantIdx = i; break; }
    }
    if (lastAssistantIdx < 0) return;
    const assistant = messages[lastAssistantIdx];
    const key = _msgKey(assistant);
    if (!key || lastSpokenKeyRef.current === key) return;
    // eslint-disable-next-line no-console
    console.debug('[conv] speaking-trigger', { key, hasBlocks: Array.isArray(assistant.blocks) ? assistant.blocks.length : 0, hasText: !!(assistant.text && assistant.text.length) });
    // _startSpeaking marks lastSpokenKeyRef itself when (and only when) it
    // actually queues TTS. If the current emission has no speakable prose
    // (early thinking/tool-only payload), it returns without marking — the
    // structured_blocks merge re-fires this effect and we'll try again with
    // the merged content. Without this gating, the no-prose first emission
    // would mark the turn spoken, the prose-merge re-fire would skip via
    // the lastSpokenKey check, and TTS would never play for this turn.
    _startSpeaking(assistant, key);
  }, [active, messages, isStreaming, _msgKey, _startSpeaking]);

  // Track sending → thinking via the orchestrator's isStreaming flag.
  _cUseEffect(() => {
    if (!active) return;
    if (terminalModeRef.current) return;  // terminalMode goes THINKING directly
    if (stateRef.current === _CONV_STATE.SENDING && isStreaming) {
      setState(_CONV_STATE.THINKING);
    }
  }, [active, isStreaming, setState]);

  // Drive vc.start() whenever the conversation needs the mic active. Runs as
  // an effect so it picks up vc.state transitions (e.g. 'finalizing' → 'idle'
  // after a turn) without the stale-closure trap inherent to setTimeout. No-op
  // if vc isn't yet idle (vc.start() guards internally).
  _cUseEffect(() => {
    if (!active) return;
    if (state !== _CONV_STATE.LISTENING) return;
    // vc 'error' is otherwise terminal (this effect only starts from 'idle'):
    // after a failed capture the spoken retry promised a retry — deliver it by
    // resetting through abort() (synchronously lands in 'idle' → effect
    // refires → doStart). Without this the loop sits in LISTENING deaf.
    if (terminalModeRef.current && vc.state === 'error' && typeof vc.abort === 'function') {
      try { vc.abort(); } catch (e) { /* ignore */ }
      return;
    }
    if (vc.state !== 'idle') return;
    const doStart = () => {
      try {
        const r = vc.start();
        if (r && typeof r.catch === 'function') r.catch(() => { /* error path handled in vc */ });
      } catch (e) { /* vc surfaces errors via onError */ }
    };
    // HARD INVARIANT (terminalMode): never open the mic while TTS is audible —
    // a self-spoken phrase or a draining tail would be captured as "speech"
    // and re-sent as a garbage turn. The drain machinery normally guarantees
    // quiet before LISTENING; this guard covers every other path in, and the
    // poll un-sticks the case where audio was still draining on entry.
    if (terminalModeRef.current && getTtsSpeakingRef.current && getTtsSpeakingRef.current()) {
      const t = setInterval(() => {
        if (stateRef.current !== _CONV_STATE.LISTENING) { clearInterval(t); return; }
        if (getTtsSpeakingRef.current && getTtsSpeakingRef.current()) return;
        clearInterval(t);
        doStart();
      }, _CONV_DRAIN_POLL_MS);
      return () => clearInterval(t);
    }
    doStart();
    return undefined;
  }, [active, state, vc.state, vc]);

  // ── terminalMode effects (always mounted, internally gated — hook order
  // must stay stable in the no-build CDN-React setup) ──

  // THINKING timers: spoken keep-alive (12s, then every 20s, only when the
  // TTS is quiet — and queued on the same flow key anyway, so it physically
  // can't talk over a block) + the 50s release that frees the mic while
  // claude grinds ("dispatch and keep talking").
  _cUseEffect(() => {
    if (!active || !terminalModeRef.current) return undefined;
    if (state !== _CONV_STATE.THINKING) return undefined;
    const speakQuiet = (text) => {
      if (getTtsSpeakingRef.current && getTtsSpeakingRef.current()) return;
      if (speakTextRef.current) { try { speakTextRef.current(text); } catch (e) { /* ignore */ } }
    };
    const first = setTimeout(function tick() {
      if (stateRef.current !== _CONV_STATE.THINKING) return;
      speakQuiet('Pracuję nad tym…');
      keepAliveTimerRef.current = setTimeout(tick, _CONV_KEEPALIVE_EVERY_MS);
    }, _CONV_KEEPALIVE_FIRST_MS);
    const release = setTimeout(() => {
      if (stateRef.current !== _CONV_STATE.THINKING) return;
      _speakSystem('Pracuję dalej w tle — możesz mówić.', { forceListen: true });
    }, _CONV_RELEASE_MS);
    return () => {
      clearTimeout(first);
      clearTimeout(release);
      if (keepAliveTimerRef.current) { clearTimeout(keepAliveTimerRef.current); keepAliveTimerRef.current = null; }
    };
  }, [active, state, _speakSystem]);

  // Mirror vc 'finalizing' as TRANSCRIBING ("Rozumiem…") — eyes-free feedback
  // that the utterance was captured and is being transcribed.
  _cUseEffect(() => {
    if (!active || !terminalModeRef.current) return;
    if (state === _CONV_STATE.LISTENING && vc.state === 'finalizing') {
      setState(_CONV_STATE.TRANSCRIBING);
    }
  }, [active, state, vc.state, setState]);

  // Wake lock + resume protocol: iOS freezes JS/SSE/mic when the phone locks.
  // Re-acquire on return + resync whichever half of the loop died.
  _cUseEffect(() => {
    if (!active || !terminalModeRef.current) return undefined;
    const onVis = () => {
      if (document.visibilityState !== 'visible') return;
      _acquireWakeLock();
      const st = stateRef.current;
      if (st === _CONV_STATE.LISTENING && vcRef.current && vcRef.current.state !== 'recording') {
        _speakSystem('Wracam, słucham.', { forceListen: true });
      } else if (st === _CONV_STATE.SPEAKING
          && !(getTtsSpeakingRef.current && getTtsSpeakingRef.current())) {
        _startDrainPoll();  // audio died while locked → route out of SPEAKING
      }
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [active, _acquireWakeLock, _speakSystem, _startDrainPoll]);

  // Mic pinning DISABLED after the in-car test (2026-06-11, Mitsubishi
  // Outlander + iPhone, static/mic-route-test.html): an explicit
  // deviceId:{exact} pin to the built-in mic made iOS HIJACK THE WHOLE AUDIO
  // ROUTE — TTS playback moved from the car's A2DP to the phone speaker (and
  // closing the mic did not restore it). DEFAULT constraints are the ideal
  // behavior on iOS: it picks the built-in "iPhone Mikrofon" anyway (same
  // deviceId as the pin would force) while KEEPING hi-fi A2DP output in the
  // car — no HFP flip, no phone-call mode. So: never set micId here; the
  // preferredDeviceId plumbing stays in useVoiceCapture (generic, default '')
  // should a desktop use case ever want it.

  // Phantom in-flight decay: a missed end_flow (phone locked through the turn
  // boundary — speak frames are not replayed) would hold the send-gate
  // forever. After 10 min with NO turn activity, reset the counter and tell
  // the driver to glance at the screen when convenient.
  _cUseEffect(() => {
    if (!active || !terminalModeRef.current) return undefined;
    const t = setInterval(() => {
      if (inFlightRef.current <= 0) return;
      const now = performance.now();
      if (now - lastSpeakActivityRef.current < _CONV_INFLIGHT_DECAY_MS) return;
      if (now - lastSendAtRef.current < _CONV_INFLIGHT_DECAY_MS) return;
      inFlightRef.current = 0;
      hadSpeakSinceSendRef.current = false;
      if (pickerTimerRef.current) { clearTimeout(pickerTimerRef.current); pickerTimerRef.current = null; }
      _speakSystem('Sesja długo milczy — odblokowuję wysyłanie. Sprawdź ekran, gdy się zatrzymasz.', { forceListen: true });
    }, 60_000);
    return () => clearInterval(t);
  }, [active, _speakSystem]);

  // Debug breadcrumbs: log every conversation state change + vc.state so the
  // browser console gives us a timeline when things misbehave. Cheap; gated
  // off by default in production once stable.
  _cUseEffect(() => {
    if (!active && state === _CONV_STATE.IDLE) return;
    // eslint-disable-next-line no-console
    console.debug('[conv]', state, '| vc=', vc.state, '| streaming=', isStreaming);
  }, [active, state, vc.state, isStreaming]);

  // Watchdog: if isStreaming flips false while we're still in THINKING and
  // no new assistant prose lands shortly after, force cooldown. This covers
  // the case where the runner emits an `error` event mid-stream — the error
  // block now lands in state.messages (reducer push) so the speaking effect
  // sees it and gracefully transitions, but if claude exited cleanly with
  // only thinking blocks (no prose, no error) we still need this catch-all.
  // Grace 2500ms: covers client GC pauses / SSE buffer jitter while still
  // catching genuinely stuck turns. If the latest assistant message is
  // error-only, surface the actual error text instead of the generic
  // context-limit hint.
  _cUseEffect(() => {
    if (!active) return;
    // terminalMode: isStreaming is permanently false here, so this watchdog
    // would arm the moment THINKING starts. The terminalMode timers (keep-
    // alive / release / picker fallback) own that job instead.
    if (terminalModeRef.current) return;
    if (state !== _CONV_STATE.THINKING) return;
    if (isStreaming) return;
    const t = setTimeout(() => {
      if (stateRef.current !== _CONV_STATE.THINKING) return;
      // Inspect latest assistant — if it carries an error block, surface
      // that. Otherwise fall back to the generic watchdog message.
      let errText = null;
      const msgs = messages || [];
      for (let i = msgs.length - 1; i >= 0; i -= 1) {
        const m = msgs[i];
        if (!m || m.role !== 'assistant') continue;
        const blocks = Array.isArray(m.blocks) ? m.blocks : [];
        const errBlock = blocks.find((b) => b && b.kind === 'error');
        if (errBlock && errBlock.text) errText = errBlock.text;
        break;
      }
      setError(errText || 'Brak odpowiedzi modelu (możliwy limit kontekstu — sesja jest długa, użyj Compact).');
      setState(_CONV_STATE.COOLDOWN);
      _scheduleListen();
    }, 30_000);
    // Bumped 2500 → 8000 → 30000ms: iOS PWA standalone mode aggressively
    // buffers / delays SSE EventSource events, sometimes falls back to
    // 3-second poll mode mid-stream. Even at 8s the watchdog tripped
    // before structured_blocks landed in messages, leaving the modal
    // stuck in COOLDOWN with the response visible in chat but no auto-
    // play. 30s is the new "truly stuck" ceiling — past that it's a
    // genuine network / claude failure. The user can always tap X to
    // exit early.
    return () => clearTimeout(t);
  }, [active, state, isStreaming, messages, _scheduleListen, setState]);

  // Safety: if SPEAKING lasts more than 4 minutes the playback machinery
  // is stuck (audio.onerror not firing, network hang, etc.). Force cooldown
  // so the loop unfreezes rather than trapping the user in "Mówię…"
  // indefinitely.
  // Empirical battery 2026-05-04 saw Opus produce 363-word answers (~145 s
  // TTS @ 150 wpm) — bumped 180s → 240s so the absolute ceiling has clear
  // headroom for verbose lists / multi-paragraph synthesis. User can always
  // tap the waveform (square stop icon) to interrupt early.
  _cUseEffect(() => {
    if (!active) return;
    if (state !== _CONV_STATE.SPEAKING) return;
    // Inactivity-based (not since-state-entry): terminalMode async narration
    // can legally hold SPEAKING for many minutes across queued turns — every
    // feedSpeak bumps lastSpeakActivityRef. Legacy mode never bumps it, so
    // the ceiling degenerates to the original 240s-from-entry behavior.
    lastSpeakActivityRef.current = performance.now();
    const t = setInterval(() => {
      if (stateRef.current !== _CONV_STATE.SPEAKING) return;
      if (performance.now() - lastSpeakActivityRef.current < _CONV_SPEAK_IDLE_MAX_MS) return;
      // eslint-disable-next-line no-console
      console.warn('[conv] speaking idle timeout — forcing cooldown');
      _stopDrainPoll();
      _cancelPlayback();
      if (cancelTtsExternalRef.current) { try { cancelTtsExternalRef.current(); } catch (e) { /* ignore */ } }
      setState(_CONV_STATE.COOLDOWN);
      _scheduleListen();
    }, 15_000);
    return () => clearInterval(t);
  }, [active, state, _cancelPlayback, _scheduleListen, setState, _stopDrainPoll]);

  // Public actions ---------------------------------------------------------

  const start = _cUseCallback(async () => {
    setError(null);
    setLastUserText('');
    if (terminalModeRef.current) {
      // ── ttyd / driving path: external TTS (read-aloud flow key) is the
      // ONLY audio out, mic is the only input; no AEC loopback, no own
      // Web-Audio chain (skipping them also keeps the gesture-critical
      // start() fast). Refuses to start without the read-aloud watcher —
      // otherwise the loop is a one-way conversation theater.
      if (!readAloudAvailableRef.current) {
        if (ttsUnlock) { try { ttsUnlock(); } catch (e) { /* ignore */ } }
        setError('Auto-czytanie jest wyłączone na serwerze — włącz read_aloud_tmux_enabled.');
        if (speakTextRef.current) {
          try { speakTextRef.current('Auto-czytanie jest wyłączone na serwerze. Włącz je w ustawieniach.'); } catch (e) { /* ignore */ }
        }
        return;
      }
      setActive(true);
      activeRef.current = true;
      turnCountRef.current = 0;
      inFlightRef.current = 0;
      hadSpeakSinceSendRef.current = false;
      pendingSendsRef.current = [];
      pendingAudioRef.current = [];
      suppressRef.current = false;
      drainTargetRef.current = 'auto';
      errorCountRef.current = 0;
      firstTurnRef.current = true;
      // Inside the user gesture: unlock TTS autoplay, grab the wake lock,
      // start the Now Playing anchor + steering-wheel handlers.
      if (ttsUnlock) { try { ttsUnlock(); } catch (e) { /* ignore */ } }
      if (cancelTtsExternalRef.current) { try { cancelTtsExternalRef.current(); } catch (e) { /* ignore */ } }
      _acquireWakeLock();
      _startAnchor();
      // Spoken readiness check — confirms the whole audio path BEFORE the
      // first listen (and routes through drain → COOLDOWN → LISTENING, so
      // the mic arms only after the phrase finishes).
      _speakSystem('Tryb rozmowy aktywny.', { forceListen: true });
      return;
    }
    setActive(true);
    // If a turn is already in-flight when the modal opens (push-to-talk
    // just sent before the user tapped 🎧), go straight to THINKING. The
    // speaking effect picks up the assistant when it finalises and TTS
    // plays. After playback ends → COOLDOWN → LISTENING via the cooldown
    // timer + vcEffect, recovering the normal loop without starting the
    // mic during someone else's stream.
    setState(isStreaming ? _CONV_STATE.THINKING : _CONV_STATE.ARMED);

    // Cancel any in-flight regular-auto-speak playback so we don't overlap
    // it with the conversation loop. Without this, opening the modal right
    // after a push-to-talk turn would let the previous assistant message
    // keep speaking while the loop tries to handle a new turn — which the
    // user perceives as "agent answers the wrong (previous) message".
    if (cancelTtsExternal) { try { cancelTtsExternal(); } catch (e) {} }

    // Seed lastSpokenKeyRef so the speaking effect doesn't pick up a stale
    // prior assistant message as "the response for this turn" the moment we
    // transition to THINKING. EXCEPT when we open mid-stream: the latest
    // assistant in messages might BE the in-flight one (or a thinking-only
    // partial that the prose will merge into). Skip the latest assistant
    // and seed with the second-latest, so the in-flight gets spoken.
    // ALSO: claim every existing assistant key in spokenKeys so the
    // regular auto-speak effect (voiceOutput='always') won't replay the
    // pre-modal history when the user closes the modal.
    {
      const msgs = messages || [];
      let seenAssistantCount = 0;
      for (let i = msgs.length - 1; i >= 0; i -= 1) {
        const m = msgs[i];
        if (!m || m.role !== 'assistant') continue;
        seenAssistantCount += 1;
        const k = _msgKey(m);
        if (markAssistantSpoken && k) {
          try { markAssistantSpoken(k); } catch (e) {}
        }
        if (isStreaming && seenAssistantCount === 1) continue;
        if (lastSpokenKeyRef.current == null && k) {
          lastSpokenKeyRef.current = k;
        }
      }
    }

    // 1) Audio unlock (must run inside the click handler that called this).
    if (ttsUnlock) { try { ttsUnlock(); } catch (e) {} }

    // 1b) Create AudioContext + MediaStreamDestination + <audio> output
    //     element inside the gesture. The <audio> with srcObject from a
    //     MediaStream is the iOS-PWA-safe playback path:
    //       a) iOS autoplay policy allows audio.srcObject from MediaStream
    //          to play after the FIRST gesture-bound .play() call (treated
    //          like a WebRTC remote stream — not blocked seconds later)
    //       b) HTMLAudioElement output uses the "media playback" audio
    //          category which IGNORES the iOS side-switch silent / vibrate
    //          mode — the silent switch mutes raw Web Audio (ctx.destination)
    //          but not <audio>-element playback. User had this exact issue:
    //          speaker-button TTS (HTMLAudioElement) was audible while
    //          conversation TTS (raw Web Audio) was silent.
    //     We feed the streamDest from BufferSources later — chunks decode →
    //     source.connect(streamDest) → audio element receives the stream →
    //     iOS plays it through the speaker.
    if (!audioCtxRef.current) {
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) {
          const ctx = new Ctx();
          audioCtxRef.current = ctx;
          if (ctx.state === 'suspended') {
            ctx.resume().catch(() => { /* iOS may reject silently */ });
          }
          const dest = ctx.createMediaStreamDestination();
          audioDestRef.current = dest;
          const out = new Audio();
          out.srcObject = dest.stream;
          out.autoplay = true;
          out.playsInline = true;
          // Don't add to DOM — invisible audio element is fine for srcObject
          // playback. Safari sometimes needs an explicit play() within the
          // gesture; ignore rejection (autoplay heuristic might still allow
          // it via MediaStream rules anyway).
          const p = out.play();
          if (p && typeof p.catch === 'function') {
            p.catch((err) => {
              // eslint-disable-next-line no-console
              console.warn('[conv] output element play() rejected', err && err.message);
            });
          }
          audioOutRef.current = out;
          // Prime: 1-sample silent BufferSource → streamDest. This forces
          // the audio graph to flush its pipeline once, so subsequent
          // chunks reach the audio element without first-buffer delay.
          try {
            const primer = ctx.createBuffer(1, 1, 22050);
            const src = ctx.createBufferSource();
            src.buffer = primer;
            src.connect(dest);
            src.start(0);
          } catch (e) { /* best-effort prime */ }
        }
      } catch (e) { /* AudioContext unsupported — degrade silently */ }
    }

    // 2) Build loopback for AEC mode (lazy).
    if (echoMode === 'aec' && !loopbackRef.current) {
      try {
        loopbackRef.current = await window.createAecLoopback();
      } catch (e) {
        // Fall back to plain Audio playback. Degrades gracefully.
        loopbackRef.current = null;
      }
    }

    // 3) Mid-stream entry: don't start the mic yet — vc.start() will fire
    //    automatically via the vcEffect once we hit LISTENING after the
    //    in-flight assistant's TTS plays out. Starting the mic here would
    //    capture the AI's voice while it's still mid-sentence.
    if (isStreaming) return;

    // 4) Normal entry: kick off mic acquisition immediately so the
    //    user-gesture chain isn't broken by any later awaits.
    let vcStartPromise;
    try { vcStartPromise = vc.start(); } catch (e) { vcStartPromise = Promise.resolve(); }
    try { await vcStartPromise; } catch (e) {}
    // The vc state will transition via its own internal logic; once it hits
    // 'recording' our render will see it and we move to LISTENING.
    if (stateRef.current === _CONV_STATE.ARMED) {
      setState(_CONV_STATE.LISTENING);
    }
  }, [vc, echoMode, ttsUnlock, setState, cancelTtsExternal, messages, _msgKey, isStreaming, markAssistantSpoken]);

  const barge = _cUseCallback(() => {
    if (terminalModeRef.current) {
      // ONLY in SPEAKING: iOS/cars fire media-pause on interruptions (Siri,
      // ignition, source switch) — a THINKING barge would silently drop the
      // ENTIRE upcoming reply. Never sends keys to the claude TUI.
      if (stateRef.current !== _CONV_STATE.SPEAKING) return;
      // Suppress the rest of the turn only if its end_flow hasn't arrived
      // (drain-poll alive ⇒ end already seen ⇒ nothing more comes for it,
      // and suppressing would eat a FUTURE turn's blocks instead).
      if (!drainPollRef.current) suppressRef.current = true;
      _stopDrainPoll();
      if (cancelTtsExternalRef.current) { try { cancelTtsExternalRef.current(); } catch (e) { /* ignore */ } }
      setState(_CONV_STATE.COOLDOWN);
      _scheduleListen();
      return;
    }
    if (stateRef.current !== _CONV_STATE.SPEAKING && stateRef.current !== _CONV_STATE.THINKING) return;
    setState(_CONV_STATE.INTERRUPTING);
    if (stateRef.current === _CONV_STATE.THINKING && onCancelStream) {
      try { onCancelStream(); } catch (e) {}
    }
    _cancelPlayback();
    setState(_CONV_STATE.COOLDOWN);
    _scheduleListen();
  }, [_cancelPlayback, _scheduleListen, onCancelStream, setState, _stopDrainPoll]);
  bargeRef.current = barge;

  const stop = _cUseCallback(() => {
    setActive(false);
    activeRef.current = false;
    setState(_CONV_STATE.IDLE);
    if (cooldownRef.current) { clearTimeout(cooldownRef.current); cooldownRef.current = null; }
    if (terminalModeRef.current) {
      // abort, NOT stop — no orphan transcribe POST, no late onFinal send.
      try { if (vc.abort) vc.abort(); else vc.stop(); } catch (e) { /* ignore */ }
      if (cancelTtsExternalRef.current) { try { cancelTtsExternalRef.current(); } catch (e) { /* ignore */ } }
      _stopDrainPoll();
      _clearThinkingTimers();
      if (pickerTimerRef.current) { clearTimeout(pickerTimerRef.current); pickerTimerRef.current = null; }
      pendingSendsRef.current = [];
      pendingAudioRef.current = [];
      suppressRef.current = false;
      drainTargetRef.current = 'auto';
      inFlightRef.current = 0;
      _releaseWakeLock();
      _stopAnchor();
    }
    _cancelPlayback();
    try { vc.stop(); } catch (e) {}
    if (loopbackRef.current) {
      try { loopbackRef.current.close(); } catch (e) {}
      loopbackRef.current = null;
    }
    if (audioOutRef.current) {
      try { audioOutRef.current.pause(); } catch (e) {}
      try { audioOutRef.current.srcObject = null; } catch (e) {}
      audioOutRef.current = null;
    }
    audioDestRef.current = null;
    if (audioCtxRef.current) {
      try { audioCtxRef.current.close(); } catch (e) {}
      audioCtxRef.current = null;
    }
    lastSpokenKeyRef.current = null;
    firstTurnRef.current = true;
    setError(null);
    setLastUserText('');
  }, [vc, _cancelPlayback, setState]);

  // Keep activeRef in lockstep with the state slice (start()/stop() also set
  // it synchronously so same-tick callbacks see the right value).
  _cUseEffect(() => { activeRef.current = active; }, [active]);

  // Belt + suspenders cleanup on unmount.
  _cUseEffect(() => () => {
    if (cooldownRef.current) clearTimeout(cooldownRef.current);
    if (playbackRef.current) { try { playbackRef.current.cancel(); } catch (e) {} }
    if (bargeMonRef.current) { try { bargeMonRef.current.close(); } catch (e) {} }
    if (loopbackRef.current) { try { loopbackRef.current.close(); } catch (e) {} }
    if (audioOutRef.current) { try { audioOutRef.current.pause(); } catch (e) {} }
    if (audioCtxRef.current) { try { audioCtxRef.current.close(); } catch (e) {} }
    if (drainPollRef.current) clearInterval(drainPollRef.current);
    if (keepAliveTimerRef.current) clearTimeout(keepAliveTimerRef.current);
    if (pickerTimerRef.current) clearTimeout(pickerTimerRef.current);
    if (wakeLockRef.current) { try { wakeLockRef.current.release(); } catch (e) {} }
    if (anchorRef.current) { try { anchorRef.current.pause(); } catch (e) {} }
    if (anchorUrlRef.current) { try { URL.revokeObjectURL(anchorUrlRef.current); } catch (e) {} }
  }, []);

  return _cUseMemo(() => ({
    active,
    state,
    level: vc.level || 0,
    error,
    lastUserText,
    start,
    stop,
    barge,
    feedSpeak,
    feedTurnEnd,
    speakSystem: _speakSystem,
  }), [active, state, vc.level, error, lastUserText, start, stop, barge, feedSpeak, feedTurnEnd, _speakSystem]);
}

// Circular waveform visualiser. Single SVG circle that scales with `level`
// (0..1 RMS). Color shifts per state for visual feedback. Doubles as the
// barge-in trigger: when state is speaking/thinking, clicking the circle
// interrupts the AI. A small white square is overlaid in the centre to
// signal "tap to stop", matching audio-player conventions.
function Waveform({ level, state, onBarge, canBarge }) {
  const safeLevel = Math.max(0, Math.min(1, level || 0));
  const radius = 80 + safeLevel * 28;
  const color = (state === _CONV_STATE.SPEAKING) ? 'var(--accent)'
    : (state === _CONV_STATE.LISTENING) ? 'var(--ok)'
    : (state === _CONV_STATE.ERROR) ? 'var(--err)'
    : (state === _CONV_STATE.THINKING || state === _CONV_STATE.SENDING) ? 'var(--info)'
    : 'var(--fg-3)';
  const onClick = canBarge ? onBarge : undefined;
  return (
    <button onClick={onClick} disabled={!canBarge}
      aria-label={canBarge ? 'Przerwij asystenta' : 'Mikrofon'}
      style={{
        background: 'transparent', border: 'none', padding: 0,
        cursor: canBarge ? 'pointer' : 'default',
      }}>
      <svg width={240} height={240} viewBox="0 0 240 240"
        style={{ display: 'block', filter: 'drop-shadow(0 0 18px rgba(140,90,255,0.15))' }}>
        <circle cx={120} cy={120} r={108} fill="none"
          stroke="var(--hairline-strong)" strokeWidth={1} />
        <circle cx={120} cy={120} r={radius} fill="none"
          stroke={color} strokeWidth={2}
          style={{ transition: 'r 80ms linear, stroke 200ms' }} />
        <circle cx={120} cy={120} r={radius - 16} fill={color} fillOpacity={0.08}
          style={{ transition: 'r 80ms linear, fill 200ms' }} />
        {canBarge && (
          <rect x={104} y={104} width={32} height={32} rx={4}
            fill={color} style={{ transition: 'fill 200ms' }} />
        )}
      </svg>
    </button>
  );
}

// Flatten an assistant or user message into a one-line preview for the
// "recent exchanges" tail on the modal. Truncated at 140 chars. Markdown
// is stripped (reuse stripMarkdownForSpeech which handles **bold**, code,
// links, headings, list markers, etc.) so the bubbles read like prose
// rather than raw `**foo**` `_bar_` syntax.
function _convFlattenForBubble(m) {
  if (!m) return '';
  const blocks = Array.isArray(m.blocks) ? m.blocks : [];
  const stripper = window.stripMarkdownForSpeech;
  let raw = '';
  for (const b of blocks) {
    if (!b) continue;
    if (b.kind === 'markdown' || b.kind === 'text') {
      let t = (b.text != null ? b.text : (b.content || ''));
      // Strip voice-mode hint BEFORE markdown stripping. The markdown
      // stripper rewrites every <…> tag to a space, which would orphan
      // the tag's content (the user would see the entire Polish prompt
      // instructions in their own message bubble — see screenshot bug
      // report 2026-05-04).
      t = t.replace(/<voice-mode>[\s\S]*?<\/voice-mode>\s*/g, '');
      raw += (stripper ? stripper(t) : t) + ' ';
    } else if (b.kind === 'image') {
      raw += '[obraz] ';
    } else if (b.kind === 'code') {
      raw += '[kod] ';
    }
  }
  raw = raw.replace(/\s+/g, ' ').trim();
  if (raw.length > 140) raw = raw.slice(0, 139) + '…';
  return raw;
}

// Three-dot pulse for "thinking" / "cooldown". Ditches the static "..." in
// the state label which looks like a bug. Each dot fades sequentially.
function ThinkingDots() {
  return (
    <span aria-hidden="true" style={{ display: 'inline-flex', gap: 4, marginLeft: 6 }}>
      {[0, 1, 2].map((i) => (
        <span key={i} style={{
          width: 6, height: 6, borderRadius: '50%',
          background: 'currentColor',
          animation: `conv-dot-pulse 1100ms ease-in-out ${i * 180}ms infinite`,
        }} />
      ))}
    </span>
  );
}

function ConversationModal({ open, onClose, conversation, recentMessages, msgKey, sessionId, activeModel, onModelChange }) {
  const safeRecent = Array.isArray(recentMessages) ? recentMessages : [];
  const lastTwo = _cUseMemo(() => {
    // Find last 2 turns (one user + one assistant pair, max 4 messages).
    return safeRecent.slice(-4);
  }, [safeRecent]);

  // ESC closes — cheap UX win, especially on laptop.
  _cUseEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  const { state, level, error, barge, lastUserText } = conversation;
  const labelText = _CONV_STATE_LABELS[state] || '';
  const showDots = state === _CONV_STATE.THINKING
    || state === _CONV_STATE.SENDING
    || state === _CONV_STATE.COOLDOWN
    || state === _CONV_STATE.TRANSCRIBING;
  const canBarge = state === _CONV_STATE.SPEAKING || state === _CONV_STATE.THINKING;
  // Cooldown shows ONLY dots — no "..." label text — so it doesn't look stuck.
  const labelDisplay = state === _CONV_STATE.COOLDOWN ? '' : labelText;

  return (
    <div role="dialog" aria-modal="true" aria-label="Tryb rozmowy"
      style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'var(--bg)',
        display: 'flex', flexDirection: 'column',
        animation: 'conv-fade-in 180ms ease-out',
      }}>
      <style>{`
        @keyframes conv-fade-in {
          from { opacity: 0; transform: scale(0.97); }
          to   { opacity: 1; transform: scale(1); }
        }
        @keyframes conv-dot-pulse {
          0%, 80%, 100% { opacity: 0.2; transform: scale(0.85); }
          40%           { opacity: 1;   transform: scale(1.15); }
        }
      `}</style>
      {/* Top: model picker on the left, exit on the right */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: 16, gap: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {sessionId && window.ModelButton && (
            <window.ModelButton compact={false} sessionId={sessionId}
              model={activeModel || null} onChanged={onModelChange} />
          )}
        </div>
        <button onClick={onClose} aria-label="Zamknij tryb rozmowy"
          style={{
            width: 56, height: 56, borderRadius: 28,
            background: 'var(--surface-2)', color: 'var(--fg-2)',
            border: '1px solid var(--hairline-strong)',
            cursor: 'pointer', fontSize: 'var(--t-h2)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
          ✕
        </button>
      </div>

      {/* Middle: waveform + label. The WHOLE middle area is a barge target —
          a driver can't aim at a 240px circle; barge() itself no-ops outside
          SPEAKING (terminalMode), so stray taps are harmless. */}
      <div onClick={canBarge ? barge : undefined} style={{
        flex: 1, display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', gap: 24,
        padding: '0 24px',
        cursor: canBarge ? 'pointer' : 'default',
      }}>
        <Waveform level={level} state={state} canBarge={canBarge} onBarge={barge} />
        <div aria-live="polite" style={{
          fontSize: 22, fontWeight: 500, color: 'var(--fg)',
          fontFamily: 'JetBrains Mono, monospace',
          letterSpacing: '-0.01em',
          display: 'inline-flex', alignItems: 'center', gap: 4,
        }}>
          {labelDisplay}
          {showDots && <ThinkingDots />}
        </div>
        {error && (
          <div role="alert" style={{
            fontSize: 'var(--t-md)', color: 'var(--err)',
            background: 'var(--surface-2)', border: '1px solid var(--err)',
            padding: '8px 14px', borderRadius: 'var(--r-control)', maxWidth: 480, textAlign: 'center',
          }}>{error}</div>
        )}
        {lastUserText && (
          <div className="mono" style={{
            fontSize: 'var(--t-cap)', color: 'var(--fg-3)', textAlign: 'center',
            maxWidth: 480, lineHeight: 1.5, marginTop: -8,
          }}>„{lastUserText}"</div>
        )}
      </div>

      {/* Recent history (last 2 exchanges, mini bubbles) */}
      {lastTwo.length > 0 && (
        <div style={{
          padding: '0 24px 12px', maxHeight: '24vh', overflowY: 'auto',
          display: 'flex', flexDirection: 'column', gap: 8,
          maskImage: 'linear-gradient(to bottom, transparent, black 12%)',
        }}>
          {lastTwo.map((m) => {
            const flat = _convFlattenForBubble(m);
            if (!flat) return null;
            const isUser = m.role === 'user';
            return (
              <div key={msgKey ? msgKey(m) : Math.random()}
                style={{
                  alignSelf: isUser ? 'flex-end' : 'flex-start',
                  maxWidth: '88%',
                  padding: '8px 12px',
                  borderRadius: 'var(--r-widget)',
                  background: isUser ? 'var(--accent-soft)' : 'var(--surface-2)',
                  border: '1px solid ' + (isUser ? 'var(--accent-line)' : 'var(--hairline)'),
                  color: 'var(--fg-2)',
                  fontSize: 'var(--t-sm)', lineHeight: 1.45,
                }}>
                {flat}
              </div>
            );
          })}
        </div>
      )}

    </div>
  );
}

Object.assign(window, { useConversation, ConversationModal });
