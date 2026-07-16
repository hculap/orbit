// Voice capture hook for the Orchestrator input.
//   - Records mic audio via MediaRecorder (opus/webm preferred)
//   - Runs an RMS-based VAD over an AnalyserNode for auto-stop on silence
//   - POSTs the full-audio-so-far to /api/orchestrator/transcribe every ~2s;
//     each response is a fresh full transcript that overrides the input
//   - One final POST on stop with the complete blob → onFinal
//
// Published as window.useVoiceCapture.

const { useEffect: _useEffect, useRef: _useRef, useState: _useState, useCallback: _useCallback } = React;

const _VOICE_THRESHOLD = 0.015;     // RMS below this counts as silence
const _VOICE_VAD_TICK_MS = 50;      // VAD sampling cadence
const _VOICE_WARMUP_MS = 500;       // ignore silence right after start
const _VOICE_LEVEL_DELTA = 0.02;    // throttle setLevel renders

function _pickRecorderMime() {
  if (typeof MediaRecorder === 'undefined') return '';
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  for (const m of candidates) {
    try { if (MediaRecorder.isTypeSupported(m)) return m; } catch (e) { /* ignore */ }
  }
  return '';
}

function useVoiceCapture(opts) {
  const o = opts || {};
  const onPartial = o.onPartial;
  const onFinal = o.onFinal;
  const onError = o.onError;
  const language = o.language || 'pl';
  const silenceMs = typeof o.silenceMs === 'number' ? o.silenceMs : 1500;
  const chunkMs = typeof o.chunkMs === 'number' ? o.chunkMs : 2000;
  const maxRecordingMs = typeof o.maxRecordingMs === 'number' ? o.maxRecordingMs : 5 * 60 * 1000;
  const baseUrl = typeof o.baseUrl === 'string' ? o.baseUrl : (window.HUB_BASE_PATH || '');
  // Pin capture to a specific input (e.g. the built-in iPhone mic in the car —
  // opening a Bluetooth mic flips the car's A2DP link to HFP). Best-effort:
  // if the exact device is gone, start() retries unpinned.
  const preferredDeviceId = typeof o.preferredDeviceId === 'string' ? o.preferredDeviceId : '';

  const [state, setState] = _useState('idle');
  const [level, setLevel] = _useState(0);
  const [lastError, setLastError] = _useState(null);

  const stateRef = _useRef('idle');
  const streamRef = _useRef(null);
  const audioCtxRef = _useRef(null);
  const analyserRef = _useRef(null);
  const recorderRef = _useRef(null);
  const chunksRef = _useRef([]);
  const mimeRef = _useRef('');

  const vadTimerRef = _useRef(null);
  const chunkTimerRef = _useRef(null);
  const inflightRef = _useRef(false);
  const inflightAbortRef = _useRef(null);

  const silenceAccumRef = _useRef(0);
  const speechSeenRef = _useRef(false);
  const startedAtRef = _useRef(0);
  const lastLevelEmitRef = _useRef(0);
  // Generation counter: abort() bumps it so any in-flight stop() continuation
  // (which captured the old value) bails out instead of tearing down a NEWER
  // recording or delivering a stale onFinal.
  const epochRef = _useRef(0);
  // In-flight FINAL transcribe POST — abort() cancels it outright (the partial
  // in-flight controller is inflightAbortRef; the final one needs its own).
  const finalAbortRef = _useRef(null);
  // Ambient-calibrated VAD threshold (recomputed each start() from the warmup
  // window — cabin noise changes with speed, a fixed 0.015 RMS never goes
  // silent on the road, so auto-stop would never fire). Tracks the warmup
  // MINIMUM, not the average: if the user starts talking during the 500ms
  // warmup, an average would inflate the threshold above speech RMS (auto-stop
  // dead, hasSpeech poisoned) — the min keeps tracking the noise floor between
  // syllables. Capped so the threshold can never exceed quiet speech.
  const thresholdRef = _useRef(_VOICE_THRESHOLD);
  const ambientMinRef = _useRef(Infinity);
  // Timestamp of the last above-threshold frame → hasSpeech() ("the user is
  // speaking RIGHT NOW-ish"), unlike the sticky speechSeenRef auto-stop gate.
  const lastSpeechAtRef = _useRef(0);

  const onPartialRef = _useRef(onPartial);
  const onFinalRef = _useRef(onFinal);
  const onErrorRef = _useRef(onError);
  _useEffect(() => { onPartialRef.current = onPartial; }, [onPartial]);
  _useEffect(() => { onFinalRef.current = onFinal; }, [onFinal]);
  _useEffect(() => { onErrorRef.current = onError; }, [onError]);

  const setStateBoth = _useCallback((s) => { stateRef.current = s; setState(s); }, []);

  const reportError = _useCallback((msg) => {
    setLastError(msg);
    setStateBoth('error');
    try { if (onErrorRef.current) onErrorRef.current(msg); } catch (e) { /* swallow */ }
  }, [setStateBoth]);

  const teardownAudio = _useCallback(() => {
    if (vadTimerRef.current) { clearInterval(vadTimerRef.current); vadTimerRef.current = null; }
    if (chunkTimerRef.current) { clearInterval(chunkTimerRef.current); chunkTimerRef.current = null; }
    if (inflightAbortRef.current) {
      try { inflightAbortRef.current.abort(); } catch (e) { /* ignore */ }
      inflightAbortRef.current = null;
    }
    inflightRef.current = false;
    if (streamRef.current) {
      try { streamRef.current.getTracks().forEach((t) => { try { t.stop(); } catch (e) {} }); } catch (e) {}
      streamRef.current = null;
    }
    if (audioCtxRef.current) {
      try { audioCtxRef.current.close(); } catch (e) {}
      audioCtxRef.current = null;
    }
    analyserRef.current = null;
    recorderRef.current = null;
    setLevel(0);
    lastLevelEmitRef.current = 0;
  }, []);

  // Build a fresh Blob from everything captured so far. The chunks array
  // contains segments emitted by MediaRecorder; concatenating them produces
  // a valid container for the transcribe endpoint.
  const buildBlob = _useCallback(() => {
    const parts = chunksRef.current;
    if (!parts.length) return null;
    const type = parts[0].type || mimeRef.current || 'audio/webm';
    return new Blob(parts, { type });
  }, []);

  const postChunk = _useCallback(async (blob, isFinal) => {
    const ctrl = new AbortController();
    if (!isFinal) inflightAbortRef.current = ctrl;
    else finalAbortRef.current = ctrl;  // abort() can cancel the final POST too
    const fd = new FormData();
    const ext = (blob.type || '').includes('mp4') ? 'mp4' : 'webm';
    fd.append('audio', blob, 'chunk.' + ext);
    fd.append('language', language);
    try {
      const res = await fetch(baseUrl + '/api/orchestrator/transcribe', {
        method: 'POST', body: fd, signal: ctrl.signal,
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      const text = (json && typeof json.text === 'string') ? json.text : '';
      return text;
    } finally {
      if (!isFinal && inflightAbortRef.current === ctrl) inflightAbortRef.current = null;
      if (isFinal && finalAbortRef.current === ctrl) finalAbortRef.current = null;
    }
  }, [baseUrl, language]);

  const tickChunk = _useCallback(async () => {
    if (stateRef.current !== 'recording') return;
    if (inflightRef.current) return;
    const rec = recorderRef.current;
    if (!rec || rec.state !== 'recording') return;
    try { rec.requestData(); } catch (e) { /* ignore */ }
    // Yield a microtask so dataavailable handler can flush the new segment.
    await new Promise((r) => setTimeout(r, 0));
    const blob = buildBlob();
    if (!blob || blob.size === 0) return;
    inflightRef.current = true;
    try {
      const text = await postChunk(blob, false);
      try { if (onPartialRef.current) onPartialRef.current(text); } catch (e) { /* swallow */ }
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      console.warn('[voice] chunk failed', err);
    } finally {
      inflightRef.current = false;
    }
  }, [buildBlob, postChunk]);

  // Forward declaration so the VAD loop can call stop() on auto-stop.
  const stopRef = _useRef(null);

  const tickVad = _useCallback(() => {
    if (stateRef.current !== 'recording') return;
    const analyser = analyserRef.current;
    if (!analyser) return;
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i += 1) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    if (Math.abs(rms - lastLevelEmitRef.current) > _VOICE_LEVEL_DELTA) {
      lastLevelEmitRef.current = rms;
      setLevel(rms);
    }
    const elapsed = performance.now() - startedAtRef.current;
    // Warmup grace: don't accumulate silence right after start, otherwise a
    // user who hasn't begun speaking would auto-stop instantly. The warmup
    // window doubles as ambient-noise sampling: threshold = max(base,
    // ambient*2.5) so a moving-car cabin (road noise >> 0.015) still gets a
    // working silence detector instead of recording until maxRecordingMs.
    if (elapsed < _VOICE_WARMUP_MS) {
      silenceAccumRef.current = 0;
      ambientMinRef.current = Math.min(ambientMinRef.current, rms);
      const ambient = Number.isFinite(ambientMinRef.current) ? ambientMinRef.current : 0;
      thresholdRef.current = Math.min(
        Math.max(_VOICE_THRESHOLD, ambient * 2.5),
        0.06,  // hard cap: never above quiet speech RMS
      );
    } else if (rms < thresholdRef.current) {
      silenceAccumRef.current += _VOICE_VAD_TICK_MS;
    } else {
      silenceAccumRef.current = 0;
      speechSeenRef.current = true;
      lastSpeechAtRef.current = performance.now();
    }
    // Speech-detected gate: only auto-stop after we've heard at least one
    // above-threshold frame. Dead-quiet recordings would otherwise terminate
    // the moment the warmup window closes.
    if (speechSeenRef.current && silenceAccumRef.current >= silenceMs) {
      if (stopRef.current) stopRef.current();
      return;
    }
    if (elapsed > maxRecordingMs) {
      if (stopRef.current) stopRef.current();
    }
  }, [silenceMs, maxRecordingMs]);

  const start = _useCallback(async () => {
    if (stateRef.current !== 'idle') return;
    setLastError(null);
    speechSeenRef.current = false;
    silenceAccumRef.current = 0;
    chunksRef.current = [];
    // Recalibrate the VAD threshold from THIS recording's warmup window —
    // ambient noise changes between turns (speed, windows, music).
    thresholdRef.current = _VOICE_THRESHOLD;
    ambientMinRef.current = Infinity;
    lastSpeechAtRef.current = 0;
    // Epoch snapshot: abort() during the getUserMedia await must kill THIS
    // start — otherwise the mic opens into live TTS playback and the orphan
    // capture later posts a polluted transcript.
    const epoch = epochRef.current;
    const baseAudio = { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true };
    let stream = null;
    try {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: preferredDeviceId
            ? { ...baseAudio, deviceId: { exact: preferredDeviceId } }
            : baseAudio,
        });
      } catch (errPinned) {
        // Pinned device gone (unplugged BT, id rotated) → retry unpinned
        // rather than failing the whole capture.
        if (!preferredDeviceId) throw errPinned;
        stream = await navigator.mediaDevices.getUserMedia({ audio: baseAudio });
      }
    } catch (err) {
      const msg = 'Brak dostępu do mikrofonu';
      setLastError(msg);
      setStateBoth('error');
      try { if (onErrorRef.current) onErrorRef.current(msg); } catch (e) {}
      return;
    }
    if (epochRef.current !== epoch) {
      // Aborted while we awaited the grant → drop the freshly opened tracks.
      try { stream.getTracks().forEach((t) => { try { t.stop(); } catch (e) {} }); } catch (e) {}
      return;
    }
    try {
      streamRef.current = stream;
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      audioCtxRef.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0;
      source.connect(analyser);
      analyserRef.current = analyser;

      const mime = _pickRecorderMime();
      mimeRef.current = mime;
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      recorderRef.current = recorder;
      recorder.ondataavailable = (e) => {
        if (e && e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.start();

      startedAtRef.current = performance.now();
      setStateBoth('recording');
      vadTimerRef.current = setInterval(tickVad, _VOICE_VAD_TICK_MS);
      // Chunked partial transcription is opt-in: callers who only need the
      // final transcript (e.g. conversation mode) pass chunkMs <= 0 so we
      // don't run requestData() mid-recording. requestData()'s emitted
      // chunks each carry their own webm header; concatenating several
      // produces an invalid container that Whisper parses as empty audio,
      // which is what triggered the "audio too short" loop in conversation
      // mode where each turn is brief.
      if (Number.isFinite(chunkMs) && chunkMs > 0) {
        chunkTimerRef.current = setInterval(() => { tickChunk(); }, chunkMs);
      }
    } catch (err) {
      teardownAudio();
      reportError('Nie udało się uruchomić nagrywania: ' + (err && err.message ? err.message : String(err)));
    }
  }, [chunkMs, tickVad, tickChunk, teardownAudio, setStateBoth, reportError, preferredDeviceId]);

  const stop = _useCallback(async () => {
    if (stateRef.current !== 'recording') return;
    // Epoch snapshot: if abort() runs while we're awaiting below, it bumps the
    // epoch and ALREADY did teardown + state reset (and may have started a NEW
    // recording). A stale continuation must then do a FULL bail — touching
    // teardownAudio()/setStateBoth() here would kill the new recording's
    // stream/timers while the consumer believes it is live.
    const epoch = epochRef.current;
    setStateBoth('finalizing');
    if (vadTimerRef.current) { clearInterval(vadTimerRef.current); vadTimerRef.current = null; }
    if (chunkTimerRef.current) { clearInterval(chunkTimerRef.current); chunkTimerRef.current = null; }
    // Abort any in-flight partial: the final POST below will re-send the
    // full audio anyway, so the partial response is no longer useful.
    if (inflightAbortRef.current) {
      try { inflightAbortRef.current.abort(); } catch (e) {}
      inflightAbortRef.current = null;
    }
    inflightRef.current = false;

    const recorder = recorderRef.current;
    if (recorder && recorder.state !== 'inactive') {
      await new Promise((resolve) => {
        let done = false;
        const finish = () => { if (done) return; done = true; resolve(); };
        recorder.addEventListener('stop', finish, { once: true });
        try { recorder.stop(); } catch (e) { finish(); }
        // Safety net: if stop never fires, don't block forever.
        setTimeout(finish, 1500);
      });
    }
    if (epochRef.current !== epoch) return;  // aborted mid-finalize → full bail

    const finalBlob = buildBlob();
    let finalErr = null;
    if (finalBlob && finalBlob.size > 0) {
      try {
        const text = await postChunk(finalBlob, true);
        if (epochRef.current !== epoch) return;  // aborted during the POST → discard
        try { if (onFinalRef.current) onFinalRef.current(text); } catch (e) {}
      } catch (err) {
        if (epochRef.current !== epoch) return;  // abort() cancelled the fetch
        finalErr = err;
      }
    }
    if (epochRef.current !== epoch) return;

    teardownAudio();

    if (finalErr) {
      const msg = 'Transkrypcja nie powiodła się: ' + (finalErr && finalErr.message ? finalErr.message : String(finalErr));
      setLastError(msg);
      setStateBoth('error');
      try { if (onErrorRef.current) onErrorRef.current(msg); } catch (e) {}
    } else {
      setStateBoth('idle');
    }
  }, [buildBlob, postChunk, teardownAudio, setStateBoth]);

  // Discard the current capture WITHOUT transcribing: no final POST, no
  // onFinal. Epoch bump comes FIRST so any interleaved stop() continuation
  // sees the stale epoch and bails fully. Used by the conversation loop when
  // an async-result turn lands while the mic is open but silent — nothing of
  // value is recorded, and stop() would burn a Groq call + deliver garbage.
  const abort = _useCallback(() => {
    epochRef.current += 1;
    if (finalAbortRef.current) {
      try { finalAbortRef.current.abort(); } catch (e) {}
      finalAbortRef.current = null;
    }
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== 'inactive') {
      try { recorder.stop(); } catch (e) {}
    }
    teardownAudio();
    chunksRef.current = [];
    setStateBoth('idle');
  }, [teardownAudio, setStateBoth]);

  // "Is the user speaking right now-ish": an above-threshold frame within the
  // last 1.5s. Unlike the sticky speechSeenRef (auto-stop gate), this resets
  // as the user goes quiet — in a noisy cabin speechSeen flips true on the
  // first noisy frame and stays true, which would force the defer path for
  // every async result. Uses the calibrated threshold, so cabin noise alone
  // doesn't count as speech.
  const hasSpeech = _useCallback(
    () => lastSpeechAtRef.current > 0 && (performance.now() - lastSpeechAtRef.current) < 1500,
    [],
  );

  _useEffect(() => { stopRef.current = stop; }, [stop]);

  // Best-effort cleanup on unmount: kill timers, tracks, ctx, in-flight fetch.
  // No final POST — the consumer is gone, there's nothing to deliver to.
  _useEffect(() => () => {
    if (stateRef.current === 'recording' || stateRef.current === 'finalizing') {
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== 'inactive') {
        try { recorder.stop(); } catch (e) {}
      }
      teardownAudio();
      stateRef.current = 'idle';
    }
  }, [teardownAudio]);

  return { state, level, lastError, start, stop, abort, hasSpeech };
}

Object.assign(window, { useVoiceCapture });
