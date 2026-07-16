// skills-detail.jsx — /skills/<name> page.
//
// Sections:
//   1. Header card — emoji + name + source badge + version + Update buttons
//   2. Description — frontmatter.description rendered as markdown
//   3. SKILL.md preview (collapsible, full-body markdown)
//   4. Agents matrix — checkbox per agent (Global + areas + projects + resources)
//   5. Danger zone — Uninstall (with confirmation modal)
//
// API contract (Agent B owns):
//   GET    /api/skills/<name> →
//     { name, frontmatter:{description?,...}, register:{source,version,...},
//       skill_md? (raw), agents: [{kind, lib_id, label, enabled, icon?}] }
//   POST   /api/skills/<name>/check-update → {update_available, current?, latest?}
//   POST   /api/skills/<name>/update       → updated detail OR {ok:true}
//   PATCH  /api/skills/<name>/agents       → bulk {enable_for, disable_for}
//   DELETE /api/skills/<name>              → 204 / 200

const { useState: _sdvUseState, useEffect: _sdvUseEffect, useMemo: _sdvUseMemo, useCallback: _sdvUseCallback } = React;

const _SDV_DEFAULT_ICON = '🧩';

const _SDV_SOURCE_LABELS = {
  github: 'GitHub',
  'github-shorthand': 'GitHub',
  marketplace: 'Marketplace',
  zip: 'ZIP',
  custom: 'Custom',
  builtin: 'Built-in',
  local: 'Local',
};

function _sdvSourceLabel(source) {
  return _SDV_SOURCE_LABELS[source] || (source ? String(source) : 'Unknown');
}

function _sdvSafeMarkdown(text) {
  if (!text || !window.marked || typeof window.marked.parse !== 'function') return '';
  try {
    const raw = window.marked.parse(String(text));
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
      return window.DOMPurify.sanitize(raw);
    }
    return raw;
  } catch (_) {
    return '';
  }
}

async function _sdvReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) detail = String(data.error || data.detail).slice(0, 240);
  } catch (_) { /* ignore */ }
  return detail;
}

