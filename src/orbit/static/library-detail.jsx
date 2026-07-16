// library-detail.jsx — fullscreen detail view for an Area or Project.
//
// Tabs (Phase 3): Overview · Files · Branches · PRs · Issues
//   - Overview: description (from main files), stats, link picker
//   - Files:    LibraryFileTree + inline file preview pane (with edit for
//               whitelisted files via LibraryMdEditor)
//   - Branches: git status + branch list, switch / create+checkout
//   - PRs:      list + detail of GitHub pull requests (LibraryPRList)
//   - Issues:   list + detail + create dialog for GitHub issues (LibraryIssueList)
// PRs/Issues panels surface a friendly inline error when the repo has no
// github remote / gh CLI is not authorised (424 from backend).
//
// Mounted INSIDE the parent content slot (BottomSheet on compact, <main>
// content area on desktop) so the app's header / sidebar / mobile tab bar
// remain visible. Earlier this used `position: fixed; inset: 0` —
// orchestrator-conversation's ConversationModal. ESC closes (calls onBack).
//
// Endpoints used (directly + via child panels):
//   GET    /api/library/{kind}/{name}/main             — bundle of canonical docs
//   GET    /api/library/{kind}/{name}/file?rel=...     — { content, sha256, mtime, size }
//   PUT    /api/library/{kind}/{name}/file             — { rel, content, expected_sha256? }
//   DELETE /api/library/{kind}/{name}/file?rel=...     — delete a file from disk
//   GET    /api/library/{kind}/{name}/file/download?rel=...  — file download (FileResponse)
//   PATCH  /api/library/{kind}/{name}                  — { new_name?, description? }
//   DELETE /api/library/{kind}/{name}                  — soft-delete to _archive/
//   GET    /api/library/{kind}/{name}/git              — git status
//   POST   /api/library/{kind}/{name}/git/checkout     — { branch, create?, force? }

const { useState: _ldUseState, useEffect: _ldUseEffect, useMemo: _ldUseMemo, useCallback: _ldUseCallback } = React;

const _LD_WRITE_WHITELIST = new Set(['INDEX.md', 'README.md', 'AGENTS.md', 'CLAUDE.md', '.gitignore']);

function _ldBasename(rel) {
  if (!rel) return '';
  const parts = String(rel).split('/');
  return parts[parts.length - 1] || '';
}

function _ldIsWritable(rel) {
  return _LD_WRITE_WHITELIST.has(_ldBasename(rel));
}

// `lib_id` is the relative path under ~/Areas/ or ~/Projects/. Areas have
// `lib_id === name` (single-level). Projects may be nested
// (e.g. "parent/child"). FastAPI's {name:path} accepts both raw
// '/' and '%2F'-encoded slashes; we keep raw '/' but encode each segment
// so spaces / unicode are safe.
function _ldEncodeLibId(libId) {
  return String(libId || '').split('/').map(encodeURIComponent).join('/');
}

function _ldGetLibId(item) {
  return (item && (item.lib_id || item.name)) || '';
}

function _ldFirstParagraph(md) {
  if (!md || typeof md !== 'string') return '';
  // Strip leading h1, then take everything until the first blank line.
  const cleaned = md.replace(/^#\s+.*$/m, '').trim();
  const para = cleaned.split(/\n\s*\n/)[0] || '';
  return para.trim();
}

function _ldExtractDescription(mainBundle) {
  if (!mainBundle || typeof mainBundle !== 'object') return '';
  // INDEX.md wins, then README.md.
  for (const key of ['INDEX.md', 'README.md']) {
    const f = mainBundle[key];
    if (f && f.exists && typeof f.content === 'string' && f.content.trim()) {
      const para = _ldFirstParagraph(f.content);
      if (para) return para;
    }
  }
  return '';
}

function _ldRenderMarkdown(text) {
  if (!text || !window.marked || typeof window.marked.parse !== 'function') return null;
  const raw = window.marked.parse(text);
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    return window.DOMPurify.sanitize(raw);
  }
  return null;
}

