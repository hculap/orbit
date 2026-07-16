// scheduler-detail-runs.jsx — Runs tab for /scheduler/<job_id>.
//
// Exposes:
//   window.SdvRunsTab       — the tab component (jobId prop)
//   window.SdvRunStatusIcon — small status icon (used inside the tab)
//   window.SdvRunRow        — single row inside the runs table
//
// Loaded BEFORE scheduler-detail.jsx in templates/index.html.

const { useState: _sdvrUseState, useEffect: _sdvrUseEffect, useMemo: _sdvrUseMemo, useCallback: _sdvrUseCallback, useRef: _sdvrUseRef } = React;

// Local copies of small formatting helpers — kept in sync with scheduler-detail.jsx
// to avoid load-order coupling. These are pure formatters; if they ever drift,
// behaviour stays identical because both copies follow the same contract.
function _sdvrFmtAbs(ts) {
  if (!ts) return '—';
  const t = typeof ts === 'number' ? ts * 1000 : Date.parse(ts);
  if (!Number.isFinite(t)) return '—';
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}

function _sdvrFmtRel(ts) {
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

function _sdvrFmtDuration(ms) {
  if (!ms && ms !== 0) return '—';
  const n = Number(ms);
  if (!Number.isFinite(n)) return '—';
  if (n < 1000) return `${n}ms`;
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`;
  const min = Math.floor(n / 60000);
  const sec = Math.floor((n % 60000) / 1000);
  return `${min}m ${sec}s`;
}

async function _sdvrReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) detail = String(data.error || data.detail).slice(0, 240);
  } catch (_) { /* ignore */ }
  return detail;
}

function _SdvRunStatusIcon({ status }) {
  const map = {
    ok: { name: 'check', color: 'var(--ok, #5fbf6c)' },
    failed: { name: 'close', color: 'var(--err)' },
    skipped: { name: 'square', color: 'var(--fg-4)' },
    cancelled: { name: 'square', color: 'var(--fg-4)' },
  };
  const m = map[status] || { name: 'circle', color: 'var(--fg-3)' };
  return <Icon name={m.name} size={14} color={m.color} />;
}

// Pull a $HOME-anchored PARA path out of the job's shell command so the
// diagnose chat opens with the right cwd. Falls back to action.agent if it
// looks like a path, else null (Global agent).
//
// 2026-05-31: previously matched an optional sub-segment via
// `(?:/[A-Za-z0-9._-]+)?`, which captured trailing filenames like `/.env`
// or `/data` and produced cwds that pointed at FILES, not directories —
// the agent endpoint then rejected with "cwd must be an existing
// directory". Anchor strictly on the project/area root.
function _sdvrResolveJobCwd(action) {
  if (!action || typeof action !== 'object') return null;
  const cmd = String(action.command || '');
  const m = cmd.match(/\/home\/[a-z][a-z0-9-]*\/(?:Projects|Areas)\/[A-Za-z0-9._-]+/);
  if (m) return m[0];
  const agent = action.agent;
  if (typeof agent === 'string' && agent.startsWith('/')) return agent;
  return null;
}

function _SdvRunRow({ run, expanded, onToggle, onDiagnose }) {
  const stderrTail = run.stderr_tail || run.error || '';
  return (
    <>
      <tr onClick={onToggle} style={{ cursor: 'pointer', borderBottom: '1px solid var(--hairline)' }}>
        <td style={{ padding: '8px 6px', width: 24 }}>
          <_SdvRunStatusIcon status={run.status} />
        </td>
        <td style={{ padding: '8px 6px', fontSize: 'var(--t-cap)' }} title={_sdvrFmtAbs(run.started_at)}>
          {_sdvrFmtRel(run.started_at)}
        </td>
        <td className="mono" style={{ padding: '8px 6px', fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
          {_sdvrFmtDuration(run.duration_ms)}
        </td>
        <td style={{ padding: '8px 6px' }}>
          <span className="mono" style={{
            fontSize: 'var(--t-2xs)', padding: '2px 7px', borderRadius: 'var(--r-pill)',
            background: run.trigger === 'manual' ? 'var(--accent-soft)' : 'var(--surface-2)',
            color: run.trigger === 'manual' ? 'var(--accent)' : 'var(--fg-3)',
            border: '1px solid ' + (run.trigger === 'manual' ? 'var(--accent-line)' : 'var(--hairline)'),
          }}>{run.trigger || 'scheduled'}</span>
        </td>
        <td style={{ padding: '8px 6px', fontSize: 'var(--t-xs)' }}>
          {run.session_id ? (
            <a href={(window.HUB_BASE_PATH || '') + `/chat/${encodeURIComponent(run.session_id)}`}
               target="_blank" rel="noreferrer"
               onClick={(e) => e.stopPropagation()}
               className="mono"
               style={{ color: 'var(--accent)', textDecoration: 'underline' }}>
              {String(run.session_id).slice(0, 8)}…
            </a>
          ) : <span className="mono" style={{ color: 'var(--fg-4)' }}>—</span>}
        </td>
        <td className="mono" style={{ padding: '8px 6px', fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textAlign: 'right' }}>
          {run.exit_code != null ? `exit ${run.exit_code}` : '—'}
        </td>
      </tr>
      {expanded && (
        <tr style={{ borderBottom: '1px solid var(--hairline)' }}>
          <td colSpan={6} style={{ padding: '10px 12px', background: 'var(--surface-2)' }}>
            {stderrTail ? (
              <pre className="mono" style={{
                margin: 0, fontSize: 'var(--t-xs)', color: 'var(--fg-2)',
                whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                maxHeight: 240, overflow: 'auto',
              }}>{stderrTail}</pre>
            ) : (
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>(no stderr/error captured)</span>
            )}
            {onDiagnose && (
              <div style={{ marginTop: 10, display: 'flex', justifyContent: 'flex-end' }}>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(e) => { e.stopPropagation(); onDiagnose(run); }}
                  title="Spawn an agent chat with this run's logs + job context to diagnose why it failed (reuses session on re-click)"
                >🤖 Diagnose with agent</Button>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function _SdvRunsTab({ jobId }) {
  const [runs, setRuns] = _sdvrUseState(null);
  const [error, setError] = _sdvrUseState(null);
  const [expanded, setExpanded] = _sdvrUseState(null);
  const [filters, setFilters] = _sdvrUseState({ failedOnly: false, last24h: false, manualOnly: false });
  const [diagnosing, setDiagnosing] = _sdvrUseState(false);
  // Tracks run_ids we've already auto-fired from the #diagnose=<run_id> deep
  // link (used by the Telegram failure alert button). Survives StrictMode
  // double-mount and re-renders triggered by reload events.
  const autoFiredRunIdsRef = _sdvrUseRef(new Set());

  const handleDiagnose = _sdvrUseCallback(async (run) => {
    if (typeof window.launchAgentSession !== 'function') {
      alert('Agent launcher not loaded yet — reload the page.');
      return;
    }
    if (diagnosing) return;
    setDiagnosing(true);
    // Pull the job spec so we can resolve cwd + include action context in the prompt.
    let job = {};
    try {
      const r = await fetch(window.apiUrl(`/api/cron/jobs/${encodeURIComponent(jobId)}`));
      if (r.ok) job = await r.json();
    } catch (_) { /* best-effort */ }
    const action = job.action || {};
    const cwd = _sdvrResolveJobCwd(action);
    const jobName = job.name || job.id || jobId;
    const startedIso = run.started_at
      ? new Date(Number(run.started_at) * 1000).toISOString()
      : '(unknown)';
    const dur = (run.duration_ms != null) ? `${run.duration_ms} ms` : '(unknown)';
    const stderrBlock = run.stderr_tail || run.error || '(empty)';
    const stdoutBlock = run.output_preview || run.stdout || '(empty)';

    const prompt =
      `Pomóż mi zdiagnozować ten run schedulera.\n\n` +
      `### Job\n` +
      `- ID: ${job.id || jobId}\n` +
      `- Nazwa: ${jobName}\n` +
      `- Tryb: ${action.mode || '?'}\n` +
      (action.command ? `- Komenda:\n  \`\`\`\n  ${String(action.command).slice(0, 800)}\n  \`\`\`\n` : '') +
      (action.prompt ? `- Prompt (agent-mode):\n  \`\`\`\n  ${String(action.prompt).slice(0, 800)}\n  \`\`\`\n` : '') +
      (action.agent ? `- Agent: ${action.agent}\n` : '') +
      (job.trigger?.spec ? `- Trigger: ${job.trigger.spec}\n` : '') +
      `\n### Run\n` +
      `- Run ID: ${run.run_id || '?'}\n` +
      `- Started: ${startedIso}\n` +
      `- Duration: ${dur}\n` +
      `- Status: ${run.status || '?'}\n` +
      `- Exit code: ${run.exit_code != null ? run.exit_code : '(none)'}\n` +
      `- Trigger: ${run.trigger || 'scheduled'}\n` +
      `\n### Stderr / error (tail)\n\`\`\`\n${stderrBlock}\n\`\`\`\n` +
      `\n### Stdout (preview)\n\`\`\`\n${stdoutBlock}\n\`\`\`\n` +
      `\n### Co od Ciebie chcę\n` +
      `1. Co spowodowało błąd (na podstawie stderr/exit).\n` +
      `2. Czy to jednorazowy incydent czy systemowy problem.\n` +
      `3. Konkretne kroki naprawcze — jeśli trzeba odpalić shell, sprawdzić plik logu, ` +
      `uruchomić komendę ręcznie, zrób to.\n` +
      `4. Czy job należy wyłączyć / przepisać / zmienić harmonogram.\n\n` +
      `Jeśli czegoś brakuje (logi spoza stderr_tail, stan zasobu, środowisko) — wskaż co ` +
      `i jak to zdobyć. Cwd masz ustawiony pod ścieżkę powiązaną z tym jobem (jeśli udało ` +
      `się ją wykryć), więc bash-em można poruszać się po projekcie.`;

    try {
      await window.launchAgentSession({
        title: `Diagnose: ${jobName} @ ${startedIso.slice(0, 19)}`,
        prompt,
        cwd,
        lib_id: `cron-run:${run.run_id || jobId}`,
      });
    } catch (e) {
      alert(`Diagnose failed: ${(e && e.message) || e}`);
    } finally {
      setDiagnosing(false);
    }
  }, [jobId, diagnosing]);

  const load = _sdvrUseCallback(async () => {
    setError(null);
    try {
      const r = await fetch(window.apiUrl(`/api/cron/runs?job_id=${encodeURIComponent(jobId)}&limit=50`));
      if (!r.ok) throw new Error(await _sdvrReadError(r));
      const data = await r.json();
      const list = Array.isArray(data) ? data : (data && Array.isArray(data.runs)) ? data.runs : [];
      setRuns(list);
    } catch (e) {
      setError((e && e.message) || 'failed to load runs');
      setRuns([]);
    }
  }, [jobId]);

  _sdvrUseEffect(() => { load(); }, [load]);
  _sdvrUseEffect(() => {
    const onReload = () => load();
    window.addEventListener('scheduler:reload', onReload);
    return () => window.removeEventListener('scheduler:reload', onReload);
  }, [load]);
  // Deep-link from the Telegram failure alert: `#diagnose=<run_id>` on this
  // page auto-fires the same flow as the manual "🤖 Diagnose with agent"
  // button. Waits until `runs` is populated; clears the hash after firing so
  // navigating back doesn't re-trigger.
  _sdvrUseEffect(() => {
    if (!Array.isArray(runs) || runs.length === 0) return;
    const hash = (window.location.hash || '').replace(/^#/, '');
    const match = /(?:^|&)diagnose=([^&]+)/.exec(hash);
    if (!match) return;
    const targetRunId = decodeURIComponent(match[1]);
    if (autoFiredRunIdsRef.current.has(targetRunId)) return;
    const run = runs.find((r) => r && r.run_id === targetRunId);
    if (!run) return;
    autoFiredRunIdsRef.current.add(targetRunId);
    try {
      const url = new URL(window.location.href);
      url.hash = '';
      window.history.replaceState(null, '', url.toString());
    } catch (_) { /* hash cleanup is best-effort */ }
    handleDiagnose(run);
  }, [runs, handleDiagnose]);

  const filtered = _sdvrUseMemo(() => {
    const arr = Array.isArray(runs) ? runs.slice() : [];
    const cutoff = Date.now() / 1000 - 86400;
    return arr.filter((r) => {
      if (filters.failedOnly && r.status !== 'failed') return false;
      if (filters.last24h && Number(r.started_at) < cutoff) return false;
      if (filters.manualOnly && r.trigger !== 'manual') return false;
      return true;
    });
  }, [runs, filters]);

  const Chip = ({ label, on, onClick }) => (
    <button type="button" onClick={onClick} style={{
      padding: '5px 10px', borderRadius: 'var(--r-pill)',
      background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
      color: on ? 'var(--accent)' : 'var(--fg-2)',
      border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
      fontSize: 'var(--t-xs)', cursor: 'pointer', fontFamily: 'inherit',
    }}>{label}</button>
  );

  return (
    <Card padding={16}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          <Chip label="Failed only" on={filters.failedOnly} onClick={() => setFilters({ ...filters, failedOnly: !filters.failedOnly })} />
          <Chip label="Last 24h" on={filters.last24h} onClick={() => setFilters({ ...filters, last24h: !filters.last24h })} />
          <Chip label="Manual only" on={filters.manualOnly} onClick={() => setFilters({ ...filters, manualOnly: !filters.manualOnly })} />
          <span style={{ flex: 1 }} />
          <window.IconButton icon="refresh" label="Reload" size={30} onClick={load} />
        </div>
        {error && <StatusBanner variant="err" label={error} inline />}
        {runs === null ? (
          <Spinner label="Loading runs…" />
        ) : filtered.length === 0 ? (
          <EmptyState message="No runs match the filters" />
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--hairline-strong)' }}>
                  <th style={{ padding: '6px', width: 24 }}></th>
                  <th className="mono" style={{ textAlign: 'left', padding: '6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Started</th>
                  <th className="mono" style={{ textAlign: 'left', padding: '6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Duration</th>
                  <th className="mono" style={{ textAlign: 'left', padding: '6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Trigger</th>
                  <th className="mono" style={{ textAlign: 'left', padding: '6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Session</th>
                  <th className="mono" style={{ textAlign: 'right', padding: '6px', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Exit</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <_SdvRunRow key={r.run_id || `${r.started_at}-${r.status}`}
                    run={r}
                    expanded={expanded === (r.run_id || `${r.started_at}-${r.status}`)}
                    onToggle={() => {
                      const key = r.run_id || `${r.started_at}-${r.status}`;
                      setExpanded((cur) => cur === key ? null : key);
                    }}
                    onDiagnose={handleDiagnose}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

Object.assign(window, {
  SdvRunsTab: _SdvRunsTab,
  SdvRunStatusIcon: _SdvRunStatusIcon,
  SdvRunRow: _SdvRunRow,
});
