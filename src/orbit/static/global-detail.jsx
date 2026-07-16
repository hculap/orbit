// global-detail.jsx — the "Global" meta-area detail view.
//
// The Global agent has no PARA dir / git repo, so it can't reuse LibraryDetail
// (which is wired to /api/library/{kind}/{name}/*). This is a focused stand-in
// that mimics the area/project detail look (header + tab bar) with the three
// tabs that make sense for global scope:
//   • Overview — counts (global artifacts + global sessions) + blurb
//   • Agent    — read-only general/orchestrator prompt layers + an editable
//                custom layer (orchestrator-custom.md) via
//                /api/orchestrator/global-agent/prompt
//   • Galeria  — artifacts from the global scope (lib_id=__global__), reusing
//                window.ArtifactGallery
//
// Rendered as a top-level section (window.GlobalDetail, route /global). Deps via
// window globals (no bundler): Card, Icon, Button, useToast, apiUrl,
// ArtifactGallery, useRouter, buildPath.

const { useState: _gdUseState, useEffect: _gdUseEffect, useCallback: _gdUseCallback } = React;

// ── Overview ─────────────────────────────────────────────────────────
function _GlobalOverview({ compact }) {
  const [stats, setStats] = _gdUseState(null);
  _gdUseEffect(() => {
    let cancelled = false;
    (async () => {
      const out = { artifacts: null, sessions: null };
      try {
        const r = await fetch(apiUrl('/api/orchestrator/artifacts?lib_id=__global__'));
        if (r.ok) { const d = await r.json(); out.artifacts = Array.isArray(d.artifacts) ? d.artifacts.length : 0; }
      } catch (_e) { /* leave null */ }
      try {
        const r = await fetch(apiUrl('/api/orchestrator/sessions'));
        if (r.ok) {
          const d = await r.json();
          const list = Array.isArray(d) ? d : (Array.isArray(d.sessions) ? d.sessions : []);
          out.sessions = list.filter(s => s && !s.lib_id && !s.archived).length;
        }
      } catch (_e) { /* leave null */ }
      if (!cancelled) setStats(out);
    })();
    return () => { cancelled = true; };
  }, []);

  const cells = [
    { k: 'scope', v: 'global (cwd ~/, no agent)' },
    { k: 'global sesje', v: stats && stats.sessions != null ? String(stats.sessions) : '…' },
    { k: 'artefakty', v: stats && stats.artifacts != null ? String(stats.artifacts) : '…' },
  ];
  return (
    <div style={{ display: 'grid', gap: 14, gridTemplateColumns: compact ? '1fr' : '1.4fr 1fr' }}>
      <Card padding={18}>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>Global agent</div>
        <div style={{ fontSize: 'var(--t-md)', lineHeight: 1.6, color: 'var(--fg)' }}>
          Domyślny agent Claude (sesje bez przypisanego projektu/area). Tu zobaczysz jego artefakty i ustawisz wspólny prompt dla wszystkich globalnych sesji.
        </div>
        <div style={{ marginTop: 16, display: 'grid', gridTemplateColumns: compact ? '1fr 1fr' : 'repeat(3, 1fr)', gap: 12 }}>
          {cells.map(c => (
            <div key={c.k}>
              <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 }}>{c.k}</div>
              <div className="mono" style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>{c.v}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

// ── Agent (prompt layers) ────────────────────────────────────────────
function _GlobalReadonlyLayer({ label, content }) {
  if (!content) return null;
  return (
    <Card padding={0}>
      <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--hairline)', display: 'flex', alignItems: 'center', gap: 8 }}>
        <Icon name="file" size={13} color="var(--fg-3)" />
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-2)' }}>{label}</span>
        <span style={{ flex: 1 }} />
        <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>read-only</span>
      </div>
      <pre className="mono" style={{ margin: 0, padding: '12px 16px', maxHeight: 220, overflow: 'auto', fontSize: 'var(--t-cap)', lineHeight: 1.6, color: 'var(--fg-2)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{content}</pre>
    </Card>
  );
}

function _GlobalAgentPrompt() {
  const toast = useToast();
  const [data, setData] = _gdUseState(null);
  const [draft, setDraft] = _gdUseState('');
  const [busy, setBusy] = _gdUseState(false);
  const [err, setErr] = _gdUseState(null);

  _gdUseEffect(() => {
    let cancelled = false;
    fetch(apiUrl('/api/orchestrator/global-agent/prompt'))
      .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(d => { if (!cancelled) { setData(d); setDraft((d.custom && d.custom.content) || ''); } })
      .catch(e => { if (!cancelled) setErr(String(e)); });
    return () => { cancelled = true; };
  }, []);

  const save = _gdUseCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch(apiUrl('/api/orchestrator/global-agent/prompt'), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ custom: draft }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      // Sync `data` with the saved content so the dirty flag clears and Save
      // disables (the PATCH echoes the persisted custom layer).
      const saved = await r.json();
      setData(prev => (prev ? { ...prev, custom: (saved && saved.custom) || { content: draft } } : prev));
      toast && toast('Zapisano prompt', 'ok');
    } catch (e) {
      toast && toast('Zapis nieudany: ' + ((e && e.message) || e), 'err');
    } finally { setBusy(false); }
  }, [draft, toast]);

  if (err) return <div style={{ padding: 16, color: 'var(--err)', fontSize: 'var(--t-sm)' }}>Błąd ładowania promptu: {err}</div>;
  if (!data) return <div style={{ padding: 16 }}><Spinner label="ładowanie…" inline size={16} /></div>;

  const dirty = draft !== ((data.custom && data.custom.content) || '');
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div>
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>
          Custom prompt (global) — orchestrator-custom.md
        </div>
        <Textarea
          value={draft} onChange={setDraft}
          placeholder="Dodatkowe instrukcje dla wszystkich globalnych sesji…"
          rows={8}
          mono
          style={{ width: '100%', minHeight: 180 }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 8 }}>
          <Button icon="check" onClick={save} disabled={busy || !dirty}>{busy ? 'Zapisywanie…' : 'Zapisz'}</Button>
        </div>
      </div>
      <_GlobalReadonlyLayer label="general.md (dziedziczone przez wszystkich)" content={data.general && data.general.content} />
      <_GlobalReadonlyLayer label="orchestrator.md (zarządzane)" content={data.orchestrator && data.orchestrator.content} />
    </div>
  );
}

