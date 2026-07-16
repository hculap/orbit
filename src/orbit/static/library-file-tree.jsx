// library-file-tree.jsx — recursive lazy-loaded directory tree for the
// Areas / Projects library detail view. Each directory fetches its own
// children on first expansion so we never preload the whole tree.
//
// Endpoint: GET /api/library/{kind}/{name}/tree?rel=<path>
//   → { items: [{ name, type, size, mtime, target? }] } where type ∈ {dir,file,link}
//
// Symlinks render as `🔗 name → target` and are NOT traversable in this
// component (Phase 1 keeps things simple — symlinks point at peer items
// in the library, which the user navigates to via the link picker).

const { useState: _lftUseState, useEffect: _lftUseEffect, useMemo: _lftUseMemo } = React;

// Filter dotfiles out of the tree by default. Phase 1 has no toggle UI —
// callers can opt-in via showHidden if they ever need to.
function _lftVisibleItems(items, showHidden) {
  if (showHidden) return items;
  return items.filter((it) => !(it.name && it.name.startsWith('.')));
}

function _lftSortItems(items) {
  // Directories first, then files, then symlinks; alphabetical within group.
  const order = { dir: 0, file: 1, link: 2 };
  const out = items.slice();
  out.sort((a, b) => {
    const oa = order[a.type] ?? 3;
    const ob = order[b.type] ?? 3;
    if (oa !== ob) return oa - ob;
    return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
  });
  return out;
}

// Encode each '/'-separated segment so nested project lib_ids
// (e.g. "parent/child") survive while structural slashes pass
// through to FastAPI's {name:path} parameter.
function _lftEncodeLibId(libId) {
  return String(libId || '').split('/').map(encodeURIComponent).join('/');
}

