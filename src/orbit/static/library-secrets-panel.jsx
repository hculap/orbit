// library-secrets-panel.jsx — per-scope Env & secrets surface mounted
// inside the library detail view as the "Env & secrets" tab.
//
// Composition:
//   1. .env section          — flat KEY=VALUE entries for this scope.
//   2. .secrets/ section     — opaque files (JSON keys, raw token files).
//      Empty state explains the directory will be created lazily on the
//      first add.
//
// Endpoints (scope = "areas/<id>" | "projects/<id>", URL-encoded):
//   GET    /api/secrets/{scope}/env          — [{key, masked, last_modified}]
//   GET    /api/secrets/{scope}/files        —
//     {has_secrets_dir, items: [{name, size, mode, masked}]}
//
// All mutations (add/edit/delete/reveal) go through window.* primitives
// owned by secrets-components.jsx (top-level /secrets agent). This file is
// a thin per-scope wrapper around them — fetching + state + reload glue.
//
// Resources are excluded — the tab itself is gated on kind in (area, project)
// in library-detail.jsx, so this panel can assume one of those.

const {
  useState: _lspUseState,
  useEffect: _lspUseEffect,
  useCallback: _lspUseCallback,
  useMemo: _lspUseMemo,
} = React;

const _LSP_SECRETS_DIR_EMPTY = "This project has no `.secrets/` directory yet — adding the first entry will create it.";

function _lspKindPath(kind) {
  return kind === 'area' ? 'areas' : 'projects';
}

function _lspEncodeLibId(libId) {
  return String(libId || '').split('/').map(encodeURIComponent).join('/');
}

function _lspScopeSegment(kind, libId) {
  return `${_lspKindPath(kind)}/${_lspEncodeLibId(libId)}`;
}

async function _lspReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) {
      detail = String(data.error || data.detail).slice(0, 240);
    }
  } catch (_) { /* ignore — keep status fallback */ }
  return detail;
}

