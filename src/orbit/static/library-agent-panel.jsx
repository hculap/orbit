// library-agent-panel.jsx — "Agent" tab body for an Area or Project.
//
// Composition (top → bottom of left column):
//   1. AgentIdentityHeader  — big icon avatar + emoji input + "Pick via LLM"
//      button. POSTs /agent/regenerate-identity (re-picks icon too).
//   2. AgentPromptCard      — reusable identity / custom textarea editor.
//      PATCH /agent/prompts {identity?|custom?}. Identity variant adds a
//      Regenerate button that calls /agent/regenerate-identity.
//   3. AgentGeneralBanner   — readonly info banner pointing to /settings.
//   4. AgentProfileCard     — keeps the per-agent model override. PATCH
//      /agent {model}.
//
// Right column:
//   5. NewSessionButton + AgentSessionsList — POST /api/orchestrator/sessions
//      and list existing sessions for cwd via window.SessionList.
//
// Endpoints used:
//   GET   /api/library/{kind}/{name:path}/agent/prompts
//   PATCH /api/library/{kind}/{name:path}/agent/prompts          {identity?, custom?, icon?}
//   POST  /api/library/{kind}/{name:path}/agent/regenerate-identity
//   GET   /api/library/{kind}s/{lib_id}/agent                    (model overrides)
//   PATCH /api/library/{kind}s/{lib_id}/agent                    {model}
//   GET   /api/orchestrator/sessions?cwd=<abs>
//   POST  /api/orchestrator/sessions                             {cwd, lib_id, model}
//
// Caps mirror server: identity + custom each ≤8 KB, icon ≤8 chars (ZWJ).

const { useState: _lapUseState, useEffect: _lapUseEffect, useMemo: _lapUseMemo, useCallback: _lapUseCallback } = React;

const _LAP_PROMPT_MAX_BYTES = 8 * 1024;
const _LAP_ICON_MAX_LEN = 8;

const _LAP_MODEL_OPTIONS = [
  { value: '', label: 'Inherit (global default)' },
  { value: 'opus', label: 'opus' },
  { value: 'sonnet', label: 'sonnet' },
  { value: 'haiku', label: 'haiku' },
];

function _lapByteLength(str) {
  if (!str) return 0;
  if (typeof TextEncoder !== 'undefined') {
    try { return new TextEncoder().encode(str).length; } catch (_) { /* fallthrough */ }
  }
  return str.length;
}
function _lapDeriveCwd(kind, libId)       { return `~/${kind === 'area' ? 'Areas' : 'Projects'}/${libId}`; }
function _lapDeriveLibIdParam(kind, libId){ return `${kind === 'area' ? 'areas' : 'projects'}/${libId}`; }
function _lapEncodeLibId(libId)           { return String(libId || '').split('/').map(encodeURIComponent).join('/'); }
function _lapKindPath(kind)               { return kind === 'area' ? 'areas' : 'projects'; }
function _lapFallbackIcon(kind)           { return kind === 'area' ? '📂' : '📦'; }

async function _lapReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) detail = String(data.error || data.detail).slice(0, 240);
  } catch (_) { /* ignore */ }
  return detail;
}

const _LAP_INPUT_BASE = {
  boxSizing: 'border-box', background: 'var(--surface-2)',
  border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)',
  padding: '8px 12px', color: 'var(--fg)', fontSize: 'var(--t-sm)',
  fontFamily: 'inherit', outline: 'none',
};
const _LAP_LABEL = {
  fontSize: 'var(--t-2xs)', color: 'var(--fg-4)',
  textTransform: 'uppercase', letterSpacing: '0.08em',
};
const _LAP_ERR_BANNER = {
  padding: '8px 10px', fontSize: 'var(--t-xs)', color: 'var(--err)',
  background: 'var(--err-bg)',
  borderRadius: 'var(--r-sm)', border: '1px solid var(--err)',
};

function _useToast() {
  return (typeof window !== 'undefined' && typeof window.useToast === 'function') ? window.useToast() : null;
}

