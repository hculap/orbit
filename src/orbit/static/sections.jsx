// sections.jsx — All non-orchestrator section views, wired to live backend.
// Each takes { compact } where compact=true is the mobile rendering.

const { useState, useEffect, useRef, useMemo, useCallback } = React;

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────
function bytesShort(b) {
  if (b == null) return '—';
  if (b > 1e12) return (b / 1e12).toFixed(2) + ' TB';
  if (b > 1e9)  return (b / 1e9).toFixed(2)  + ' GB';
  if (b > 1e6)  return (b / 1e6).toFixed(0)  + ' MB';
  if (b > 1e3)  return (b / 1e3).toFixed(0)  + ' KB';
  return b + ' B';
}

function SubHeader({ title, sub, action }) {
  return (
    <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--hairline)', display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
      <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{title}</span>
      {sub && <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{sub}</span>}
      {action}
    </div>
  );
}

// `onClick` (optional) makes the card a clickable drill-down trigger: it
// forwards to Card (which already renders cursor:pointer + a glow on hover when
// onClick is set) and surfaces a small expand chevron. Cards without onClick
// render exactly as before.
function MetricCard({ label, mono, children, onClick }) {
  return (
    <Card padding={14} hover={!!onClick} onClick={onClick}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>{label}</span>
        {onClick && <Icon name="maximize" size={12} color="var(--fg-4)" />}
      </div>
      <div className="mono" style={{ fontSize: 18, fontWeight: 500, marginBottom: 8, letterSpacing: '-0.01em' }}>{mono}</div>
      {children}
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// SHARE — file browser, the daily-driver section
// ─────────────────────────────────────────────────────────────
// Navigation (current folder + open file) is driven entirely by the URL via
// the `sharePath`/`shareFile` props and the `onNavigate`/`onOpenFilePath`
// callbacks (app.jsx useHubRoute). This is what makes browser Back step back
// one folder/file instead of escaping the section — the folder is no longer
// held in local state.
function ShareView({ compact, onPreview, sharePath, shareFile, onNavigate, onOpenFilePath }) {
  const currentPath = sharePath || '';
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(new Set());
  const [filter, setFilter] = useState('');
  const [sort, setSort] = useState('kind');
  const [uploading, setUploading] = useState(null);
  const fileInputRef = useRef(null);
  const toast = useToast();

  const loadDir = useCallback(async (path) => {
    setLoading(true);
    setSelected(new Set());
    try {
      const url = path ? apiUrl('/api/share/' + encodePath(path)) : apiUrl('/api/share');
      const r = await fetch(url);
      const d = await r.json();
      setItems(d.items || []);
    } catch (e) {
      console.error('share load failed', e);
      setItems([]);
      toast && toast('Failed to load files', 'err');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadDir(currentPath); }, [currentPath, loadDir]);

  // FilePreview / detail views fire `share:reload` after rename/delete so the
  // grid can refetch without prop drilling a callback through every modal.
  useEffect(() => {
    const onReload = () => loadDir(currentPath);
    window.addEventListener('share:reload', onReload);
    return () => window.removeEventListener('share:reload', onReload);
  }, [currentPath, loadDir]);

  const breadcrumbs = useMemo(() => ['Sync', ...currentPath.split('/').filter(Boolean)], [currentPath]);

  const goUp = () => {
    const parts = currentPath.split('/').filter(Boolean);
    parts.pop();
    onNavigate && onNavigate(parts.join('/'));
  };

  const goCrumb = (i) => {
    if (i === 0) { onNavigate && onNavigate(''); return; }
    const parts = currentPath.split('/').filter(Boolean).slice(0, i);
    onNavigate && onNavigate(parts.join('/'));
  };

  const sorted = useMemo(() => {
    const arr = items.slice();
    const cmpName = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    switch (sort) {
      case 'name':  arr.sort(cmpName); break;
      case 'mtime': arr.sort((a, b) => (b.mtime || 0) - (a.mtime || 0)); break;
      case 'size':  arr.sort((a, b) => (b.size || 0) - (a.size || 0)); break;
      default:      arr.sort((a, b) => ((a.type === 'dir' ? 0 : 1) - (b.type === 'dir' ? 0 : 1)) || cmpName(a, b));
    }
    return arr;
  }, [items, sort]);

  const visible = useMemo(() =>
    sorted.filter(f => !filter || f.name.toLowerCase().includes(filter.toLowerCase())),
    [sorted, filter]
  );

  const toggle = (name) => {
    setSelected(s => {
      const n = new Set(s);
      if (n.has(name)) n.delete(name); else n.add(name);
      return n;
    });
  };

  const onFilesPicked = (fileList) => {
    if (!fileList || !fileList.length) return;
    const fd = new FormData();
    fd.append('rel_dir', currentPath);
    for (const f of fileList) fd.append('files', f);
    const xhr = new XMLHttpRequest();
    setUploading({ name: fileList.length === 1 ? fileList[0].name : `${fileList.length} files`, pct: 0 });
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) {
        setUploading(u => u && ({ ...u, pct: (e.loaded / e.total) * 100 }));
      }
    });
    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        setUploading({ name: 'done', pct: 100 });
        setTimeout(() => { setUploading(null); toast && toast('Upload complete', 'ok'); loadDir(currentPath); }, 350);
      } else {
        setUploading(null);
        toast && toast(`Upload failed (${xhr.status})`, 'err');
      }
    });
    xhr.addEventListener('error', () => {
      setUploading(null);
      toast && toast('Upload network error', 'err');
    });
    xhr.open('POST', apiUrl('/share/upload'));
    xhr.send(fd);
  };

  const startUpload = () => fileInputRef.current && fileInputRef.current.click();

  const newFolder = async () => {
    const name = prompt('New folder name:');
    if (!name) return;
    try {
      const r = await fetch(apiUrl('/api/share/mkdir'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rel_dir: currentPath, name }),
      });
      const d = await r.json();
      if (!d.ok) { toast && toast('Mkdir failed: ' + (d.error || 'unknown'), 'err'); return; }
      loadDir(currentPath);
    } catch (e) { toast && toast('Mkdir failed', 'err'); }
  };

  const bulkDownload = async () => {
    if (!selected.size) return;
    const paths = Array.from(selected).map(n => currentPath ? currentPath + '/' + n : n);
    try {
      const r = await fetch(apiUrl('/share/download-zip'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paths }),
      });
      if (!r.ok) { toast && toast('Download failed', 'err'); return; }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `share-${new Date().toISOString().slice(0, 10)}.zip`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { toast && toast('Download error', 'err'); }
  };

  const bulkDelete = async () => {
    if (!selected.size) return;
    if (!confirm(`Delete ${selected.size} item(s)? (recovery via Syncthing .stversions/)`)) return;
    const errs = [];
    for (const name of selected) {
      const rel = currentPath ? currentPath + '/' + name : name;
      try {
        const r = await fetch(apiUrl('/api/share/' + encodePath(rel)), { method: 'DELETE' });
        const d = await r.json();
        if (!d.ok) errs.push(name);
      } catch (e) { errs.push(name); }
    }
    if (errs.length) toast && toast(`${errs.length} failed`, 'err');
    else toast && toast('Deleted', 'ok');
    setSelected(new Set());
    loadDir(currentPath);
  };

  const onItemClick = (it) => {
    const fullPath = currentPath ? currentPath + '/' + it.name : it.name;
    if (it.type === 'dir') {
      // Navigate in place via the URL (PUSH) — Back returns to this folder.
      onNavigate && onNavigate(fullPath);
    } else if (onOpenFilePath) {
      // Open the file via the URL so Back returns to the listing / previous
      // file. Pass the full item so FilePreview gets kind/ext without a fetch.
      onOpenFilePath(currentPath, fullPath, { ...it, path: fullPath });
    } else {
      onPreview && onPreview({ ...it, path: fullPath });
    }
  };

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }}
             onChange={e => onFilesPicked(e.target.files)} />
      <SectionHeader
        eyebrow="~/Sync"
        title="Sync"
        count={`${items.length} items`}
        actions={!compact && (
          <>
            <Button icon="upload" onClick={startUpload}>Upload</Button>
            <Button icon="folder" onClick={newFolder}>New folder</Button>
          </>
        )}
      />

      {/* breadcrumbs */}
      <div className="mono" style={{ marginTop: 14, fontSize: 'var(--t-cap)', color: 'var(--fg-3)', display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {breadcrumbs.map((b, i) => (
          <React.Fragment key={i}>
            <span
              onClick={() => goCrumb(i)}
              style={{
                color: i === breadcrumbs.length - 1 ? 'var(--fg)' : 'var(--fg-3)',
                cursor: i === breadcrumbs.length - 1 ? 'default' : 'pointer',
              }}
            >{b}</span>
            {i < breadcrumbs.length - 1 && <Icon name="chevron-r" size={12} color="var(--fg-4)"/>}
          </React.Fragment>
        ))}
      </div>

      {/* filter + actions row */}
      <div style={{ display: 'flex', gap: 8, marginTop: 16, alignItems: 'center', flexWrap: 'wrap' }}>
        {currentPath && <IconButton icon="chevron-l" label="Up" onClick={goUp} />}
        <SearchInput value={filter} onChange={setFilter} placeholder="filter files…" style={{ flex: 1, minWidth: 200 }} />
        <Select
          value={sort}
          onChange={setSort}
          mono
          size="sm"
          options={[
            { value: 'kind',  label: 'sort: kind' },
            { value: 'name',  label: 'sort: name' },
            { value: 'mtime', label: 'sort: newest' },
            { value: 'size',  label: 'sort: size' },
          ]}
        />
        {compact && <IconButton icon="upload" label="Upload" onClick={startUpload} />}
        {compact && <IconButton icon="folder" label="New folder" onClick={newFolder} />}
        <IconButton icon="refresh" label="Refresh" onClick={() => loadDir(currentPath)} />
      </div>

      {/* upload progress */}
      {uploading && (
        <div className="fade-in" style={{ marginTop: 14, padding: 12, background: 'var(--surface-1)', borderRadius: 'var(--r-md)', border: '1px solid var(--accent-line)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, fontSize: 'var(--t-sm)' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Icon name="upload" size={14} color="var(--accent)"/>
              <span className="mono" style={{ color: 'var(--fg-2)' }}>{uploading.name}</span>
            </span>
            <span className="mono" style={{ color: 'var(--accent)', fontSize: 'var(--t-cap)' }}>{Math.round(uploading.pct)}%</span>
          </div>
          <ProgressBar value={uploading.pct}/>
        </div>
      )}

      {/* file grid */}
      <div style={{
        marginTop: 18,
        display: 'grid',
        gridTemplateColumns: compact ? '1fr 1fr' : 'repeat(auto-fill, minmax(180px, 1fr))',
        gap: 10,
      }}>
        {loading && items.length === 0 && (
          Array.from({ length: 6 }).map((_, i) => (
            <div key={i} style={{
              background: 'var(--surface-1)', border: '1px solid var(--hairline)',
              borderRadius: 'var(--r-widget)', padding: 10, height: 200, opacity: 0.4,
            }}/>
          ))
        )}
        {visible.map(f => (
          <FileCard
            key={f.name}
            f={f}
            currentPath={currentPath}
            compact={compact}
            selected={selected.has(f.name)}
            onSelect={() => toggle(f.name)}
            onClick={() => onItemClick(f)}
          />
        ))}
        {!loading && visible.length === 0 && (
          <EmptyState
            message={filter ? 'no matches' : 'empty'}
            centered
            padded
            style={{ gridColumn: '1 / -1' }}
          />
        )}
      </div>

      {/* bulk action bar */}
      {selected.size > 0 && (
        <div className="fade-in" style={{
          position: compact ? 'fixed' : 'sticky',
          left: compact ? 12 : 'auto', right: compact ? 12 : 'auto',
          bottom: compact ? 80 : 16,
          marginTop: compact ? 0 : 18,
          padding: '10px 12px 10px 16px',
          background: 'var(--surface-2)', border: '1px solid var(--hairline-strong)',
          borderRadius: 'var(--r-widget)', display: 'flex', alignItems: 'center', gap: 10,
          boxShadow: 'var(--shadow-2)', zIndex: 5,
        }}>
          <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{selected.size} selected</span>
          <span style={{ flex: 1 }}/>
          <IconButton icon="download" label="Download" onClick={bulkDownload} />
          <IconButton icon="trash" label="Delete" onClick={bulkDelete} />
          <Button size="sm" variant="quiet" onClick={() => setSelected(new Set())}>clear</Button>
        </div>
      )}
    </div>
  );
}

function FileCard({ f, currentPath, compact, selected, onSelect, onClick }) {
  const [hover, setHover] = useState(false);
  const isFolder = f.type === 'dir';
  const isImg = !isFolder && f.kind === 'image';
  const fullPath = currentPath ? currentPath + '/' + f.name : f.name;
  const sizeStr = isFolder
    ? (f.size != null ? bytesShort(f.size) : 'folder')
    : bytesShort(f.size);
  const mtimeStr = relTime(f.mtime);

  return (
    <div
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      onClick={onClick}
      style={{
        background: 'var(--surface-1)',
        border: '1px solid ' + (selected ? 'var(--accent-line)' : 'var(--hairline)'),
        borderRadius: 'var(--r-widget)', padding: 10,
        cursor: 'pointer',
        position: 'relative',
        transition: 'border-color .15s, background .15s',
        ...(selected ? { background: 'var(--accent-soft)' } : {}),
      }}
    >
      <div style={{
        width: '100%', aspectRatio: '4/3', borderRadius: 'var(--r-control)',
        background: 'var(--surface-2)',
        marginBottom: 10, position: 'relative', overflow: 'hidden',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        {isImg ? (
          <img
            src={apiUrl(`/share/thumb/${encodePath(fullPath)}?size=180`)}
            loading="lazy"
            alt=""
            style={{ width: '100%', height: '100%', objectFit: 'cover' }}
            onError={e => { e.currentTarget.style.display = 'none'; }}
          />
        ) : (
          <FileIconBox name={f.name} isFolder={isFolder} size={44}/>
        )}
        <button
          onClick={e => { e.stopPropagation(); onSelect(); }}
          style={{
            position: 'absolute', top: 6, left: 6,
            width: 22, height: 22, borderRadius: 'var(--r-sm)',
            background: selected ? 'var(--accent)' : 'rgba(0,0,0,0.5)',
            border: '1px solid ' + (selected ? 'var(--accent)' : 'rgba(255,255,255,0.2)'),
            display: hover || selected ? 'flex' : 'none',
            alignItems: 'center', justifyContent: 'center',
            color: selected ? 'var(--accent-fg)' : 'transparent',
            cursor: 'pointer', padding: 0,
          }}
        >
          {selected && <Icon name="check" size={14} stroke={2.5}/>}
        </button>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
        {isFolder && <Icon name="folder" size={13} color="var(--fg-3)"/>}
        <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{f.name}</div>
      </div>
      <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', display: 'flex', justifyContent: 'space-between' }}>
        <span>{sizeStr}</span>
        <span>{mtimeStr}</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// SYSTEM
// ─────────────────────────────────────────────────────────────

// Tiny self-ticking child for "updated Ns ago" — its 1 s interval
// re-renders only this <span>, not the whole SystemView (services list,
// peers, sparklines, metric cards) which used to re-render every second
// because the parent owned the tick.
function RelativeTime({ ts }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  if (!ts) return <span>now</span>;
  const delta = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (delta < 5) return <span>now</span>;
  if (delta < 60) return <span>{delta}s ago</span>;
  if (delta < 3600) return <span>{Math.floor(delta / 60)}m ago</span>;
  return <span>{Math.floor(delta / 3600)}h ago</span>;
}

// Format a duration in seconds as a compact "1h 23m" / "5m 12s" / "8s".
function _fmtDur(secs) {
  const s = Math.max(0, Math.round(Number(secs) || 0));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
}

// Countdown formatter — ALWAYS down to seconds so a live ticker visibly
// moves every second ("3h 59m 12s"), unlike _fmtDur which drops seconds
// past the hour mark.
function _fmtCountdown(secs) {
  let s = Math.max(0, Math.floor(Number(secs) || 0));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// Live eviction countdown. Captures an absolute deadline from the server's
// remaining-seconds value and ticks it down every second locally, so it
// counts smoothly between the System view's 5 s polls. Re-syncs the
// deadline whenever the server value changes (each poll) so client clock
// drift never accumulates.
function _TmuxCountdown({ seconds }) {
  const init = Math.max(0, Number(seconds) || 0);
  const deadlineRef = useRef(Date.now() + init * 1000);
  const [remaining, setRemaining] = useState(init);
  useEffect(() => {
    deadlineRef.current = Date.now() + Math.max(0, Number(seconds) || 0) * 1000;
    setRemaining(Math.max(0, Number(seconds) || 0));
  }, [seconds]);
  useEffect(() => {
    const id = setInterval(() => {
      setRemaining(Math.max(0, (deadlineRef.current - Date.now()) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--warn)', flexShrink: 0 }}>zamknie za {_fmtCountdown(remaining)}</span>;
}

// ─────────────────────────────────────────────────────────────
// Tmux session row actions (System view "Sesje interaktywne") —
// wired to the existing orchestrator session endpoints, so this is
// pure frontend. "Keep-alive" = PATCH {persistent} (exempts the slot
// from the idle reaper); "Kill" = POST /close (frees the pool slot
// but KEEPS the transcript, so the session reopens on demand — that's
// why the metaphor is "stop", not "delete"). Both refresh the live
// snapshot so the row reflects the new state without waiting for the
// 5 s poll. Helpers are module-level (mirrors _libraryArchive) and
// never mutate the slot object.
// ─────────────────────────────────────────────────────────────
async function _tmuxTogglePersistent(s, toast, refreshLive) {
  const next = !s.persistent;
  try {
    const r = await fetch(apiUrl(`/api/orchestrator/sessions/${encodeURIComponent(s.session_id)}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ persistent: next }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      toast && toast(`Nie udało się ${next ? 'przypiąć' : 'odpiąć'}: ${(d && d.detail) || r.status}`, 'err');
      return;
    }
    toast && toast(next ? 'Sesja przypięta (keep-alive)' : 'Zdjęto keep-alive', 'ok');
    refreshLive && refreshLive();
  } catch (e) {
    toast && toast('Akcja nie powiodła się', 'err');
  }
}

// Confirmation is handled by the in-app ConfirmModal in SystemView — this just
// runs the kill. Returns true on success so the caller can close the modal.
async function _tmuxKillSession(s, toast, refreshLive) {
  try {
    const r = await fetch(apiUrl(`/api/orchestrator/sessions/${encodeURIComponent(s.session_id)}/close`), {
      method: 'POST',
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      toast && toast(`Nie udało się zabić sesji: ${(d && d.detail) || r.status}`, 'err');
      return false;
    }
    toast && toast('Sesja zabita · slot zwolniony', 'ok');
    refreshLive && refreshLive();
    return true;
  } catch (e) {
    toast && toast('Nie udało się zabić sesji', 'err');
    return false;
  }
}

function SystemView({ compact, onOpenService }) {
  const { live, refreshLive } = useHubData();
  const toast = useToast();
  // In-app confirm for killing a tmux session (replaces native confirm()).
  const [killTarget, setKillTarget] = useState(null);
  const [killBusy, setKillBusy] = useState(false);
  // Which metric's per-process drill-down is open (cpu|mem|disk|null). Swap
  // (#104) reuses the mem breakdown — per-process swap needs costly smaps_rollup.
  const [drill, setDrill] = useState(null);

  if (!live) {
    return (
      <div style={{ padding: compact ? 16 : 28 }}>
        <SectionHeader eyebrow="live · refresh 5s" title="System"/>
        <div style={{ marginTop: 18, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>loading…</div>
      </div>
    );
  }

  const cpu = live.cpu || {};
  const mem = live.memory || {};
  const dsk = live.disk || {};
  const cpuHistory = live.cpuHistory && live.cpuHistory.length ? live.cpuHistory : [cpu.load_1m || 0];
  const memHistory = live.memHistory && live.memHistory.length ? live.memHistory : [mem.percent || 0];

  const services = (live.services || []).map(s => ({
    name: s.unit,
    scope: s.scope,
    state: s.active,
    enabled: s.enabled,
    status: s.active === 'active' ? 'ok' : (s.active === 'failed' ? 'err' : 'warn'),
  }));

  const peers = (live.peers || []).map(p => ({
    host: p.hostname || '?',
    ip: p.tailscale_ip || '',
    os: p.os || '',
    online: !!p.online,
    last: p.online ? 'now' : (p.last_seen ? new Date(p.last_seen).toLocaleString() : '—'),
  }));

  const ramUsedMB = (mem.used_bytes || 0) / 1024 / 1024;
  const ramTotalMB = (mem.total_bytes || 0) / 1024 / 1024;
  const swapUsedMB = (mem.swap_used_bytes || 0) / 1024 / 1024;
  const swapTotalMB = (mem.swap_total_bytes || 0) / 1024 / 1024;
  const diskUsedGB = (dsk.used_bytes || 0) / 1e9;
  const diskTotalGB = (dsk.total_bytes || 0) / 1e9;

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <SectionHeader
        eyebrow="live · refresh 5s"
        title="System"
        actions={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--fg-3)', fontSize: 'var(--t-cap)' }} className="mono">
            <span className="orbit-core" style={{ width: 8, height: 8 }}/>
            <span>updated <RelativeTime ts={live.ts}/></span>
            <IconButton icon="refresh" label="Refresh" onClick={() => refreshLive && refreshLive()} />
          </div>
        }
      />

      {/* metric cards */}
      <div style={{
        marginTop: 18, display: 'grid', gap: 12,
        gridTemplateColumns: compact ? '1fr 1fr' : 'repeat(4, 1fr)',
      }}>
        <MetricCard label="CPU" onClick={() => setDrill('cpu')} mono={`${(cpu.load_1m || 0).toFixed(2)} · ${cpu.cpu_count || '—'}c`}>
          <Sparkline data={cpuHistory} width={compact ? 130 : 160} height={32}/>
          <div style={{ display: 'flex', gap: 12, marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} className="mono">
            <span>1m {(cpu.load_1m || 0).toFixed(2)}</span>
            <span>5m {(cpu.load_5m || 0).toFixed(2)}</span>
            <span>15m {(cpu.load_15m || 0).toFixed(2)}</span>
          </div>
        </MetricCard>
        <MetricCard label="RAM" onClick={() => setDrill('mem')} mono={`${(ramUsedMB / 1024).toFixed(1)} / ${(ramTotalMB / 1024).toFixed(0)} GB`}>
          <Sparkline data={memHistory} width={compact ? 130 : 160} height={32}/>
          <div style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} className="mono">{Math.round(mem.percent || 0)}% used</div>
        </MetricCard>
        <MetricCard label="Swap" onClick={() => setDrill('mem')} mono={swapTotalMB > 0 ? `${swapUsedMB.toFixed(0)} / ${(swapTotalMB / 1024).toFixed(1)} GB` : 'none'}>
          <ProgressBar value={mem.swap_used_bytes || 0} max={mem.swap_total_bytes || 1} color="var(--info)"/>
          <div style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} className="mono">{Math.round(mem.swap_percent || 0)}% used</div>
        </MetricCard>
        <MetricCard label="Disk /" onClick={() => setDrill('disk')} mono={`${diskUsedGB.toFixed(0)} / ${diskTotalGB.toFixed(0)} GB`}>
          <ProgressBar value={dsk.used_bytes || 0} max={dsk.total_bytes || 1} color="var(--warn)"/>
          <div style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} className="mono">{Math.round(dsk.percent || 0)}% used</div>
        </MetricCard>
      </div>

      {/* Per-process drill-down — opened by clicking a metric card (#104).
          Component published on window by system-drilldown.jsx. */}
      {drill && window.SystemDrilldownModal && (
        <window.SystemDrilldownModal metric={drill} open onClose={() => setDrill(null)} />
      )}

      {/* services + peers */}
      <div style={{
        marginTop: 18, display: 'grid', gap: 12,
        gridTemplateColumns: compact ? '1fr' : '1.2fr 1fr',
      }}>
        <Card padding={0}>
          <SubHeader title="Services" sub={`${services.filter(s => s.status === 'ok').length}/${services.length} healthy`} />
          <div>
            {services.map((s, i) => (
              <div key={s.name} onClick={() => onOpenService && onOpenService(s)} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '10px 14px',
                borderTop: i === 0 ? 'none' : '1px solid var(--hairline)',
                cursor: 'pointer',
              }}>
                <StatusDot status={s.status}/>
                <span style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{s.name}</span>
                <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)', padding: '2px 6px', background: 'var(--surface-3)', borderRadius: 4 }}>{s.scope}</span>
                <span style={{ flex: 1 }}/>
                <span className="mono" style={{ fontSize: 'var(--t-cap)', color: s.status === 'ok' ? 'var(--fg-3)' : (s.status === 'warn' ? 'var(--warn)' : 'var(--err)') }}>{s.state}</span>
              </div>
            ))}
            {services.length === 0 && <div style={{ padding: 18, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>no services reported</div>}
          </div>
        </Card>

        <Card padding={0}>
          <SubHeader title="Tailscale peers" sub={`${peers.filter(p => p.online).length}/${peers.length} online`} />
          <div>
            {peers.map((p, i) => (
              <div key={p.host + i} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '10px 14px',
                borderTop: i === 0 ? 'none' : '1px solid var(--hairline)',
                opacity: p.online ? 1 : 0.55,
              }}>
                <StatusDot status={p.online ? 'ok' : 'off'}/>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>{p.host}</div>
                  <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{p.ip} · {p.os}</div>
                </div>
                <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{p.last}</span>
              </div>
            ))}
            {peers.length === 0 && <div style={{ padding: 18, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>no peers</div>}
          </div>
        </Card>
      </div>

      {/* Interactive tmux session pool — diagnostics. Sorted youngest-first
          server-side; "stały" = one of the pool_size hot slots (never
          evicted), otherwise a cooldown countdown until teardown. */}
      {(() => {
        const tmux = live.tmux || {};
        const tslots = tmux.slots || [];
        return (
          <div style={{ marginTop: 18 }}>
            <Card padding={0}>
              <SubHeader
                title="Sesje interaktywne (tmux)"
                sub={`${tmux.active || 0} aktywne · ${tmux.pool_size || 0} stałych slotów · idle TTL ${_fmtDur(tmux.idle_ttl_s)}`}
              />
              <div>
                {tslots.map((s, i) => {
                  const sid = (s.session_id || '').slice(0, 8);
                  const cwdBase = (s.cwd || '').split('/').filter(Boolean).pop() || '~';
                  const title = (s.title && s.title.trim()) || `sesja ${sid}`;
                  const openSession = () => {
                    if (typeof window.routerReplace === 'function' && typeof window.buildPath === 'function') {
                      // Switches section to orchestrator AND opens the session
                      // (the hub's useRouter re-reads the path on router:change).
                      window.routerReplace(window.buildPath({ section: 'orchestrator', sessionId: s.session_id }));
                    }
                  };
                  return (
                    <div key={s.session_id} onClick={openSession} title="Otwórz sesję" style={{
                      display: 'flex', alignItems: 'center', gap: 12,
                      padding: '10px 14px',
                      borderTop: i === 0 ? 'none' : '1px solid var(--hairline)',
                      cursor: 'pointer',
                    }}>
                      <StatusDot status={s.cooling ? 'warn' : 'ok'} />
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</div>
                        <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {sid} · {cwdBase} · up {_fmtDur(s.uptime_s)} · idle {_fmtDur(s.idle_s)}
                        </div>
                      </div>
                      {s.persistent
                        ? <span className="mono" title="Keep-alive — wyłączona z auto-reapu" style={{ fontSize: 'var(--t-xs)', color: 'var(--ok)', padding: '2px 6px', background: 'oklch(0.62 0.13 150 / 0.16)', borderRadius: 4, flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4 }}><Icon name="pin-fill" size={11} color="var(--ok)" /> keep-alive</span>
                        : s.cooling
                          ? <_TmuxCountdown seconds={s.evict_in_s} />
                          : <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)', padding: '2px 6px', background: 'var(--surface-3)', borderRadius: 4, flexShrink: 0 }}>stały</span>}
                      {/* stopPropagation: the whole row opens the session on click,
                          so the kebab toggle + its menu items must not bubble up. */}
                      <div onClick={e => e.stopPropagation()} style={{ flexShrink: 0 }}>
                        <KebabMenu
                          size={28}
                          items={[
                            {
                              icon: s.persistent ? 'pin' : 'pin-fill',
                              label: s.persistent ? 'Zdejmij keep-alive' : 'Przypnij (keep-alive)',
                              onClick: () => _tmuxTogglePersistent(s, toast, refreshLive),
                            },
                            {
                              icon: 'stop-circle',
                              label: 'Zabij · zwolnij slot',
                              danger: true,
                              onClick: () => setKillTarget(s),
                            },
                          ]}
                        />
                      </div>
                      <Icon name="chevron-r" size={16} color="var(--fg-4)" />
                    </div>
                  );
                })}
                {tslots.length === 0 && (
                  <div style={{ padding: 18, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>brak aktywnych sesji</div>
                )}
              </div>
            </Card>
          </div>
        );
      })()}

      {window.ConfirmModal && (
        <window.ConfirmModal
          open={!!killTarget}
          onClose={() => { if (!killBusy) setKillTarget(null); }}
          title="Zabić sesję?"
          message={killTarget
            ? `Zabić „${(killTarget.title && killTarget.title.trim()) || `sesja ${(killTarget.session_id || '').slice(0, 8)}`}" i zwolnić slot? Transkrypt zostaje — sesję można otworzyć ponownie.`
            : ''}
          confirmLabel="Zabij · zwolnij slot"
          cancelLabel="Anuluj"
          variant="danger"
          busy={killBusy}
          onConfirm={async () => {
            if (!killTarget) return;
            setKillBusy(true);
            await _tmuxKillSession(killTarget, toast, refreshLive);
            setKillBusy(false);
            setKillTarget(null);
          }}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// AREAS
// ─────────────────────────────────────────────────────────────
// Shared CRUD action helpers — used by AreasView and ProjectsView card
// kebabs. Both rename + archive dispatch `library:reload` so the
// DataProvider reloads /api/data without prop-drilling.
//
// URL segment uses `lib_id` (relative path under ~/Areas/ or ~/Projects/)
// rather than `name` (basename). For nested projects like
// "parent/child" this is essential — backend's _safe_project_path
// resolves multi-segment paths but only when given the full lib_id.
function _libraryEncodeLibId(item) {
  const id = (item && (item.lib_id || item.name)) || '';
  return String(id).split('/').map(encodeURIComponent).join('/');
}

async function _libraryRename(kind, item, toast) {
  const newName = prompt('Rename to:', item.name);
  if (!newName || newName === item.name) return;
  try {
    const r = await fetch(apiUrl(`/api/library/${kind}/${_libraryEncodeLibId(item)}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: newName.trim() }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      toast && toast(`Rename failed: ${(d && d.error) || r.status}`, 'err');
      return;
    }
    toast && toast('Renamed', 'ok');
    window.dispatchEvent(new CustomEvent('library:reload'));
  } catch (e) {
    toast && toast('Rename failed', 'err');
  }
}

async function _libraryArchive(kind, item, toast) {
  if (!confirm(`Move "${item.name}" to archive?`)) return;
  try {
    const r = await fetch(apiUrl(`/api/library/${kind}/${_libraryEncodeLibId(item)}`), { method: 'DELETE' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      toast && toast(`Archive failed: ${(d && d.error) || r.status}`, 'err');
      return;
    }
    toast && toast('Archived', 'ok');
    window.dispatchEvent(new CustomEvent('library:reload'));
  } catch (e) {
    toast && toast('Archive failed', 'err');
  }
}

function AreasView({ compact, onOpenArea }) {
  const { areas = [] } = useHubData();
  const [modalOpen, setModalOpen] = useState(false);
  const toast = useToast();
  const CreateModal = window.LibraryCreateModal;
  // Synthetic "Global" meta-area → /global (artifacts + prompt of the global
  // agent: sessions with no project/area). Not a real PARA dir.
  const _router = window.useRouter ? window.useRouter() : null;
  const openGlobal = () => { if (_router && window.buildPath) _router.push(window.buildPath({ section: 'global' })); };
  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <SectionHeader
        eyebrow="PARA"
        title="Areas"
        count={`${areas.length} active`}
        actions={<Button icon="plus" onClick={() => setModalOpen(true)}>New area</Button>}
      />
      <div style={{
        marginTop: 18, display: 'grid', gap: 14,
        gridTemplateColumns: compact ? '1fr' : 'repeat(2, 1fr)',
      }}>
        {/* Global pinned first, intentionally outside areas.map (AreasView
            doesn't sort areas; Global is a fixed meta-entry, not a real area). */}
        <Card key="__global__" hover padding={18} onClick={openGlobal}
          style={{ cursor: 'pointer', position: 'relative', borderColor: 'var(--accent-line)', boxShadow: 'var(--glow-soft)' }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
            <div style={{
              width: 44, height: 44, borderRadius: 'var(--r-md)',
              background: 'var(--grad-cosmic-soft)', border: '1px solid var(--accent-line)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22, flexShrink: 0,
            }}>🤖</div>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                <span className="orbit-gradient-text" style={{ fontSize: 17, fontWeight: 500 }}>Global</span>
                <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>meta-area</span>
              </div>
              <div style={{ marginTop: 6, fontSize: 'var(--t-sm)', color: 'var(--fg-2)', lineHeight: 1.5 }}>
                Artefakty i prompt globalnego agenta (sesje bez projektu/area).
              </div>
            </div>
          </div>
        </Card>
        {areas.map(a => {
          const chips = (a.linked_projects || []).slice(0, 5);
          const more = Math.max(0, (a.linked_projects || []).length - 5);
          return (
            <Card
              key={a.name}
              hover
              padding={18}
              onClick={() => onOpenArea && onOpenArea(a)}
              style={{ cursor: onOpenArea ? 'pointer' : 'default', position: 'relative' }}
            >
              <div style={{ position: 'absolute', top: 10, right: 10 }} onClick={(e) => e.stopPropagation()}>
                <KebabMenu
                  size={28}
                  items={[
                    { icon: 'pencil',  label: 'Rename',  onClick: () => _libraryRename('areas', a, toast) },
                    { icon: 'archive', label: 'Archive', onClick: () => _libraryArchive('areas', a, toast), danger: true },
                  ]}
                />
              </div>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
                <div style={{
                  width: 44, height: 44, borderRadius: 'var(--r-md)',
                  background: 'var(--grad-cosmic-soft)', border: '1px solid var(--accent-line)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 22, flexShrink: 0,
                }}>{a.icon || '📂'}</div>
                <div style={{ minWidth: 0, flex: 1, paddingRight: 32 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
                    <span style={{ fontSize: 17, fontWeight: 500 }}>{a.label || a.name}</span>
                    <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>~/Areas/{a.name}/</span>
                  </div>
                  {a.description && <div style={{ marginTop: 6, fontSize: 'var(--t-sm)', color: 'var(--fg-2)', lineHeight: 1.5 }}>{a.description}</div>}
                  <div className="mono" style={{ marginTop: 12, display: 'flex', gap: 16, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
                    <span>📦 {a.links_projects ?? 0}</span>
                    <span>📚 {a.links_resources ?? 0}</span>
                    <span>📝 {a.notes ?? 0}</span>
                  </div>
                  {(chips.length > 0 || more > 0) && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 12 }}>
                      {chips.map(c => <Chip key={c} mono>📦 {c}</Chip>)}
                      {more > 0 && <Chip mono>+{more}</Chip>}
                    </div>
                  )}
                </div>
              </div>
            </Card>
          );
        })}
        {areas.length === 0 && <div style={{ color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>no areas — add directories under ~/Areas/</div>}
      </div>
      {modalOpen && CreateModal && (
        <CreateModal kind="area" onClose={() => setModalOpen(false)} onCreated={() => setModalOpen(false)} />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// PROJECTS
// ─────────────────────────────────────────────────────────────
function ProjectCard({ p, onOpenProject, toast }) {
  const live = p.exposed != null;
  const liveUrl = p.exposed && p.exposed.path;
  return (
    <Card
      hover
      padding={14}
      onClick={() => onOpenProject && onOpenProject(p)}
      style={{ cursor: 'pointer', position: 'relative' }}
    >
      <div style={{ position: 'absolute', top: 6, right: 6 }} onClick={(e) => e.stopPropagation()}>
        <KebabMenu
          size={26}
          items={[
            { icon: 'pencil',  label: 'Rename',  onClick: () => _libraryRename('projects', p, toast) },
            { icon: 'archive', label: 'Archive', onClick: () => _libraryArchive('projects', p, toast), danger: true },
          ]}
        />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, paddingRight: 28 }}>
        <div style={{
          width: 32, height: 32, borderRadius: 'var(--r-control)',
          background: 'var(--grad-cosmic-soft)', border: '1px solid var(--accent-line)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 'var(--t-h3)', flexShrink: 0,
        }}>{p.icon || '📦'}</div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 'var(--t-md)', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.label || p.name}</div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.rel_path}</div>
        </div>
        {live && <Chip accent mono icon={<span className="orbit-core" style={{ width: 6, height: 6 }}/>}>live</Chip>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 10, gap: 8 }}>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {(p.linked_areas || []).map(a => <Chip key={a} mono>📂 {a}</Chip>)}
        </div>
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', flexShrink: 0 }}>{relTime(p.mtime)}</span>
      </div>
      {live && liveUrl && (
        <a href={liveUrl} onClick={e => e.stopPropagation()} style={{ textDecoration: 'none' }}>
          <Button size="sm" icon="globe" style={{ marginTop: 10 }}>Open {liveUrl}</Button>
        </a>
      )}
    </Card>
  );
}

function ProjectsView({ compact, onOpenProject }) {
  const { projects = [], areas = [] } = useHubData();
  const [filter, setFilter] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const toast = useToast();
  const CreateModal = window.LibraryCreateModal;

  // Group projects by Area (one section per area + a trailing "Bez area"
  // bucket). Falls back to a single flat group if the plain-JS module failed
  // to load, so the view never breaks.
  const groups = useMemo(() =>
    (window.HubProjectsGroup
      ? window.HubProjectsGroup.groupProjectsByArea(projects, areas, filter)
      : [{ key: '__all__', label: 'Projects', icon: '📦', projects: projects.filter(p => !filter || (p.label || p.name).toLowerCase().includes(filter.toLowerCase())) }]),
    [projects, areas, filter]
  );
  const total = useMemo(() =>
    projects.filter(p => !filter || (p.label || p.name).toLowerCase().includes(filter.toLowerCase())).length,
    [projects, filter]
  );

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <SectionHeader
        eyebrow="~/Projects"
        title="Projects"
        count={projects.length}
        actions={<Button icon="plus" onClick={() => setModalOpen(true)}>New project</Button>}
      />
      <div style={{ marginTop: 14 }}>
        <SearchInput value={filter} onChange={setFilter} placeholder="filter projects…" />
      </div>
      {groups.map((g, idx) => (
        <div key={g.key} style={{ marginTop: idx === 0 ? 14 : 22 }}>
          <SubHeader
            title={`${g.icon} ${g.label}`}
            sub={`${g.projects.length}${g.subtitle ? ' · ' + g.subtitle : ''}`}
          />
          <div style={{
            marginTop: 10, display: 'grid', gap: 10,
            gridTemplateColumns: compact ? '1fr' : 'repeat(auto-fill, minmax(280px, 1fr))',
          }}>
            {g.projects.map(p => (
              <ProjectCard key={g.key + '/' + p.lib_id} p={p} onOpenProject={onOpenProject} toast={toast} />
            ))}
          </div>
        </div>
      ))}
      {total === 0 && (
        <div style={{ marginTop: 14, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>{filter ? 'no matches' : 'no projects'}</div>
      )}
      {modalOpen && CreateModal && (
        <CreateModal kind="project" onClose={() => setModalOpen(false)} onCreated={() => setModalOpen(false)} />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// APPS
// ─────────────────────────────────────────────────────────────
function AppsView({ compact }) {
  const { apps = [] } = useHubData();
  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <SectionHeader eyebrow="exposed via nginx" title="Apps" count={apps.length}/>
      <div style={{
        marginTop: 18, display: 'grid', gap: 12,
        gridTemplateColumns: compact ? '1fr' : 'repeat(auto-fill, minmax(260px, 1fr))',
      }}>
        {apps.map(a => {
          const isExternal = !!a.external;
          const href = isExternal ? a.url : (a.path ? a.path + (a.path.endsWith('/') ? '' : '/') : '#');
          // Apps are separate web apps (own origin / nginx vhost) — always
          // open them in a new tab so the dashboard stays put underneath.
          const target = '_blank';
          const rel = 'noopener';
          const subtitle = isExternal
            ? a.url
            : `${a.path}${a.port ? ' · :' + a.port : ''}`;
          return (
            <Card key={a.name + (a.path || a.url || '')} hover padding={16}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{ fontSize: 26 }}>{a.icon || '🔗'}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 'var(--t-body)', fontWeight: 500 }}>{a.label || a.name}</span>
                    <StatusDot status="ok"/>
                  </div>
                  <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{subtitle}</div>
                </div>
              </div>
              {a.description && <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)', marginTop: 10, lineHeight: 1.5 }}>{a.description}</div>}
              <Button href={href} target={target} rel={rel} size="sm" icon="globe" style={{ marginTop: 12 }}>{isExternal ? 'Open ↗' : 'Open ↗'}</Button>
            </Card>
          );
        })}
        {apps.length === 0 && <div style={{ color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>no apps</div>}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// RESOURCES
// ─────────────────────────────────────────────────────────────
function ResourcesView({ compact }) {
  const { resources = [] } = useHubData();
  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <SectionHeader eyebrow="PARA · reference" title="Resources" count={resources.length}/>
      <div style={{
        marginTop: 18, display: 'grid', gap: 12,
        gridTemplateColumns: compact ? '1fr' : 'repeat(auto-fill, minmax(260px, 1fr))',
      }}>
        {resources.map(r => (
          <Card key={r.name} hover padding={16}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ fontSize: 24 }}>{r.icon || '📚'}</div>
              <div>
                <div style={{ fontSize: 'var(--t-body)', fontWeight: 500 }}>{r.label || r.name}</div>
                <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{r.files ?? 0} files</div>
              </div>
            </div>
            {r.description && <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)', marginTop: 10 }}>{r.description}</div>}
          </Card>
        ))}
        {resources.length === 0 && <div style={{ color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>no resources — add directories under ~/Resources/</div>}
      </div>
    </div>
  );
}

Object.assign(window, {
  ShareView, SystemView, AreasView, ProjectsView, AppsView, ResourcesView,
  MetricCard, SubHeader,
});
