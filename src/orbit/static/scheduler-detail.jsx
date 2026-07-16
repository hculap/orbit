// scheduler-detail.jsx — /scheduler/<job_id> page.
//
// Tabs: Overview · Trigger · Action · Destination · Runs.
// Each tab has its own Save button that PATCHes /api/cron/jobs/{id} with
// only the changed slice. On success: dispatch `scheduler:reload`.
//
// API contract (Agent B owns):
//   GET    /api/cron/jobs/{id} → {…job, recent_runs?: [...]}
//   PATCH  /api/cron/jobs/{id} → updated job
//   DELETE /api/cron/jobs/{id} → 204
//   POST   /api/cron/jobs/{id}/pause | /resume | /run
//   GET    /api/cron/runs?job_id=<id>&limit=50

const { useState: _sdvUseState, useEffect: _sdvUseEffect, useMemo: _sdvUseMemo, useCallback: _sdvUseCallback } = React;

const _SDV_TABS = [
  { k: 'overview', l: 'Overview', icon: 'cog' },
  { k: 'trigger', l: 'Trigger', icon: 'bell' },
  { k: 'action', l: 'Action', icon: 'sparkle' },
  { k: 'destination', l: 'Destination', icon: 'send' },
  { k: 'runs', l: 'Runs', icon: 'logs' },
];

const _SDV_TZ_OPTIONS = [
  'Europe/Warsaw', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
  'UTC', 'America/New_York', 'America/Los_Angeles', 'America/Chicago',
  'Asia/Tokyo', 'Asia/Singapore', 'Asia/Dubai', 'Australia/Sydney',
];

const _SDV_TOOLS_ALLOW = ['Bash', 'Read', 'Edit', 'Write', 'Grep', 'Glob', 'Skill'];

function _sdvInputStyle(extra) {
  return {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
    ...(extra || {}),
  };
}

async function _sdvReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) detail = String(data.error || data.detail).slice(0, 240);
  } catch (_) { /* ignore */ }
  return detail;
}

function _sdvFmtAbs(ts) {
  if (!ts) return '—';
  const t = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
  if (!Number.isFinite(t)) return '—';
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}

