// library-link-picker.jsx — multi-select chip picker for area↔project links.
//
// One component handles both directions:
//   - For an Area:    candidates = projects, value = item.linked_projects
//   - For a Project:  candidates = areas,    value = item.linked_areas
//
// Each candidate is a chip; clicking toggles the link via:
//   POST   /api/library/links  { area, project }
//   DELETE /api/library/links  { area, project }
// After the response succeeds, calls onChange(newLinkedNames) so the parent
// can update its in-memory copy of the item without a full refetch.
//
// Phase 1 deliberately keeps this UI dense — no submit button, no draft
// state — every click is a live mutation, mirroring how Sync renames work.

const { useState: _llpUseState, useMemo: _llpUseMemo } = React;

function _llpLibId(item) {
  // Backend's link helpers walk all areas/projects to match — they accept
  // either the bare basename (legacy) or the full relative path under
  // ~/Areas / ~/Projects. Prefer lib_id for nested projects (e.g.
  // "parent/child"). Fall back to name for old payloads.
  return (item && (item.lib_id || item.name)) || '';
}

function _llpBuildPayload(kind, item, candidateLibId) {
  // The backend always wants {area, project}. We just figure out which side
  // is fixed (the item we're editing) and which side is the candidate.
  if (kind === 'area') {
    return { area: _llpLibId(item), project: candidateLibId };
  }
  return { area: candidateLibId, project: _llpLibId(item) };
}

function LibraryLinkPicker({ kind, item, candidates, value, onChange }) {
  const [filter, setFilter] = _llpUseState('');
  const [pending, setPending] = _llpUseState(() => new Set());
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  const linked = _llpUseMemo(() => new Set(Array.isArray(value) ? value : []), [value]);
  const list = _llpUseMemo(() => {
    const arr = Array.isArray(candidates) ? candidates.slice() : [];
    arr.sort((a, b) => {
      const an = (a.label || a.name || '').toLowerCase();
      const bn = (b.label || b.name || '').toLowerCase();
      return an.localeCompare(bn);
    });
    return arr;
  }, [candidates]);

  const filtered = _llpUseMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return list;
    return list.filter((c) => {
      const label = (c.label || c.name || '').toLowerCase();
      return label.includes(q);
    });
  }, [list, filter]);

  const toggle = async (candidate) => {
    if (!candidate || !candidate.name) return;
    const cname = candidate.name;
    if (pending.has(cname)) return;
    const wasLinked = linked.has(cname);
    const method = wasLinked ? 'DELETE' : 'POST';
    // URL/payload uses lib_id so nested projects (e.g. "parent/child")
    // resolve correctly. The local `linked` Set still holds basenames
    // because that's what the backend stores in linked_projects/linked_areas
    // (symlink basename in ~/Areas/<a>/projects/).
    const candidateLibId = _llpLibId(candidate);
    const body = JSON.stringify(_llpBuildPayload(kind, item, candidateLibId));
    // Mark pending — block double-clicks.
    setPending((s) => {
      const next = new Set(s);
      next.add(cname);
      return next;
    });
    try {
      const r = await fetch(apiUrl('/api/library/links'), {
        method,
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (!r.ok) {
        const text = await r.text().catch(() => '');
        throw new Error(`http ${r.status}${text ? ': ' + text.slice(0, 80) : ''}`);
      }
      // Compute new linked list immutably and hand it to the parent.
      const nextLinked = wasLinked
        ? Array.from(linked).filter((n) => n !== cname)
        : [...Array.from(linked), cname];
      onChange && onChange(nextLinked);
      // Hint the rest of the app that the library state changed.
      window.dispatchEvent(new CustomEvent('library:reload'));
    } catch (err) {
      console.error('link toggle failed', err);
      toast && toast(`Link ${wasLinked ? 'remove' : 'add'} failed`, 'err');
    } finally {
      setPending((s) => {
        const next = new Set(s);
        next.delete(cname);
        return next;
      });
    }
  };

  const otherKindLabel = kind === 'area' ? 'projects' : 'areas';
  const otherKindIcon = kind === 'area' ? '📦' : '📂';

  if (!list.length) {
    return (
      <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
        no {otherKindLabel} available — create one first
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <SearchInput
        value={filter}
        onChange={setFilter}
        placeholder={`filter ${otherKindLabel}…`}
      />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {filtered.map((c) => {
          const cname = c.name;
          const isLinked = linked.has(cname);
          const isPending = pending.has(cname);
          return (
            <span
              key={cname}
              onClick={() => toggle(c)}
              className="mono"
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                padding: '4px 10px', borderRadius: 'var(--r-pill)',
                fontSize: 'var(--t-cap)', lineHeight: '14px',
                background: isLinked ? 'var(--accent-soft)' : 'var(--surface-2)',
                color: isLinked ? 'var(--accent)' : 'var(--fg-2)',
                border: '1px solid ' + (isLinked ? 'var(--accent-line)' : 'var(--hairline)'),
                cursor: isPending ? 'wait' : 'pointer',
                opacity: isPending ? 0.55 : 1,
                whiteSpace: 'nowrap',
                transition: 'background .12s, border-color .12s, color .12s',
              }}
            >
              <span>{otherKindIcon}</span>
              <span>{c.label || c.name}</span>
              {isLinked && <span style={{ marginLeft: 2, fontSize: 'var(--t-2xs)' }}>✓</span>}
            </span>
          );
        })}
        {filtered.length === 0 && (
          <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>no matches</span>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { LibraryLinkPicker });
