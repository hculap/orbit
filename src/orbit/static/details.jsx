// details.jsx — detail/inspector views for Project, Share folder, Log line, Service.
// Ported from designer's mock to wire real backend data via apiUrl().
// Where no real endpoint exists we render an honest placeholder (no fake data).

const { useState, useEffect } = React;

// ─────────────────────────────────────────────────────────────
// DetailHeader — back button + title + actions (presentational)
// ─────────────────────────────────────────────────────────────
function DetailHeader({ onBack, eyebrow, title, sub, actions, compact }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-start', gap: 12,
      padding: compact ? '4px 0 16px' : '0 0 22px',
    }}>
      <button onClick={onBack} style={{
        width: 34, height: 34, borderRadius: 'var(--r-control)',
        background: 'var(--surface-1)', border: '1px solid var(--hairline)',
        color: 'var(--fg-2)', cursor: 'pointer', flexShrink: 0,
        display: 'flex', alignItems: 'center', justifyContent: 'center', marginTop: 2,
      }}>
        <Icon name="chevron-l" size={16}/>
      </button>
      <div style={{ flex: 1, minWidth: 0 }}>
        {eyebrow && (
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 4 }}>
            {eyebrow}
          </div>
        )}
        <div style={{ fontSize: compact ? 22 : 26, fontWeight: 500, letterSpacing: '-0.02em', overflow: 'hidden', textOverflow: 'ellipsis' }}>{title}</div>
        {sub && <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginTop: 4 }}>{sub}</div>}
      </div>
      {actions && <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>{actions}</div>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// MarkdownPane — pure presentational, used as fallback by other details
// ─────────────────────────────────────────────────────────────
function MarkdownPane({ title, body }) {
  return (
    <Card padding={0}>
      <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--hairline)', display: 'flex', alignItems: 'center', gap: 10 }}>
        <Icon name="file" size={14} color="var(--fg-3)"/>
        <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-2)' }}>{title}</span>
      </div>
      <pre className="mono" style={{
        margin: 0, padding: '18px 22px', fontSize: 12.5, lineHeight: 1.7,
        color: 'var(--fg)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      }}>{body}</pre>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// PROJECT DETAIL
// ─────────────────────────────────────────────────────────────
function ProjectDetail({ project, compact, onBack }) {
  const [tab, setTab] = useState('overview');
  const tabs = [
    { k: 'overview', l: 'Overview' },
    { k: 'files',    l: 'Files' },
    { k: 'claude',   l: 'CLAUDE.md' },
    { k: 'readme',   l: 'README' },
  ];

  const liveLabel = project.exposed
    ? `${project.exposed.path} :${project.exposed.port}`
    : null;
  const updatedLabel = project.mtime ? relTime(project.mtime) : '—';
  const areas = Array.isArray(project.linked_areas) && project.linked_areas.length
    ? project.linked_areas.join(', ')
    : '—';

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <DetailHeader
        onBack={onBack} compact={compact}
        eyebrow="Project"
        title={project.label || project.name}
        sub={project.rel_path}
        actions={!compact && liveLabel && (
          <>
            <Button icon="globe">Open {project.exposed.path}</Button>
            <IconButton icon="dots" label="More" size={34}/>
          </>
        )}
      />

      {/* meta row — chips */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 18 }}>
        {liveLabel && (
          <Chip accent mono icon={<span className="live-dot" style={{ width: 6, height: 6 }}/>}>
            live · {liveLabel}
          </Chip>
        )}
        {Array.isArray(project.linked_areas) && project.linked_areas.map(a => (
          <Chip key={a} mono>📂 {a}</Chip>
        ))}
        <Chip mono>updated {updatedLabel}</Chip>
      </div>

      {/* tabs */}
      <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--hairline)', marginBottom: 18, overflowX: 'auto' }} className="scroll-hide">
        {tabs.map(t => (
          <button key={t.k} onClick={() => setTab(t.k)} style={{
            padding: '10px 14px', background: 'transparent', border: 'none', cursor: 'pointer',
            color: tab === t.k ? 'var(--fg)' : 'var(--fg-3)',
            fontSize: 'var(--t-sm)', fontWeight: 500, fontFamily: 'inherit',
            borderBottom: '2px solid ' + (tab === t.k ? 'var(--accent)' : 'transparent'),
            marginBottom: -1,
          }}>{t.l}</button>
        ))}
      </div>

      {tab === 'overview' && (
        <ProjectOverview
          project={project}
          compact={compact}
          updatedLabel={updatedLabel}
          liveLabel={liveLabel}
          areas={areas}
        />
      )}
      {tab === 'files'  && <ProjectPlaceholder note={`(file tree not available — ssh + ls ~/Projects/${project.name})`}/>}
      {tab === 'claude' && <ProjectPlaceholder note={`(open via SSH: cat ~/Projects/${project.name}/CLAUDE.md)`}/>}
      {tab === 'readme' && <ProjectPlaceholder note={`(open via SSH: cat ~/Projects/${project.name}/README.md)`}/>}
    </div>
  );
}

