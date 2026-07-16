'use strict';
// Cross-subsystem audio coordinator ("bus") for the dashboard.
//
// Three independent audio subsystems each queue INTERNALLY but none coordinate
// with each other: TTS read-aloud (orchestrator-tts.jsx), the singleton media
// player (media-player-engine.jsx), and conversation voice
// (orchestrator-conversation.jsx). Without a global mutex, TTS reading a reply
// + tapping Play on an audio artifact = overlapping audio (issue #89).
//
// This is an EXCLUSIVITY bus (handoff, not a merged queue): whoever starts
// pauses everyone else. Each subsystem keeps its own internal queue; the bus
// only arbitrates BETWEEN them so a single source is audible at a time.
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). `window` is
// referenced ONLY inside the publish guard at the very bottom, never at module
// load, so require() under Node never touches a missing global.
//
// Immutability: the registry is a plain Map of `{pause, kind}` entries; the bus
// never mutates caller-supplied objects.
(function () {
  function createAudioBus() {
    const sources = new Map(); // id -> { pause, kind }
    let active = null;

    // Register a source. `entry.pause` is invoked when another source takes
    // over. Returns an unregister() fn; also available as bus.unregister(id).
    function register(id, entry) {
      const pause = entry && typeof entry.pause === 'function' ? entry.pause : function () {};
      const kind = entry && entry.kind ? entry.kind : id;
      sources.set(id, { pause: pause, kind: kind });
      return function unregisterFn() {
        unregister(id);
      };
    }

    function unregister(id) {
      sources.delete(id);
      if (active === id) active = null;
    }

    // Claim the audio channel for `id`: pause every OTHER registered source.
    // Idempotent — re-requesting the current active id pauses nobody.
    function requestActive(id) {
      if (active === id) return;
      active = id;
      sources.forEach(function (entry, sid) {
        if (sid === id) return; // never pause the requester
        try {
          entry.pause();
        } catch (e) {
          /* a source's pause throwing must not break the handoff */
        }
      });
      // A paused source's callback may re-enter the bus (e.g. its cancel()
      // calls release()), nulling `active` mid-handoff and leaving
      // getActive()===null while `id` is audibly the owner. Re-affirm the
      // claim after the pause sweep so the bus stays consistent.
      active = id;
    }

    // Release the channel if `id` currently holds it. No auto-resume —
    // sequential handoff, not ducking. Releasing a non-active id is a no-op.
    function release(id) {
      if (active === id) active = null;
    }

    function getActive() {
      return active;
    }

    return {
      register: register,
      unregister: unregister,
      requestActive: requestActive,
      release: release,
      getActive: getActive,
    };
  }

  const api = createAudioBus();
  api.createAudioBus = createAudioBus; // exposed for isolated tests

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubAudioBus = window.HubAudioBus || api;
})();
