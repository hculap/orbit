// library-overview-panels.jsx — Overview tab body + Gitignore tab body,
// extracted to keep library-detail.jsx under the 800 LOC project ceiling.
//
// Exposes (via window):
//   - LibraryOverviewPanel  — renders Description / Stats / Linked /
//                             AGENTS.md / CLAUDE.md / README.md / INDEX.md
//                             quick-edit panels for the Overview tab.
//   - LibraryGitignoreTab   — dedicated Gitignore tab content.
//   - LibraryFilePanel      — small reusable inline file viewer/editor used
//                             by both Overview panels and Gitignore tab.
//   - LinkedItemsPanel      — chips of currently-linked items with a
//                             "+ Link new" popover that mounts the existing
//                             LibraryLinkPicker. Replaces "always show full
//                             picker" Overview behaviour.
//   - OverviewGitPanel      — branches on item.is_repo / has_github_remote:
//                               !is_repo            → Init + Attach remote
//                               is_repo, no github  → Attach remote
//                               is_repo + github    → Status + Open on GitHub
//   - RecentCommitsPanel    — last 5 commits via /git/recent.
//
// Endpoints (all relative to the parent's `apiBase`):
//   GET  /file?rel=...                — { content, sha256, mtime, size }
//   PUT  /file  body { rel, content, expected_sha256? }
//   POST /git/init
//   POST /git/attach-remote  body: { url, fetch? }
//   GET  /git/recent?limit=5          — { ok, commits: [...] }
//
// Markdown is rendered with marked + DOMPurify (already loaded in index.html).
// The whitelist below MUST mirror the backend's WRITABLE_BASENAMES.

const { useState: _lopUseState, useEffect: _lopUseEffect, useMemo: _lopUseMemo, useCallback: _lopUseCallback } = React;

const _LOP_WRITE_WHITELIST = new Set(['INDEX.md', 'README.md', 'AGENTS.md', 'CLAUDE.md', '.gitignore']);

const _LOP_GITIGNORE_DEFAULT = `.DS_Store
*.swp
node_modules/
.venv/
__pycache__/
`;

function _lopRenderMarkdown(text) {
  if (!text || typeof text !== 'string') return '';
  if (!window.marked || typeof window.marked.parse !== 'function') return '';
  const raw = window.marked.parse(text);
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    return window.DOMPurify.sanitize(raw);
  }
  return '';
}

function _lopShortSha(sha) {
  if (!sha || typeof sha !== 'string') return '';
  return sha.slice(0, 7);
}

