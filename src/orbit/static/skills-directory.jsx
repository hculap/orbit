// skills-directory.jsx — /skills landing page.
//
// Card grid of every installed skill. Each card shows the icon (emoji from
// frontmatter, default 🧩), name, source badge, version, "Enabled on N
// agents" line, and an "Update available" pill when the registry knows
// upstream has moved past the local SHA.
//
// Header has an "Install" CTA which mounts <SkillsInstallModal>. The page
// listens for `skills:reload` so the modal (and the per-agent panel) can
// trigger a refresh without prop drilling.
//
// Click on a card → /skills/<name> via the existing router.
//
// API:
//   GET /api/skills → [{name, source, version, icon, description,
//                       agents_enabled_count, update_available}]

const { useMemo: _sdUseMemo, useState: _sdUseState, useEffect: _sdUseEffect, useCallback: _sdUseCallback } = React;

const _SD_DEFAULT_ICON = '🧩';

const _SD_SORT_OPTIONS = [
  { value: 'alpha', label: 'A–Z' },
  { value: 'recent', label: 'Recently installed' },
  { value: 'source', label: 'Source' },
];

const _SD_SOURCE_LABELS = {
  github: 'GitHub',
  'github-shorthand': 'GitHub',
  marketplace: 'Marketplace',
  zip: 'ZIP',
  custom: 'Custom',
  builtin: 'Built-in',
  local: 'Local',
};

function _sdSourceLabel(source) {
  return _SD_SOURCE_LABELS[source] || (source ? String(source) : 'Unknown');
}

function _sdSourceAccent(source) {
  if (source === 'custom' || source === 'builtin') return true;
  return false;
}

function _sdSortSkills(skills, sortBy) {
  const arr = [...skills];
  if (sortBy === 'recent') {
    arr.sort((a, b) => {
      const at = Number(a.installed_at || 0);
      const bt = Number(b.installed_at || 0);
      if (at !== bt) return bt - at;
      return String(a.name || '').localeCompare(String(b.name || ''));
    });
  } else if (sortBy === 'source') {
    arr.sort((a, b) => {
      const as = String(a.source || '~');
      const bs = String(b.source || '~');
      if (as !== bs) return as.localeCompare(bs);
      return String(a.name || '').localeCompare(String(b.name || ''));
    });
  } else {
    arr.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
  }
  return arr;
}

function _sdMatchesQuery(skill, q) {
  if (!q) return true;
  const haystack = [
    skill.name,
    skill.description,
    skill.body,
  ].map((v) => (typeof v === 'string' ? v.toLowerCase() : '')).join('\n');
  return haystack.includes(q);
}

