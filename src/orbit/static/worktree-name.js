'use strict';
// Pure validation for git-worktree NAMES typed in the new-session modal.
//
// JSX-free + dependency-free so Node's built-in `node --test` can require() it
// directly (this no-build CDN-React project has no jsdom/bundler). The modal
// (orchestrator-new-session-modal.jsx) is thin glue on top of these functions.
//
// `window` is referenced ONLY inside the publish guard at the very bottom, never
// at module load, so `require()` under Node never touches a missing global.
//
// The rules mirror claude-cli's own `--worktree <name>` validation (≤64 chars,
// [A-Za-z0-9._-] only, no `.`/`..`/`.git`) plus two of our own:
//   * no `/` — claude accepts slash-separated segments but mangles them to `+`
//     on disk (`feat/x` → .claude/worktrees/feat+x). Forbidding `/` keeps the
//     path we predict server-side byte-identical to the one claude creates.
//   * no leading `-` — a name that looks like a flag has no business anywhere
//     near an argv boundary, even though we pass `--worktree=<name>` as one token.
//
// The server re-validates with the same regex; the Python literal is diffed
// against NAME_RE by tests/test_orchestrator_worktree.py so they can't drift.
(function () {
  const MAX_LEN = 64;
  // Anchored, and the {0,63} tail makes the length cap part of the pattern —
  // but we still range-check first so an over-long name reports `too-long`
  // instead of the generic charset error.
  const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  // Names git itself refuses (or that would resolve to a path segment with a
  // special meaning). Compared case-insensitively — `.GIT` is reserved too.
  const RESERVED = ['.', '..', '.git'];

  const MESSAGES = {
    'bad-start': 'Musi zaczynać się od litery lub cyfry.',
    slash: 'Ukośnik "/" jest niedozwolony.',
    'bad-chars': 'Dozwolone tylko litery A-Z, cyfry, kropka, myślnik i podkreślnik.',
    'too-long': 'Maksymalnie ' + MAX_LEN + ' znaki.',
    reserved: 'Ta nazwa jest zarezerwowana przez git.',
    duplicate: 'Worktree o tej nazwie już istnieje.',
  };

  function _trim(raw) {
    return typeof raw === 'string' ? raw.trim() : '';
  }

  // Returns { ok, code, message }. `ok:true` with code 'empty' means "blank →
  // let the server generate a random name", which is a valid submission.
  function validate(raw, opts) {
    const name = _trim(raw);
    if (!name) return { ok: true, code: 'empty', message: '' };

    if (name.length > MAX_LEN) return { ok: false, code: 'too-long', message: MESSAGES['too-long'] };
    if (RESERVED.indexOf(name.toLowerCase()) !== -1) {
      // `.`/`..`/`.git` also fail bad-start, but naming the real reason is
      // kinder than "must start with a letter" for a deliberate `.git`.
      return { ok: false, code: 'reserved', message: MESSAGES.reserved };
    }
    if (!/^[A-Za-z0-9]/.test(name)) {
      return { ok: false, code: 'bad-start', message: MESSAGES['bad-start'] };
    }
    // Checked before the generic charset rule so the most common mistake
    // (`feat/x`, muscle memory from branch names) gets a specific message.
    if (name.indexOf('/') !== -1) return { ok: false, code: 'slash', message: MESSAGES.slash };
    if (!NAME_RE.test(name)) {
      return { ok: false, code: 'bad-chars', message: MESSAGES['bad-chars'] };
    }

    const existing = (opts && opts.existing) || [];
    for (let i = 0; i < existing.length; i += 1) {
      if (String(existing[i]) === name) {
        return { ok: false, code: 'duplicate', message: MESSAGES.duplicate };
      }
    }
    return { ok: true, code: 'ok', message: '' };
  }

  // The branch claude-cli will create for this worktree. Shown live in the
  // modal hint so the user sees exactly what will exist in git.
  function branchFor(raw) {
    const name = _trim(raw);
    return name ? 'worktree-' + name : '';
  }

  // What the worktree will actually be called: what was typed, else the
  // server's suggestion, else "" (server generates one at create time).
  function effectiveName(raw, suggested) {
    return _trim(raw) || _trim(suggested) || '';
  }

  const api = { MAX_LEN, NAME_RE, RESERVED, MESSAGES, validate, branchFor, effectiveName };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.HubWorktreeName = api;
})();