function _lspUseToast() {
  return (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;
}

const _LSP_BANNER_ERR = {
  padding: '10px 12px', fontSize: 'var(--t-cap)', color: 'var(--err)',
  background: 'var(--err-bg)',
  borderRadius: 'var(--r-control)', border: '1px solid var(--err)',
};

const _LSP_HINT = {
  fontSize: 'var(--t-cap)', color: 'var(--fg-4)',
  padding: '12px 14px',
  background: 'var(--surface-2)',
  border: '1px solid var(--hairline)',
  borderRadius: 'var(--r-control)',
};

const _LSP_SECTION_GAP = { display: 'flex', flexDirection: 'column', gap: 10 };

const _LSP_CREATE_PROMPT = {
  display: 'flex', flexDirection: 'column', gap: 8,
  padding: '14px 16px',
  background: 'var(--surface-1)',
  border: '1px dashed var(--hairline-strong)',
  borderRadius: 'var(--r-control)',
};

function _LspCreatePrompt({ title, body, onCreate, busy }) {
  const Button = window.Button;
  return (
    <div style={_LSP_CREATE_PROMPT}>
      <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)' }}>{title}</div>
      <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>{body}</div>
      <div style={{ marginTop: 4 }}>
        {Button ? (
          <Button variant="primary" icon="plus" onClick={onCreate} disabled={busy}>
            {busy ? 'Creating…' : 'Create'}
          </Button>
        ) : (
          <button onClick={onCreate} disabled={busy}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Sub-section header — matches the visual rhythm of other panels
// without depending on a specific window.SubHeader signature.
// ─────────────────────────────────────────────────────────────
function _LspSectionHeader({ icon, title, sub, count }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'baseline', gap: 10,
      padding: '4px 2px',
    }}>
      {icon && (window.Icon
        ? <window.Icon name={icon} size={14} color="var(--fg-3)" />
        : null)}
      <div style={{
        fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)',
        letterSpacing: '-0.01em',
      }}>{title}</div>
      {typeof count === 'number' && (
        <code className="mono" style={{
          fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
          padding: '1px 6px', borderRadius: 'var(--r-sm)',
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
        }}>{count}</code>
      )}
      {sub && (
        <div className="mono" style={{
          fontSize: 'var(--t-xs)', color: 'var(--fg-4)',
          marginLeft: 'auto',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }} title={sub}>{sub}</div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Hook: fetch one secrets endpoint, expose {data, loading, error, reload}.
// Generic so it serves both /env and /files without duplication.
// ─────────────────────────────────────────────────────────────
function _lspUseSecretsResource(scope, suffix) {
  const [data, setData] = _lspUseState(null);
  const [loading, setLoading] = _lspUseState(true);
  const [error, setError] = _lspUseState(null);

  const reload = _lspUseCallback(async () => {
    if (!scope) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(window.apiUrl(`/api/secrets/${scope}/${suffix}`));
      if (!response.ok) {
        const detail = await _lspReadError(response);
        throw new Error(detail);
      }
      const next = await response.json();
      setData(next);
    } catch (err) {
      console.error(`secrets: fetch /${suffix} failed`, err);
      setError(err.message || 'failed to load');
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [scope, suffix]);

  _lspUseEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(window.apiUrl(`/api/secrets/${scope}/${suffix}`))
      .then(async (response) => {
        if (!response.ok) {
          const detail = await _lspReadError(response);
          throw new Error(detail);
        }
        return response.json();
      })
      .then((next) => { if (!cancelled) setData(next); })
      .catch((err) => {
        if (cancelled) return;
        console.error(`secrets: fetch /${suffix} failed`, err);
        setError(err.message || 'failed to load');
        setData(null);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [scope, suffix]);

  return { data, loading, error, reload };
}

// ─────────────────────────────────────────────────────────────
// LibrarySecretsPanel — top-level component for the tab body.
// ─────────────────────────────────────────────────────────────
function LibrarySecretsPanel({ kind, libId, item, compact }) {
  const toast = _lspUseToast();
  const scope = _lspUseMemo(() => _lspScopeSegment(kind, libId), [kind, libId]);
  const scopeLabel = _lspUseMemo(() => {
    const root = kind === 'area' ? '~/Areas' : '~/Projects';
    return `${root}/${libId}`;
  }, [kind, libId]);

  const envResource = _lspUseSecretsResource(scope, 'env');
  const filesResource = _lspUseSecretsResource(scope, 'files');

  // Files endpoint returns either an array (legacy) or
  // {has_secrets_dir, items: [...]}; normalise so downstream treats both.
  const filesNormalised = _lspUseMemo(() => {
    const raw = filesResource.data;
    if (raw == null) return { hasSecretsDir: false, items: [] };
    if (Array.isArray(raw)) {
      return { hasSecretsDir: raw.length > 0, items: raw };
    }
    return {
      hasSecretsDir: !!raw.has_secrets_dir,
      items: Array.isArray(raw.items) ? raw.items : [],
    };
  }, [filesResource.data]);

  const envNormalised = _lspUseMemo(() => {
    const raw = envResource.data;
    if (raw == null) return { hasEnv: false, items: [] };
    if (Array.isArray(raw)) {
      return { hasEnv: raw.length > 0, items: raw };
    }
    return {
      hasEnv: !!raw.has_env,
      items: Array.isArray(raw.items) ? raw.items : [],
    };
  }, [envResource.data]);
  const envItems = envNormalised.items;

  // After any successful mutation flowing through the shared modals we
  // simply refetch the affected resource. The shared modals are expected
  // to call `onReload` themselves; this wrapper makes the boundary
  // explicit so we never silently drop a refresh.
  const reloadEnv = _lspUseCallback(() => {
    envResource.reload().catch(() => { /* error already toasted in modal */ });
  }, [envResource.reload]);
  const reloadFiles = _lspUseCallback(() => {
    filesResource.reload().catch(() => { /* error already toasted in modal */ });
  }, [filesResource.reload]);

  // Per-section modal state — mirrors the global page so users can
  // reveal/edit/delete/add entries from inside a library detail tab too.
  // (Without these handlers EntryList renders rows but no row-level
  // actions and no Add button.)
  const [envReveal, setEnvReveal] = _lspUseState(null);
  const [envEdit, setEnvEdit] = _lspUseState(null);
  const [envDelete, setEnvDelete] = _lspUseState(null);
  const [envAddOpen, setEnvAddOpen] = _lspUseState(false);
  const [filesReveal, setFilesReveal] = _lspUseState(null);
  const [filesEdit, setFilesEdit] = _lspUseState(null);
  const [filesDelete, setFilesDelete] = _lspUseState(null);
  const [filesAddOpen, setFilesAddOpen] = _lspUseState(false);

  // Idempotent init: POST creates the file/dir if missing, then reloads.
  // No captcha — touching an empty file or empty dir is reversible (rm) and
  // the user clicked an explicit "Create" CTA.
  const [initBusy, setInitBusy] = _lspUseState(null);  // 'env' | 'files' | null
  const initEnv = _lspUseCallback(async () => {
    setInitBusy('env');
    try {
      const r = await fetch(window.apiUrl(`/api/secrets/${scope}/env/init`), { method: 'POST' });
      if (!r.ok) throw new Error(await _lspReadError(r));
      toast && toast('.env created', 'ok');
      reloadEnv();
    } catch (err) {
      console.error('secrets: init env failed', err);
      toast && toast(`Create .env failed: ${err.message || 'error'}`, 'err');
    } finally {
      setInitBusy(null);
    }
  }, [scope, reloadEnv, toast]);
  const initFiles = _lspUseCallback(async () => {
    setInitBusy('files');
    try {
      const r = await fetch(window.apiUrl(`/api/secrets/${scope}/files/init`), { method: 'POST' });
      if (!r.ok) throw new Error(await _lspReadError(r));
      toast && toast('.secrets/ created', 'ok');
      reloadFiles();
    } catch (err) {
      console.error('secrets: init files failed', err);
      toast && toast(`Create .secrets/ failed: ${err.message || 'error'}`, 'err');
    } finally {
      setInitBusy(null);
    }
  }, [scope, reloadFiles, toast]);

  // Resources tab visibility is enforced upstream; if the panel is
  // ever instantiated for an unsupported kind, surface a clear message.
  if (kind !== 'area' && kind !== 'project') {
    return (
      <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
        Credentials are only available for Areas and Projects.
      </div>
    );
  }

  // EntryList from secrets-components.jsx is the canonical UI; if it isn't
  // loaded yet (e.g. script tag missing) we render a soft placeholder
  // rather than crash the tab.
  const EntryList = window.EntryList;

  return (
    <div style={{
      padding: compact ? 16 : 24,
      paddingBottom: compact ? 100 : 24,
      display: 'flex', flexDirection: 'column', gap: 18,
    }}>
      {!EntryList && (
        <div className="mono" style={{
          ..._LSP_HINT, color: 'var(--fg-3)',
        }}>
          Secrets components not loaded yet — refresh the page if this persists.
        </div>
      )}

      {/* .env section */}
      <section style={_LSP_SECTION_GAP}>
        <_LspSectionHeader
          icon="key"
          title=".env"
          sub={`${scopeLabel}/.env`}
          count={envResource.error ? undefined : envItems.length}
        />
        {envResource.error && (
          <StatusBanner variant="err" label={`Failed to load .env entries: ${envResource.error}`} inline />
        )}
        {!envResource.error && !envResource.loading && !envNormalised.hasEnv && (
          <_LspCreatePrompt
            title="No .env file yet"
            body={`Initialise an empty file at ${scopeLabel}/.env. You can add KEY=VALUE entries afterwards.`}
            onCreate={initEnv}
            busy={initBusy === 'env'}
          />
        )}
        {EntryList && envNormalised.hasEnv && (
          <EntryList
            scope={scope}
            kind="env"
            items={envItems}
            loading={envResource.loading}
            onReload={reloadEnv}
            onAdd={() => setEnvAddOpen(true)}
            onReveal={(it) => setEnvReveal(it)}
            onEdit={(it) => setEnvEdit(it)}
            onDelete={(it) => setEnvDelete(it)}
            toast={toast}
            compact={compact}
            scopeLabel={scopeLabel}
          />
        )}
      </section>

      {/* .secrets/ section */}
      <section style={_LSP_SECTION_GAP}>
        <_LspSectionHeader
          icon="folder"
          title=".secrets/"
          sub={`${scopeLabel}/.secrets/`}
          count={
            filesResource.error
              ? undefined
              : (filesNormalised.hasSecretsDir ? filesNormalised.items.length : undefined)
          }
        />
        {filesResource.error && (
          <StatusBanner variant="err" label={`Failed to load .secrets/ entries: ${filesResource.error}`} inline />
        )}
        {!filesResource.error && !filesResource.loading && !filesNormalised.hasSecretsDir && (
          <_LspCreatePrompt
            title="No .secrets/ directory yet"
            body={`Initialise an empty directory at ${scopeLabel}/.secrets/ (mode 0700). You can drop key files into it afterwards.`}
            onCreate={initFiles}
            busy={initBusy === 'files'}
          />
        )}
        {EntryList && filesNormalised.hasSecretsDir && (
          <EntryList
            scope={scope}
            kind="files"
            items={filesNormalised.items}
            loading={filesResource.loading}
            hasSecretsDir={filesNormalised.hasSecretsDir}
            onReload={reloadFiles}
            onAdd={() => setFilesAddOpen(true)}
            onReveal={(it) => setFilesReveal(it)}
            onEdit={(it) => setFilesEdit(it)}
            onDelete={(it) => setFilesDelete(it)}
            toast={toast}
            compact={compact}
            scopeLabel={scopeLabel}
            emptyMessage="No files yet — drop in your first JSON key or token below."
          />
        )}
      </section>

      {/* Modal mounts (env) */}
      {envReveal && window.RevealModal && (
        <window.RevealModal open={true} onClose={() => setEnvReveal(null)}
          scope={scope} kind="env" item={envReveal} />
      )}
      {envEdit && window.EditModal && (
        <window.EditModal open={true} onClose={() => setEnvEdit(null)}
          scope={scope} kind="env" item={envEdit} onSaved={reloadEnv} />
      )}
      {envDelete && window.DeleteModal && (
        <window.DeleteModal open={true} onClose={() => setEnvDelete(null)}
          scope={scope} kind="env" item={envDelete} onDeleted={reloadEnv} />
      )}
      {envAddOpen && window.AddEntryModal && (
        <window.AddEntryModal open={true} onClose={() => setEnvAddOpen(false)}
          scope={scope} kind="env" onAdded={reloadEnv} />
      )}

      {/* Modal mounts (files) */}
      {filesReveal && window.RevealModal && (
        <window.RevealModal open={true} onClose={() => setFilesReveal(null)}
          scope={scope} kind="files" item={filesReveal} />
      )}
      {filesEdit && window.EditModal && (
        <window.EditModal open={true} onClose={() => setFilesEdit(null)}
          scope={scope} kind="files" item={filesEdit} onSaved={reloadFiles} />
      )}
      {filesDelete && window.DeleteModal && (
        <window.DeleteModal open={true} onClose={() => setFilesDelete(null)}
          scope={scope} kind="files" item={filesDelete} onDeleted={reloadFiles} />
      )}
      {filesAddOpen && window.AddEntryModal && (
        <window.AddEntryModal open={true} onClose={() => setFilesAddOpen(false)}
          scope={scope} kind="files" onAdded={reloadFiles} />
      )}
    </div>
  );
}

Object.assign(window, { LibrarySecretsPanel });
