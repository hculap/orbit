// library-branch-panel.jsx — Branches tab body for library detail view.
//
// Extracted from library-detail.jsx to keep that file under the project's
// 800 LOC ceiling and because the branches tab grew quite a bit:
//   - Working tree status (porcelain) with status_x/y chips
//   - Commit message + Commit button → POST /git/commit
//   - Push button → POST /git/push (when ahead > 0 OR upstream missing)
//   - Open PR modal (when current branch != default branch + has_github_remote)
//   - Branch list with switch + create + checkout
//
// Endpoints consumed (relative to apiBase):
//   GET  /git                           — fetched by parent, passed in via props
//   GET  /git/status                    — porcelain v1 working tree status
//   POST /git/checkout      body: {branch, create?, force?}
//   POST /git/commit        body: {message}
//   POST /git/push
//   POST /github/prs        body: {title, body, base?}
//
// Backend may return `{ok: false, error: "nothing to commit"}` for empty
// commits — surfaced inline.

const { useState: _lbpUseState, useEffect: _lbpUseEffect, useCallback: _lbpUseCallback } = React;

function _lbpToast() {
  if (typeof window !== 'undefined' && typeof window.useToast === 'function') {
    return window.useToast();
  }
  return null;
}

// Map porcelain v1 status codes to a friendly label + chip color.
// X is the index/staged column; Y is the worktree column.
// '?' '?' is the conventional untracked marker.
function _lbpStatusLabel(x, y) {
  const sx = (x || ' ').trim();
  const sy = (y || ' ').trim();
  if (x === '?' && y === '?') return { label: 'untracked',  tone: 'add' };
  if (sx === 'A' || sy === 'A') return { label: 'added',     tone: 'add' };
  if (sx === 'D' || sy === 'D') return { label: 'deleted',   tone: 'del' };
  if (sx === 'R' || sy === 'R') return { label: 'renamed',   tone: 'mod' };
  if (sx === 'C' || sy === 'C') return { label: 'copied',    tone: 'mod' };
  if (sx === 'M' || sy === 'M') return { label: 'modified',  tone: 'mod' };
  if (sx === 'U' || sy === 'U') return { label: 'conflict',  tone: 'del' };
  return { label: ((x || '') + (y || '')).trim() || '·', tone: 'mod' };
}

function _LbpStatusChip({ x, y }) {
  const { tone } = _lbpStatusLabel(x, y);
  const palette = {
    add: { bg: 'oklch(0.30 0.10 145 / 0.18)', border: 'oklch(0.55 0.13 145)', fg: 'oklch(0.78 0.13 145)' },
    mod: { bg: 'var(--surface-2)',             border: 'var(--hairline)',     fg: 'var(--fg-2)' },
    del: { bg: 'var(--err-bg)',   border: 'var(--err)',          fg: 'var(--err)' },
  };
  const p = palette[tone] || palette.mod;
  return (
    <span className="mono" style={{
      display: 'inline-block', minWidth: 28, textAlign: 'center',
      padding: '2px 6px', borderRadius: 4,
      fontSize: 10.5, color: p.fg,
      background: p.bg, border: `1px solid ${p.border}`,
    }}>{((x || '·')[0]) + ((y || '·')[0])}</span>
  );
}

