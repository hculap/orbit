// secrets-view.jsx — top-level /secrets page.
//
// Sections:
//   1. Header with eyebrow="Manage", title="Env & secrets", count, search +
//      scope-selector dropdown.
//   2. Globalne: three cards (.env, .secrets/, .ssh) → opens the matching
//      <EntryList> for the global scope.
//   3. Cross-scope inventory: GET /api/secrets/scopes → list of every Area
//      and Project; each renders a small EntryList that links to the
//      library-detail secrets tab on row click.
//
// Header search filters all visible entries client-side (live, not via API)
// using the same shape as skills-directory.jsx _sdMatchesQuery.
//
// All shared primitives come from secrets-components.jsx via window.*.

const {
  useState: _secvUseState,
  useEffect: _secvUseEffect,
  useMemo: _secvUseMemo,
  useCallback: _secvUseCallback,
} = React;

const _SECV_GLOBAL_KINDS = [
  { id: 'env', label: '.env', icon: 'file', desc: 'Flat KEY=VALUE secrets in ~/.env.' },
  { id: 'files', label: '.secrets/', icon: 'folder', desc: 'Opaque files: service-account JSON, raw token files.' },
  { id: 'ssh', label: '.ssh', icon: 'cog', desc: 'Keypairs, authorized_keys, known_hosts.' },
];

function _secvMatchesQuery(item, q) {
  if (!q) return true;
  const haystack = [
    item && item.key,
    item && item.name,
    item && item.host,
    item && item.comment,
    item && item.type,
    item && item.masked,
    item && item.fingerprint,
    item && (item.idx != null ? `#${item.idx}` : ''),
  ].map((v) => (typeof v === 'string' ? v.toLowerCase() : '')).join('\n');
  return haystack.includes(q);
}