function _sdvFmtRel(ts) {
  if (!ts) return '—';
  const t = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
  if (!Number.isFinite(t)) return '—';
  const diff = Math.floor((Date.now() - t) / 1000);
  if (diff < 0) {
    const future = -diff;
    if (future < 60) return `in ${future}s`;
    if (future < 3600) return `in ${Math.floor(future / 60)}m`;
    if (future < 86400) return `in ${Math.floor(future / 3600)}h`;
    return `in ${Math.floor(future / 86400)}d`;
  }
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function _sdvBuildAgentOptions(hubData) {
  const opts = [{ value: '', label: 'Global', icon: '🤖', sub: 'default ~/' }];
  const push = (item, kind, fallbackIcon) => {
    const libId = item.lib_id || item.name;
    if (!libId) return;
    opts.push({
      value: `${kind}/${libId}`,
      label: item.label || item.name || libId,
      icon: item.icon || fallbackIcon,
      sub: kind,
    });
  };
  ((hubData && hubData.areas) || []).forEach((a) => push(a, 'areas', '📂'));
  ((hubData && hubData.projects) || []).forEach((p) => push(p, 'projects', '📦'));
  ((hubData && hubData.resources) || []).forEach((r) => push(r, 'resources', '📚'));
  return opts;
}

// ─────────────────────────────────────────────────────────────
// Tab strip
// ─────────────────────────────────────────────────────────────
function _SdvTabStrip({ tab, onChange }) {
  return (
    <div style={{
      display: 'flex', gap: 4, padding: 3, flexWrap: 'wrap',
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', alignSelf: 'flex-start',
    }}>
      {_SDV_TABS.map((t) => (
        <button type="button" key={t.k} onClick={() => onChange(t.k)} style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '6px 12px', borderRadius: 'var(--r-sm)',
          background: tab === t.k ? 'var(--accent-soft)' : 'transparent',
          color: tab === t.k ? 'var(--accent)' : 'var(--fg-2)',
          border: '1px solid ' + (tab === t.k ? 'var(--accent-line)' : 'transparent'),
          fontSize: 'var(--t-cap)', fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
        }}>
          <Icon name={t.icon} size={12} />
          <span>{t.l}</span>
        </button>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Overview tab
// ─────────────────────────────────────────────────────────────
function _SdvOverviewTab({ job, onPatch, onPause, onResume, onRunNow, onDelete }) {
  const [name, setName] = _sdvUseState(job.name || '');
  const [description, setDescription] = _sdvUseState(job.description || '');
  const [confirmDelete, setConfirmDelete] = _sdvUseState(false);
  const [saving, setSaving] = _sdvUseState(false);
  const [busy, setBusy] = _sdvUseState(null);

  _sdvUseEffect(() => { setName(job.name || ''); setDescription(job.description || ''); }, [job.id]);

  const dirty = name !== (job.name || '') || description !== (job.description || '');

  const onSave = async () => {
    setSaving(true);
    try { await onPatch({ name, description }); } finally { setSaving(false); }
  };

  const trigger = async (kind, fn) => {
    setBusy(kind);
    try { await fn(); } finally { setBusy(null); }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Card padding={16}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Name</span>
            <Input value={name} onChange={setName} size="md" />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Description</span>
            <Textarea value={description} onChange={setDescription} rows={3} mono={false} />
          </label>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button variant="primary" type="button" icon={saving ? 'spinner' : 'check'}
              onClick={onSave}
              style={(!dirty || saving) ? { opacity: 0.5, pointerEvents: saving ? 'none' : 'auto' } : undefined}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </div>
      </Card>

      <Card padding={16}>
        <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          Status
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <div style={{ fontSize: 'var(--t-sm)' }}>
              {job.enabled ? <Chip mono accent>enabled</Chip> : <Chip mono>paused</Chip>}
            </div>
            <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
              {Number(job.run_count) || 0} runs · {Number(job.failure_count) || 0} failures
            </div>
            {job.last_run_at && (
              <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
                last: {_sdvFmtRel(job.last_run_at)} · {job.last_run_status || '—'}
              </div>
            )}
            {job.next_run_at && job.enabled && (
              <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--accent)' }}>
                next: {_sdvFmtRel(job.next_run_at)} · {_sdvFmtAbs(job.next_run_at)}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {job.enabled
              ? <Button variant="ghost" icon={busy === 'pause' ? 'spinner' : 'square'}
                  onClick={() => trigger('pause', onPause)}>Pause</Button>
              : <Button variant="ghost" icon={busy === 'resume' ? 'spinner' : 'arrow-up'}
                  onClick={() => trigger('resume', onResume)}>Resume</Button>
            }
            <Button variant="primary" icon={busy === 'run' ? 'spinner' : 'send'}
              onClick={() => trigger('run', onRunNow)}>Run now</Button>
          </div>
        </div>
      </Card>

      <Card padding={16} style={{ borderColor: 'var(--err)' }}>
        <div className="mono" style={{
          fontSize: 'var(--t-2xs)', color: 'var(--err)',
          textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8,
        }}>Danger zone</div>
        <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)', lineHeight: 1.55, marginBottom: 12 }}>
          Deleting removes the job from the registry and cancels any future fires.
          Run history stays in the logs.
        </div>
        <Button variant="danger" icon="trash" onClick={() => setConfirmDelete(true)}>
          Delete job
        </Button>
        <Modal open={confirmDelete} onClose={() => setConfirmDelete(false)} title="Delete job?" width={420}>
          <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>
              This permanently removes "{job.name || job.id}" and unschedules it. Are you sure?
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <Button variant="quiet" onClick={() => setConfirmDelete(false)}>Cancel</Button>
              <Button variant="danger" icon="trash"
                onClick={() => { setConfirmDelete(false); onDelete(); }}>Delete</Button>
            </div>
          </div>
        </Modal>
      </Card>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Trigger tab
// ─────────────────────────────────────────────────────────────
function _SdvTriggerTab({ job, onPatch }) {
  const [trig, setTrig] = _sdvUseState(() => ({
    type: (job.trigger && job.trigger.type) || 'cron',
    spec: (job.trigger && job.trigger.spec) || '',
    tz: (job.trigger && job.trigger.tz) || 'Europe/Warsaw',
  }));
  const [endCondition, setEndCondition] = _sdvUseState(() => ({
    max_runs: (job.end_condition && job.end_condition.max_runs) || null,
    until: (job.end_condition && job.end_condition.until) || null,
  }));
  const [saving, setSaving] = _sdvUseState(false);

  _sdvUseEffect(() => {
    setTrig({
      type: (job.trigger && job.trigger.type) || 'cron',
      spec: (job.trigger && job.trigger.spec) || '',
      tz: (job.trigger && job.trigger.tz) || 'Europe/Warsaw',
    });
    setEndCondition({
      max_runs: (job.end_condition && job.end_condition.max_runs) || null,
      until: (job.end_condition && job.end_condition.until) || null,
    });
  }, [job.id]);

  const onSave = async () => {
    setSaving(true);
    try {
      await onPatch({
        trigger: { type: trig.type, spec: trig.spec.trim(), tz: trig.tz },
        end_condition: {
          max_runs: endCondition.max_runs || null,
          until: endCondition.until || null,
        },
      });
    } finally { setSaving(false); }
  };

  return (
    <Card padding={16}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Trigger type</span>
          <Select
            value={trig.type}
            onChange={(v) => setTrig({ ...trig, type: v })}
            options={[
              { value: 'cron', label: 'cron expression' },
              { value: 'interval', label: 'interval (e.g. 5m, 1h)' },
              { value: 'date', label: 'one-shot datetime' },
            ]}
            style={{ width: 200 }}
          />
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Spec</span>
          {trig.type === 'date' ? (
            <Input type="datetime-local" value={trig.spec}
              onChange={(v) => setTrig({ ...trig, spec: v })}
              style={{ width: 280 }} />
          ) : (
            <Input value={trig.spec}
              onChange={(v) => setTrig({ ...trig, spec: v })}
              placeholder={trig.type === 'cron' ? '0 7 * * 1-5' : '5m'}
              mono
            />
          )}
          {trig.type === 'cron' && (
            <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
              5-field cron · <a href="https://crontab.guru" target="_blank" rel="noreferrer"
                style={{ color: 'var(--accent)' }}>crontab.guru</a>
            </span>
          )}
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Timezone</span>
          <Select
            value={trig.tz}
            onChange={(v) => setTrig({ ...trig, tz: v })}
            options={_SDV_TZ_OPTIONS.map((tz) => ({ value: tz, label: tz }))}
            style={{ width: 280 }}
          />
        </label>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>End condition</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', minWidth: 110 }}>Max runs</span>
            <Input type="number" min={1} value={endCondition.max_runs != null ? String(endCondition.max_runs) : ''}
              onChange={(v) => setEndCondition({ ...endCondition, max_runs: v ? Number(v) : null })}
              placeholder="unlimited"
              style={{ width: 140 }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', minWidth: 110 }}>Run until</span>
            <Input type="datetime-local" value={endCondition.until || ''}
              onChange={(v) => setEndCondition({ ...endCondition, until: v || null })}
              style={{ width: 220 }} />
          </div>
        </div>

        {window.NextFiresPreview && (
          <window.NextFiresPreview trigger={trig} endCondition={endCondition} count={5} />
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="primary" type="button" icon={saving ? 'spinner' : 'check'}
            onClick={onSave}
            style={saving ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
            {saving ? 'Saving…' : 'Save trigger'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Action tab
// ─────────────────────────────────────────────────────────────
function _SdvActionTab({ job, onPatch }) {
  const hubData = (typeof window.useHubData === 'function') ? window.useHubData() : null;
  const agentOptions = _sdvUseMemo(() => _sdvBuildAgentOptions(hubData), [hubData]);
  const [action, setAction] = _sdvUseState(() => ({
    mode: (job.action && job.action.mode) || 'llm',
    prompt: (job.action && job.action.prompt) || '',
    command: (job.action && job.action.command) || '',
    agent: (job.action && job.action.agent) || null,
    tools_allow: (job.action && job.action.tools_allow) || [],
    model: (job.action && job.action.model) || null,
  }));
  const [saving, setSaving] = _sdvUseState(false);
  _sdvUseEffect(() => {
    setAction({
      mode: (job.action && job.action.mode) || 'llm',
      prompt: (job.action && job.action.prompt) || '',
      command: (job.action && job.action.command) || '',
      agent: (job.action && job.action.agent) || null,
      tools_allow: (job.action && job.action.tools_allow) || [],
      model: (job.action && job.action.model) || null,
    });
  }, [job.id]);

  const update = (patch) => setAction({ ...action, ...patch });
  const toggleTool = (t) => {
    const set = new Set(action.tools_allow || []);
    if (set.has(t)) set.delete(t); else set.add(t);
    update({ tools_allow: Array.from(set) });
  };

  const onSave = async () => {
    setSaving(true);
    try {
      const payload = action.mode === 'llm' ? {
        action: {
          mode: 'llm',
          prompt: action.prompt.trim(),
          agent: action.agent || null,
          tools_allow: (action.tools_allow && action.tools_allow.length > 0) ? action.tools_allow : null,
          model: action.model || null,
        },
      } : {
        action: { mode: 'shell', command: action.command.trim() },
      };
      await onPatch(payload);
    } finally { setSaving(false); }
  };

  return (
    <Card padding={16}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'flex', gap: 8 }}>
          {[{ v: 'llm', l: 'AI prompt' }, { v: 'shell', l: 'Shell command' }].map((opt) => {
            const on = action.mode === opt.v;
            return (
              <label key={opt.v} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '8px 12px', borderRadius: 'var(--r-control)',
                background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
                border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
                color: on ? 'var(--accent)' : 'var(--fg-2)',
                cursor: 'pointer',
              }}>
                <input type="radio" checked={on} onChange={() => update({ mode: opt.v })}
                  style={{ accentColor: 'var(--accent)' }} />
                <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{opt.l}</span>
              </label>
            );
          })}
        </div>

        {action.mode === 'llm' && (
          <>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Prompt</span>
              <Textarea value={action.prompt}
                onChange={(v) => update({ prompt: v })}
                rows={6}
              />
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Run as agent</span>
              <Select
                value={action.agent || ''}
                onChange={(v) => update({ agent: v || null })}
                options={agentOptions.map((opt) => ({ value: opt.value, label: `${opt.icon} ${opt.label} (${opt.sub})` }))}
                style={{ width: 320 }}
              />
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
                Model <span style={{ color: 'var(--fg-4)', fontWeight: 400 }}>
                  (only used for isolated fires; via-session uses session's model)
                </span>
              </span>
              <Select
                value={action.model || ''}
                onChange={(v) => update({ model: v || null })}
                options={[
                  { value: '', label: 'default (claude CLI)' },
                  { value: 'haiku', label: 'haiku (fast, cheapest)' },
                  { value: 'sonnet', label: 'sonnet (balanced)' },
                  { value: 'opus', label: 'opus (deep reasoning)' },
                ]}
                style={{ width: 320 }}
              />
            </label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Tools allow (empty = default)</span>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {_SDV_TOOLS_ALLOW.map((t) => {
                  const on = (action.tools_allow || []).includes(t);
                  return (
                    <button type="button" key={t} onClick={() => toggleTool(t)} style={{
                      padding: '5px 10px', borderRadius: 'var(--r-sm)',
                      background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
                      color: on ? 'var(--accent)' : 'var(--fg-2)',
                      border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
                      fontSize: 'var(--t-xs)', cursor: 'pointer', fontFamily: 'JetBrains Mono, ui-monospace, monospace',
                    }}>{t}</button>
                  );
                })}
              </div>
            </div>
          </>
        )}

        {action.mode === 'shell' && (
          <>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Command</span>
              <Textarea value={action.command}
                onChange={(v) => update({ command: v })}
                rows={5}
                mono
              />
            </label>
            <StatusBanner variant="warn" label="Runs as the service user with full perms. Inputs aren't sanitized." inline />
          </>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="primary" type="button" icon={saving ? 'spinner' : 'check'}
            onClick={onSave}
            style={saving ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
            {saving ? 'Saving…' : 'Save action'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Destination tab
// ─────────────────────────────────────────────────────────────
function _SdvDestSessionPicker({ agent, value, onChange }) {
  const [sessions, setSessions] = _sdvUseState(null);
  _sdvUseEffect(() => {
    let cancelled = false;
    setSessions(null);
    (async () => {
      try {
        const url = agent
          ? `/api/orchestrator/sessions?cwd=${encodeURIComponent(agent.startsWith('areas/') ? `~/Areas/${agent.slice(6)}` : agent.startsWith('projects/') ? `~/Projects/${agent.slice(9)}` : agent.startsWith('resources/') ? `~/Resources/${agent.slice(10)}` : '')}`
          : `/api/orchestrator/sessions?cwd=__global__`;
        const r = await fetch(window.apiUrl(url));
        if (!r.ok) throw new Error('http ' + r.status);
        const list = await r.json();
        if (!cancelled) setSessions(Array.isArray(list) ? list : []);
      } catch (_) { if (!cancelled) setSessions([]); }
    })();
    return () => { cancelled = true; };
  }, [agent]);

  if (sessions === null) {
    return <Spinner label="Loading sessions…" inline size={14} />;
  }
  return (
    <Select
      value={value || ''}
      onChange={(v) => onChange(v || null)}
      placeholder="— select session —"
      options={sessions.map((s) => ({ value: s.id, label: (s.title || s.id).slice(0, 80) }))}
      style={{ width: '100%' }}
    />
  );
}

function _SdvDestinationTab({ job, onPatch }) {
  const hubData = (typeof window.useHubData === 'function') ? window.useHubData() : null;
  const agentOptions = _sdvUseMemo(() => _sdvBuildAgentOptions(hubData), [hubData]);
  const [dest, setDest] = _sdvUseState(() => ({
    mode: (job.destination && job.destination.mode) || 'rolling',
    agent: (job.destination && job.destination.agent) || null,
    session_id: (job.destination && job.destination.session_id) || null,
  }));
  const [saving, setSaving] = _sdvUseState(false);
  _sdvUseEffect(() => {
    setDest({
      mode: (job.destination && job.destination.mode) || 'rolling',
      agent: (job.destination && job.destination.agent) || null,
      session_id: (job.destination && job.destination.session_id) || null,
    });
  }, [job.id]);

  const update = (patch) => setDest({ ...dest, ...patch });

  const onSave = async () => {
    setSaving(true);
    try {
      const payload = dest.mode === 'none' ? { destination: { mode: 'none' } }
        : dest.mode === 'existing'
          ? { destination: { mode: 'existing', agent: dest.agent || null, session_id: dest.session_id || null } }
          : { destination: { mode: dest.mode, agent: dest.agent || null } };
      await onPatch(payload);
    } finally { setSaving(false); }
  };

  return (
    <Card padding={16}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {[
            { v: 'fresh', l: 'Fresh' },
            { v: 'rolling', l: 'Rolling' },
            { v: 'existing', l: 'Existing' },
            { v: 'none', l: 'None' },
          ].map((opt) => {
            const on = dest.mode === opt.v;
            return (
              <label key={opt.v} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '8px 14px', borderRadius: 'var(--r-control)',
                background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
                border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
                color: on ? 'var(--accent)' : 'var(--fg-2)',
                cursor: 'pointer',
              }}>
                <input type="radio" checked={on} onChange={() => update({ mode: opt.v, session_id: null })}
                  style={{ accentColor: 'var(--accent)' }} />
                <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{opt.l}</span>
              </label>
            );
          })}
        </div>
        {(dest.mode === 'fresh' || dest.mode === 'rolling' || dest.mode === 'existing') && (
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Agent</span>
            <Select
              value={dest.agent || ''}
              onChange={(v) => update({ agent: v || null, session_id: null })}
              options={agentOptions.map((opt) => ({ value: opt.value, label: `${opt.icon} ${opt.label} (${opt.sub})` }))}
              style={{ width: 320 }}
            />
          </label>
        )}
        {dest.mode === 'existing' && (
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Session</span>
            <_SdvDestSessionPicker agent={dest.agent} value={dest.session_id}
              onChange={(s) => update({ session_id: s })} />
          </label>
        )}
        {dest.mode === 'rolling' && (
          <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            First fire creates the session; subsequent fires append.
          </div>
        )}
        {dest.mode === 'none' && (
          <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            Run output only goes to logs (no chat session).
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="primary" type="button" icon={saving ? 'spinner' : 'check'}
            onClick={onSave}
            style={saving ? { opacity: 0.6, pointerEvents: 'none' } : undefined}>
            {saving ? 'Saving…' : 'Save destination'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Runs tab — extracted to scheduler-detail-runs.jsx (window.SdvRunsTab)
// ─────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────
// Main view
// ─────────────────────────────────────────────────────────────
function SchedulerDetailView({ jobId, compact, onBack }) {
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const [job, setJob] = _sdvUseState(null);
  const [error, setError] = _sdvUseState(null);
  const [tab, setTab] = _sdvUseState('overview');

  const load = _sdvUseCallback(async () => {
    setError(null);
    try {
      const r = await fetch(window.apiUrl(`/api/cron/jobs/${encodeURIComponent(jobId)}`));
      if (!r.ok) throw new Error(await _sdvReadError(r));
      const data = await r.json();
      setJob(data || null);
    } catch (e) {
      setError((e && e.message) || 'failed to load job');
      setJob({ id: jobId });
    }
  }, [jobId]);

  _sdvUseEffect(() => {
    if (!jobId) return;
    setJob(null);
    load();
  }, [jobId, load]);

  _sdvUseEffect(() => {
    const onReload = () => load();
    window.addEventListener('scheduler:reload', onReload);
    return () => window.removeEventListener('scheduler:reload', onReload);
  }, [load]);

  const navigateBack = _sdvUseCallback(() => {
    if (typeof onBack === 'function') { onBack(); return; }
    if (router && typeof router.push === 'function') router.push('/scheduler');
    else { try { window.location.assign((window.HUB_BASE_PATH || '') + '/scheduler'); } catch (_) { /* ignore */ } }
  }, [onBack, router]);

  const onPatch = _sdvUseCallback(async (patch) => {
    try {
      const r = await fetch(window.apiUrl(`/api/cron/jobs/${encodeURIComponent(jobId)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(await _sdvReadError(r));
      const data = await r.json().catch(() => null);
      if (data && data.id) setJob(data); else await load();
      try { window.dispatchEvent(new CustomEvent('scheduler:reload')); } catch (_) { /* ignore */ }
      toast && toast('Saved', 'ok');
    } catch (e) {
      toast && toast(`Save failed: ${(e && e.message) || 'error'}`, 'err');
    }
  }, [jobId, load, toast]);

  const onAction = _sdvUseCallback(async (action) => {
    try {
      const r = await fetch(window.apiUrl(`/api/cron/jobs/${encodeURIComponent(jobId)}/${action}`), {
        method: 'POST',
      });
      if (!r.ok) throw new Error(await _sdvReadError(r));
      try { window.dispatchEvent(new CustomEvent('scheduler:reload')); } catch (_) { /* ignore */ }
      toast && toast(`${action[0].toUpperCase()}${action.slice(1)} ok`, 'ok');
      await load();
    } catch (e) {
      toast && toast(`${action} failed: ${(e && e.message) || 'error'}`, 'err');
    }
  }, [jobId, load, toast]);

  const onDelete = _sdvUseCallback(async () => {
    try {
      const r = await fetch(window.apiUrl(`/api/cron/jobs/${encodeURIComponent(jobId)}`), {
        method: 'DELETE',
      });
      if (!r.ok) throw new Error(await _sdvReadError(r));
      try { window.dispatchEvent(new CustomEvent('scheduler:reload')); } catch (_) { /* ignore */ }
      toast && toast('Job deleted', 'ok');
      navigateBack();
    } catch (e) {
      toast && toast(`Delete failed: ${(e && e.message) || 'error'}`, 'err');
    }
  }, [jobId, navigateBack, toast]);

  if (job === null) {
    return (
      <div style={{ padding: compact ? 16 : 28 }}>
        <Spinner label="Loading job…" />
      </div>
    );
  }

  return (
    <div style={{
      padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28,
      display: 'flex', flexDirection: 'column', gap: 14,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <window.IconButton icon="chevron-l" label="Back" size={32} onClick={navigateBack} />
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', letterSpacing: '0.08em', textTransform: 'uppercase' }}>
          Scheduler · {job.name || job.id}
        </span>
      </div>

      {error && <StatusBanner variant="err" label={`error: ${error}`} />}

      <_SdvTabStrip tab={tab} onChange={setTab} />

      {tab === 'overview' && (
        <_SdvOverviewTab
          job={job}
          onPatch={onPatch}
          onPause={() => onAction('pause')}
          onResume={() => onAction('resume')}
          onRunNow={() => onAction('run')}
          onDelete={onDelete}
        />
      )}
      {tab === 'trigger' && <_SdvTriggerTab job={job} onPatch={onPatch} />}
      {tab === 'action' && <_SdvActionTab job={job} onPatch={onPatch} />}
      {tab === 'destination' && <_SdvDestinationTab job={job} onPatch={onPatch} />}
      {tab === 'runs' && window.SdvRunsTab && <window.SdvRunsTab jobId={job.id || jobId} />}
    </div>
  );
}

Object.assign(window, { SchedulerDetailView });
