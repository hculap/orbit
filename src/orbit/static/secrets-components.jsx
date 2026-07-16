// secrets-components.jsx — shared primitives for Env & secrets manager.
//
// Published to window for reuse by:
//   - secrets-view.jsx (top-level /secrets page)
//   - library-secrets-panel.jsx (per-Area/Project tab)
//
// Components:
//   <MaskedValue>          — chip showing dot-prefixed last-4 / fingerprint
//   <EntryRow>             — single-line entry: name + masked + actions
//   <EntryList>            — masked listing for one scope/kind, with CTAs
//   <CaptchaGate>          — server-issued code prompt for destructive ops
//   <RevealModal>          — auto-hide plaintext + countdown + Copy & close
//   <EditModal>            — change a value (captcha required on overwrite)
//   <DeleteModal>          — confirm destructive removal (captcha)
//   <AddEntryModal>        — create a new entry (env / files / authorized_keys)
//   <GenerateSshKeyModal>  — wraps `ssh-keygen` with captcha gate
//
// Conventions:
//   - All API calls use window.apiUrl(path) for BASE_PATH-aware URLs.
//   - Captcha is fetched from POST /api/secrets/captcha/issue and
//     `{token, code}` is included in the body of every destructive/reveal
//     request as `captcha`.
//   - Confirm buttons stay disabled until the user has typed the code back
//     verbatim. Server still does the authoritative single-use check.
//   - Reveal modal auto-hides plaintext after _SEC_REVEAL_TTL_MS (30s) with
//     a visible countdown. Plaintext is wiped from React state on close.

const {
  useState: _secUseState,
  useEffect: _secUseEffect,
  useMemo: _secUseMemo,
  useCallback: _secUseCallback,
  useRef: _secUseRef,
} = React;

const _SEC_REVEAL_TTL_MS = 30_000;

// Feature-prefixed input style. Mirrors window._scmInputStyle
// (scheduler-create-modal.jsx:17-28) but kept private so we don't fight
// over the global if scheduler ever changes it.
function _SECInputStyle(extra) {
  return {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
    ...(extra || {}),
  };
}

const _SECStyles = {
  modalBody: { padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: 12 },
  modalFooter: {
    padding: '12px 18px', borderTop: '1px solid var(--hairline)',
    display: 'flex', gap: 8, justifyContent: 'flex-end',
  },
  label: { fontSize: 'var(--t-cap)', color: 'var(--fg-3)', fontWeight: 500 },
  helpText: { fontSize: 'var(--t-xs)', color: 'var(--fg-3)', lineHeight: 1.4 },
  errorBox: {
    padding: '8px 10px', borderRadius: 'var(--r-sm)',
    background: 'var(--err-bg)',
    border: '1px solid var(--err)',
    color: 'var(--err)', fontSize: 'var(--t-cap)', fontFamily: 'inherit',
  },
};

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────
async function _secFetchJson(path, opts) {
  const r = await fetch(window.apiUrl(path), opts);
  let body = null;
  try { body = await r.json(); } catch (_) { /* ignore */ }
  if (!r.ok) {
    const detail = (body && (body.detail || body.error || body.message)) || `http ${r.status}`;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return body;
}

async function _secIssueCaptcha() {
  return _secFetchJson('/api/secrets/captcha/issue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
}

function _secCopyToClipboard(text) {
  if (!text) return Promise.resolve(false);
  // Delegate to the shared util (iOS-Safari/PWA execCommand fallback).
  return window.HubClipboard.copyText(text);
}

function _secScopeUrlSegment(scope) {
  if (!scope) return 'global';
  // Pre-built URL segment from library-secrets-panel (e.g. "projects/<id>",
  // "areas/<id>") or the literal "global". Pass through verbatim so the
  // request reaches the right scope; previously this fell through to the
  // object-shape branch and silently routed every per-library file op to
  // the global scope.
  if (typeof scope === 'string') {
    if (scope === 'global') return 'global';
    if (scope.startsWith('areas/') || scope.startsWith('projects/')) return scope;
    return 'global';
  }
  if (scope.kind === 'global') return 'global';
  if (scope.kind === 'area' || scope.kind === 'areas') return `areas/${encodeURIComponent(scope.lib_id || '')}`;
  if (scope.kind === 'project' || scope.kind === 'projects') return `projects/${encodeURIComponent(scope.lib_id || '')}`;
  return 'global';
}

// ─────────────────────────────────────────────────────────────
// MaskedValue — chip with copy-once button.
// `masked` is the server-rendered preview (e.g. `••••cv2D`). The component
// never receives plaintext; the copy button is intentionally limited to the
// preview so users get a one-tap "what's the suffix again" answer without
// re-issuing a captcha.
// ─────────────────────────────────────────────────────────────
function MaskedValue({ value, masked, mono = true, copyable = true }) {
  const text = String(masked || value || '');
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const onCopy = _secUseCallback((e) => {
    e.stopPropagation();
    _secCopyToClipboard(text).then((ok) => {
      if (toast) toast(ok ? 'Copied preview' : 'Copy failed', ok ? 'ok' : 'err');
    });
  }, [text, toast]);
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      padding: '4px 8px', borderRadius: 'var(--r-sm)',
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      color: 'var(--fg-2)',
      fontSize: 'var(--t-cap)',
    }} className={mono ? 'mono' : ''}>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 220 }}>
        {text || '—'}
      </span>
      {copyable && text && (
        <button
          type="button"
          onClick={onCopy}
          aria-label="Copy preview"
          title="Copy preview"
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 20, height: 20, padding: 0,
            background: 'transparent', border: 'none', cursor: 'pointer',
            color: 'var(--fg-3)',
          }}>
          <window.Icon name="copy" size={12} />
        </button>
      )}
    </span>
  );
}

