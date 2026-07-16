// library-github-panels.jsx — Phase 3 PR + Issue panels for Areas/Projects.
//
// Top-level: LibraryPRList, LibraryIssueList. Both share the same list/detail
// scaffold (segmented state filter, table on the left, detail pane on the
// right; on compact viewports the detail opens fullscreen).
//
// Endpoints under /api/library/{kind}/{name}/github/…
//   GET    /prs?state=open|closed|all&limit=50
//   GET    /prs/{n}
//   PATCH  /prs/{n}                    body: { body }
//   GET    /issues?state=open|closed|all&limit=50
//   GET    /issues/{n}
//   POST   /issues                     body: { title, body?, labels? }
//   PATCH  /issues/{n}                 body: { title?, body?, state?, add_labels?, remove_labels? }
//
// 424 = github failed (no remote / auth / network / 404) — surfaced inline.
// 400 = bad input.

const { useState: _ghUseState, useEffect: _ghUseEffect, useCallback: _ghUseCallback, useMemo: _ghUseMemo } = React;

const _GH_STATE_OPTIONS = [
  { k: 'open',   l: 'Open' },
  { k: 'closed', l: 'Closed' },
  { k: 'all',    l: 'All' },
];

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────
function _ghKindPath(kind) {
  if (kind === 'area') return 'areas';
  if (kind === 'project') return 'projects';
  return kind;
}

function _ghEncodeLibId(libId) {
  // Encode each path segment so nested projects (e.g. "parent/child")
  // and spaces survive while structural slashes pass through to FastAPI's
  // {name:path} parameter.
  return String(libId || '').split('/').map(encodeURIComponent).join('/');
}

function _ghBaseUrl(kind, libId) {
  return `/api/library/${encodeURIComponent(_ghKindPath(kind))}/${_ghEncodeLibId(libId)}/github`;
}

function _ghAuthorName(a) {
  if (!a) return '';
  if (typeof a === 'string') return a;
  // gh JSON returns user objects {id, is_bot, login, name}
  return a.login || a.name || '';
}

function _ghRenderMarkdown(text) {
  if (!text || typeof text !== 'string') return '';
  if (!window.marked || typeof window.marked.parse !== 'function') return '';
  const raw = window.marked.parse(text);
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    return window.DOMPurify.sanitize(raw);
  }
  return '';
}

function _ghRelTime(iso) {
  // gh JSON returns ISO 8601 strings (e.g. "2026-05-04T20:30:00Z").
  // Don't delegate to window.relTime — that helper expects a unix
  // timestamp number, and Number("2026-…") is NaN → "NaN-NaN-NaN".
  if (!iso) return '';
  const d = new Date(iso);
  const t = d.getTime();
  if (Number.isNaN(t)) return '';
  const diff = Math.floor((Date.now() - t) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + 'd ago';
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return yyyy + '-' + mm + '-' + dd;
}

async function _ghFetchJson(url, init) {
  let res;
  try { res = await fetch(url, init); }
  catch (e) { throw new Error('network error: ' + (e && e.message ? e.message : 'fetch failed')); }
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (res.status === 424) {
    const hint = (data && data.error)
      ? String(data.error).slice(0, 200)
      : 'Repo nie ma remote’a github lub gh CLI nie autoryzowany';
    const err = new Error(hint); err.status = 424; throw err;
  }
  if (!res.ok || (data && data.ok === false)) {
    const detail = (data && (data.error || data.detail)) || `http ${res.status}`;
    const err = new Error(String(detail).slice(0, 200)); err.status = res.status; throw err;
  }
  return data || {};
}

function _ghToast() {
  if (typeof window !== 'undefined' && typeof window.useToast === 'function') {
    return window.useToast();
  }
  return null;
}

function _ghUseCompact(breakpoint = 880) {
  const [compact, setCompact] = _ghUseState(typeof window !== 'undefined' && window.innerWidth < breakpoint);
  _ghUseEffect(() => {
    const onResize = () => setCompact(window.innerWidth < breakpoint);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [breakpoint]);
  return compact;
}

// ─────────────────────────────────────────────────────────────
// Tiny presentational bits
// ─────────────────────────────────────────────────────────────
function _GhStateChip({ state, isDraft }) {
  const lower = String(state || '').toLowerCase();
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <Chip mono accent={lower === 'open'}>{lower || '—'}</Chip>
      {isDraft && <Chip mono>draft</Chip>}
    </span>
  );
}

function _GhLabelChip({ label }) {
  const name = (label && label.name) ? String(label.name) : '';
  const color = (label && label.color) ? String(label.color) : '';
  return (
    <span className="mono" style={{
      display: 'inline-flex', alignItems: 'center',
      padding: '2px 8px', borderRadius: 'var(--r-pill)',
      fontSize: 'var(--t-xs)', color: 'var(--fg-2)',
      background: color ? `#${color}33` : 'var(--surface-2)',
      border: '1px solid ' + (color ? `#${color}66` : 'var(--hairline)'),
      whiteSpace: 'nowrap',
    }}>{name}</span>
  );
}

function _GhSegmented({ value, onChange, options }) {
  return (
    <div style={{
      display: 'inline-flex', gap: 4, padding: 3,
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)',
    }}>
      {options.map((opt) => {
        const active = value === opt.k;
        return (
          <button
            key={opt.k} type="button" onClick={() => onChange(opt.k)}
            style={{
              padding: '5px 12px', borderRadius: 'var(--r-sm)',
              background: active ? 'var(--accent-soft)' : 'transparent',
              color: active ? 'var(--accent)' : 'var(--fg-2)',
              border: '1px solid ' + (active ? 'var(--accent-line)' : 'transparent'),
              fontSize: 'var(--t-cap)', fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
            }}
          >{opt.l}</button>
        );
      })}
    </div>
  );
}