// ─────────────────────────────────────────────────────────────
// _GlobalTabs — tab strip matching the library-detail tab vocabulary.
// ─────────────────────────────────────────────────────────────
function _GlobalTabs({ active, onChange, counts }) {
  return (
    <div style={{
      display: 'flex', gap: 4,
      borderBottom: '1px solid var(--hairline)',
      overflowX: 'auto',
    }} className="scroll-hide">
      {_SECV_GLOBAL_KINDS.map((kd) => {
        const isActive = active === kd.id;
        const count = counts[kd.id];
        return (
          <button
            key={kd.id}
            onClick={() => onChange(kd.id)}
            style={{
              padding: '10px 14px', background: 'transparent', border: 'none', cursor: 'pointer',
              color: isActive ? 'var(--fg)' : 'var(--fg-3)',
              fontSize: 'var(--t-sm)', fontWeight: 500, fontFamily: 'inherit',
              borderBottom: '2px solid ' + (isActive ? 'var(--accent)' : 'transparent'),
              marginBottom: -1,
              display: 'inline-flex', alignItems: 'center', gap: 8,
            }}
          >
            <span className="mono">{kd.label}</span>
            {count != null && (
              <span className="mono" style={{
                fontSize: 'var(--t-xs)', color: isActive ? 'var(--fg-3)' : 'var(--fg-4)',
                padding: '1px 6px', borderRadius: 'var(--r-sm)',
                background: 'var(--surface-2)', border: '1px solid var(--hairline)',
              }}>{count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// _GlobalEnvSection — expanded panel for global .env entries.
// ─────────────────────────────────────────────────────────────
function _GlobalEnvSection({ query, onCount }) {
  const [items, setItems] = _secvUseState(null);
  const [loading, setLoading] = _secvUseState(false);
  const [error, setError] = _secvUseState(null);
  const [activeReveal, setActiveReveal] = _secvUseState(null);
  const [activeEdit, setActiveEdit] = _secvUseState(null);
  const [activeDelete, setActiveDelete] = _secvUseState(null);
  const [addOpen, setAddOpen] = _secvUseState(false);

  const load = _secvUseCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await window._secFetchJson('/api/secrets/global/env');
      // Backend now returns {has_env, items: [...]} (was a bare array).
      // Tolerate both shapes so older deployments still render.
      const arr = Array.isArray(data)
        ? data
        : (data && Array.isArray(data.items) ? data.items : []);
      setItems(arr);
    } catch (e) {
      setError(e.message || 'failed');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  _secvUseEffect(() => { load(); }, [load]);

  const filtered = _secvUseMemo(() => {
    const list = items || [];
    if (!query) return list;
    return list.filter((it) => _secvMatchesQuery(it, query));
  }, [items, query]);

  _secvUseEffect(() => {
    if (typeof onCount === 'function') onCount(items ? items.length : 0);
  }, [items, onCount]);

  return (
    <>
      <window.EntryList
        scope="global"
        kind="env"
        title="Global .env"
        items={filtered}
        loading={loading}
        error={error}
        onReload={load}
        onAdd={() => setAddOpen(true)}
        onReveal={(it) => setActiveReveal(it)}
        onEdit={(it) => setActiveEdit(it)}
        onDelete={(it) => setActiveDelete(it)}
      />
      {activeReveal && (
        <window.RevealModal
          open={true}
          onClose={() => setActiveReveal(null)}
          scope="global"
          kind="env"
          item={activeReveal}
        />
      )}
      {activeEdit && (
        <window.EditModal
          open={true}
          onClose={() => setActiveEdit(null)}
          scope="global"
          kind="env"
          item={activeEdit}
          onSaved={load}
        />
      )}
      {activeDelete && (
        <window.DeleteModal
          open={true}
          onClose={() => setActiveDelete(null)}
          scope="global"
          kind="env"
          item={activeDelete}
          onDeleted={load}
        />
      )}
      {addOpen && (
        <window.AddEntryModal
          open={true}
          onClose={() => setAddOpen(false)}
          scope="global"
          kind="env"
          onAdded={load}
        />
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────
// _GlobalFilesSection — expanded panel for global .secrets/ entries.
// ─────────────────────────────────────────────────────────────
function _GlobalFilesSection({ query, onCount }) {
  const [items, setItems] = _secvUseState(null);
  const [loading, setLoading] = _secvUseState(false);
  const [error, setError] = _secvUseState(null);
  const [activeReveal, setActiveReveal] = _secvUseState(null);
  const [activeEdit, setActiveEdit] = _secvUseState(null);
  const [activeDelete, setActiveDelete] = _secvUseState(null);
  const [addOpen, setAddOpen] = _secvUseState(false);

  const load = _secvUseCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await window._secFetchJson('/api/secrets/global/files');
      // Backend now returns {has_secrets_dir, items: [...]} (was a bare array).
      const arr = Array.isArray(data)
        ? data
        : (data && Array.isArray(data.items) ? data.items : []);
      setItems(arr);
    } catch (e) {
      setError(e.message || 'failed');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  _secvUseEffect(() => { load(); }, [load]);

  const filtered = _secvUseMemo(() => {
    const list = items || [];
    if (!query) return list;
    return list.filter((it) => _secvMatchesQuery(it, query));
  }, [items, query]);

  _secvUseEffect(() => {
    if (typeof onCount === 'function') onCount(items ? items.length : 0);
  }, [items, onCount]);

  return (
    <>
      <window.EntryList
        scope="global"
        kind="files"
        title="Global ~/.secrets/"
        items={filtered}
        loading={loading}
        error={error}
        onReload={load}
        onAdd={() => setAddOpen(true)}
        onReveal={(it) => setActiveReveal(it)}
        onEdit={(it) => setActiveEdit(it)}
        onDelete={(it) => setActiveDelete(it)}
      />
      {activeReveal && (
        <window.RevealModal
          open={true}
          onClose={() => setActiveReveal(null)}
          scope="global"
          kind="files"
          item={activeReveal}
        />
      )}
      {activeEdit && (
        <window.EditModal
          open={true}
          onClose={() => setActiveEdit(null)}
          scope="global"
          kind="files"
          item={activeEdit}
          onSaved={load}
        />
      )}
      {activeDelete && (
        <window.DeleteModal
          open={true}
          onClose={() => setActiveDelete(null)}
          scope="global"
          kind="files"
          item={activeDelete}
          onDeleted={load}
        />
      )}
      {addOpen && (
        <window.AddEntryModal
          open={true}
          onClose={() => setAddOpen(false)}
          scope="global"
          kind="files"
          onAdded={load}
        />
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────
// _GlobalSshSection — expanded panel for ~/.ssh entries.
// ─────────────────────────────────────────────────────────────
function _GlobalSshSection({ query, onCount }) {
  const [data, setData] = _secvUseState(null);
  const [loading, setLoading] = _secvUseState(false);
  const [error, setError] = _secvUseState(null);
  const [generateOpen, setGenerateOpen] = _secvUseState(false);
  const [activeReveal, setActiveReveal] = _secvUseState(null);
  const [authAdd, setAuthAdd] = _secvUseState(false);
  const [authEdit, setAuthEdit] = _secvUseState(null);
  const [authDelete, setAuthDelete] = _secvUseState(null);
  const [knownAdd, setKnownAdd] = _secvUseState(false);
  const [knownEdit, setKnownEdit] = _secvUseState(null);
  const [knownDelete, setKnownDelete] = _secvUseState(null);

  const load = _secvUseCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await window._secFetchJson('/api/secrets/global/ssh');
      setData(result || {});
    } catch (e) {
      setError(e.message || 'failed');
      setData({});
    } finally {
      setLoading(false);
    }
  }, []);

  _secvUseEffect(() => { load(); }, [load]);

  const privateKeys = _secvUseMemo(() => (data && data.private_keys) || [], [data]);
  const publicKeys = _secvUseMemo(() => (data && data.public_keys) || [], [data]);
  const authorizedKeys = _secvUseMemo(() => (data && data.authorized_keys) || [], [data]);
  const knownHosts = _secvUseMemo(() => (data && data.known_hosts) || [], [data]);
  const otherFiles = _secvUseMemo(() => (data && data.other) || [], [data]);

  const filterList = _secvUseCallback((arr) => {
    if (!query) return arr;
    return arr.filter((it) => _secvMatchesQuery(it, query));
  }, [query]);

  const totalCount = privateKeys.length + publicKeys.length + authorizedKeys.length + knownHosts.length + otherFiles.length;

  _secvUseEffect(() => {
    if (typeof onCount === 'function') onCount(totalCount);
  }, [totalCount, onCount]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {error && (
        <StatusBanner variant="err" label={String(error)} inline />
      )}
      <window.EntryList
        scope="global"
        kind="ssh"
        title="Private keys"
        subtitle="Generate-only; reveal pulls plaintext through a captcha gate."
        items={filterList(privateKeys)}
        loading={loading}
        onReload={load}
        onGenerateSsh={() => setGenerateOpen(true)}
        onReveal={(it) => setActiveReveal(it)}
        emptyMessage="No private keys in ~/.ssh yet."
      />
      <window.EntryList
        scope="global"
        kind="ssh-public"
        title="Public keys"
        items={filterList(publicKeys)}
        loading={loading}
        emptyMessage="No public keys."
      />
      <window.EntryList
        scope="global"
        kind="authorized_keys"
        title="authorized_keys"
        subtitle="Last-line guard: the server refuses to delete the only entry."
        items={filterList(authorizedKeys)}
        loading={loading}
        onReload={load}
        onAdd={() => setAuthAdd(true)}
        onEdit={(it) => setAuthEdit(it)}
        onDelete={(it) => setAuthDelete(it)}
        emptyMessage="No authorized_keys entries."
      />
      <window.EntryList
        scope="global"
        kind="known_hosts"
        title="known_hosts"
        items={filterList(knownHosts)}
        loading={loading}
        onReload={load}
        onAdd={() => setKnownAdd(true)}
        onEdit={(it) => setKnownEdit(it)}
        onDelete={(it) => setKnownDelete(it)}
        emptyMessage="No known_hosts entries."
      />
      {otherFiles.length > 0 && (
        <window.EntryList
          scope="global"
          kind="ssh-other"
          title="Other files (read-only)"
          items={filterList(otherFiles)}
          loading={loading}
          emptyMessage="No other files."
        />
      )}

      {generateOpen && (
        <window.GenerateSshKeyModal
          open={true}
          onClose={() => setGenerateOpen(false)}
          onGenerated={load}
        />
      )}
      {activeReveal && (
        <window.RevealModal
          open={true}
          onClose={() => setActiveReveal(null)}
          scope="global"
          kind="ssh"
          item={activeReveal}
        />
      )}
      {authAdd && (
        <window.AddEntryModal
          open={true}
          onClose={() => setAuthAdd(false)}
          scope="global"
          kind="authorized_keys"
          onAdded={load}
        />
      )}
      {authEdit && (
        <window.EditModal
          open={true}
          onClose={() => setAuthEdit(null)}
          scope="global"
          kind="authorized_keys"
          item={authEdit}
          onSaved={load}
        />
      )}
      {authDelete && (
        <window.DeleteModal
          open={true}
          onClose={() => setAuthDelete(null)}
          scope="global"
          kind="authorized_keys"
          item={authDelete}
          onDeleted={load}
        />
      )}
      {knownAdd && (
        <window.AddEntryModal
          open={true}
          onClose={() => setKnownAdd(false)}
          scope="global"
          kind="known_hosts"
          onAdded={load}
        />
      )}
      {knownEdit && (
        <window.EditModal
          open={true}
          onClose={() => setKnownEdit(null)}
          scope="global"
          kind="known_hosts"
          item={knownEdit}
          onSaved={load}
        />
      )}
      {knownDelete && (
        <window.DeleteModal
          open={true}
          onClose={() => setKnownDelete(null)}
          scope="global"
          kind="known_hosts"
          item={knownDelete}
          onDeleted={load}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// SecretsView — top-level page mounted by router.jsx.
// ─────────────────────────────────────────────────────────────
function SecretsView({ compact }) {
  const [query, setQuery] = _secvUseState('');
  const [globalTab, setGlobalTab] = _secvUseState('env');
  const [counts, setCounts] = _secvUseState({ env: 0, files: 0, ssh: 0 });

  const onGlobalCount = _secvUseCallback((kind, n) => {
    setCounts((prev) => (prev[kind] === n ? prev : { ...prev, [kind]: n }));
  }, []);

  const totalGlobal = counts.env + counts.files + counts.ssh;
  const q = query.trim().toLowerCase();

  const headerActions = (
    <>
      <Input
        type="search"
        value={query}
        onChange={setQuery}
        placeholder="Search keys, names, fingerprints…"
        ariaLabel="Search secrets"
        icon="search"
        size="sm"
        style={{ minWidth: 240 }}
      />
    </>
  );

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <window.SectionHeader
        eyebrow="Manage"
        title="Credentials"
        count={`${totalGlobal} global ${totalGlobal === 1 ? 'entry' : 'entries'}`}
        actions={headerActions}
      />

      <div style={{ marginTop: 24 }}>
        <div className="mono" style={{
          fontSize: 'var(--t-xs)', color: 'var(--fg-3)', letterSpacing: '0.08em',
          textTransform: 'uppercase', marginBottom: 10,
        }}>Globalne</div>
        <_GlobalTabs
          active={globalTab}
          onChange={setGlobalTab}
          counts={counts}
        />
        <div style={{ marginTop: 14 }}>
          {globalTab === 'env' && (
            <_GlobalEnvSection query={q} onCount={(n) => onGlobalCount('env', n)} />
          )}
          {globalTab === 'files' && (
            <_GlobalFilesSection query={q} onCount={(n) => onGlobalCount('files', n)} />
          )}
          {globalTab === 'ssh' && (
            <_GlobalSshSection query={q} onCount={(n) => onGlobalCount('ssh', n)} />
          )}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { SecretsView });
