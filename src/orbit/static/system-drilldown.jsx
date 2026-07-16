// system-drilldown.jsx — clickable System cards → per-process drill-down (#104)
//
// SystemView's CPU/RAM/Swap/Disk MetricCards forward an onClick that opens this
// modal. It fetches GET /api/system/processes?metric=cpu|mem|disk and renders
// the attributed groups (agent / project / area / app / system / other) as
// ranked bars; expanding a group reveals its top member processes.
//
// cpu/mem auto-refresh on the same 5 s cadence as the live header (paused while
// the tab is hidden); disk is fetched ONCE on open (the backend walk is
// expensive + 60 s cached server-side), with a manual refresh button.
//
// Reads window.Modal / ProgressBar / IconButton / Icon / apiUrl from
// components.jsx and the bytesShort helper + token styles from sections.jsx —
// both load before this file (see index.html load order).

const { useState, useEffect, useRef, useCallback } = React;

// kind → { short Polish label, accent color } for the row badge. Mirrors the
// scope-chip pill style used in SystemView's service rows.
const _DRILL_KIND = {
  agent:   { label: 'agent',   color: 'var(--accent)' },
  project: { label: 'projekt', color: 'var(--info)' },
  area:    { label: 'area',    color: 'var(--info)' },
  app:     { label: 'app',     color: 'var(--warn)' },
  system:  { label: 'system',  color: 'var(--fg-3)' },
  other:   { label: 'inne',    color: 'var(--fg-4)' },
};

const _DRILL_TITLE = { cpu: 'CPU', mem: 'RAM', disk: 'Dysk' };

function _drillGroupValue(metric, value) {
  if (metric === 'cpu') return (value || 0).toFixed(1) + '%';
  return bytesShort(value || 0); // mem + disk are byte values
}

// Headline under the title summarizing the card total.
function _drillTotalLine(metric, total) {
  if (!total) return '';
  if (metric === 'cpu') {
    return `${(total.value || 0).toFixed(1)}% · ${total.cores || '?'} rdzeni`;
  }
  return `${bytesShort(total.used_bytes || 0)} / ${bytesShort(total.total_bytes || 0)} zajęte`;
}

function _DrillProcRow({ metric, proc }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'baseline', gap: 8,
      padding: '4px 14px 4px 28px', fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
    }} className="mono">
      <span style={{ color: 'var(--fg-4)', flexShrink: 0, width: 56 }}>{proc.pid}</span>
      <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {proc.cmd_short || proc.comm}
      </span>
      <span style={{ flexShrink: 0, color: 'var(--fg-2)' }}>{_drillGroupValue(metric, proc.value)}</span>
    </div>
  );
}

function _DrillGroupRow({ metric, group, maxVal, expanded, onToggle }) {
  const kind = _DRILL_KIND[group.kind] || _DRILL_KIND.system;
  const isOther = group.kind === 'other';
  const hasProcs = (group.procs || []).length > 0;
  return (
    <div style={{ borderTop: '1px solid var(--hairline)', opacity: isOther ? 0.6 : 1 }}>
      <div
        onClick={hasProcs ? onToggle : undefined}
        style={{ padding: '10px 14px', cursor: hasProcs ? 'pointer' : 'default' }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          {hasProcs
            ? <Icon name={expanded ? 'chevron-d' : 'chevron-r'} size={13} color="var(--fg-4)" />
            : <span style={{ width: 13, flexShrink: 0 }} />}
          <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {group.label}
          </span>
          <span className="mono" style={{
            fontSize: 'var(--t-xs)', color: kind.color, padding: '2px 6px',
            background: 'var(--surface-3)', borderRadius: 4, flexShrink: 0,
          }}>{kind.label}</span>
          <span style={{ flex: 1 }} />
          <span className="mono" style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)', flexShrink: 0 }}>
            {_drillGroupValue(metric, group.value)}
          </span>
        </div>
        <ProgressBar value={group.value || 0} max={maxVal} color={kind.color} height={5} />
      </div>
      {expanded && hasProcs && (
        <div style={{ paddingBottom: 6 }}>
          {group.procs.map((p) => <_DrillProcRow key={p.pid} metric={metric} proc={p} />)}
        </div>
      )}
    </div>
  );
}

function SystemDrilldownModal({ metric, open, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());
  const liveMetric = metric === 'disk' ? null : metric; // disk = one-shot

  const load = useCallback(async () => {
    if (!metric) return;
    try {
      setLoading(true);
      setError(null);
      const res = await fetch(apiUrl(`/api/system/processes?metric=${encodeURIComponent(metric)}&top=12`),
        { headers: { Accept: 'application/json' } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      setData(await res.json());
    } catch (e) {
      setError(String(e && e.message ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [metric]);

  // Fetch on open (+ whenever the metric changes). Reset transient state so
  // switching CPU→RAM doesn't flash the previous metric's bars/expansion.
  useEffect(() => {
    if (!open || !metric) return;
    setData(null);
    setExpanded(new Set());
    load();
  }, [open, metric, load]);

  // Auto-refresh cpu/mem on the 5 s live cadence; pause while hidden. Disk is
  // intentionally NOT polled (server walk is costly + 60 s cached).
  useEffect(() => {
    if (!open || !liveMetric) return;
    let timer = null;
    const tick = () => { if (document.visibilityState === 'visible') load(); };
    timer = setInterval(tick, 5000);
    return () => { if (timer) clearInterval(timer); };
  }, [open, liveMetric, load]);

  if (!open) return null;

  const total = data && data.total;
  const groups = (data && data.groups) || [];
  const maxVal = metric === 'cpu'
    ? ((total && total.value) || 1)
    : ((total && total.used_bytes) || 1);

  const toggle = (key) => setExpanded((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <Modal open={open} onClose={onClose} title={`${_DRILL_TITLE[metric] || metric} · rozbicie zużycia`} width={560}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        gap: 12, padding: '12px 14px', borderBottom: '1px solid var(--hairline)',
      }}>
        <div>
          <div className="mono" style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-1)' }}>
            {_drillTotalLine(metric, total)}
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)', marginTop: 2 }}>
            {liveMetric ? 'live · co 5s' : 'snapshot katalogów'}
          </div>
        </div>
        <IconButton icon="refresh" label="Odśwież" onClick={load} />
      </div>

      <div>
        {error && (
          <div style={{ padding: 18, color: 'var(--err)', fontSize: 'var(--t-sm)' }}>
            Nie udało się pobrać danych: {error}
          </div>
        )}
        {!error && !data && loading && (
          <div style={{ padding: 18, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>ładowanie…</div>
        )}
        {!error && data && groups.length === 0 && (
          <div style={{ padding: 18, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>brak danych do pokazania</div>
        )}
        {groups.map((g) => {
          const key = `${g.kind}:${g.key}`;
          return (
            <_DrillGroupRow
              key={key}
              metric={metric}
              group={g}
              maxVal={maxVal}
              expanded={expanded.has(key)}
              onToggle={() => toggle(key)}
            />
          );
        })}
      </div>
    </Modal>
  );
}

Object.assign(window, { SystemDrilldownModal });
