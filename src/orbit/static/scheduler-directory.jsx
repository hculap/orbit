// scheduler-directory.jsx — /scheduler landing page.
//
// Card grid of every cron job. Each card shows name, status pill, trigger
// summary in plain English, "next fire" relative time, and a sparkline of
// the last 20 runs (success/fail). Header has a "+ Create job" button which
// opens <SchedulerCreateModal>.
//
// API contract (Agent B owns):
//   GET /api/cron/jobs → [{id, name, enabled, trigger, action, destination,
//     last_run_at, last_run_status, next_run_at, run_count, failure_count,
//     recent_runs?: [{status}, ...], emoji?: "..."}]

const { useMemo: _schUseMemo, useState: _schUseState, useEffect: _schUseEffect, useCallback: _schUseCallback } = React;

const _SCH_DEFAULT_ICON = '⏰';

const _SCH_WEEKDAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function _schPad2(n) { return String(n).padStart(2, '0'); }

// Translate `{type, spec, tz}` into a short human-readable summary. Best-
// effort — falls back to raw spec for cron expressions we don't recognise.
function _schTriggerSummary(trigger) {
  if (!trigger || !trigger.type || !trigger.spec) return '—';
  const tz = trigger.tz || 'Europe/Warsaw';
  if (trigger.type === 'date') {
    return `one-shot · ${trigger.spec} · ${tz}`;
  }
  if (trigger.type === 'interval') {
    return `every ${trigger.spec} · ${tz}`;
  }
  if (trigger.type === 'cron') {
    const m = String(trigger.spec).trim().split(/\s+/);
    if (m.length === 5) {
      const [min, hr, dom, mon, dow] = m;
      const everyMin = /^\*\/(\d+)$/.exec(min);
      if (everyMin && hr === '*' && dom === '*' && mon === '*' && dow === '*') {
        return `every ${everyMin[1]} min · ${tz}`;
      }
      const everyHour = /^\*\/(\d+)$/.exec(hr);
      if (min === '0' && everyHour && dom === '*' && mon === '*' && dow === '*') {
        return `every ${everyHour[1]}h · ${tz}`;
      }
      const isSimpleTime = /^\d+$/.test(min) && /^\d+$/.test(hr);
      if (isSimpleTime && dom === '*' && mon === '*') {
        const time = `${_schPad2(hr)}:${_schPad2(min)}`;
        if (dow === '*') return `daily at ${time} · ${tz}`;
        if (dow === '1-5') return `weekdays at ${time} · ${tz}`;
        const days = dow.split(',').map((d) => _SCH_WEEKDAY_NAMES[Number(d)] || d).join('/');
        return `${days} at ${time} · ${tz}`;
      }
      if (isSimpleTime && /^\d+$/.test(dom) && mon === '*' && dow === '*') {
        return `monthly day ${dom} at ${_schPad2(hr)}:${_schPad2(min)} · ${tz}`;
      }
    }
    return `cron · ${trigger.spec} · ${tz}`;
  }
  return `${trigger.type} · ${trigger.spec}`;
}

function _schNextFireLabel(next, enabled) {
  if (!enabled) return 'paused';
  if (!next) return 'no next fire';
  const t = typeof next === 'number' ? next * 1000 : Date.parse(next);
  if (!Number.isFinite(t)) return 'no next fire';
  const diff = t - Date.now();
  if (diff <= 0) return 'now';
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `Next: in ${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `Next: in ${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 48) {
    const remMin = min - hr * 60;
    return remMin > 0 ? `Next: in ${hr}h ${remMin}m` : `Next: in ${hr}h`;
  }
  const days = Math.floor(hr / 24);
  return `Next: in ${days}d`;
}