function _GhErrorAlert({ message, onRetry }) {
  return (
    <StatusBanner
      variant="err"
      label={message}
      inline
      action={onRetry ? <Button size="sm" variant="ghost" icon="refresh" onClick={onRetry} type="button">Retry</Button> : undefined}
    />
  );
}

function _GhLoading() {
  return <div style={{ padding: 22 }}><Spinner inline size={14} label="ładuję…" /></div>;
}

function _GhBodyEditor({ initial, busy, onCancel, onSave }) {
  const [draft, setDraft] = _ghUseState(typeof initial === 'string' ? initial : '');
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <Textarea
        value={draft} onChange={setDraft}
        rows={10}
        style={{ resize: 'vertical' }}
      />
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <Button size="sm" variant="quiet" onClick={onCancel} type="button">Cancel</Button>
        <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'check'}
                onClick={() => onSave(draft)} type="button">
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </div>
  );
}

function _GhBodyView({ markdown }) {
  const safe = _ghRenderMarkdown(markdown || '');
  if (safe) {
    return (
      <div className="md-render" style={{ fontSize: 'var(--t-sm)', lineHeight: 1.6, color: 'var(--fg)' }}
           dangerouslySetInnerHTML={{ __html: safe }} />
    );
  }
  return (
    <pre className="mono" style={{
      margin: 0, fontSize: 'var(--t-cap)', lineHeight: 1.7, color: 'var(--fg)',
      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    }}>{markdown || ''}</pre>
  );
}

