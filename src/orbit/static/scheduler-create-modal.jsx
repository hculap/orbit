// scheduler-create-modal.jsx — 4-step wizard for creating a cron job.
//
// Steps:
//   1. Basics      — name + description
//   2. Trigger     — preset / cron / one-shot, TZ, optional end condition
//   3. Action      — LLM (prompt + agent + tools_allow) or Shell (command)
//   4. Destination — fresh / rolling / existing / none + agent + session
// Final review screen → POST /api/cron/jobs.
//
// On success: dispatch `scheduler:reload`, close modal, navigate to detail.

const { useState: _scmUseState, useEffect: _scmUseEffect, useMemo: _scmUseMemo, useCallback: _scmUseCallback } = React;

const _SCM_STEPS = ['Basics', 'Trigger', 'Action', 'Destination', 'Review'];
const _SCM_TOOLS_ALLOW = ['Bash', 'Read', 'Edit', 'Write', 'Grep', 'Glob', 'Skill'];
const _SCM_NOTIFY_ON = [
  { value: 'failed', label: 'Failed' },
  { value: 'ok', label: 'OK' },
  { value: 'skipped', label: 'Skipped' },
  { value: 'all', label: 'All' },
];

function _scmInputStyle(extra) {
  return {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
    ...(extra || {}),
  };
}
// Expose so the extracted Trigger step (scheduler-create-modal-trigger.jsx)
// can share the same input styling.
window._scmInputStyle = _scmInputStyle;

function _scmDeriveId(name) {
  return String(name || '').toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-').replace(/-+/g, '-')
    .replace(/^-|-$/g, '').slice(0, 64);
}

function _scmPad2(n) { return String(n).padStart(2, '0'); }

function _scmBuildPresetSpec(preset) {
  const { kind, value, hour, minute, weekdays, day } = preset;
  if (kind === 'every-min') {
    const n = Math.max(1, Math.min(59, Number(value) || 5));
    return { type: 'cron', spec: `*/${n} * * * *` };
  }
  if (kind === 'every-hour') {
    const n = Math.max(1, Math.min(23, Number(value) || 1));
    return { type: 'cron', spec: `0 */${n} * * *` };
  }
  if (kind === 'daily') {
    const h = Math.max(0, Math.min(23, Number(hour) || 9));
    const m = Math.max(0, Math.min(59, Number(minute) || 0));
    return { type: 'cron', spec: `${m} ${h} * * *` };
  }
  if (kind === 'weekly') {
    const h = Math.max(0, Math.min(23, Number(hour) || 9));
    const m = Math.max(0, Math.min(59, Number(minute) || 0));
    const ws = (Array.isArray(weekdays) && weekdays.length > 0)
      ? [...weekdays].sort().join(',') : '1';
    return { type: 'cron', spec: `${m} ${h} * * ${ws}` };
  }
  if (kind === 'monthly') {
    const h = Math.max(0, Math.min(23, Number(hour) || 9));
    const m = Math.max(0, Math.min(59, Number(minute) || 0));
    const d = Math.max(1, Math.min(28, Number(day) || 1));
    return { type: 'cron', spec: `${m} ${h} ${d} * *` };
  }
  return { type: 'cron', spec: '0 9 * * *' };
}

function _scmBuildAgentOptions(hubData) {
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

async function _scmReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) detail = String(data.error || data.detail).slice(0, 280);
  } catch (_) { /* ignore */ }
  return detail;
}