function _schStatusInfo(job) {
  if (!job.enabled) return { label: 'paused', color: 'var(--fg-3)', bg: 'var(--surface-2)', border: 'var(--hairline)' };
  const failCount = Number(job.failure_count) || 0;
  const lastStatus = job.last_run_status;
  if (lastStatus === 'failed') return { label: 'failing', color: 'var(--err)', bg: 'var(--err-bg)', border: 'var(--err)' };
  if (failCount > 0 && Number(job.run_count) > 0 && failCount * 3 >= Number(job.run_count)) {
    return { label: 'flaky', color: 'oklch(0.78 0.14 80)', bg: 'oklch(0.32 0.10 80 / 0.18)', border: 'oklch(0.70 0.16 80 / 0.4)' };
  }
  return { label: 'running', color: 'var(--ok, #5fbf6c)', bg: 'oklch(0.32 0.10 150 / 0.18)', border: 'oklch(0.65 0.14 150 / 0.4)' };
}

function _schRunBars(recentRuns) {
  // recentRuns: array of {status} from most-recent → oldest. Render as 20 bars
  // in chronological order (oldest left → newest right). Falls back to empty.
  const slots = 20;
  const list = Array.isArray(recentRuns) ? recentRuns.slice(0, slots) : [];
  // Reverse so we render oldest → newest, left → right.
  const items = list.slice().reverse();
  if (items.length === 0) {
    return (
      <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
        no runs yet
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', gap: 2, alignItems: 'flex-end', height: 16 }}>
      {Array.from({ length: slots }).map((_, i) => {
        const r = items[i];
        if (!r) {
          return <span key={i} style={{ width: 4, height: 6, background: 'var(--surface-3)', borderRadius: 1 }} />;
        }
        const ok = r.status === 'ok';
        const skip = r.status === 'skipped' || r.status === 'cancelled';
        return (
          <span key={i} style={{
            width: 4, height: ok ? 16 : (skip ? 8 : 14),
            background: ok ? 'var(--ok, #5fbf6c)' : (skip ? 'var(--fg-4)' : 'var(--err)'),
            borderRadius: 1,
          }} title={r.status} />
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Card
// ─────────────────────────────────────────────────────────────
function _schAgentLabel(agentKey) {
  if (!agentKey || agentKey === 'global' || agentKey === 'null') return 'Global';
  const m = String(agentKey).match(/^(areas|projects|resources)\/(.+)$/);
  if (!m) return agentKey;
  return m[2].split('/').pop();
}

function _schMatchesQuery(job, q) {
  if (!q) return true;
  const action = job.action || {};
  const trigger = job.trigger || {};
  const destination = job.destination || {};
  const haystack = [
    job.id,
    job.name,
    job.description,
    action.prompt,
    action.command,
    action.agent,
    action.mode,
    destination.agent,
    destination.path,
    trigger.spec,
    trigger.type,
    _schTriggerSummary(trigger),
  ].map((v) => (typeof v === 'string' ? v.toLowerCase() : '')).join('\n');
  return haystack.includes(q);
}

function _schLinkedAgents(job) {
  const seen = new Set();
  const out = [];
  for (const key of [
    job && job.action && job.action.agent,
    job && job.destination && job.destination.agent,
  ]) {
    const norm = (typeof key === 'string' ? key.trim() : '') || 'global';
    if (seen.has(norm)) continue;
    seen.add(norm);
    out.push(norm);
  }
  return out;
}

function _SchedulerCard({ job, onOpen }) {
  const icon = job.emoji || job.icon || _SCH_DEFAULT_ICON;
  const status = _schStatusInfo(job);
  const summary = _schTriggerSummary(job.trigger);
  const next = _schNextFireLabel(job.next_run_at, job.enabled);
  const agents = _schLinkedAgents(job);
  return (
    <window.Card hover padding={16} onClick={onOpen} style={{ cursor: 'pointer' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <div style={{
          width: 40, height: 40, borderRadius: '50%',
          background: 'var(--accent-soft)', border: '1px solid var(--accent-line)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 'var(--t-h2)', lineHeight: 1, flexShrink: 0,
        }}>{icon}</div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, justifyContent: 'space-between' }}>
            <span style={{
              fontSize: 'var(--t-body)', fontWeight: 500,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{job.name || job.id}</span>
            <span className="mono" style={{
              fontSize: 'var(--t-2xs)', padding: '2px 7px', borderRadius: 'var(--r-pill)',
              background: status.bg, color: status.color, border: '1px solid ' + status.border,
              flexShrink: 0,
            }}>{status.label}</span>
          </div>
          <div className="mono" style={{
            marginTop: 5, fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>{summary}</div>
          <div className="mono" style={{ marginTop: 3, fontSize: 'var(--t-xs)', color: job.enabled ? 'var(--accent)' : 'var(--fg-4)' }}>
            {next}
          </div>
          {agents.length > 0 && (
            <div style={{ marginTop: 6, display: 'flex', gap: 5, flexWrap: 'wrap' }}>
              {agents.map((key) => (
                <span key={key} className="mono" title={`Linked to ${key}`} style={{
                  fontSize: 'var(--t-2xs)', padding: '2px 7px', borderRadius: 'var(--r-pill)',
                  color: 'var(--fg-2)', background: 'var(--surface-2)',
                  border: '1px solid var(--hairline)',
                }}>🔗 {_schAgentLabel(key)}</span>
              ))}
            </div>
          )}
          <div style={{ marginTop: 10 }}>
            {_schRunBars(job.recent_runs)}
          </div>
        </div>
      </div>
    </window.Card>
  );
}

// ─────────────────────────────────────────────────────────────
// Main view
// ─────────────────────────────────────────────────────────────
function SchedulerDirectoryView({ compact }) {
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;
  const [jobs, setJobs] = _schUseState(null);
  const [error, setError] = _schUseState(null);
  const [createOpen, setCreateOpen] = _schUseState(false);
  const [query, setQuery] = _schUseState('');
  // Sub-tab: 'scheduler' (cron jobs, original view) or 'reminders' (delegates
  // to window.StandaloneRemindersView from tasks.jsx). Reminders are scheduled
  // notifications, conceptually the same surface as cron — hence a tab here
  // rather than as a top-level sidebar item.
  const [subTab, setSubTab] = _schUseState(() => {
    try { return localStorage.getItem('hub:scheduler:sub') || 'scheduler'; }
    catch (_) { return 'scheduler'; }
  });
  const setSubTabPersist = (v) => {
    setSubTab(v);
    try { localStorage.setItem('hub:scheduler:sub', v); } catch (_) {}
  };

  const load = _schUseCallback(async () => {
    setError(null);
    try {
      const r = await fetch(window.apiUrl('/api/cron/jobs'));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const data = await r.json();
      const list = Array.isArray(data) ? data
        : (data && Array.isArray(data.jobs)) ? data.jobs
        : [];
      setJobs(list);
    } catch (e) {
      setError((e && e.message) || 'failed to load jobs');
      setJobs([]);
    }
  }, []);

  _schUseEffect(() => { load(); }, [load]);

  _schUseEffect(() => {
    const onReload = () => { load(); };
    window.addEventListener('scheduler:reload', onReload);
    return () => window.removeEventListener('scheduler:reload', onReload);
  }, [load]);

  // Re-render every 30s so "Next fire: in 3h" stays current without polling.
  _schUseEffect(() => {
    const id = setInterval(() => setJobs((prev) => prev ? prev.slice() : prev), 30000);
    return () => clearInterval(id);
  }, []);

  const filtered = _schUseMemo(() => {
    const list = Array.isArray(jobs) ? jobs : [];
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter((j) => _schMatchesQuery(j, q));
  }, [jobs, query]);

  const sorted = _schUseMemo(() => {
    const arr = Array.isArray(filtered) ? filtered.slice() : [];
    arr.sort((a, b) => {
      const ae = a.enabled ? 0 : 1;
      const be = b.enabled ? 0 : 1;
      if (ae !== be) return ae - be;
      const an = Number(a.next_run_at) || Infinity;
      const bn = Number(b.next_run_at) || Infinity;
      if (an !== bn) return an - bn;
      return String(a.name || '').localeCompare(String(b.name || ''));
    });
    return arr;
  }, [filtered]);

  const navigate = _schUseCallback((id) => {
    if (!id) return;
    const path = `/scheduler/${encodeURIComponent(id)}`;
    if (router && typeof router.push === 'function') router.push(path);
    else { try { window.location.assign((window.HUB_BASE_PATH || '') + path); } catch (_) { /* ignore */ } }
  }, [router]);

  const headerActions = (
    <>
      <Input
        value={query}
        onChange={setQuery}
        placeholder="Search name, prompt, trigger…"
        ariaLabel="Search jobs"
        icon="search"
        size="sm"
        style={{ minWidth: 240 }}
        inputStyle={{ fontSize: 13, fontFamily: 'inherit', color: 'var(--fg)' }}
      />
      <window.IconButton icon="refresh" label="Reload" size={34} onClick={load} />
      <window.Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>
        Create job
      </window.Button>
    </>
  );

  const isLoading = jobs === null;
  const totalJobs = (Array.isArray(jobs) ? jobs : []).length;
  const filterActive = query.trim().length > 0;
  const isEmpty = !isLoading && sorted.length === 0;
  const emptyDueToFilter = !isLoading && totalJobs > 0 && filterActive && sorted.length === 0;

  const subTabStrip = (
    <div style={{ display: 'flex', gap: 4, padding: '8px 14px 0', borderBottom: '1px solid var(--hairline)', background: 'var(--surface-1)' }}>
      {[{ key: 'scheduler', label: 'Scheduler' }, { key: 'reminders', label: 'Reminders' }].map(t => {
        const active = subTab === t.key;
        return (
          <button key={t.key} onClick={() => setSubTabPersist(t.key)} style={{
            background: 'transparent',
            color: active ? 'var(--fg-1)' : 'var(--fg-3)',
            border: 'none',
            padding: '8px 14px',
            fontSize: 'var(--t-sm)',
            fontWeight: 500,
            cursor: 'pointer',
            borderBottom: active ? '2px solid var(--accent)' : '2px solid transparent',
            marginBottom: -1,
          }}>{t.label}</button>
        );
      })}
    </div>
  );

  if (subTab === 'reminders') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        {subTabStrip}
        {window.StandaloneRemindersView
          ? <window.StandaloneRemindersView />
          : <div style={{ padding: 24, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>Reminders view not loaded.</div>}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      {subTabStrip}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <window.SectionHeader
        eyebrow="Schedule"
        title="Scheduler"
        count={isLoading
          ? ''
          : (filterActive
            ? `${sorted.length} of ${totalJobs} match`
            : `${sorted.length} job${sorted.length === 1 ? '' : 's'}`)}
        actions={headerActions}
      />

      {error && <StatusBanner variant="err" label={`error: ${error}`} style={{ marginTop: 18 }} />}

      {isLoading && <div style={{ marginTop: 24 }}><Spinner label="Loading jobs…" /></div>}

      {emptyDueToFilter && (
        <EmptyState
          message={`No jobs match “${query.trim()}”`}
          sub="Try a different term or clear the search."
          padded
        />
      )}

      {isEmpty && !emptyDueToFilter && (
        <EmptyState
          icon="clock"
          message="No cron jobs yet"
          sub="Schedule a recurring AI prompt or shell command. Trigger an agent every morning, run a backup nightly, ping a webhook every 5 minutes — your call."
          action={<window.Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>Create your first job</window.Button>}
          centered
          padded
        />
      )}

      {!isEmpty && !isLoading && (
        <div style={{
          marginTop: 18, display: 'grid', gap: 12,
          gridTemplateColumns: compact ? 'minmax(0, 1fr)' : 'repeat(auto-fill, minmax(300px, minmax(0, 1fr)))',
        }}>
          {sorted.map((j) => (
            <_SchedulerCard key={j.id} job={j} onOpen={() => navigate(j.id)} />
          ))}
        </div>
      )}

      {createOpen && window.SchedulerCreateModal && (
        <window.SchedulerCreateModal
          onClose={() => setCreateOpen(false)}
          onCreated={() => { /* scheduler:reload event fired by modal */ }}
        />
      )}
      </div>
    </div>
  );
}

Object.assign(window, { SchedulerDirectoryView });