function _GhFullscreen({ children, onClose }) {
  _ghUseEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose && onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div role="dialog" aria-modal="true" style={{
      position: 'fixed', inset: 0, zIndex: 220,
      background: 'var(--bg)', display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', background: 'var(--surface-1)' }}
           className="scroll-hide">
        {children}
      </div>
    </div>
  );
}

const _ghTh = {
  textAlign: 'left', fontSize: 'var(--t-xs)', fontWeight: 500,
  textTransform: 'uppercase', letterSpacing: '0.08em',
  padding: '8px 12px', borderBottom: '1px solid var(--hairline)',
  background: 'var(--surface-1)',
};
const _ghTd = { padding: '10px 12px', verticalAlign: 'top' };

// ─────────────────────────────────────────────────────────────
// Generic list+detail hook used by both PR and Issue panels.
// resource = 'prs' | 'issues'
// ─────────────────────────────────────────────────────────────
function _useGhListPanel({ kind, libId, resource }) {
  const apiBase = _ghUseMemo(() => _ghBaseUrl(kind, libId), [kind, libId]);
  const [stateFilter, setStateFilter] = _ghUseState('open');
  const [items, setItems] = _ghUseState([]);
  const [loading, setLoading] = _ghUseState(true);
  const [error, setError] = _ghUseState(null);
  const [selectedNum, setSelectedNum] = _ghUseState(null);
  const [detail, setDetail] = _ghUseState(null);
  const [editing, setEditing] = _ghUseState(false);
  const [savingBody, setSavingBody] = _ghUseState(false);

  const toast = _ghToast();

  const reload = _ghUseCallback(async () => {
    setLoading(true); setError(null);
    try {
      const data = await _ghFetchJson(
        apiUrl(`${apiBase}/${resource}?state=${encodeURIComponent(stateFilter)}&limit=50`),
      );
      setItems(Array.isArray(data.items) ? data.items : []);
    } catch (e) {
      setItems([]); setError(e.message || `failed to load ${resource}`);
    } finally { setLoading(false); }
  }, [apiBase, resource, stateFilter]);

  _ghUseEffect(() => { reload(); }, [reload]);

  const openDetail = _ghUseCallback(async (num) => {
    setSelectedNum(num); setEditing(false);
    setDetail({ loading: true, error: null, data: null });
    try {
      const res = await _ghFetchJson(apiUrl(`${apiBase}/${resource}/${encodeURIComponent(num)}`));
      setDetail({ loading: false, error: null, data: res.data || null });
    } catch (e) {
      setDetail({ loading: false, error: e.message || 'failed to load', data: null });
    }
  }, [apiBase, resource]);

  const closeDetail = _ghUseCallback(() => {
    setSelectedNum(null); setDetail(null); setEditing(false);
  }, []);

  const saveBody = _ghUseCallback(async (nextBody) => {
    if (!selectedNum || savingBody) return;
    setSavingBody(true);
    try {
      await _ghFetchJson(apiUrl(`${apiBase}/${resource}/${encodeURIComponent(selectedNum)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: nextBody }),
      });
      toast && toast('Body updated', 'ok');
      setDetail((prev) => prev && prev.data ? { ...prev, data: { ...prev.data, body: nextBody } } : prev);
      setEditing(false);
    } catch (e) {
      toast && toast(`Save failed: ${e.message || 'error'}`, 'err');
    } finally { setSavingBody(false); }
  }, [apiBase, resource, selectedNum, savingBody, toast]);

  return {
    apiBase, stateFilter, setStateFilter, items, loading, error,
    selectedNum, detail, setDetail, editing, setEditing, savingBody,
    reload, openDetail, closeDetail, saveBody, toast,
  };
}

// ─────────────────────────────────────────────────────────────
// Issue comments — list + add box (issue detail only)
// ─────────────────────────────────────────────────────────────
function _GhComments({ comments, onAdd, adding }) {
  const [text, setText] = _ghUseState('');
  const list = Array.isArray(comments) ? comments : [];
  const submit = () => {
    const t = text.trim();
    if (t && !adding) onAdd(t, () => setText(''));
  };
  return (
    <div style={{
      paddingTop: 12, borderTop: '1px solid var(--hairline)',
      display: 'flex', flexDirection: 'column', gap: 10,
    }}>
      <span className="mono" style={{
        fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em',
      }}>Comments ({list.length})</span>
      {list.map((c, i) => {
        const html = _ghRenderMarkdown(c && c.body);
        return (
          <div key={(c && c.id) || i} style={{
            background: 'var(--surface-2)', border: '1px solid var(--hairline)',
            borderRadius: 'var(--r-control)', padding: '8px 10px',
          }}>
            <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 4 }}>
              {_ghAuthorName(c && c.author) || '?'} · {_ghRelTime(c && c.createdAt)}
            </div>
            {html
              ? <div className="gh-md" dangerouslySetInnerHTML={{ __html: html }} />
              : <div style={{ whiteSpace: 'pre-wrap', fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>{(c && c.body) || ''}</div>}
          </div>
        );
      })}
      <Textarea value={text} onChange={setText} rows={3}
        placeholder="Add a comment…"
        style={{ resize: 'vertical' }} />
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button size="sm" variant="primary" icon={adding ? 'spinner' : 'send'} type="button"
          onClick={submit} style={!text.trim() ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
          {adding ? 'Posting…' : 'Comment'}
        </Button>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Issue editor — title + labels + body in one edit pass.
// Diffs labels against the current set → add_labels/remove_labels.
// ─────────────────────────────────────────────────────────────
function _GhIssueEditor({ data, busy, onCancel, onSave }) {
  const cur = (Array.isArray(data.labels) ? data.labels : []).map((l) => l && l.name).filter(Boolean);
  const [title, setTitle] = _ghUseState(data.title || '');
  const [body, setBody] = _ghUseState(data.body || '');
  const [labelsRaw, setLabelsRaw] = _ghUseState(cur.join(', '));
  const inputStyle = {
    width: '100%', boxSizing: 'border-box', background: 'var(--surface-2)',
    border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)', padding: '8px 10px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
  };
  const save = () => {
    if (busy) return;
    const next = labelsRaw.split(',').map((s) => s.trim()).filter(Boolean);
    const add = next.filter((l) => !cur.includes(l));
    const remove = cur.filter((l) => !next.includes(l));
    const patch = {};
    const t = title.trim();
    if (t && t !== (data.title || '')) patch.title = t;
    if (body !== (data.body || '')) patch.body = body;
    if (add.length) patch.add_labels = add;
    if (remove.length) patch.remove_labels = remove;
    onSave(patch, { title: t || data.title, body, labels: next.map((n) => ({ name: n })) });
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <Input value={title} onChange={setTitle} placeholder="Title" mono={false} />
      <Input value={labelsRaw} onChange={setLabelsRaw} placeholder="labels, comma, separated" mono={false} />
      <Textarea value={body} onChange={setBody} rows={8}
        style={{ resize: 'vertical' }} />
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <Button size="sm" variant="quiet" onClick={onCancel} type="button">Cancel</Button>
        <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'check'} onClick={save} type="button">
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Detail pane (shared for PR and Issue)
// ─────────────────────────────────────────────────────────────
function _GhDetailPane({
  kind, detail, editing, onEdit, onCancelEdit, onSaveBody, savingBody, onClose, compact,
  onToggleState, togglingState, onMakeTask, makingTask, renderEditor, extras,
}) {
  if (!detail) return null;
  if (detail.loading) return <div style={{ padding: 14 }}><_GhLoading /></div>;
  if (detail.error) return <div style={{ padding: 14 }}><_GhErrorAlert message={detail.error} /></div>;

  const data = detail.data || {};
  const isIssue = kind === 'issue';
  const labels = Array.isArray(data.labels) ? data.labels : [];
  const isClosed = String(data.state || '').toLowerCase() === 'closed';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '12px 14px', borderBottom: '1px solid var(--hairline)',
      }}>
        <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>#{data.number}</span>
        <span style={{
          flex: 1, fontSize: 'var(--t-md)', color: 'var(--fg)', fontWeight: 500,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{data.title || '—'}</span>
        {compact && <IconButton icon="close" label="Close" size={28} onClick={onClose} />}
      </div>

      <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 12, overflow: 'auto' }}
           className="scroll-hide">
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <_GhStateChip state={data.state} isDraft={data.isDraft} />
          {!isIssue && data.headRefName && (
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-2)' }}>
              {data.headRefName} <span style={{ color: 'var(--fg-4)' }}>{'→'}</span> {data.baseRefName || ''}
            </span>
          )}
          {isIssue && Array.isArray(data.comments) && data.comments.length > 0 && (
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{data.comments.length} comments</span>
          )}
          {_ghAuthorName(data.author) && (
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>by {_ghAuthorName(data.author)}</span>
          )}
          {data.updatedAt && (
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>· {_ghRelTime(data.updatedAt)}</span>
          )}
        </div>

        {isIssue && labels.length > 0 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {labels.map((l, i) => <_GhLabelChip key={(l && l.name) || i} label={l} />)}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {data.url && (
            <a href={data.url} target="_blank" rel="noopener noreferrer" style={{ textDecoration: 'none' }}>
              <Button size="sm" variant="ghost" icon="external" type="button">Open on GitHub</Button>
            </a>
          )}
          {!editing && (
            <Button size="sm" variant="ghost" icon="pencil" onClick={onEdit} type="button">
              {isIssue ? 'Edit' : 'Edit body'}
            </Button>
          )}
          {isIssue && onToggleState && (
            <Button size="sm" variant={isClosed ? 'ghost' : 'danger'}
              icon={togglingState ? 'spinner' : (isClosed ? 'refresh' : 'close')}
              onClick={onToggleState} type="button">
              {togglingState ? 'Working…' : (isClosed ? 'Reopen' : 'Close issue')}
            </Button>
          )}
          {isIssue && onMakeTask && (
            <Button size="sm" variant="ghost" icon={makingTask ? 'spinner' : 'tasks'}
              onClick={onMakeTask} type="button"
              title="Add this issue to the global Tasks board">
              {makingTask ? 'Working…' : 'Make task'}
            </Button>
          )}
        </div>

        <div style={{ paddingTop: 6, borderTop: '1px solid var(--hairline)' }}>
          {editing ? (
            renderEditor ? renderEditor() : (
              <_GhBodyEditor initial={data.body || ''} busy={savingBody}
                              onCancel={onCancelEdit} onSave={onSaveBody} />
            )
          ) : (
            <_GhBodyView markdown={data.body} />
          )}
        </div>
        {extras}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// PR table + LibraryPRList
// ─────────────────────────────────────────────────────────────
function _GhPRTable({ items, loading, error, onOpen, selectedNum, onRetry }) {
  if (loading) return <_GhLoading />;
  if (error) return <div style={{ padding: 14 }}><_GhErrorAlert message={error} onRetry={onRetry} /></div>;
  if (!items || items.length === 0) {
    return <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>No pull requests.</div>;
  }
  return (
    <div style={{ overflowX: 'auto' }} className="scroll-hide">
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
        <thead>
          <tr style={{ color: 'var(--fg-3)' }}>
            <th style={_ghTh}>#</th><th style={_ghTh}>Title</th><th style={_ghTh}>Branch</th>
            <th style={_ghTh}>Author</th><th style={_ghTh}>Updated</th>
          </tr>
        </thead>
        <tbody>
          {items.map((pr) => (
            <tr key={pr.number} onClick={() => onOpen(pr.number)} style={{
              cursor: 'pointer',
              background: pr.number === selectedNum ? 'var(--accent-soft)' : 'transparent',
              borderTop: '1px solid var(--hairline)',
            }}>
              <td style={_ghTd} className="mono">#{pr.number}</td>
              <td style={_ghTd}>
                <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                  <span style={{
                    color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap', maxWidth: 320,
                  }}>{pr.title || '—'}</span>
                  {pr.isDraft && <Chip mono>draft</Chip>}
                </span>
              </td>
              <td style={_ghTd} className="mono"><span style={{ color: 'var(--fg-2)' }}>{pr.headRefName || '—'}</span></td>
              <td style={_ghTd} className="mono"><span style={{ color: 'var(--fg-2)' }}>{_ghAuthorName(pr.author) || '—'}</span></td>
              <td style={_ghTd} className="mono"><span style={{ color: 'var(--fg-3)' }}>{_ghRelTime(pr.updatedAt)}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LibraryPRList({ kind, name, libId }) {
  // Prefer libId (relative path under ~/Areas or ~/Projects); fall back to
  // name so legacy single-level call sites keep working.
  const effectiveLibId = libId || name;
  const ctl = _useGhListPanel({ kind, libId: effectiveLibId, resource: 'prs' });
  const compact = _ghUseCompact();

  const detailNode = (ctl.selectedNum && ctl.detail) ? (
    <_GhDetailPane
      kind="pr" detail={ctl.detail}
      editing={ctl.editing}
      onEdit={() => ctl.setEditing(true)}
      onCancelEdit={() => ctl.setEditing(false)}
      onSaveBody={ctl.saveBody}
      savingBody={ctl.savingBody}
      onClose={ctl.closeDetail}
      compact={compact}
    />
  ) : null;

  return (
    <div style={_ghPanelContainer(compact)}>
      <Card padding={0} style={compact ? {} : { alignSelf: 'start' }}>
        <_GhPanelHeader title="Pull requests"
          stateFilter={ctl.stateFilter} setStateFilter={ctl.setStateFilter}
          onReload={ctl.reload} />
        <_GhPRTable items={ctl.items} loading={ctl.loading} error={ctl.error}
          onOpen={ctl.openDetail} selectedNum={ctl.selectedNum} onRetry={ctl.reload} />
      </Card>

      {!compact && (
        <Card padding={0}>
          {detailNode || (
            <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
              Pick a PR to see details.
            </div>
          )}
        </Card>
      )}
      {compact && ctl.selectedNum && (
        <_GhFullscreen onClose={ctl.closeDetail}>{detailNode}</_GhFullscreen>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Issue table + LibraryIssueList
// ─────────────────────────────────────────────────────────────
function _GhIssueTable({ items, loading, error, onOpen, selectedNum, onRetry }) {
  if (loading) return <_GhLoading />;
  if (error) return <div style={{ padding: 14 }}><_GhErrorAlert message={error} onRetry={onRetry} /></div>;
  if (!items || items.length === 0) {
    return <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>No issues.</div>;
  }
  return (
    <div style={{ overflowX: 'auto' }} className="scroll-hide">
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
        <thead>
          <tr style={{ color: 'var(--fg-3)' }}>
            <th style={_ghTh}>#</th><th style={_ghTh}>Title</th><th style={_ghTh}>Labels</th>
            <th style={_ghTh}>Author</th><th style={_ghTh}>Updated</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => {
            const labels = Array.isArray(it.labels) ? it.labels : [];
            return (
              <tr key={it.number} onClick={() => onOpen(it.number)} style={{
                cursor: 'pointer',
                background: it.number === selectedNum ? 'var(--accent-soft)' : 'transparent',
                borderTop: '1px solid var(--hairline)',
              }}>
                <td style={_ghTd} className="mono">#{it.number}</td>
                <td style={_ghTd}>
                  <span style={{
                    color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap', maxWidth: 320, display: 'inline-block',
                  }}>{it.title || '—'}</span>
                </td>
                <td style={_ghTd}>
                  <span style={{ display: 'inline-flex', gap: 4, flexWrap: 'wrap' }}>
                    {labels.slice(0, 4).map((l, i) => <_GhLabelChip key={(l && l.name) || i} label={l} />)}
                    {labels.length > 4 && (
                      <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>+{labels.length - 4}</span>
                    )}
                  </span>
                </td>
                <td style={_ghTd} className="mono"><span style={{ color: 'var(--fg-2)' }}>{_ghAuthorName(it.author) || '—'}</span></td>
                <td style={_ghTd} className="mono"><span style={{ color: 'var(--fg-3)' }}>{_ghRelTime(it.updatedAt)}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LibraryIssueList({ kind, name, libId }) {
  const effectiveLibId = libId || name;
  const ctl = _useGhListPanel({ kind, libId: effectiveLibId, resource: 'issues' });
  const compact = _ghUseCompact();
  const [togglingState, setTogglingState] = _ghUseState(false);
  const [createOpen, setCreateOpen] = _ghUseState(false);
  const [makingTask, setMakingTask] = _ghUseState(false);
  const [addingComment, setAddingComment] = _ghUseState(false);
  const [savingIssue, setSavingIssue] = _ghUseState(false);

  const toggleState = _ghUseCallback(async () => {
    if (!ctl.selectedNum || togglingState || !ctl.detail || !ctl.detail.data) return;
    const current = String(ctl.detail.data.state || '').toLowerCase();
    const next = current === 'closed' ? 'open' : 'closed';
    setTogglingState(true);
    try {
      await _ghFetchJson(apiUrl(`${ctl.apiBase}/issues/${encodeURIComponent(ctl.selectedNum)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: next }),
      });
      ctl.toast && ctl.toast(next === 'closed' ? 'Issue closed' : 'Issue reopened', 'ok');
      ctl.setDetail((prev) => prev && prev.data
        ? { ...prev, data: { ...prev.data, state: next } } : prev);
      ctl.reload();
    } catch (e) {
      ctl.toast && ctl.toast(`Failed: ${e.message || 'error'}`, 'err');
    } finally { setTogglingState(false); }
  }, [ctl, togglingState]);

  const makeTask = _ghUseCallback(async () => {
    if (!ctl.selectedNum || makingTask) return;
    setMakingTask(true);
    try {
      await _ghFetchJson(apiUrl(`${ctl.apiBase}/issues/${encodeURIComponent(ctl.selectedNum)}/make-task`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      ctl.toast && ctl.toast('Added to the Tasks board', 'ok');
    } catch (e) {
      ctl.toast && ctl.toast(`Make task failed: ${e.message || 'error'}`, 'err');
    } finally { setMakingTask(false); }
  }, [ctl, makingTask]);

  const addComment = _ghUseCallback(async (text, done) => {
    if (!ctl.selectedNum || addingComment) return;
    setAddingComment(true);
    try {
      await _ghFetchJson(apiUrl(`${ctl.apiBase}/issues/${encodeURIComponent(ctl.selectedNum)}/comments`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ body: text }),
      });
      ctl.toast && ctl.toast('Comment added', 'ok');
      done && done();
      ctl.openDetail(ctl.selectedNum);  // refresh detail to surface the new comment
    } catch (e) {
      ctl.toast && ctl.toast(`Comment failed: ${e.message || 'error'}`, 'err');
    } finally { setAddingComment(false); }
  }, [ctl, addingComment]);

  const saveIssue = _ghUseCallback(async (patch, optimistic) => {
    if (savingIssue) return;
    if (!patch || Object.keys(patch).length === 0) { ctl.setEditing(false); return; }
    setSavingIssue(true);
    try {
      await _ghFetchJson(apiUrl(`${ctl.apiBase}/issues/${encodeURIComponent(ctl.selectedNum)}`), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
      });
      ctl.toast && ctl.toast('Issue updated', 'ok');
      ctl.setDetail((prev) => prev && prev.data ? { ...prev, data: { ...prev.data, ...optimistic } } : prev);
      ctl.setEditing(false);
      ctl.reload();
    } catch (e) {
      ctl.toast && ctl.toast(`Save failed: ${e.message || 'error'}`, 'err');
    } finally { setSavingIssue(false); }
  }, [ctl, savingIssue]);

  const onCreated = _ghUseCallback((num) => {
    setCreateOpen(false);
    ctl.reload();
    if (num) ctl.openDetail(num);
  }, [ctl]);

  const issueData = (ctl.detail && ctl.detail.data) ? ctl.detail.data : null;
  const detailNode = (ctl.selectedNum && ctl.detail) ? (
    <_GhDetailPane
      kind="issue" detail={ctl.detail}
      editing={ctl.editing}
      onEdit={() => ctl.setEditing(true)}
      onCancelEdit={() => ctl.setEditing(false)}
      onSaveBody={ctl.saveBody}
      savingBody={ctl.savingBody}
      onToggleState={toggleState}
      togglingState={togglingState}
      onClose={ctl.closeDetail}
      compact={compact}
      onMakeTask={makeTask}
      makingTask={makingTask}
      renderEditor={issueData ? (() => (
        <_GhIssueEditor data={issueData} busy={savingIssue}
          onCancel={() => ctl.setEditing(false)} onSave={saveIssue} />
      )) : null}
      extras={issueData ? (
        <_GhComments comments={issueData.comments} onAdd={addComment} adding={addingComment} />
      ) : null}
    />
  ) : null;

  return (
    <div style={_ghPanelContainer(compact)}>
      <Card padding={0} style={compact ? {} : { alignSelf: 'start' }}>
        <_GhPanelHeader title="Issues"
          stateFilter={ctl.stateFilter} setStateFilter={ctl.setStateFilter}
          onReload={ctl.reload}
          extra={(
            <Button size="sm" variant="primary" icon="plus"
              onClick={() => setCreateOpen(true)} type="button">New issue</Button>
          )} />
        <_GhIssueTable items={ctl.items} loading={ctl.loading} error={ctl.error}
          onOpen={ctl.openDetail} selectedNum={ctl.selectedNum} onRetry={ctl.reload} />
      </Card>

      {!compact && (
        <Card padding={0}>
          {detailNode || (
            <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
              Pick an issue to see details.
            </div>
          )}
        </Card>
      )}
      {compact && ctl.selectedNum && (
        <_GhFullscreen onClose={ctl.closeDetail}>{detailNode}</_GhFullscreen>
      )}

      {createOpen && (
        <CreateIssueDialog kind={kind} name={name} libId={effectiveLibId}
          onClose={() => setCreateOpen(false)} onCreated={onCreated} />
      )}
    </div>
  );
}

