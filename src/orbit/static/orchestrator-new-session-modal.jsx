// New-session confirmation modal (window.NewSessionModal).
//
// Extracted from orchestrator.jsx when the "run this session in a git
// worktree" option landed: the body grew from a one-line yes/no confirm to a
// checkbox + validated name field + a preflight fetch, and orchestrator.jsx is
// already 3200 lines. Living in its own module is also what lets the library
// Agent panel reuse it instead of copy-pasting the fetch.
//
// Contract: `onConfirm({ worktree, worktreeName })`. The worktree keys are
// only ever populated when the box is ticked, so the default path posts a
// payload byte-identical to the pre-feature one.
//
// The checkbox NEVER blocks the primary action. Preflight still loading, the
// agent isn't a git repo, the request failed — the row greys out with a reason
// and "Nowa sesja" keeps working, creating a plain session. Silent degradation
// is the right failure mode for an opt-in enhancement, and matters here
// because 4 of 5 Areas are not git repos: the disabled state is the COMMON
// one, which is also why it's a quiet grey line and not a coloured banner.
(function () {
  const { useState, useEffect, useRef, useCallback } = React;

  // Preflight answers change only when a worktree is created/removed, so a
  // short TTL is plenty and keeps repeated "+" taps from re-forking git.
  // Module-level so it survives the modal unmounting between opens.
  const _PREFLIGHT_TTL_MS = 30_000;
  const _preflightCache = new Map();

  function _cacheKey(cwd) { return cwd || '__global__'; }

  // Exposed so the creator can drop a stale entry after actually creating a
  // worktree — otherwise `existing[]` would keep saying the name is free.
  function invalidatePreflight(cwd) { _preflightCache.delete(_cacheKey(cwd)); }

  function usePreflight(open, cwd, enabled) {
    const [state, setState] = useState({ status: 'idle', data: null });
    // Monotonic request id: ⌘←/→ can switch agents while the modal is open,
    // so a slow response for the previous agent must not overwrite the new one.
    const reqIdRef = useRef(0);

    const run = useCallback((force) => {
      if (!open || !enabled) { setState({ status: 'idle', data: null }); return undefined; }
      const key = _cacheKey(cwd);
      if (force) _preflightCache.delete(key);
      const cached = _preflightCache.get(key);
      if (cached && (Date.now() - cached.at) < _PREFLIGHT_TTL_MS) {
        setState({ status: 'ready', data: cached.data });
        return undefined;
      }
      const myId = ++reqIdRef.current;
      setState({ status: 'loading', data: null });
      const ctrl = new AbortController();
      // Manual timeout rather than AbortSignal.timeout(): this ships to an
      // iOS PWA and the manual form has no baseline question.
      const timer = setTimeout(() => ctrl.abort(), 4000);
      const url = (window.HUB_BASE_PATH || '')
        + '/api/orchestrator/worktrees/preflight?cwd=' + encodeURIComponent(cwd || '');
      fetch(url, { signal: ctrl.signal })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
        .then((data) => {
          if (myId !== reqIdRef.current) return;  // superseded
          _preflightCache.set(key, { at: Date.now(), data });
          setState({ status: 'ready', data });
        })
        .catch(() => {
          if (myId !== reqIdRef.current) return;
          setState({ status: 'error', data: null });
        })
        .finally(() => clearTimeout(timer));
      return () => { clearTimeout(timer); ctrl.abort(); };
    }, [open, cwd, enabled]);

    useEffect(() => run(false), [run]);
    return [state, () => run(true)];
  }

  function NewSessionModal({
    open, onClose, onConfirm, agentName, agentCwd, worktreeEnabled = false, busy = false,
  }) {
    const ConfirmModal = window.ConfirmModal;
    const Field = window.Field;
    const Input = window.Input;
    const names = window.HubWorktreeName;

    const [useWorktree, setUseWorktree] = useState(false);
    const [name, setName] = useState('');
    const [touched, setTouched] = useState(false);
    const bodyRef = useRef(null);

    const [preflight, retryPreflight] = usePreflight(open, agentCwd, worktreeEnabled);

    // Reset per open so a previous agent's name never leaks into a new session.
    useEffect(() => {
      if (!open) return;
      setUseWorktree(false); setName(''); setTouched(false);
    }, [open]);

    if (!ConfirmModal) return null;

    const pf = preflight.data || {};
    const canWorktree = preflight.status === 'ready' && pf.ok !== false && pf.is_repo === true;
    const validation = names ? names.validate(name, { existing: pf.existing || [] })
      : { ok: true, code: 'ok', message: '' };
    const showError = touched && !validation.ok;
    const effective = names ? names.effectiveName(name, pf.suggested_name) : name;

    let hint = 'Osobna gałąź i katalog roboczy w .claude/worktrees/';
    if (preflight.status === 'loading') hint = 'Sprawdzanie repozytorium…';
    else if (preflight.status === 'error') hint = 'Nie udało się sprawdzić repozytorium.';
    else if (preflight.status === 'ready' && !canWorktree) {
      hint = agentCwd
        ? `${agentName || 'Ten agent'} nie jest repozytorium git — worktree niedostępne.`
        : 'Agent Global nie ma repozytorium git — worktree niedostępne.';
    }

    const submit = () => {
      if (useWorktree && !validation.ok) { setTouched(true); return; }
      onConfirm && onConfirm({
        worktree: !!(useWorktree && canWorktree),
        worktreeName: (useWorktree && name.trim()) ? name.trim() : null,
      });
    };

    // The name field only mounts once the box is ticked, so `autoFocus` on it
    // gives the one-tap-then-type flow with no ref/rAF dance.
    const toggleWorktree = (next) => setUseWorktree(next);

    return (
      <ConfirmModal
        open={open}
        onClose={onClose}
        title="Nowa sesja"
        message={
          <>Rozpocząć nową sesję{agentName && agentName !== 'Global'
            ? <> w agencie <strong style={{ color: 'var(--fg)' }}>{agentName}</strong></> : null}?</>
        }
        confirmLabel="Nowa sesja"
        cancelLabel="Anuluj"
        onConfirm={submit}
        busy={busy}
        confirmDisabled={useWorktree && !validation.ok}
      >
        {worktreeEnabled && (
          <div ref={bodyRef} data-new-session-body="1"
               style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <label style={{
              display: 'flex', alignItems: 'flex-start', gap: 10, cursor: canWorktree ? 'pointer' : 'default',
              opacity: canWorktree ? 1 : 0.55,
            }}>
              <input
                type="checkbox"
                checked={useWorktree}
                disabled={!canWorktree}
                onChange={(e) => toggleWorktree(e.target.checked)}
                style={{ width: 18, height: 18, marginTop: 1, flexShrink: 0, accentColor: 'var(--accent)' }}
              />
              <span style={{ minWidth: 0 }}>
                <span style={{ fontSize: 'var(--t-md)', color: 'var(--fg)' }}>Uruchom w worktree</span>
                <span className="mono" style={{
                  display: 'block', fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
                  marginTop: 2, lineHeight: 1.45,
                }}>
                  {hint}
                  {preflight.status === 'error' && (
                    <a role="button" tabIndex={0} onClick={retryPreflight}
                       onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') retryPreflight(); }}
                       style={{ marginLeft: 6, color: 'var(--accent)', cursor: 'pointer' }}>
                      Spróbuj ponownie
                    </a>
                  )}
                </span>
              </span>
            </label>

            {useWorktree && canWorktree && Field && Input && (
              <Field
                label="Nazwa worktree"
                // Field renders error OR hint in one slot — never pass both.
                hint={showError ? undefined : (effective ? `Gałąź: ${names.branchFor(effective)}` : 'Puste = losowa nazwa')}
                error={showError ? validation.message : undefined}
              >
                <div onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); submit(); }
                }}>
                  <Input
                    value={name}
                    onChange={setName}
                    mono
                    autoFocus
                    maxLength={names ? names.MAX_LEN : 64}
                    placeholder={pf.suggested_name || 'losowa'}
                    error={showError}
                    onBlur={() => setTouched(true)}
                    // tokens.css only bumps controls to 16px inside
                    // .settings-root, so without this iOS Safari zooms on focus.
                    inputStyle={{ fontSize: 16 }}
                  />
                </div>
              </Field>
            )}
          </div>
        )}
      </ConfirmModal>
    );
  }

  window.NewSessionModal = NewSessionModal;
  window.NewSessionModal.invalidatePreflight = invalidatePreflight;
})();