// ─────────────────────────────────────────────────────────────
// LibraryFilePanel — single-file inline viewer/editor.
// Used by Overview (per canonical doc) and the Gitignore tab.
// ─────────────────────────────────────────────────────────────
function LibraryFilePanel({ apiBase, rel, initialBundle, isMarkdown, defaultContent, onSaved }) {
  // initialBundle = mainFiles[rel] from the bundle fetch — { exists, content,
  // sha256, mtime, size }. May be missing if not in canonical bundle (e.g.
  // .gitignore — we fall back to fetching /file directly).
  const seed = initialBundle || null;
  const [exists, setExists] = _lopUseState(seed ? !!seed.exists : null);
  const [content, setContent] = _lopUseState(seed && seed.exists ? (seed.content || '') : '');
  const [sha, setSha] = _lopUseState(seed && seed.exists ? (seed.sha256 || null) : null);
  const [editing, setEditing] = _lopUseState(false);
  const [draft, setDraft] = _lopUseState('');
  const [busy, setBusy] = _lopUseState(false);
  const [err, setErr] = _lopUseState(null);
  const [loadingExtra, setLoadingExtra] = _lopUseState(false);

  const writable = _LOP_WRITE_WHITELIST.has(rel);
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  // If not in the canonical bundle (e.g. .gitignore on Overview), pull directly
  // so we know whether the file exists. Bundle fetches happen in the parent
  // and only cover the canonical doc set.
  _lopUseEffect(() => {
    if (initialBundle !== undefined) return; // bundle answer is authoritative
    if (!apiBase || !rel) return;
    let cancelled = false;
    setLoadingExtra(true);
    (async () => {
      try {
        const r = await fetch(apiUrl(`${apiBase}/file?rel=${encodeURIComponent(rel)}`));
        if (cancelled) return;
        if (r.status === 404) {
          setExists(false); setContent(''); setSha(null);
        } else if (!r.ok) {
          throw new Error(`http ${r.status}`);
        } else {
          const data = await r.json();
          setExists(true);
          setContent(typeof data.content === 'string' ? data.content : '');
          setSha(data.sha256 || null);
        }
        setErr(null);
      } catch (e) {
        if (cancelled) return;
        setErr(e.message || 'failed to load');
      } finally {
        if (!cancelled) setLoadingExtra(false);
      }
    })();
    return () => { cancelled = true; };
  }, [apiBase, rel, initialBundle]);

  const startEdit = _lopUseCallback(() => {
    setDraft(content || '');
    setEditing(true);
    setErr(null);
  }, [content]);

  const cancelEdit = _lopUseCallback(() => {
    setEditing(false);
    setDraft('');
    setErr(null);
  }, []);

  const save = _lopUseCallback(async (nextContent, expectedSha) => {
    if (busy) return;
    setBusy(true); setErr(null);
    try {
      const body = { rel, content: nextContent };
      if (expectedSha) body.expected_sha256 = expectedSha;
      const r = await fetch(apiUrl(`${apiBase}/file`), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.status === 409) {
        const detail = await r.json().catch(() => ({}));
        const reload = window.confirm(
          'Plik został zmieniony na dysku.\n\nOK – pobierz wersję z dysku (lokalne zmiany zostaną odrzucone).\nAnuluj – kontynuuj edycję (kolejny zapis użyje nowej bazy).',
        );
        if (reload) {
          const fr = await fetch(apiUrl(`${apiBase}/file?rel=${encodeURIComponent(rel)}`));
          const fd = await fr.json().catch(() => ({}));
          setContent(typeof fd.content === 'string' ? fd.content : '');
          setSha(fd.sha256 || null);
          setExists(true);
          cancelEdit();
        } else if (detail && detail.sha256) {
          setSha(detail.sha256);
        }
        return;
      }
      if (r.status === 403) throw new Error('Plik nie jest na liście dozwolonych do zapisu.');
      if (r.status === 413) throw new Error('Plik za duży (limit 1 MB).');
      if (!r.ok) {
        let msg = `http ${r.status}`;
        try {
          const d = await r.json();
          if (d && (d.error || d.detail)) msg = String(d.error || d.detail).slice(0, 200);
        } catch (_) { /* ignore */ }
        throw new Error(msg);
      }
      const data = await r.json().catch(() => ({}));
      setContent(nextContent);
      setSha(data.sha256 || null);
      setExists(true);
      setEditing(false);
      setDraft('');
      toast && toast(`Saved ${rel}`, 'ok');
      if (typeof onSaved === 'function') onSaved(rel, data);
    } catch (e) {
      setErr(e.message || 'save failed');
    } finally {
      setBusy(false);
    }
  }, [apiBase, rel, busy, cancelEdit, toast, onSaved]);

  const createWithDefault = _lopUseCallback(() => {
    if (!writable) return;
    save(typeof defaultContent === 'string' ? defaultContent : '', null);
  }, [save, writable, defaultContent]);

  const headerStyle = {
    display: 'flex', alignItems: 'center', gap: 8,
    padding: '10px 14px',
    borderBottom: '1px solid var(--hairline)',
  };

  const filenameSpan = (
    <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-2)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
      {rel}
    </span>
  );

  const shaChip = sha ? (
    <code className="mono" style={{
      fontSize: 10.5, color: 'var(--fg-4)',
      padding: '2px 6px', borderRadius: 4,
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    }}>{_lopShortSha(sha)}</code>
  ) : null;

  if (loadingExtra && exists === null) {
    return (
      <Card padding={0}>
        <div style={headerStyle}>
          <Icon name="file" size={13} color="var(--fg-3)" />
          {filenameSpan}
        </div>
        <div className="mono" style={{ padding: 14, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>loading…</div>
      </Card>
    );
  }

  // File doesn't exist on disk — offer a Create button (writable only).
  if (exists === false) {
    return (
      <Card padding={0}>
        <div style={headerStyle}>
          <Icon name="file" size={13} color="var(--fg-3)" />
          {filenameSpan}
        </div>
        <div style={{ padding: 14, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>Brak pliku</span>
          {writable && (
            <Button size="sm" variant="ghost" icon={busy ? 'spinner' : 'plus'}
                    onClick={createWithDefault} type="button">
              {busy ? 'Tworzę…' : 'Create'}
            </Button>
          )}
          {err && (
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--err)' }}>{err}</span>
          )}
        </div>
      </Card>
    );
  }

  return (
    <Card padding={0}>
      <div style={headerStyle}>
        <Icon name="file" size={13} color="var(--fg-3)" />
        {filenameSpan}
        {shaChip}
        {!editing && writable && (
          <Button size="sm" variant="ghost" icon="pencil" onClick={startEdit} type="button">Edit</Button>
        )}
        {editing && (
          <>
            <Button size="sm" variant="quiet" onClick={cancelEdit} type="button">Cancel</Button>
            <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'check'}
                    onClick={() => save(draft, sha)} type="button">
              {busy ? 'Saving…' : 'Save'}
            </Button>
          </>
        )}
      </div>

      {editing ? (
        <Textarea
          value={draft}
          onChange={setDraft}
          rows={Math.min(20, Math.max(6, (draft.match(/\n/g) || []).length + 2))}
          mono
          style={{ display: 'block', border: 'none', minHeight: 120, resize: 'vertical' }}
          inputStyle={{ fontSize: 12.5, lineHeight: 1.6 }}
        />
      ) : (
        _LopRenderBody({ rel, content, isMarkdown })
      )}

      {err && !editing && (
        <StatusBanner variant="err" label={err} inline />
      )}
    </Card>
  );
}

function _LopRenderBody({ rel, content, isMarkdown }) {
  if (!content) {
    return (
      <div className="mono" style={{ padding: '12px 14px', fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
        (pusty plik)
      </div>
    );
  }
  if (isMarkdown) {
    const safe = _lopRenderMarkdown(content);
    if (safe) {
      return (
        <div className="md-render"
             style={{ padding: '12px 18px', fontSize: 'var(--t-sm)', lineHeight: 1.6, color: 'var(--fg)' }}
             dangerouslySetInnerHTML={{ __html: safe }} />
      );
    }
  }
  return (
    <pre className="mono" style={{
      margin: 0, padding: '12px 14px',
      fontSize: 'var(--t-cap)', lineHeight: 1.7, color: 'var(--fg)',
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      maxHeight: '40vh', overflowY: 'auto',
    }}>{content}</pre>
  );
}

// LinkedItemsPanel / OverviewGitPanel / RecentCommitsPanel live in
// library-overview-extras.jsx — kept separate to keep this file under the
// 800 LOC project ceiling. They publish via Object.assign(window, …)
// before this file runs so the references below are stable at render-time.

// ─────────────────────────────────────────────────────────────
// LibraryOverviewPanel — Overview tab body
// ─────────────────────────────────────────────────────────────
function LibraryOverviewPanel({
  kind, item, apiBase, description, mainFiles, mainLoading, mainError,
  candidates, linkedNames, onLinksChange, onFileSaved, onItemChanged,
  gitState, gitLoading, onReloadGit, compact,
}) {
  const stats = kind === 'area'
    ? [
        { k: 'projects',  v: (item.links_projects ?? (item.linked_projects ? item.linked_projects.length : 0)) || 0 },
        { k: 'resources', v: item.links_resources ?? 0 },
        { k: 'notes',     v: item.notes ?? 0 },
      ]
    : [
        { k: 'areas', v: (item.linked_areas ? item.linked_areas.length : 0) },
        { k: 'live',  v: item.exposed && item.exposed.path ? item.exposed.path : 'no' },
      ];

  // Canonical doc panels — order is intentional. Areas show INDEX.md first
  // (areas use INDEX.md as the description root); projects skip it unless
  // the bundle reports it exists.
  const docFiles = _lopUseMemo(() => {
    const list = [];
    if (kind === 'area') {
      list.push('INDEX.md', 'README.md', 'AGENTS.md', 'CLAUDE.md');
    } else {
      list.push('README.md', 'AGENTS.md', 'CLAUDE.md');
      // Projects rarely have INDEX.md, but if they do, surface it last so
      // users can still edit it inline.
      if (mainFiles && mainFiles['INDEX.md'] && mainFiles['INDEX.md'].exists) {
        list.push('INDEX.md');
      }
    }
    return list;
  }, [kind, mainFiles]);

  return (
    <div style={{
      padding: compact ? 16 : 24,
      paddingBottom: compact ? 100 : 24,
      display: 'grid', gap: 14,
      gridTemplateColumns: compact ? '1fr' : '1.4fr 1fr',
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
        <Card padding={18}>
          <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
            Description
          </div>
          {mainLoading && <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>loading…</div>}
          {!mainLoading && mainError && (
            <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--err)' }}>error: {mainError}</div>
          )}
          {!mainLoading && !mainError && (
            description
              ? <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg)', lineHeight: 1.6 }}>{description}</div>
              : <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(no INDEX.md / README.md found)</div>
          )}
          <div style={{ marginTop: 16, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 12 }}>
            {stats.map((s) => (
              <div key={s.k}>
                <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 }}>{s.k}</div>
                <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }} className="mono">{s.v}</div>
              </div>
            ))}
          </div>
        </Card>

        {/* Canonical doc panels — quick inline view/edit. The fullscreen
            MdEditor remains available via the Files tab. */}
        {!mainLoading && !mainError && docFiles.map((rel) => (
          <LibraryFilePanel
            key={rel}
            apiBase={apiBase}
            rel={rel}
            initialBundle={mainFiles ? mainFiles[rel] : undefined}
            isMarkdown={/\.(md|markdown)$/i.test(rel)}
            defaultContent={''}
            onSaved={onFileSaved}
          />
        ))}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
        {window.LinkedItemsPanel
          ? <window.LinkedItemsPanel
              kind={kind}
              item={item}
              candidates={candidates}
              linkedNames={linkedNames}
              onLinksChange={onLinksChange}
            />
          : null}

        {window.OverviewGitPanel
          ? <window.OverviewGitPanel
              apiBase={apiBase}
              item={item}
              gitState={gitState}
              gitLoading={gitLoading}
              onReloadGit={onReloadGit}
              onItemChanged={onItemChanged}
            />
          : null}

        {item && item.is_repo && window.RecentCommitsPanel && (
          <window.RecentCommitsPanel apiBase={apiBase} item={item} limit={5} />
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// LibraryGitignoreTab — dedicated tab for .gitignore.
// Always shown (between Files and Branches) regardless of repo status —
// users may want to seed .gitignore before `git init`.
// ─────────────────────────────────────────────────────────────
function LibraryGitignoreTab({ apiBase, mainFiles, onFileSaved, compact }) {
  // Bundle fetch may or may not include .gitignore depending on backend.
  // Pass a sentinel `undefined` initialBundle so the panel falls back to a
  // direct GET /file?rel=.gitignore if necessary.
  const fromBundle = mainFiles && Object.prototype.hasOwnProperty.call(mainFiles, '.gitignore')
    ? mainFiles['.gitignore']
    : undefined;
  return (
    <div style={{ padding: compact ? 16 : 24, paddingBottom: compact ? 100 : 24, display: 'flex', flexDirection: 'column', gap: 14 }}>
      <LibraryFilePanel
        apiBase={apiBase}
        rel=".gitignore"
        initialBundle={fromBundle}
        isMarkdown={false}
        defaultContent={_LOP_GITIGNORE_DEFAULT}
        onSaved={onFileSaved}
      />
    </div>
  );
}

// LibraryBranchPanel was moved to library-branch-panel.jsx so this file
// stays focused on Overview / Gitignore + their inline file panels.

Object.assign(window, {
  LibraryOverviewPanel,
  LibraryGitignoreTab,
  LibraryFilePanel,
});
