// Browser TTS for the Orchestrator. Wraps window.speechSynthesis with:
//   - Polish voice picker (waits for `voiceschanged` since voices load async)
//   - Markdown stripper so raw `**bold**` / `[link](url)` / fenced code never
//     reach the speech engine
//   - Sentence chunker that splits on `.!?…` (and re-splits oversized chunks
//     on `,;:—–`) so each utterance has natural prosody and we can cancel
//     mid-message between chunks rather than mid-sentence
//   - One queue, drained in order; speakBlocks(blocks) appends; cancel() clears
//   - iOS PWA unlock: queueing audio without a user gesture is silently
//     blocked, so unlock() runs an empty utterance from a click handler to
//     warm the audio path. Persisted to localStorage so we only do it once.
//
// Published as window.useTts + window.stripMarkdownForSpeech for tests.

const { useEffect: _tUseEffect, useRef: _tUseRef, useState: _tUseState, useCallback: _tUseCallback, useMemo: _tUseMemo } = React;

const _TTS_LANG = 'pl-PL';
const _TTS_RATE = 1.05;
const _TTS_PITCH = 1.0;
const _TTS_CHUNK_MAX = 180;     // chars before we sub-split on commas
const _TTS_UNLOCK_KEY = 'hub-tts-unlocked';

