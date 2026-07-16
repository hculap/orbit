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

  const api = { copyText };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubClipboard = api;
})();
