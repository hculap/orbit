'use strict';

// Shared clipboard-copy util with an iOS-Safari / PWA fallback.
//
// On iOS Safari and standalone PWAs `navigator.clipboard.writeText()` is
// unreliable: it REJECTS/throws outside a perfect secure context, without a
// fresh user gesture, or in some WKWebView/PWA states. `copyText()` tries the
// async Clipboard API first and, on rejection or absence, falls through to a
// `<textarea>` + `document.execCommand('copy')` fallback (the only reliable iOS
// path). Always resolves Promise<boolean> — never throws.
//
// Plain `.js` (NOT text/babel) so it can be required under `node --test` and
// loaded before every JSX consumer. `window` is touched only inside the publish
// guard, mirroring session-switcher-order.js.
(function () {
  function _isIOS() {
    if (typeof navigator === 'undefined') return false;
    return /ipad|iphone|ipod/i.test(navigator.userAgent || '')
      || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  function _execCommandFallback(text) {
    try {
      if (typeof document === 'undefined' || !document.body) return false;
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');          // iOS: stops the keyboard popping
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '0';
      ta.style.opacity = '0';
      ta.style.pointerEvents = 'none';
      document.body.appendChild(ta);
      // iOS Safari: .select() is a no-op for textarea — must use a Range
      // + setSelectionRange, else execCommand('copy') copies nothing.
      if (_isIOS()) {
        const range = document.createRange();
        range.selectNodeContents(ta);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        ta.setSelectionRange(0, text.length);
      } else {
        ta.select();
      }
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (_) {
      return false;
    }
  }

  function copyText(text) {
    if (text == null || text === '') return Promise.resolve(false);
    const str = String(text);
    const clip = (typeof navigator !== 'undefined') && navigator.clipboard;
    if (clip && typeof clip.writeText === 'function') {
      // writeText first; on iOS rejection, FALL THROUGH to execCommand.
      return clip.writeText(str).then(
        function () { return true; },
        function () { return _execCommandFallback(str); },
      );
    }
    return Promise.resolve(_execCommandFallback(str));
  }

  // Normalize text lifted off a TERMINAL grid before it reaches the clipboard.
  //
  // A terminal row is always `cols` cells wide — there is no "end of line".
  // Whatever painted the screen (tmux's redraw, claude's Ink TUI) pads the
  // remainder of each row with REAL space characters, and xterm.js's own
  // trimRight() only drops never-written (null) cells, so those spaces travel
  // into the clipboard. Pasting a copied paragraph into LinkedIn/Slack then
  // shows a run of spaces at the end of every line (UAT 2026-07-30).
  //
  // Interior blank lines are real paragraph breaks and stay untouched.
  //
  // The left side needs its own rule: claude's TUI paints a one-space gutter,
  // so EVERY copied line arrives indented by one and the user has to strip it
  // by hand before pasting (UAT 2026-07-30). We remove the gutter as a common
  // prefix — never per-line — so relative indentation survives and a copied
  // code block keeps its shape. See _leftMargin for the two cases.
  function tidyTerminalText(text) {
    if (text == null) return '';
    const lines = String(text)
      .replace(/\r\n?/g, '\n')                      // CRLF/CR grids → LF
      .split('\n')
      .map(function (line) { return line.replace(/[ \t ]+$/, ''); });
    return _dedent(lines)
      .join('\n')
      .replace(/^\n+/, '')              // drop blank rows above the selection
      .replace(/\n+$/, '');             // …and below it
  }

  function _lead(line) { return /^ */.exec(line)[0].length; }

  // Peel claude's left gutter without touching real indentation. Two shapes:
  //
  //  1. EVERY non-blank row is indented ⇒ remove the common minimum. Always
  //     safe: subtracting a constant cannot reorder indentation levels, so a
  //     copied code block (and a diff's +/- markers) keeps its structure.
  //  2. Some rows sit flush left, because the TUI paints its gutter raggedly —
  //     the 2026-07-30 paste had 4 of 6 rows guttered, which no ratio test can
  //     tell apart from content. The reliable tell is WIDTH: exactly one space
  //     is a gutter, never an indentation level (nobody indents code by one).
  //     So peel one space off the exactly-one rows and leave the rest alone —
  //     deeper rows are a real code block and keep every space they have.
  //
  // Case 2 is vetoed for a unified diff, where a single leading space is the
  // marker for "context line" and carries meaning. The veto is scoped to case
  // 2 on purpose: case 1 is structure-preserving, so a diff copied WITH the
  // gutter still dedents correctly.
  //
  // Tabs count as zero leading spaces, so tab-indented text opts itself out of
  // both cases and comes back untouched.
  function _dedent(lines) {
    const widths = [];
    for (let i = 0; i < lines.length; i++) {
      if (lines[i] === '') continue;                // blank rows say nothing
      widths.push(_lead(lines[i]));
    }
    if (!widths.length) return lines;
    const min = Math.min.apply(null, widths);
    if (min > 0) {
      // Slice at most what the line actually has, so nothing can eat a real
      // character if the minimum is ever wider than a given line.
      return lines.map(function (line) { return line.slice(Math.min(_lead(line), min)); });
    }
    if (!widths.some(function (w) { return w === 1; })) return lines;
    if (_looksLikeDiff(lines)) return lines;
    // `^ [^ ]` = exactly one leading space. A trailing-stripped blank row is
    // '' and cannot match, so blank lines stay blank.
    return lines.map(function (line) { return /^ [^ ]/.test(line) ? line.slice(1) : line; });
  }

  // Unified-diff detection: a hunk header, or the ---/+++ file-header pair.
  // Precise on purpose — this vetoes a de-guttering the user DOES want, so it
  // must not fire on prose that merely contains a dash.
  const _DIFF_HUNK = /^@@[ +\-0-9,]*@@/;
  function _looksLikeDiff(lines) {
    let oldFile = false;
    let newFile = false;
    for (let i = 0; i < lines.length; i++) {
      if (_DIFF_HUNK.test(lines[i])) return true;
      if (lines[i].slice(0, 4) === '--- ') oldFile = true;
      else if (lines[i].slice(0, 4) === '+++ ') newFile = true;
    }
    return oldFile && newFile;
  }

  const api = { copyText, tidyTerminalText };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubClipboard = api;
})();
