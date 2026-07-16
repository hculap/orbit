// logs-view.jsx — journalctl + nginx tail viewer, wired to /api/logs.
// Extracted from sections.jsx to keep that file under the 800-line ceiling.

const { useState: useLogState, useEffect: useLogEffect, useRef: useLogRef,
        useMemo: useLogMemo, useCallback: useLogCallback } = React;

function parseLogLine(raw, srcLabel) {
  const text = String(raw || '');
  let t = '';
  const isoMatch = text.match(/^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)/);
  const bracketMatch = !isoMatch && text.match(/^\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]/);
  const journalMatch = !isoMatch && !bracketMatch && text.match(/^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})/);
  if (isoMatch) {
    const dt = isoMatch[1].replace('T', ' ');
    const m = dt.match(/(\d{2}:\d{2}:\d{2})/);
    t = m ? m[1] : dt;
  } else if (bracketMatch) {
    t = bracketMatch[1];
  } else if (journalMatch) {
    t = journalMatch[1];
  }

  let lvl = 'INFO';
  if (/\bERROR\b|\[error\]|level=error|status=5\d\d|"\s*5\d\d\s|\sERR\s/i.test(text)) lvl = 'ERROR';
  else if (/\bWARN(?:ING)?\b|level=warn|status=4\d\d|"\s*4\d\d\s/i.test(text)) lvl = 'WARN';
  else if (/\bDEBUG\b|level=debug/i.test(text)) lvl = 'DEBUG';
  else if (/\sstatus=3\d\d|"\s*3\d\d\s/.test(text)) lvl = 'WARN';

  return { t, lvl, src: srcLabel, msg: text, raw: text };
}

function logHighlight(text, q) {
  if (!q) return text;
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return text;
  return (
    <>
      {text.slice(0, i)}
      <mark style={{ background: 'var(--accent-soft)', color: 'var(--accent)', padding: '0 2px', borderRadius: 2 }}>
        {text.slice(i, i + q.length)}
      </mark>
      {text.slice(i + q.length)}
    </>
  );
}

function LogsView({ compact, onOpenLog }) {
  const [sources, setSources] = useLogState([]);
  const [src, setSrc] = useLogState('');
  const [filter, setFilter] = useLogState('');
  const [lines, setLines] = useLogState(200);
  const [autoRefresh, setAutoRefresh] = useLogState(true);
  const [rawLines, setRawLines] = useLogState([]);
  const [loading, setLoading] = useLogState(false);
  const [error, setError] = useLogState('');
  const termRef = useLogRef(null);

  useLogEffect(() => {
    let cancelled = false;
    fetch(apiUrl('/api/logs/sources'))
      .then(r => r.json())
      .then(d => {
        if (cancelled) return;
        const list = Array.isArray(d) ? d : [];
        setSources(list);
        if (list.length && !src) setSrc(list[0].id);
      })
      .catch(() => setError('failed to load sources'));
    return () => { cancelled = true; };
  }, []);

  const fetchLogs = useLogCallback(async () => {
    if (!src) return;
    setLoading(true);
    try {
      const r = await fetch(apiUrl(`/api/logs/${encodeURIComponent(src)}?lines=${lines}`));
      const d = await r.json();
      if (!d.ok) {
        setError(d.error || 'log fetch failed');
        setRawLines([]);
      } else {
        setError('');
        setRawLines(d.lines || []);
      }
    } catch (e) {
      setError('network error');
      setRawLines([]);
    } finally {
      setLoading(false);
    }
  }, [src, lines]);

  useLogEffect(() => { fetchLogs(); }, [fetchLogs]);

  useLogEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(fetchLogs, 5000);
    return () => clearInterval(id);
  }, [autoRefresh, fetchLogs]);

  const srcLabel = useLogMemo(() => {
    const s = sources.find(s => s.id === src);
    return s ? s.label : src;
  }, [sources, src]);

  const parsed = useLogMemo(() =>
    rawLines.map(raw => parseLogLine(raw, srcLabel)),
    [rawLines, srcLabel]
  );

  const items = useLogMemo(() =>
    parsed.filter(l => !filter || l.msg.toLowerCase().includes(filter.toLowerCase())),
    [parsed, filter]
  );

  useLogEffect(() => {
    if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight;
  }, [items.length]);

  const lvlColor = (l) =>
    l === 'ERROR' ? 'var(--err)' :
    l === 'WARN'  ? 'var(--warn)' :
    l === 'DEBUG' ? 'var(--fg-4)' :
    'var(--fg-3)';

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <SectionHeader
        eyebrow="journalctl · nginx"
        title="Logs"
        actions={
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 'var(--t-cap)' }} className="mono">
            {autoRefresh && <span className="live-dot"/>}
            <span style={{ color: 'var(--fg-3)' }}>{autoRefresh ? 'tailing' : 'paused'}</span>
          </div>
        }
      />

      <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <Select
          value={src}
          onChange={setSrc}
          options={sources.map(s => ({ value: s.id, label: s.label }))}
          placeholder={sources.length === 0 ? '(loading…)' : undefined}
          mono
          size="sm"
          style={{ maxWidth: compact ? '100%' : 260 }}
        />
        <Select
          value={String(lines)}
          onChange={v => setLines(Number(v))}
          options={[50, 200, 500, 1000].map(n => ({ value: String(n), label: `${n} lines` }))}
          mono
          size="sm"
        />
        <SearchInput value={filter} onChange={setFilter} placeholder="filter…" style={{ flex: 1, minWidth: 140 }}/>
        <button onClick={() => setAutoRefresh(!autoRefresh)} style={{
          height: 36, padding: '0 12px', borderRadius: 'var(--r-control)',
          background: autoRefresh ? 'var(--accent-soft)' : 'var(--surface-1)',
          color: autoRefresh ? 'var(--accent)' : 'var(--fg-2)',
          border: '1px solid ' + (autoRefresh ? 'var(--accent-line)' : 'var(--hairline)'),
          fontSize: 'var(--t-cap)', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6,
        }}>
          <Icon name="refresh" size={13}/> auto
        </button>
        <IconButton icon="refresh" label="Refresh" onClick={fetchLogs} />
      </div>

      <div ref={termRef} className="mono scroll-hide" style={{
        marginTop: 14,
        background: '#0a0b0d', border: '1px solid var(--hairline)',
        borderRadius: 'var(--r-widget)', padding: 14,
        fontSize: 'var(--t-cap)', lineHeight: 1.7, color: 'var(--fg-2)',
        height: compact ? 380 : 520, overflowY: 'auto',
      }}>
        {error && <div style={{ color: 'var(--err)' }}>[{error}]</div>}
        {!error && items.length === 0 && !loading && <div style={{ color: 'var(--fg-4)' }}>(no lines)</div>}
        {!error && loading && rawLines.length === 0 && <div style={{ color: 'var(--fg-4)' }}>loading…</div>}
        {items.map((l, i) => (
          <div key={i} onClick={() => onOpenLog && onOpenLog(l)}
               style={{ display: 'flex', gap: 12, padding: '1px 0', cursor: 'pointer' }}
               onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.03)'}
               onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
            <span style={{ color: 'var(--fg-4)', flexShrink: 0, width: 70 }}>{l.t}</span>
            <span style={{ color: lvlColor(l.lvl), width: 48, flexShrink: 0, fontWeight: 600 }}>{l.lvl}</span>
            <span style={{ flex: 1, wordBreak: 'break-word' }}>
              {filter ? logHighlight(l.msg, filter) : l.msg}
            </span>
          </div>
        ))}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6, color: 'var(--fg-4)' }}>
          <span>$</span><span className="caret"/>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { LogsView, parseLogLine });