function ProjectOverview({ project, compact, updatedLabel, liveLabel, areas }) {
  const stats = [
    { k: 'path',    v: project.rel_path || '—' },
    { k: 'updated', v: updatedLabel },
    { k: 'live',    v: liveLabel || 'no' },
    { k: 'areas',   v: areas },
  ];
  const description = project.exposed && project.exposed.description
    ? project.exposed.description
    : null;

  return (
    <div style={{ display: 'grid', gap: 14, gridTemplateColumns: compact ? '1fr' : '1.4fr 1fr' }}>
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          {description ? 'Description' : 'Project'}
        </div>
        <div style={{ fontSize: 'var(--t-md)', lineHeight: 1.6, color: 'var(--fg)' }}>
          {description || `Local working tree at ~/${project.rel_path || ''}. Detailed git/test/dependency stats are not exposed by the backend yet — open via SSH for live state.`}
        </div>
        <div style={{ marginTop: 16, display: 'grid', gridTemplateColumns: compact ? '1fr 1fr' : 'repeat(2, 1fr)', gap: 12 }}>
          {stats.map(s => (
            <div key={s.k}>
              <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 }}>{s.k}</div>
              <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }} className="mono">{s.v}</div>
            </div>
          ))}
        </div>
      </Card>
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
          SSH cheats
        </div>
        <div className="mono" style={{ fontSize: 'var(--t-cap)', lineHeight: 1.8, color: 'var(--fg-2)' }}>
          <div>cd ~/{project.rel_path || ''}</div>
          <div>git status</div>
          <div>cat CLAUDE.md</div>
        </div>
      </Card>
    </div>
  );
}