// ─────────────────────────────────────────────────────────────
// AgentIdentityHeader — avatar + icon input + "Pick via LLM".
// ─────────────────────────────────────────────────────────────
function AgentIdentityHeader({ kind, libId, icon, onIconSaved }) {
  // Icon was picked by the LLM at agent-creation time; this header is purely
  // for the user to override it later. We render ONE input (which doubles as
  // the visible icon since it's font-size 18 + centered) — no separate
  // avatar preview. The "Pick via LLM" button is gone too: the regenerate
  // flow on the Identity card re-runs claude and re-picks the icon there.
  const fallback = _lapFallbackIcon(kind);
  const [draft, setDraft] = _lapUseState(icon || '');
  const [busy, setBusy] = _lapUseState(false);
  const [err, setErr] = _lapUseState(null);
  const toast = _useToast();
  _lapUseEffect(() => { setDraft(icon || ''); }, [icon]);

  const tooLong = draft && draft.length > _LAP_ICON_MAX_LEN;
  const dirty = (draft || '') !== (icon || '');

  const onSave = _lapUseCallback(async () => {
    if (!dirty || busy || tooLong) return;
    setBusy(true); setErr(null);
    try {
      const url = `/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent/prompts`;
      const r = await fetch(apiUrl(url), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ icon: draft || '' }),
      });
      if (!r.ok) throw new Error(await _lapReadError(r));
      toast && toast('Icon saved', 'ok');
      if (typeof onIconSaved === 'function') onIconSaved(draft || '');
    } catch (e) {
      const msg = e.message || 'save failed';
      setErr(msg);
      toast && toast(`Icon save failed: ${msg}`, 'err');
    } finally { setBusy(false); }
  }, [dirty, busy, tooLong, kind, libId, draft, toast, onIconSaved]);

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <label className="mono" style={{ ..._LAP_LABEL, marginRight: 4 }}>icon</label>
        <Input
          value={draft}
          onChange={setDraft}
          placeholder={fallback}
          maxLength={_LAP_ICON_MAX_LEN}
          error={tooLong}
          style={{ width: 84 }}
          inputStyle={{ fontSize: 22, textAlign: 'center' }}
        />
        <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'check'} onClick={onSave} type="button"
          style={(!dirty || tooLong || busy) ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
          {busy ? 'Saving…' : 'Save icon'}
        </Button>
        <span className="mono" style={{ flex: '0 1 100%', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', marginTop: 4 }}>
          Pick a single emoji (1–{_LAP_ICON_MAX_LEN} chars).
        </span>
        {err && <StatusBanner variant="err" label={err} inline style={{ flex: '0 1 100%', marginTop: 4 }} />}
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// AgentPromptCard — shared identity/custom textarea editor.
//   field: 'identity' | 'custom'
// ─────────────────────────────────────────────────────────────
function AgentPromptCard({
  kind, libId, field, value, label, sublabel, placeholder, rows = 8, minHeight = 140,
  onSaved, onRegenerate, regenBusy, regenError,
}) {
  const persisted = typeof value === 'string' ? value : '';
  const [draft, setDraft] = _lapUseState(persisted);
  const [busy, setBusy] = _lapUseState(false);
  const [err, setErr] = _lapUseState(null);
  const toast = _useToast();
  _lapUseEffect(() => { setDraft(persisted); }, [persisted]);

  const bytes = _lapUseMemo(() => _lapByteLength(draft), [draft]);
  const overLimit = bytes > _LAP_PROMPT_MAX_BYTES;
  const dirty = draft !== persisted;

  const save = _lapUseCallback(async () => {
    if (!dirty || busy || overLimit) return;
    setBusy(true); setErr(null);
    try {
      const url = `/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent/prompts`;
      const r = await fetch(apiUrl(url), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: draft }),
      });
      if (!r.ok) throw new Error(await _lapReadError(r));
      toast && toast(`${label} saved`, 'ok');
      if (typeof onSaved === 'function') onSaved(draft);
    } catch (e) {
      const msg = e.message || 'save failed';
      setErr(msg);
      toast && toast(`Save failed: ${msg}`, 'err');
    } finally { setBusy(false); }
  }, [dirty, busy, overLimit, kind, libId, field, label, draft, toast, onSaved]);

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
        <div style={{ ..._LAP_LABEL, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flex: 1 }}>{label}</div>
        {typeof onRegenerate === 'function' && (
          <Button size="sm" variant="ghost" icon={regenBusy ? 'spinner' : 'refresh-cw'}
            onClick={onRegenerate} type="button"
            style={regenBusy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
            {regenBusy ? 'Regenerating…' : 'Regenerate'}
          </Button>
        )}
        <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'check'} onClick={save} type="button"
          style={(!dirty || overLimit || busy) ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <label className="mono" style={{ ..._LAP_LABEL, flex: 1 }}>{sublabel}</label>
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: overLimit ? 'var(--err)' : 'var(--fg-4)' }}
            title={`${bytes} of ${_LAP_PROMPT_MAX_BYTES} bytes`}>
            {bytes} / {_LAP_PROMPT_MAX_BYTES} B
          </span>
        </div>
        <Textarea
          value={draft} onChange={setDraft}
          rows={rows} placeholder={placeholder}
          error={overLimit}
          mono
          style={{ resize: 'vertical', minHeight, fontSize: 12.5, lineHeight: 1.6 }}
        />
      </div>

      {(err || regenError) && (
        <StatusBanner variant="err" label={err || regenError} inline style={{ marginTop: 12 }} />
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// AgentGeneralBanner — readonly notice + link to /settings.
// ─────────────────────────────────────────────────────────────
function AgentGeneralBanner() {
  const router = (typeof window !== 'undefined' && typeof window.useRouter === 'function') ? window.useRouter() : null;
  const onOpenSettings = _lapUseCallback((e) => {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    if (router && typeof router.push === 'function' && typeof window.buildPath === 'function') {
      router.push(window.buildPath({ section: 'settings' })); return;
    }
    try { window.location.assign((window.HUB_BASE_PATH || '') + '/settings'); } catch (_) { /* ignore */ }
  }, [router]);

  return (
    <Card padding={14}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
        <div style={{
          width: 22, height: 22, borderRadius: 'var(--r-sm)', flexShrink: 0,
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--fg-3)',
        }}><Icon name="info" size={13} /></div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', lineHeight: 1.5 }}>
            Inherits a shared prompt from <a href="#" onClick={onOpenSettings}
              style={{ color: 'var(--accent)', textDecoration: 'underline' }}>Settings → Prompts</a>.
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 4 }}>
            The general prompt is shared across every agent. Edit it once in Settings.
          </div>
        </div>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// AgentProfileCard — model override.
// ─────────────────────────────────────────────────────────────
function AgentProfileCard({ kind, libId, cwd, profile, onProfileSaved }) {
  const persistedModel = (profile && profile.model) || '';
  const persistedSkills = (profile && profile.skills_allowlist) || null;
  // Preserve the legacy system_prompt if backend still echoes it (lazy
  // migration may still be pending — don't drop it from the PATCH).
  const persistedSystemPrompt = (profile && typeof profile.system_prompt === 'string') ? profile.system_prompt : null;
  const [draftModel, setDraftModel] = _lapUseState(persistedModel);
  const [busy, setBusy] = _lapUseState(false);
  const [err, setErr] = _lapUseState(null);
  const toast = _useToast();
  _lapUseEffect(() => { setDraftModel(persistedModel); }, [persistedModel]);

  const dirty = draftModel !== persistedModel;

  const onSave = _lapUseCallback(async () => {
    if (!dirty || busy) return;
    setBusy(true); setErr(null);
    try {
      const url = `/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent`;
      const body = { model: draftModel || null, system_prompt: persistedSystemPrompt, skills_allowlist: persistedSkills };
      const r = await fetch(apiUrl(url), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await _lapReadError(r));
      const data = await r.json().catch(() => ({}));
      const next = (data && data.agent) || body;
      toast && toast('Model saved', 'ok');
      if (typeof onProfileSaved === 'function') onProfileSaved(next);
    } catch (e) {
      const msg = e.message || 'save failed';
      setErr(msg); toast && toast(`Save failed: ${msg}`, 'err');
    } finally { setBusy(false); }
  }, [dirty, busy, kind, libId, draftModel, persistedSkills, persistedSystemPrompt, toast, onProfileSaved]);

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <div style={{ ..._LAP_LABEL, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flex: 1 }}>Agent profile</div>
        <Button size="sm" variant="primary" icon={busy ? 'spinner' : 'check'} onClick={onSave} type="button"
          style={(!dirty || busy) ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
        <label className="mono" style={_LAP_LABEL}>cwd</label>
        <code className="mono" style={{
          fontSize: 'var(--t-cap)', color: 'var(--fg-2)', padding: '8px 10px', borderRadius: 'var(--r-sm)',
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }} title={cwd}>{cwd}</code>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <label className="mono" style={_LAP_LABEL}>model</label>
        <Select
          value={draftModel}
          onChange={setDraftModel}
          options={_LAP_MODEL_OPTIONS}
        />
      </div>
      {err && <StatusBanner variant="err" label={err} inline style={{ marginTop: 12 }} />}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// LinkedSchedulersCard — surface cron jobs whose action.agent or
// destination.agent matches this library entity. Lets the user jump
// to /scheduler/<id> from a project/area page so the linkage is
// discoverable from both sides.
// ─────────────────────────────────────────────────────────────
function LinkedSchedulersCard({ kind, libId }) {
  const [jobs, setJobs] = _lapUseState(null);
  const [error, setError] = _lapUseState(null);
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;

  const reload = _lapUseCallback(async () => {
    setError(null);
    try {
      const agentKey = `${_lapKindPath(kind)}/${libId}`;
      const r = await fetch(apiUrl(`/api/cron/jobs?agent=${encodeURIComponent(agentKey)}`));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const data = await r.json();
      setJobs(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message || 'failed to load schedulers');
      setJobs([]);
    }
  }, [kind, libId]);

  _lapUseEffect(() => {
    reload();
    const onReload = () => { reload(); };
    window.addEventListener('scheduler:reload', onReload);
    return () => window.removeEventListener('scheduler:reload', onReload);
  }, [reload]);

  const open = (jobId) => {
    if (router && typeof router.push === 'function') {
      router.push(`/scheduler/${encodeURIComponent(jobId)}`);
    } else {
      try { window.location.assign((window.HUB_BASE_PATH || '') + `/scheduler/${encodeURIComponent(jobId)}`); } catch (_) {}
    }
  };
  const goNew = () => {
    if (router && typeof router.push === 'function') router.push('/scheduler');
    else { try { window.location.assign((window.HUB_BASE_PATH || '') + '/scheduler'); } catch (_) {} }
  };

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <div style={{ ..._LAP_LABEL, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flex: 1 }}>Schedulers</div>
        {Array.isArray(jobs) && (
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            {jobs.length} linked
          </span>
        )}
      </div>
      {error && <StatusBanner variant="err" label={error} inline style={{ marginBottom: 10 }} />}
      {jobs === null && (
        <Spinner inline size={14} label="loading…" />
      )}
      {jobs !== null && jobs.length === 0 && (
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
          No schedulers linked. Create one in <a onClick={(e) => { e.preventDefault(); goNew(); }}
            href="#" style={{ color: 'var(--accent)' }}>/scheduler</a> and pick this agent
          as the action or destination.
        </div>
      )}
      {jobs && jobs.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {jobs.map((job) => (
            <button key={job.id} onClick={() => open(job.id)} style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '8px 10px', borderRadius: 'var(--r-sm)',
              background: 'var(--surface-2)', border: '1px solid var(--hairline)',
              cursor: 'pointer', fontFamily: 'inherit', textAlign: 'left',
            }}>
              <span style={{ fontSize: 'var(--t-h3)', lineHeight: 1, width: 22, textAlign: 'center' }}>
                {job.icon || job.emoji || '⏰'}
              </span>
              <span style={{
                flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)',
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}>{job.name || job.id}</span>
              {!job.enabled && (
                <span className="mono" style={{
                  fontSize: 9, padding: '1px 5px', borderRadius: 4,
                  color: 'var(--fg-3)', background: 'var(--surface-1)',
                  border: '1px solid var(--hairline)',
                }}>PAUSED</span>
              )}
              {job.last_run_status === 'failed' && job.enabled && (
                <span className="mono" style={{
                  fontSize: 9, padding: '1px 5px', borderRadius: 4,
                  color: 'var(--err)', background: 'var(--err-bg)',
                  border: '1px solid var(--err)',
                }}>FAILING</span>
              )}
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// AgentSkillsCard — per-agent skill toggles.
//
// Persists via PATCH /api/library/<kind>s/<lib_id>/agent/skills with
// { enabled: [name1, name2, ...] }. Optimistic update + rollback on error.
// Listens to `skills:reload` so newly installed skills show up live.
// ─────────────────────────────────────────────────────────────
function AgentSkillsCard({ kind, libId }) {
  const [skills, setSkills] = _lapUseState(null);
  const [enabledSet, setEnabledSet] = _lapUseState(null);
  const [globalSet, setGlobalSet] = _lapUseState(null);
  const [allowlistSet, setAllowlistSet] = _lapUseState(null);
  const [error, setError] = _lapUseState(null);
  const [busyName, setBusyName] = _lapUseState(null);
  const toast = _useToast();

  const reload = _lapUseCallback(async () => {
    setError(null);
    try {
      // skills list + per-agent effective skills (= global ∪ allowlist).
      // Effective state is what /skills/<name>/agents shows; reusing it
      // here keeps the two views consistent.
      const [skillsRes, agentSkillsRes] = await Promise.all([
        fetch(apiUrl('/api/skills')),
        fetch(apiUrl(`/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent/skills`)),
      ]);
      if (!skillsRes.ok) throw new Error(await _lapReadError(skillsRes));
      const skillsData = await skillsRes.json();
      const list = Array.isArray(skillsData) ? skillsData
        : (skillsData && Array.isArray(skillsData.skills) ? skillsData.skills : []);
      setSkills(list);

      let enabled = new Set();
      let globals = new Set();
      let allowlist = new Set();
      if (agentSkillsRes.ok) {
        const agentSkills = await agentSkillsRes.json().catch(() => ({}));
        if (Array.isArray(agentSkills.enabled)) enabled = new Set(agentSkills.enabled);
        if (Array.isArray(agentSkills.global_enabled)) globals = new Set(agentSkills.global_enabled);
        if (Array.isArray(agentSkills.allowlist)) allowlist = new Set(agentSkills.allowlist);
      }
      setEnabledSet(enabled);
      setGlobalSet(globals);
      setAllowlistSet(allowlist);
    } catch (e) {
      setError(e.message || 'failed to load skills');
      setSkills([]);
      setEnabledSet(new Set());
      setGlobalSet(new Set());
      setAllowlistSet(new Set());
    }
  }, [kind, libId]);

  _lapUseEffect(() => {
    let cancelled = false;
    setSkills(null); setEnabledSet(null); setError(null);
    (async () => {
      try {
        await reload();
      } catch (_) { /* reload sets its own error */ }
      if (cancelled) return;
    })();
    return () => { cancelled = true; };
  }, [reload]);

  _lapUseEffect(() => {
    const onReload = () => { reload(); };
    window.addEventListener('skills:reload', onReload);
    return () => window.removeEventListener('skills:reload', onReload);
  }, [reload]);

  const toggle = _lapUseCallback(async (skillName, nextEnabled) => {
    if (busyName) return;
    if (globalSet && globalSet.has(skillName)) {
      // Globally-enabled skills aren't toggleable per-agent. Hint where
      // to manage them and bail before touching disk.
      toast && toast(`"${skillName}" is globally enabled — manage in /skills`, 'warn');
      return;
    }
    setBusyName(skillName);
    const prevAllowlist = allowlistSet || new Set();
    const prevEnabled = enabledSet || new Set();
    const nextAllowlist = new Set(prevAllowlist);
    const nextEffective = new Set(prevEnabled);
    if (nextEnabled) {
      nextAllowlist.add(skillName);
      nextEffective.add(skillName);
    } else {
      nextAllowlist.delete(skillName);
      nextEffective.delete(skillName);
    }
    setAllowlistSet(nextAllowlist);
    setEnabledSet(nextEffective);
    try {
      const url = `/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent/skills`;
      const r = await fetch(apiUrl(url), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: Array.from(nextAllowlist) }),
      });
      if (!r.ok) throw new Error(await _lapReadError(r));
      toast && toast(nextEnabled ? `Enabled "${skillName}"` : `Disabled "${skillName}"`, 'ok');
    } catch (e) {
      // Rollback both sets.
      setAllowlistSet(prevAllowlist);
      setEnabledSet(prevEnabled);
      toast && toast(`Toggle failed: ${e.message || 'error'}`, 'err');
    } finally {
      setBusyName(null);
    }
  }, [kind, libId, allowlistSet, enabledSet, globalSet, busyName, toast]);

  const isLoading = skills === null || enabledSet === null;

  return (
    <Card padding={18}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <div style={{ ..._LAP_LABEL, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flex: 1 }}>Skills</div>
        {Array.isArray(skills) && (
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            {(enabledSet ? enabledSet.size : 0)} / {skills.length} enabled
          </span>
        )}
      </div>
      {error && <StatusBanner variant="err" label={error} inline style={{ marginBottom: 10 }} />}
      {isLoading && (
        <Spinner inline size={14} label="loading skills…" />
      )}
      {!isLoading && skills.length === 0 && (
        <EmptyState message="No skills installed" sub={<>Install one from <a href={(window.HUB_BASE_PATH || '') + '/skills'} style={{ color: 'var(--accent)' }}>/skills</a>.</>} />
      )}
      {!isLoading && skills.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {skills.map((s) => {
            const checked = enabledSet.has(s.name);
            const isGlobal = !!(globalSet && globalSet.has(s.name));
            const busy = busyName === s.name;
            return (
              <label key={s.name} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '8px 10px', borderRadius: 'var(--r-sm)',
                cursor: busy ? 'progress' : (isGlobal ? 'help' : 'pointer'),
                background: checked ? 'var(--accent-soft)' : 'var(--surface-2)',
                border: '1px solid ' + (checked ? 'var(--accent-line)' : 'var(--hairline)'),
                opacity: busy ? 0.6 : 1,
              }} title={isGlobal ? 'Globally enabled — manage in /skills' : undefined}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={busy || isGlobal}
                  onChange={(e) => toggle(s.name, e.target.checked)}
                  style={{ accentColor: 'var(--accent)' }}
                />
                <span style={{ fontSize: 'var(--t-h3)', lineHeight: 1, width: 22, textAlign: 'center' }}>
                  {s.icon || '🧩'}
                </span>
                <span style={{
                  flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)',
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>{s.name}</span>
                {isGlobal && (
                  <span className="mono" style={{
                    fontSize: 'var(--t-2xs)', color: 'var(--fg-3)',
                    padding: '2px 6px', borderRadius: 4,
                    background: 'var(--surface-1)', border: '1px solid var(--hairline)',
                  }}>GLOBAL</span>
                )}
                {s.version && (
                  <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
                    {s.version}
                  </span>
                )}
              </label>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// AgentSessionsList — fetch + render via window.SessionList.
// ─────────────────────────────────────────────────────────────
function AgentSessionsList({ cwd, onSelectSession, onNewSession, compact }) {
  const [sessions, setSessions] = _lapUseState(null);
  const [error, setError] = _lapUseState(null);

  const reload = _lapUseCallback(async () => {
    setError(null);
    try {
      const r = await fetch(apiUrl(`/api/orchestrator/sessions?cwd=${encodeURIComponent(cwd)}`));
      if (!r.ok) throw new Error(await _lapReadError(r));
      const data = await r.json();
      setSessions(Array.isArray(data) ? data : (data && Array.isArray(data.sessions) ? data.sessions : []));
    } catch (e) {
      setError(e.message || 'failed to load sessions'); setSessions([]);
    }
  }, [cwd]);

  _lapUseEffect(() => {
    let cancelled = false;
    setSessions(null); setError(null);
    (async () => {
      try {
        const r = await fetch(apiUrl(`/api/orchestrator/sessions?cwd=${encodeURIComponent(cwd)}`));
        if (cancelled) return;
        if (!r.ok) throw new Error(await _lapReadError(r));
        const data = await r.json();
        if (cancelled) return;
        setSessions(Array.isArray(data) ? data : (data && Array.isArray(data.sessions) ? data.sessions : []));
      } catch (e) {
        if (cancelled) return;
        setError(e.message || 'failed to load sessions'); setSessions([]);
      }
    })();
    return () => { cancelled = true; };
  }, [cwd]);

  _lapUseEffect(() => {
    const onReload = () => { reload(); };
    window.addEventListener('library:reload', onReload);
    return () => window.removeEventListener('library:reload', onReload);
  }, [reload]);

  const SessionListImpl = window.SessionList;

  return (
    <Card padding={0} style={{ minHeight: compact ? 320 : 420, display: 'flex', flexDirection: 'column' }}>
      {sessions === null && (
        <div style={{ padding: 22 }}><Spinner inline size={14} label="loading sessions…" /></div>
      )}
      {sessions !== null && error && (
        <StatusBanner variant="err" label={`error: ${error}`} inline />
      )}
      {sessions !== null && !SessionListImpl && (
        <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>
          (session list unavailable — orchestrator.jsx not loaded)
        </div>
      )}
      {sessions !== null && SessionListImpl && (
        <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          {/* This cwd-scoped list deliberately omits poolStatusById (and passes
              activeId=null): the per-agent panel sorts by recency only — the
              "open tmux floats to the top" + live dots are a main-sidebar
              affordance, not needed for a small single-agent list. The week-fold
              still applies, but partition()'s minVisible floor keeps the newest
              session visible so an agent untouched for a week is never just a
              bare "show more" divider. */}
          <SessionListImpl
            sessions={sessions} activeId={null}
            onSelect={(id) => onSelectSession && onSelectSession(id)}
            onNew={() => onNewSession && onNewSession()}
            compact={compact}
          />
        </div>
      )}
    </Card>
  );
}

function NewSessionButton({ busy, error, onCreate }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <Button size="md" variant="primary" icon={busy ? 'spinner' : 'plus'} onClick={onCreate} type="button"
        style={busy ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
        {busy ? 'Creating…' : 'New session'}
      </Button>
      {error && <StatusBanner variant="err" label={error} inline />}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// LibraryAgentPanel — top-level Agent tab body.
// ─────────────────────────────────────────────────────────────
function LibraryAgentPanel({ kind, item, libId, compact, onChangeTab }) {
  const [profile, setProfile] = _lapUseState(null);
  const [profileError, setProfileError] = _lapUseState(null);
  const [prompts, setPrompts] = _lapUseState(null);
  const [promptsError, setPromptsError] = _lapUseState(null);
  const [regenBusy, setRegenBusy] = _lapUseState(false);
  const [regenError, setRegenError] = _lapUseState(null);
  const [createBusy, setCreateBusy] = _lapUseState(false);
  const [createError, setCreateError] = _lapUseState(null);

  const cwd = _lapUseMemo(() => _lapDeriveCwd(kind, libId), [kind, libId]);
  const libIdParam = _lapUseMemo(() => _lapDeriveLibIdParam(kind, libId), [kind, libId]);

  const router = (typeof window !== 'undefined' && typeof window.useRouter === 'function') ? window.useRouter() : null;
  const toast = _useToast();

  // Profile fetch (model + sidecar leftovers).
  _lapUseEffect(() => {
    let cancelled = false;
    setProfile(null); setProfileError(null);
    (async () => {
      try {
        const r = await fetch(apiUrl(`/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent`));
        if (cancelled) return;
        if (!r.ok) throw new Error(await _lapReadError(r));
        const data = await r.json();
        if (cancelled) return;
        setProfile((data && data.agent) || {});
      } catch (e) {
        if (cancelled) return;
        setProfileError(e.message || 'failed to load profile');
        setProfile({ model: null, system_prompt: null, skills_allowlist: null });
      }
    })();
    return () => { cancelled = true; };
  }, [kind, libId]);

  // Prompts bundle fetch.
  _lapUseEffect(() => {
    let cancelled = false;
    setPrompts(null); setPromptsError(null);
    (async () => {
      try {
        const r = await fetch(apiUrl(`/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent/prompts`));
        if (cancelled) return;
        if (!r.ok) throw new Error(await _lapReadError(r));
        const data = await r.json();
        if (cancelled) return;
        // Backend shape per plan: identity / custom may be {content, readonly?}
        // or a plain string. Coerce to string for the editor.
        const flatten = (v) => typeof v === 'object' && v !== null ? (v.content || '') : (typeof v === 'string' ? v : '');
        setPrompts({
          general: (data && data.general) || { content: '', readonly: true },
          orchestrator: (data && data.orchestrator) || { content: '', readonly: true },
          identity: flatten(data && data.identity),
          custom: flatten(data && data.custom),
          icon: (data && data.icon) || '',
        });
      } catch (e) {
        if (cancelled) return;
        setPromptsError(e.message || 'failed to load prompts');
        setPrompts({ general: { content: '', readonly: true }, orchestrator: { content: '', readonly: true }, identity: '', custom: '', icon: '' });
      }
    })();
    return () => { cancelled = true; };
  }, [kind, libId]);

  const onProfileSaved = _lapUseCallback((next) => { setProfile(next || null); }, []);
  const onIdentitySaved = _lapUseCallback((next) => { setPrompts((p) => p ? { ...p, identity: next } : p); }, []);
  const onCustomSaved   = _lapUseCallback((next) => { setPrompts((p) => p ? { ...p, custom: next } : p); }, []);
  const onIconSaved     = _lapUseCallback((next) => {
    setPrompts((p) => p ? { ...p, icon: next } : p);
    try { window.dispatchEvent(new CustomEvent('library:reload')); } catch (_) { /* ignore */ }
  }, []);

  const onRegenerate = _lapUseCallback(async () => {
    if (regenBusy) return;
    setRegenBusy(true); setRegenError(null);
    try {
      const url = `/api/library/${_lapKindPath(kind)}/${_lapEncodeLibId(libId)}/agent/regenerate-identity`;
      const r = await fetch(apiUrl(url), { method: 'POST' });
      if (!r.ok) throw new Error(await _lapReadError(r));
      const data = await r.json().catch(() => ({}));
      if (data && data.ok === false && data.error) throw new Error(String(data.error).slice(0, 240));
      setPrompts((p) => p ? {
        ...p,
        identity: typeof data.identity === 'string' ? data.identity : p.identity,
        icon: typeof data.icon === 'string' ? data.icon : p.icon,
      } : p);
      toast && toast('Identity regenerated', 'ok');
      try { window.dispatchEvent(new CustomEvent('library:reload')); } catch (_) { /* ignore */ }
    } catch (e) {
      const msg = e.message || 'regenerate failed';
      setRegenError(msg);
      toast && toast(`Regenerate failed: ${msg}`, 'err');
    } finally { setRegenBusy(false); }
  }, [regenBusy, kind, libId, toast]);

  const onSelectSession = _lapUseCallback((sid) => {
    if (!sid) return;
    if (router && typeof router.push === 'function' && typeof window.buildPath === 'function') {
      router.push(window.buildPath({ section: 'orchestrator', sessionId: sid })); return;
    }
    try { window.location.assign((window.HUB_BASE_PATH || '') + `/orchestrator/${sid}`); } catch (_) { /* ignore */ }
  }, [router]);

  const onCreate = _lapUseCallback(async () => {
    if (createBusy) return;
    setCreateBusy(true); setCreateError(null);
    try {
      const body = { cwd, lib_id: libIdParam, model: (profile && profile.model) || null };
      const r = await fetch(apiUrl('/api/orchestrator/sessions'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await _lapReadError(r));
      const data = await r.json().catch(() => ({}));
      const sid = data && (data.id || (data.session && data.session.id));
      if (!sid) throw new Error('Server returned no session id');
      onSelectSession(sid);
    } catch (e) {
      setCreateError(e.message || 'create failed');
    } finally { setCreateBusy(false); }
  }, [createBusy, profile, cwd, libIdParam, onSelectSession]);

  const promptsLoading = prompts === null;

  return (
    <div style={{
      padding: compact ? 16 : 24, paddingBottom: compact ? 100 : 24,
      display: 'grid', gap: 14, gridTemplateColumns: compact ? '1fr' : '1.4fr 1fr',
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
        {promptsLoading && (
          <Card padding={18}>
            <Spinner inline size={14} label="loading agent prompts…" />
          </Card>
        )}
        {!promptsLoading && promptsError && (
          <Card padding={14}>
            <StatusBanner variant="err" label={`error: ${promptsError}`} inline />
          </Card>
        )}
        {!promptsLoading && (
          <>
            <AgentIdentityHeader
              kind={kind} libId={libId} icon={prompts.icon}
              onIconSaved={onIconSaved} onRegenerate={onRegenerate} regenBusy={regenBusy}
            />
            <AgentPromptCard
              kind={kind} libId={libId} field="identity"
              value={prompts.identity}
              label="Identity"
              sublabel="identity (LLM-generated, editable)"
              placeholder="Description of this agent's purpose, key dirs/files, conventions. Use Regenerate to recreate from scratch via the LLM."
              rows={10} minHeight={180}
              onSaved={onIdentitySaved}
              onRegenerate={onRegenerate} regenBusy={regenBusy} regenError={regenError}
            />
            <AgentPromptCard
              kind={kind} libId={libId} field="custom"
              value={prompts.custom}
              label="Custom"
              sublabel="custom additions (appended after identity)"
              placeholder="Optional ad-hoc additions for this agent only. Appended after identity in the final system prompt."
              rows={6} minHeight={120}
              onSaved={onCustomSaved}
            />
          </>
        )}

        {profile === null && (
          <Card padding={18}>
            <Spinner inline size={14} label="loading agent profile…" />
          </Card>
        )}
        {profile !== null && profileError && (
          <Card padding={14}>
            <StatusBanner variant="err" label={`error: ${profileError}`} inline />
          </Card>
        )}
        {profile !== null && (
          <AgentProfileCard kind={kind} libId={libId} cwd={cwd} profile={profile} onProfileSaved={onProfileSaved} />
        )}

        <AgentSkillsCard kind={kind} libId={libId} />

        <LinkedSchedulersCard kind={kind} libId={libId} />
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
        <Card padding={18}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <div style={{ ..._LAP_LABEL, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flex: 1 }}>New session</div>
          </div>
          <div style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginBottom: 10, lineHeight: 1.5 }}>
            Launch a fresh chat with cwd <code className="mono" style={{
              fontSize: 'var(--t-xs)', color: 'var(--fg-2)', padding: '1px 6px', borderRadius: 4,
              background: 'var(--surface-2)', border: '1px solid var(--hairline)',
            }}>{cwd}</code> and the saved agent profile.
          </div>
          <NewSessionButton busy={createBusy} error={createError} onCreate={onCreate} />
        </Card>

        {/* New-session button lives in the dedicated Card above; the
            embedded SessionList intentionally doesn't get one to avoid
            two CTAs side by side. */}
        <AgentSessionsList cwd={cwd} onSelectSession={onSelectSession} compact={compact} />

        {window.ArtifactGallery && (
          <Card padding={0} style={{ overflow: 'hidden' }}>
            <div className="mono" style={{
              padding: '12px 14px', fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
              textTransform: 'uppercase', letterSpacing: '0.06em',
              borderBottom: '1px solid var(--hairline)',
            }}>Artefakty agenta</div>
            <window.ArtifactGallery scope="agent" libId={libIdParam} compact={compact} />
          </Card>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { LibraryAgentPanel });