// ─────────────────────────────────────────────────────────────
// Stepper
// ─────────────────────────────────────────────────────────────
function _ScmStepper({ step, total, onJump }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      {_SCM_STEPS.slice(0, total).map((label, i) => {
        const active = i === step;
        const done = i < step;
        return (
          <React.Fragment key={label}>
            <button type="button" onClick={() => onJump && onJump(i)} style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '5px 10px', borderRadius: 'var(--r-pill)',
              background: active ? 'var(--accent-soft)' : (done ? 'var(--surface-2)' : 'transparent'),
              border: '1px solid ' + (active ? 'var(--accent-line)' : (done ? 'var(--hairline)' : 'var(--hairline)')),
              color: active ? 'var(--accent)' : (done ? 'var(--fg-2)' : 'var(--fg-4)'),
              fontSize: 'var(--t-xs)', fontWeight: 500, cursor: onJump ? 'pointer' : 'default',
              fontFamily: 'inherit',
            }}>
              <span className="mono" style={{ fontSize: 'var(--t-2xs)' }}>{i + 1}</span>
              <span>{label}</span>
            </button>
            {i < total - 1 && <span style={{ color: 'var(--fg-4)', fontSize: 'var(--t-2xs)' }}>·</span>}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Step 1 — Basics
// ─────────────────────────────────────────────────────────────
function _ScmStepBasics({ name, setName, description, setDescription }) {
  const derivedId = _scmDeriveId(name);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Name *</span>
        <Input
          value={name}
          onChange={setName}
          placeholder="Morning Briefing"
          autoFocus
          size="md"
        />
        {name && (
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            id → {derivedId || '(invalid)'}
          </span>
        )}
      </label>
      <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Description (optional)</span>
        <Textarea
          value={description}
          onChange={setDescription}
          placeholder="What does this job do?"
          rows={3}
          mono={false}
          inputStyle={{ resize: 'vertical', minHeight: 70 }}
        />
      </label>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Step 2 — Trigger
//   Lives in scheduler-create-modal-trigger.jsx (loaded before this file
//   in templates/index.html). Exposed as window._ScmStepTrigger.
// ─────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────
// Step 3 — Action
// ─────────────────────────────────────────────────────────────
function _ScmModeRadio({ value, onChange, options }) {
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {options.map((opt) => {
        const on = value === opt.value;
        return (
          <label key={opt.value} style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '8px 12px', borderRadius: 'var(--r-control)',
            background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
            border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
            color: on ? 'var(--accent)' : 'var(--fg-2)',
            cursor: 'pointer', flex: '1 1 140px', minWidth: 140,
          }}>
            <input type="radio" checked={on} onChange={() => onChange(opt.value)}
              style={{ accentColor: 'var(--accent)' }} />
            <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{opt.label}</span>
              {opt.sub && <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>{opt.sub}</span>}
            </div>
          </label>
        );
      })}
    </div>
  );
}

function _ScmAgentSelect({ value, onChange, options }) {
  return (
    <Select
      value={value || ''}
      onChange={(v) => onChange(v || null)}
      options={options.map((opt) => ({ value: opt.value, label: `${opt.icon} ${opt.label} (${opt.sub})` }))}
      style={{ width: 320 }}
    />
  );
}

function _ScmToolsAllow({ value, onChange }) {
  const set = new Set(value || []);
  const toggle = (t) => {
    const next = new Set(set);
    if (next.has(t)) next.delete(t); else next.add(t);
    onChange(Array.from(next));
  };
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {_SCM_TOOLS_ALLOW.map((t) => {
        const on = set.has(t);
        return (
          <button type="button" key={t} onClick={() => toggle(t)} style={{
            padding: '5px 10px', borderRadius: 'var(--r-sm)',
            background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
            color: on ? 'var(--accent)' : 'var(--fg-2)',
            border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
            fontSize: 'var(--t-xs)', cursor: 'pointer', fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          }}>{t}</button>
        );
      })}
    </div>
  );
}