function _ghPanelContainer(compact) {
  return {
    padding: compact ? 16 : 24,
    paddingBottom: compact ? 100 : 24,
    display: compact ? 'flex' : 'grid',
    flexDirection: 'column',
    gap: 14,
    gridTemplateColumns: compact ? undefined : '1fr 1fr',
    minHeight: 0,
  };
}

function _GhPanelHeader({ title, stateFilter, setStateFilter, onReload, extra }) {
  return (
    <div style={{
      padding: '12px 14px', borderBottom: '1px solid var(--hairline)',
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
    }}>
      <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>{title}</span>
      <span style={{ flex: 1 }} />
      <_GhSegmented value={stateFilter} onChange={setStateFilter} options={_GH_STATE_OPTIONS} />
      <Button size="sm" variant="ghost" icon="refresh" onClick={onReload} type="button">Refresh</Button>
      {extra}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// CreateIssueDialog
// ─────────────────────────────────────────────────────────────
function CreateIssueDialog({ kind, name, libId, onClose, onCreated }) {
  const effectiveLibId = libId || name;
  const apiBase = _ghUseMemo(() => _ghBaseUrl(kind, effectiveLibId), [kind, effectiveLibId]);
  const [title, setTitle] = _ghUseState('');
  const [body, setBody] = _ghUseState('');
  const [labelsRaw, setLabelsRaw] = _ghUseState('');
  const [busy, setBusy] = _ghUseState(false);
  const [error, setError] = _ghUseState(null);
  const toast = _ghToast();

  const titleTrim = title.trim();
  const titleError = !titleTrim ? 'Title is required' : null;

  const submit = _ghUseCallback(async (e) => {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    if (busy) return;
    setError(null);
    if (titleError) { setError(titleError); return; }

    const labels = labelsRaw.split(',').map((s) => s.trim()).filter(Boolean);
    const payload = { title: titleTrim };
    if (body.trim()) payload.body = body;
    if (labels.length > 0) payload.labels = labels;

    setBusy(true);
    try {
      const res = await _ghFetchJson(apiUrl(`${apiBase}/issues`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      toast && toast('Issue created', 'ok');
      window.dispatchEvent(new CustomEvent('library:reload'));
      onCreated && onCreated(res.number || null);
    } catch (err) {
      setError(err.message || 'Create failed');
    } finally { setBusy(false); }
  }, [apiBase, busy, titleError, titleTrim, body, labelsRaw, toast, onCreated]);

  const inputStyle = {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
  };
  const labelStyle = { display: 'flex', flexDirection: 'column', gap: 6 };
  const labelEyebrow = {
    fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
    textTransform: 'uppercase', letterSpacing: '0.08em',
  };

  return (
    <Modal open={true} onClose={onClose} title="New issue" width={520}>
      <form onSubmit={submit} style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <label style={labelStyle}>
          <span className="mono" style={labelEyebrow}>Title</span>
          <Input autoFocus value={title} onChange={setTitle}
            placeholder="Short summary" mono={false} />
        </label>

        <label style={labelStyle}>
          <span className="mono" style={labelEyebrow}>Body (markdown, optional)</span>
          <Textarea value={body} onChange={setBody}
            rows={8}
            style={{ resize: 'vertical' }} />
        </label>

        <label style={labelStyle}>
          <span className="mono" style={labelEyebrow}>Labels (comma-separated, optional)</span>
          <Input value={labelsRaw} onChange={setLabelsRaw}
            placeholder="bug, ux, wontfix" mono={false} />
        </label>

        {error && <_GhErrorAlert message={error} />}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="quiet" onClick={onClose} type="button">Cancel</Button>
          <Button variant="primary" type="submit"
            icon={busy ? 'spinner' : 'plus'}
            style={titleError ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
            {busy ? 'Creating…' : 'Create issue'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ─────────────────────────────────────────────────────────────
// No-repo CTA — one-click "Create GitHub repo" so Issues can exist.
// Rendered by LibraryDetail on the Issues tab when the project/area has no
// remote yet AND issues.create_repo is enabled (gated server-side too).
// ─────────────────────────────────────────────────────────────
function LibraryCreateRepoCTA({ kind, name, libId }) {
  const effectiveLibId = libId || name;
  const apiBase = _ghUseMemo(() => _ghBaseUrl(kind, effectiveLibId), [kind, effectiveLibId]);
  const [busy, setBusy] = _ghUseState(false);
  const [error, setError] = _ghUseState(null);
  const [visibility, setVisibility] = _ghUseState('private');
  const toast = _ghToast();

  const create = _ghUseCallback(async () => {
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      await _ghFetchJson(apiUrl(`${apiBase}/create-repo`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ visibility }),
      });
      toast && toast('GitHub repo created — Issues enabled', 'ok');
      // /api/data refresh flips has_github_remote → the Issues list mounts.
      window.dispatchEvent(new CustomEvent('library:reload'));
    } catch (e) {
      setError(e.message || 'Create failed');
    } finally { setBusy(false); }
  }, [apiBase, busy, visibility, toast]);

  const isArea = kind === 'areas' || kind === 'area';
  return (
    <div style={{
      padding: 28, display: 'flex', flexDirection: 'column',
      alignItems: 'center', gap: 14, textAlign: 'center',
    }}>
      <Icon name="box" size={40} color="var(--fg-3)" />
      <div style={{ fontSize: 'var(--t-md)', fontWeight: 500, color: 'var(--fg)' }}>No GitHub repo yet</div>
      <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', maxWidth: 380, lineHeight: 1.5 }}>
        Create a repo for this {isArea ? 'area' : 'project'} to start tracking issues — runs
        {' '}git init + an initial commit, then gh repo create --source --push.
      </div>
      <_GhSegmented value={visibility} onChange={setVisibility}
        options={[{ k: 'private', l: 'Private' }, { k: 'public', l: 'Public' }]} />
      {error && <_GhErrorAlert message={error} />}
      <Button variant="primary" icon={busy ? 'spinner' : 'plus'} onClick={create} type="button"
        style={busy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
        {busy ? 'Creating…' : 'Create GitHub repo'}
      </Button>
    </div>
  );
}

Object.assign(window, { LibraryPRList, LibraryIssueList, CreateIssueDialog, LibraryCreateRepoCTA });
