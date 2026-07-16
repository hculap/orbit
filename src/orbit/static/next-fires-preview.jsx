// next-fires-preview.jsx — debounced preview of the next N fires for a trigger.
//
// Reusable across SchedulerCreateModal (live preview while wizard fields
// change) and SchedulerDetailView (Trigger tab). POSTs the trigger spec to
// /api/cron/preview and renders "Next fire: <relative>" + a list of the
// following 4. Validation errors from the backend (e.g. bad cron spec) are
// surfaced inline so the user can correct without leaving the form.

const { useState: _nfpUseState, useEffect: _nfpUseEffect, useRef: _nfpUseRef } = React;

const _NFP_DEBOUNCE_MS = 300;

function _nfpRelative(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  const diffMs = t - Date.now();
  if (diffMs <= 0) return 'now';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `in ${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `in ${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 48) {
    const remMin = min - hr * 60;
    return remMin > 0 ? `in ${hr}h ${remMin}m` : `in ${hr}h`;
  }
  const days = Math.floor(hr / 24);
  return `in ${days}d`;
}

function _nfpAbsolute(iso, tz) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return iso;
    const opts = {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
      hour12: false,
    };
    if (tz) opts.timeZone = tz;
    return d.toLocaleString('sv-SE', opts).replace('T', ' ');
  } catch (_) { return iso; }
}

function _nfpTriggerSignature(trigger, endCondition) {
  if (!trigger) return '';
  const t = JSON.stringify(trigger);
  const e = endCondition ? JSON.stringify(endCondition) : '';
  return t + '|' + e;
}

function NextFiresPreview({ trigger, endCondition, count = 5 }) {
  const [data, setData] = _nfpUseState(null);
  const [error, setError] = _nfpUseState(null);
  const [busy, setBusy] = _nfpUseState(false);
  const ctrlRef = _nfpUseRef(null);
  const sig = _nfpTriggerSignature(trigger, endCondition);

  _nfpUseEffect(() => {
    if (!trigger || !trigger.type || !trigger.spec) {
      setData(null); setError(null); setBusy(false);
      return undefined;
    }
    if (ctrlRef.current) { try { ctrlRef.current.abort(); } catch (_) { /* ignore */ } }
    const handle = setTimeout(async () => {
      const ctrl = new AbortController();
      ctrlRef.current = ctrl;
      setBusy(true);
      setError(null);
      try {
        const r = await fetch(window.apiUrl('/api/cron/preview'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ trigger, end_condition: endCondition || null, count }),
          signal: ctrl.signal,
        });
        if (!r.ok) {
          let detail = `http ${r.status}`;
          try {
            const j = await r.json();
            if (j && (j.error || j.detail)) detail = String(j.error || j.detail).slice(0, 240);
          } catch (_) { /* ignore */ }
          throw new Error(detail);
        }
        const json = await r.json();
        const list = Array.isArray(json && json.next_fires) ? json.next_fires : [];
        setData(list);
      } catch (e) {
        if (e && e.name === 'AbortError') return;
        setError(e && e.message ? e.message : 'preview failed');
        setData([]);
      } finally {
        setBusy(false);
      }
    }, _NFP_DEBOUNCE_MS);
    return () => {
      clearTimeout(handle);
      if (ctrlRef.current) { try { ctrlRef.current.abort(); } catch (_) { /* ignore */ } }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, count]);

  if (!trigger || !trigger.type || !trigger.spec) {
    return (
      <div className="mono" style={{
        padding: '10px 12px', fontSize: 'var(--t-xs)', color: 'var(--fg-4)',
        background: 'var(--surface-2)', border: '1px dashed var(--hairline)',
        borderRadius: 'var(--r-control)',
      }}>
        Fill in the trigger to preview the next fires.
      </div>
    );
  }
  if (error) {
    return (
      <div className="mono" style={{
        padding: '10px 12px', fontSize: 'var(--t-xs)', color: 'var(--err)',
        background: 'var(--err-bg)', border: '1px solid var(--err)',
        borderRadius: 'var(--r-control)',
      }}>
        {error}
      </div>
    );
  }
  const tz = trigger && trigger.tz;
  const list = Array.isArray(data) ? data : [];
  const head = list[0];
  const rest = list.slice(1);
  return (
    <div style={{
      padding: '10px 12px', borderRadius: 'var(--r-control)',
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      display: 'flex', flexDirection: 'column', gap: 6,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Icon name="bell" size={13} color="var(--accent)" />
        <span style={{ fontSize: 'var(--t-cap)', fontWeight: 500, color: 'var(--fg)' }}>
          {busy && !head ? 'Computing…' : (head ? `Next fire: ${_nfpRelative(head)}` : 'No upcoming fires')}
        </span>
        {head && (
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>
            ({_nfpAbsolute(head, tz)}{tz ? ` · ${tz}` : ''})
          </span>
        )}
      </div>
      {rest.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 2 }}>
          {rest.map((iso, i) => (
            <div key={i} className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', display: 'flex', gap: 8 }}>
              <span style={{ color: 'var(--fg-4)', minWidth: 18 }}>#{i + 2}</span>
              <span>{_nfpRelative(iso)}</span>
              <span style={{ color: 'var(--fg-4)' }}>·</span>
              <span style={{ color: 'var(--fg-4)' }}>{_nfpAbsolute(iso, tz)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { NextFiresPreview });