// ─────────────────────────────────────────────────────────────
// Card
// ─────────────────────────────────────────────────────────────
function _SkillCard({ skill, onOpen }) {
  const icon = skill.icon || _SD_DEFAULT_ICON;
  const enabledCount = Number(skill.agents_enabled_count || 0);
  const enabledLabel = enabledCount === 0
    ? 'Not enabled on any agent'
    : enabledCount === 1
      ? 'Enabled on 1 agent'
      : `Enabled on ${enabledCount} agents`;
  return (
    <window.Card hover padding={18} onClick={onOpen} style={{ cursor: 'pointer' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
        <div style={{
          width: 44, height: 44, borderRadius: '50%',
          background: 'var(--accent-soft)', border: '1px solid var(--accent-line)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 22, lineHeight: 1, flexShrink: 0,
        }}>{icon}</div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, justifyContent: 'space-between' }}>
            <span style={{
              fontSize: 'var(--t-h3)', fontWeight: 500,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{skill.name}</span>
            {skill.version && (
              <span className="mono" style={{
                fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', flexShrink: 0,
              }}>{skill.version}</span>
            )}
          </div>
          {skill.description && (
            <div style={{
              marginTop: 6, fontSize: 'var(--t-cap)', color: 'var(--fg-2)', lineHeight: 1.45,
              display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}>{skill.description}</div>
          )}
          <div style={{ marginTop: 10, display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
            <window.Chip mono accent={_sdSourceAccent(skill.source)}>
              {_sdSourceLabel(skill.source)}
            </window.Chip>
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
              {enabledLabel}
            </span>
            {skill.update_available && (
              <window.Chip mono accent>
                <Icon name="arrow-up" size={11} /> update
              </window.Chip>
            )}
          </div>
        </div>
      </div>
    </window.Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Main view
// ─────────────────────────────────────────────────────────────
function SkillsDirectoryView({ compact }) {
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [skills, setSkills] = _sdUseState(null);
  const [error, setError] = _sdUseState(null);
  const [installOpen, setInstallOpen] = _sdUseState(false);
  const [sortBy, setSortBy] = _sdUseState('alpha');
  const [query, setQuery] = _sdUseState('');

  const load = _sdUseCallback(async () => {
    setError(null);
    try {
      const r = await fetch(apiUrl('/api/skills'));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const data = await r.json();
      setSkills(Array.isArray(data) ? data : (data && Array.isArray(data.skills) ? data.skills : []));
    } catch (e) {
      setError(e.message || 'failed to load skills');
      setSkills([]);
    }
  }, []);

  _sdUseEffect(() => { load(); }, [load]);

  _sdUseEffect(() => {
    const onReload = () => { load(); };
    window.addEventListener('skills:reload', onReload);
    return () => window.removeEventListener('skills:reload', onReload);
  }, [load]);

  const filtered = _sdUseMemo(() => {
    const q = query.trim().toLowerCase();
    const list = skills || [];
    if (!q) return list;
    return list.filter((s) => _sdMatchesQuery(s, q));
  }, [skills, query]);

  const sorted = _sdUseMemo(() => _sdSortSkills(filtered, sortBy), [filtered, sortBy]);

  const navigate = _sdUseCallback((name) => {
    if (!name) return;
    const path = `/skills/${encodeURIComponent(name)}`;
    if (router && typeof router.push === 'function') router.push(path);
    else { try { window.location.assign((window.HUB_BASE_PATH || '') + path); } catch (_) { /* ignore */ } }
  }, [router]);

  const onRescan = _sdUseCallback(async () => {
    try {
      const r = await fetch(apiUrl('/api/skills/rescan'), { method: 'POST' });
      if (!r.ok) throw new Error(`http ${r.status}`);
      toast && toast('Skills rescanned', 'ok');
      try { window.dispatchEvent(new CustomEvent('skills:reload')); } catch (_) { /* ignore */ }
    } catch (e) {
      toast && toast(`Rescan failed: ${e.message || 'error'}`, 'err');
    }
  }, [toast]);

  const headerActions = (
    <>
      <Input
        type="search"
        value={query}
        onChange={setQuery}
        placeholder="Search name, description, body…"
        ariaLabel="Search skills"
        icon="search"
        size="sm"
        style={{ minWidth: 240 }}
        inputStyle={{ fontSize: 13, fontFamily: 'inherit' }}
      />
      <Select
        value={sortBy}
        onChange={setSortBy}
        options={_SD_SORT_OPTIONS.map((opt) => ({ value: opt.value, label: `Sort: ${opt.label}` }))}
        size="sm"
      />
      <window.IconButton icon="refresh" label="Rescan" size={34} onClick={onRescan} />
      <window.Button variant="primary" icon="plus" onClick={() => setInstallOpen(true)}>
        Install
      </window.Button>
    </>
  );

  const isLoading = skills === null;
  const totalSkills = (skills || []).length;
  const filterActive = query.trim().length > 0;
  const isEmpty = !isLoading && sorted.length === 0;
  const emptyDueToFilter = !isLoading && totalSkills > 0 && filterActive && sorted.length === 0;

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <window.SectionHeader
        eyebrow="Installed"
        title="Skills"
        count={isLoading
          ? ''
          : (filterActive
            ? `${sorted.length} of ${totalSkills} match`
            : `${sorted.length} installed`)}
        actions={headerActions}
      />

      {error && (
        <div style={{ marginTop: 18 }}>
          <StatusBanner variant="err" label={`error: ${error}`} inline />
        </div>
      )}

      {isLoading && (
        <div style={{ marginTop: 24 }}>
          <Spinner label="Loading skills…" inline size={16} />
        </div>
      )}

      {emptyDueToFilter && (
        <div style={{ marginTop: 18 }}>
          <EmptyState
            message={`No skills match “${query.trim()}”.`}
            sub="Try a different term or clear the search."
            padded
          />
        </div>
      )}

      {isEmpty && !emptyDueToFilter && (
        <div style={{ marginTop: 28 }}>
          <EmptyState
            icon="sparkle"
            message="No skills installed yet"
            sub="Install one from a GitHub repo, a ZIP file, or ask the Global agent to scaffold one for you."
            action={
              <window.Button variant="primary" icon="plus" onClick={() => setInstallOpen(true)}>
                Install your first skill
              </window.Button>
            }
            centered
            padded
          />
        </div>
      )}

      {!isEmpty && !isLoading && (
        <div style={{
          marginTop: 18, display: 'grid', gap: 14,
          gridTemplateColumns: compact ? 'minmax(0, 1fr)' : 'repeat(auto-fill, minmax(280px, minmax(0, 1fr)))',
        }}>
          {sorted.map((s) => (
            <_SkillCard key={s.name} skill={s} onOpen={() => navigate(s.name)} />
          ))}
        </div>
      )}

      {installOpen && window.SkillsInstallModal && (
        <window.SkillsInstallModal
          onClose={() => setInstallOpen(false)}
          onInstalled={() => { /* skills:reload event fired by modal */ }}
        />
      )}
    </div>
  );
}

Object.assign(window, { SkillsDirectoryView });
