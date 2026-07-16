// library-md-editor.jsx — fullscreen modal editor for whitelisted text files.
//
// Split-pane: textarea on the left, rendered markdown (or plain pre) on the
// right. Markdown is rendered via window.marked + DOMPurify (already loaded
// in index.html). Save uses optimistic concurrency via expected_sha256.
//
// PUT /api/library/{kind}/{name}/file
//   body: { rel, content, expected_sha256? }
//   errors: 403 (not whitelisted), 409 (sha mismatch),
//           400 (bad payload), 413 (>1MB)
//
// On 409 we surface a confirm() prompt — reload the on-disk version (which
// discards local edits and updates baselineSha) or keep typing (next save
// will use the new baseline).
//
// Whitelisted: INDEX.md, README.md, AGENTS.md, CLAUDE.md, .gitignore.
// (Backend is the source of truth; this mirror just informs the UI.)

const { useState: _mdUseState, useEffect: _mdUseEffect, useRef: _mdUseRef, useCallback: _mdUseCallback } = React;

const _MD_WHITELIST = new Set(['INDEX.md', 'README.md', 'AGENTS.md', 'CLAUDE.md', '.gitignore']);

function _mdBasename(rel) {
  if (!rel) return '';
  const parts = String(rel).split('/');
  return parts[parts.length - 1] || '';
}

function _mdIsMarkdown(rel) {
  return /\.(md|markdown)$/i.test(rel || '');
}

function _mdRenderMarkdown(text) {
  if (!text || !window.marked || typeof window.marked.parse !== 'function') return '';
  const raw = window.marked.parse(text);
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    return window.DOMPurify.sanitize(raw);
  }
  return '';
}

function _mdEncodeLibId(libId) {
  // Backend's {name:path} accepts both raw '/' and percent-encoded '%2F'.
  // We split on '/' and encode each segment so spaces / unicode are safe
  // but the path structure is preserved (FastAPI parses it as a path).
  return String(libId || '').split('/').map(encodeURIComponent).join('/');
}