function LibraryDetail({ kind, item, compact, tab: tabProp, onChangeTab, openFile, onOpenFile, onBack }) {
  // Tab state is owned by the URL when `tabProp`/`onChangeTab` are wired
  // (current behaviour from app.jsx). Falls back to local state for any
  // legacy mounter that doesn't pass them.
  const [tabLocal, setTabLocal] = _ldUseState(tabProp || 'overview');
  _ldUseEffect(() => {
    if (typeof tabProp === 'string' && tabProp !== tabLocal) setTabLocal(tabProp);
  }, [tabProp]);
  const tab = typeof tabProp === 'string' ? tabProp : tabLocal;
  const setTab = (next) => {
    if (typeof onChangeTab === 'function') onChangeTab(next);
    else setTabLocal(next);
  };
  const [mainFiles, setMainFiles] = _ldUseState(null);
  const [mainLoading, setMainLoading] = _ldUseState(true);
  const [mainError, setMainError] = _ldUseState(null);
  const [openedFile, setOpenedFile] = _ldUseState(null); // { rel, content, mtime, size, sha256, loading, error }
  const [editorOpen, setEditorOpen] = _ldUseState(null); // null | { rel, content, sha256 }
  const [renameOpen, setRenameOpen] = _ldUseState(false);
  // Shared git status — lifted out of LibraryBranchPanel so the header chip
  // and the Branches tab share a single fetch.
  const [gitState, setGitState] = _ldUseState(null);
  const [gitLoading, setGitLoading] = _ldUseState(false);
  // Local mirror of the item so link-picker edits reflect immediately.
  const [localItem, setLocalItem] = _ldUseState(item);
  _ldUseEffect(() => { setLocalItem(item); }, [item]);

  const hub = useHubData();
  const candidates = kind === 'area' ? (hub && hub.projects) || [] : (hub && hub.areas) || [];
  const linkedNames = kind === 'area'
    ? (localItem && localItem.linked_projects) || []
    : (localItem && localItem.linked_areas) || [];

  const libId = _ldGetLibId(localItem);
  const encodedLibId = _ldEncodeLibId(libId);
  const kindPath = kind === 'area' ? 'areas' : 'projects';
  const baseUrl = `/api/library/${kindPath}/${encodedLibId}`;
  const isRepo = !!localItem.is_repo;
  const hasGithub = !!localItem.has_github_remote;
  // issues.create_repo flag (stamped per-item by _apply_overrides). When the
  // project/area has no remote yet, lets the Issues tab offer a one-click
  // "Create GitHub repo" CTA instead of being hidden.
  const canCreateRepo = (kind === 'area' || kind === 'project') && !hasGithub && !!localItem.issues_create_repo;

  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  // Fetch the main-files bundle once on mount (or when the item changes).
  _ldUseEffect(() => {
    let cancelled = false;
    setMainLoading(true);
    setMainError(null);
    setMainFiles(null);
    fetch(apiUrl(`${baseUrl}/main`))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`))))
      .then((data) => {
        if (cancelled) return;
        // Backend returns {kind, name, main_filename, files: {…}}; flatten so
        // mainFiles[rel] resolves directly without per-file refetches.
        setMainFiles((data && data.files) || data || {});
      })
      .catch((err) => {
        if (cancelled) return;
        setMainError(err.message || 'failed to load');
      })
      .finally(() => { if (!cancelled) setMainLoading(false); });
    return () => { cancelled = true; };
  }, [baseUrl]);

  // ESC closes (mirrors ConversationModal). The MdEditor handles its own ESC
  // (with a "discard?" prompt if dirty), so we only react when no editor is
  // open — preventing double-handling and accidental detail dismissal.
  _ldUseEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      if (editorOpen) return; // editor owns ESC while open
      if (openedFile) {
        setOpenedFile(null);
        if (typeof onOpenFile === 'function') onOpenFile(null);
      } else {
        onBack && onBack();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onBack, openedFile, editorOpen, onOpenFile]);

  const description = _ldUseMemo(() => _ldExtractDescription(mainFiles), [mainFiles]);

  // Fetch git status once (when we know the item is a repo) so the header
  // chip + Branches tab can share the same data without double-fetching.
  const reloadGit = _ldUseCallback(async () => {
    if (!isRepo) { setGitState(null); return; }
    setGitLoading(true);
    try {
      const r = await fetch(apiUrl(`${baseUrl}/git`));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const data = await r.json();
      setGitState(data || null);
    } catch (e) {
      // Silently degrade — header just hides the branch chip on failure.
      setGitState({ _error: e.message || 'failed' });
    } finally {
      setGitLoading(false);
    }
  }, [baseUrl, isRepo]);

  _ldUseEffect(() => {
    let cancelled = false;
    if (!isRepo) { setGitState(null); return undefined; }
    setGitLoading(true);
    fetch(apiUrl(`${baseUrl}/git`))
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`))))
      .then((data) => { if (!cancelled) setGitState(data || null); })
      .catch((e) => { if (!cancelled) setGitState({ _error: e.message || 'failed' }); })
      .finally(() => { if (!cancelled) setGitLoading(false); });
    return () => { cancelled = true; };
  }, [baseUrl, isRepo]);

  const onTreeOpen = _ldUseCallback(async (rel) => {
    if (!rel) return;
    setOpenedFile({ rel, loading: true, error: null, content: null });
    // Reflect the open file in the URL immediately (deep-link / restore).
    if (typeof onOpenFile === 'function') onOpenFile(rel);
    try {
      const r = await fetch(apiUrl(`${baseUrl}/file?rel=${encodeURIComponent(rel)}`));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const data = await r.json();
      setOpenedFile({
        rel,
        loading: false,
        error: null,
        content: typeof data.content === 'string' ? data.content : '',
        sha256: data.sha256 || null,
        size: data.size,
        mtime: data.mtime,
      });
    } catch (err) {
      setOpenedFile({ rel, loading: false, error: err.message || 'failed', content: null });
    }
  }, [baseUrl, onOpenFile]);

  // Files-tab deep-link reconciliation (URL-driven mode only). The URL's
  // open-file segment is the source of truth: open it when it appears or
  // changes, and drop the preview when it's cleared. Guarded against the
  // open/close handlers (which already set both state AND the URL) by
  // comparing to the currently-open file, so this never loops. `openedFile`
  // is intentionally NOT a dep — we react to URL (`openFile`) + `tab` only.
  _ldUseEffect(() => {
    if (typeof onOpenFile !== 'function') return;  // legacy mounters: local state only
    if (tab !== 'files') return;
    const wanted = openFile || null;
    const current = (openedFile && openedFile.rel) || null;
    if (wanted === current) return;
    if (wanted) onTreeOpen(wanted);
    else setOpenedFile(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, openFile, onOpenFile]);

  const onEditFile = _ldUseCallback(() => {
    if (!openedFile || openedFile.loading || openedFile.error) return;
    if (!_ldIsWritable(openedFile.rel)) return;
    setEditorOpen({
      rel: openedFile.rel,
      content: openedFile.content || '',
      sha256: openedFile.sha256 || null,
    });
  }, [openedFile]);

  const onEditorSaved = _ldUseCallback((rel, response) => {
    // Refresh the underlying preview so after closing the editor the user
    // sees the freshly saved content (and the new sha256).
    setOpenedFile((prev) => {
      if (!prev || prev.rel !== rel) return prev;
      return {
        ...prev,
        content: (editorOpen && editorOpen.rel === rel) ? editorOpen.content : prev.content,
        sha256: (response && response.sha256) || prev.sha256,
      };
    });
    // Re-fetch main bundle if INDEX.md / README.md was edited (description).
    if (_ldBasename(rel) === 'INDEX.md' || _ldBasename(rel) === 'README.md') {
      window.dispatchEvent(new CustomEvent('library:reload'));
    }
  }, [editorOpen]);

  // Inline saves from Overview / Gitignore panels — patch the cached main
  // bundle so the description / panels stay in sync without a refetch.
  const onInlineFileSaved = _ldUseCallback((rel, response) => {
    setMainFiles((prev) => {
      if (!prev) return prev;
      const next = { ...prev };
      const prior = next[rel] || {};
      next[rel] = {
        ...prior,
        exists: true,
        content: (response && typeof response.content === 'string')
          ? response.content
          : (prior.content || ''),
        sha256: (response && response.sha256) || prior.sha256 || null,
        mtime: (response && response.mtime) || prior.mtime,
        size: (response && response.size) ?? prior.size,
      };
      return next;
    });
    if (_ldBasename(rel) === 'INDEX.md' || _ldBasename(rel) === 'README.md') {
      window.dispatchEvent(new CustomEvent('library:reload'));
    }
  }, []);

  const onArchive = async () => {
    if (!confirm('Move to archive?')) return;
    try {
      const r = await fetch(apiUrl(baseUrl), { method: 'DELETE' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) {
        toast && toast(`Archive failed: ${(d && d.error) || r.status}`, 'err');
        return;
      }
      toast && toast('Archived', 'ok');
      window.dispatchEvent(new CustomEvent('library:reload'));
      onBack && onBack();
    } catch (e) {
      toast && toast('Archive failed', 'err');
    }
  };

  const onRename = async (newName) => {
    if (!newName || newName === localItem.name) {
      setRenameOpen(false);
      return;
    }
    try {
      const r = await fetch(apiUrl(baseUrl), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) {
        toast && toast(`Rename failed: ${(d && d.error) || r.status}`, 'err');
        return;
      }
      toast && toast('Renamed', 'ok');
      setLocalItem((it) => ({ ...it, name: newName, label: newName }));
      window.dispatchEvent(new CustomEvent('library:reload'));
      setRenameOpen(false);
      // Closing detail is the safest move — its baseUrl is now stale.
      onBack && onBack();
    } catch (e) {
      toast && toast('Rename failed', 'err');
    }
  };

  const onLinksChange = (nextLinked) => {
    setLocalItem((it) => {
      if (kind === 'area') return { ...it, linked_projects: nextLinked };
      return { ...it, linked_areas: nextLinked };
    });
  };

  // Patch a slice of localItem in place — used by OverviewGitPanel after
  // a successful `git init` / `attach-remote` so the tabs unlock without
  // waiting for /api/data to refresh.
  const onItemChanged = _ldUseCallback((patch) => {
    if (!patch || typeof patch !== 'object') return;
    setLocalItem((it) => ({ ...it, ...patch }));
  }, []);

  // ── File preview helpers (download + delete) ─────────────────
  const onDownloadFile = _ldUseCallback(() => {
    if (!openedFile || openedFile.loading || openedFile.error || !openedFile.rel) return;
    const url = apiUrl(`${baseUrl}/file/download?rel=${encodeURIComponent(openedFile.rel)}`);
    try { window.open(url, '_blank', 'noopener'); } catch (_) { /* ignore */ }
  }, [baseUrl, openedFile]);

  const onDeleteFile = _ldUseCallback(async () => {
    if (!openedFile || openedFile.loading || openedFile.error || !openedFile.rel) return;
    const rel = openedFile.rel;
    if (!window.confirm(`Delete ${rel}?\n\nThis cannot be undone.`)) return;
    try {
      const r = await fetch(apiUrl(`${baseUrl}/file?rel=${encodeURIComponent(rel)}`), {
        method: 'DELETE',
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || (d && d.ok === false)) {
        const msg = (d && (d.error || d.detail)) || `http ${r.status}`;
        toast && toast(`Delete failed: ${String(msg).slice(0, 120)}`, 'err');
        return;
      }
      toast && toast('Deleted', 'ok');
      // Drop the preview pane and refresh the tree by bumping a key so
      // descendants remount + refetch.
      setOpenedFile(null);
      if (typeof onOpenFile === 'function') onOpenFile(null);
      // Tree refresh: dispatch library:reload so any listeners refresh.
      window.dispatchEvent(new CustomEvent('library:reload'));
    } catch (e) {
      toast && toast('Delete failed', 'err');
    }
  }, [baseUrl, openedFile, toast, onOpenFile]);

  // Tab visibility is gated by repo / github status:
  //   - Branches: requires `is_repo`
  //   - PRs / Issues: require `has_github_remote` (and gh CLI auth, which
  //     is reported by backend at fetch time as 424; tab still shows but
  //     panel surfaces the inline error).
  // Gitignore is always available (users may seed .gitignore before
  // `git init`).
  const tabs = _ldUseMemo(() => {
    const out = [
      { k: 'overview',  l: 'Overview' },
      { k: 'agent',     l: 'Agent' },
    ];
    // Gallery + Env/secrets tabs are gated to Areas/Projects (Resources
    // excluded — they have no agent dir). Inserted after Agent so the
    // authoring/output tools sit together, before the repo tabs. The gallery
    // is agent-scoped: every session of this project/area writes into the same
    // <cwd>/.artifacts/, so it aggregates artifacts across ALL the agent's
    // sessions in one place.
    if (kind === 'area' || kind === 'project') {
      out.push({ k: 'gallery', l: 'Galeria' });
      out.push({ k: 'secrets', l: 'Credentials' });
    }
    out.push({ k: 'files',     l: 'Files' });
    out.push({ k: 'gitignore', l: 'Gitignore' });
    if (isRepo) out.push({ k: 'branches', l: 'Branches' });
    if (hasGithub) {
      out.push({ k: 'prs',    l: 'PRs' });
      out.push({ k: 'issues', l: 'Issues' });
    } else if (canCreateRepo) {
      out.push({ k: 'issues', l: 'Issues' });
    }
    return out;
  }, [kind, isRepo, hasGithub, canCreateRepo]);

  // Defensive: if the active tab vanished (e.g. flags changed mid-session
  // because /api/data refreshed), drop back to Overview.
  _ldUseEffect(() => {
    if (!tabs.some((t) => t.k === tab)) setTab('overview');
  }, [tabs, tab]);

  const titleText = localItem.label || localItem.name;
  const subText = localItem.rel_path
    || (kind === 'area' ? `~/Areas/${libId}/` : `~/Projects/${libId}/`);
  const branchText = isRepo
    ? (gitLoading
        ? '…'
        : ((gitState && !gitState._error && gitState.branch) || ''))
    : '';

  const kebabItems = [
    { icon: 'pencil', label: 'Rename', onClick: () => setRenameOpen(true) },
    { icon: 'archive', label: 'Archive', onClick: onArchive, danger: true },
  ];

  return (
    <div
      role="region"
      aria-label={`${kind} ${titleText}`}
      style={{
        // Fit into the parent content slot (BottomSheet on compact, <main>
        // content area on desktop) instead of covering the viewport. Without
        // this, the app header / sidebar / mobile tab bar disappeared because
        // the previous `position: fixed; inset: 0; z-index: 200` overlay
        // sat on top of them.
        background: 'var(--bg)',
        display: 'flex', flexDirection: 'column',
        height: '100%', minHeight: 0,
      }}
    >
      {/* Header — shown in full on compact too: the mobile topbar now renders
          the drawer HAMBURGER (no back/title), so this header owns the back
          arrow + kind eyebrow + title + branch chip + subText + kebab, matching
          the Global meta-area detail. */}
      <div style={{
        flexShrink: 0,
        padding: compact ? '10px 16px' : '14px 18px',
        borderBottom: '1px solid var(--hairline)',
        display: 'flex', alignItems: 'center', gap: 12,
      }}>
        <IconButton icon="chevron-l" label="Back" size={34} onClick={onBack} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
            {kind === 'area' ? 'Area' : 'Project'}
          </div>
          <div style={{
            display: 'flex', alignItems: 'baseline', gap: 8,
            overflow: 'hidden',
          }}>
            <div style={{
              fontSize: 'var(--t-h2)', fontWeight: 500, letterSpacing: '-0.01em',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              minWidth: 0,
            }}>{titleText}</div>
            {isRepo && branchText && (
              <code className="mono" style={{
                flexShrink: 0,
                fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
                padding: '2px 8px', borderRadius: 'var(--r-sm)',
                background: 'var(--surface-2)', border: '1px solid var(--hairline)',
                maxWidth: compact ? 110 : 220,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }} title={`branch: ${branchText}`}>{branchText}</code>
            )}
          </div>
          {subText && (
            <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{subText}</div>
          )}
        </div>
        <KebabMenu items={kebabItems} />
      </div>

      {/* Tabs */}
      <div style={{
        flexShrink: 0,
        display: 'flex', gap: 4,
        padding: '0 18px',
        borderBottom: '1px solid var(--hairline)',
        overflowX: 'auto',
      }} className="scroll-hide">
        {tabs.map((t) => (
          <button
            key={t.k}
            onClick={() => setTab(t.k)}
            style={{
              padding: '10px 14px', background: 'transparent', border: 'none', cursor: 'pointer',
              color: tab === t.k ? 'var(--fg)' : 'var(--fg-3)',
              fontSize: 'var(--t-sm)', fontWeight: 500, fontFamily: 'inherit',
              borderBottom: '2px solid ' + (tab === t.k ? 'var(--accent)' : 'transparent'),
              marginBottom: -1,
            }}
          >
            {t.l}
          </button>
        ))}
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }} className="scroll-hide">
        {tab === 'overview' && (
          window.LibraryOverviewPanel
            ? <window.LibraryOverviewPanel
                kind={kind}
                item={localItem}
                apiBase={baseUrl}
                description={description}
                mainFiles={mainFiles}
                mainLoading={mainLoading}
                mainError={mainError}
                candidates={candidates}
                linkedNames={linkedNames}
                onLinksChange={onLinksChange}
                onFileSaved={onInlineFileSaved}
                onItemChanged={onItemChanged}
                gitState={gitState}
                gitLoading={gitLoading}
                onReloadGit={reloadGit}
                compact={compact}
              />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(overview panel unavailable)</div>
        )}
        {tab === 'agent' && (
          window.LibraryAgentPanel
            ? <window.LibraryAgentPanel
                kind={kind}
                item={localItem}
                libId={libId}
                compact={compact}
                onChangeTab={onChangeTab}
              />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(agent panel unavailable)</div>
        )}
        {tab === 'gallery' && (
          window.ArtifactGallery
            ? <window.ArtifactGallery scope="agent" libId={`${kindPath}/${libId}`} compact={compact} />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(gallery unavailable)</div>
        )}
        {tab === 'secrets' && (
          window.LibrarySecretsPanel
            ? <window.LibrarySecretsPanel
                kind={kind}
                libId={libId}
                item={localItem}
                compact={compact}
              />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(secrets panel unavailable)</div>
        )}
        {tab === 'files' && (
          <_LdFiles
            kind={kindPath}
            libId={libId}
            compact={compact}
            openedFile={openedFile}
            onTreeOpen={onTreeOpen}
            onCloseFile={() => { setOpenedFile(null); if (typeof onOpenFile === 'function') onOpenFile(null); }}
            onEditFile={onEditFile}
            onDownloadFile={onDownloadFile}
            onDeleteFile={onDeleteFile}
          />
        )}
        {tab === 'gitignore' && (
          window.LibraryGitignoreTab
            ? <window.LibraryGitignoreTab
                apiBase={baseUrl}
                mainFiles={mainFiles}
                onFileSaved={onInlineFileSaved}
                compact={compact}
              />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(gitignore panel unavailable)</div>
        )}
        {tab === 'branches' && (
          window.LibraryBranchPanel
            ? <window.LibraryBranchPanel
                apiBase={baseUrl}
                git={gitState}
                loading={gitLoading}
                onReload={reloadGit}
                hasGithub={hasGithub}
                defaultBranch={(gitState && gitState.default_branch) || localItem.default_branch || 'main'}
                compact={compact}
              />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(branches panel unavailable)</div>
        )}
        {tab === 'prs' && (
          window.LibraryPRList
            ? <window.LibraryPRList kind={kindPath} libId={libId} />
            : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(PR panel unavailable)</div>
        )}
        {tab === 'issues' && (
          hasGithub
            ? (window.LibraryIssueList
                ? <window.LibraryIssueList kind={kindPath} libId={libId} />
                : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(Issue panel unavailable)</div>)
            : (window.LibraryCreateRepoCTA
                ? <window.LibraryCreateRepoCTA kind={kindPath} libId={libId} />
                : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(Create-repo unavailable)</div>)
        )}
      </div>

      {renameOpen && (
        <_LdRenamePrompt
          initial={localItem.name}
          onCancel={() => setRenameOpen(false)}
          onSubmit={onRename}
        />
      )}

      {editorOpen && window.LibraryMdEditor && (
        <window.LibraryMdEditor
          kind={kind}
          name={localItem.name}
          libId={libId}
          rel={editorOpen.rel}
          initialContent={editorOpen.content}
          baselineSha={editorOpen.sha256}
          onSaved={onEditorSaved}
          onClose={() => setEditorOpen(null)}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Files tab — tree + preview pane (split on desktop, stack on compact)
// (Overview is rendered by window.LibraryOverviewPanel — see
//  library-overview-panels.jsx)
// ─────────────────────────────────────────────────────────────
function _LdFiles({ kind, libId, compact, openedFile, onTreeOpen, onCloseFile, onEditFile, onDownloadFile, onDeleteFile }) {
  const ready = !!(openedFile && !openedFile.loading && !openedFile.error);
  const canEdit = ready && _ldIsWritable(openedFile.rel);
  // Bump on library:reload so the file tree remounts + refetches after a
  // delete (the tree is otherwise stable so it would keep showing the
  // freshly-deleted file until the user navigated away).
  const [treeKey, setTreeKey] = _ldUseState(0);
  _ldUseEffect(() => {
    const onReload = () => setTreeKey((k) => k + 1);
    window.addEventListener('library:reload', onReload);
    return () => window.removeEventListener('library:reload', onReload);
  }, []);
  return (
    <div style={{
      padding: compact ? 16 : 24,
      paddingBottom: compact ? 100 : 24,
      display: compact ? 'flex' : 'grid',
      flexDirection: 'column',
      gap: 14,
      gridTemplateColumns: compact ? undefined : '320px 1fr',
      minHeight: 0,
    }}>
      <Card padding={0} style={compact ? {} : { alignSelf: 'start', position: 'sticky', top: 0 }}>
        <SubHeader title="Files" sub={`~/${kind === 'areas' ? 'Areas' : 'Projects'}/${libId}/`} />
        <div style={{ padding: '8px 6px', maxHeight: compact ? 320 : '70vh', overflowY: 'auto' }} className="scroll-hide">
          {window.LibraryFileTree
            ? <window.LibraryFileTree key={treeKey} kind={kind} libId={libId} onOpen={onTreeOpen} />
            : <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(tree unavailable)</div>}
        </div>
      </Card>

      <Card padding={0}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 14px', borderBottom: '1px solid var(--hairline)', flexWrap: 'wrap' }}>
          <Icon name="file" size={14} color="var(--fg-3)"/>
          <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-2)', flex: 1, minWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {openedFile ? openedFile.rel : 'pick a file…'}
          </span>
          {canEdit && (
            <Button size="sm" variant="ghost" icon="pencil" onClick={onEditFile} type="button">Edit</Button>
          )}
          {ready && (
            <Button size="sm" variant="ghost" icon="download" onClick={onDownloadFile} type="button">Download</Button>
          )}
          {ready && (
            <Button size="sm" variant="ghost" icon="trash" onClick={onDeleteFile} type="button">Delete</Button>
          )}
          {openedFile && (
            <IconButton icon="close" label="Close" size={28} onClick={onCloseFile} />
          )}
        </div>
        <_LdFileBody openedFile={openedFile} />
      </Card>
    </div>
  );
}

function _LdFileBody({ openedFile }) {
  if (!openedFile) {
    return (
      <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
        Select a file from the tree to preview its contents.
      </div>
    );
  }
  if (openedFile.loading) {
    return <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>loading…</div>;
  }
  if (openedFile.error) {
    return <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--err)' }}>error: {openedFile.error}</div>;
  }
  const isMarkdown = /\.(md|markdown)$/i.test(openedFile.rel || '');
  if (isMarkdown) {
    const safe = _ldRenderMarkdown(openedFile.content || '');
    if (safe != null) {
      return (
        <div className="md-render" style={{ padding: '18px 22px', fontSize: 'var(--t-sm)', lineHeight: 1.6, color: 'var(--fg)' }}
             dangerouslySetInnerHTML={{ __html: safe }} />
      );
    }
  }
  return (
    <pre className="mono" style={{
      margin: 0, padding: '18px 22px',
      fontSize: 'var(--t-cap)', lineHeight: 1.7, color: 'var(--fg)',
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      maxHeight: '70vh', overflowY: 'auto',
    }}>
      {openedFile.content || ''}
    </pre>
  );
}

// ─────────────────────────────────────────────────────────────
// Tiny inline rename prompt (modal)
// ─────────────────────────────────────────────────────────────
function _LdRenamePrompt({ initial, onCancel, onSubmit }) {
  const [value, setValue] = _ldUseState(initial || '');
  return (
    <Modal open={true} onClose={onCancel} title="Rename" width={420}>
      <form
        onSubmit={(e) => { e.preventDefault(); onSubmit(value.trim()); }}
        style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}
      >
        <Input
          autoFocus
          value={value}
          onChange={setValue}
          style={{ width: '100%', boxSizing: 'border-box' }}
          inputStyle={{ fontSize: 13 }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="quiet" onClick={onCancel} type="button">Cancel</Button>
          <Button variant="primary" type="submit" icon="check">Rename</Button>
        </div>
      </form>
    </Modal>
  );
}

// LibraryBranchPanel lives in library-overview-panels.jsx (alongside the
// other tab body components). Re-exported on window for backwards compat
// by that file.
Object.assign(window, { LibraryDetail });
