'use strict';
// Client-side search over the issue list already held by the Issues panel —
// matches on NUMBER, TITLE and BODY/description.
//
// JSX-free + dependency-free so Node's built-in runner can require() it
// directly (`node --test tests/issue-search.test.js`); this no-build CDN-React
// project has no bundler and no jsdom, so any logic worth asserting on has to
// live outside a .jsx file. library-github-panels.jsx is thin glue on top.
//
// WHY client-side: the panel already holds every row it will ever show (one
// `gh issue list --limit 50` per mount, no pagination), and every other search
// box in this app — the artifact gallery, tasks.jsx, skills-directory.jsx —
// filters an in-memory array with no debounce (there is no useDebounce helper
// anywhere in static/). A round trip per keystroke would be both slower and a
// new idiom. The honest limit, worth saying in the UI: this filters the ≤50
// rows the list fetched — it is NOT a GitHub-wide search.
//
// `window` is referenced ONLY inside the publish guard at the very bottom, so
// require()ing this under Node never touches a missing global.
(function () {
  // Case folding only — deliberately NO diacritics stripping. The corpus is
  // Polish and the usual NFD-strip trick is half-broken here: ó/ź/ę decompose
  // into base+mark (ó→o) but ł does NOT — it is its own letter, not a
  // composition — so "lodz" would still miss "Łódź" while "zazolc" found
  // "zażółć". That inconsistency is worse than no folding at all, and
  // toLowerCase() already casefolds Polish correctly.
  function normalize(raw) {
    return typeof raw === 'string' ? raw.trim().toLowerCase() : '';
  }

  // "#14" and "14" are the same ask — users type the hash out of habit.
  // Returns the digit string, or '' when the query is not a bare number
  // reference (so "#" alone can never degenerate into "match everything").
  function numberToken(q) {
    const m = /^#?(\d+)$/.exec(q || '');
    return m ? m[1] : '';
  }

  // Exact issue number a bare-number query asks for, or null. Used for
  // RANKING only — "14" must still surface issues that merely mention 14.
  function exactNumber(q) {
    const tok = numberToken(q);
    return tok ? Number(tok) : null;
  }

  // Normalizes defensively: filterIssues already folded the query once for
  // the whole list, so this is a no-op there, but a caller that hands over a
  // RAW "ZAŻÓŁĆ" must get a hit rather than a silent empty result.
  function matchesIssue(issue, rawQ) {
    const q = normalize(rawQ);
    if (!q) return true;
    if (!issue || typeof issue !== 'object') return false;
    // Number branch: substring, so "14" finds #14 AND #140. Only runs for a
    // bare-number query — for text queries String(number).includes('map') is
    // dead weight and, for the empty string, actively wrong.
    const tok = numberToken(q);
    if (tok && String(issue.number == null ? '' : issue.number).includes(tok)) return true;
    const hay = [issue.title, issue.body]
      .map((v) => (typeof v === 'string' ? v.toLowerCase() : ''))
      .join('\n');
    return hay.indexOf(q) !== -1;
  }

  // Returns a NEW array (never sorts in place — the caller's `items` is React
  // state). A bare-number query floats the exactly-matching issue to the top
  // while keeping the looser hits below it; every other query keeps gh's own
  // ordering, which is already "most recently updated first".
  function filterIssues(items, rawQuery) {
    const list = Array.isArray(items) ? items : [];
    const q = normalize(rawQuery);
    if (!q) return list;
    const hits = list.filter((it) => matchesIssue(it, q));
    const exact = exactNumber(q);
    if (exact === null || hits.length < 2) return hits;
    const first = hits.filter((it) => Number(it && it.number) === exact);
    if (first.length === 0 || first.length === hits.length) return hits;
    return first.concat(hits.filter((it) => Number(it && it.number) !== exact));
  }

  const api = { normalize, numberToken, exactNumber, matchesIssue, filterIssues };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubIssueSearch = api;
})();
