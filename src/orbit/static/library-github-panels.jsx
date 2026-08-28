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

const { useState: _ghUseState, useEffect: _ghUseEffect, useCallback: _ghUseCallback, useMemo: _ghUseMemo, useRef: _ghUseRef, useLayoutEffect: _ghUseLayoutEffect } = React;

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

// Measure an element's OWN width. `_ghUseCompact` reads window.innerWidth,
// which is the wrong box for anything inside this panel: the list table lives
// in one half of a two-column grid, inside a modal card capped at
// min(1700px, 96vw), inside a page that may also have a sidebar. At a 1000px
// window the viewport says "not compact" while the table it is deciding for is
// 448px wide. Everything that depends on how much room the TABLE has must ask
// the table.
//
// useLayoutEffect (not useEffect) so the first measurement lands before paint
// — otherwise every mount flashes the width-0 column set for one frame.
// ResizeObserver then catches window resizes, the grid re-flowing, the modal
// animating open, and the sidebar toggling, none of which a `resize` listener
// on window would see.
function _ghUseElementWidth() {
  const ref = _ghUseRef(null);
  const [width, setWidth] = _ghUseState(null);
  _ghUseLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => {
      const next = el.getBoundingClientRect().width;
      // Ignore sub-pixel churn: re-rendering per 0.3px would be a render loop
      // on any layout that reacts to its own width. (Nothing here does — the
      // wrapper is a block filling a `minmax(0,1fr)` track, so the column set
      // can't feed back into the measurement — but the guard is free.)
      setWidth((prev) => ((prev != null && Math.abs(prev - next) < 1) ? prev : next));
    };
    measure();
    if (typeof ResizeObserver !== 'function') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

// How many columns the issue table can afford, given the width it actually
// got. The fixed columns cost ~350px together (# 54 · Labels ~121 · Author 76
// · Updated 99) and the title cell is `maxWidth:0`, so it is the ONLY column
// the auto table algorithm lets yield — every pixel the metadata takes comes
// straight out of the one column the list exists to show. Uncapped that ends
// at 58px of title (the <th>'s min-content) at a 400px table: five characters
// and an ellipsis, i.e. not a scan surface, plus a residual 10-20px of clipped
// metadata hidden behind `.scroll-hide`'s invisible scrollbar.
//
// So the metadata yields first, in order of how little it earns its width in a
// personal repo: Author (almost always the same one person) below 700px, then
// Labels below 620px. That keeps the title at >=350px — the width the old hard
// `max-width:320px` span clamp used to guarantee — all the way down to a
// ~500px table, and >=235px down to the ~390px table a 880px window produces.
// Below that the drops have run out and the title shrinks, but by then the
// table is a phone and only three columns are left to share it.
function _ghIssueCols(width, compact) {
  // Not measured yet (first paint before the layout effect, or no layout at
  // all): fall back to the viewport heuristic so behaviour never gets WORSE
  // than it was, then the measurement corrects it in the same frame.
  if (width == null) return { labels: !compact, author: !compact };
  return { labels: width >= 620, author: width >= 700 };
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
    // Register on the shared Escape stack (overlay-stack.js). This panel can
    // now be nested inside a HOST that also closes on Escape — the session
    // overlay modal, which mounts LibraryIssueList — and every Escape handler
    // in this app is bound to `window`, so DOM nesting buys no precedence:
    // without the stack one keypress would close the detail AND the host.
    const S = (typeof window !== 'undefined' && window.HubOverlayStack) || null;
    const id = S ? S.push(S.nextId()) : null;
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      if (id && !S.isTop(id)) return;
      onClose && onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (id) S.pop(id);
    };
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
// Title cell: `maxWidth:0` lets the auto table algorithm shrink the cell below
// its content (so a long title can never inflate the table), `width` then
// hands it the leftover share, and the ellipsis clips at whatever that turns
// out to be — fluid, unlike the fixed 320px cap it replaces.
//
// The catch, and why _ghIssueCols exists: being the only yielding column also
// makes it the only LOSING one. Every other column takes its max-content and
// the title eats the entire deficit, so "fluid" degrades to 58px (the <th>'s
// min-content) long before the layout looks tight. The elasticity is only an
// improvement if something else is made to yield first — hence the measured
// column budget. There is deliberately no `minWidth` floor here: a floor would
// just push the metadata back out past the right edge, behind the invisible
// `.scroll-hide` scrollbar, which is the failure mode this replaced.
//
// `width:100%` (not a 55% share) is the other half of the same idea. A
// declared percentage is a CEILING as well as a claim: at 55% the title took
// 319px of a 580px table and the 108px of slack went into padding out `#`
// (54→92px) and Updated (99→169px) — a four-character issue number given a
// third of the money the title was starved of. 100% + the `width:1%` siblings
// below is the standard one-elastic-column table idiom: everyone else shrinks
// to max-content, the remainder is the title's.
const _ghTdTitle = {
  ..._ghTd,
  width: '100%', maxWidth: 0,
  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
};
// `width:1%` reads as "shrink to max-content" to the auto table algorithm once
// some other cell asks for 100%; nowrap keeps a date or a login off a second
// line. Safe only BECAUSE the title cell is maxWidth:0 — these win the width
// they need and the elastic title absorbs the difference.
const _ghTdTight = { ..._ghTd, width: '1%', whiteSpace: 'nowrap' };
// The `#` cell wants the same treatment but is NOT shared with the PR table
// (which still uses the plain _ghTd for all five of its columns).
const _ghTdNum = { ..._ghTd, width: '1%', whiteSpace: 'nowrap' };

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
  onToggleState, togglingState, onMakeTask, makingTask, onSendToSession, renderEditor, extras,
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
        {/* minWidth:0 — a flex item floors at min-content too, and this
            nowrap title measured 554px of min-content on its own (613px for
            the whole header row) against a 518px fair share: `overflow:hidden`
            does NOT shrink it, it only clips what already claimed the space. */}
        <span style={{
          flex: 1, minWidth: 0, fontSize: 'var(--t-md)', color: 'var(--fg)', fontWeight: 500,
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
          {/* Send-to-agent is OPT-IN via the callback, not a flag: this panel
              also renders as the library-detail Issues TAB, where no session is
              on screen and firing the paste would land invisibly in whatever
              session happens to be active. Only the session overlay modal —
              which already knows its sessionId — passes onSendToSession, so
              the full-page tab is unchanged by construction. */}
          {/* One-word labels cannot carry the whole distinction, so `title`
              (hover) + `ariaLabel` (screen readers) spell it out. Both only
              started reaching the DOM once Button's closed destructure learned
              about them — see components.jsx. On touch, where neither shows,
              the pasted prompt itself opens with the distinction and is NOT
              auto-submitted (/term/paste does not press Enter), so the user
              still reads it before it runs. */}
          {isIssue && onSendToSession && (
            <Button size="sm" variant="ghost" icon="zap" type="button"
              onClick={() => onSendToSession('solve')}
              title="Wklej do sesji agenta polecenie: rozwiąż to issue (zaplanuj i zaimplementuj)"
              ariaLabel="Rozwiąż — wklej do sesji agenta polecenie zaimplementowania tego issue">
              Rozwiąż
            </Button>
          )}
          {isIssue && onSendToSession && (
            <Button size="sm" variant="ghost" icon="sparkle" type="button"
              onClick={() => onSendToSession('refine')}
              title="Wklej do sesji agenta polecenie: dopracuj OPIS tego issue (bez implementacji)"
              ariaLabel="Dopracuj — wklej do sesji agenta polecenie poprawienia opisu tego issue, bez pisania kodu">
              Dopracuj
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
      {/* minWidth:0 is the same fix as minmax(0,…) applied from the item
          side (measured identical) — kept as belt-and-braces so a future
          container tweak can't quietly reintroduce the min-content floor. */}
      <Card padding={0} style={compact ? { minWidth: 0 } : { alignSelf: 'start', minWidth: 0 }}>
        <_GhPanelHeader title="Pull requests"
          stateFilter={ctl.stateFilter} setStateFilter={ctl.setStateFilter}
          onReload={ctl.reload} />
        <_GhPRTable items={ctl.items} loading={ctl.loading} error={ctl.error}
          onOpen={ctl.openDetail} selectedNum={ctl.selectedNum} onRetry={ctl.reload} />
      </Card>

      {!compact && (
        <Card padding={0} style={{ minWidth: 0 }}>
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
function _GhIssueTable({ items, loading, error, onOpen, selectedNum, onRetry, emptyLabel, compact }) {
  // The measuring div is the ROOT of every branch, loading/error/empty
  // included — if it only wrapped the populated table it would unmount on each
  // Open/Closed/All flip, the width would fall back to null, and the column
  // set would flicker on the way back in.
  const [wrapRef, wrapW] = _ghUseElementWidth();
  const cols = _ghIssueCols(wrapW, compact);
  let inner;
  if (loading) {
    inner = <_GhLoading />;
  } else if (error) {
    inner = <div style={{ padding: 14 }}><_GhErrorAlert message={error} onRetry={onRetry} /></div>;
  } else if (!items || items.length === 0) {
    // emptyLabel distinguishes "this repo has no open issues" from "your
    // query matched none of them" — same empty box, very different fix.
    inner = <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>{emptyLabel || 'No issues.'}</div>;
  } else {
    inner = (
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
        <thead>
          {/* Labels + Author are not optional decoration — they are what the
              elastic (maxWidth:0) title cell has to pay for. Dropping them is
              keyed on the TABLE's measured width, never the viewport: at a
              1000px window this table is 448px inside the issues modal and the
              title had collapsed to 96px with all five columns up. See
              _ghIssueCols for the budget. */}
          <tr style={{ color: 'var(--fg-3)' }}>
            <th style={_ghTh}>#</th><th style={_ghTh}>Title</th>
            {cols.labels && <th style={_ghTh}>Labels</th>}
            {cols.author && <th style={_ghTh}>Author</th>}
            <th style={_ghTh}>Updated</th>
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
                <td style={_ghTdNum} className="mono">#{it.number}</td>
                {/* The truncation lives on the CELL, not on the span, and the
                    cap is `max-width:0` + a percentage width instead of a hard
                    320px. Two wins: the title stops contributing its full
                    min-content to the table (it was the reason the list column
                    over-asked 689px and scrolled behind an invisible
                    `.scroll-hide` scrollbar), and the visible length now GROWS
                    with the modal instead of ellipsizing at 320px no matter how
                    much room there is. */}
                <td style={_ghTdTitle} title={it.title || undefined}>
                  <span style={{ color: 'var(--fg)' }}>{it.title || '—'}</span>
                </td>
                {cols.labels && (
                  <td style={_ghTd}>
                    <span style={{ display: 'inline-flex', gap: 4, flexWrap: 'wrap' }}>
                      {labels.slice(0, 4).map((l, i) => <_GhLabelChip key={(l && l.name) || i} label={l} />)}
                      {labels.length > 4 && (
                        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>+{labels.length - 4}</span>
                      )}
                    </span>
                  </td>
                )}
                {cols.author && (
                  <td style={_ghTdTight} className="mono"><span style={{ color: 'var(--fg-2)' }}>{_ghAuthorName(it.author) || '—'}</span></td>
                )}
                <td style={_ghTdTight} className="mono"><span style={{ color: 'var(--fg-3)' }}>{_ghRelTime(it.updatedAt)}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    );
  }
  return (
    <div ref={wrapRef} style={{ overflowX: 'auto' }} className="scroll-hide">
      {inner}
    </div>
  );
}

// `onSendToSession(text)` is a CAPABILITY prop, not a flag: pass it and the
// detail pane grows the two send-to-agent buttons, omit it and this panel is
// byte-identical to what library-detail's Issues tab has always rendered.
// Only the caller that actually has a live session on screen (the session
// overlay modal) passes it — this component never learns the orchestrator
// exists, it just hands back a finished prompt string.
function LibraryIssueList({ kind, name, libId, onSendToSession }) {
  const effectiveLibId = libId || name;
  const ctl = _useGhListPanel({ kind, libId: effectiveLibId, resource: 'issues' });
  const compact = _ghUseCompact();
  const [query, setQuery] = _ghUseState('');
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

  // Search is a pure CLIENT-side filter over the rows already fetched — the
  // state filter (open/closed/all) is a server round trip, so the two compose
  // by construction: gh narrows, then the query narrows what came back. No
  // debounce and no refetch per keystroke; that is the house idiom (gallery,
  // tasks.jsx, skills-directory.jsx) and 50 rows do not need one.
  // window.* is read at RENDER time — issue-search.js is a plain <script>, so
  // it is there, but never assume load order at module eval.
  const searchApi = (typeof window !== 'undefined' && window.HubIssueSearch) || null;
  const visibleItems = _ghUseMemo(
    () => (searchApi ? searchApi.filterIssues(ctl.items, query) : ctl.items),
    [searchApi, ctl.items, query],
  );
  const queryActive = query.trim().length > 0;

  // Builds the prompt from the DETAIL payload (the list has a truncated body
  // and no guarantee of a url) and hands it to the caller. Dispatching is the
  // caller's job and it CLOSES THE OVERLAY, which unmounts this tree — so the
  // call is the last statement and nothing sets state after it.
  const sendToSession = _ghUseCallback((mode) => {
    if (typeof onSendToSession !== 'function') return;
    const data = (ctl.detail && ctl.detail.data) ? ctl.detail.data : null;
    if (!data) return;
    const prompts = (typeof window !== 'undefined' && window.HubIssuePrompt) || null;
    const text = prompts ? prompts.buildPrompt(mode, data) : '';
    // Better to no-op than to paste a half-built instruction into a live pane.
    if (!text) { ctl.toast && ctl.toast('Nie udało się zbudować polecenia', 'err'); return; }
    onSendToSession(text);
  }, [ctl.detail, ctl.toast, onSendToSession]);

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
      onSendToSession={onSendToSession ? sendToSession : null}
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
      {/* minWidth:0 — see the note in LibraryPRList. */}
      <Card padding={0} style={compact ? { minWidth: 0 } : { alignSelf: 'start', minWidth: 0 }}>
        <_GhPanelHeader title="Issues"
          stateFilter={ctl.stateFilter} setStateFilter={ctl.setStateFilter}
          onReload={ctl.reload}
          compact={compact}
          search={(
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
              <Input type="search" value={query} onChange={setQuery}
                placeholder="Szukaj #, tytuł, treść…" ariaLabel="Szukaj w issues"
                icon="search" size="sm"
                style={{ flex: 1, minWidth: 0 }}
                inputStyle={{ fontSize: 13, fontFamily: 'inherit' }} />
              {/* `!ctl.loading` is load-bearing. `reload()` sets loading but
                  deliberately does NOT clear `items` (clearing would blank the
                  table on every Refresh), so during the ~0.4-1.2s gh round
                  trip after an Open/Closed/All flip both operands still
                  describe the PREVIOUS state — the chip read "2 z 18" for the
                  better part of a second next to a table already showing the
                  spinner. The table drops the stale rows; the count has to
                  agree with the table, not with the state behind it. Same hole
                  applied to every other reload() caller (Refresh, save,
                  create), which is why the guard sits on the chip. */}
              {queryActive && !ctl.loading && (
                <span className="mono" style={{
                  fontSize: 'var(--t-xs)', color: 'var(--fg-3)', whiteSpace: 'nowrap',
                }}>{visibleItems.length} z {ctl.items.length}</span>
              )}
            </div>
          )}
          extra={(
            <Button size="sm" variant="primary" icon="plus"
              onClick={() => setCreateOpen(true)} type="button">New issue</Button>
          )} />
        <_GhIssueTable items={visibleItems} loading={ctl.loading} error={ctl.error}
          onOpen={ctl.openDetail} selectedNum={ctl.selectedNum} onRetry={ctl.reload} compact={compact}
          emptyLabel={queryActive ? 'Nic nie pasuje do zapytania (szukam tylko w pobranych issues).' : undefined} />
      </Card>

      {!compact && (
        <Card padding={0} style={{ minWidth: 0 }}>
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
    // minmax(0,1fr), NOT a bare `1fr`. A grid item carries an implicit
    // `min-width:auto`, so each track's base size floors at its item's
    // MIN-CONTENT — one wide child (a fenced code block, a markdown table, a
    // bare long URL) inflates its own track and the two stop being equal:
    // measured 691px + 956px inside a 1098px modal, i.e. 587px of sideways
    // scroll on the MODAL BODY while the Card itself clipped the overflow
    // (overflow:hidden) so there was nothing to grab. With a DEFINITE 0
    // minimum the tracks stay equal and the wide child scrolls inside its own
    // box, which is what `.md-render pre { overflow-x:auto }` always intended.
    // (The tell that this is the bug and not a too-narrow modal: a healthy
    // `1fr 1fr` is always EQUAL. Widening alone only papers over it — at
    // 1698px the shipped grid cleared by ~11px of subpixel luck.)
    gridTemplateColumns: compact ? undefined : 'minmax(0, 1fr) minmax(0, 1fr)',
    minHeight: 0,
  };
}

function _GhPanelHeader({ title, stateFilter, setStateFilter, onReload, extra, search, compact }) {
  return (
    <div style={{
      padding: '12px 14px', borderBottom: '1px solid var(--hairline)',
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
    }}>
      <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>{title}</span>
      {/* The search box REPLACES the bare spacer instead of being appended:
          measured, this row has ~216px of slack on desktop and stays one line,
          but at ≤900px it ALREADY wraps to two and the spacer collapses to
          ~0 — an inline input there would just make a third ragged wrap. On
          compact it takes a full-width line of its own instead. */}
      {search
        ? <div style={{ flex: compact ? '1 1 100%' : '1 1 180px', minWidth: 0 }}>{search}</div>
        : <span style={{ flex: 1 }} />}
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

  // closeOnEscape: this dialog can open INSIDE the session overlay modal,
  // which also closes on Escape — the shared stack (overlay-stack.js) makes
  // one keypress peel exactly one layer. Modal's handler skips Escape while an
  // INPUT/TEXTAREA has focus, so a half-written title/body is never lost.
  return (
    <Modal open={true} onClose={onClose} title="New issue" width={520} closeOnEscape>
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