// ─────────────────────────────────────────────────────────────
// CaptchaGate — server-issued code displayed prominently; user must type
// it back before the parent can enable Confirm.
// ─────────────────────────────────────────────────────────────
function CaptchaGate({ token, code, value, setValue, error, loading }) {
  if (loading) {
    return (
      <div className="mono" style={{ ..._SECStyles.helpText, color: 'var(--fg-3)' }}>
        Issuing captcha…
      </div>
    );
  }
  if (error) {
    return (
      <StatusBanner variant="err" label={`captcha error: ${String(error)}`} inline />
    );
  }
  if (!token || !code) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={_SECStyles.label}>Type this code to confirm</div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <span className="mono" style={{
          fontSize: 22, letterSpacing: '0.18em', fontWeight: 600,
          padding: '8px 14px', borderRadius: 'var(--r-control)',
          background: 'var(--accent-soft)', border: '1px solid var(--accent-line)',
          color: 'var(--accent)',
          userSelect: 'all',
        }}>{code}</span>
        <Input
          type="text"
          autoComplete="off"
          inputMode="text"
          ariaLabel="Captcha code"
          value={value}
          onChange={setValue}
          placeholder="Type code"
          mono
          style={{ flex: 1 }}
          inputStyle={{ fontSize: 'var(--t-h3)', letterSpacing: '0.18em', textTransform: 'uppercase' }}
        />
      </div>
      <div style={_SECStyles.helpText}>
        Single-use; expires in 60 seconds. Defends against fat-finger destroys, not network adversaries.
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// useCaptcha — fetches a captcha when `enabled` flips true; returns
// {token, code, value, setValue, ready, error, reload}. `ready` is true
// when the user has typed the code back verbatim.
// ─────────────────────────────────────────────────────────────
function _secUseCaptcha(enabled) {
  const [state, setState] = _secUseState({ token: null, code: null, error: null, loading: false });
  const [value, setValue] = _secUseState('');

  const reload = _secUseCallback(() => {
    if (!enabled) return;
    setState({ token: null, code: null, error: null, loading: true });
    setValue('');
    _secIssueCaptcha().then((data) => {
      setState({
        token: data && data.token,
        code: data && data.code,
        error: null,
        loading: false,
      });
    }).catch((e) => {
      setState({ token: null, code: null, error: e.message || 'failed', loading: false });
    });
  }, [enabled]);

  _secUseEffect(() => {
    if (enabled) reload();
    else {
      setState({ token: null, code: null, error: null, loading: false });
      setValue('');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  const ready = !!(state.token && state.code && value.trim().toUpperCase() === String(state.code).toUpperCase());
  return { ...state, value, setValue, ready, reload };
}

// ─────────────────────────────────────────────────────────────
// EntryRow — single-line entry display.
// `kind` controls iconography and which actions are shown.
// `item` shape varies; we read item.name / item.key / item.idx defensively.
// ─────────────────────────────────────────────────────────────
function EntryRow({ item, kind, onReveal, onEdit, onDelete, onClick }) {
  if (!item) return null;
  const displayName = item.key || item.name || item.host || (item.idx != null ? `#${item.idx}` : '?');
  const masked = item.masked || item.fingerprint || item.value || '';
  const subBits = [];
  if (item.type) subBits.push(item.type);
  if (item.comment) subBits.push(item.comment);
  if (item.size != null && kind === 'files') subBits.push(`${item.size} bytes`);
  const sub = subBits.join(' · ');
  const clickable = typeof onClick === 'function';
  return (
    <div
      onClick={clickable ? onClick : undefined}
      style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '10px 12px',
        borderBottom: '1px solid var(--hairline)',
        cursor: clickable ? 'pointer' : 'default',
      }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="mono" style={{
          fontSize: 'var(--t-sm)', color: 'var(--fg)', fontWeight: 500,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{displayName}</div>
        {sub && (
          <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{sub}</div>
        )}
      </div>
      <MaskedValue masked={masked} />
      <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
        {typeof onReveal === 'function' && (
          <window.IconButton icon="eye" label="Reveal" size={30} onClick={(e) => { if (e && e.stopPropagation) e.stopPropagation(); onReveal(item); }} />
        )}
        {typeof onEdit === 'function' && (
          <window.IconButton icon="pencil" label="Edit" size={30} onClick={(e) => { if (e && e.stopPropagation) e.stopPropagation(); onEdit(item); }} />
        )}
        {typeof onDelete === 'function' && (
          <window.IconButton icon="trash" label="Delete" size={30} onClick={(e) => { if (e && e.stopPropagation) e.stopPropagation(); onDelete(item); }} />
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Env session-unlock — once-per-session captcha for inline env operations.
//
// .env entries are usually configs the user rotates frequently. Per-op
// captcha modals would be too much friction. Instead: type one captcha
// to unlock, then reveal/edit/delete behave inline with silent captcha
// auto-issuance behind the scenes. .secrets/ files (private keys, JSON
// service-account creds) keep the stricter per-op modal flow.
//
// Unlock state lives in sessionStorage so it persists across tab switches
// within the same browser session and clears when the browser closes.
// ─────────────────────────────────────────────────────────────

const _SEC_ENV_UNLOCK_KEY = 'secrets:env-unlocked:v1';

function _secIsEnvUnlocked() {
  try { return sessionStorage.getItem(_SEC_ENV_UNLOCK_KEY) === '1'; }
  catch (_) { return false; }
}
function _secSetEnvUnlocked(yes) {
  try {
    if (yes) sessionStorage.setItem(_SEC_ENV_UNLOCK_KEY, '1');
    else sessionStorage.removeItem(_SEC_ENV_UNLOCK_KEY);
  } catch (_) { /* ignore — sessionStorage may be disabled */ }
}

// EnvUnlockModal — fetches one captcha and asks the user to type it back.
// On match, flips the session flag. The captcha itself is not consumed
// against the backend; subsequent env ops issue their own.
function EnvUnlockModal({ open, onClose, onUnlocked }) {
  const cap = _secUseCaptcha(open);
  const submit = _secUseCallback(() => {
    if (!cap.ready) return;
    _secSetEnvUnlocked(true);
    if (typeof onUnlocked === 'function') onUnlocked();
    onClose();
  }, [cap.ready, onUnlocked, onClose]);
  if (!open || !window.Modal) return null;
  return (
    <window.Modal open={open} onClose={onClose} title="Unlock .env editing" width={460}>
      <div style={_SECStyles.modalBody}>
        <div style={_SECStyles.helpText}>
          Type the code below once to unlock inline reveal / edit / delete on
          this scope's <code className="mono">.env</code> entries. Stays unlocked
          for the rest of this browser session.
        </div>
        <CaptchaGate token={cap.token} code={cap.code}
          value={cap.value} setValue={cap.setValue}
          error={cap.error} loading={cap.loading} />
      </div>
      <div style={_SECStyles.modalFooter}>
        <window.Button variant="ghost" onClick={onClose}>Cancel</window.Button>
        <window.Button variant="primary" onClick={submit} disabled={!cap.ready}>
          Unlock
        </window.Button>
      </div>
    </window.Modal>
  );
}

// EnvRow — env-specific row with inline reveal / edit / delete.
// Replaces EntryRow when EntryList is rendering kind === 'env' with a scope.
function EnvRow({ scope, item, onAfterChange, toast }) {
  const [mode, setMode] = _secUseState('view');
  const [plaintext, setPlaintext] = _secUseState(null);
  const [draftValue, setDraftValue] = _secUseState('');
  const [busy, setBusy] = _secUseState(false);
  const [unlockOpen, setUnlockOpen] = _secUseState(false);
  const [pendingAction, setPendingAction] = _secUseState(null);
  const [revealLeft, setRevealLeft] = _secUseState(0);

  // Auto-hide reveal after 30 s with a visible countdown.
  _secUseEffect(() => {
    if (mode !== 'reveal') { setRevealLeft(0); return; }
    setRevealLeft(Math.ceil(_SEC_REVEAL_TTL_MS / 1000));
    const tick = setInterval(() => {
      setRevealLeft((n) => (n > 1 ? n - 1 : 0));
    }, 1000);
    const expire = setTimeout(() => {
      setMode('view');
      setPlaintext(null);
    }, _SEC_REVEAL_TTL_MS);
    return () => { clearInterval(tick); clearTimeout(expire); };
  }, [mode]);

  const reqUrl = `/api/secrets/${scope}/env/${encodeURIComponent(item.key)}`;

  const fetchPlaintext = _secUseCallback(async () => {
    const c = await _secIssueCaptcha();
    return _secFetchJson(`${reqUrl}/reveal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ captcha: { token: c.token, code: c.code } }),
    });
  }, [reqUrl]);

  const doReveal = _secUseCallback(async () => {
    setBusy(true);
    try {
      const r = await fetchPlaintext();
      setPlaintext(r.value || '');
      setMode('reveal');
    } catch (e) {
      toast && toast(`Reveal failed: ${e.message || 'error'}`, 'err');
    } finally { setBusy(false); }
  }, [fetchPlaintext, toast]);

  const doStartEdit = _secUseCallback(async () => {
    setBusy(true);
    try {
      const r = await fetchPlaintext();
      setDraftValue(r.value || '');
      setMode('edit');
    } catch (e) {
      toast && toast(`Edit failed: ${e.message || 'error'}`, 'err');
    } finally { setBusy(false); }
  }, [fetchPlaintext, toast]);

  const doSaveEdit = _secUseCallback(async () => {
    setBusy(true);
    try {
      const c = await _secIssueCaptcha();
      await _secFetchJson(reqUrl, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: draftValue, captcha: { token: c.token, code: c.code } }),
      });
      toast && toast('Saved', 'ok');
      setMode('view');
      setDraftValue('');
      if (typeof onAfterChange === 'function') onAfterChange();
    } catch (e) {
      toast && toast(`Save failed: ${e.message || 'error'}`, 'err');
    } finally { setBusy(false); }
  }, [reqUrl, draftValue, onAfterChange, toast]);

  const doDelete = _secUseCallback(async () => {
    setBusy(true);
    try {
      const c = await _secIssueCaptcha();
      await _secFetchJson(reqUrl, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ captcha: { token: c.token, code: c.code } }),
      });
      toast && toast('Deleted', 'ok');
      if (typeof onAfterChange === 'function') onAfterChange();
    } catch (e) {
      toast && toast(`Delete failed: ${e.message || 'error'}`, 'err');
      setMode('view');
    } finally { setBusy(false); }
  }, [reqUrl, onAfterChange, toast]);

  const onUnlocked = _secUseCallback(() => {
    setUnlockOpen(false);
    const action = pendingAction;
    setPendingAction(null);
    if (action === 'reveal') doReveal();
    else if (action === 'edit') doStartEdit();
    else if (action === 'delete') setMode('confirmDelete');
  }, [pendingAction, doReveal, doStartEdit]);

  const requestAction = (action, runIfUnlocked) => {
    if (_secIsEnvUnlocked()) { runIfUnlocked(); return; }
    setPendingAction(action);
    setUnlockOpen(true);
  };

  const onClickReveal = () => {
    if (mode === 'reveal') { setMode('view'); setPlaintext(null); return; }
    requestAction('reveal', doReveal);
  };
  const onClickEdit = () => {
    if (mode === 'edit') return;
    requestAction('edit', doStartEdit);
  };
  const onClickDelete = () => {
    requestAction('delete', () => setMode('confirmDelete'));
  };

  const Btn = window.Button || ((p) => <button {...p}>{p.children}</button>);
  const IconBtn = window.IconButton || ((p) => <button onClick={p.onClick}>{p.label}</button>);

  return (
    <>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '8px 12px',
        borderBottom: '1px solid var(--hairline)',
      }}>
        <div style={{ minWidth: 0, flex: '0 1 auto' }}>
          <div className="mono" style={{
            fontSize: 'var(--t-sm)', color: 'var(--fg)', fontWeight: 500,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{item.key}</div>
        </div>

        {mode === 'view' && (
          <>
            <div style={{ flex: 1, display: 'flex', justifyContent: 'flex-end' }}>
              <MaskedValue masked={item.masked} />
            </div>
            <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
              <IconBtn icon="eye" label="Reveal" size={30} onClick={onClickReveal} />
              <IconBtn icon="pencil" label="Edit" size={30} onClick={onClickEdit} />
              <IconBtn icon="trash" label="Delete" size={30} onClick={onClickDelete} />
            </div>
          </>
        )}

        {mode === 'reveal' && (
          <>
            <code className="mono" style={{
              flex: 1, minWidth: 0,
              padding: '6px 10px',
              background: 'var(--accent-soft)',
              border: '1px solid var(--accent-line)', borderRadius: 'var(--r-sm)',
              fontSize: 'var(--t-cap)', color: 'var(--fg)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{plaintext}</code>
            <span className="mono" style={{
              fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', flexShrink: 0,
            }}>auto-hide {revealLeft}s</span>
            <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
              <IconBtn icon="copy" label="Copy" size={30} onClick={() => {
                _secCopyToClipboard(plaintext).then((ok) => {
                  toast && toast(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'err');
                });
              }} />
              <IconBtn icon="x" label="Hide" size={30} onClick={() => { setMode('view'); setPlaintext(null); }} />
              <IconBtn icon="pencil" label="Edit" size={30} onClick={onClickEdit} />
              <IconBtn icon="trash" label="Delete" size={30} onClick={onClickDelete} />
            </div>
          </>
        )}

        {mode === 'edit' && (
          <>
            <Input
              type="text"
              value={draftValue}
              onChange={setDraftValue}
              autoFocus
              disabled={busy}
              onKeyDown={(e) => {
                if (e.key === 'Enter') doSaveEdit();
                else if (e.key === 'Escape') { setMode('view'); setDraftValue(''); }
              }}
              ariaLabel={`New value for ${item.key}`}
              mono
              style={{ flex: 1 }}
              inputStyle={{ padding: '6px 10px', fontSize: 'var(--t-cap)' }}
            />
            <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
              <Btn variant="primary" size="sm" onClick={doSaveEdit} disabled={busy}>Save</Btn>
              <Btn variant="ghost" size="sm" onClick={() => { setMode('view'); setDraftValue(''); }} disabled={busy}>Cancel</Btn>
            </div>
          </>
        )}

        {mode === 'confirmDelete' && (
          <>
            <span style={{
              flex: 1, fontSize: 'var(--t-cap)', color: 'var(--err)', textAlign: 'right',
            }}>Delete <code className="mono">{item.key}</code>?</span>
            <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
              <Btn variant="danger" size="sm" onClick={doDelete} disabled={busy}>Delete</Btn>
              <Btn variant="ghost" size="sm" onClick={() => setMode('view')} disabled={busy}>Cancel</Btn>
            </div>
          </>
        )}
      </div>

      <EnvUnlockModal
        open={unlockOpen}
        onClose={() => { setUnlockOpen(false); setPendingAction(null); }}
        onUnlocked={onUnlocked}
      />
    </>
  );
}

// ─────────────────────────────────────────────────────────────
// EntryList — masked listing for one scope/kind. Pure presentational; the
// parent owns the data fetch & action plumbing. Also exposes "+ Add" and
// (when kind==='ssh') "+ Generate SSH key" CTAs.
// ─────────────────────────────────────────────────────────────
function EntryList({
  scope, kind, items, loading, error, title, subtitle,
  onReload, onAdd, onGenerateSsh,
  onReveal, onEdit, onDelete, onRowClick,
  emptyMessage,
  hideHeader = false,
}) {
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const list = Array.isArray(items) ? items : [];
  const isEmpty = !loading && list.length === 0;
  // env rows go inline (reveal/edit/delete in place, captcha unlocked once
  // per session). Other kinds keep the modal flow handled by the parent.
  const useInlineEnv = kind === 'env' && !!scope && !onRowClick;
  const defaultEmpty = (
    kind === 'env' ? 'No env entries yet.'
      : kind === 'files' ? 'No secret files yet.'
        : kind === 'ssh' ? 'No SSH entries yet.'
          : kind === 'authorized_keys' ? 'No authorized keys yet.'
            : kind === 'known_hosts' ? 'No known hosts yet.'
              : 'No entries.'
  );
  return (
    <div style={{
      background: 'var(--surface-1)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-lg)', overflow: 'hidden',
    }}>
      {!hideHeader && (
        <div style={{
          padding: '10px 12px',
          display: 'flex', alignItems: 'center', gap: 10,
          borderBottom: '1px solid var(--hairline)',
          background: 'var(--surface-2)',
        }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            {title && <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>{title}</div>}
            {subtitle && <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2 }}>{subtitle}</div>}
          </div>
          {typeof onReload === 'function' && (
            <window.IconButton icon="refresh" label="Reload" size={28} onClick={onReload} />
          )}
          {typeof onGenerateSsh === 'function' && kind === 'ssh' && (
            <window.Button variant="ghost" size="sm" icon="plus" onClick={onGenerateSsh}>Generate SSH key</window.Button>
          )}
          {typeof onAdd === 'function' && (
            <window.Button variant="primary" size="sm" icon="plus" onClick={onAdd}>Add</window.Button>
          )}
        </div>
      )}

      {error && (
        <StatusBanner variant="err" label={String(error)} inline />
      )}

      {loading && (
        <div style={{ padding: '14px 12px' }}>
          <Spinner label="Loading…" inline size={16} />
        </div>
      )}

      {isEmpty && (
        <EmptyState
          message={emptyMessage || defaultEmpty}
          action={typeof onAdd === 'function'
            ? <window.Button variant="ghost" size="sm" icon="plus" onClick={onAdd}>Add entry</window.Button>
            : undefined}
          padded
        />
      )}

      {!loading && !isEmpty && list.map((item, idx) => {
        if (useInlineEnv) {
          return (
            <EnvRow
              key={item.key || idx}
              scope={scope}
              item={item}
              onAfterChange={onReload}
              toast={toast}
            />
          );
        }
        return (
          <EntryRow
            key={item.key || item.name || item.host || item.idx || idx}
            item={item}
            kind={kind}
            onReveal={onReveal}
            onEdit={onEdit}
            onDelete={onDelete}
            onClick={onRowClick ? () => onRowClick(item) : undefined}
          />
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// RevealModal — plaintext reveal with auto-hide + Copy & close.
// Server endpoint differs by kind:
//   env     → POST /api/secrets/<scope>/env/<key>/reveal → {value}
//   files   → POST /api/secrets/<scope>/files/<name>/reveal → {content_b64}
//   ssh     → POST /api/secrets/global/ssh/private/<name>/reveal → {value}
// ─────────────────────────────────────────────────────────────
function RevealModal({ open, onClose, scope, kind, item }) {
  const captcha = _secUseCaptcha(open);
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [plaintext, setPlaintext] = _secUseState(null);
  const [submitting, setSubmitting] = _secUseState(false);
  const [error, setError] = _secUseState(null);
  const [secondsLeft, setSecondsLeft] = _secUseState(0);
  const timerRef = _secUseRef(null);

  // Stop the countdown / clear plaintext on close.
  const reset = _secUseCallback(() => {
    setPlaintext(null);
    setError(null);
    setSubmitting(false);
    setSecondsLeft(0);
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  _secUseEffect(() => {
    if (!open) reset();
    return () => reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const startCountdown = _secUseCallback(() => {
    setSecondsLeft(Math.floor(_SEC_REVEAL_TTL_MS / 1000));
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      setSecondsLeft((prev) => {
        const next = prev - 1;
        if (next <= 0) {
          if (timerRef.current) {
            clearInterval(timerRef.current);
            timerRef.current = null;
          }
          setPlaintext(null);
          return 0;
        }
        return next;
      });
    }, 1000);
  }, []);

  const onConfirm = _secUseCallback(async () => {
    if (!captcha.ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const scopeSeg = _secScopeUrlSegment(scope);
      let path;
      if (kind === 'env') {
        path = `/api/secrets/${scopeSeg}/env/${encodeURIComponent(item.key)}/reveal`;
      } else if (kind === 'files') {
        path = `/api/secrets/${scopeSeg}/files/${encodeURIComponent(item.name)}/reveal`;
      } else if (kind === 'ssh') {
        path = `/api/secrets/global/ssh/private/${encodeURIComponent(item.name)}/reveal`;
      } else {
        throw new Error(`unsupported reveal kind: ${kind}`);
      }
      const body = { captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() } };
      const data = await _secFetchJson(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      let text = '';
      if (kind === 'files' && data && data.content_b64) {
        try { text = atob(data.content_b64); }
        catch (_) { text = `<<base64 ${data.content_b64.length} chars>>`; }
      } else {
        text = (data && data.value) || '';
      }
      setPlaintext(text);
      startCountdown();
    } catch (e) {
      setError(e.message || 'reveal failed');
      captcha.reload();
    } finally {
      setSubmitting(false);
    }
  }, [captcha, submitting, scope, kind, item, startCountdown]);

  const onCopyAndClose = _secUseCallback(async () => {
    if (!plaintext) return;
    const ok = await _secCopyToClipboard(plaintext);
    if (toast) toast(ok ? 'Copied' : 'Copy failed', ok ? 'ok' : 'err');
    onClose();
  }, [plaintext, toast, onClose]);

  const title = `Reveal ${item ? (item.key || item.name) : ''}`;
  const totalSec = Math.floor(_SEC_REVEAL_TTL_MS / 1000);
  const pct = totalSec > 0 ? Math.max(0, Math.min(100, (secondsLeft / totalSec) * 100)) : 0;

  return (
    <window.Modal open={open} onClose={onClose} title={title} width={520}>
      <div style={_SECStyles.modalBody}>
        {plaintext == null && (
          <>
            <div style={_SECStyles.helpText}>
              Plaintext will auto-hide after {totalSec} seconds. The copy step is one-tap.
            </div>
            <CaptchaGate
              token={captcha.token}
              code={captcha.code}
              value={captcha.value}
              setValue={captcha.setValue}
              error={captcha.error}
              loading={captcha.loading}
            />
            {error && <StatusBanner variant="err" label={error} inline />}
          </>
        )}
        {plaintext != null && (
          <>
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '8px 10px', borderRadius: 'var(--r-control)',
              background: 'var(--accent-soft)', border: '1px solid var(--accent-line)',
              color: 'var(--accent)',
            }}>
              <span style={{ fontSize: 'var(--t-cap)', fontWeight: 500 }}>
                Auto-hide in {secondsLeft}s
              </span>
              <window.Button variant="primary" size="sm" icon="copy" onClick={onCopyAndClose}>
                Copy & close
              </window.Button>
            </div>
            <div style={{
              height: 4, background: 'var(--surface-2)',
              borderRadius: 4, overflow: 'hidden',
            }}>
              <div style={{
                width: `${pct}%`, height: '100%',
                background: 'var(--accent)', transition: 'width 1s linear',
              }} />
            </div>
            <pre className="mono" style={{
              margin: 0, padding: 12, borderRadius: 'var(--r-control)',
              background: 'var(--surface-2)', border: '1px solid var(--hairline)',
              fontSize: 'var(--t-cap)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
              color: 'var(--fg)', maxHeight: 240, overflow: 'auto',
            }}>{plaintext}</pre>
          </>
        )}
      </div>
      <div style={_SECStyles.modalFooter}>
        <window.Button variant="quiet" onClick={onClose}>Close</window.Button>
        {plaintext == null && (
          <window.Button
            variant="primary"
            icon="eye"
            onClick={onConfirm}
            style={{ opacity: captcha.ready && !submitting ? 1 : 0.5, cursor: captcha.ready && !submitting ? 'pointer' : 'not-allowed' }}>
            {submitting ? 'Revealing…' : 'Reveal'}
          </window.Button>
        )}
      </div>
    </window.Modal>
  );
}

// ─────────────────────────────────────────────────────────────
// EditModal — change an existing entry's value. Captcha required because
// it's an overwrite of plaintext.
// ─────────────────────────────────────────────────────────────
function EditModal({ open, onClose, scope, kind, item, onSaved }) {
  const captcha = _secUseCaptcha(open);
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [value, setValue] = _secUseState('');
  const [submitting, setSubmitting] = _secUseState(false);
  const [error, setError] = _secUseState(null);

  _secUseEffect(() => {
    if (open) {
      setValue('');
      setError(null);
      setSubmitting(false);
    }
  }, [open]);

  const onConfirm = _secUseCallback(async () => {
    if (!captcha.ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const scopeSeg = _secScopeUrlSegment(scope);
      let path;
      let method;
      let body;
      if (kind === 'env') {
        path = `/api/secrets/${scopeSeg}/env/${encodeURIComponent(item.key)}`;
        method = 'PUT';
        body = { value, captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() } };
      } else if (kind === 'files') {
        path = `/api/secrets/${scopeSeg}/files/${encodeURIComponent(item.name)}`;
        method = 'PUT';
        let b64 = '';
        try { b64 = btoa(unescape(encodeURIComponent(value))); }
        catch (_) { b64 = btoa(value); }
        body = { content_b64: b64, captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() } };
      } else if (kind === 'authorized_keys') {
        path = `/api/secrets/global/ssh/authorized_keys/${item.idx}`;
        method = 'PATCH';
        body = { line: value, captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() } };
      } else if (kind === 'known_hosts') {
        path = `/api/secrets/global/ssh/known_hosts/${item.idx}`;
        method = 'PATCH';
        body = { line: value, captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() } };
      } else {
        throw new Error(`unsupported edit kind: ${kind}`);
      }
      await _secFetchJson(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (toast) toast('Saved', 'ok');
      if (typeof onSaved === 'function') onSaved();
      onClose();
    } catch (e) {
      setError(e.message || 'save failed');
      captcha.reload();
    } finally {
      setSubmitting(false);
    }
  }, [captcha, submitting, scope, kind, item, value, toast, onSaved, onClose]);

  const useTextarea = kind === 'files';
  const inputLabel = kind === 'env'
    ? 'New value'
    : kind === 'files'
      ? 'New file contents'
      : 'New line';

  return (
    <window.Modal open={open} onClose={onClose} title={`Edit ${item ? (item.key || item.name || `#${item.idx}`) : ''}`} width={560}>
      <div style={_SECStyles.modalBody}>
        <div style={_SECStyles.helpText}>
          The previous value is overwritten on disk. There is no undo.
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={_SECStyles.label}>{inputLabel}</label>
          {useTextarea ? (
            <Textarea
              value={value}
              onChange={setValue}
              rows={8}
              mono
            />
          ) : (
            <Input
              type="text"
              value={value}
              onChange={setValue}
              autoComplete="off"
              mono
            />
          )}
        </div>
        <CaptchaGate
          token={captcha.token}
          code={captcha.code}
          value={captcha.value}
          setValue={captcha.setValue}
          error={captcha.error}
          loading={captcha.loading}
        />
        {error && <StatusBanner variant="err" label={error} inline />}
      </div>
      <div style={_SECStyles.modalFooter}>
        <window.Button variant="quiet" onClick={onClose}>Cancel</window.Button>
        <window.Button
          variant="primary"
          icon="check"
          onClick={onConfirm}
          style={{
            opacity: captcha.ready && !submitting && value.length > 0 ? 1 : 0.5,
            cursor: captcha.ready && !submitting && value.length > 0 ? 'pointer' : 'not-allowed',
          }}>
          {submitting ? 'Saving…' : 'Save'}
        </window.Button>
      </div>
    </window.Modal>
  );
}

// ─────────────────────────────────────────────────────────────
// DeleteModal — captcha-gated destruction. Server enforces the
// authorized_keys last-line guard; we surface the 400 message.
// ─────────────────────────────────────────────────────────────
function DeleteModal({ open, onClose, scope, kind, item, onDeleted }) {
  const captcha = _secUseCaptcha(open);
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [submitting, setSubmitting] = _secUseState(false);
  const [error, setError] = _secUseState(null);

  _secUseEffect(() => {
    if (open) {
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const onConfirm = _secUseCallback(async () => {
    if (!captcha.ready || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const scopeSeg = _secScopeUrlSegment(scope);
      let path;
      if (kind === 'env') {
        path = `/api/secrets/${scopeSeg}/env/${encodeURIComponent(item.key)}`;
      } else if (kind === 'files') {
        path = `/api/secrets/${scopeSeg}/files/${encodeURIComponent(item.name)}`;
      } else if (kind === 'authorized_keys') {
        path = `/api/secrets/global/ssh/authorized_keys/${item.idx}`;
      } else if (kind === 'known_hosts') {
        path = `/api/secrets/global/ssh/known_hosts/${item.idx}`;
      } else {
        throw new Error(`unsupported delete kind: ${kind}`);
      }
      await _secFetchJson(path, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() } }),
      });
      if (toast) toast('Deleted', 'ok');
      if (typeof onDeleted === 'function') onDeleted();
      onClose();
    } catch (e) {
      setError(e.message || 'delete failed');
      captcha.reload();
    } finally {
      setSubmitting(false);
    }
  }, [captcha, submitting, scope, kind, item, toast, onDeleted, onClose]);

  const targetLabel = item ? (item.key || item.name || (item.idx != null ? `#${item.idx}` : '')) : '';

  return (
    <window.Modal open={open} onClose={onClose} title={`Delete ${targetLabel}`} width={500}>
      <div style={_SECStyles.modalBody}>
        <div style={{ ..._SECStyles.helpText, color: 'var(--fg-2)' }}>
          This will remove <span className="mono" style={{ color: 'var(--err)' }}>{targetLabel}</span> permanently. There is no undo.
        </div>
        <CaptchaGate
          token={captcha.token}
          code={captcha.code}
          value={captcha.value}
          setValue={captcha.setValue}
          error={captcha.error}
          loading={captcha.loading}
        />
        {error && <StatusBanner variant="err" label={error} inline />}
      </div>
      <div style={_SECStyles.modalFooter}>
        <window.Button variant="quiet" onClick={onClose}>Cancel</window.Button>
        <window.Button
          variant="danger"
          icon="trash"
          onClick={onConfirm}
          style={{
            opacity: captcha.ready && !submitting ? 1 : 0.5,
            cursor: captcha.ready && !submitting ? 'pointer' : 'not-allowed',
          }}>
          {submitting ? 'Deleting…' : 'Delete'}
        </window.Button>
      </div>
    </window.Modal>
  );
}

// ─────────────────────────────────────────────────────────────
// AddEntryModal — create a new entry.
//   - env / files inserts: no captcha (server contract: captcha required
//     on overwrite, not first insert).
//   - authorized_keys / known_hosts: captcha REQUIRED. authorized_keys in
//     particular grants SSH login to the box; treating it like an env
//     "insert" would let any in-network request add a key without friction.
// ─────────────────────────────────────────────────────────────
function AddEntryModal({ open, onClose, scope, kind, onAdded }) {
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [name, setName] = _secUseState('');
  const [value, setValue] = _secUseState('');
  const [submitting, setSubmitting] = _secUseState(false);
  const [error, setError] = _secUseState(null);
  const captchaNeeded = kind === 'authorized_keys' || kind === 'known_hosts';
  const captcha = _secUseCaptcha(open && captchaNeeded);

  _secUseEffect(() => {
    if (open) {
      setName('');
      setValue('');
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const validate = _secUseCallback(() => {
    if (kind === 'env') {
      if (!name.trim()) return 'Key is required';
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name.trim())) {
        return 'Key must match [A-Za-z_][A-Za-z0-9_]*';
      }
      if (value.includes('\n')) return 'Multi-line .env values are not supported (v1)';
      return null;
    }
    if (kind === 'files') {
      if (!name.trim()) return 'Filename is required';
      if (name.includes('/') || name.includes('..')) return 'Filename must not contain slashes or ..';
      return null;
    }
    if (kind === 'authorized_keys' || kind === 'known_hosts') {
      if (!value.trim()) return 'Line is required';
      return null;
    }
    return null;
  }, [kind, name, value]);

  const validationError = validate();

  const onConfirm = _secUseCallback(async () => {
    if (validationError || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const scopeSeg = _secScopeUrlSegment(scope);
      let path;
      let method;
      let body;
      if (kind === 'env') {
        path = `/api/secrets/${scopeSeg}/env/${encodeURIComponent(name.trim())}`;
        method = 'PUT';
        body = { value };  // insert path: server doesn't require captcha
      } else if (kind === 'files') {
        path = `/api/secrets/${scopeSeg}/files/${encodeURIComponent(name.trim())}`;
        method = 'PUT';
        let b64 = '';
        try { b64 = btoa(unescape(encodeURIComponent(value))); }
        catch (_) { b64 = btoa(value); }
        body = { content_b64: b64 };
      } else if (kind === 'authorized_keys') {
        path = `/api/secrets/global/ssh/authorized_keys`;
        method = 'POST';
        body = {
          line: value.trim(),
          captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() },
        };
      } else if (kind === 'known_hosts') {
        path = `/api/secrets/global/ssh/known_hosts`;
        method = 'POST';
        body = {
          line: value.trim(),
          captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() },
        };
      } else {
        throw new Error(`unsupported add kind: ${kind}`);
      }
      await _secFetchJson(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (toast) toast('Added', 'ok');
      if (typeof onAdded === 'function') onAdded();
      onClose();
    } catch (e) {
      setError(e.message || 'add failed');
    } finally {
      setSubmitting(false);
    }
  }, [validationError, submitting, scope, kind, name, value, captcha.token, captcha.value, toast, onAdded, onClose]);

  const titleByKind = {
    env: 'Add env entry',
    files: 'Add secret file',
    authorized_keys: 'Add authorized key',
    known_hosts: 'Add known host',
  };

  return (
    <window.Modal open={open} onClose={onClose} title={titleByKind[kind] || 'Add entry'} width={560}>
      <div style={_SECStyles.modalBody}>
        {kind === 'env' && (
          <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Key</label>
              <Input
                type="text"
                value={name}
                onChange={setName}
                placeholder="MY_TOKEN"
                autoComplete="off"
                mono
              />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Value</label>
              <Input
                type="text"
                value={value}
                onChange={setValue}
                autoComplete="off"
                mono
              />
            </div>
          </>
        )}
        {kind === 'files' && (
          <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Filename</label>
              <Input
                type="text"
                value={name}
                onChange={setName}
                placeholder="service-account.json"
                autoComplete="off"
                mono
              />
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Contents</label>
              <Textarea
                value={value}
                onChange={setValue}
                rows={10}
                mono
              />
            </div>
          </>
        )}
        {(kind === 'authorized_keys' || kind === 'known_hosts') && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={_SECStyles.label}>
              {kind === 'authorized_keys' ? 'Public key line' : 'Known-hosts line'}
            </label>
            <Input
              type="text"
              value={value}
              onChange={setValue}
              placeholder={kind === 'authorized_keys'
                ? 'ssh-ed25519 AAAA... user@host'
                : 'host ssh-ed25519 AAAA...'}
              autoComplete="off"
              mono
            />
            <div style={_SECStyles.helpText}>
              Single line. The server validates the format and computes the fingerprint.
            </div>
          </div>
        )}
        {captchaNeeded && (
          <CaptchaGate
            token={captcha.token} code={captcha.code}
            value={captcha.value} setValue={captcha.setValue}
            error={captcha.error} loading={captcha.loading}
          />
        )}
        {validationError && <StatusBanner variant="err" label={validationError} inline />}
        {error && <StatusBanner variant="err" label={error} inline />}
      </div>
      <div style={_SECStyles.modalFooter}>
        <window.Button variant="quiet" onClick={onClose}>Cancel</window.Button>
        <window.Button
          variant="primary"
          icon="plus"
          onClick={onConfirm}
          style={{
            opacity: (!validationError && !submitting && (!captchaNeeded || captcha.ready)) ? 1 : 0.5,
            cursor: (!validationError && !submitting && (!captchaNeeded || captcha.ready)) ? 'pointer' : 'not-allowed',
          }}>
          {submitting ? 'Adding…' : 'Add'}
        </window.Button>
      </div>
    </window.Modal>
  );
}

// ─────────────────────────────────────────────────────────────
// GenerateSshKeyModal — wraps `ssh-keygen`. Captcha-gated because we're
// writing private key material to disk.
// ─────────────────────────────────────────────────────────────
function GenerateSshKeyModal({ open, onClose, onGenerated }) {
  const captcha = _secUseCaptcha(open);
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [name, setName] = _secUseState('');
  const [type, setType] = _secUseState('ed25519');
  const [comment, setComment] = _secUseState('');
  const [submitting, setSubmitting] = _secUseState(false);
  const [error, setError] = _secUseState(null);
  const [result, setResult] = _secUseState(null);

  _secUseEffect(() => {
    if (open) {
      setName('');
      setType('ed25519');
      setComment('');
      setSubmitting(false);
      setError(null);
      setResult(null);
    }
  }, [open]);

  const validationError = (() => {
    if (result) return null;
    if (!name.trim()) return 'Name is required';
    if (!/^[A-Za-z0-9_.-]+$/.test(name.trim())) return 'Name must be alphanumeric (with - _ . allowed)';
    if (type !== 'ed25519' && type !== 'rsa') return 'Type must be ed25519 or rsa';
    return null;
  })();

  const onConfirm = _secUseCallback(async () => {
    if (validationError || submitting) return;
    if (!captcha.ready) return;
    setSubmitting(true);
    setError(null);
    try {
      const data = await _secFetchJson('/api/secrets/global/ssh/private/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim(),
          type,
          comment: comment.trim(),
          captcha: { token: captcha.token, code: captcha.value.trim().toUpperCase() },
        }),
      });
      setResult(data || {});
      if (toast) toast('SSH key generated', 'ok');
      if (typeof onGenerated === 'function') onGenerated();
    } catch (e) {
      setError(e.message || 'generate failed');
      captcha.reload();
    } finally {
      setSubmitting(false);
    }
  }, [validationError, submitting, captcha, name, type, comment, toast, onGenerated]);

  const onCopyPub = _secUseCallback(async () => {
    if (!result || !result.public_key) return;
    const ok = await _secCopyToClipboard(result.public_key);
    if (toast) toast(ok ? 'Public key copied' : 'Copy failed', ok ? 'ok' : 'err');
  }, [result, toast]);

  return (
    <window.Modal open={open} onClose={onClose} title="Generate SSH key" width={560}>
      <div style={_SECStyles.modalBody}>
        {!result && (
          <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Name</label>
              <Input
                type="text"
                value={name}
                onChange={setName}
                placeholder="github_deploy"
                autoComplete="off"
                mono
              />
              <div style={_SECStyles.helpText}>
                Files will be written to ~/.ssh/{name.trim() || '<name>'} and ~/.ssh/{name.trim() || '<name>'}.pub.
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Algorithm</label>
              <div style={{ display: 'flex', gap: 16 }}>
                {['ed25519', 'rsa'].map((opt) => (
                  <label key={opt} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 'var(--t-sm)' }}>
                    <input
                      type="radio"
                      name="ssh-key-type"
                      value={opt}
                      checked={type === opt}
                      onChange={() => setType(opt)}
                    />
                    <span className="mono">{opt}</span>
                  </label>
                ))}
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={_SECStyles.label}>Comment</label>
              <Input
                type="text"
                value={comment}
                onChange={setComment}
                placeholder="user@host"
                autoComplete="off"
                mono
              />
            </div>
            <CaptchaGate
              token={captcha.token}
              code={captcha.code}
              value={captcha.value}
              setValue={captcha.setValue}
              error={captcha.error}
              loading={captcha.loading}
            />
            {validationError && <StatusBanner variant="err" label={validationError} inline />}
            {error && <StatusBanner variant="err" label={error} inline />}
          </>
        )}
        {result && (
          <>
            <div style={{ ..._SECStyles.helpText, color: 'var(--ok)' }}>
              Key generated. Private key stays on disk; public key + fingerprint shown below.
            </div>
            {result.fingerprint && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={_SECStyles.label}>Fingerprint</span>
                <code className="mono" style={{
                  padding: '8px 10px', borderRadius: 'var(--r-sm)',
                  background: 'var(--surface-2)', border: '1px solid var(--hairline)',
                  fontSize: 'var(--t-cap)', color: 'var(--fg)', wordBreak: 'break-all',
                }}>{result.fingerprint}</code>
              </div>
            )}
            {result.public_key && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span style={_SECStyles.label}>Public key</span>
                <pre className="mono" style={{
                  margin: 0, padding: 12, borderRadius: 'var(--r-sm)',
                  background: 'var(--surface-2)', border: '1px solid var(--hairline)',
                  fontSize: 'var(--t-cap)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                  color: 'var(--fg)', maxHeight: 200, overflow: 'auto',
                }}>{result.public_key}</pre>
                <window.Button variant="ghost" size="sm" icon="copy" onClick={onCopyPub}>Copy public key</window.Button>
              </div>
            )}
          </>
        )}
      </div>
      <div style={_SECStyles.modalFooter}>
        <window.Button variant="quiet" onClick={onClose}>{result ? 'Close' : 'Cancel'}</window.Button>
        {!result && (
          <window.Button
            variant="primary"
            icon="plus"
            onClick={onConfirm}
            style={{
              opacity: !validationError && captcha.ready && !submitting ? 1 : 0.5,
              cursor: !validationError && captcha.ready && !submitting ? 'pointer' : 'not-allowed',
            }}>
            {submitting ? 'Generating…' : 'Generate'}
          </window.Button>
        )}
      </div>
    </window.Modal>
  );
}

Object.assign(window, {
  MaskedValue,
  EntryRow,
  EnvRow,
  EnvUnlockModal,
  EntryList,
  CaptchaGate,
  RevealModal,
  EditModal,
  DeleteModal,
  AddEntryModal,
  GenerateSshKeyModal,
  // Helpers exposed so secrets-view / library-secrets-panel can reuse them.
  _secScopeUrlSegment,
  _secFetchJson,
  _secUseCaptcha,
  _SECInputStyle,
});
