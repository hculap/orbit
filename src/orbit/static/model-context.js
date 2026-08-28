'use strict';
// Context-window sizing for the "kontekst NN%" badge (session list + composer).
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler); the badge
// itself lives in orchestrator-attachments.jsx and is thin glue on top of
// `contextUsage()`.
//
// `window` is referenced ONLY inside the publish guard at the very bottom,
// never at module load, so `require()` under Node never touches a missing
// global.
//
// **1M is the DEFAULT**; 200k is the exception list. Hardcoding one version as
// "the 1M one" is how this broke twice: first a 597k-token Opus 4.8 session
// read as 100% (red) instead of ~60% because only `opus-4-7` was special-cased,
// then the `opus-4`-prefix fix that followed regressed the day `claude-opus-5` /
// `claude-fable-5` shipped. Anthropic now ships 1M everywhere except Haiku and
// the pre-4.6 generations, so an unrecognised id is assumed 1M and a future
// model bump inherits the right window with no edit here. The failure mode is
// deliberately the mild one — a badge that reads low, never a false 100% red.
(function () {
  const WIN_1M = 1000000;
  const WIN_200K = 200000;

  // Family + major(-minor) out of ids like `claude-opus-4-8`, `claude-sonnet-5`,
  // `claude-haiku-4-5-20251001`.
  const FAMILY_RE = /(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?/;

  function contextWindow(modelId) {
    if (typeof modelId !== 'string' || !modelId) return WIN_1M;
    const id = modelId.toLowerCase();
    // Claude Code appends `[1m]` to the id when the 1M-context variant is
    // selected — an explicit signal, trust it over any inference below.
    if (id.indexOf('[1m]') !== -1) return WIN_1M;
    // Legacy ids (`claude-3-5-sonnet-20241022`) put the version BEFORE the
    // family name; none of them are 1M, and letting one reach FAMILY_RE would
    // read its date suffix as a version number.
    if (/^claude-\d/.test(id)) return WIN_200K;
    const m = id.match(FAMILY_RE);
    if (!m) return WIN_1M;
    const family = m[1];
    const major = Number(m[2]);
    const minor = m[3] === undefined ? 0 : Number(m[3]);
    // Haiku has stayed 200k across every version so far — but only pin the
    // versions that actually shipped that way, so a future 1M Haiku isn't
    // capped by a rule nobody remembers to revisit.
    if (family === 'haiku') return major <= 4 ? WIN_200K : WIN_1M;
    // opus / sonnet: 200k up to 4.5, 1M from 4.6 upwards.
    if (family === 'opus' || family === 'sonnet') {
      if (major > 4) return WIN_1M;
      return (major === 4 && minor >= 6) ? WIN_1M : WIN_200K;
    }
    // fable / mythos: 1M since they were introduced (at 5).
    return WIN_1M;
  }

  function formatTokens(n) {
    if (!Number.isFinite(n) || n <= 0) return '0';
    if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(0) + 'k';
    return String(n);
  }

  // Shared usage descriptor for the badge. Returns null when there's nothing
  // to render (no assistant turns yet) so callers can early-exit.
  function contextUsage(tokens, model) {
    const t = Number(tokens) || 0;
    if (t <= 0) return null;
    const win = contextWindow(model);
    const pct = Math.min(100, Math.round((t / win) * 100));
    // Tier colours: muted < 60, accent 60-79, error 80+. Aligned with the user
    // mental model where ~80% is "compact soon".
    const color = pct >= 80 ? 'var(--err)'
                : pct >= 60 ? 'var(--accent)'
                :             'var(--fg-3)';
    const title = formatTokens(t) + ' / ' + formatTokens(win) + ' tokens'
                + (model ? ' · ' + model : '');
    return { pct, color, title, tokens: t, window: win };
  }

  const api = { WIN_1M, WIN_200K, contextWindow, formatTokens, contextUsage };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubModelContext = api;
})();