// ── shell ────────────────────────────────────────────────────────────
function GlobalDetail({ compact }) {
  const router = window.useRouter ? window.useRouter() : null;
  const [tab, setTab] = _gdUseState('overview');
  const goBack = () => {
    if (router && window.buildPath) router.push(window.buildPath({ section: 'areas' }));
  };
  // Escape → back, matching the library/area detail interaction — but ignore
  // it while typing in the prompt editor (Escape there should just blur).
  _gdUseEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      const t = e.target;
      if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT')) return;
      goBack();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // router.push is stable (useCallback in router.jsx); bind once.
  }, []);
  const tabs = [
    { k: 'overview', l: 'Overview' },
    { k: 'agent', l: 'Agent' },
    { k: 'gallery', l: 'Galeria' },
  ];
  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        {/* Own back arrow + eyebrow always shown — on mobile the shell renders
            the drawer HAMBURGER above (Global is a section), and this is the
            only back affordance, matching the project/area detail header. */}
        <IconButton icon="chevron-l" label="Wstecz" size={34} onClick={goBack} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Meta-area</div>
          <div style={{ fontSize: 22, fontWeight: 600, color: 'var(--fg)', display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>🤖</span> Global
          </div>
        </div>
        <KebabMenu items={[
          { icon: 'pencil', label: 'Edytuj prompt', onClick: () => setTab('agent') },
          { icon: 'image', label: 'Galeria', onClick: () => setTab('gallery') },
        ]} />
      </div>

      <div style={{ display: 'flex', gap: 4, borderBottom: '1px solid var(--hairline)', marginBottom: 18, overflowX: 'auto' }} className="scroll-hide">
        {tabs.map(t => (
          <button key={t.k} onClick={() => setTab(t.k)} style={{
            padding: '10px 14px', background: 'transparent', border: 'none', cursor: 'pointer',
            color: tab === t.k ? 'var(--fg)' : 'var(--fg-3)',
            fontSize: 'var(--t-sm)', fontWeight: 500, fontFamily: 'inherit',
            borderBottom: '2px solid ' + (tab === t.k ? 'var(--accent)' : 'transparent'), marginBottom: -1,
          }}>{t.l}</button>
        ))}
      </div>

      {tab === 'overview' && <_GlobalOverview compact={compact} />}
      {tab === 'agent' && <_GlobalAgentPrompt />}
      {tab === 'gallery' && (
        window.ArtifactGallery
          ? <window.ArtifactGallery scope="agent" libId="__global__" compact={compact} />
          : <div className="mono" style={{ padding: 22, fontSize: 'var(--t-cap)', color: 'var(--fg-4)' }}>(gallery unavailable)</div>
      )}
    </div>
  );
}

Object.assign(window, { GlobalDetail });