// ─────────────────────────────────────────────────────────────
// Working-tree panel
// ─────────────────────────────────────────────────────────────
function _LbpWorkingTree({ apiBase, refreshKey, onChanged, compact }) {
  const [data, setData] = _lbpUseState(null);
  const [loading, setLoading] = _lbpUseState(true);
  const [err, setErr] = _lbpUseState(null);

  const reload = _lbpUseCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch(apiUrl(`${apiBase}/git/status`));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const d = await r.json();
      setData(d || { ok: true, files: [] });
    } catch (e) {
      setErr(e.message || 'failed to load status');
      setData(null);
    } finally { setLoading(false); }
  }, [apiBase]);

  _lbpUseEffect(() => { reload(); }, [reload, refreshKey]);
  // Bubble dirty flag up so parent (Branches panel) can light up Push button.
  _lbpUseEffect(() => {
    if (typeof onChanged === 'function') onChanged(data);
  }, [data, onChanged]);

  const files = (data && Array.isArray(data.files)) ? data.files : [];

  return (
    <Card padding={0}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px', borderBottom: '1px solid var(--hairline)' }}>
        <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>Working tree</span>
        <span style={{ flex: 1 }} />
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
          {loading ? '…' : `${files.length} ${files.length === 1 ? 'file' : 'files'}`}
        </span>
        <Button size="sm" variant="ghost" icon="refresh" onClick={reload} type="button">Refresh</Button>
      </div>
      {loading && (
        <div style={{ padding: 14 }}><Spinner inline size={14} label="loading…" /></div>
      )}
      {!loading && err && (
        <div className="mono" style={{ padding: 14, fontSize: 'var(--t-cap)', color: 'var(--err)' }}>error: {err}</div>
      )}
      {!loading && !err && files.length === 0 && (
        <div className="mono" style={{ padding: 14, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(clean)</div>
      )}
      {!loading && !err && files.length > 0 && (
        <ul style={{ listStyle: 'none', margin: 0, padding: '4px 0', maxHeight: compact ? 220 : 320, overflowY: 'auto' }}
            className="scroll-hide">
          {files.map((f, i) => (
            <li key={(f.path || i) + '@' + i} style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '6px 14px',
            }}>
              <_LbpStatusChip x={f.status_x} y={f.status_y} />
              <code className="mono" style={{
                fontSize: 'var(--t-cap)', color: 'var(--fg-2)',
                flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{f.path || '—'}</code>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Commit + push panel
// ─────────────────────────────────────────────────────────────
function _LbpCommitPanel({ apiBase, ahead, hasUpstream, dirty, onAfterCommit, onAfterPush }) {
  const [message, setMessage] = _lbpUseState('');
  const [committing, setCommitting] = _lbpUseState(false);
  const [pushing, setPushing] = _lbpUseState(false);
  const [info, setInfo] = _lbpUseState(null);  // {kind:'ok'|'err', text}
  const toast = _lbpToast();

  const canPush = (ahead > 0) || !hasUpstream;
  const canCommit = !!message.trim() && !committing && dirty;

  const commit = _lbpUseCallback(async () => {
    if (!canCommit) return;
    setCommitting(true); setInfo(null);
    try {
      const r = await fetch(apiUrl(`${apiBase}/git/commit`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: message.trim() }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d && d.ok === false && d.error) {
        // Backend returns 200 + ok:false for "nothing to commit".
        setInfo({ kind: 'err', text: d.error });
        return;
      }
      if (!r.ok || (d && d.ok === false)) {
        const msg = (d && (d.error || d.detail)) || `http ${r.status}`;
        throw new Error(String(msg).slice(0, 200));
      }
      const sha = (d && d.sha) ? String(d.sha).slice(0, 7) : '';
      const fc = (d && typeof d.files_changed === 'number') ? d.files_changed : null;
      const okText = `Committed ${sha}${fc != null ? ` · ${fc} file${fc === 1 ? '' : 's'}` : ''}`;
      toast && toast(okText, 'ok');
      setInfo({ kind: 'ok', text: okText });
      setMessage('');
      onAfterCommit && onAfterCommit(d);
    } catch (e) {
      setInfo({ kind: 'err', text: e.message || 'commit failed' });
    } finally { setCommitting(false); }
  }, [apiBase, canCommit, message, toast, onAfterCommit]);

  const push = _lbpUseCallback(async () => {
    if (pushing) return;
    setPushing(true); setInfo(null);
    try {
      const r = await fetch(apiUrl(`${apiBase}/git/push`), { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || (d && d.ok === false)) {
        const msg = (d && (d.error || d.detail)) || `http ${r.status}`;
        throw new Error(String(msg).slice(0, 200));
      }
      const tgt = (d && d.pushed_to) ? d.pushed_to : 'remote';
      toast && toast(`Pushed → ${tgt}`, 'ok');
      setInfo({ kind: 'ok', text: `Pushed → ${tgt}` });
      onAfterPush && onAfterPush(d);
    } catch (e) {
      setInfo({ kind: 'err', text: e.message || 'push failed' });
    } finally { setPushing(false); }
  }, [apiBase, pushing, toast, onAfterPush]);

  return (
    <Card padding={18}>
      <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
        Commit &amp; push
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <Textarea
          value={message}
          onChange={setMessage}
          placeholder={dirty ? 'Commit message…' : '(working tree clean)'}
          rows={3}
          disabled={!dirty}
          mono={false}
          style={{ resize: 'vertical', minHeight: 64, opacity: dirty ? 1 : 0.6 }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, flexWrap: 'wrap' }}>
          <Button
            size="sm" variant="primary"
            icon={committing ? 'spinner' : 'check'}
            onClick={commit} type="button"
            style={canCommit ? undefined : { opacity: 0.5, pointerEvents: 'none' }}
          >
            {committing ? 'Committing…' : 'Commit'}
          </Button>
          <Button
            size="sm" variant={canPush ? 'primary' : 'ghost'}
            icon={pushing ? 'spinner' : 'arrow-up'}
            onClick={push} type="button"
            style={canPush && !pushing ? undefined : { opacity: 0.5, pointerEvents: 'none' }}
          >
            {pushing ? 'Pushing…' : (hasUpstream ? `Push${ahead ? ` (${ahead})` : ''}` : 'Push (set upstream)')}
          </Button>
        </div>
        {info && (
          <StatusBanner variant={info.kind === 'ok' ? 'ok' : 'err'} label={info.text} inline />
        )}
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Open-PR modal
// ─────────────────────────────────────────────────────────────
function _LbpOpenPRModal({ apiBase, currentBranch, defaultBranch, onClose, onCreated }) {
  const [title, setTitle] = _lbpUseState(currentBranch || '');
  const [body, setBody] = _lbpUseState('');
  const [base, setBase] = _lbpUseState(defaultBranch || 'main');
  const [busy, setBusy] = _lbpUseState(false);
  const [err, setErr] = _lbpUseState(null);
  const toast = _lbpToast();

  const submit = async (e) => {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    const t = title.trim();
    if (!t) { setErr('Title is required'); return; }
    if (busy) return;
    setBusy(true); setErr(null);
    try {
      const payload = { title: t, body, base: base || undefined };
      // Backend exposes PR creation under /git/open-pr (gh shell-out wrapper);
      // /github/prs only has GET list/view + PATCH body.
      const r = await fetch(apiUrl(`${apiBase}/git/open-pr`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 424) {
        throw new Error((d && d.error) || 'gh CLI not authorised / no remote');
      }
      if (!r.ok || (d && d.ok === false)) {
        throw new Error(((d && (d.error || d.detail)) || `http ${r.status}`).toString());
      }
      const url = d && d.url ? d.url : null;
      const num = d && d.number ? `#${d.number}` : '';
      toast && toast(`PR opened ${num}`, 'ok');
      onCreated && onCreated({ url, number: d && d.number });
    } catch (e) {
      setErr(e.message || 'PR open failed');
    } finally { setBusy(false); }
  };

  const inputStyle = {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
  };
  const labelEyebrow = {
    fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
    textTransform: 'uppercase', letterSpacing: '0.08em',
  };
  const labelStyle = { display: 'flex', flexDirection: 'column', gap: 6 };

  return (
    <Modal open={true} onClose={onClose} title="Open pull request" width={520}>
      <form onSubmit={submit} style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div className="mono" style={{ fontSize: 11.5, color: 'var(--fg-3)' }}>
          {currentBranch} <span style={{ color: 'var(--fg-4)' }}>→</span> <input
            value={base} onChange={(e) => setBase(e.target.value)}
            spellCheck={false}
            style={{ ...inputStyle, display: 'inline-block', width: 160, padding: '4px 8px', fontSize: 'var(--t-cap)' }}
          />
        </div>
        <label style={labelStyle}>
          <span className="mono" style={labelEyebrow}>Title</span>
          <Input autoFocus value={title} onChange={setTitle}
                 placeholder="Short summary" mono={false}
                 style={{ width: '100%', boxSizing: 'border-box', padding: '10px 12px' }}
                 inputStyle={{ fontSize: 'var(--t-sm)', fontFamily: 'inherit', color: 'var(--fg)' }} />
        </label>
        <label style={labelStyle}>
          <span className="mono" style={labelEyebrow}>Body (markdown, optional)</span>
          <Textarea value={body} onChange={setBody} rows={8}
                    mono
                    style={{ resize: 'vertical' }}
                    inputStyle={{ fontSize: 12.5, lineHeight: 1.6 }} />
        </label>
        {err && (
          <StatusBanner variant="err" label={err} inline />
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="quiet" onClick={onClose} type="button">Cancel</Button>
          <Button variant="primary" type="submit" icon={busy ? 'spinner' : 'plus'}>
            {busy ? 'Opening…' : 'Open PR'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ─────────────────────────────────────────────────────────────
// Public component
// ─────────────────────────────────────────────────────────────
function LibraryBranchPanel({ apiBase, git, loading, onReload, hasGithub, defaultBranch, compact }) {
  const [busy, setBusy] = _lbpUseState(false);
  const [filter, setFilter] = _lbpUseState('');
  const [err, setErr] = _lbpUseState(null);
  const [statusKey, setStatusKey] = _lbpUseState(0);
  const [wtData, setWtData] = _lbpUseState(null);
  const [prModal, setPrModal] = _lbpUseState(false);
  const [fetching, setFetching] = _lbpUseState(false);

  const toast = _lbpToast();

  const branch = (git && git.branch) || '';
  const ahead = (git && typeof git.ahead === 'number') ? git.ahead : 0;
  const behind = (git && typeof git.behind === 'number') ? git.behind : 0;
  const branches = Array.isArray(git && git.branches) ? git.branches : [];
  const remoteUrl = (git && git.remote_url) || '';
  const hasUpstream = !!(git && (git.upstream || git.has_upstream));
  // Backend exposes default branch via git status (best-effort) or we fall
  // back to the prop (default — derived from item.has_github_remote bundle).
  const effDefault = (git && git.default_branch) || defaultBranch || 'main';
  const isOnDefault = !!branch && branch === effDefault;
  const dirty = !!(git && git.dirty) || (wtData && Array.isArray(wtData.files) && wtData.files.length > 0);

  const refreshAll = _lbpUseCallback(async () => {
    setStatusKey((k) => k + 1);
    if (typeof onReload === 'function') await onReload();
  }, [onReload]);

  const fetchRemote = _lbpUseCallback(async () => {
    if (fetching) return;
    setFetching(true); setErr(null);
    try {
      const r = await fetch(apiUrl(`${apiBase}/git/fetch`), { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || (d && d.ok === false)) {
        const msg = (d && (d.error || d.detail)) || `http ${r.status}`;
        throw new Error(String(msg).slice(0, 200));
      }
      toast && toast('Fetched from remote', 'ok');
      if (typeof onReload === 'function') await onReload();
    } catch (e) {
      setErr(e.message || 'fetch failed');
    } finally { setFetching(false); }
  }, [apiBase, fetching, toast, onReload]);

  const switchTo = _lbpUseCallback(async (target, create = false, force = false) => {
    if (!target || busy) return;
    setBusy(true); setErr(null);
    try {
      const body = { branch: target, create, force };
      const r = await fetch(apiUrl(`${apiBase}/git/checkout`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.status === 409) {
        const detail = await r.json().catch(() => ({}));
        const msg = (detail && (detail.error || detail.detail)) || 'Working tree is dirty.';
        if (window.confirm(`${msg}\n\nForce checkout? (uncommitted changes may be lost)`)) {
          setBusy(false);
          return switchTo(target, create, true);
        }
        return;
      }
      if (!r.ok) {
        let msg = `http ${r.status}`;
        try {
          const d = await r.json();
          if (d && (d.error || d.detail)) msg = String(d.error || d.detail).slice(0, 200);
        } catch (_) { /* ignore */ }
        throw new Error(msg);
      }
      toast && toast(create ? `Created ${target}` : `Checked out ${target}`, 'ok');
      if (create) setFilter('');
      await refreshAll();
    } catch (e) {
      setErr(e.message || 'checkout failed');
    } finally { setBusy(false); }
  }, [apiBase, busy, toast, refreshAll]);

  if (loading) {
    return (
      <div style={{ padding: 22 }}><Spinner inline size={14} label="loading…" /></div>
    );
  }

  if (!git || git._error || !git.is_repo) {
    return (
      <div style={{ padding: compact ? 16 : 24 }}>
        <Card padding={18}>
          <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)' }}>
            {git && git._error ? `Error: ${git._error}` : 'Not a git repository.'}
          </div>
          <div className="mono" style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
            Use the Overview tab to <code>git init</code> + attach a remote.
          </div>
        </Card>
      </div>
    );
  }

  const showPRButton = hasGithub && !isOnDefault;

  return (
    <div style={{
      padding: compact ? 16 : 24,
      paddingBottom: compact ? 100 : 24,
      display: 'flex', flexDirection: 'column', gap: 14,
    }}>
      {/* Status card */}
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          Status
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>branch</span>
            <code className="mono" style={{
              fontSize: 'var(--t-cap)', color: 'var(--fg)',
              padding: '2px 8px', borderRadius: 'var(--r-sm)',
              background: 'var(--surface-2)', border: '1px solid var(--hairline)',
            }}>{branch || '—'}</code>
            {dirty ? <Chip mono>dirty</Chip> : <Chip mono accent>clean</Chip>}
            {!isOnDefault && (
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
                (default: {effDefault})
              </span>
            )}
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-2)' }}>
            ahead <span style={{ color: 'var(--fg)' }}>{ahead}</span>
            {' · '}
            behind <span style={{ color: 'var(--fg)' }}>{behind}</span>
            {' · '}
            upstream <span style={{ color: 'var(--fg)' }}>{hasUpstream ? 'yes' : 'no'}</span>
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            remote: <span style={{ color: 'var(--fg-2)' }}>{remoteUrl || '–'}</span>
          </div>
        </div>
        <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Button size="sm" variant="ghost" icon="refresh" onClick={refreshAll} type="button">Refresh</Button>
          {showPRButton && (
            <Button size="sm" variant="primary" icon="plus" onClick={() => setPrModal(true)} type="button">
              Open PR
            </Button>
          )}
        </div>
      </Card>

      {/* Working tree (full width) */}
      <div style={{ gridColumn: compact ? 'auto' : '1 / -1' }}>
        <_LbpWorkingTree apiBase={apiBase} refreshKey={statusKey} onChanged={setWtData} compact={compact} />
      </div>

      {/* Commit + push (full width) */}
      <div style={{ gridColumn: compact ? 'auto' : '1 / -1' }}>
        <_LbpCommitPanel
          apiBase={apiBase}
          ahead={ahead}
          hasUpstream={hasUpstream}
          dirty={dirty}
          onAfterCommit={refreshAll}
          onAfterPush={refreshAll}
        />
      </div>

      {/* Unified Branches list — filter, fetch, create + checkout, list */}
      {(() => {
        const trimmed = filter.trim();
        const matches = trimmed
          ? branches.filter((b) => b.toLowerCase().includes(trimmed.toLowerCase()))
          : branches;
        const exactMatch = trimmed && branches.some((b) => b === trimmed);
        const showCreate = trimmed && !exactMatch;
        const onSubmit = (e) => {
          e.preventDefault();
          if (!trimmed || busy) return;
          if (exactMatch) {
            if (trimmed !== branch) switchTo(trimmed, false);
          } else {
            switchTo(trimmed, true);
          }
        };
        return (
          <Card padding={0} style={{ gridColumn: compact ? 'auto' : '1 / -1' }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '12px 14px', borderBottom: '1px solid var(--hairline)',
            }}>
              <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>Branches</span>
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
                {branches.length} total
              </span>
              <span style={{ flex: 1 }} />
              <Button size="sm" variant="ghost"
                      icon={fetching ? 'spinner' : 'download'}
                      onClick={fetchRemote} type="button"
                      style={fetching || !hasGithub ? { opacity: 0.6, pointerEvents: fetching ? 'none' : undefined } : undefined}
                      title={hasGithub ? 'git fetch --prune --all' : 'no remote configured'}>
                {fetching ? 'Fetching…' : 'Fetch'}
              </Button>
              <Button size="sm" variant="ghost" icon="refresh" onClick={refreshAll} type="button">
                Refresh
              </Button>
            </div>

            <form onSubmit={onSubmit}
                  style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 14px', borderBottom: '1px solid var(--hairline)' }}>
              <Input
                value={filter}
                onChange={setFilter}
                placeholder="filter or new branch name…"
                mono
                style={{ flex: 1, minWidth: 0 }}
                inputStyle={{ fontSize: 'var(--t-sm)' }}
              />
              {showCreate ? (
                <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'plus'}
                        type="submit"
                        style={busy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
                  {busy ? 'Working…' : 'Create + checkout'}
                </Button>
              ) : exactMatch && trimmed !== branch ? (
                <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'arrow-right'}
                        type="submit"
                        style={busy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
                  {busy ? 'Working…' : 'Checkout'}
                </Button>
              ) : null}
            </form>

            <ul style={{ listStyle: 'none', margin: 0, padding: '4px 0', maxHeight: compact ? 280 : 420, overflowY: 'auto' }}
                className="scroll-hide">
              {matches.length === 0 && (
                <li className="mono" style={{ padding: '12px 18px', fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
                  {trimmed ? `(no branch matches "${trimmed}")` : '(no branches reported)'}
                </li>
              )}
              {matches.map((b) => {
                const isCurrent = b === branch;
                const isDefault = b === effDefault;
                return (
                  <li key={b} style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '8px 18px', borderBottom: '1px solid var(--hairline)',
                  }}>
                    <code className="mono" style={{
                      fontSize: 'var(--t-cap)', color: isCurrent ? 'var(--accent)' : 'var(--fg)',
                      flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>{b}</code>
                    {isDefault && !isCurrent && <Chip mono>default</Chip>}
                    {isCurrent ? (
                      <Chip mono accent>current</Chip>
                    ) : (
                      <Button size="sm" variant="ghost"
                              onClick={() => switchTo(b, false)} type="button"
                              style={busy ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
                        Checkout
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          </Card>
        );
      })()}

      {err && (
        <StatusBanner variant="err" label={err} inline style={{ gridColumn: compact ? 'auto' : '1 / -1' }} />
      )}

      {prModal && (
        <_LbpOpenPRModal
          apiBase={apiBase}
          currentBranch={branch}
          defaultBranch={effDefault}
          onClose={() => setPrModal(false)}
          onCreated={(out) => {
            setPrModal(false);
            if (out && out.url) {
              try { window.open(out.url, '_blank', 'noopener'); } catch (_) { /* ignore */ }
            }
          }}
        />
      )}
    </div>
  );
}

Object.assign(window, { LibraryBranchPanel });
