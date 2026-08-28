'use strict';
// overlay-stack.js — LIFO registry of the overlays currently on screen, so
// Escape closes exactly ONE of them: the top-most.
//
// Why this exists: every Escape handler in this app is bound to `window` (or
// `document`), so DOM nesting buys ZERO precedence — listeners fire in
// REGISTRATION order. An outer overlay registers first (it mounted first), so a
// naive `window.keydown → onClose` on it fires BEFORE the nested overlay the
// user is actually looking at, and one keypress closes both. Concretely: the
// session-overlay modal hosts LibraryIssueList (which opens CreateIssueDialog
// and, on compact, _GhFullscreen) and ArtifactGallery (ArtifactViewerModal) —
// Escape there would nuke the host, taking an unsaved issue draft with it.
// e.stopPropagation() cannot help: the listeners sit on the SAME node.
//
// The stack is the honest fix — each overlay registers an opaque id on mount,
// splices it out on unmount, and only acts on Escape while `isTop(id)`.
// pop() removes by identity ANYWHERE in the stack: React unmount order is not
// guaranteed to be LIFO (a parent can unmount while a child is still
// registered), so "pop the last entry" would corrupt the stack.
//
// JSX-free + dependency-free so `node --test` can require() it (this no-build
// CDN-React project has no bundler/jsdom). `window` is touched ONLY inside the
// publish guard at the very bottom, never at module load.
(function () {
  const stack = [];
  let seq = 0;

  // Opaque, monotonic per page load. Callers never parse it — ids are only
  // ever compared by identity — so the prefix is purely for debuggability.
  function nextId() {
    seq += 1;
    return 'ovl-' + seq;
  }

  function push(id) {
    if (!id) return id;
    stack.push(id);
    return id;
  }

  // True when the id was actually registered. lastIndexOf (not indexOf) so a
  // re-registered id drops its most recent entry first.
  function pop(id) {
    const i = stack.lastIndexOf(id);
    if (i < 0) return false;
    stack.splice(i, 1);
    return true;
  }

  function isTop(id) {
    return stack.length > 0 && stack[stack.length - 1] === id;
  }

  function size() {
    return stack.length;
  }

  // Test-only escape hatch; nothing in the app calls it (an overlay that
  // forgets to pop is a leak we want to SEE, not silently paper over).
  function reset() {
    stack.length = 0;
  }

  const api = { nextId, push, pop, isTop, size, reset };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubOverlayStack = api;
})();
