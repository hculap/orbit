// AEC-aware playback loopback for the conversation mode.
//
// Why this exists: Chrome's `getUserMedia({echoCancellation:true})` only
// filters audio that reached the speakers via the WebRTC playout path. Audio
// played through plain `<audio>` elements (or Web Audio API destination) is
// invisible to the AEC reference signal — so when the AI's TTS comes out of
// the same speaker the mic is picking up, AEC does NOTHING. The mic captures
// the AI's voice, the VAD trips, and the loop self-triggers.
//
// The standard workaround (per chromium AEC docs + dev.to write-up cited in
// the design doc): route playback through a local RTCPeerConnection pair so
// the receiver-side `<audio>` is fed via WebRTC, which IS in the AEC
// reference path. Source side is a Web Audio MediaStreamDestinationNode that
// any chunk-audio can be connected to via `createMediaElementSource`.
//
// Public surface (window.createAecLoopback):
//   const lb = await createAecLoopback();
//   const audio = lb.makeAudio(url);   // Audio routed through the loopback
//   audio.play();                      // sound emerges from lb.audioElement
//   ...                                // AEC sees lb.audioElement → cancels
//   lb.close();                        // tear everything down
//
// Notes / gotchas:
//   - The receiver-side `audioElement` MUST be in the DOM (or at least have
//     `play()` called from a user gesture once) to start outputting on iOS.
//     We call `audioElement.play()` ourselves at construction; consumers
//     should ensure the create call happens inside a click handler.
//   - `createMediaElementSource(a)` captures `a` — the original element no
//     longer plays through default destination. Output happens only via the
//     loopback. This is the desired behaviour.
//   - Each Audio source must be created here (not by the consumer) so we can
//     attach the MediaElementSource → MediaStreamDestination wiring. We
//     return the Audio so consumers can play/pause/listen for `ended` etc.

const _LOOPBACK_DEBUG = false;

async function createAecLoopback() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx || typeof RTCPeerConnection === 'undefined') {
    throw new Error('AEC loopback unsupported (missing AudioContext or RTCPeerConnection)');
  }
  const audioCtx = new Ctx();
  if (audioCtx.state === 'suspended') {
    try { await audioCtx.resume(); } catch (e) { /* fall through; resume on first play */ }
  }

  const dest = audioCtx.createMediaStreamDestination();
  const sender = new RTCPeerConnection();
  const receiver = new RTCPeerConnection();

  // Bridge ICE both ways so candidate gathering completes locally.
  sender.onicecandidate = (e) => {
    if (e.candidate) { try { receiver.addIceCandidate(e.candidate); } catch (err) { /* ignore */ } }
  };
  receiver.onicecandidate = (e) => {
    if (e.candidate) { try { sender.addIceCandidate(e.candidate); } catch (err) { /* ignore */ } }
  };

  for (const track of dest.stream.getAudioTracks()) {
    sender.addTrack(track, dest.stream);
  }

  // Wait for the receiver to expose its remote stream before continuing.
  const remoteStreamPromise = new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('loopback ontrack timeout')), 4000);
    receiver.ontrack = (e) => {
      clearTimeout(t);
      const stream = (e.streams && e.streams[0]) || new MediaStream([e.track]);
      resolve(stream);
    };
  });

  const offer = await sender.createOffer();
  await sender.setLocalDescription(offer);
  await receiver.setRemoteDescription(offer);
  const answer = await receiver.createAnswer();
  await receiver.setLocalDescription(answer);
  await sender.setRemoteDescription(answer);

  const remoteStream = await remoteStreamPromise;

  const audioElement = new Audio();
  audioElement.srcObject = remoteStream;
  audioElement.autoplay = true;
  // Inline play kicks the element into "started" state so subsequent flows
  // through it don't get blocked by autoplay heuristics.
  try {
    const p = audioElement.play();
    if (p && typeof p.catch === 'function') p.catch(() => { /* user gesture missing — ok */ });
  } catch (e) { /* ignore */ }

  if (_LOOPBACK_DEBUG) console.log('[aec-loopback] ready', { ctxState: audioCtx.state });

  // makeAudio: build an Audio whose output flows through the loopback.
  // The returned element's `.play()` triggers playback through the WebRTC
  // path (visible to AEC). `.pause()` + `.removeAttribute('src')` for cancel.
  // Consumers should attach `.onended` / `.onerror` exactly as with a native
  // Audio.
  const _activeSources = new Set();
  function makeAudio(url) {
    const a = new Audio(url);
    a.preload = 'auto';
    a.crossOrigin = 'anonymous';
    let src = null;
    const wireOnce = () => {
      if (src) return;
      try {
        src = audioCtx.createMediaElementSource(a);
        src.connect(dest);
        _activeSources.add(src);
      } catch (e) {
        // createMediaElementSource throws if `a` was already captured. That
        // shouldn't happen since we own the Audio, but guard anyway.
        if (_LOOPBACK_DEBUG) console.warn('[aec-loopback] wire failed', e);
      }
    };
    // Wire as soon as the audio has metadata — `play()` works regardless
    // but wiring up-front gives the browser time to negotiate sample rate.
    a.addEventListener('loadedmetadata', wireOnce, { once: true });
    a.addEventListener('play', wireOnce, { once: true });
    const detach = () => {
      if (!src) return;
      try { src.disconnect(); } catch (e) { /* ignore */ }
      _activeSources.delete(src);
      src = null;
    };
    a.addEventListener('ended', detach);
    a.addEventListener('error', detach);
    return a;
  }

  function close() {
    for (const s of _activeSources) {
      try { s.disconnect(); } catch (e) { /* ignore */ }
    }
    _activeSources.clear();
    try { audioElement.pause(); } catch (e) { /* ignore */ }
    try { audioElement.srcObject = null; } catch (e) { /* ignore */ }
    try { sender.close(); } catch (e) { /* ignore */ }
    try { receiver.close(); } catch (e) { /* ignore */ }
    try { audioCtx.close(); } catch (e) { /* ignore */ }
    if (_LOOPBACK_DEBUG) console.log('[aec-loopback] closed');
  }

  return { audioElement, audioCtx, dest, makeAudio, close };
}

Object.assign(window, { createAecLoopback });