function LibraryMdEditor({ kind, name, libId, rel, initialContent, baselineSha, onSaved, onClose }) {
  const [draft, setDraft] = _mdUseState(typeof initialContent === 'string' ? initialContent : '');
  const [baseline, setBaseline] = _mdUseState(typeof initialContent === 'string' ? initialContent : '');
  const [sha, setSha] = _mdUseState(baselineSha || null);
  const [busy, setBusy] = _mdUseState(false);
  const [err, setErr] = _mdUseState(null);
  const [savedAt, setSavedAt] = _mdUseState(null);
  const [showPreview, setShowPreview] = _mdUseState(true);
  const [compact, setCompact] = _mdUseState(typeof window !== 'undefined' && window.innerWidth < 760);
  const textareaRef = _mdUseRef(null);
  const draftRef = _mdUseRef(draft);
  const shaRef = _mdUseRef(sha);
  const busyRef = _mdUseRef(false);

  _mdUseEffect(() => { draftRef.current = draft; }, [draft]);
  _mdUseEffect(() => { shaRef.current = sha; }, [sha]);
  _mdUseEffect(() => { busyRef.current = busy; }, [busy]);

  const dirty = draft !== baseline;

  const kindPath = kind === 'area' ? 'areas' : (kind === 'project' ? 'projects' : kind);
  // Prefer libId (relative path) so nested projects resolve correctly on
  // the backend. Fall back to `name` for callers that haven't been updated.
  const apiBase = `/api/library/${encodeURIComponent(kindPath)}/${_mdEncodeLibId(libId || name)}`;

  const isMarkdown = _mdIsMarkdown(rel);
  const basename = _mdBasename(rel);
  const whitelisted = _MD_WHITELIST.has(basename);

  // Track viewport size for compact (mobile/narrow) layout.
  _mdUseEffect(() => {
    const onResize = () => setCompact(window.innerWidth < 760);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // Toast helper (optional — Phase 2 polish).
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  const reloadFromDisk = _mdUseCallback(async () => {
    try {
      setBusy(true);
      const r = await fetch(apiUrl(`${apiBase}/file?rel=${encodeURIComponent(rel)}`));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const data = await r.json();
      const next = typeof data.content === 'string' ? data.content : '';
      setDraft(next);
      setBaseline(next);
      setSha(data.sha256 || null);
      setErr(null);
    } catch (e) {
      setErr(e.message || 'reload failed');
    } finally {
      setBusy(false);
    }
  }, [apiBase, rel]);

  const save = _mdUseCallback(async () => {
    if (busyRef.current) return;
    setBusy(true);
    setErr(null);
    try {
      const body = {
        rel,
        content: draftRef.current,
      };
      if (shaRef.current) body.expected_sha256 = shaRef.current;
      const r = await fetch(apiUrl(`${apiBase}/file`), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.status === 409) {
        const detail = await r.json().catch(() => ({}));
        const reload = window.confirm(
          'Plik został zmieniony na dysku od czasu otwarcia edytora.\n\n'
          + 'OK – wczytaj wersję z dysku (Twoje zmiany zostaną odrzucone).\n'
          + 'Anuluj – kontynuuj edycję (kolejny zapis użyje nowej wersji jako bazy).',
        );
        if (reload) {
          await reloadFromDisk();
        } else if (detail && detail.sha256) {
          // Adopt new baseline so next save will succeed.
          setSha(detail.sha256);
        }
        return;
      }
      if (r.status === 403) {
        throw new Error('Plik nie jest na liście dozwolonych do zapisu.');
      }
      if (r.status === 413) {
        throw new Error('Plik za duży (limit 1 MB).');
      }
      if (!r.ok) {
        let msg = `http ${r.status}`;
        try {
          const d = await r.json();
          if (d && (d.error || d.detail)) msg = String(d.error || d.detail).slice(0, 200);
        } catch (_) { /* ignore */ }
        throw new Error(msg);
      }
      const data = await r.json().catch(() => ({}));
      // Update baseline so dirty becomes false; adopt new sha.
      setBaseline(draftRef.current);
      if (data && data.sha256) setSha(data.sha256);
      setSavedAt(Date.now());
      toast && toast('Saved', 'ok');
      if (typeof onSaved === 'function') onSaved(rel, data);
    } catch (e) {
      setErr(e.message || 'save failed');
    } finally {
      setBusy(false);
    }
  }, [apiBase, rel, onSaved, reloadFromDisk, toast]);

  // Cmd/Ctrl+S = save, ESC = close (with discard prompt if dirty).
  _mdUseEffect(() => {
    const onKey = (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && (e.key === 's' || e.key === 'S')) {
        e.preventDefault();
        if (whitelisted) save();
        return;
      }
      if (e.key === 'Escape') {
        if (draftRef.current !== baseline) {
          if (!window.confirm('Niezapisane zmiany — porzucić?')) return;
        }
        if (typeof onClose === 'function') onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [save, baseline, whitelisted, onClose]);

  const onCloseClick = () => {
    if (dirty && !window.confirm('Niezapisane zmiany — porzucić?')) return;
    if (typeof onClose === 'function') onClose();
  };

  const inputBg = 'var(--surface-2)';
  const headerStyle = {
    flexShrink: 0,
    padding: '12px 18px',
    borderBottom: '1px solid var(--hairline)',
    display: 'flex', alignItems: 'center', gap: 10,
    background: 'var(--surface-1)',
  };

  const splitStyle = compact
    ? { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }
    : { flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: showPreview ? '1fr 1fr' : '1fr' };

  const showCompactPreview = compact && showPreview;

  const previewHtml = isMarkdown ? _mdRenderMarkdown(draft) : '';

  const textareaPane = (
    <textarea
      ref={textareaRef}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      spellCheck={false}
      readOnly={!whitelisted}
      placeholder={whitelisted ? '' : 'Plik tylko do odczytu (poza listą dozwolonych do zapisu).'}
      style={{
        flex: 1, minHeight: 0,
        width: '100%', boxSizing: 'border-box',
        background: inputBg,
        color: 'var(--fg)',
        border: 'none',
        borderRight: compact ? 'none' : '1px solid var(--hairline)',
        outline: 'none',
        padding: '14px 18px',
        fontFamily: 'JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 'var(--t-sm)', lineHeight: 1.6,
        resize: 'none',
      }}
    />
  );

  const previewPane = (
    <div style={{
      flex: 1, minHeight: 0, overflowY: 'auto',
      background: 'var(--surface-1)',
      padding: '14px 18px',
    }} className="scroll-hide">
      {isMarkdown ? (
        <div className="md-render"
             style={{ fontSize: 'var(--t-sm)', lineHeight: 1.6, color: 'var(--fg)' }}
             dangerouslySetInnerHTML={{ __html: previewHtml }} />
      ) : (
        <pre className="mono" style={{
          margin: 0, fontSize: 'var(--t-cap)', lineHeight: 1.7, color: 'var(--fg)',
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}>{draft}</pre>
      )}
    </div>
  );

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Editing ${rel}`}
      className="library-md-editor"
      style={{
        position: 'fixed', inset: 0, zIndex: 200,
        background: 'var(--bg)',
        display: 'flex', flexDirection: 'column',
      }}
    >
      <div style={headerStyle}>
        <Icon name="file" size={14} color="var(--fg-3)" />
        <span className="mono" style={{
          fontSize: 'var(--t-cap)', color: 'var(--fg-2)',
          flex: 1, minWidth: 0,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{rel}</span>
        {dirty && (
          <Chip mono style={{ fontSize: 'var(--t-2xs)' }}>unsaved</Chip>
        )}
        {savedAt && !dirty && (
          <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>Saved</span>
        )}
        <Button
          size="sm"
          variant="quiet"
          onClick={() => setShowPreview((v) => !v)}
          icon="eye"
        >
          {showPreview ? 'Hide preview' : 'Show preview'}
        </Button>
        <Button
          size="sm"
          variant="primary"
          icon={busy ? 'spinner' : 'check'}
          onClick={save}
          type="button"
          style={whitelisted ? undefined : { opacity: 0.5, pointerEvents: 'none' }}
        >
          {busy ? 'Saving…' : 'Save'}
        </Button>
        <Button size="sm" variant="quiet" icon="close" onClick={onCloseClick} type="button">Close</Button>
      </div>

      <div style={splitStyle}>
        {compact ? (showCompactPreview ? previewPane : textareaPane) : textareaPane}
        {!compact && showPreview && previewPane}
      </div>

      {err && (
        <div className="mono" style={{
          flexShrink: 0,
          padding: '8px 14px',
          fontSize: 'var(--t-cap)', color: 'var(--err)',
          background: 'var(--err-bg)',
          borderTop: '1px solid var(--err)',
        }}>
          {err}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { LibraryMdEditor });
