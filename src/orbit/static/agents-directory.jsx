// agents-directory.jsx — /agents landing page.
//
// Replaces the old "Orchestrator" sidebar entry with a directory of every
// agent the hub knows about: the legacy Global agent (today's /orchestrator
// chat), every Area, and every Project. Each card is a click-to-navigate
// shortcut to that agent's home:
//
//   Global  → /orchestrator             (the existing single-chat UI)
//   Area    → /areas/<lib_id>/agent     (LibraryDetail's new Agent tab)
//   Project → /projects/<lib_id>/agent  (idem)
//
// v1 deliberately skips last-activity / session-count enrichment. We render
// areas + projects from `useHubData()` (already cached in DataProvider) and
// don't hit /api/orchestrator/sessions per card — that would fan out N
// network calls on every mount. Defer to a follow-up that batches a single
// "session counts by cwd" endpoint.
//
// Card layout:
//   [icon]  <name>           <kind label>
//           <sub-line>
//
// Empty state: technically reachable only on a fresh install with no Areas
// AND no Projects (Global card always renders), but kept defensive.

const { useMemo: _agentsUseMemo, useState: _agentsUseState, useEffect: _agentsUseEffect } = React;

function _AgentCard({ icon, name, sub, kind, busy, error, unread, active, onOpenChat, onNewSession }) {
  // Card is "click anywhere = open the agent's latest chat"; the corner "+"
  // is a separate hit-target that always starts a brand-new session (the
  // action the user reaches for most). Its onClick stops propagation so it
  // doesn't also fire the card's openChat.
  const startNew = (e) => {
    e.stopPropagation();
    if (onNewSession) onNewSession();
  };
  return (
    <window.Card
      hover
      padding={18}
      onClick={onOpenChat}
      style={{ cursor: busy ? 'progress' : 'pointer', position: 'relative', opacity: busy ? 0.6 : 1 }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
        {/* Avatar — matches LibraryAgentPanel's AgentIdentityHeader (44px
            circle on accent-soft). Per-agent icon from sidecar wins; fall
            back to the static PARA emoji passed in as prop. */}
        <div style={{
          width: 44, height: 44, borderRadius: '50%',
          background: 'var(--accent-soft)', border: '1px solid var(--accent-line)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 22, lineHeight: 1, flexShrink: 0, position: 'relative',
        }}>
          {icon}
          {/* Green dot = a warm tmux session exists for this agent right now.
              (The red "new activity" dot was removed — didn't help in
              practice; the unread plumbing is left dormant to revisit.) */}
          {active && (
            <span title="Aktywna sesja (tmux)" style={{
              position: 'absolute', bottom: -1, right: -1,
              width: 12, height: 12, borderRadius: 'var(--r-sm)',
              background: 'var(--ok)', border: '2px solid var(--surface-1)',
              boxSizing: 'content-box',
            }} />
          )}
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, justifyContent: 'space-between' }}>
            <span style={{
              fontSize: 17, fontWeight: 500,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{name}</span>
            <span className="mono" style={{
              fontSize: 'var(--t-2xs)', color: 'var(--fg-3)',
              textTransform: 'uppercase', letterSpacing: '0.08em', flexShrink: 0,
            }}>{kind}</span>
          </div>
          {sub && (
            <div className="mono" style={{
              marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}>{sub}</div>
          )}
          {error && (
            <div className="mono" style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--err)' }}>{error}</div>
          )}
        </div>
        {onNewSession && (
          <button onClick={startNew} title="Nowa sesja" aria-label="Nowa sesja"
            disabled={busy}
            style={{
              flexShrink: 0, background: 'transparent', border: 'none',
              cursor: busy ? 'progress' : 'pointer',
              padding: 8, borderRadius: 'var(--r-control)', color: 'var(--accent)',
            }}>
            <window.Icon name="plus" size={18} />
          </button>
        )}
      </div>
    </window.Card>
  );
}

function AgentsDirectoryView({ compact }) {
  const data = window.useHubData ? window.useHubData() : { areas: [], projects: [] };
  const areas = (data && data.areas) || [];
  const projects = (data && data.projects) || [];
  const router = window.useRouter ? window.useRouter() : null;

  // Force a filesystem re-scan whenever this directory mounts (= every time
  // the user opens the Agents view). This is the single reliable trigger for
  // surfacing a project the agent just created mid-chat without a manual
  // server refresh — ?fresh=1 bypasses the backend's 30s discovery cache.
  _agentsUseEffect(() => {
    if (data && typeof data.refreshHub === 'function') {
      try { data.refreshHub({ fresh: true }); } catch (_e) { /* non-fatal */ }
    }
    // run-once on mount — refreshHub is stable (useCallback []) so this won't
    // re-fire, and we deliberately don't want it to on every data change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // per-card busy state keyed by `cardKey` so multiple cards don't share a flag
  const [busyKey, setBusyKey] = _agentsUseState(null);
  const [errors, setErrors] = _agentsUseState({});
  // Map of agent-key → last-activity epoch. Built from one /api/orchestrator/sessions
  // call on mount so we can sort cards by recency. Keys: 'global' for the Global
  // agent, '<kind>/<lib_id>' (matching sidecar lib_id) for per-agent rows.
  const [activityByKey, setActivityByKey] = _agentsUseState({});
  // Set of agent keys ('global' / '<kind>/<lib_id>') that currently have a
  // warm tmux session, for the green "active session" dot. Polled so the
  // dots follow sessions warming up / cooling down.
  const [activeKeys, setActiveKeys] = _agentsUseState(() => new Set());

  _agentsUseEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(window.apiUrl('/api/orchestrator/sessions'));
        if (!r.ok) return;
        const sessions = await r.json();
        if (cancelled || !Array.isArray(sessions)) return;
        const acc = {};
        for (const s of sessions) {
          const ts = Number(s && s.updated_at) || 0;
          if (!ts) continue;
          const key = (s && s.lib_id) ? String(s.lib_id) : 'global';
          if (!acc[key] || ts > acc[key]) acc[key] = ts;
        }
        setActivityByKey(acc);
      } catch (_) { /* tolerate offline / 401 */ }
    })();
    return () => { cancelled = true; };
  }, []);

  _agentsUseEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch(window.apiUrl('/api/orchestrator/pool'));
        if (!r.ok) return;
        const data = await r.json();
        if (cancelled) return;
        const set = new Set();
        for (const s of (data && data.slots) || []) {
          set.add(s.lib_id ? String(s.lib_id) : 'global');
        }
        setActiveKeys(set);
      } catch (_) { /* tolerate offline / 401 */ }
    };
    load();
    const id = setInterval(load, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const navigate = (path) => {
    if (!path || !router || typeof router.push !== 'function') return;
    router.push(path);
  };

  // Create a brand-new session for `card` and navigate straight to it.
  // For per-agent kinds the new session inherits the agent's model + extra
  // prompt; Global has no profile and posts an empty payload (cwd=null).
  // Shared by openChat's "no session yet" fallback and the per-card "+"
  // button, which always forces a fresh session. Always lands on a concrete
  // /chat/<sid> URL so the chat surface opens exactly this session instead
  // of whatever localStorage last remembered.
  const createSession = async (card) => {
    const isGlobal = card.kind === 'Global';
    const cwd = isGlobal ? null
      : (card.kind === 'Area' ? `~/Areas/${card.libId}` : `~/Projects/${card.libId}`);
    let profile = {};
    if (!isGlobal) {
      try {
        const pr = await fetch(window.apiUrl(`/api/library/${card.kind === 'Area' ? 'areas' : 'projects'}/${card.libIdEncoded}/agent`));
        if (pr.ok) {
          const pj = await pr.json();
          profile = (pj && pj.agent) || {};
        }
      } catch (_) { /* tolerate; create with defaults */ }
    }
    const payload = isGlobal ? {} : {
      cwd,
      lib_id: `${card.kind === 'Area' ? 'areas' : 'projects'}/${card.libId}`,
    };
    if (profile.model) payload.model = profile.model;
    if (profile.system_prompt) payload.extra_system_prompt = profile.system_prompt;
    const cr = await fetch(window.apiUrl('/api/orchestrator/sessions'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const cj = await cr.json().catch(() => ({}));
    if (!cr.ok || !cj || !cj.id) {
      throw new Error((cj && (cj.error || cj.detail)) || `http ${cr.status}`);
    }
    navigate(window.buildPath({ section: 'orchestrator', sessionId: cj.id }));
  };

  // Open the agent's chat: most-recent session if one exists, else create a
  // fresh one. Global is treated symmetrically — its sessions just have
  // cwd=null (sentinel `__global__` in the filter).
  const openChat = async (card) => {
    if (busyKey) return;
    setBusyKey(card.key);
    setErrors((prev) => ({ ...prev, [card.key]: null }));
    try {
      const isGlobal = card.kind === 'Global';
      const cwd = isGlobal ? null
        : (card.kind === 'Area' ? `~/Areas/${card.libId}` : `~/Projects/${card.libId}`);
      const listUrl = isGlobal
        ? `/api/orchestrator/sessions?cwd=__global__`
        : `/api/orchestrator/sessions?cwd=${encodeURIComponent(cwd)}`;
      const listRes = await fetch(window.apiUrl(listUrl));
      if (listRes.ok) {
        const sessions = await listRes.json().catch(() => []);
        if (Array.isArray(sessions) && sessions.length > 0) {
          // Open the MOST RECENT session by last message (updated_at). The list
          // endpoint returns pinned-first, but "open the agent" should land on
          // the last one you actually used.
          let top = null;
          for (const s of sessions) {
            if (s && s.id && (!top || (Number(s.updated_at) || 0) > (Number(top.updated_at) || 0))) top = s;
          }
          if (top && top.id) {
            navigate(window.buildPath({ section: 'orchestrator', sessionId: top.id }));
            return;
          }
        }
      }
      await createSession(card);  // no session yet
    } catch (e) {
      setErrors((prev) => ({ ...prev, [card.key]: (e && e.message) || 'failed to open chat' }));
    } finally {
      setBusyKey(null);
    }
  };

  // The corner "+" always starts a brand-new session, skipping openChat's
  // reuse-most-recent path.
  const newSession = async (card) => {
    if (busyKey) return;
    setBusyKey(card.key);
    setErrors((prev) => ({ ...prev, [card.key]: null }));
    try {
      await createSession(card);
    } catch (e) {
      setErrors((prev) => ({ ...prev, [card.key]: (e && e.message) || 'nie udało się utworzyć sesji' }));
    } finally {
      setBusyKey(null);
    }
  };

  // Global pinned to the top. Per-agent cards sort by last-activity desc
  // (most recently used first), with alphabetical tiebreak when no session
  // exists yet for either side. `activityByKey` is the recency map built
  // from /api/orchestrator/sessions on mount; keys match `lib_id` exactly
  // so every per-agent card has a 1:1 lookup.
  const cards = _agentsUseMemo(() => {
    const encodeLibId = (id) => String(id || '').split('/').map(encodeURIComponent).join('/');
    const decorate = (item, kind) => {
      const libId = item.lib_id || item.name;
      const sidecarKind = kind === 'Area' ? 'areas' : 'projects';
      const activityKey = `${sidecarKind}/${libId}`;
      return {
        key: kind.toLowerCase() + ':' + libId,
        icon: item.icon || (kind === 'Area' ? '📂' : '📦'),
        name: item.label || item.name,
        sub: (kind === 'Area' ? '~/Areas/' : '~/Projects/') + libId + '/',
        kind,
        libId,
        libIdEncoded: encodeLibId(libId),
        lastActivity: activityByKey[activityKey] || 0,
        active: activeKeys.has(activityKey),
        unreadKey: activityKey,  // matches "<kind>/<lib_id>" in unreadByAgent map
      };
    };
    // Order: agents with a live (warm tmux) session first, each group
    // sorted by most-recent message; then non-active agents by recency.
    // (Global is sorted in with the rest under the same rule.)
    const sortAgents = (a, b) => {
      if (!!a.active !== !!b.active) return a.active ? -1 : 1;        // active first
      if (a.lastActivity !== b.lastActivity) return b.lastActivity - a.lastActivity;  // recent first
      const an = (a.name || '').toLowerCase();
      const bn = (b.name || '').toLowerCase();
      return an < bn ? -1 : an > bn ? 1 : 0;
    };
    const globalCard = {
      key: 'global',
      icon: '🤖',
      name: 'Global',
      sub: 'Default Claude session · cwd ~/',
      kind: 'Global',
      lastActivity: activityByKey['global'] || 0,
      active: activeKeys.has('global'),
      unreadKey: 'global',
    };
    return [
      globalCard,
      ...areas.map((a) => decorate(a, 'Area')),
      ...projects.map((p) => decorate(p, 'Project')),
    ].sort(sortAgents);
  }, [areas, projects, activityByKey, activeKeys]);

  const empty = cards.length === 1 && areas.length === 0 && projects.length === 0;


  return (
    <div style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <window.SectionHeader
        eyebrow="Agents"
        title="Agents"
        count={`${cards.length} available`}
      />
      <div style={{
        marginTop: 18, display: 'grid', gap: 14,
        // Multi-column tile grid on desktop (matches ProjectsView), single
        // column on mobile. NB: a nested minmax() as the MAX arg is invalid CSS
        // and silently drops the whole declaration → that's why this used to
        // render as one full-width column. `minmax(280px, 1fr)` is the valid form.
        gridTemplateColumns: compact ? 'minmax(0, 1fr)' : 'repeat(auto-fill, minmax(280px, 1fr))',
      }}>
        {cards.map((c) => (
          <_AgentCard
            key={c.key}
            icon={c.icon}
            name={c.name}
            sub={c.sub}
            kind={c.kind}
            busy={busyKey === c.key}
            error={errors[c.key]}
            active={activeKeys.has(c.unreadKey)}
            onOpenChat={() => openChat(c)}
            onNewSession={() => newSession(c)}
          />
        ))}
      </div>
      {empty && (
        <div style={{ marginTop: 24, color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>
          No areas or projects yet. The Global agent is always available — add directories under ~/Areas/ or ~/Projects/ to give them their own agent.
        </div>
      )}
    </div>
  );
}

Object.assign(window, { AgentsDirectoryView });