// ─────────────────────────────────────────────────────────────
// Header card
// ─────────────────────────────────────────────────────────────
function _SdvHeaderCard({ detail, busy, onCheckUpdate, onUpdate, checkBusy }) {
  const icon = (detail.frontmatter && detail.frontmatter.icon)
    || (detail.register && detail.register.icon)
    || detail.icon
    || _SDV_DEFAULT_ICON;
  const source = (detail.register && detail.register.source) || detail.source;
  const version = (detail.register && detail.register.version) || detail.version;
  const updateAvailable = detail.update_available
    || (detail.register && detail.register.update_available);

  return (
    <Card padding={20}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
        <div style={{
          width: 56, height: 56, borderRadius: '50%', flexShrink: 0,
          background: 'var(--accent-soft)', border: '1px solid var(--accent-line)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 'var(--t-h1)', lineHeight: 1,
        }}>{icon}</div>
        <div style={{ flex: 1, minWidth: 200 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
            <h2 style={{ margin: 0, fontSize: 'var(--t-h2)', fontWeight: 500, letterSpacing: '-0.01em' }}>
              {detail.name}
            </h2>
            {version && (
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
                {version}
              </span>
            )}
          </div>
          <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
            <Chip mono>{_sdvSourceLabel(source)}</Chip>
            {updateAvailable && <Chip mono accent><Icon name="arrow-up" size={11} /> update available</Chip>}
            {detail.register && detail.register.git_origin && (
              <span className="mono" style={{
                fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                maxWidth: 320,
              }} title={detail.register.git_origin}>{detail.register.git_origin}</span>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          <Button variant="ghost" icon={checkBusy ? 'spinner' : 'refresh'}
            onClick={onCheckUpdate} type="button"
            style={checkBusy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
            {checkBusy ? 'Checking…' : 'Check'}
          </Button>
          <Button variant="primary" icon={busy ? 'spinner' : 'arrow-up'}
            onClick={onUpdate} type="button"
            style={(!updateAvailable || busy) ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
            {busy ? 'Updating…' : 'Update'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Description card (frontmatter.description)
// ─────────────────────────────────────────────────────────────
function _SdvDescriptionCard({ description }) {
  const html = _sdvUseMemo(() => _sdvSafeMarkdown(description), [description]);
  if (!description) {
    return (
      <Card padding={16}>
        <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
          (no description in SKILL.md frontmatter)
        </div>
      </Card>
    );
  }
  return (
    <Card padding={18}>
      <div className="mono" style={{
        fontSize: 'var(--t-2xs)', color: 'var(--fg-4)',
        textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8,
      }}>Description</div>
      <div className="md-body" style={{ fontSize: 13.5, lineHeight: 1.6 }}
        dangerouslySetInnerHTML={{ __html: html || description }} />
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// SKILL.md preview (collapsible)
// ─────────────────────────────────────────────────────────────
function _SdvSkillMdCard({ body }) {
  const [open, setOpen] = _sdvUseState(false);
  const html = _sdvUseMemo(() => _sdvSafeMarkdown(body), [body]);
  if (!body) return null;
  return (
    <Card padding={0}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex', alignItems: 'center', gap: 10, width: '100%',
          padding: '14px 18px', background: 'transparent', border: 'none',
          color: 'var(--fg)', fontSize: 'var(--t-sm)', fontWeight: 500, cursor: 'pointer',
          textAlign: 'left', fontFamily: 'inherit',
        }}
      >
        <Icon name={open ? 'chevron-d' : 'chevron-r'} size={14} />
        <span style={{ flex: 1 }}>SKILL.md preview</span>
        <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
          {open ? 'collapse' : 'expand'}
        </span>
      </button>
      {open && (
        <div style={{
          padding: '0 18px 18px', borderTop: '1px solid var(--hairline)',
          maxHeight: 540, overflowY: 'auto',
        }} className="scroll-hide">
          <div className="md-body" style={{ fontSize: 'var(--t-sm)', lineHeight: 1.6, paddingTop: 14 }}
            dangerouslySetInnerHTML={{ __html: html || '' }} />
        </div>
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Agents matrix
// ─────────────────────────────────────────────────────────────
function _SdvAgentRow({ agent, onToggle, busy }) {
  const icon = agent.icon || (agent.kind === 'global' ? '🤖'
    : agent.kind === 'areas' ? '📂'
    : agent.kind === 'projects' ? '📦'
    : agent.kind === 'resources' ? '📚' : '·');
  const sub = agent.kind === 'global'
    ? 'Default ~/'
    : `${agent.kind}/${agent.lib_id || ''}`;
  return (
    <label style={{
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '10px 12px', borderRadius: 'var(--r-control)', cursor: busy ? 'progress' : 'pointer',
      background: agent.enabled ? 'var(--accent-soft)' : 'transparent',
      border: '1px solid ' + (agent.enabled ? 'var(--accent-line)' : 'var(--hairline)'),
      opacity: busy ? 0.6 : 1,
    }}>
      <input
        type="checkbox"
        checked={!!agent.enabled}
        disabled={busy}
        onChange={() => onToggle(agent)}
        style={{ accentColor: 'var(--accent)' }}
      />
      <span style={{ fontSize: 18, lineHeight: 1, width: 24, textAlign: 'center' }}>{icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>{agent.label || agent.lib_id || agent.kind}</div>
        <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>{sub}</div>
      </div>
    </label>
  );
}

function _SdvAgentsMatrix({ agents, busy, onToggle }) {
  if (!agents || agents.length === 0) {
    return (
      <Card padding={16}>
        <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
          (no agents configured yet — add Areas/Projects to enable per-agent toggles)
        </div>
      </Card>
    );
  }
  const grouped = { global: [], areas: [], projects: [], resources: [] };
  agents.forEach((a) => {
    const k = grouped[a.kind] ? a.kind : 'resources';
    grouped[k].push(a);
  });
  const sections = [
    { key: 'global', label: 'Global' },
    { key: 'areas', label: 'Areas' },
    { key: 'projects', label: 'Projects' },
    { key: 'resources', label: 'Resources' },
  ];
  return (
    <Card padding={18}>
      <div className="mono" style={{
        fontSize: 'var(--t-2xs)', color: 'var(--fg-4)',
        textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 12,
      }}>Enabled on</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {sections.map((sec) => grouped[sec.key].length > 0 && (
          <div key={sec.key}>
            <div className="mono" style={{
              fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', marginBottom: 6,
              letterSpacing: '0.05em',
            }}>{sec.label}</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {grouped[sec.key].map((agent) => (
                <_SdvAgentRow
                  key={`${agent.kind}:${agent.lib_id || 'global'}`}
                  agent={agent}
                  busy={busy}
                  onToggle={onToggle}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Danger zone
// ─────────────────────────────────────────────────────────────
function _SdvDangerZone({ onUninstall, busy }) {
  const [confirm, setConfirm] = _sdvUseState(false);
  return (
    <Card padding={18} style={{ borderColor: 'var(--err)' }}>
      <div className="mono" style={{
        fontSize: 'var(--t-2xs)', color: 'var(--err)',
        textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8,
      }}>Danger zone</div>
      <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)', lineHeight: 1.55, marginBottom: 12 }}>
        Uninstalling removes the skill from the registry and cleans up every per-agent
        symlink farm. The action cannot be undone, but the skill can be re-installed
        from its source.
      </div>
      <Button variant="danger" icon={busy ? 'spinner' : 'trash'} onClick={() => setConfirm(true)}
        type="button"
        style={busy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
        {busy ? 'Uninstalling…' : 'Uninstall skill'}
      </Button>

      <Modal open={confirm} onClose={() => setConfirm(false)} title="Uninstall skill?" width={420}>
        <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>
            This will delete the skill from the registry and clean up every per-agent
            farm symlink. Are you sure?
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button variant="quiet" onClick={() => setConfirm(false)} type="button">Cancel</Button>
            <Button variant="danger" icon="trash" type="button"
              onClick={() => { setConfirm(false); onUninstall(); }}>
              Uninstall
            </Button>
          </div>
        </div>
      </Modal>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Main view
// ─────────────────────────────────────────────────────────────
function SkillsDetailView({ name, compact, onBack }) {
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [detail, setDetail] = _sdvUseState(null);
  const [error, setError] = _sdvUseState(null);
  const [updating, setUpdating] = _sdvUseState(false);
  const [checking, setChecking] = _sdvUseState(false);
  const [uninstalling, setUninstalling] = _sdvUseState(false);
  const [agentBusyKey, setAgentBusyKey] = _sdvUseState(null);

  const load = _sdvUseCallback(async () => {
    setError(null);
    try {
      const r = await fetch(apiUrl(`/api/skills/${encodeURIComponent(name)}`));
      if (!r.ok) throw new Error(await _sdvReadError(r));
      const data = await r.json();
      setDetail(data || null);
    } catch (e) {
      setError(e.message || 'failed to load skill');
      setDetail({ name, agents: [] });
    }
  }, [name]);

  _sdvUseEffect(() => {
    if (!name) return;
    setDetail(null);
    load();
  }, [name, load]);

  _sdvUseEffect(() => {
    const onReload = () => { load(); };
    window.addEventListener('skills:reload', onReload);
    return () => window.removeEventListener('skills:reload', onReload);
  }, [load]);

  const navigateBack = _sdvUseCallback(() => {
    if (typeof onBack === 'function') { onBack(); return; }
    if (router && typeof router.push === 'function') router.push('/skills');
    else { try { window.location.assign((window.HUB_BASE_PATH || '') + '/skills'); } catch (_) { /* ignore */ } }
  }, [onBack, router]);

  const onCheckUpdate = _sdvUseCallback(async () => {
    if (checking) return;
    setChecking(true);
    try {
      const r = await fetch(apiUrl(`/api/skills/${encodeURIComponent(name)}/check-update`), {
        method: 'POST',
      });
      if (!r.ok) throw new Error(await _sdvReadError(r));
      const data = await r.json().catch(() => ({}));
      setDetail((d) => d ? { ...d, update_available: !!(data && data.update_available) } : d);
      const msg = (data && data.update_available) ? 'Update available' : 'Up to date';
      toast && toast(msg, 'ok');
    } catch (e) {
      toast && toast(`Check failed: ${e.message || 'error'}`, 'err');
    } finally { setChecking(false); }
  }, [name, checking, toast]);

  const onUpdate = _sdvUseCallback(async () => {
    if (updating) return;
    setUpdating(true);
    try {
      const r = await fetch(apiUrl(`/api/skills/${encodeURIComponent(name)}/update`), {
        method: 'POST',
      });
      if (!r.ok) throw new Error(await _sdvReadError(r));
      const data = await r.json().catch(() => ({}));
      if (data && data.name) setDetail(data);
      else await load();
      try { window.dispatchEvent(new CustomEvent('skills:reload')); } catch (_) { /* ignore */ }
      toast && toast('Skill updated', 'ok');
    } catch (e) {
      toast && toast(`Update failed: ${e.message || 'error'}`, 'err');
    } finally { setUpdating(false); }
  }, [name, updating, load, toast]);

  // Optimistic toggle: flip the agent's `enabled` flag immediately, send the
  // PATCH, roll back on failure.
  const onToggleAgent = _sdvUseCallback(async (agent) => {
    if (!detail || agentBusyKey) return;
    const key = `${agent.kind}:${agent.lib_id || 'global'}`;
    const nextEnabled = !agent.enabled;
    setAgentBusyKey(key);
    setDetail((d) => {
      if (!d || !Array.isArray(d.agents)) return d;
      return {
        ...d,
        agents: d.agents.map((a) => (
          (a.kind === agent.kind && (a.lib_id || 'global') === (agent.lib_id || 'global'))
            ? { ...a, enabled: nextEnabled }
            : a
        )),
      };
    });
    try {
      const token = agent.kind === 'global' ? 'global' : `${agent.kind}:${agent.lib_id}`;
      const body = nextEnabled
        ? { enable_for: [token], disable_for: [] }
        : { enable_for: [], disable_for: [token] };
      const r = await fetch(apiUrl(`/api/skills/${encodeURIComponent(name)}/agents`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await _sdvReadError(r));
    } catch (e) {
      // Rollback.
      setDetail((d) => {
        if (!d || !Array.isArray(d.agents)) return d;
        return {
          ...d,
          agents: d.agents.map((a) => (
            (a.kind === agent.kind && (a.lib_id || 'global') === (agent.lib_id || 'global'))
              ? { ...a, enabled: !nextEnabled }
              : a
          )),
        };
      });
      toast && toast(`Toggle failed: ${e.message || 'error'}`, 'err');
    } finally {
      setAgentBusyKey(null);
    }
  }, [name, detail, agentBusyKey, toast]);

  const onUninstall = _sdvUseCallback(async () => {
    if (uninstalling) return;
    setUninstalling(true);
    try {
      const r = await fetch(apiUrl(`/api/skills/${encodeURIComponent(name)}`), { method: 'DELETE' });
      if (!r.ok) throw new Error(await _sdvReadError(r));
      try { window.dispatchEvent(new CustomEvent('skills:reload')); } catch (_) { /* ignore */ }
      toast && toast('Skill uninstalled', 'ok');
      navigateBack();
    } catch (e) {
      toast && toast(`Uninstall failed: ${e.message || 'error'}`, 'err');
      setUninstalling(false);
    }
  }, [name, uninstalling, navigateBack, toast]);

  if (detail === null) {
    return (
      <div style={{ padding: compact ? 16 : 28 }}>
        <Spinner label="Loading skill…" inline size={16} />
      </div>
    );
  }

  const fm = detail.frontmatter || {};
  const description = fm.description || detail.description || '';
  const skillMd = detail.skill_md || detail.body || '';
  const agents = Array.isArray(detail.agents) ? detail.agents : [];

  return (
    <div style={{
      padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28,
      display: 'flex', flexDirection: 'column', gap: 14,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <window.IconButton icon="chevron-l" label="Back" size={32} onClick={navigateBack} />
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
          Skills · {detail.name}
        </span>
      </div>

      {error && (
        <StatusBanner variant="err" label={`error: ${error}`} inline />
      )}

      <_SdvHeaderCard
        detail={detail}
        busy={updating}
        checkBusy={checking}
        onCheckUpdate={onCheckUpdate}
        onUpdate={onUpdate}
      />

      <_SdvDescriptionCard description={description} />
      <_SdvSkillMdCard body={skillMd} />
      <_SdvAgentsMatrix agents={agents} busy={!!agentBusyKey} onToggle={onToggleAgent} />
      <_SdvDangerZone onUninstall={onUninstall} busy={uninstalling} />
    </div>
  );
}

Object.assign(window, { SkillsDetailView });