// Markdown → plain prose. Aggressively strips formatting that would be read
// out as literal characters. Preserves sentence punctuation.
function stripMarkdownForSpeech(input) {
  if (!input) return '';
  let s = String(input);
  s = s.replace(/```[\s\S]*?```/g, ' ');                    // fenced code → drop
  s = s.replace(/`([^`]+)`/g, '$1');                        // inline code → keep text
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');              // images → drop
  s = s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');            // [text](url) → text
  s = s.replace(/<https?:\/\/[^>\s]+>/g, ' ');              // bare angle URLs → drop
  s = s.replace(/https?:\/\/\S+/g, ' ');                    // bare URLs → drop
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, '');                 // headings markers
  s = s.replace(/^\s{0,3}>\s?/gm, '');                      // blockquote markers
  s = s.replace(/^\s{0,3}([-*+•]|\d+\.)\s+/gm, '');         // list markers (incl. unicode bullet)
  s = s.replace(/^\s*[-*_]{3,}\s*$/gm, ' ');                // horizontal rules
  s = s.replace(/(\*\*|__)(.+?)\1/g, '$2');                 // bold
  s = s.replace(/(\*|_)(.+?)\1/g, '$2');                    // italics
  s = s.replace(/~~(.+?)~~/g, '$1');                        // strikethrough
  s = s.replace(/\|/g, ' ');                                // table pipes
  s = s.replace(/<[^>]+>/g, ' ');                           // raw HTML tags
  // Arrow glyphs: read as a small pause rather than literal "strzałka".
  s = s.replace(/[→←↑↓⇒⇐⇑⇓⟶⟵⟹⟸▶◀▲▼]/g, ' ');
  // Strip emoji / pictographic glyphs (Unicode plane ranges) — TTS engines
  // either pronounce them as their full emoji name (jarring) or trip on them.
  // Covers U+1F300..U+1FAFF (emoji), U+2600..U+27BF (dingbats/misc symbols),
  // U+FE0F variation selector, and zero-width joiners used in compound emoji.
  s = s.replace(
    /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}]/gu,
    ' ',
  );
  s = s.replace(/\s+/g, ' ').trim();                        // collapse whitespace
  return s;
}

// Split text into ≤_TTS_CHUNK_MAX‑char chunks ending at sentence punctuation,
// re‑splitting on softer pause marks if a single sentence runs too long.
function chunkForSpeech(text) {
  if (!text) return [];
  // First pass: greedy split at sentence terminators (with optional
  // closing quotes/parens). The trailing punctuation stays with the chunk
  // so the engine renders the natural pause.
  const sentenceRe = /[^.!?…]+[.!?…]+["')\]]*\s*/g;
  const out = [];
  let m;
  let lastIdx = 0;
  while ((m = sentenceRe.exec(text)) !== null) {
    out.push(m[0].trim());
    lastIdx = sentenceRe.lastIndex;
  }
  const tail = text.slice(lastIdx).trim();
  if (tail) out.push(tail);

  // Second pass: any chunk longer than the max gets re-split on , ; : — – tokens.
  const final = [];
  for (const c of out) {
    if (c.length <= _TTS_CHUNK_MAX) { final.push(c); continue; }
    const parts = c.split(/([,;:—–])\s+/);
    let buf = '';
    for (let i = 0; i < parts.length; i += 2) {
      const piece = (parts[i] || '') + (parts[i + 1] || '');
      if (!piece.trim()) continue;
      if ((buf + ' ' + piece).trim().length > _TTS_CHUNK_MAX && buf) {
        final.push(buf.trim());
        buf = piece;
      } else {
        buf = (buf ? buf + ' ' : '') + piece;
      }
    }
    if (buf.trim()) final.push(buf.trim());
  }
  return final.filter(Boolean);
}

// Pick the most natural-sounding Polish voice. iOS exposes "Zosia" / "Krzysztof"
// (Apple system voices, very good); Chrome picks Google or eSpeak depending
// on platform. Falls back to any pl-* voice, then any voice.
function pickPolishVoice(voices) {
  if (!Array.isArray(voices) || !voices.length) return null;
  const norm = (v) => ({
    v,
    name: (v.name || '').toLowerCase(),
    lang: (v.lang || '').toLowerCase(),
  });
  const list = voices.map(norm);
  const score = (it) => {
    let n = 0;
    if (it.lang.startsWith('pl')) n += 100;
    if (it.lang === 'pl-pl') n += 10;
    if (it.name.includes('zosia')) n += 30;
    if (it.name.includes('krzysztof')) n += 25;
    if (it.name.includes('ewa')) n += 20;
    if (it.name.includes('google')) n += 5;
    if (it.name.includes('enhanced') || it.name.includes('premium')) n += 8;
    return n;
  };
  const ranked = list.slice().sort((a, b) => score(b) - score(a));
  if (ranked.length && ranked[0].lang.startsWith('pl')) return ranked[0].v;
  return null;
}

function _readUnlocked() {
  try { return localStorage.getItem(_TTS_UNLOCK_KEY) === '1'; }
  catch (e) { return false; }
}

function _writeUnlocked(v) {
  try { localStorage.setItem(_TTS_UNLOCK_KEY, v ? '1' : '0'); } catch (e) { /* ignore */ }
}

// Remote engine plumbing — backend owns provider selection (ElevenLabs /
// OpenAI / Gemini); we just feed the chunked text through a chain of
// <audio> elements, with `preload='auto'` to let the browser pre‑fetch
// chunk N+1 while N plays.
function _buildRemoteUrl(text, engine, voice) {
  const base = (typeof window !== 'undefined' && typeof window.HUB_BASE_PATH === 'string')
    ? window.HUB_BASE_PATH : '';
  const params = new URLSearchParams({ text, engine });
  if (voice) params.set('voice', voice);
  return base + '/api/orchestrator/tts?' + params.toString();
}

// Returns a controller {cancel}. Plays chunks sequentially via Audio.
//
// Concurrency: at most TWO upstream fetches in flight at once — the chunk
// currently playing, and the next one being pre-fetched in the background.
// Creating all <audio> elements upfront with preload='auto' triggers as many
// parallel requests as the browser allows (typically 6 per host) and easily
// exceeds providers' concurrent-request limits (ElevenLabs caps at 10).
// Lazy + N+1 lookahead keeps latency low while staying well under the cap.
//
// Error handling: a 4xx/5xx on one chunk skips to the next so a single bad
// upstream response doesn't kill playback of surviving sentences.
function _startRemotePlayback(initialChunks, engine, voice, onDone, onAllFailed) {
  let cancelled = false;
  let ended = false;        // streamer signaled "no more chunks coming"
  let current = null;       // <audio> currently playing (idx)
  let nextAudio = null;     // <audio> pre-fetching for idx+1
  let nextIdx = -1;
  let cursor = 0;           // next-to-play idx, advanced as audio ends
  // `queue` is mutable so streaming consumers can `append(...moreChunks)`
  // mid-playback. Once we hit `cursor === queue.length` we either go idle
  // (waiting for append) or fire onDone (when ended() was called).
  const queue = Array.isArray(initialChunks) ? [...initialChunks] : [];
  let playedAny = false;    // did ANY chunk actually produce audio?

  // Finalise (once). If not a single chunk played (engine down / missing API
  // key), hand off to onAllFailed (browser-TTS fallback) instead of a silent
  // onDone. Guarded because onerror AND play()'s rejection can both fire for the
  // same failed chunk, and either can reach finish() once the stream has ended.
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    if (!playedAny && queue.length > 0 && typeof onAllFailed === 'function') {
      try { onAllFailed(); } catch (e) { /* swallow */ }
    } else {
      try { if (onDone) onDone(); } catch (e) { /* swallow */ }
    }
  };

  const buildAudio = (idx) => {
    if (idx < 0 || idx >= queue.length) return null;
    const a = new Audio(_buildRemoteUrl(queue[idx], engine, voice));
    a.preload = 'auto';
    return a;
  };

  const playAt = (idx) => {
    if (cancelled) return;
    cursor = idx;
    if (idx >= queue.length) {
      // Idle. If the streamer already signaled end, finalise; otherwise wait.
      if (ended) finish();
      return;
    }
    if (nextAudio && nextIdx === idx) {
      current = nextAudio;
      nextAudio = null;
      nextIdx = -1;
    } else {
      current = buildAudio(idx);
    }
    if (!current) {
      finish();
      return;
    }
    // Count a chunk as "played" the moment audio actually starts — not only on
    // onended — so a chunk that plays audibly then drops mid-stream doesn't look
    // like "never played" and route to onAllFailed (which would re-read the
    // whole message via the browser fallback, repeating the heard prefix).
    current.onplaying = () => { playedAny = true; };
    current.onended = () => { current = null; playedAny = true; playAt(idx + 1); };
    current.onerror = () => { current = null; playAt(idx + 1); };

    if (idx + 1 < queue.length) {
      nextAudio = buildAudio(idx + 1);
      nextIdx = idx + 1;
    }

    const p = current.play();
    if (p && typeof p.catch === 'function') {
      p.catch(() => { current = null; playAt(idx + 1); });
    }
  };

  playAt(0);

  return {
    cancel: () => {
      cancelled = true;
      [current, nextAudio].forEach((a) => {
        if (!a) return;
        try { a.pause(); } catch (e) { /* ignore */ }
        try { a.removeAttribute('src'); a.load(); } catch (e) { /* ignore */ }
      });
      current = null;
      nextAudio = null;
      nextIdx = -1;
    },
    append: (...moreChunks) => {
      if (cancelled || ended) return;
      if (!moreChunks.length) return;
      const wasIdle = !current && cursor >= queue.length;
      queue.push(...moreChunks);
      if (wasIdle) playAt(cursor);
    },
    endStream: () => {
      if (cancelled || ended) return;
      ended = true;
      if (!current && cursor >= queue.length) finish();
    },
    // True once the controller can no longer accept appends (a new turn must
    // start a FRESH controller rather than append into this finalized queue,
    // which append() would silently drop).
    isEnded: () => ended || cancelled,
    // Re-open an ended-but-not-FINISHED stream for the next turn's blocks.
    // The backend emits end_flow right after a turn's last block, so turn B's
    // first block routinely lands on an `ended` controller whose tail is still
    // draining — without reopen, the cancel+fresh branch would CLIP turn A's
    // tail audio. Returns false once finished/cancelled (caller starts fresh).
    reopen: (...moreChunks) => {
      if (cancelled || finished) return false;
      if (!moreChunks.length) return true;
      ended = false;
      const wasIdle = !current && cursor >= queue.length;
      queue.push(...moreChunks);
      if (wasIdle) playAt(cursor);
      return true;
    },
  };
}

// Cross-path de-dup window: the composer auto-speak effect, the read-aloud
// watcher, and the manual button can all request the SAME assistant turn within
// a moment of each other, but their de-dup keys live in different namespaces
// (turn_idx vs assistant uuid) so the shared spokenKeysRef can't catch it.
// speakBlocks suppresses an identical text re-requested within this window.
const _TTS_DEDUP_WINDOW_MS = 3000;

function useTts() {
  const browserSupported = typeof window !== 'undefined'
    && typeof window.speechSynthesis !== 'undefined'
    && typeof window.SpeechSynthesisUtterance !== 'undefined';
  // The backend `/api/orchestrator/tts` is always reachable when the
  // dashboard is reachable, so "remote engines work" iff fetch + Audio
  // exist — both are universally available where this app runs.
  const supported = browserSupported || (typeof Audio !== 'undefined');

  // Settings drives engine choice. Default is 'elevenlabs' (set in
  // settings-view defaults) but we tolerate it being undefined here so the
  // hook still works if someone calls it before settings are written.
  const [_settings] = (window.useSettings || (() => [{}, () => {}]))();
  const engine = (_settings && _settings.ttsEngine) || 'elevenlabs';
  const voiceId = (_settings && _settings.ttsVoice) || null;

  const [voices, setVoices] = _tUseState([]);
  const [unlocked, setUnlocked] = _tUseState(_readUnlocked);
  const [speaking, setSpeaking] = _tUseState(false);
  const [activeKey, setActiveKey] = _tUseState(null);

  const queueRef = _tUseRef([]);                  // pending utterance specs (browser engine)
  const currentRef = _tUseRef(null);              // currently speaking utterance (browser engine)
  const remoteCtrlRef = _tUseRef(null);           // active remote-playback controller
  const activeKeyRef = _tUseRef(null);            // who owns the queue (msg id)
  const lastSpokenRef = _tUseRef({ text: null, at: 0 });  // cross-path same-text de-dup

  // Voices load asynchronously on most browsers — listen for `voiceschanged`
  // and refresh. On Safari the event fires once after first getVoices() call.
  _tUseEffect(() => {
    if (!supported) return undefined;
    const synth = window.speechSynthesis;
    const refresh = () => { try { setVoices(synth.getVoices() || []); } catch (e) {} };
    refresh();
    synth.addEventListener('voiceschanged', refresh);
    return () => { try { synth.removeEventListener('voiceschanged', refresh); } catch (e) {} };
  }, [supported]);

  const voice = _tUseRef(null);
  _tUseEffect(() => { voice.current = pickPolishVoice(voices); }, [voices]);

  const _drainNext = _tUseCallback(() => {
    if (!supported) return;
    const synth = window.speechSynthesis;
    const next = queueRef.current.shift();
    if (!next) {
      currentRef.current = null;
      activeKeyRef.current = null;
      setSpeaking(false);
      setActiveKey(null);
      if (window.HubAudioBus) window.HubAudioBus.release('tts');
      return;
    }
    const u = new SpeechSynthesisUtterance(next.text);
    u.lang = _TTS_LANG;
    u.rate = _TTS_RATE;
    u.pitch = _TTS_PITCH;
    if (voice.current) u.voice = voice.current;
    u.onend = () => { _drainNext(); };
    u.onerror = (ev) => {
      // 'canceled' / 'interrupted' fire on cancel(); both are normal.
      if (ev && (ev.error === 'canceled' || ev.error === 'interrupted')) return;
      _drainNext();
    };
    currentRef.current = u;
    try { synth.speak(u); } catch (e) { _drainNext(); }
  }, [supported]);

  const cancel = _tUseCallback(() => {
    queueRef.current = [];
    activeKeyRef.current = null;
    currentRef.current = null;
    setSpeaking(false);
    setActiveKey(null);
    if (remoteCtrlRef.current) {
      try { remoteCtrlRef.current.cancel(); } catch (e) { /* ignore */ }
      remoteCtrlRef.current = null;
    }
    if (browserSupported) {
      try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    }
    if (window.HubAudioBus) window.HubAudioBus.release('tts');
  }, [browserSupported]);

  // Register on the cross-subsystem audio bus. The bus pauses TTS (= cancel())
  // whenever another source (media clip / conversation) takes the channel.
  // Capture `cancel` through a ref so the latest closure is always invoked.
  const cancelRef = _tUseRef(cancel);
  _tUseEffect(() => { cancelRef.current = cancel; }, [cancel]);
  _tUseEffect(() => {
    if (!window.HubAudioBus) return undefined;
    const un = window.HubAudioBus.register('tts', {
      kind: 'tts',
      pause: () => { if (cancelRef.current) cancelRef.current(); },
    });
    return () => { try { un && un(); } catch (e) { /* ignore */ } };
  }, []);

  // Append chunks for one logical message. `key` is a stable id (e.g. turn_idx
  // or 'live') used by per-message play buttons to know which one is active.
  // Dispatches to the browser engine (speechSynthesis) or to a remote engine
  // (server-side ElevenLabs / OpenAI / Gemini proxy) based on settings.
  const speakBlocks = _tUseCallback((blocks, opts) => {
    const o = opts || {};
    const key = o.key != null ? String(o.key) : null;
    const arr = Array.isArray(blocks) ? blocks : [blocks];
    const chunkTexts = [];
    for (const b of arr) {
      const text = (b && typeof b === 'object')
        ? (b.text != null ? b.text : (b.content || ''))
        : String(b || '');
      if (!text) continue;
      const stripped = stripMarkdownForSpeech(text);
      if (!stripped) continue;
      for (const c of chunkForSpeech(stripped)) chunkTexts.push(c);
    }
    if (!chunkTexts.length) return;
    // Cross-path de-dup: skip an identical text re-requested within the window
    // (a turn reaching speakBlocks twice via two paths whose keys never collide)
    // — checked BEFORE cancel() so the in-flight first read isn't interrupted.
    const _joined = chunkTexts.join(' ');
    const _now = Date.now();
    if (lastSpokenRef.current.text === _joined && (_now - lastSpokenRef.current.at) < _TTS_DEDUP_WINDOW_MS) return;
    lastSpokenRef.current = { text: _joined, at: _now };
    // Replace any in-flight queue so the latest message wins.
    cancel();
    if (window.HubAudioBus) window.HubAudioBus.requestActive('tts');
    activeKeyRef.current = key;
    setActiveKey(key);
    setSpeaking(true);
    if (engine === 'browser') {
      if (!browserSupported) { setSpeaking(false); setActiveKey(null); return; }
      queueRef.current = chunkTexts.map((t) => ({ text: t }));
      _drainNext();
    } else {
      const ctrl = _startRemotePlayback(chunkTexts, engine, voiceId, () => {
        // Drain complete — clear UI state.
        remoteCtrlRef.current = null;
        activeKeyRef.current = null;
        setSpeaking(false);
        setActiveKey(null);
        if (window.HubAudioBus) window.HubAudioBus.release('tts');
      }, () => {
        // Every remote chunk failed (engine down / missing API key) — fall back
        // to the browser's native SpeechSynthesis so the reply is still read
        // aloud instead of silently producing nothing. activeKey/speaking stay
        // as set above, so the UI doesn't flicker during the hand-off.
        remoteCtrlRef.current = null;
        if (browserSupported) {
          queueRef.current = chunkTexts.map((t) => ({ text: t }));
          _drainNext();
        } else {
          activeKeyRef.current = null;
          setSpeaking(false);
          setActiveKey(null);
        }
      });
      remoteCtrlRef.current = ctrl;
      // Bounded queue: all chunks are known up-front. Without this call the
      // controller's `ended` flag stays false, `onDone` never fires when the
      // last chunk finishes, and the speaking/activeKey state gets stuck.
      // Streaming consumers (`streamChunk`) call `endStream` later when the
      // model finalises; speakBlocks finalises immediately.
      try { ctrl.endStream(); } catch (e) { /* ignore */ }
    }
  }, [browserSupported, engine, voiceId, cancel, _drainNext]);

  // Streaming TTS: append a single text segment to the active queue without
  // cancelling. Starts a new playback if nothing is in flight; otherwise the
  // segment plays after whatever's currently buffered. Use `endStream(key)`
  // to signal that no more segments will come, so the controller fires
  // `onDone` once the queue drains.
  const streamChunk = _tUseCallback((text, opts) => {
    if (!supported) return;
    if (typeof text !== 'string' || !text.trim()) return;
    const stripped = stripMarkdownForSpeech(text);
    if (!stripped) return;
    const newChunks = chunkForSpeech(stripped);
    if (!newChunks.length) return;
    const o = opts || {};
    const key = o.key != null ? String(o.key) : null;
    if (engine === 'browser') {
      if (!browserSupported) return;
      queueRef.current.push(...newChunks.map((t) => ({ text: t })));
      if (!currentRef.current) {
        if (window.HubAudioBus) window.HubAudioBus.requestActive('tts');
        activeKeyRef.current = key;
        setActiveKey(key);
        setSpeaking(true);
        _drainNext();
      }
      return;
    }
    if (remoteCtrlRef.current && activeKeyRef.current === key) {
      const liveCtrl = remoteCtrlRef.current;
      if (typeof liveCtrl.append === 'function'
          && !(typeof liveCtrl.isEnded === 'function' && liveCtrl.isEnded())) {
        liveCtrl.append(...newChunks);
        return;
      }
      // Same key but the controller is already ended (this key's end_flow fired
      // — i.e. the NEXT turn's first block while the previous tail still
      // drains). Re-open it so the tail is NOT clipped; speaking stays true
      // throughout, so a conversation drain-poll won't re-arm the mic early.
      // Falls through to a fresh controller only once it fully finished.
      if (typeof liveCtrl.reopen === 'function' && liveCtrl.reopen(...newChunks)) return;
    }
    // Different key, or the active controller is cancelled/finished → cancel
    // any orphan and start a FRESH controller. Appending into an ended
    // controller silently drops the chunks (its append() bails on `ended`) —
    // that was the 'reads only the last block' / 'on-voice reads one turn then
    // stops' bug. The onDone is guarded by identity so a draining old turn
    // can't null a newer turn's controller.
    if (remoteCtrlRef.current) { try { remoteCtrlRef.current.cancel(); } catch (e) { /* ignore */ } }
    if (window.HubAudioBus) window.HubAudioBus.requestActive('tts');
    activeKeyRef.current = key;
    setActiveKey(key);
    setSpeaking(true);
    const _ctrl = _startRemotePlayback(newChunks, engine, voiceId, () => {
      if (remoteCtrlRef.current !== _ctrl) return;  // a newer turn replaced us
      remoteCtrlRef.current = null;
      activeKeyRef.current = null;
      setSpeaking(false);
      setActiveKey(null);
      if (window.HubAudioBus) window.HubAudioBus.release('tts');
    });
    remoteCtrlRef.current = _ctrl;
  }, [browserSupported, engine, voiceId, supported, _drainNext]);

  // Signal that streaming is done so the in-flight remote controller fires
  // its onDone once the buffered queue plays out. No-op for browser engine
  // (the queueRef drains naturally via _drainNext recursion).
  const endStream = _tUseCallback((opts) => {
    const o = opts || {};
    const key = o.key != null ? String(o.key) : null;
    if (engine === 'browser') return;
    const ctrl = remoteCtrlRef.current;
    if (!ctrl || typeof ctrl.endStream !== 'function') return;
    if (key != null && activeKeyRef.current !== key) return;
    try { ctrl.endStream(); } catch (e) { /* ignore */ }
  }, [engine]);

  // iOS / Safari refuse to play audio that wasn't initiated from a user
  // gesture. Both engines need this priming; for the browser engine we burn
  // a near-silent utterance, for remote engines we play+immediately-pause a
  // tiny audio so subsequent <audio>.play() outside-of-gesture works.
  const unlock = _tUseCallback(() => {
    if (browserSupported) {
      try {
        const u = new SpeechSynthesisUtterance(' ');
        u.volume = 0.01;
        u.lang = _TTS_LANG;
        window.speechSynthesis.speak(u);
      } catch (e) { /* ignore */ }
    }
    if (typeof Audio !== 'undefined') {
      try {
        // Tiny silent WAV payload — 16 bits, 1 sample, 24kHz, ≈48 bytes.
        // Browsers accept this as "audio played in user gesture" and lift
        // their autoplay block for the rest of the session.
        const a = new Audio('data:audio/wav;base64,UklGRiwAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQgAAAAAAAAAAAAAAA==');
        a.volume = 0.01;
        const p = a.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (e) { /* ignore */ }
    }
    _writeUnlocked(true);
    setUnlocked(true);
  }, [browserSupported]);

  // Read a short test phrase from a click — gives the user immediate
  // confirmation that audio works AND piggy-backs the unlock gesture.
  // Routes through whichever engine is currently configured.
  const speakTest = _tUseCallback(() => {
    cancel();
    if (window.HubAudioBus) window.HubAudioBus.requestActive('tts');
    _writeUnlocked(true);
    setUnlocked(true);
    const text = 'OK, słyszysz mnie? Zaczynam czytać odpowiedzi.';
    setSpeaking(true);
    if (engine === 'browser') {
      if (!browserSupported) { setSpeaking(false); return; }
      queueRef.current = [{ text }];
      _drainNext();
    } else {
      remoteCtrlRef.current = _startRemotePlayback([text], engine, voiceId, () => {
        remoteCtrlRef.current = null;
        setSpeaking(false);
        if (window.HubAudioBus) window.HubAudioBus.release('tts');
      });
    }
  }, [browserSupported, engine, voiceId, cancel, _drainNext]);

  // Best-effort cleanup on unmount.
  _tUseEffect(() => () => {
    try { if (browserSupported) window.speechSynthesis.cancel(); } catch (e) {}
    if (remoteCtrlRef.current) { try { remoteCtrlRef.current.cancel(); } catch (e) {} }
  }, [browserSupported]);

  // Memoize the public surface so consumers can put `tts` (or specific fields)
  // into their useEffect deps without triggering a re-fire on every render.
  // The callbacks are already useCallback-stable; speaking/activeKey/unlocked
  // are useState-stable; voices is a state slice that legitimately changes
  // when getVoices() resolves.
  return _tUseMemo(() => ({
    supported,
    voices,
    voice: voice.current,
    speaking,
    activeKey,
    unlocked,
    speakBlocks,
    speakTest,
    streamChunk,
    endStream,
    cancel,
    unlock,
  }), [supported, voices, speaking, activeKey, unlocked, speakBlocks, speakTest, streamChunk, endStream, cancel, unlock]);
}

Object.assign(window, { useTts, stripMarkdownForSpeech, chunkForSpeech, pickPolishVoice });