function LibraryFileTree({ kind, name, libId, rel = '', onOpen, depth = 0, showHidden = false, autoExpand = false }) {
  const [items, setItems] = _lftUseState(null);
  const [loading, setLoading] = _lftUseState(false);
  const [error, setError] = _lftUseState(null);
  // Top-level (depth 0) is always expanded so the user sees something on mount.
  // Nested directories start collapsed and expand lazily.
  const [expanded, setExpanded] = _lftUseState(depth === 0 || autoExpand);

  // Prefer libId (relative path); fall back to name for legacy callers.
  const effectiveLibId = libId || name;

  const url = _lftUseMemo(() => {
    const base = `/api/library/${encodeURIComponent(kind)}/${_lftEncodeLibId(effectiveLibId)}/tree`;
    const qs = rel ? `?rel=${encodeURIComponent(rel)}` : '';
    return apiUrl(base + qs);
  }, [kind, effectiveLibId, rel]);

  _lftUseEffect(() => {
    if (!expanded || items != null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`))))
      .then((data) => {
        if (cancelled) return;
        const list = Array.isArray(data && data.items) ? data.items : [];
        setItems(_lftSortItems(_lftVisibleItems(list, showHidden)));
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message || 'failed to load');
        setItems([]);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [url, expanded, showHidden, items]);

  const indent = depth * 14;

  if (depth === 0) {
    // Root: just render the children list (no folder row for the root itself).
    return (
      <div style={{ fontSize: 'var(--t-sm)', lineHeight: 1.6 }}>
        {loading && <div className="mono" style={{ padding: '6px 8px', color: 'var(--fg-4)' }}>loading…</div>}
        {error && <div className="mono" style={{ padding: '6px 8px', color: 'var(--err)' }}>error: {error}</div>}
        {!loading && !error && items && items.length === 0 && (
          <div className="mono" style={{ padding: '6px 8px', color: 'var(--fg-4)' }}>(empty)</div>
        )}
        {!loading && items && items.map((it) => (
          <_LftRow
            key={it.name}
            item={it}
            kind={kind}
            libId={effectiveLibId}
            parentRel={rel}
            depth={depth + 1}
            onOpen={onOpen}
            showHidden={showHidden}
          />
        ))}
      </div>
    );
  }

  return null; // Non-root nodes render via _LftRow.
}

function _LftRow({ item, kind, libId, parentRel, depth, onOpen, showHidden }) {
  const [open, setOpen] = _lftUseState(false);
  const fullRel = parentRel ? `${parentRel}/${item.name}` : item.name;
  const isDir = item.type === 'dir';
  const isLink = item.type === 'link';
  const indent = (depth - 1) * 14;

  const onClick = () => {
    if (isDir) {
      setOpen((o) => !o);
    } else if (isLink) {
      // Phase 1: symlinks don't recurse, but if the user clicks we still
      // hand the path up so the parent can decide what to do (open as file
      // or jump to the linked item).
      onOpen && onOpen(fullRel, item);
    } else {
      onOpen && onOpen(fullRel, item);
    }
  };

  const icon = isDir
    ? (open ? '▾' : '▸')
    : (isLink ? '🔗' : '·');

  return (
    <div>
      <div
        onClick={onClick}
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '4px 8px', paddingLeft: 8 + indent,
          cursor: 'pointer', borderRadius: 'var(--r-sm)',
          color: isLink ? 'var(--fg-3)' : 'var(--fg-2)',
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = 'var(--surface-2)'; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
      >
        <span className="mono" style={{ width: 12, color: 'var(--fg-4)', flexShrink: 0, textAlign: 'center' }}>{icon}</span>
        <span className="mono" style={{
          fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1,
        }}>
          {item.name}
          {isLink && item.target && (
            <span style={{ color: 'var(--fg-4)', marginLeft: 8 }}>→ {item.target}</span>
          )}
        </span>
        {!isDir && typeof item.size === 'number' && (
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--fg-4)', flexShrink: 0 }}>
            {fmtBytes(item.size)}
          </span>
        )}
      </div>
      {isDir && open && (
        <_LftChildren
          kind={kind}
          libId={libId}
          rel={fullRel}
          depth={depth}
          onOpen={onOpen}
          showHidden={showHidden}
        />
      )}
    </div>
  );
}

// Helper that fetches one directory level and renders its rows. Kept separate
// from LibraryFileTree (the root) so the per-row state doesn't get tangled
// with the top-level autoExpand logic.
function _LftChildren({ kind, libId, rel, depth, onOpen, showHidden }) {
  const [items, setItems] = _lftUseState(null);
  const [loading, setLoading] = _lftUseState(false);
  const [error, setError] = _lftUseState(null);

  const url = _lftUseMemo(() => {
    const base = `/api/library/${encodeURIComponent(kind)}/${_lftEncodeLibId(libId)}/tree`;
    const qs = rel ? `?rel=${encodeURIComponent(rel)}` : '';
    return apiUrl(base + qs);
  }, [kind, libId, rel]);

  _lftUseEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`))))
      .then((data) => {
        if (cancelled) return;
        const list = Array.isArray(data && data.items) ? data.items : [];
        setItems(_lftSortItems(_lftVisibleItems(list, showHidden)));
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message || 'failed to load');
        setItems([]);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [url, showHidden]);

  const indent = depth * 14;

  if (loading) {
    return (
      <div className="mono" style={{ padding: '4px 8px', paddingLeft: 8 + indent, color: 'var(--fg-4)', fontSize: 'var(--t-xs)' }}>
        loading…
      </div>
    );
  }
  if (error) {
    return (
      <div className="mono" style={{ padding: '4px 8px', paddingLeft: 8 + indent, color: 'var(--err)', fontSize: 'var(--t-xs)' }}>
        error: {error}
      </div>
    );
  }
  if (!items || items.length === 0) {
    return (
      <div className="mono" style={{ padding: '4px 8px', paddingLeft: 8 + indent, color: 'var(--fg-4)', fontSize: 'var(--t-xs)' }}>
        (empty)
      </div>
    );
  }
  return (
    <div>
      {items.map((it) => (
        <_LftRow
          key={it.name}
          item={it}
          kind={kind}
          libId={libId}
          parentRel={rel}
          depth={depth + 1}
          onOpen={onOpen}
          showHidden={showHidden}
        />
      ))}
    </div>
  );
}

Object.assign(window, { LibraryFileTree });
