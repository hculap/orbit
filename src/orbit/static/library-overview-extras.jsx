// library-overview-extras.jsx — sidebar panels mounted by
// LibraryOverviewPanel (Overview tab):
//   - LinkedItemsPanel   — chips of currently linked Areas/Projects with a
//                          "+ Link new" popover wrapping LibraryLinkPicker.
//   - OverviewGitPanel   — repo onboarding (init / attach remote) + status
//                          block when the repo is wired up.
//   - RecentCommitsPanel — last N commits via /git/recent.
//
// Extracted from library-overview-panels.jsx to keep that file under the
// 800 LOC project ceiling.
//
// All publish via Object.assign(window, …) so the parent reads them lazily.

const { useState: _loxUseState, useEffect: _loxUseEffect, useMemo: _loxUseMemo, useCallback: _loxUseCallback } = React;

// ─────────────────────────────────────────────────────────────
// LinkedItemsPanel — chips + popover picker.
// Clicking a chip dispatches `library:open-detail` so the app shell can
// swap the detail view (avoids prop-drilling navigation through detail).
// ─────────────────────────────────────────────────────────────
function LinkedItemsPanel({ kind, item, candidates, linkedNames, onLinksChange }) {
  const [pickerOpen, setPickerOpen] = _loxUseState(false);
  const wrapRef = React.useRef(null);

  // Click-outside dismiss for the popover.
  _loxUseEffect(() => {
    if (!pickerOpen) return undefined;
    const onDoc = (e) => {
      if (!wrapRef.current) return;
      if (e.target && wrapRef.current.contains(e.target)) return;
      setPickerOpen(false);
    };
    const onEsc = (e) => { if (e.key === 'Escape') setPickerOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('touchstart', onDoc);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('touchstart', onDoc);
      document.removeEventListener('keydown', onEsc);
    };
  }, [pickerOpen]);

  // Resolve each linked-name to its candidate object so we can render
  // its label + click into its detail. Falls back to a synthetic stub if
  // the candidate vanished from hub data (rare; e.g. broken symlink).
  const linkedItems = _loxUseMemo(() => {
    const byName = new Map();
    (candidates || []).forEach((c) => { if (c && c.name) byName.set(c.name, c); });
    return (linkedNames || []).map((n) => byName.get(n) || { name: n, label: n });
  }, [candidates, linkedNames]);

  const otherKindLabel = kind === 'area' ? 'projects' : 'areas';
  const otherKindIcon = kind === 'area' ? '📦' : '📂';
  const otherKind = kind === 'area' ? 'project' : 'area';

  const openLinked = (target) => {
    if (!target) return;
    window.dispatchEvent(new CustomEvent('library:open-detail', {
      detail: { kind: otherKind, name: target.name, lib_id: target.lib_id || target.name },
    }));
  };

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', flex: 1 }}>
          Linked {otherKindLabel}
        </div>
        <div ref={wrapRef} style={{ position: 'relative' }}>
          <Button size="sm" variant="ghost" icon="plus"
                  onClick={() => setPickerOpen((o) => !o)} type="button">
            Link new
          </Button>
          {pickerOpen && window.LibraryLinkPicker && (
            <div style={{
              position: 'absolute', top: '100%', right: 0, zIndex: 30,
              marginTop: 6, minWidth: 280, maxWidth: 360,
              background: 'var(--surface-1)',
              border: '1px solid var(--hairline-strong)',
              borderRadius: 'var(--r-md, 8px)',
              boxShadow: 'var(--shadow-2)',
              padding: 12,
            }}>
              <window.LibraryLinkPicker
                kind={kind}
                item={item}
                candidates={candidates}
                value={linkedNames}
                onChange={onLinksChange}
              />
            </div>
          )}
        </div>
      </div>

      {linkedItems.length === 0 ? (
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
          no links yet
        </div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {linkedItems.map((c) => (
            <span
              key={c.name}
              onClick={() => openLinked(c)}
              className="mono"
              role="link"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') openLinked(c); }}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                padding: '4px 10px', borderRadius: 'var(--r-pill)',
                fontSize: 'var(--t-cap)', lineHeight: '14px',
                background: 'var(--accent-soft)', color: 'var(--accent)',
                border: '1px solid var(--accent-line)',
                cursor: 'pointer', whiteSpace: 'nowrap',
              }}
              title={`Open ${c.label || c.name}`}
            >
              <span>{otherKindIcon}</span>
              <span>{c.label || c.name}</span>
            </span>
          ))}
        </div>
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// OverviewGitPanel — repo onboarding + status block.
// Three states based on item.is_repo / item.has_github_remote:
//   1. !is_repo                   → Init + Attach remote URL
//   2. is_repo && !has_github     → Attach remote URL only
//   3. is_repo && has_github      → Status block + Open on GitHub
// onItemChanged({is_repo?, has_github_remote?}) lets the parent merge
// new flags into its localItem so the UI re-renders without waiting on
// a hub refetch.
// ─────────────────────────────────────────────────────────────
function _loxGithubUrlFromOrigin(remote) {
  if (!remote || typeof remote !== 'string') return null;
  // Match git@github.com:owner/repo(.git) or https://github.com/owner/repo(.git)
  let m = remote.match(/^git@github\.com:([^/]+)\/([^/.]+)(?:\.git)?$/i);
  if (m) return `https://github.com/${m[1]}/${m[2]}`;
  m = remote.match(/^https?:\/\/github\.com\/([^/]+)\/([^/.]+?)(?:\.git)?\/?$/i);
  if (m) return `https://github.com/${m[1]}/${m[2]}`;
  return null;
}

function _LoxErrInline({ message }) {
  if (!message) return null;
  return <StatusBanner variant="err" label={message} inline style={{ marginTop: 10 }} />;
}

function OverviewGitPanel({ apiBase, item, gitState, gitLoading, onReloadGit, onItemChanged }) {
  const isRepo = !!(item && item.is_repo);
  const hasGithub = !!(item && item.has_github_remote);
  const [remoteUrl, setRemoteUrl] = _loxUseState('');
  const [busy, setBusy] = _loxUseState(null); // 'init' | 'attach' | null
  const [err, setErr] = _loxUseState(null);
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast() : null;

  const initRepo = _loxUseCallback(async () => {
    if (busy) return;
    setBusy('init'); setErr(null);
    try {
      const r = await fetch(apiUrl(`${apiBase}/git/init`), { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || (d && d.ok === false)) {
        throw new Error(((d && (d.error || d.detail)) || `http ${r.status}`).toString());
      }
      toast && toast(d && d.already_initialized ? 'Already a git repo' : 'Initialised git repo', 'ok');
      onItemChanged && onItemChanged({ is_repo: true });
      window.dispatchEvent(new CustomEvent('library:reload'));
      onReloadGit && onReloadGit();
    } catch (e) {
      setErr(e.message || 'init failed');
    } finally { setBusy(null); }
  }, [apiBase, busy, toast, onItemChanged, onReloadGit]);

  const attachRemote = _loxUseCallback(async (e) => {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    const url = remoteUrl.trim();
    if (!url) { setErr('Remote URL is required'); return; }
    if (busy) return;
    setBusy('attach'); setErr(null);
    try {
      const r = await fetch(apiUrl(`${apiBase}/git/attach-remote`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, fetch: true }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || (d && d.ok === false)) {
        throw new Error(((d && (d.error || d.detail)) || `http ${r.status}`).toString());
      }
      toast && toast('Remote attached', 'ok');
      const isGithub = /github\.com/i.test(url);
      onItemChanged && onItemChanged({ is_repo: true, has_github_remote: isGithub || hasGithub });
      window.dispatchEvent(new CustomEvent('library:reload'));
      onReloadGit && onReloadGit();
      setRemoteUrl('');
    } catch (e) {
      setErr(e.message || 'attach failed');
    } finally { setBusy(null); }
  }, [apiBase, remoteUrl, busy, hasGithub, toast, onItemChanged, onReloadGit]);

  const inputStyle = {
    flex: 1, minWidth: 0, boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '8px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)',
    fontFamily: 'JetBrains Mono, ui-monospace, monospace', outline: 'none',
  };

  // Helper for the attach-remote form (shared by stages 1 + 2).
  const attachForm = (
    <form onSubmit={attachRemote} style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <Input
        value={remoteUrl}
        onChange={setRemoteUrl}
        placeholder="git@github.com:owner/repo.git"
        style={{ flex: 1, minWidth: 0 }}
        inputStyle={{
          boxSizing: 'border-box',
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
          borderRadius: 'var(--r-control)', padding: '8px 12px',
          color: 'var(--fg)', fontSize: 'var(--t-sm)',
          fontFamily: 'JetBrains Mono, ui-monospace, monospace', outline: 'none',
        }}
      />
      <Button size="sm" variant="primary"
              icon={busy === 'attach' ? 'spinner' : 'plus'}
              type="submit"
              style={remoteUrl.trim() ? undefined : { opacity: 0.5, pointerEvents: 'none' }}>
        {busy === 'attach' ? 'Attaching…' : 'Attach remote'}
      </Button>
    </form>
  );

  // Stage 1: not a repo — offer Init + Attach.
  if (!isRepo) {
    return (
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          Git
        </div>
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginBottom: 12 }}>
          Not a git repository.
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div>
            <Button size="sm" variant="primary"
                    icon={busy === 'init' ? 'spinner' : 'plus'}
                    onClick={initRepo} type="button"
                    style={busy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
              {busy === 'init' ? 'Initializing…' : 'Initialize git repo'}
            </Button>
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
            …or attach an existing remote (will <code>git init</code> if needed):
          </div>
          {attachForm}
        </div>
        <_LoxErrInline message={err} />
      </Card>
    );
  }

  // Stage 2: repo without github remote — offer Attach.
  if (!hasGithub) {
    return (
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          Git
        </div>
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginBottom: 12 }}>
          Repo has no GitHub remote yet.
        </div>
        {attachForm}
        <_LoxErrInline message={err} />
      </Card>
    );
  }

  // Stage 3: repo + github — show status block.
  const branch = (gitState && gitState.branch) || '';
  const ahead = (gitState && typeof gitState.ahead === 'number') ? gitState.ahead : 0;
  const behind = (gitState && typeof gitState.behind === 'number') ? gitState.behind : 0;
  const dirty = !!(gitState && gitState.dirty);
  const remote = (gitState && gitState.remote_url) || '';
  const ghUrl = _loxGithubUrlFromOrigin(remote);
  const lastCommit = (gitState && gitState.last_commit) || (gitState && gitState.head_commit) || null;

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', flex: 1 }}>
          Git
        </div>
        <Button size="sm" variant="ghost" icon="refresh" onClick={onReloadGit} type="button">Refresh</Button>
      </div>
      {gitLoading && !gitState && (
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>loading…</div>
      )}
      {gitState && gitState._error && (
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--err)' }}>error: {gitState._error}</div>
      )}
      {gitState && !gitState._error && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <code className="mono" style={{
              fontSize: 'var(--t-cap)', color: 'var(--fg)',
              padding: '2px 8px', borderRadius: 'var(--r-sm)',
              background: 'var(--surface-2)', border: '1px solid var(--hairline)',
            }}>{branch || '—'}</code>
            {dirty ? <Chip mono>dirty</Chip> : <Chip mono accent>clean</Chip>}
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
              ahead <span style={{ color: 'var(--fg)' }}>{ahead}</span>
              {' · '}
              behind <span style={{ color: 'var(--fg)' }}>{behind}</span>
            </span>
          </div>
          {lastCommit && (
            <div className="mono" style={{ fontSize: 11.5, color: 'var(--fg-2)' }}>
              <code style={{ color: 'var(--fg-3)' }}>
                {lastCommit.sha ? lastCommit.sha.slice(0, 7) : ''}
              </code>{' '}
              <span>{lastCommit.subject || ''}</span>
              {lastCommit.author && (
                <span style={{ color: 'var(--fg-4)' }}> · {lastCommit.author}</span>
              )}
              {lastCommit.ts && (
                <span style={{ color: 'var(--fg-4)' }}>
                  {' · '}{(typeof window.relTime === 'function') ? window.relTime(lastCommit.ts) : ''}
                </span>
              )}
            </div>
          )}
          <div className="mono" style={{
            fontSize: 11.5, color: 'var(--fg-3)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            remote: <span style={{ color: 'var(--fg-2)' }}>{remote || '–'}</span>
          </div>
          {ghUrl && (
            <div>
              <a href={ghUrl} target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none' }}>
                <Button size="sm" variant="ghost" icon="globe" type="button">Open on GitHub</Button>
              </a>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// RecentCommitsPanel — last N commits via /git/recent.
// Only renders when item.is_repo. Silently shows nothing on error so
// the Overview tab doesn't get noisy for fresh repos.
// ─────────────────────────────────────────────────────────────
function RecentCommitsPanel({ apiBase, item, limit = 5 }) {
  const [data, setData] = _loxUseState(null);
  const [loading, setLoading] = _loxUseState(true);
  const [err, setErr] = _loxUseState(null);
  const isRepo = !!(item && item.is_repo);

  _loxUseEffect(() => {
    if (!isRepo) { setData(null); setLoading(false); return undefined; }
    let cancelled = false;
    setLoading(true); setErr(null);
    fetch(apiUrl(`${apiBase}/git/recent?limit=${encodeURIComponent(limit)}`))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`))))
      .then((d) => { if (!cancelled) setData(d || null); })
      .catch((e) => { if (!cancelled) setErr(e.message || 'failed'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [apiBase, isRepo, limit]);

  if (!isRepo) return null;

  const commits = (data && Array.isArray(data.commits)) ? data.commits : [];

  return (
    <Card padding={0}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderBottom: '1px solid var(--hairline)' }}>
        <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>Recent commits</span>
      </div>
      {loading && (
        <div className="mono" style={{ padding: 12, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>loading…</div>
      )}
      {!loading && err && (
        <div className="mono" style={{ padding: 12, fontSize: 11.5, color: 'var(--fg-4)' }}>(no recent commits)</div>
      )}
      {!loading && !err && commits.length === 0 && (
        <div className="mono" style={{ padding: 12, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(no commits yet)</div>
      )}
      {!loading && !err && commits.length > 0 && (
        <ul style={{ listStyle: 'none', margin: 0, padding: '4px 0' }}>
          {commits.map((c, i) => (
            <li key={(c.sha || i) + '@' + i} style={{
              display: 'flex', alignItems: 'baseline', gap: 8,
              padding: '6px 14px',
              borderBottom: i === commits.length - 1 ? 'none' : '1px solid var(--hairline)',
            }}>
              <code className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
                {c.sha ? c.sha.slice(0, 7) : '—'}
              </code>
              <span className="mono" style={{
                fontSize: 'var(--t-cap)', color: 'var(--fg-2)',
                flex: 1, minWidth: 0,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>{c.subject || ''}</span>
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)', flexShrink: 0 }}>
                {c.author || ''}
                {c.ts ? (' · ' + ((typeof window.relTime === 'function') ? window.relTime(c.ts) : '')) : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

Object.assign(window, { LinkedItemsPanel, OverviewGitPanel, RecentCommitsPanel });