function ProjectPlaceholder({ note }) {
  return (
    <Card padding={22}>
      <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', lineHeight: 1.7 }}>
        {note}
      </div>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────
// FileCardLite — local copy (sections.jsx FileCard not exposed globally)
// ─────────────────────────────────────────────────────────────
function FileCardLite({ f, compact, selected, onSelect, onPreview, onOpenFolder }) {
  const [hover, setHover] = useState(false);
  const isFolder = f.type === 'dir' || f.folder === true;
  const isImg = !isFolder && /\.(png|jpe?g|heic|webp|gif|bmp|svg)$/i.test(f.name);
  const sizeStr = typeof f.size === 'number' ? fmtBytes(f.size) : (f.size || '');
  const mtimeStr = typeof f.mtime === 'number' ? relTime(f.mtime) : (f.mtime || '');

  return (
    <div
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      onClick={() => isFolder ? (onOpenFolder && onOpenFolder()) : (onPreview && onPreview())}
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
        background: isImg
          ? 'linear-gradient(135deg, oklch(0.30 0.04 240), oklch(0.40 0.06 280))'
          : 'var(--surface-2)',
        marginBottom: 10, position: 'relative', overflow: 'hidden',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <FileIconBox name={f.name} isFolder={isFolder} size={44}/>
        <button
          onClick={e => { e.stopPropagation(); onSelect && onSelect(); }}
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
// SHARE — folder detail (deeper navigation into a Sync folder)
// ─────────────────────────────────────────────────────────────
function ShareFolderDetail({ folder, compact, onBack, onPreview }) {
  // Internal navigation stack of relative paths (head = current dir). The entry
  // folder is the stack root; descending into a subfolder pushes, Back pops —
  // only falling through to onBack (exit to the section list) at the root. This
  // gives recursive in-app folder navigation; the existing fetch effect (keyed
  // on `rel`) reloads on every push/pop. (The share folder detail is local
  // state, not a URL route — so this is in-app Back, not browser history.)
  const rootRel = folder.rel || folder.path || folder.name || '';
  const [stack, setStack] = useState([rootRel]);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const rel = stack[stack.length - 1];
  // Reset the stack when the ENTRY folder changes: the folder detail isn't
  // remounted across top-level folders (same DetailRouter key), so without this
  // the previous folder's path stack would leak into a newly opened one.
  useEffect(() => { setStack([rootRel]); }, [rootRel]);
  const descend = (childRel) => { setSelected(new Set()); setStack(s => [...s, childRel]); };
  const goBack = () => {
    if (stack.length > 1) { setSelected(new Set()); setStack(s => s.slice(0, -1)); return; }
    onBack && onBack();
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(apiUrl(`/api/share/${encodePath(rel)}`))
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`)))
      .then(data => {
        if (cancelled) return;
        if (data && data.ok) {
          setItems(Array.isArray(data.items) ? data.items : []);
        } else {
          setError((data && data.error) || 'failed to load folder');
          setItems([]);
        }
      })
      .catch(err => {
        if (cancelled) return;
        setError(err.message || 'fetch failed');
        setItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [rel]);

  const toggle = (n) => setSelected(s => {
    const next = new Set(s);
    if (next.has(n)) next.delete(n); else next.add(n);
    return next;
  });
  const clearSelection = () => setSelected(new Set());

  const totalSize = items.reduce((acc, it) => acc + (typeof it.size === 'number' ? it.size : 0), 0);
  const subPath = rel ? `~/Sync/${rel}` : '~/Sync';

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <DetailHeader
        onBack={goBack} compact={compact}
        eyebrow="Sync · folder"
        title={folder.name || rel || 'Sync'}
        sub={subPath}
        actions={!compact && (
          <>
            <Button icon="upload">Upload</Button>
            <Button icon="download">Download all</Button>
          </>
        )}
      />

      <div style={{ display: 'flex', gap: 12, fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginBottom: 14 }} className="mono">
        <span>{loading ? '…' : `${items.length} items`}</span>
        <span>·</span>
        <span>{loading ? '' : fmtBytes(totalSize) + ' total'}</span>
        {folder.mtime && (
          <>
            <span>·</span>
            <span>updated {typeof folder.mtime === 'number' ? relTime(folder.mtime) : folder.mtime}</span>
          </>
        )}
      </div>

      {selected.size > 0 && (
        <div style={{
          display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12,
          padding: '8px 12px', background: 'var(--accent-soft)',
          border: '1px solid var(--accent-line)', borderRadius: 'var(--r-md)',
        }}>
          <span className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-2)' }}>
            {selected.size} selected
          </span>
          <span style={{ flex: 1 }}/>
          <Button size="sm" variant="quiet" icon="download">Download</Button>
          <Button size="sm" variant="quiet" icon="close" onClick={clearSelection}>Clear</Button>
        </div>
      )}

      {error && (
        <StatusBanner variant="err" label={`error: ${error}`} style={{ marginBottom: 14 }} />
      )}

      {!loading && !error && items.length === 0 && (
        <EmptyState icon="inbox" message="(empty folder)" padded />
      )}

      {items.length > 0 && (
        <div style={{
          display: 'grid', gap: 10,
          gridTemplateColumns: compact ? '1fr 1fr' : 'repeat(auto-fill, minmax(170px, 1fr))',
        }}>
          {items.map(f => (
            <FileCardLite
              key={f.name}
              f={f}
              compact={compact}
              selected={selected.has(f.name)}
              onSelect={() => toggle(f.name)}
              onPreview={() => onPreview && onPreview({ ...f, rel: rel ? `${rel}/${f.name}` : f.name })}
              onOpenFolder={() => descend(rel ? `${rel}/${f.name}` : f.name)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// LOG DETAIL — single log line expanded with structured fields
// ─────────────────────────────────────────────────────────────
function LogDetail({ line, context, compact, onBack }) {
  const lvl = line.lvl || 'INFO';
  const lvlColor = lvl === 'ERROR' ? 'var(--err)' : lvl === 'WARN' ? 'var(--warn)' : 'var(--ok)';
  const priority = lvl === 'ERROR' ? '3' : lvl === 'WARN' ? '4' : '6';
  const hostname = (window.HUB_INITIAL_DATA && window.HUB_INITIAL_DATA.host && window.HUB_INITIAL_DATA.host.name) || 'server';
  const unit = line.src ? `${line.src}.service` : '—';
  // Quick-actions are UI placeholders — none of them are wired yet. The
  // caller (app.jsx:DetailRouter) doesn't pass an onAction prop, so
  // referencing a bare `onAction` (the previous shape) threw at render.
  // Until the underlying flows exist, surface a toast that names the
  // intended action so the buttons still react and aren't silent dead-ends.
  const _toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;
  const onAction = (label) => () => {
    if (_toast) _toast('"' + label + '" — wkrótce', 'info');
  };

  const fields = [
    ['_TRANSPORT',      '—'],
    ['_HOSTNAME',       hostname],
    ['_SYSTEMD_UNIT',   unit],
    ['_PID',            '—'],
    ['_UID',            '—'],
    ['PRIORITY',        priority],
    ['SYSLOG_FACILITY', '—'],
    ['MESSAGE_ID',      '—'],
  ];

  const ctx = Array.isArray(context) ? context : [];
  const myIdx = ctx.findIndex(l => l.t === line.t && l.msg === line.msg);

  const copyLine = async () => {
    await window.HubClipboard.copyText(line.msg || line.raw || '');
  };

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <DetailHeader
        onBack={onBack} compact={compact}
        eyebrow={`Log · ${line.src || 'unknown'}`}
        title={lvl === 'ERROR' ? 'Error' : lvl === 'WARN' ? 'Warning' : 'Info'}
        sub={`${line.t || ''} · journalctl -u ${line.src || ''}`}
      />

      <Card padding={16} style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
          <span style={{ width: 8, height: 8, borderRadius: 'var(--r-pill)', background: lvlColor, display: 'inline-block' }}/>
          <span className="mono" style={{ fontSize: 'var(--t-cap)', color: lvlColor, fontWeight: 600 }}>{lvl}</span>
          <span style={{ flex: 1 }}/>
          <Button size="sm" variant="quiet" icon="download" onClick={copyLine}>Copy</Button>
        </div>
        <div className="mono" style={{ fontSize: 'var(--t-md)', lineHeight: 1.6, color: 'var(--fg)' }}>{line.msg}</div>
      </Card>

      <div style={{
        display: 'grid', gap: 14,
        gridTemplateColumns: compact ? '1fr' : '1fr 1fr',
        marginBottom: 14,
      }}>
        <Card padding={0}>
          <SubHeader title="Structured fields"/>
          <div className="mono" style={{ fontSize: 'var(--t-cap)' }}>
            {fields.map(([k, v], i) => (
              <div key={k} style={{ display: 'flex', padding: '8px 14px', borderTop: i ? '1px solid var(--hairline)' : 'none', gap: 12 }}>
                <span style={{ color: 'var(--fg-4)', width: 130, flexShrink: 0 }}>{k}</span>
                <span style={{ color: 'var(--fg-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card padding={0}>
          <SubHeader title="Quick actions"/>
          <div style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <Button icon="bot" onClick={onAction('ask orchestrator')}>Ask Orchestrator about this</Button>
            <Button icon="logs" onClick={onAction('open journalctl tail')}>Open journalctl tail</Button>
            <Button icon="refresh" onClick={onAction(`restart ${line.src || ''}`)}>Restart {line.src || 'source'}</Button>
            <Button variant="quiet" icon="filter" onClick={onAction(`filter to ${line.src || ''}`)}>Filter to this source</Button>
          </div>
        </Card>
      </div>

      {ctx.length > 0 && (
        <Card padding={0}>
          <SubHeader title="Context · ±5 lines around" sub={`source: ${line.src || ''}`}/>
          <div className="mono scroll-hide" style={{ fontSize: 'var(--t-cap)', lineHeight: 1.7, padding: '8px 0', maxHeight: 320, overflowY: 'auto' }}>
            {ctx.map((l, i) => {
              const me = i === myIdx;
              const lLvl = l.lvl || 'INFO';
              return (
                <div key={i} style={{
                  display: 'flex', gap: 12, padding: '2px 14px',
                  background: me ? 'var(--accent-soft)' : 'transparent',
                  borderLeft: me ? '2px solid var(--accent)' : '2px solid transparent',
                }}>
                  <span style={{ color: 'var(--fg-4)', flexShrink: 0 }}>{l.t}</span>
                  <span style={{ color: lLvl === 'ERROR' ? 'var(--err)' : lLvl === 'WARN' ? 'var(--warn)' : 'var(--fg-3)', width: 48, flexShrink: 0, fontWeight: 600 }}>{lLvl}</span>
                  <span style={{ color: 'var(--accent)', width: 90, flexShrink: 0, opacity: 0.85 }}>{l.src}</span>
                  <span style={{ color: me ? 'var(--fg)' : 'var(--fg-2)', flex: 1, wordBreak: 'break-word' }}>{l.msg}</span>
                </div>
              );
            })}
          </div>
        </Card>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// SERVICE DETAIL — wired to /api/logs/svc:<name>
// ─────────────────────────────────────────────────────────────
function ServiceDetail({ service, compact, onBack }) {
  const [tab, setTab] = useState('summary');
  const tabs = [
    { k: 'summary', l: 'Summary' },
    { k: 'logs',    l: 'Logs' },
    { k: 'unit',    l: 'Unit file' },
  ];
  const isErr  = service.status === 'err';
  const isWarn = service.status === 'warn';
  const isOk   = service.status === 'ok';
  const userScope = service.scope === 'user';

  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <DetailHeader
        onBack={onBack} compact={compact}
        eyebrow={`Service · ${service.scope || 'system'}`}
        title={service.name}
        sub={`systemctl${userScope ? ' --user' : ''} status ${service.name}`}
      />

      {/* status banner */}
      <div style={{
        padding: '14px 16px', borderRadius: 'var(--r-widget)', marginBottom: 18,
        background: isErr ? 'var(--err-bg)' : isWarn ? 'oklch(0.40 0.12 80 / 0.16)' : 'oklch(0.40 0.10 155 / 0.14)',
        border: '1px solid ' + (isErr ? 'var(--err)' : isWarn ? 'var(--warn)' : 'var(--ok)'),
        display: 'flex', alignItems: 'center', gap: 14,
      }}>
        <StatusDot status={service.status} size={12}/>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 'var(--t-md)', fontWeight: 500 }}>
            {isOk && 'Active and running'}
            {isWarn && 'Inactive'}
            {isErr && 'Failed'}
            {!isOk && !isWarn && !isErr && (service.state || 'unknown')}
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2 }}>{service.state || ''}</div>
        </div>
      </div>

      {/* tabs */}
      <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--hairline)', marginBottom: 18 }}>
        {tabs.map(t => (
          <button key={t.k} onClick={() => setTab(t.k)} style={{
            padding: '10px 14px', background: 'transparent', border: 'none', cursor: 'pointer',
            color: tab === t.k ? 'var(--fg)' : 'var(--fg-3)',
            fontSize: 'var(--t-sm)', fontWeight: 500, fontFamily: 'inherit',
            borderBottom: '2px solid ' + (tab === t.k ? 'var(--accent)' : 'transparent'),
            marginBottom: -1,
          }}>{t.l}</button>
        ))}
      </div>

      {tab === 'summary' && <ServiceSummary service={service} compact={compact}/>}
      {tab === 'logs'    && <ServiceLogs service={service}/>}
      {tab === 'unit'    && <ServiceUnitPlaceholder service={service}/>}
    </div>
  );
}

function ServiceSummary({ service, compact }) {
  const userScope = service.scope === 'user';
  const sysPath = userScope ? '/etc/systemd/user' : '/etc/systemd/system';
  const enabledPart = service.enabled ? `; ${service.enabled}` : '';
  const meta = [
    ['Loaded', `loaded (${sysPath}/${service.name}.service${enabledPart})`],
    ['Active', service.state || '—'],
    ['Status', `(detailed status: ssh + systemctl${userScope ? ' --user' : ''} status ${service.name})`],
  ];

  return (
    <div style={{ display: 'grid', gap: 14, gridTemplateColumns: compact ? '1fr' : '1.4fr 1fr' }}>
      <Card padding={0}>
        <SubHeader title="systemctl status"/>
        <div className="mono" style={{ fontSize: 'var(--t-cap)' }}>
          {meta.map(([k, v], i) => (
            <div key={k} style={{ display: 'flex', padding: '9px 14px', borderTop: i ? '1px solid var(--hairline)' : 'none', gap: 12 }}>
              <span style={{ color: 'var(--fg-4)', width: 90, flexShrink: 0 }}>{k}</span>
              <span style={{ color: 'var(--fg-2)', flex: 1, wordBreak: 'break-word' }}>{v}</span>
            </div>
          ))}
        </div>
      </Card>
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>
          Per-service metrics
        </div>
        <div style={{ fontSize: 'var(--t-sm)', lineHeight: 1.6, color: 'var(--fg-2)' }}>
          Per-service CPU/memory history is not collected by the backend.
        </div>
        <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginTop: 10 }}>
          systemctl{userScope ? ' --user' : ''} status {service.name}
        </div>
      </Card>
    </div>
  );
}

function ServiceLogs({ service }) {
  const sourceId = `svc:${service.name}`;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(apiUrl(`/api/logs/${encodeURIComponent(sourceId)}?lines=200`))
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`http ${r.status}`)))
      .then(d => {
        if (cancelled) return;
        if (d && d.ok) setData(d);
        else { setError((d && d.error) || 'unknown source'); setData(null); }
      })
      .catch(err => {
        if (cancelled) return;
        setError(err.message || 'fetch failed');
        setData(null);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [sourceId]);

  const lines = (data && Array.isArray(data.lines)) ? data.lines : [];

  return (
    <Card padding={0}>
      <SubHeader title={`journalctl -u ${service.name} -n 200`} sub={data && data.label ? data.label : 'live'}/>
      <div className="mono scroll-hide" style={{ fontSize: 'var(--t-cap)', lineHeight: 1.7, padding: '8px 0', maxHeight: 360, overflowY: 'auto' }}>
        {loading && <div style={{ padding: '8px 14px' }}><Spinner label="Loading…" inline size={16} /></div>}
        {!loading && error && (
          <div style={{ padding: '8px 14px', color: 'var(--fg-3)' }}>
            (no logs available for source <span style={{ color: 'var(--fg-2)' }}>{sourceId}</span> — {error})
          </div>
        )}
        {!loading && !error && lines.length === 0 && (
          <div style={{ padding: '8px 14px', color: 'var(--fg-3)' }}>(empty)</div>
        )}
        {!loading && !error && lines.map((l, i) => (
          <div key={i} style={{ display: 'flex', gap: 12, padding: '2px 14px' }}>
            <span style={{ color: 'var(--fg-2)', flex: 1, wordBreak: 'break-word' }}>{l}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ServiceUnitPlaceholder({ service }) {
  const userScope = service.scope === 'user';
  return (
    <Card padding={22}>
      <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', lineHeight: 1.7 }}>
        (view via SSH: systemctl{userScope ? ' --user' : ''} cat {service.name})
      </div>
    </Card>
  );
}

Object.assign(window, { ProjectDetail, ShareFolderDetail, LogDetail, ServiceDetail, DetailHeader, MarkdownPane });