function _ScmStepAction({ action, setAction, agentOptions }) {
  const update = (patch) => setAction({ ...action, ...patch });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <_ScmModeRadio
        value={action.mode}
        onChange={(m) => update({ mode: m })}
        options={[
          { value: 'llm', label: 'AI prompt', sub: 'spawns claude with a prompt' },
          { value: 'shell', label: 'Shell command', sub: 'deterministic bash, no LLM' },
        ]}
      />
      {action.mode === 'llm' && (
        <>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Prompt *</span>
            <Textarea
              value={action.prompt}
              onChange={(v) => update({ prompt: v })}
              placeholder="Project brief: status, recent commits, open PRs"
              rows={5}
              inputStyle={{ resize: 'vertical', minHeight: 110, lineHeight: 1.5 }}
            />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Run as agent</span>
            <_ScmAgentSelect value={action.agent} onChange={(a) => update({ agent: a })} options={agentOptions} />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
              Model <span style={{ color: 'var(--fg-4)', fontWeight: 400 }}>
                (optional; default = claude CLI default; only used for isolated fires)
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
              style={{ height: 38 }}
            />
          </label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Tools allow (optional, leave empty for default)</span>
            <_ScmToolsAllow value={action.tools_allow} onChange={(t) => update({ tools_allow: t })} />
          </div>
        </>
      )}
      {action.mode === 'shell' && (
        <>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Command *</span>
            <Textarea
              value={action.command}
              onChange={(v) => update({ command: v })}
              placeholder="df -h | tail -n +2"
              rows={4}
              mono
              inputStyle={{ resize: 'vertical', minHeight: 90, lineHeight: 1.4 }}
            />
          </label>
          <StatusBanner variant="warn" label="Runs as the service user with full perms. Inputs aren't sanitized." inline />
        </>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Step 4 — Destination
// ─────────────────────────────────────────────────────────────
function _ScmSessionPicker({ agent, value, onChange }) {
  const [sessions, setSessions] = _scmUseState(null);
  const [error, setError] = _scmUseState(null);

  _scmUseEffect(() => {
    let cancelled = false;
    setSessions(null); setError(null);
    (async () => {
      try {
        const url = agent
          ? `/api/orchestrator/sessions?cwd=${encodeURIComponent(agent.startsWith('areas/') ? `~/Areas/${agent.slice(6)}` : agent.startsWith('projects/') ? `~/Projects/${agent.slice(9)}` : agent.startsWith('resources/') ? `~/Resources/${agent.slice(10)}` : '')}`
          : `/api/orchestrator/sessions?cwd=__global__`;
        const r = await fetch(window.apiUrl(url));
        if (!r.ok) throw new Error(`http ${r.status}`);
        const list = await r.json();
        if (!cancelled) setSessions(Array.isArray(list) ? list : []);
      } catch (e) {
        if (!cancelled) {
          setError((e && e.message) || 'failed to load sessions');
          setSessions([]);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [agent]);

  if (sessions === null) {
    return <Spinner label="Loading sessions…" inline size={14} />;
  }
  if (error) {
    return <StatusBanner variant="err" label={error} inline />;
  }
  if (sessions.length === 0) {
    return <EmptyState message="No sessions for this agent yet — pick another mode" />;
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

function _ScmNotifyOn({ value, onChange }) {
  const set = new Set(Array.isArray(value) ? value : []);
  const toggle = (t) => {
    const next = new Set(set);
    if (next.has(t)) next.delete(t); else next.add(t);
    // Selecting "all" supersedes the other entries; selecting a specific
    // status while "all" is on drops "all" so the result stays meaningful.
    if (t === 'all' && next.has('all')) {
      onChange(['all']);
      return;
    }
    if (t !== 'all' && next.has('all')) next.delete('all');
    onChange(Array.from(next));
  };
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {_SCM_NOTIFY_ON.map((opt) => {
        const on = set.has(opt.value);
        return (
          <button type="button" key={opt.value} onClick={() => toggle(opt.value)} style={{
            padding: '5px 10px', borderRadius: 'var(--r-sm)',
            background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
            color: on ? 'var(--accent)' : 'var(--fg-2)',
            border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
            fontSize: 'var(--t-xs)', cursor: 'pointer', fontFamily: 'JetBrains Mono, ui-monospace, monospace',
          }}>{opt.label}</button>
        );
      })}
    </div>
  );
}

function _ScmStepDestination({ dest, setDest, agentOptions, notify, setNotify }) {
  const update = (patch) => setDest({ ...dest, ...patch });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <_ScmModeRadio
        value={dest.mode}
        onChange={(m) => update({ mode: m })}
        options={[
          { value: 'fresh', label: 'Fresh session', sub: 'new session each fire' },
          { value: 'rolling', label: 'Rolling session', sub: 'one long-lived session' },
          { value: 'existing', label: 'Existing session', sub: 'pick a specific SID' },
          { value: 'telegram', label: 'Telegram', sub: 'full output → Telegram bot, no chat session' },
          { value: 'none', label: 'None', sub: 'logs only, no chat' },
        ]}
      />
      {(dest.mode === 'fresh' || dest.mode === 'rolling' || dest.mode === 'existing') && (
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Agent</span>
          <_ScmAgentSelect value={dest.agent} onChange={(a) => update({ agent: a, session_id: null })} options={agentOptions} />
        </label>
      )}
      {dest.mode === 'rolling' && (
        <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
          First fire creates the session; subsequent fires append.
        </div>
      )}
      {dest.mode === 'existing' && (
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Session</span>
          <_ScmSessionPicker agent={dest.agent} value={dest.session_id}
            onChange={(s) => update({ session_id: s })} />
        </label>
      )}
      {dest.mode === 'telegram' && (
        <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
          Run output (stdout / stderr / LLM reply) goes straight to the Telegram bot.
          Long output is attached as a .txt file. The "Notify on" toggle below
          fires a separate short alert ping — you can have both.
        </div>
      )}
      {dest.mode === 'none' && (
        <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
          Run output only goes to logs (no chat session).
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Notify on</span>
        <_ScmNotifyOn
          value={notify && Array.isArray(notify.on) ? notify.on : ['failed']}
          onChange={(on) => setNotify({ ...(notify || {}), on })}
        />
        <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
          Push via Telegram → mobile. Default: failed only.
        </span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Step 5 — Review
// ─────────────────────────────────────────────────────────────
function _ScmReviewRow({ label, value }) {
  return (
    <div style={{ display: 'flex', gap: 12, padding: '6px 0', borderBottom: '1px solid var(--hairline)' }}>
      <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', minWidth: 110 }}>{label}</span>
      <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg)', flex: 1, wordBreak: 'break-word' }}>{value || '—'}</span>
    </div>
  );
}

function _ScmStepReview({ payload, livePreview }) {
  const t = payload.trigger;
  const triggerLabel = t.type === 'cron' ? `cron · ${t.spec}` : t.type === 'date' ? `one-shot · ${t.spec}` : `${t.type} · ${t.spec}`;
  const action = payload.action || {};
  const actionLabel = action.mode === 'llm'
    ? `LLM · ${(action.prompt || '').slice(0, 90)}${(action.prompt || '').length > 90 ? '…' : ''}`
    : `Shell · ${(action.command || '').slice(0, 90)}${(action.command || '').length > 90 ? '…' : ''}`;
  const dest = payload.destination || {};
  const destLabel = dest.mode === 'none' ? 'none (logs only)'
    : dest.mode === 'telegram' ? 'Telegram (output → bot)'
    : dest.mode === 'existing' ? `existing · ${dest.session_id || '(no session)'}`
    : `${dest.mode} · ${dest.agent || 'Global'}`;
  const ec = payload.end_condition || {};
  const ecLabel = (ec.max_runs || ec.until)
    ? [ec.max_runs ? `max ${ec.max_runs} runs` : null, ec.until ? `until ${ec.until}` : null].filter(Boolean).join(' · ')
    : 'unlimited';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <_ScmReviewRow label="Name" value={payload.name} />
      <_ScmReviewRow label="ID" value={payload.id} />
      {payload.description && <_ScmReviewRow label="Description" value={payload.description} />}
      <_ScmReviewRow label="Trigger" value={`${triggerLabel} · ${t.tz || 'Europe/Warsaw'}`} />
      <_ScmReviewRow label="End condition" value={ecLabel} />
      <_ScmReviewRow label="Action" value={actionLabel} />
      {action.mode === 'llm' && action.agent && <_ScmReviewRow label="Agent" value={action.agent} />}
      {action.mode === 'llm' && action.model && <_ScmReviewRow label="Model" value={action.model} />}
      {action.mode === 'llm' && Array.isArray(action.tools_allow) && action.tools_allow.length > 0 && (
        <_ScmReviewRow label="Tools allow" value={action.tools_allow.join(', ')} />
      )}
      <_ScmReviewRow label="Destination" value={destLabel} />
      {payload.notify && Array.isArray(payload.notify.on) && payload.notify.on.length > 0 && (
        <_ScmReviewRow label="Notify on" value={payload.notify.on.join(', ')} />
      )}
      <div style={{ marginTop: 6 }}>{livePreview}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Main wizard
// ─────────────────────────────────────────────────────────────
function _scmBuildTriggerPayload(triggerState) {
  const tz = triggerState.tz || 'Europe/Warsaw';
  if (triggerState.tab === 'cron') {
    return { type: 'cron', spec: (triggerState.cronExpr || '').trim(), tz };
  }
  if (triggerState.tab === 'date') {
    return { type: 'date', spec: (triggerState.dateValue || '').trim(), tz };
  }
  const built = _scmBuildPresetSpec(triggerState.preset || { kind: 'daily' });
  return { ...built, tz };
}

function SchedulerCreateModal({ onClose, onCreated }) {
  const hubData = (typeof window.useHubData === 'function') ? window.useHubData() : null;
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;

  const [step, setStep] = _scmUseState(0);
  const [name, setName] = _scmUseState('');
  const [description, setDescription] = _scmUseState('');
  const [triggerState, setTriggerState] = _scmUseState({
    tab: 'preset',
    preset: { kind: 'daily', hour: 9, minute: 0, weekdays: ['1'], day: 1, value: 5 },
    cronExpr: '',
    dateValue: '',
    tz: 'Europe/Warsaw',
  });
  const [endCondition, setEndCondition] = _scmUseState({ max_runs: null, until: null });
  const [action, setAction] = _scmUseState({
    mode: 'llm', prompt: '', command: '', agent: null, tools_allow: [], model: null,
  });
  const [dest, setDest] = _scmUseState({
    mode: 'rolling', agent: null, session_id: null,
  });
  const [notify, setNotify] = _scmUseState({ on: ['failed'] });
  const [submitting, setSubmitting] = _scmUseState(false);
  const [error, setError] = _scmUseState(null);

  const agentOptions = _scmUseMemo(() => _scmBuildAgentOptions(hubData), [hubData]);
  const triggerPayload = _scmUseMemo(() => _scmBuildTriggerPayload(triggerState), [triggerState]);
  const livePreview = _scmUseMemo(() => (
    window.NextFiresPreview
      ? <window.NextFiresPreview trigger={triggerPayload} endCondition={endCondition} count={5} />
      : null
  ), [triggerPayload, endCondition]);

  const stepError = _scmUseMemo(() => {
    if (step === 0) {
      if (!name.trim()) return 'Name is required';
      if (!_scmDeriveId(name)) return 'Name must include letters or digits';
    }
    if (step === 1) {
      if (triggerPayload.type === 'cron' && !triggerPayload.spec) return 'Cron expression required';
      if (triggerPayload.type === 'date' && !triggerPayload.spec) return 'Pick a date/time';
    }
    if (step === 2) {
      if (action.mode === 'llm' && !action.prompt.trim()) return 'Prompt is required';
      if (action.mode === 'shell' && !action.command.trim()) return 'Command is required';
    }
    if (step === 3) {
      if (dest.mode === 'existing' && !dest.session_id) return 'Pick a session';
    }
    return null;
  }, [step, name, triggerPayload, action, dest]);

  const buildPayload = _scmUseCallback(() => {
    const id = _scmDeriveId(name);
    const payload = {
      id,
      name: name.trim(),
      enabled: true,
      trigger: triggerPayload,
      end_condition: {
        max_runs: endCondition.max_runs || null,
        until: endCondition.until || null,
      },
      action: action.mode === 'llm' ? {
        mode: 'llm',
        prompt: action.prompt.trim(),
        agent: action.agent || null,
        tools_allow: (action.tools_allow && action.tools_allow.length > 0) ? action.tools_allow : null,
      } : {
        mode: 'shell',
        command: action.command.trim(),
      },
      destination: dest.mode === 'none' ? { mode: 'none' }
        : dest.mode === 'telegram' ? { mode: 'telegram' }
        : dest.mode === 'existing' ? { mode: 'existing', agent: dest.agent || null, session_id: dest.session_id || null }
        : { mode: dest.mode, agent: dest.agent || null },
      concurrency: 'skip',
    };
    if (description.trim()) payload.description = description.trim();
    const notifyOn = (notify && Array.isArray(notify.on)) ? notify.on : [];
    if (notifyOn.length > 0) {
      payload.notify = {
        on: notifyOn,
        priority: (notify && Number.isInteger(notify.priority)) ? notify.priority : null,
        topic: (notify && typeof notify.topic === 'string' && notify.topic.trim()) ? notify.topic.trim() : null,
      };
    }
    return payload;
  }, [name, description, triggerPayload, endCondition, action, dest, notify]);

  const submit = _scmUseCallback(async () => {
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const body = buildPayload();
      const r = await fetch(window.apiUrl('/api/cron/jobs'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await _scmReadError(r));
      const data = await r.json().catch(() => ({}));
      try { window.dispatchEvent(new CustomEvent('scheduler:reload')); } catch (_) { /* ignore */ }
      toast && toast('Cron job created', 'ok');
      if (typeof onCreated === 'function') onCreated(data);
      const newId = (data && (data.id || data.job_id)) || body.id;
      if (router && newId && typeof router.push === 'function') {
        router.push(`/scheduler/${encodeURIComponent(newId)}`);
      }
      if (onClose) onClose();
    } catch (e) {
      setError((e && e.message) || 'Create failed');
    } finally {
      setSubmitting(false);
    }
  }, [submitting, buildPayload, toast, onCreated, onClose, router]);

  const onNext = _scmUseCallback(() => {
    if (stepError) { setError(stepError); return; }
    setError(null);
    if (step < 4) setStep(step + 1);
    else submit();
  }, [step, stepError, submit]);

  const onBack = _scmUseCallback(() => {
    setError(null);
    if (step > 0) setStep(step - 1);
  }, [step]);

  const payloadForReview = _scmUseMemo(() => buildPayload(), [buildPayload]);

  return (
    <Modal open={true} onClose={onClose} title="Create cron job" width={620}>
      <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <_ScmStepper step={step} total={5} onJump={(i) => i <= step && setStep(i)} />
        <div style={{ minHeight: 180 }}>
          {step === 0 && (
            <_ScmStepBasics
              name={name} setName={setName}
              description={description} setDescription={setDescription}
            />
          )}
          {step === 1 && window._ScmStepTrigger && (
            <window._ScmStepTrigger
              trigger={triggerState}
              setTrigger={setTriggerState}
              endCondition={endCondition}
              setEndCondition={setEndCondition}
              livePreview={livePreview}
            />
          )}
          {step === 2 && (
            <_ScmStepAction action={action} setAction={setAction} agentOptions={agentOptions} />
          )}
          {step === 3 && (
            <_ScmStepDestination
              dest={dest} setDest={setDest} agentOptions={agentOptions}
              notify={notify} setNotify={setNotify}
            />
          )}
          {step === 4 && (
            <_ScmStepReview payload={payloadForReview} livePreview={livePreview} />
          )}
        </div>
        {error && <StatusBanner variant="err" label={error} inline />}
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
          <Button variant="quiet" onClick={step === 0 ? onClose : onBack} type="button">
            {step === 0 ? 'Cancel' : 'Back'}
          </Button>
          <Button
            variant="primary"
            type="button"
            icon={submitting ? 'spinner' : (step === 4 ? 'plus' : 'chevron-r')}
            onClick={onNext}
            style={(submitting || stepError) ? { opacity: 0.55, pointerEvents: submitting ? 'none' : 'auto' } : undefined}
          >
            {submitting ? 'Creating…' : (step === 4 ? 'Create job' : 'Next')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

Object.assign(window, { SchedulerCreateModal });
