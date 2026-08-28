'use strict';
// The two prompts the Issues panel hands to the live agent session: "Rozwiąż"
// (implement the issue) and "Dopracuj" (improve the ISSUE ITSELF, never the
// code). Pure string building, JSX-free + dependency-free so Node's built-in
// runner can require() it (`node --test tests/issue-agent-prompt.test.js`).
//
// These are system prompts in everything but name — a vague one makes the
// whole feature useless — so three constraints are baked in and asserted by
// the tests:
//
//  1. EXACTLY ONE LINE. The payload is pasted into the live tmux pane via
//     POST /term/paste (tmux load-buffer + paste-buffer -p -d, no Enter).
//     Measured against claude-cli 2.1.237: a bracketed paste never submits,
//     but at 4+ LINES claude's REPL collapses it into an opaque
//     "[Pasted text #N +M lines]" chip the user can no longer read or edit.
//     One long line stays visible and editable.
//  2. Self-contained enough to act on without a round trip: issue NUMBER,
//     TITLE and (when known) the canonical URL, plus the command that expands
//     it. We POINT at the issue instead of inlining its body — the body can be
//     65 kB and /term/paste hard-caps text at 8192 chars (over-cap surfaces as
//     an untranslated "→ 400" toast).
//  3. `gh issue view/edit` rather than the bundled gh-issues skill CLI: the
//     agent's pane runs inside the repo (or its worktree — same repo), so gh
//     auto-detects owner/repo, and gh is unconditionally installed, whereas
//     a registry skill is only present once ENABLED for that agent.
//
// `window` is referenced ONLY in the publish guard at the bottom.
(function () {
  // Titles are ≤256 chars on GitHub; clamp anyway so one pathological title
  // can never push the payload anywhere near the 8192-char paste cap.
  const TITLE_MAX = 160;

  function _clean(raw) {
    // Newlines would break constraint #1 — a title can't contain one via the
    // GitHub UI, but a body-derived string could.
    return String(raw == null ? '' : raw).replace(/\s+/g, ' ').trim();
  }

  function clampTitle(raw) {
    const t = _clean(raw);
    return t.length > TITLE_MAX ? t.slice(0, TITLE_MAX - 1) + '…' : t;
  }

  // "#14 „Tytuł” (https://github.com/…/14)" — the URL is what makes the
  // reference unambiguous if the agent's cwd ever isn't the repo we think it
  // is, so include it whenever the payload carries one.
  function issueRef(issue) {
    const it = issue || {};
    const num = String(it.number == null ? '' : it.number);
    const title = clampTitle(it.title);
    const url = _clean(it.url);
    let ref = '#' + num;
    if (title) ref += ' „' + title + '”';
    if (url) ref += ' (' + url + ')';
    return ref;
  }

  function _viewCmd(issue) {
    return '`gh issue view ' + String((issue || {}).number || '') + ' --comments`';
  }

  // IMPLEMENT it. Explicitly asks for the comments (that is where the
  // decisions usually are), forbids an unsupervised commit, and — the part
  // that saves the most time — tells the agent to come back with questions
  // instead of guessing at an underspecified issue.
  function buildSolvePrompt(issue) {
    return 'Rozwiąż issue ' + issueRef(issue) + ' w tym repo. Zacznij od ' + _viewCmd(issue)
      + ' — w komentarzach zwykle są ustalenia, których nie ma w opisie. Potem zaplanuj zmianę,'
      + ' zaimplementuj ją, uruchom testy i pokaż mi diff; nie commituj, nie pushuj i nie zamykaj'
      + ' issue bez mojej zgody. Jeśli opis jest niejednoznaczny albo to w rzeczywistości kilka'
      + ' zadań naraz — dopytaj mnie ZANIM napiszesz pierwszą linijkę kodu.';
  }

  // Improve the ISSUE. The double negation up front is deliberate: the
  // default reflex of a coding agent handed an issue number is to start
  // editing code, and a single "nie implementuj" buried mid-sentence does not
  // beat that reflex. Everything after it is the shape of a good issue, so
  // the agent has a checklist rather than "make it better".
  function buildRefinePrompt(issue) {
    return 'Dopracuj TREŚĆ issue ' + issueRef(issue) + ' — NIE implementuj go i nie zmieniaj kodu.'
      + ' Przeczytaj je (' + _viewCmd(issue) + ') i rozejrzyj się po repo tyle, ile trzeba, żeby opis'
      + ' był konkretny: problem i po co go rozwiązujemy, zakres (co wchodzi, a co NIE), kryteria'
      + ' akceptacji jako lista do odhaczenia, konkretne pliki/miejsca w kodzie, ryzyka i otwarte'
      + ' pytania. Zapisz poprawiony opis na GitHubie (`gh issue edit ' + String((issue || {}).number || '')
      + ' --body-file -`), zachowując ustalenia z komentarzy. Jeśli to naprawdę kilka zadań —'
      + ' zaproponuj mi podział na osobne issues i utwórz je dopiero po mojej zgodzie. Na koniec'
      + ' streść w 1-2 zdaniach, co zmieniłeś.';
  }

  // 'solve' | 'refine' → text. One entry point so the JSX side carries no
  // branch of its own and an unknown mode can never paste the wrong prompt.
  function buildPrompt(mode, issue) {
    if (mode === 'refine') return buildRefinePrompt(issue);
    if (mode === 'solve') return buildSolvePrompt(issue);
    return '';
  }

  const api = { TITLE_MAX, clampTitle, issueRef, buildSolvePrompt, buildRefinePrompt, buildPrompt };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubIssuePrompt = api;
})();
