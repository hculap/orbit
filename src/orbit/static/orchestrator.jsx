// orchestrator.jsx — Hero chat panel wired to /api/orchestrator/*.
// Talks to a real backend that runs `claude -p` server-side and streams
// stream-json back over SSE. Sessions persist via Claude's own JSONL store.

const { useState, useEffect, useReducer, useRef, useMemo, useCallback } = React;

// Helpers from sibling files (orchestrator-blocks, -attachments, -transcript)
// are published to window and used here as bare globals.

// Mirrors orchestrator_uploads.MAX_UPLOAD_BYTES — keep in sync.
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

function previewText(s, n = 120) {
  if (!s) return '';
  const t = String(s).replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

// Stable per-message key. Used both for chip-target lookup (replyingToKey)
// and for the auto-speak de-dupe set. Pure helper so it's safe to call from
// any callback regardless of declaration order.
function keyForMsg(msg, fallbackSessionId) {
  if (!msg) return null;
  const turn = msg.turn_idx != null && msg.turn_idx >= 0 ? msg.turn_idx : null;
  return (msg.session_id || fallbackSessionId || 'x') + ':' + (turn != null ? 't' + turn : 's' + (msg.ts || 0));
}

// Stitch a message's blocks into clipboard-ready text. Markdown/text blocks
// are stripped to plain text via window.stripMarkdownForCopy (provided by
// orchestrator-message-actions.jsx); code blocks are copied verbatim.
function blocksToCopyText(blocks) {
  if (!Array.isArray(blocks)) return '';
  const parts = [];
  for (const b of blocks) {
    if (!b) continue;
    if (b.kind === 'markdown' || b.kind === 'text') {
      const t = b.text != null ? b.text : (b.content || '');
      if (t) parts.push((typeof window !== 'undefined' && window.stripMarkdownForCopy) ? window.stripMarkdownForCopy(t) : t);
    } else if (b.kind === 'code') {
      const t = b.text != null ? b.text : (b.content || '');
      if (t) parts.push(t);
    }
  }
  return parts.join('\n\n').trim();
}

const SSE_KINDS = ['init', 'delta', 'tool_start', 'tool_use', 'tool_result', 'assistant_message', 'done', 'error', 'spawning'];

// Coerce a raw block to the {kind, ...} shape the transcript renders. The
// envelope pipeline is gone, so live turns only ever carry markdown/code/
// tool_* blocks; this stays minimal (markdown/code + passthrough) for the
// historical-hydration path and is published for any sibling that coerces
// a block.
function normalizeStructuredBlock(b) {
  if (!b || typeof b !== 'object') return null;
  const t = b.type || b.kind;
  if (t === 'markdown')   return { kind: 'markdown', text: b.content != null ? b.content : (b.text || '') };
  if (t === 'code')       return { kind: 'code', lang: b.lang || '', text: b.content != null ? b.content : (b.text || '') };
  if (t) return { ...b, kind: t };
  return null;
}

const INITIAL = {
  sessions: [], activeId: null, messages: [],
  streaming: null, isLoading: false, error: null,
  connectionStatus: 'live',
  // Last `runner_mode` echoed by POST /messages response (Phase 2B). Drives
  // the RunnerModeBadge in the header. Cleared on session switch so users
  // don't see a stale badge from a previous session's last POST.
  lastRunnerMode: null,
};

// `spawning` is set by the SPAWNING_EVENT case when the backend emits the
// pre-init event for interactive-mode cold starts (~10-20s). Cleared by
// INIT_EVENT so the skeleton dissolves automatically once claude is ready.
const makeStreaming = () => ({ partial_text: '', partial_blocks: [], started_at: Date.now(), init: null, spawning: null });

function orchestratorReducer(state, a) {
  const s = state, str = state.streaming;
  switch (a.type) {
    case 'LOAD_SESSIONS':       return { ...s, sessions: a.sessions };
    case 'SET_LOADING':         return { ...s, isLoading: !!a.loading };
    case 'SET_ERROR':           return { ...s, error: a.error || null };
    case 'SELECT_SESSION':      return { ...s, activeId: a.id, messages: [], streaming: null, error: null, lastRunnerMode: null };
    case 'SET_LAST_RUNNER_MODE': return { ...s, lastRunnerMode: a.runner_mode };
    case 'LOAD_MESSAGES':       return { ...s, messages: a.messages };
    case 'BEGIN_STREAM':        return { ...s, streaming: makeStreaming() };
    case 'SPAWNING_EVENT':      return str ? { ...s, streaming: { ...str, spawning: a.payload } } : s;
    case 'INIT_EVENT':          return str ? { ...s, streaming: { ...str, init: a.payload, spawning: null } } : s;
    case 'APPEND_DELTA':        return str ? { ...s, streaming: { ...str, partial_text: (str.partial_text || '') + (a.text || '') } } : s;
    case 'APPEND_BLOCK':        return str ? { ...s, streaming: { ...str, partial_blocks: [...str.partial_blocks, a.block] } } : s;
    case 'UPDATE_BLOCK':        return str
      ? { ...s, streaming: { ...str, partial_blocks: str.partial_blocks.map(b => a.match(b) ? { ...b, ...a.patch } : b) } }
      : s;
    case 'FINALIZE_TURN': {
      // Runner emits MULTIPLE finalize events per logical user query when
      // tool-use is involved: each Anthropic API turn (thinking call →
      // tool_call → prose) is forwarded with an incremented `turn_idx`
      // even though the user issued a single query. Empirical battery
      // 2026-05-04 observed turn_idx [0, 1, 2] for one weather query.
      //
      // Merge into the LAST message whenever role matches — regardless of
      // turn_idx. The intervening APPEND_USER_MESSAGE for the next user
      // turn breaks the same-role chain, so subsequent user queries get
      // fresh bubbles and there's no risk of merging across queries.
      // Block-level dedup (kind+content) prevents the prose from being
      // appended twice when both `assistant_message` and `structured_blocks`
      // happen to carry overlapping content.
      if (!a.message) return { ...s, streaming: str ? makeStreaming() : null };
      const newMsg = a.message;
      const last = s.messages.length ? s.messages[s.messages.length - 1] : null;
      const mergeable = !!(last && last.role === newMsg.role);
      if (!mergeable) {
        return {
          ...s,
          messages: [...s.messages, newMsg],
          streaming: str ? makeStreaming() : null,
        };
      }
      const seen = new Set();
      // Identify a block by kind + the most-distinguishing of its fields.
      // Falling back through text/content/tool_use_id/path/url/video_id/
      // data prevents two distinct media blocks of the same kind from
      // collapsing to a single dedup key (which would silently drop one).
      const keyOf = (b) => b && (b.kind + ':' + (
        b.text || b.content || b.tool_use_id
        || b.path || b.url || b.video_id || b.html
        || (b.data && JSON.stringify(b.data))
        || (b.center && JSON.stringify(b.center))
        || ''
      ));
      const mergedBlocks = [];
      for (const b of [...(last.blocks || []), ...(newMsg.blocks || [])]) {
        const k = keyOf(b);
        if (k && seen.has(k)) continue;
        if (k) seen.add(k);
        mergedBlocks.push(b);
      }
      return {
        ...s,
        messages: [
          ...s.messages.slice(0, -1),
          { ...last, blocks: mergedBlocks, ts: newMsg.ts || last.ts },
        ],
        streaming: str ? makeStreaming() : null,
      };
    }
    case 'APPEND_USER_MESSAGE': return { ...s, messages: [...s.messages, a.message] };
    case 'STREAM_ERROR': {
      // Always escalate the error block into messages, not into
      // streaming.partial_blocks. The stream handler dispatches
      // CLEAR_STREAMING right after STREAM_ERROR (the 'error' event is
      // terminal), which would wipe streaming.partial_blocks before any
      // renderer ever saw the errBlock. Result was: regular chat showed
      // nothing for an Anthropic 'prompt too long' API error, and the
      // conversation modal blamed it on a generic context-limit watchdog.
      // Pushing into messages makes the error visible AND lets the
      // conversation hook's speaking effect dispatch a cleaner error
      // surface (it'll find the error-only message, _speechTextFor
      // returns empty, the watchdog still fires its message but the
      // user has the actual API error rendered in the chat).
      const errBlock = { kind: 'error', text: a.text || 'stream error' };
      const errMsg = { role: 'assistant', ts: Date.now() / 1000, turn_idx: -1, blocks: [errBlock] };
      return { ...s, messages: [...s.messages, errMsg], connectionStatus: 'error' };
    }
    case 'STREAM_LIVE':         return { ...s, connectionStatus: 'live', error: null };
    case 'STREAM_RETRYING':     return { ...s, connectionStatus: 'retrying' };
    case 'STREAM_POLLING':      return { ...s, connectionStatus: 'polling' };
    case 'CLEAR_STREAMING':     return { ...s, streaming: null };
    default:                    return s;
  }
}

function useOrchestratorReducer() { return useReducer(orchestratorReducer, INITIAL); }

async function apiGet(path) {
  const r = await fetch(apiUrl(path));
  if (!r.ok) throw new Error('GET ' + path + ' → ' + r.status);
  return r.json();
}
async function apiSend(path, method, body) {
  const r = await fetch(apiUrl(path), {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body == null ? undefined : JSON.stringify(body),
  });
  let d = null;
  try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) throw new Error((d && d.error) || (method + ' ' + path + ' → ' + r.status));
  return d || {};
}

// Per-session interactive-mode preference. Persisted client-side via
// localStorage because the backend already accepts `interactive_mode` as a
// per-request POST field (`orchestrator.py:_post_message_handler`), so we
// don't need a new sidecar PATCH endpoint. Tri-state:
//   * null / missing → follow global runner_mode (no override sent)
//   * 'on'           → force interactive (POST body adds interactive_mode: true)
//   * 'off'          → force programmatic (POST body adds interactive_mode: false)
function _interactivePrefKey(sid) { return 'interactive-mode:' + sid; }
function readInteractivePref(sid) {
  if (!sid) return null;
  try {
    const v = localStorage.getItem(_interactivePrefKey(sid));
    return v === 'on' || v === 'off' ? v : null;
  } catch (e) { return null; }
}
function writeInteractivePref(sid, pref) {
  if (!sid) return;
  try {
    const key = _interactivePrefKey(sid);
    if (pref == null) localStorage.removeItem(key);
    else localStorage.setItem(key, pref);
  } catch (e) { /* ignore */ }
}
function useInteractivePref(sessionId) {
  const [pref, setPref] = useState(() => readInteractivePref(sessionId));
  useEffect(() => { setPref(readInteractivePref(sessionId)); }, [sessionId]);
  const update = useCallback((next) => {
    writeInteractivePref(sessionId, next);
    setPref(next);
  }, [sessionId]);
  return [pref, update];
}

// Small read-only badge in the header reflecting the runner mode the BACKEND
// actually picked for the last POST. Helps the user distinguish whether the
// turn is hitting subscription (interactive) or API billing (programmatic).
// Hidden when no POST has happened yet for the active session.
function RunnerModeBadge({ runnerMode }) {
  if (runnerMode !== 'interactive' && runnerMode !== 'programmatic') return null;
  const isInteractive = runnerMode === 'interactive';
  return (
    <span
      className="mono"
      title={isInteractive
        ? 'Backend routed this turn to a long-lived tmux session under your subscription.'
        : 'Backend used `claude -p` per-turn — billed against API/programmatic credit.'}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        padding: '2px 8px', borderRadius: 'var(--r-pill)',
        fontSize: 'var(--t-2xs)', textTransform: 'uppercase', letterSpacing: '0.06em',
        background: 'var(--surface-1)',
        border: '1px solid var(--hairline)',
        color: isInteractive ? 'var(--accent)' : 'var(--fg-3)',
        whiteSpace: 'nowrap',
      }}
    >
      {isInteractive ? 'interactive · subscription' : 'programmatic · api'}
    </span>
  );
}

function InteractiveModeToggle({ sessionId, compact }) {
  const [pref, setPref] = useInteractivePref(sessionId);
  if (!sessionId) return null;
  // Cycle: null → on → off → null. Three discrete states so a user can
  // override the global default in EITHER direction.
  const onClick = () => {
    const next = pref == null ? 'on' : pref === 'on' ? 'off' : null;
    setPref(next);
  };
  const label = pref === 'on'
    ? 'Interactive: ON (override)'
    : pref === 'off'
      ? 'Interactive: OFF (override)'
      : 'Interactive: follow default';
  const color = pref === 'on' ? 'var(--accent)'
              : pref === 'off' ? 'var(--fg-3)'
              : 'var(--fg-2)';
  return (
    <IconButton
      icon="terminal"
      label={label}
      size={compact ? 36 : 32}
      onClick={onClick}
      style={{ color, opacity: pref == null ? 0.55 : 1 }}
    />
  );
}

function _PinTrigger({ compact, activeSession, pinnedTurnIdxs, pinPopoverOpen, setPinPopoverOpen, messages, msgKey, togglePin }) {
  const pinAnchorRef = useRef(null);
  const pinCount = pinnedTurnIdxs ? pinnedTurnIdxs.size : 0;
  if (pinCount === 0) return null;
  return (
    <div ref={pinAnchorRef} style={{ position: 'relative', display: 'inline-flex' }}>
      <IconButton icon="pin-fill"
        label={pinPopoverOpen ? 'Schowaj przypięte' : 'Pokaż przypięte (' + pinCount + ')'}
        size={32}
        onClick={() => setPinPopoverOpen && setPinPopoverOpen(!pinPopoverOpen)}
        style={{ color: 'var(--accent)' }} />
      <span aria-hidden="true" style={{
        position: 'absolute', top: 2, right: 2,
        minWidth: 14, height: 14, padding: '0 3px',
        borderRadius: 7, background: 'var(--accent)', color: 'var(--bg)',
        fontSize: 9, fontWeight: 700, lineHeight: '14px', textAlign: 'center',
        pointerEvents: 'none',
      }}>{pinCount}</span>
      {window.PinPopover && (
        <window.PinPopover
          open={!!pinPopoverOpen}
          onClose={() => setPinPopoverOpen && setPinPopoverOpen(false)}
          anchorRef={pinAnchorRef}
          activeSession={activeSession}
          messages={messages}
          msgKey={msgKey}
          togglePin={togglePin}
          compact={compact}
        />
      )}
    </div>
  );
}

// Compact branch — Sessions / Plan / Todos / Model / etc all collapse into the
// kebab. The header itself only carries the pin badge (with count, kept inline
// because the count is meaningful at-a-glance) and the kebab. Modal sheets
// (Plan / Todos / Model) are rendered as siblings, driven by `activeModal`.
function HeaderActionsCompact(props) {
  const {
    onRename, onCompact, onClear, onCloseSession, onTogglePin, onTogglePersistent, onOpenSessions, onNew,
    activeSession, compacting,
    pinnedTurnIdxs, pinPopoverOpen, setPinPopoverOpen, messages, msgKey, togglePin,
    sessionId, onModelChange,
    onOpenConversation, conversationSupported,
    viewMode, onToggleViewMode, onShowGallery, onOpenIssues,
    onOpenDrawer,
  } = props;
  const pinned = !!(activeSession && activeSession.pinned);
  const persistent = !!(activeSession && activeSession.persistent);
  const [activeModal, setActiveModal] = useState(null); // 'model' | 'plan' | 'todos' | 'terminal' | null
  // Single useSessionState instance so the kebab labels (todos count, plan
  // existence) and the sheets share one fetch.
  const sess = window.useSessionState ? window.useSessionState({ sessionId }) : { state: null, loading: false, error: null, reload: () => {} };
  const sessState = sess.state;
  const todos = (sessState && Array.isArray(sessState.todos)) ? sessState.todos : [];
  const activeTodos = todos.filter(t => t.status !== 'completed').length;
  const hasPlan = !!(sessState && sessState.plan);

  // Refetch /state when the user opens a state-driven sheet so they see the
  // freshest data even if claude updated todos / plan since the last fetch.
  useEffect(() => {
    if (activeModal === 'plan' || activeModal === 'todos') {
      try { sess.reload(); } catch (e) { /* ignore */ }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeModal]);

  const closeModal = () => setActiveModal(null);
  const todosLabel = activeTodos > 0 ? ('Todos (' + activeTodos + ')') : 'Todos';
  // Model picker + interactive-mode toggle removed from the menu: the
  // model is set inside the tmux REPL (/model) and interactive is the
  // default runner, so both are obsolete.

  // SSH host published by the backend in /api/data → window.HUB_INITIAL_DATA
  // when `terminal.ssh_host` is set in override.yaml. When missing, the
  // "Copy terminal command" kebab item simply doesn't render so the menu
  // stays clean for users who haven't configured a host.
  const sshHost = (window.HUB_INITIAL_DATA && window.HUB_INITIAL_DATA.terminal && window.HUB_INITIAL_DATA.terminal.ssh_host) || '';
  const _kebabToast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;
  const copyTerminalCmd = useCallback(() => {
    if (!sshHost || !sessionId) return;
    // `tmux -L hd-orch` mirrors TMUX_SOCKET from orchestrator_tmux.py and
    // session name is `hd-<sid>` (see TmuxPool._slot_for). `-t` allocates
    // a PTY on the remote so the attach actually shows the live pane.
    const cmd = `ssh ${sshHost} -t 'tmux -L hd-orch attach -t hd-${sessionId}'`;
    window.HubClipboard.copyText(cmd).then((ok) => {
      _kebabToast && _kebabToast(
        ok ? 'Skopiowano — wklej w terminalu' : 'Skopiuj ręcznie: ' + cmd,
        ok ? 'ok' : 'warn',
      );
    });
  }, [sshHost, sessionId, _kebabToast]);

  // UI-only removal (per request): "Skopiuj komendę terminala", the
  // chat↔terminal toggle ("Wróć do chatu"/"Pokaż terminal") and "Compact" no
  // longer appear in the menu. Their handlers (copyTerminalCmd /
  // onToggleViewMode / onCompact) stay wired so the buttons can be restored
  // without backend changes. "Tryb rozmowy" is BACK (ttyd-native rebuild).
  const kebabItems = [
    activeSession && { icon: pinned ? 'star-fill' : 'star', label: pinned ? 'Unpin' : 'Pin', onClick: onTogglePin },
    activeSession && typeof onTogglePersistent === 'function' && {
      icon: persistent ? 'pin-fill' : 'pin',
      label: persistent ? 'Keep-alive: on — sesja nie wygasa' : 'Keep-alive — nie zabijaj po idle',
      onClick: onTogglePersistent,
    },
    sessionId && conversationSupported && typeof onOpenConversation === 'function' && {
      icon: 'mic', label: 'Tryb rozmowy (głos)', onClick: onOpenConversation,
    },
    activeSession && { icon: 'pencil', label: 'Rename', onClick: onRename },
    typeof onOpenIssues === 'function' && {
      icon: 'circle', label: 'Issues projektu / area', onClick: onOpenIssues,
    },
    sessionId && typeof onShowGallery === 'function' && {
      icon: 'image',
      label: viewMode === 'gallery' ? 'Zamknij galerię' : 'Galeria artefaktów',
      onClick: onShowGallery,
    },
    hasPlan && { icon: 'notepad', label: 'Plan', onClick: () => setActiveModal('plan') },
    todos.length > 0 && { icon: 'list-checks', label: todosLabel, onClick: () => setActiveModal('todos') },
    activeSession && typeof onCloseSession === 'function' && {
      icon: 'close', label: 'Zamknij sesję — zwolnij slot', onClick: onCloseSession,
      // Keep-alive sessions are protected (mirror SessionContextMenu).
      disabled: persistent, hint: persistent ? 'Najpierw wyłącz keep-alive' : undefined,
    },
    activeSession && {
      icon: 'trash', label: 'Delete', onClick: onClear, danger: true,
      disabled: persistent, hint: persistent ? 'Najpierw wyłącz keep-alive' : undefined,
    },
  ];

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
      <_PinTrigger compact={true} activeSession={activeSession}
        pinnedTurnIdxs={pinnedTurnIdxs} pinPopoverOpen={pinPopoverOpen}
        setPinPopoverOpen={setPinPopoverOpen} messages={messages}
        msgKey={msgKey} togglePin={togglePin} />
      {typeof onNew === 'function' && (
        <IconButton icon="plus" label="Nowa sesja" size={36} onClick={onNew} />
      )}
      <IconButton icon="logs" label="Sesje" size={36} onClick={onOpenSessions} />
      <KebabMenu items={kebabItems} />
      {window.PlanViewer && (
        <window.PlanViewer open={activeModal === 'plan'} onClose={closeModal}
          anchorRef={null} compact={true}
          state={sessState} loading={sess.loading} error={sess.error} />
      )}
      {window.TodosViewer && (
        <window.TodosViewer open={activeModal === 'todos'} onClose={closeModal}
          anchorRef={null} compact={true}
          state={sessState} loading={sess.loading} error={sess.error} />
      )}
    </div>
  );
}

function HeaderActionsDesktop(props) {
  const {
    onRename, onCompact, onClear, onCloseSession, onTogglePin, onTogglePersistent, onTogglePanel,
    activeSession, panelOpen, compacting,
    pinnedTurnIdxs, pinPopoverOpen, setPinPopoverOpen, messages, msgKey, togglePin,
    sessionId, onModelChange,
    onOpenConversation, conversationSupported,
    viewMode, onToggleViewMode, onShowGallery, onOpenIssues,
  } = props;
  const pinned = !!(activeSession && activeSession.pinned);
  const persistent = !!(activeSession && activeSession.persistent);
  const terminalActive = viewMode === 'terminal';
  // SSH host from override.yaml (passed via /api/data → HUB_INITIAL_DATA).
  // Same convention as HeaderActionsCompact: hide the button when no host
  // is configured so non-self-hosted setups don't see a useless action.
  const _sshHost = (window.HUB_INITIAL_DATA && window.HUB_INITIAL_DATA.terminal && window.HUB_INITIAL_DATA.terminal.ssh_host) || '';
  const _toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;
  const _copySshCmd = () => {
    if (!_sshHost || !sessionId) return;
    const cmd = `ssh ${_sshHost} -t 'tmux -L hd-orch attach -t hd-${sessionId}'`;
    window.HubClipboard.copyText(cmd).then((ok) => {
      _toast && _toast(
        ok ? 'Skopiowano — wklej w terminalu' : 'Skopiuj ręcznie: ' + cmd,
        ok ? 'ok' : 'warn',
      );
    });
  };
  const stateButtons = (sessionId && window.StateModalButtons)
    ? <window.StateModalButtons compact={false} sessionId={sessionId} />
    : null;
  // Model picker + interactive-mode toggle removed: the model is now set
  // inside the tmux REPL (/model) and interactive is the default runner,
  // so both header controls are obsolete.
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
      <_PinTrigger compact={false} activeSession={activeSession}
        pinnedTurnIdxs={pinnedTurnIdxs} pinPopoverOpen={pinPopoverOpen}
        setPinPopoverOpen={setPinPopoverOpen} messages={messages}
        msgKey={msgKey} togglePin={togglePin} />
      {stateButtons}
      {/* UI-only removal (per request): "Tryb rozmowy" (headphones),
          "Compact session" (archive), "Skopiuj ssh" (copy), and the
          chat↔terminal toggle ("Wróć do chatu", terminal icon) no longer
          render here. Handlers (onOpenConversation / onCompact / _copySshCmd
          / onToggleViewMode) stay wired so they can be restored. Terminal is
          the default surface, so dropping the toggle keeps sessions on it. */}
      {/* Per-session lifecycle actions (Pin / Keep-alive / Rename / Close /
          Delete) moved OUT of the header — they now live in the right-click
          context menu on each session row in the side list. Handlers
          (onTogglePin / onTogglePersistent / onRename / onCloseSession /
          onClear) stay wired through props for the context menu + compact
          (mobile) header. */}
      {typeof onOpenIssues === 'function' && <IconButton icon="circle" label="Issues projektu / area" size={32} onClick={onOpenIssues} />}
      {sessionId && typeof onShowGallery === 'function' && (
        <IconButton
          icon="image"
          label={viewMode === 'gallery' ? 'Zamknij galerię' : 'Galeria artefaktów'}
          size={32}
          onClick={onShowGallery}
          style={{ color: viewMode === 'gallery' ? 'var(--accent)' : 'var(--fg-2)' }} />
      )}
      <IconButton icon="panel-r" label={panelOpen ? 'Hide sessions' : 'Show sessions'} size={32} onClick={onTogglePanel} style={{ color: panelOpen ? 'var(--accent)' : 'var(--fg-2)' }} />
    </div>
  );
}

function HeaderActions(props) {
  return props.compact ? <HeaderActionsCompact {...props} /> : <HeaderActionsDesktop {...props} />;
}

// QueueStrip — list of queued user messages rendered between reply-to bar and
// composer. Each item shows a one-line preview + attachment count, with edit
// (pull back into composer) and delete buttons. Hidden when the queue is
// empty.
function _queuePreview(item) {
  const t = (item && item.text) ? item.text : '';
  const flat = String(t).replace(/\s+/g, ' ').trim();
  if (flat.length <= 90) return flat;
  return flat.slice(0, 89) + '…';
}

function QueueStrip({ items, compact, onEdit, onRemove }) {
  if (!items || items.length === 0) return null;
  return (
    <div style={{
      margin: compact ? '0 14px 6px' : '0 24px 6px',
      display: 'flex', flexDirection: 'column', gap: 4,
    }}>
      <div className="mono" style={{
        fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase',
        letterSpacing: '0.08em', padding: '0 4px',
      }}>
        Kolejka ({items.length})
      </div>
      {items.map((item, i) => {
        const preview = _queuePreview(item) || '(brak treści)';
        const att = (item.attachments && item.attachments.length) || 0;
        return (
          <div key={item.id} style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '6px 10px',
            background: 'var(--surface-2)', border: '1px dashed var(--hairline-strong)',
            borderRadius: 'var(--r-control)', fontSize: 'var(--t-cap)', color: 'var(--fg-2)', minWidth: 0,
          }}>
            <span className="mono" style={{
              fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', flexShrink: 0,
            }}>#{i + 1}</span>
            <span style={{
              flex: 1, minWidth: 0, overflow: 'hidden',
              textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{preview}</span>
            {att > 0 && (
              <span className="mono" style={{
                fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', flexShrink: 0,
              }}>{att} plik{att === 1 ? '' : 'i'}</span>
            )}
            <IconButton icon="pencil" size={26}
              label="Edytuj — wczytaj z powrotem do inputa"
              onClick={() => onEdit && onEdit(item.id)} />
            <IconButton icon="close" size={26}
              label="Usuń z kolejki"
              onClick={() => onRemove && onRemove(item.id)} />
          </div>
        );
      })}
    </div>
  );
}

// poolStatus: 'hot' = warm slot kept alive (green) · 'cooling' = warm but
// outside the hot ring, scheduled for eviction (yellow) · null = cold/closed
// (no dot). Tells the user at a glance which sessions are live and which will
// be reaped.
function SessionItem({ s, active, onSelect, poolStatus, thinking, onContextMenu }) {
  return (
    <div onClick={() => onSelect(s.id)}
      onContextMenu={onContextMenu}
      style={{
        padding: '10px 12px', marginBottom: 4, borderRadius: 'var(--r-control)',
        border: '1px solid ' + (active ? 'var(--accent-line)' : 'transparent'),
        background: active ? 'var(--accent-soft)' : 'transparent',
        cursor: 'pointer', transition: 'background .12s',
      }}
      onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'var(--surface-2)'; }}
      onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent'; }}
    >
      <div style={{
        fontSize: 'var(--t-sm)', fontWeight: 500, color: active ? 'var(--accent)' : 'var(--fg)',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        display: 'flex', alignItems: 'center', gap: 6,
      }}>
        {(() => {
          // Leading indicator carries the warm-pool status. A PINNED session
          // shows a star tinted by that status (green=hot, yellow=cooling,
          // accent=cold) — the star replaces the dot, never both. An unpinned
          // session shows a plain status dot (nothing when cold).
          // Agent is thinking (in-flight turn) → a blinking accent dot takes
          // over the leading indicator until the turn finishes. Wins over the
          // pool dot / pin star so "it's working" is always visible.
          if (thinking) {
            return <span className="hub-think-dot" title="Tura w toku" />;
          }
          const poolTitle = poolStatus === 'hot'
            ? 'Aktywna w puli — utrzymywana (hot)'
            : poolStatus === 'cooling'
              ? 'Otwarta, poza pulą — zostanie zamknięta'
              : undefined;
          if (s.pinned) {
            const starColor = poolStatus === 'hot' ? 'var(--ok)'
              : poolStatus === 'cooling' ? 'var(--warn)' : 'var(--accent)';
            return (
              <span title={poolTitle} style={{ display: 'inline-flex', flexShrink: 0 }}>
                <Icon name="star-fill" size={12} color={starColor} />
              </span>
            );
          }
          if (poolStatus) {
            return (
              <span title={poolTitle} style={{ display: 'inline-flex', flexShrink: 0 }}>
                <StatusDot status={poolStatus === 'hot' ? 'ok' : 'warn'} size={8} />
              </span>
            );
          }
          return null;
        })()}
        <span style={{
          flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
        }}>{s.title || 'untitled session'}</span>
        {/* keep-alive marker: pink pin pinned to the RIGHT edge, distinct from
            the green/yellow pool-status colours on the left. */}
        {s.persistent && (
          <span title="Keep-alive — sesja utrzymywana na stałe"
            style={{ display: 'inline-flex', flexShrink: 0, marginLeft: 4 }}>
            <Icon name="pin-fill" size={11} color="var(--cosmic-magenta)" />
          </span>
        )}
      </div>
      <div className="mono" style={{
        fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2,
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
      }}>
        {(s.msg_count || 0) + ' msg'}
        {(() => {
          const calc = window.computeContextUsage;
          const u = calc ? calc(s.last_context_tokens, s.last_model) : null;
          if (!u) return null;
          return (
            <>
              <span style={{ color: 'var(--fg-4)' }}> · </span>
              <span title={u.title} style={{ color: u.color }}>{u.pct}%</span>
            </>
          );
        })()}
        {' · ' + relTime(s.updated_at)}
        {s.git_branch && (
          <>
            <span style={{ color: 'var(--fg-4)' }}> · </span>
            <span title={'branch: ' + s.git_branch}>{s.git_branch}</span>
          </>
        )}
      </div>
      {s.last_user_preview && (
        <div style={{
          fontSize: 'var(--t-xs)', color: 'var(--fg-4)', marginTop: 4,
          display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
        }}>{previewText(s.last_user_preview, 90)}</div>
      )}
    </div>
  );
}

// Memoised MiniSearch index, keyed on a content signature so periodic
// refreshes with no real changes don't trigger a rebuild. The heavy
// per-session ``corpus`` field is fetched lazily by SessionList via
// /api/orchestrator/sessions/corpora and passed in here as
// ``corpusById`` — that way the session list payload itself stays small
// and the index is only built once the search box is opened.
function useSessionsIndex(sessions, corpusById) {
  const indexKey = useMemo(() => {
    if (!Array.isArray(sessions) || sessions.length === 0) return '';
    let maxTs = 0;
    const ids = [];
    for (const s of sessions) {
      if (s.updated_at > maxTs) maxTs = s.updated_at;
      ids.push(s.id);
    }
    const corporaSize = corpusById ? Object.keys(corpusById).length : 0;
    return sessions.length + ':' + maxTs + ':' + corporaSize + ':' + ids.join(',');
  }, [sessions, corpusById]);
  return useMemo(() => {
    const MS = window.MiniSearch;
    if (!MS || !indexKey) return null;
    const idx = new MS({
      idField: 'id',
      fields: ['title', 'last_user_preview', 'corpus'],
      storeFields: ['id'],
      searchOptions: {
        boost: { title: 4, last_user_preview: 2, corpus: 1 },
        fuzzy: 0.2, prefix: true, combineWith: 'AND',
      },
    });
    idx.addAll(sessions.map(s => ({
      id: s.id,
      title: s.title || '',
      last_user_preview: s.last_user_preview || '',
      corpus: (corpusById && corpusById[s.id]) || s.corpus || '',
    })));
    return idx;
  // sessions / corpusById are intentionally pulled in via closure, but the
  // memo only recomputes when the content signature actually changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indexKey]);
}

// Desktop right-click menu for a session row. Carries the session-lifecycle
// actions that used to be icon buttons in the chat header (pin / keep-alive /
// rename / close / delete). Positioned at the cursor, clamped to the viewport,
// dismisses on outside-click, Escape, scroll, or resize.
function SessionContextMenu({ session, x, y, onClose, actions }) {
  const ref = useRef(null);
  const [pos, setPos] = useState({ left: x, top: y });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const pad = 8;
    let left = x;
    let top = y;
    if (left + r.width + pad > window.innerWidth) left = Math.max(pad, window.innerWidth - r.width - pad);
    if (top + r.height + pad > window.innerHeight) top = Math.max(pad, window.innerHeight - r.height - pad);
    setPos({ left, top });
  }, [x, y]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    const onScroll = () => onClose();
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onScroll, true);
    // capture-phase scroll so scrolling the session list also dismisses.
    window.addEventListener('scroll', onScroll, true);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      window.removeEventListener('resize', onScroll, true);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [onClose]);

  const run = (fn) => () => { onClose(); if (typeof fn === 'function') fn(session); };
  const pinned = !!session.pinned;
  const persistent = !!session.persistent;
  const items = [
    typeof actions.onTogglePinSession === 'function' && {
      icon: pinned ? 'star-fill' : 'star', label: pinned ? 'Odepnij' : 'Przypnij',
      onClick: run(actions.onTogglePinSession),
    },
    typeof actions.onTogglePersistentSession === 'function' && {
      icon: persistent ? 'pin-fill' : 'pin',
      label: persistent ? 'Keep-alive: wyłącz' : 'Keep-alive: włącz',
      onClick: run(actions.onTogglePersistentSession),
    },
    typeof actions.onRenameSession === 'function' && {
      icon: 'pencil', label: 'Zmień nazwę', onClick: run(actions.onRenameSession),
    },
    typeof actions.onCloseSession === 'function' && {
      icon: 'close', label: 'Zamknij sesję', onClick: run(actions.onCloseSession),
      // Keep-alive sessions are protected — close/delete is disabled until the
      // user turns keep-alive off (the toggle above stays enabled).
      disabled: persistent, hint: persistent ? 'Najpierw wyłącz keep-alive' : undefined,
    },
    typeof actions.onDeleteSession === 'function' && {
      icon: 'trash', label: 'Usuń', danger: true, onClick: run(actions.onDeleteSession),
      disabled: persistent, hint: persistent ? 'Najpierw wyłącz keep-alive' : undefined,
    },
  ].filter(Boolean);

  return (
    <>
      {/* full-screen invisible backdrop swallows the dismissing click */}
      <div
        onClick={onClose}
        onContextMenu={(e) => { e.preventDefault(); onClose(); }}
        style={{ position: 'fixed', inset: 0, zIndex: 9998 }}
      />
      <div
        ref={ref}
        role="menu"
        style={{
          position: 'fixed', left: pos.left, top: pos.top, zIndex: 9999,
          minWidth: 184, padding: 4,
          background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
          borderRadius: 'var(--r-control)', boxShadow: 'var(--shadow-2, 0 8px 28px rgba(0,0,0,.4))',
        }}
      >
        {items.map((it, i) => (
          <button
            key={i} role="menuitem" onClick={it.onClick} disabled={it.disabled}
            title={it.hint || undefined}
            style={{
              display: 'flex', alignItems: 'center', gap: 10, width: '100%',
              padding: '8px 10px', border: 'none', background: 'transparent',
              borderRadius: 'var(--r-control)', cursor: it.disabled ? 'default' : 'pointer',
              fontFamily: 'inherit', fontSize: 'var(--t-sm)', textAlign: 'left',
              opacity: it.disabled ? 0.4 : 1,
              color: it.danger ? 'var(--danger, #f87171)' : 'var(--fg)',
            }}
            onMouseEnter={e => { if (!it.disabled) e.currentTarget.style.background = 'var(--surface-2)'; }}
            onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}
          >
            <Icon name={it.icon} size={14} color={it.danger ? 'var(--danger, #f87171)' : 'var(--fg-2)'} />
            <span>{it.label}</span>
          </button>
        ))}
      </div>
    </>
  );
}

// Horizontal "show more" divider that folds away stale sessions (older than a
// week, not open / pinned / active — see window.HubSessionListOrder.partition).
// Click toggles the hidden tail. A plain line with a centered, tappable pill.
function SessionFoldDivider({ count, expanded, onToggle }) {
  const [hover, setHover] = useState(false);
  const tint = hover ? 'var(--accent)' : 'var(--fg-3)';
  return (
    <div
      role="button" tabIndex={0}
      aria-expanded={expanded}
      onClick={onToggle}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(); } }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 10,
        // minHeight pins the tap target at the 44px mobile minimum regardless of
        // font line-height (the label is only ~11px) — same guaranteed-target
        // pattern as settings-view.jsx. Negative margin keeps the visual gap to
        // neighbouring rows close to the old 12px.
        minHeight: 44, margin: '-2px 6px', padding: '4px 0',
        cursor: 'pointer', userSelect: 'none',
      }}
    >
      <div style={{ flex: 1, height: 1, background: 'var(--hairline)' }} />
      <span style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        fontSize: 'var(--t-xs)', fontWeight: 500, color: tint,
        whiteSpace: 'nowrap', transition: 'color .12s', flexShrink: 0,
      }}>
        <span style={{
          display: 'inline-flex',
          transform: expanded ? 'rotate(180deg)' : 'none',
          transition: 'transform .15s',
        }}>
          <Icon name="chevron-d" size={12} color={tint} />
        </span>
        {expanded ? 'Pokaż mniej' : ('Pokaż starsze (' + count + ')')}
      </span>
      <div style={{ flex: 1, height: 1, background: 'var(--hairline)' }} />
    </div>
  );
}

function SessionList({
  sessions, activeId, onSelect, onNew, onClose, compact, poolStatusById, runningById,
  onRenameSession, onDeleteSession, onCloseSession,
  onTogglePinSession, onTogglePersistentSession,
}) {
  // Right-click context menu state: { session, x, y } or null. Desktop only —
  // the menu replaces the per-session action icons that used to live in the
  // chat header.
  const [ctxMenu, setCtxMenu] = useState(null);
  const rowActions = {
    onRenameSession, onDeleteSession, onCloseSession,
    onTogglePinSession, onTogglePersistentSession,
  };
  const hasRowActions = Object.values(rowActions).some(fn => typeof fn === 'function');
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  // Corpora are fetched lazily on first search-open and reused across
  // re-renders. The dashboard list payload itself no longer carries the
  // corpus field (would be ~hundreds of KB on a 50-session box).
  const [corpusById, setCorpusById] = useState(null);
  const [corpusLoading, setCorpusLoading] = useState(false);
  // Whether the folded "older than a week" tail is expanded. Reset whenever the
  // session SET changes (e.g. switching agent scope) so a new list starts
  // collapsed — see the membershipKey effect below.
  const [showOld, setShowOld] = useState(false);
  const searchRef = useRef(null);
  const live = useMemo(() => (sessions || []).filter(s => !s.archived), [sessions]);
  // The MiniSearch index is keyed (among other things) on the loaded
  // corpora map size — initially null/empty, then rebuilt once corpora
  // arrive. While loading the index falls back to title/preview only.
  const index = useSessionsIndex(live, corpusById);
  // On mobile (compact) the search input is always visible — the BottomSheet
  // owns the close affordance, and × on the input only clears the query.
  const searchVisible = compact || searchOpen;
  const filtered = useMemo(() => {
    const q = query.trim();
    if (!q) return live;
    if (!index) {
      const ql = q.toLowerCase();
      return live.filter(s => (s.title || '').toLowerCase().includes(ql)
        || (s.last_user_preview || '').toLowerCase().includes(ql)
        || (corpusById && corpusById[s.id] || '').toLowerCase().includes(ql));
    }
    const hits = index.search(q);
    const order = new Map(hits.map((h, i) => [h.id, i]));
    return live
      .filter(s => order.has(s.id))
      .sort((a, b) => order.get(a.id) - order.get(b.id));
  }, [live, index, query, corpusById]);

  // Searching shows the relevance-ranked hits flat (no fold). Otherwise order
  // the list (open tmux → pinned → recency) and fold the stale tail behind the
  // "show more" divider. window.HubSessionListOrder loads before this file; the
  // guard is dead-code defence (mirrors the window.* fallbacks elsewhere).
  const searching = !!query.trim();
  const ORDER = window.HubSessionListOrder;
  const { visible, hidden } = useMemo(() => {
    if (searching || !ORDER) return { visible: filtered, hidden: [] };
    const nowS = Math.floor(Date.now() / 1000);
    return ORDER.partition(filtered, poolStatusById, nowS, { activeId });
    // Date.now() is read fresh on every recompute. The buckets only change when
    // a session crosses the week boundary; that re-runs on the next session-list
    // refresh or 8 s /pool poll (poolStatusById), so the fold can lag the exact
    // boundary by one poll — cosmetic and self-healing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtered, poolStatusById, searching, activeId, ORDER]);
  // Collapse the fold again when the session SET changes (agent switch / new /
  // deleted session) — but NOT on reorder or timestamp bumps from a poll.
  const memKey = ORDER ? ORDER.membershipKey(live) : '';
  useEffect(() => { setShowOld(false); }, [memKey]);

  useEffect(() => {
    if (searchOpen && searchRef.current) searchRef.current.focus();
  }, [searchOpen]);

  // Lazy-load corpora the first time the search UI is visible. One fetch
  // per page lifetime is enough — corpora are refreshed implicitly when
  // sessions get fresh ids/timestamps via the next mount.
  useEffect(() => {
    if (!searchVisible) return;
    if (corpusById !== null || corpusLoading) return;
    setCorpusLoading(true);
    fetch('/api/orchestrator/sessions/corpora')
      .then(r => r.ok ? r.json() : [])
      .then(rows => {
        const map = {};
        if (Array.isArray(rows)) {
          for (const r of rows) {
            if (r && r.id) map[r.id] = r.corpus || '';
          }
        }
        setCorpusById(map);
      })
      .catch(() => setCorpusById({}))  // fall back to title/preview search
      .finally(() => setCorpusLoading(false));
  }, [searchVisible, corpusById, corpusLoading]);

  const onSearchClear = () => {
    setQuery('');
    if (!compact) setSearchOpen(false);
  };

  return (
    <div style={{
      display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0,
    }}>
      <div style={{
        padding: compact ? '10px 14px' : '20px 16px 14px',
        minHeight: compact ? 0 : 71,
        borderBottom: '1px solid var(--hairline)',
        display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0,
        boxSizing: 'border-box',
      }}>
        {searchVisible ? (
          <>
            <Icon name="search" size={14} color="var(--fg-3)" />
            <input
              ref={searchRef} value={query} onChange={e => setQuery(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape' && !compact) onSearchClear(); }}
              placeholder="Search title or content…"
              style={{
                flex: 1, background: 'transparent', border: 'none', outline: 'none',
                color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', minWidth: 0,
              }}
            />
            {(query || (!compact && searchOpen)) && (
              <IconButton icon="close" label={compact ? 'Clear' : 'Close search'} size={26} onClick={onSearchClear} />
            )}
          </>
        ) : (
          <>
            <IconButton icon="search" label="Search sessions" size={26} onClick={() => setSearchOpen(true)} />
            <span className="mono" style={{ flex: 1, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.08em', textAlign: 'right' }}>sessions</span>
          </>
        )}
      </div>
      <div className="scroll-hide" style={{ flex: 1, overflowY: 'auto', padding: 8, minHeight: 0 }}>
        {live.length === 0 && <div style={{ padding: 14, fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>No sessions yet.</div>}
        {live.length > 0 && filtered.length === 0 && (
          <div style={{ padding: 14, fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>No matches for "{query}".</div>
        )}
        {(() => {
          const renderRow = (s) => (
            <SessionItem
              key={s.id} s={s} active={s.id === activeId} onSelect={onSelect}
              poolStatus={poolStatusById ? poolStatusById[s.id] : undefined}
              thinking={!!(runningById && runningById.has(s.id))}
              onContextMenu={(!compact && hasRowActions) ? ((e) => {
                e.preventDefault();
                setCtxMenu({ session: s, x: e.clientX, y: e.clientY });
              }) : undefined}
            />
          );
          return (
            <>
              {visible.map(renderRow)}
              {hidden.length > 0 && (
                <SessionFoldDivider
                  count={hidden.length} expanded={showOld}
                  onToggle={() => setShowOld(v => !v)}
                />
              )}
              {showOld && hidden.map(renderRow)}
            </>
          );
        })()}
      </div>
      {ctxMenu && (
        <SessionContextMenu
          session={ctxMenu.session} x={ctxMenu.x} y={ctxMenu.y}
          onClose={() => setCtxMenu(null)}
          actions={rowActions}
        />
      )}
      {typeof onNew === 'function' && (
        <div style={{ padding: 10, borderTop: '1px solid var(--hairline)', flexShrink: 0 }}>
          <Button variant="primary" icon="plus" onClick={onNew} style={{ width: '100%' }}>
            New session
          </Button>
        </div>
      )}
    </div>
  );
}

// Meta, MessageBubble, StreamingBubble, ThinkingDots, SuggestionChips,
// ChatTranscript, blockHasContent, messageHasVisibleContent live in
// orchestrator-transcript.jsx and are published to window.

const SUGGESTIONS = [
  'co działa na serwerze?',
  'czemu nginx wstaje powoli?',
  'ostatnie błędy w nginx',
  'dodaj nowy app pod /foo',
];

// The "active agents" strip between the chat header and the window: one tab per
// agent with a warm (or the currently-active) session. Clicking a tab opens that
// agent's top session — the same target ⌘←/→ cycles to. Purely presentational;
// grouping/ordering live in window.HubAgentTabs (agent-tabs-order.js, unit-tested).
// Legacy presentational strip — kept as a fallback. The live strip is the
// enhanced window.AgentTabs from orchestrator-agent-tabs.jsx (context menu, DnD
// reorder, hover-sessions flyout, "+" add-agent modal). See `const AgentTabs`
// just below.
function _LegacyAgentTabs({ groups, activeKey, onSelect, iconFor, compact }) {
  const [hoverKey, setHoverKey] = useState(null);
  if (!groups || groups.length === 0) return null;
  return (
    <div
      role="tablist"
      aria-label="Aktywni agenci"
      className="scroll-hide"
      style={{
        display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0,
        padding: compact ? '4px 8px' : '6px 12px',
        borderBottom: '1px solid var(--hairline)',
        overflowX: 'auto', overflowY: 'hidden',
        background: 'var(--surface-1)',
      }}
    >
      {groups.map((g) => {
        const active = g.key === activeKey;
        const hovered = !active && g.key === hoverKey;
        const icon = (typeof iconFor === 'function' ? iconFor(g.libId) : '') || '';
        return (
          <button
            key={g.key}
            role="tab"
            aria-selected={active}
            title={g.name + (g.count > 1 ? ` · ${g.count} sesji` : '')}
            onClick={() => { if (g.topSessionId && typeof onSelect === 'function') onSelect(g.topSessionId); }}
            onMouseEnter={() => setHoverKey(g.key)}
            onMouseLeave={() => setHoverKey((k) => (k === g.key ? null : k))}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              flexShrink: 0, maxWidth: 200, cursor: 'pointer',
              padding: '5px 10px', borderRadius: 'var(--r-control)',
              fontFamily: 'inherit', fontSize: 'var(--t-sm)',
              fontWeight: active ? 600 : 500,
              color: active ? 'var(--accent)' : 'var(--fg-3)',
              background: active ? 'var(--accent-soft)' : (hovered ? 'var(--surface-2)' : 'transparent'),
              border: '1px solid ' + (active ? 'var(--accent-line)' : 'transparent'),
              transition: 'background 0.12s ease, color 0.12s ease',
            }}
          >
            <span style={{ fontSize: 14, lineHeight: 1, flexShrink: 0 }}>
              {icon ? icon : <Icon name="bot" size={13} stroke={1.8} />}
            </span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{g.name}</span>
            {g.count > 1 && (
              <span
                className="mono"
                style={{
                  flexShrink: 0, fontSize: 'var(--t-2xs)',
                  padding: '0 5px', borderRadius: 999, lineHeight: '15px',
                  background: active ? 'var(--accent-line)' : 'var(--surface-3)',
                  color: active ? 'var(--accent)' : 'var(--fg-4)',
                }}
              >{g.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// Enhanced strip from orchestrator-agent-tabs.jsx (loaded just before this
// file); falls back to the legacy presentational strip if that module is absent.
const AgentTabs = window.AgentTabs || _LegacyAgentTabs;

function OrchestratorView({ compact, isActive, onOpenDrawer, keyboardOpen }) {
  const [state, dispatch] = useOrchestratorReducer();
  const [input, setInput] = useState('');
  const [sheetOpen, setSheetOpen] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState([]);
  const [lightboxFile, setLightboxFile] = useState(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const [compacting, setCompacting] = useState(false);
  const [replyingToKey, setReplyingToKey] = useState(null);
  const [pinPopoverOpen, setPinPopoverOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState(null);
  const [renameValue, setRenameValue] = useState('');
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [confirmNew, setConfirmNew] = useState(false);  // new-session confirmation
  // Active (warm tmux) sessions across ALL agents — the same pool the mobile
  // "Sesje" switcher walks. Grouped per-agent (window.HubAgentTabs) to drive
  // the agent tab strip + Cmd+←/→ agent switching; Cmd+↑/↓ cycles the current
  // agent's filtered session list instead.
  const [poolSessions, setPoolSessions] = useState([]);
  // 'chat' = transcript + composer (default). 'terminal' = inline ttyd
  // iframe replacing the chat body. Per-session, persisted in
  // localStorage so a page reload (or leave-and-return) keeps the
  // user in whatever view they last had open for that session. Fresh
  // sessions default to 'chat' since terminal mode triggers a
  // /term/ensure cold spawn that the user almost certainly didn't
  // intend on a never-used chat.
  const [viewMode, setViewMode] = useState('chat');
  // Live terminal status ({label, detail}) pushed up from TerminalLiveView
  // on desktop so it shows in the chat header next to "N msg · ago · branch"
  // instead of in the terminal panel. null when not in terminal view.
  const [terminalStatus, setTerminalStatus] = useState(null);
  // Artifact the agent asked the browser to open (via the persistent SSE
  // channel) — rendered in a top-level viewer modal regardless of viewMode.
  const [openArtifact, setOpenArtifact] = useState(null);
  const prevViewModeRef = useRef('chat');
  const esRef = useRef(null);
  const pollerRef = useRef(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const fileInputRef = useRef(null);
  const toast = useToast();
  useUnreadIndicator({ messages: state.messages, activeId: state.activeId, isActive: !!isActive });

  // Persistent server-push channel (artifact toasts/modals) — its own
  // EventSource, independent of the turn stream, so an `artifact open` between
  // turns still pops a modal even after the runner is reaped. window.* is set
  // at script load (orchestrator-artifacts.jsx loads before this file), so the
  // hook call count is stable; the noop is dead-code defence.
  (window.usePersistentEvents || (() => {}))({
    sessionId: state.activeId,
    onCreated: (a) => {
      if (toast) toast('Nowy artefakt: ' + (a.title || a.type), 'ok');
      window.dispatchEvent(new CustomEvent('orchestrator:artifacts-changed'));
    },
    onOpen: (a) => setOpenArtifact(a),
  });

  // Browser TTS — settings hook + speech queue. The voiceTurnRef latches the
  // "this turn was started by the mic" signal between MicButton's final
  // transcript and the next send() so 'on-voice' mode can read the reply
  // aloud only when it should. spokenKeysRef de-dupes auto-play after
  // re-renders / message-array swaps so a finished turn is read at most once.
  const tts = (window.useTts || (() => null))();
  const [ttsSettings] = useSettings();
  // Server-scope flags (read_aloud_tmux_enabled gates the terminal speaker
  // button + the passive read-aloud watcher). useServerSettings is published on
  // window.HubSettings by settings-primitives.jsx, which loads before this file,
  // so the real hook is ALWAYS chosen — hook order is stable (mirrors the
  // `(window.useTts || …)()` defence above). The noop fallback is dead-code.
  const _serverSettings = ((window.HubSettings && window.HubSettings.useServerSettings) || (() => ({ cfg: {} })))();
  const readAloudFlag = !!(_serverSettings && _serverSettings.cfg && _serverSettings.cfg.read_aloud_tmux_enabled === true);
  const voiceMode = ttsSettings && ttsSettings.voiceOutput ? ttsSettings.voiceOutput : 'manual';
  const voiceTurnRef = useRef(false);
  const spokenKeysRef = useRef(new Set());
  // Latches the session whose latest TERMINAL input was voice-dictated (the
  // terminal mic signals it via 'hub:terminal-voice-input'); 'on-voice'
  // read-aloud consumes it so only dictated terminal turns are read.
  const terminalVoiceTurnRef = useRef(null);

  // Restore last viewMode for this session from localStorage. The DEFAULT
  // is now 'terminal' (tmux everywhere) — only sessions the user explicitly
  // switched to chat carry a 'chat' entry in the map. The write side is
  // deliberately NOT an effect — it runs synchronously inside
  // `toggleViewMode` below — to avoid a race where the restore effect's
  // queued setViewMode and a default-write effect collide in the same
  // commit cycle (UAT 2026-05-27).
  useEffect(() => {
    if (!state.activeId) { setViewMode('terminal'); return; }
    try {
      const raw = localStorage.getItem('hub-orchestrator-view-mode');
      const map = raw ? JSON.parse(raw) : {};
      setViewMode(map[state.activeId] === 'chat' ? 'chat' : 'terminal');
    } catch (_e) {
      setViewMode('terminal');
    }
  }, [state.activeId]);
  // When the user toggles BACK from terminal to chat, refetch messages
  // and reattach SSE so any claude turn that finished while the
  // transcript was hidden surfaces immediately. The chat-side
  // EventSource was technically still open during terminal mode (we
  // don't tear it down on view-mode switch), but mobile browsers
  // throttle background sockets and a brief network blip can drop it
  // silently — this reload is the safety net.
  useEffect(() => {
    const prev = prevViewModeRef.current;
    prevViewModeRef.current = viewMode;
    if (prev !== 'terminal' || viewMode !== 'chat') return undefined;
    if (!state.activeId) return undefined;
    let cancelled = false;
    let detach = null;
    closeStream();
    (async () => {
      await loadMessages(state.activeId);
      if (cancelled || !state.activeId) return;
      detach = attachStream(state.activeId);
    })();
    return () => {
      cancelled = true;
      if (detach) { try { detach(); } catch (_e) {} }
    };
  }, [viewMode, state.activeId, closeStream, loadMessages, attachStream]);
  // Toggle + persist atomically so a reload immediately after the
  // click finds the right value in localStorage. Writing inside an
  // effect (above, removed) racy because effects flush in declaration
  // order with stale closure-captured viewMode.
  const toggleViewMode = useCallback(() => {
    const next = viewMode === 'terminal' ? 'chat' : 'terminal';
    if (state.activeId) {
      try {
        const raw = localStorage.getItem('hub-orchestrator-view-mode');
        const map = raw ? JSON.parse(raw) : {};
        // Terminal is the default → only persist the non-default 'chat'
        // choice; switching back to terminal just clears the entry.
        if (next === 'chat') map[state.activeId] = 'chat';
        else delete map[state.activeId];
        localStorage.setItem('hub-orchestrator-view-mode', JSON.stringify(map));
      } catch (_e) { /* quota / privacy mode */ }
    }
    setViewMode(next);
  }, [viewMode, state.activeId]);

  // Transient artifact-gallery view. NOT persisted to the view-mode map —
  // toggling off returns to the default terminal view, and a session switch
  // resets it via the restore effect above.
  const showGallery = useCallback(() => {
    setViewMode(m => (m === 'gallery' ? 'terminal' : 'gallery'));
  }, []);

  const onRecordingStart = useCallback(() => {
    // User is speaking — stop the agent from speaking over them.
    if (tts && tts.cancel) try { tts.cancel(); } catch (e) {}
  }, [tts]);
  const onVoiceFinalDetected = useCallback(() => {
    voiceTurnRef.current = true;
    if (tts && tts.cancel) try { tts.cancel(); } catch (e) {}
  }, [tts]);

  const isStreaming = !!state.streaming;
  // URL-driven session selection — when the URL's session id changes (e.g.
  // user clicks an agent on /agents and we push /chat/<new-sid>) the
  // already-mounted OrchestratorView would otherwise stay on the old
  // activeId. Watching the router's route + dispatching hub:open-session
  // keeps the chat in sync with the URL.
  const _orchRouter = window.useRouter ? window.useRouter() : null;
  const _urlSid = _orchRouter && _orchRouter.route && _orchRouter.route.sessionId;
  useEffect(() => {
    if (_urlSid && _urlSid !== state.activeId) {
      window.dispatchEvent(new CustomEvent('hub:open-session', { detail: { session_id: _urlSid } }));
    }
  }, [_urlSid, state.activeId]);

  // Resolve the "agent scope" via the URL. URL is the truth: bare /chat
  // means Global agent (cwd=null), /chat/<sid> means whatever agent that
  // session belongs to. We do NOT fall back to state.activeId here —
  // localStorage may have restored a per-agent session id from a prior
  // visit, and using it as the scope on a bare /chat URL would falsely
  // render that agent's chat under the Global URL.
  //
  // We DO still resolve targetSession by looking up _urlSid in the list,
  // because the chat content has to match the URL session.
  const activeSession = useMemo(
    () => (state.sessions || []).find(s => s.id === state.activeId) || null,
    [state.sessions, state.activeId]
  );

  // Mark active session as read when its msg_count grows past last_read.
  // Triggered both on open (activeId change) AND on stream-end (message
  // count grows while session is open). Cross-device — backend stores
  // last_read_msg_count, so every other client polling /api/orchestrator/
  // sessions sees unread_count=0 too.
  const _markReadAbortRef = React.useRef(null);
  useEffect(() => {
    const sess = activeSession;
    if (!sess || !sess.id) return;
    const msgCount = Number(sess.msg_count) || 0;
    const lastRead = Number(sess.last_read_msg_count) || 0;
    if (msgCount <= lastRead) return;
    if (_markReadAbortRef.current) {
      try { _markReadAbortRef.current.abort(); } catch (_) {}
    }
    const controller = new AbortController();
    _markReadAbortRef.current = controller;
    (async () => {
      try {
        await fetch(apiUrl(`/api/orchestrator/sessions/${encodeURIComponent(sess.id)}/read`), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ msg_count: msgCount }),
          signal: controller.signal,
        });
        try { window.dispatchEvent(new CustomEvent('orchestrator:sessions-changed')); } catch (_) {}
      } catch (e) {
        if (e && e.name !== 'AbortError') {
          console.warn('mark-read failed:', e);
        }
      }
    })();
  }, [activeSession && activeSession.id, activeSession && activeSession.msg_count]);
  const targetSession = useMemo(
    () => (_urlSid ? ((state.sessions || []).find(s => s.id === _urlSid) || null) : null),
    [state.sessions, _urlSid]
  );

  // Agent scope drives:
  //   - filtered side list (sessions sharing the same cwd)
  //   - header label ("Global" / "Dom" / "MyProject" instead of "Orchestrator")
  //   - cwd new sessions inherit so they stay attached to this agent
  const agentCwd = _urlSid && targetSession ? (targetSession.cwd || null) : null;
  const agentLibId = _urlSid && targetSession ? (targetSession.lib_id || null) : null;
  // Library kind (projects/areas/resources) the session is scoped to, derived
  // from its cwd. Combined with the session's lib_id it gives a deep-link to
  // that project/area's Issues tab — so from a session you can jump straight to
  // the issues of the project/area it belongs to. null for the global agent (no
  // lib) → the Issues button/menu-item simply doesn't render.
  // Only projects/areas get a GitHub-backed Issues tab — resources never do
  // (and the user asked for "projektu / area"), so a resource-scoped session
  // gets no Issues button rather than a dead-end that falls back to Overview.
  const _libSection = agentCwd
    ? (/(^|\/)Projects(\/|$)/.test(agentCwd) ? 'projects'
      : /(^|\/)Areas(\/|$)/.test(agentCwd) ? 'areas' : null)
    : null;
  // The orchestrator lib_id carries a kind prefix ("projects/x", "areas/x"),
  // but the library route's lib_id is the BARE leaf (the section already encodes
  // the kind). Strip it, else buildPath emits /projects/projects/x → 404.
  const _libLeaf = agentLibId ? agentLibId.replace(/^(areas|projects|resources)\//, '') : null;
  const onOpenIssues = (_libLeaf && _libSection && typeof window.buildPath === 'function')
    ? () => {
      const href = window.buildPath({ section: _libSection, detail: { lib_id: _libLeaf }, tab: 'issues' });
      try {
        // pushState (not routerReplace) so Back returns to the session.
        window.history.pushState(null, '', (typeof window.addBase === 'function' ? window.addBase(href) : href));
        window.dispatchEvent(new Event('router:change'));
      } catch (_e) {
        if (typeof window.routerReplace === 'function') window.routerReplace(href);
      }
    }
    : null;
  const agentNameRaw = useMemo(() => {
    if (!_urlSid) return 'Global';
    if (!targetSession) return 'Agents';
    if (!agentLibId) return 'Global';
    const m = agentLibId.match(/^(areas|projects)\/(.+)$/);
    return m ? (m[2].split('/').pop()) : agentLibId;
  }, [_urlSid, targetSession, agentLibId]);
  // Humanise the slug: kebab/snake_case + camelCase → Title Case With Spaces.
  // "my-project" / "my_project" / "MyProject" → "My Project".
  const agentName = useMemo(() => {
    if (!agentNameRaw) return agentNameRaw;
    return String(agentNameRaw)
      .replace(/[-_]+/g, ' ')
      .replace(/([a-z])([A-Z])/g, '$1 $2')
      .split(/\s+/)
      .filter(Boolean)
      .map(w => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');
  }, [agentNameRaw]);
  // The "<Agent Name> agent" label rendered next to every assistant bubble.
  // Generic fallback when we have no scope (e.g. mid-resolution).
  const agentLabel = useMemo(() => {
    if (!agentName || agentName === 'Agents') return 'agent';
    return `${agentName} agent`;
  }, [agentName]);
  // Per-agent icon for the chat header avatar. Prefer the session's own
  // `icon` field (the orchestrator session-list endpoint propagates the
  // sidecar icon once Agent B's create-handler hook lands). When missing,
  // look up the matching area/project in useHubData() by lib_id.
  const _hubData = (typeof window !== 'undefined' && typeof window.useHubData === 'function')
    ? window.useHubData() : null;
  // Resolve a per-agent emoji icon from its lib_id by matching the area/project
  // in useHubData(). Shared by the header avatar (current agent) and the agent
  // tab strip (every active agent). '' when there's no lib / no match.
  const iconForLibId = useCallback((libId) => {
    if (!libId || !_hubData) return '';
    const m = String(libId).match(/^(areas|projects)\/(.+)$/);
    if (!m) return '';
    const list = m[1] === 'areas' ? (_hubData.areas || []) : (_hubData.projects || []);
    const want = m[2];
    const hit = list.find((it) => (it.lib_id || it.name) === want);
    return (hit && hit.icon) ? String(hit.icon) : '';
  }, [_hubData]);
  const agentIcon = useMemo(() => {
    if (!targetSession) return '';
    if (targetSession.icon && typeof targetSession.icon === 'string') return targetSession.icon;
    return iconForLibId(agentLibId);
  }, [targetSession, agentLibId, iconForLibId]);
  const filteredSessions = useMemo(() => {
    // URL points at a session that hasn't loaded into state.sessions yet
    // (e.g. refresh in flight) — show empty rather than dumping every
    // global session onto the sidebar.
    if (_urlSid && !targetSession) return [];
    // No URL session id (bare /chat) — Global view, only cwd=null sessions.
    if (!_urlSid) return (state.sessions || []).filter(s => (s.cwd || null) === null);
    return (state.sessions || []).filter(s => (s.cwd || null) === agentCwd);
  }, [state.sessions, agentCwd, _urlSid, targetSession]);
  // ── "active agents" tab strip + ⌘←/→ agent cycle ────────────────────────
  // The chat's active agent scope, keyed the same way the pool slots are so the
  // highlighted tab matches the session on screen. 'Agents' is the transient
  // label while a /chat/<sid> session is still loading — skip it so a phantom
  // tab doesn't flash. The grouping/order/cycle logic is in window.HubAgentTabs.
  const currentAgentKey = useMemo(
    () => (window.HubAgentTabs ? window.HubAgentTabs.agentKey(agentLibId, agentName) : null),
    [agentLibId, agentName],
  );
  const agentGroups = useMemo(() => {
    if (!window.HubAgentTabs) return [];
    const scopeReady = (!_urlSid || !!targetSession);
    const current = (scopeReady && currentAgentKey)
      ? { key: currentAgentKey, name: agentName, libId: agentLibId || '', sessionId: state.activeId || null }
      : null;
    return window.HubAgentTabs.groupAgents(poolSessions, current);
  }, [poolSessions, currentAgentKey, agentName, agentLibId, state.activeId, _urlSid, targetSession]);
  // Per-session warm-pool status for the session-list dots: 'hot' (in the pool,
  // kept) vs 'cooling' (warm but over-capacity, will be evicted). Sessions with
  // no warm slot are absent → no dot (cold/closed). Persistent slots report
  // cooling=false → 'hot'.
  const poolStatusById = useMemo(() => {
    const m = {};
    for (const slot of (poolSessions || [])) {
      if (slot && slot.session_id) m[slot.session_id] = slot.cooling ? 'cooling' : 'hot';
    }
    return m;
  }, [poolSessions]);
  // Key on a stable string hash of the indices, not the activeSession
  // reference: every refreshSessions() returns a new activeSession object
  // even when pinned_turn_idxs is unchanged, and a fresh Set identity
  // invalidates messageActions → all MessageBubbles re-render.
  const pinnedTurnIdxsKey = (activeSession && activeSession.pinned_turn_idxs
    ? activeSession.pinned_turn_idxs.join(',')
    : '');
  const pinnedTurnIdxs = useMemo(
    () => new Set((activeSession && activeSession.pinned_turn_idxs) || []),
    [pinnedTurnIdxsKey]  // eslint-disable-line react-hooks/exhaustive-deps
  );

  const closeStream = useCallback(() => {
    if (esRef.current) {
      try { esRef.current.close(); } catch (e) {}
      esRef.current = null;
    }
    if (pollerRef.current) {
      try { pollerRef.current(); } catch (e) {}
      pollerRef.current = null;
    }
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const list = await apiGet('/api/orchestrator/sessions');
      const arr = Array.isArray(list) ? list : (list.sessions || []);
      dispatch({ type: 'LOAD_SESSIONS', sessions: arr });
      return arr;
    } catch (e) {
      console.error('sessions load failed', e);
      dispatch({ type: 'SET_ERROR', error: 'Failed to load sessions' });
      return [];
    }
  }, []);

  const loadMessages = useCallback(async (id) => {
    if (!id) return;
    try {
      const d = await apiGet('/api/orchestrator/sessions/' + encodeURIComponent(id) + '/messages');
      const msgs = (d && d.messages) || [];
      // Pre-mark every existing assistant message as already-spoken so the
      // auto-speak effect doesn't read the most recent reply when the user
      // simply switches into an old session — it should only fire for
      // messages that arrive via SSE *after* this point. Reset the set
      // first because keys embed session id and we're starting fresh.
      spokenKeysRef.current = new Set();
      // A fresh session load supersedes any pending terminal-voice latch, so a
      // dictated-but-unread turn in one session can't bleed into another.
      terminalVoiceTurnRef.current = null;
      for (const m of msgs) {
        if (m && m.role === 'assistant') {
          const k = keyForMsg(m, id);
          if (k) spokenKeysRef.current.add(k);
        }
      }
      dispatch({ type: 'LOAD_MESSAGES', messages: msgs });
    } catch (e) { console.error('messages load failed', e); dispatch({ type: 'SET_ERROR', error: 'Failed to load messages' }); }
  }, []);

  // Resilient EventSource wrapper (dispatchSseEvent + startPollMode + attachStream)
  // lives in orchestrator-stream.jsx — extracted to keep this file under the
  // 800-line cap. The hook owns the same refs (esRef/pollerRef) so closeStream()
  // tearDown stays symmetric.
  const { attachStream } = useResilientStream({
    dispatch, esRef, pollerRef, closeStream, refreshSessions,
  });

  // If the EventSource died while the tab was hidden (browsers throttle
  // background tabs and may close idle SSE connections), reattach when the
  // user comes back. We probe /status server-side rather than relying on
  // local `state.streaming` because the local slot may have been wiped
  // (e.g. SSE closed silently without a terminal event ever reaching the
  // reducer). When the server says in-flight but we have no live ES, prime
  // BEGIN_STREAM and reattach. Live deltas will land via the freshly-
  // populated streaming slot. Mirror state via ref so the effect doesn't
  // re-mount on every delta.
  const _wakeStateRef = useRef(state);
  _wakeStateRef.current = state;
  useEffect(() => {
    const onWake = async () => {
      // visibilitychange fires on BOTH hidden→visible and visible→hidden;
      // skip the latter so we don't waste a /status request and (worse) try
      // to attach a fresh SSE in a tab the browser is about to throttle.
      // `focus` events imply visibility, but it costs nothing to check.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      const cur = _wakeStateRef.current;
      if (!cur.activeId) return;
      if (esRef.current || pollerRef.current) return;
      try {
        const status = await apiGet(
          '/api/orchestrator/sessions/' + encodeURIComponent(cur.activeId) + '/status',
        );
        const cur2 = _wakeStateRef.current;
        if (cur2.activeId !== cur.activeId) return; // user switched mid-fetch
        if (esRef.current || pollerRef.current) return; // re-attached meanwhile
        if (status && status.in_flight) {
          if (!cur2.streaming) dispatch({ type: 'BEGIN_STREAM' });
          attachStream(cur2.activeId);
        }
      } catch (e) { /* swallow — wake failure is non-blocking */ }
    };
    window.addEventListener('focus', onWake);
    document.addEventListener('visibilitychange', onWake);
    return () => {
      window.removeEventListener('focus', onWake);
      document.removeEventListener('visibilitychange', onWake);
    };
  }, [attachStream]);

  // Manual reload trigger — ErrorBlock dispatches this on click so the user
  // can recover from a STREAM_ERROR or stuck STREAM_POLLING state without
  // a full page reload. Closes the dead stream, re-fetches /messages (in
  // case the server has the latest assistant reply that the dropped SSE
  // never delivered), then re-opens the stream. No prompt re-submit —
  // if the turn actually completed server-side the new stream's status
  // check picks things up.
  useEffect(() => {
    const onReload = () => {
      const sid = _wakeStateRef.current && _wakeStateRef.current.activeId;
      if (!sid) return;
      closeStream();
      loadMessages(sid);
      attachStream(sid);
    };
    window.addEventListener('orchestrator:reload', onReload);
    return () => window.removeEventListener('orchestrator:reload', onReload);
  }, [closeStream, loadMessages, attachStream]);

  useEffect(() => {
    // mount: load sessions, pick or create one. Restore the last-active
    // session from localStorage so a page reload lands the user where they
    // left off instead of jumping to whichever session is freshest.
    let cancelled = false;
    (async () => {
      dispatch({ type: 'SET_LOADING', loading: true });
      try {
        const list = await refreshSessions();
        if (cancelled) return;
        const live = list.filter(s => !s.archived);
        let selectId = null;
        try {
          const remembered = localStorage.getItem('hub-active-session');
          if (remembered && live.some(s => s.id === remembered)) selectId = remembered;
        } catch (e) { /* private mode / no storage — fall through */ }
        if (!selectId) selectId = live[0] && live[0].id;
        if (!selectId) {
          const created = await apiSend('/api/orchestrator/sessions', 'POST', { title: null });
          if (cancelled) return;
          await refreshSessions();
          selectId = created.id;
        }
        dispatch({ type: 'SELECT_SESSION', id: selectId });
      } catch (e) {
        console.error('orchestrator init failed', e);
        if (!cancelled) dispatch({ type: 'SET_ERROR', error: 'Failed to init orchestrator' });
      } finally {
        if (!cancelled) dispatch({ type: 'SET_LOADING', loading: false });
      }
    })();
    return () => { cancelled = true; closeStream(); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Persist the active session id so reloads restore it. Mirrors changes
  // from any code path (initial pick, manual select, post-delete fallback,
  // post-compact swap, push-notif open).
  useEffect(() => {
    if (!state.activeId) return;
    try { localStorage.setItem('hub-active-session', state.activeId); } catch (e) {}
  }, [state.activeId]);

  // Per-session input draft. Without this, a half-typed prompt leaks to the
  // next session you click into (one global `input` state). Strategy:
  // mirror every keystroke to `hub-draft:<sid>`, rehydrate when activeId
  // changes, clear on successful send. `skipNextSaveRef` swallows the
  // first save-pass after a switch — that pass runs with the previous
  // session's stale `input` (the setInput from restore is queued, not
  // committed) and would otherwise overwrite the new sid's draft.
  const prevActiveIdRef = useRef(null);
  const skipNextSaveRef = useRef(false);
  useEffect(() => {
    const prevId = prevActiveIdRef.current;
    const nextId = state.activeId;
    if (nextId && nextId !== prevId) {
      let restored = '';
      try { restored = localStorage.getItem('hub-draft:' + nextId) || ''; } catch (e) { /* ignore */ }
      skipNextSaveRef.current = true;
      setInput(restored);
    }
    prevActiveIdRef.current = nextId;
  }, [state.activeId]);
  useEffect(() => {
    const sid = state.activeId;
    if (!sid) return;
    if (skipNextSaveRef.current) { skipNextSaveRef.current = false; return; }
    try {
      if (input) localStorage.setItem('hub-draft:' + sid, input);
      else localStorage.removeItem('hub-draft:' + sid);
    } catch (e) { /* quota / privacy mode */ }
  }, [input, state.activeId]);

  useEffect(() => {
    if (!state.activeId) return;
    closeStream();
    setPendingAttachments([]);
    setLightboxFile(null);
    let cancelled = false;
    let detach = null;
    (async () => {
      await loadMessages(state.activeId);
      if (cancelled || !state.activeId) return;
      // Phase 1 multi-session resilience: probe /status before reattaching.
      // If the server still has an in-flight runner (e.g. user kicked off a
      // turn here, switched away, came back while it's still computing),
      // dispatch BEGIN_STREAM so the reducer's `if (str)` guards on INIT /
      // DELTA / APPEND_BLOCK actually accept the replay events that
      // attachStream is about to receive. Without this prime step the
      // reducer silently drops every partial event and the UI stays frozen
      // until the runner finalises.
      let inFlight = false;
      try {
        const status = await apiGet(
          '/api/orchestrator/sessions/' + encodeURIComponent(state.activeId) + '/status',
        );
        if (cancelled || !state.activeId) return;
        inFlight = !!(status && status.in_flight);
      } catch (e) { /* status probe failure is non-fatal */ }
      if (inFlight) dispatch({ type: 'BEGIN_STREAM' });
      // Reattach SSE — server emits `done` instantly for sessions with no
      // in-flight runner. For active runners it resumes via Last-Event-ID
      // (none on first connect → full replay; reducer accepts those events
      // because BEGIN_STREAM above set up the slot).
      detach = attachStream(state.activeId);
    })();
    return () => {
      cancelled = true;
      if (detach) { try { detach(); } catch (e) {} }
    };
  }, [state.activeId, closeStream, loadMessages, attachStream]);

  const onFilesPicked = useCallback((files) => {
    if (!files || !files.length) return;
    setPendingAttachments(prev => {
      const accepted = [];
      const seen = new Set(prev.map(p => p.name + ':' + p.size));
      for (const f of files) {
        if (f.size > MAX_UPLOAD_BYTES) {
          if (toast) toast(f.name + ': > 100 MB, pomijam', 'err');
          continue;
        }
        const key = f.name + ':' + f.size;
        if (seen.has(key)) continue;
        seen.add(key);
        accepted.push(f);
      }
      return accepted.length ? [...prev, ...accepted] : prev;
    });
  }, [toast]);

  const removePending = useCallback((idx) => {
    setPendingAttachments(prev => prev.filter((_, i) => i !== idx));
  }, []);

  const onAttachClick = useCallback(() => {
    if (fileInputRef.current) fileInputRef.current.click();
  }, []);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [state.messages, state.streaming]);

  // When the composer textarea grows (Shift+Enter newlines, large pasted text),
  // it eats vertical space from the transcript. Pin the scroll to the bottom
  // when the user was already there so the last message stays visible.
  useEffect(() => {
    const ta = inputRef.current;
    if (!ta || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      const sc = scrollRef.current;
      if (!sc) return;
      const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
      if (nearBottom) sc.scrollTop = sc.scrollHeight;
    });
    ro.observe(ta);
    return () => ro.disconnect();
  }, []);

  const send = useCallback(async (text, opts = {}) => {
    // `opts.attachments` lets the queue drain pass an explicit File[] snapshot
    // taken at enqueue time, so we don't read the live `pendingAttachments`
    // (which may already hold the user's NEXT message). When omitted we fall
    // back to live state — that's the direct-from-input path.
    const usingExplicitFiles = opts.attachments !== undefined;
    const files = usingExplicitFiles ? (opts.attachments || []) : pendingAttachments;
    const t = (text != null ? text : input).trim();
    const hasFiles = files.length > 0;
    if (!t && !hasFiles) return;
    if (!state.activeId) return;
    const sid = state.activeId;

    // Upload attachments BEFORE the optimistic message so the user bubble
    // can render server-resolved tiles immediately. Backend's
    // _post_message_handler will pop_pending() and inject the <attached>
    // block into the prompt sent to claude.
    let saved = [];
    if (hasFiles) {
      try { saved = await uploadAttachments(sid, files); }
      catch (e) {
        console.error('upload failed', e);
        toast && toast('Upload failed: ' + (e.message || 'unknown'), 'err');
        return;
      }
    }

    // Resolve any pending reply context. For queue-drained sends the caller
    // passes `opts.replyToTurnIdx` (the snapshot taken at enqueue time);
    // direct sends still derive from the live `replyingToKey` state.
    let replyTurnIdx = null;
    if (opts.replyToTurnIdx != null) {
      replyTurnIdx = opts.replyToTurnIdx;
    } else if (replyingToKey) {
      const target = (state.messages || []).find(m => keyForMsg(m, sid) === replyingToKey);
      const ti = target && target.turn_idx;
      if (typeof ti === 'number' && ti >= 0) replyTurnIdx = ti;
    }

    // Only the direct-from-input path mutates composer state. Queue drains
    // pass `opts.skipClear=true` because the input/pending strip already
    // belongs to whatever the user is typing NEXT.
    if (!opts.skipClear) {
      setInput('');
      setPendingAttachments([]);
      setReplyingToKey(null);
      try { localStorage.removeItem('hub-draft:' + sid); } catch (e) { /* ignore */ }
    }
    // Consume the voice-turn flag here: any TTS in flight is interrupted
    // (the user is moving the conversation forward) and the bit is stamped
    // onto the user-message metadata so the assistant-side TTS effect can
    // honour 'on-voice' mode without racing the message-array update.
    if (tts && tts.cancel) try { tts.cancel(); } catch (e) {}
    const fromVoice = voiceTurnRef.current;
    voiceTurnRef.current = false;
    dispatch({ type: 'APPEND_USER_MESSAGE', message: {
      role: 'user', ts: Date.now() / 1000, turn_idx: -1,
      blocks: [{ kind: 'text', text: t }],
      from_voice: fromVoice,
      attachments: saved, session_id: sid,
      reply_to_turn_idx: replyTurnIdx,
    }});
    dispatch({ type: 'BEGIN_STREAM' });
    try {
      const body = { text: t };
      if (replyTurnIdx != null) body.reply_to_turn_idx = replyTurnIdx;
      // Per-session interactive-mode override (Phase 2B). Tri-state pref in
      // localStorage: 'on' → true, 'off' → false, missing → omit (server
      // falls back to global runner_mode flag).
      const interactivePref = readInteractivePref(sid);
      if (interactivePref === 'on') body.interactive_mode = true;
      else if (interactivePref === 'off') body.interactive_mode = false;
      const postResponse = await apiSend(
        '/api/orchestrator/sessions/' + encodeURIComponent(sid) + '/messages',
        'POST', body,
      );
      // Backend echoes the chosen `runner_mode` in the POST response (Phase
      // 2A). Stash it on the stream state so the RunnerModeBadge can show
      // the effective mode for the in-flight turn.
      if (postResponse && postResponse.runner_mode) {
        dispatch({ type: 'SET_LAST_RUNNER_MODE', runner_mode: postResponse.runner_mode });
      }
      attachStream(sid);
    } catch (e) {
      console.error('send failed', e);
      dispatch({ type: 'STREAM_ERROR', text: e.message || 'send failed' });
      dispatch({ type: 'CLEAR_STREAMING' });
      toast && toast('Send failed', 'err');
    }
  }, [input, state.activeId, attachStream, toast, pendingAttachments, replyingToKey, state.messages, tts]);

  // ──────────── Message queue ────────────
  // While a turn is in flight the user can keep typing — pressing send queues
  // the message instead of dropping it. Items drain automatically when the
  // current turn ends (one at a time). Cancel drains the queue too: stopping
  // an in-flight turn usually means "nope, redirect" so dispatching follow-ups
  // straight after would be confusing.
  const [queue, setQueue] = useState([]); // [{id, text, attachments, replyToTurnIdx, createdAt}]
  const queueRef = useRef(queue);
  queueRef.current = queue;
  // Set true by onCancel; the streaming-end effect skips auto-drain once.
  const cancelledRef = useRef(false);
  const wasStreamingRef = useRef(false);

  // Drop the queue when the user switches to another session — the items
  // were composed against the prior conversation context and would be
  // surprising to fire blindly into a new one.
  useEffect(() => { setQueue([]); }, [state.activeId]);

  // Auto-drain on turn finish (only natural finishes; cancel skips one cycle).
  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    wasStreamingRef.current = isStreaming;
    if (wasStreaming && !isStreaming) {
      if (cancelledRef.current) {
        cancelledRef.current = false;
        return;
      }
      const next = queueRef.current[0];
      if (!next || !state.activeId) return;
      setQueue(q => q.slice(1));
      send(next.text, {
        attachments: next.attachments || [],
        replyToTurnIdx: next.replyToTurnIdx ?? null,
        skipClear: true,
      });
    }
  }, [isStreaming, state.activeId, send]);

  // Resolve replyingToKey → turn_idx once at enqueue time so the queue entry
  // carries a stable reference (the underlying messages array may grow before
  // the entry actually fires).
  const resolveReplyTurnIdx = useCallback(() => {
    if (!replyingToKey) return null;
    const sid = state.activeId;
    const target = (state.messages || []).find(m => keyForMsg(m, sid) === replyingToKey);
    const ti = target && target.turn_idx;
    return (typeof ti === 'number' && ti >= 0) ? ti : null;
  }, [replyingToKey, state.activeId, state.messages]);

  const enqueueMessage = useCallback((explicitText) => {
    // Voice auto-send passes the final transcript explicitly so we don't read
    // stale `input` state when this callback fires from a setTimeout — the
    // setInput() right before may not have flushed yet.
    const baseText = explicitText != null ? explicitText : input;
    const t = String(baseText).trim();
    const files = pendingAttachments;
    if (!t && files.length === 0) return;
    if (!state.activeId) return;
    const replyToTurnIdx = resolveReplyTurnIdx();
    setQueue(q => [...q, {
      id: 'q-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7),
      text: t,
      attachments: files,
      replyToTurnIdx,
      createdAt: Date.now(),
    }]);
    setInput('');
    setPendingAttachments([]);
    setReplyingToKey(null);
  }, [input, pendingAttachments, state.activeId, resolveReplyTurnIdx]);

  // Single entry point for keyboard / send-button. Picks queue vs immediate
  // based on streaming state. Non-text submissions (choice / ask pills) skip
  // this and call `send` directly — they're click responses and shouldn't be
  // queued behind a buffer. `explicitText` lets the voice auto-send path
  // bypass closure-stale `input` reads.
  const submit = useCallback((explicitText) => {
    if (isStreaming) {
      enqueueMessage(explicitText);
    } else {
      send(explicitText);
    }
  }, [isStreaming, enqueueMessage, send]);

  const removeQueuedMessage = useCallback((id) => {
    setQueue(q => q.filter(item => item.id !== id));
  }, []);

  // "Edit" pulls the queued item back into the composer (text + pendings) and
  // removes it from the queue. The user can then re-submit (which either
  // sends or re-queues depending on streaming).
  const editQueuedMessage = useCallback((id) => {
    const item = queueRef.current.find(q => q.id === id);
    if (!item) return;
    setQueue(q => q.filter(qq => qq.id !== id));
    setInput(item.text || '');
    setPendingAttachments(item.attachments || []);
    if (inputRef.current) {
      try { inputRef.current.focus(); } catch (e) { /* ignore */ }
    }
  }, []);


  // Actual session creation — gated behind a confirmation modal (a new
  // tmux REPL is a real cold spawn now that interactive is the default,
  // so a stray + tap shouldn't silently start one).
  const createSession = useCallback(async () => {
    setConfirmNew(false);
    closeStream();
    dispatch({ type: 'CLEAR_STREAMING' });
    try {
      // New sessions inherit the current agent's scope so they show up in
      // the same filtered side list. agentCwd/agentLibId are null for
      // Global, which produces a normal cwd-less session.
      const payload = { title: null };
      if (agentCwd) payload.cwd = agentCwd;
      if (agentLibId) payload.lib_id = agentLibId;
      const created = await apiSend('/api/orchestrator/sessions', 'POST', payload);
      await refreshSessions();
      dispatch({ type: 'SELECT_SESSION', id: created.id });
      setSheetOpen(false);
      toast && toast('New session', 'ok');
    } catch (e) { toast && toast('New failed: ' + e.message, 'err'); }
  }, [closeStream, refreshSessions, toast, agentCwd, agentLibId]);

  // The + button (header, kebab, session list) opens the confirmation;
  // createSession does the work on confirm.
  const onNew = useCallback(() => { setConfirmNew(true); }, []);

  // New-session confirm modal has no focusable field, so bind Enter/Esc at
  // the window level while it's open: Enter confirms (create), Esc cancels.
  // Capture phase + stopPropagation so the composer textarea's own Enter
  // handler doesn't also fire and submit a half-typed prompt.
  useEffect(() => {
    if (!confirmNew) return undefined;
    const onKey = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); createSession(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setConfirmNew(false); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [confirmNew, createSession]);

  const onRename = useCallback((id) => {
    const target = id || state.activeId;
    if (!target) return;
    const cur = (state.sessions || []).find(s => s.id === target);
    setRenameTarget(target);
    setRenameValue((cur && cur.title) || '');
  }, [state.activeId, state.sessions]);

  const submitRename = useCallback(async () => {
    if (!renameTarget) return;
    const next = renameValue.trim() || null;
    setRenameTarget(null);
    try {
      await apiSend('/api/orchestrator/sessions/' + encodeURIComponent(renameTarget), 'PATCH', { title: next });
      await refreshSessions();
      toast && toast('Renamed', 'ok');
    } catch (e) { toast && toast('Rename failed', 'err'); }
  }, [renameTarget, renameValue, refreshSessions, toast]);

  // Pull the live pool once now → drives the agent-tab strip + session "open"
  // dots. Exposed as a callback so close/delete (here and the agent-tab
  // close-agent action) can force an immediate refresh instead of waiting up to
  // 8s for the next poll tick (otherwise a just-closed tab/dot lingers). The 8s
  // interval below drives the steady-state refresh.
  const refreshPool = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/api/orchestrator/pool'));
      if (!r.ok) return;
      const data = await r.json();
      setPoolSessions((data && data.slots) || []);
    } catch (_e) { /* tolerate offline */ }
  }, []);

  // Pull a FRESH pool snapshot → {id: 'hot'|'cooling'} map. The cached
  // poolStatusById can be up to ~8s stale (the /pool poll cadence), so a session
  // opened seconds before a close/delete might not show as "open" yet and would
  // be wrongly skipped as a redirect target. We only call this when removing the
  // on-screen session (a deliberate, non-hot action), so the extra round-trip is
  // cheap. Returns null on failure → caller falls back to the cached map.
  const fetchPoolStatusMap = useCallback(async () => {
    try {
      const r = await fetch(apiUrl('/api/orchestrator/pool'));
      if (!r.ok) return null;
      const data = await r.json();
      const m = {};
      for (const slot of (data && data.slots) || []) {
        if (slot && slot.session_id) m[slot.session_id] = slot.cooling ? 'cooling' : 'hot';
      }
      return m;
    } catch (e) { return null; }
  }, []);

  // Apply the post-removal navigation decision from
  // window.HubSessionListOrder.pickRedirectTarget. 'none' → stay put (we removed
  // a session that wasn't on screen). 'session' → open that session (same core
  // as onSelectSession, inlined to avoid a TDZ ref — onSelectSession is defined
  // below). 'agents' → bounce to the agents directory.
  const applyRemovalDecision = useCallback((decision) => {
    if (!decision || decision.action === 'none') return;
    if (tts && tts.cancel) { try { tts.cancel(); } catch (e) { /* ignore */ } }
    closeStream();
    dispatch({ type: 'CLEAR_STREAMING' });
    setSheetOpen(false);  // dismiss the mobile "Sessions" sheet for either branch
    if (decision.action === 'session' && decision.id) {
      dispatch({ type: 'SELECT_SESSION', id: decision.id });
      if (typeof window.routerReplace === 'function' && typeof window.buildPath === 'function') {
        window.routerReplace(window.buildPath({ section: 'orchestrator', sessionId: decision.id }));
      }
    } else if (decision.action === 'agents') {
      // Drop the active id so neither state nor the localStorage lastPath keeps
      // pointing at the just-removed (possibly deleted) session.
      dispatch({ type: 'SELECT_SESSION', id: null });
      if (typeof window.routerReplace === 'function' && typeof window.buildPath === 'function') {
        window.routerReplace(window.buildPath({ section: 'agents' }));
      }
    }
  }, [tts, closeStream]);

  const onDelete = useCallback((id) => {
    const target = id || state.activeId;
    if (!target) return;
    setDeleteTarget(target);
  }, [state.activeId]);

  const submitDelete = useCallback(async () => {
    const target = deleteTarget;
    setDeleteTarget(null);
    if (!target) return;
    // Mark closed so terminal mode doesn't re-spawn it before it's wiped.
    if (window.HubSessionListOrder && window.HubSessionListOrder.closedGuard) {
      window.HubSessionListOrder.closedGuard.mark(target);
    }
    // Deleting a session that isn't the one displayed → 'none' (stay put);
    // deleting the displayed one → next open session in this agent → another
    // agent → agents. Use a fresh pool snapshot for the on-screen case so a
    // just-opened session isn't missed (cached map is ≤8s stale).
    const isActive = target === state.activeId;
    const pool = isActive ? (await fetchPoolStatusMap()) || poolStatusById : poolStatusById;
    const decision = (window.HubSessionListOrder && window.HubSessionListOrder.pickRedirectTarget)
      ? window.HubSessionListOrder.pickRedirectTarget({
          closedId: target, activeId: state.activeId,
          sessions: state.sessions, poolStatusById: pool, agentCwd,
        })
      : { action: isActive ? 'agents' : 'none' };
    try {
      await apiSend('/api/orchestrator/sessions/' + encodeURIComponent(target), 'DELETE', null);
      await refreshSessions();
      refreshPool();  // drop the deleted session's tab/dot now, not in ≤8s
      // Only navigate after the delete actually succeeds (closeStream runs
      // inside applyRemovalDecision, so a failed delete leaves the user put).
      applyRemovalDecision(decision);
      toast && toast('Deleted', 'ok');
    } catch (e) { toast && toast('Delete failed', 'err'); }
  }, [deleteTarget, state.activeId, state.sessions, poolStatusById, agentCwd,
      refreshSessions, refreshPool, applyRemovalDecision, toast, fetchPoolStatusMap]);

  // Close (X / "Zamknij sesję") — kill the running session + free its pool
  // slot, KEEP the transcript (unlike Delete). No confirmation: nothing is
  // destroyed, so it's a cheap, reversible "I'm done with this chat" that
  // reclaims a scarce slot. Navigation: closing a session that ISN'T on screen
  // just frees the slot and stays put; closing the displayed one jumps to the
  // next open session in this agent → another agent → the agents directory.
  const onCloseSession = useCallback(async (id) => {
    const target = id || state.activeId;
    if (!target) return;
    // Mark closed so terminal mode doesn't immediately re-spawn it.
    if (window.HubSessionListOrder && window.HubSessionListOrder.closedGuard) {
      window.HubSessionListOrder.closedGuard.mark(target);
    }
    const isActive = target === state.activeId;
    const pool = isActive ? (await fetchPoolStatusMap()) || poolStatusById : poolStatusById;
    const decision = (window.HubSessionListOrder && window.HubSessionListOrder.pickRedirectTarget)
      ? window.HubSessionListOrder.pickRedirectTarget({
          closedId: target, activeId: state.activeId,
          sessions: state.sessions, poolStatusById: pool, agentCwd,
        })
      : { action: isActive ? 'agents' : 'none' };
    try {
      await apiSend('/api/orchestrator/sessions/' + encodeURIComponent(target) + '/close', 'POST', null);
      await refreshSessions();
      refreshPool();  // drop the closed session's tab/dot now, not in ≤8s
      // Navigate (and tear down the SSE, inside applyRemovalDecision) only after
      // the close succeeds — a failed close leaves the user on the live session.
      applyRemovalDecision(decision);
      toast && toast('Sesja zamknięta — slot zwolniony', 'ok');
    } catch (e) {
      toast && toast('Nie udało się zamknąć sesji', 'err');
    }
  }, [state.activeId, state.sessions, poolStatusById, agentCwd,
      refreshSessions, refreshPool, applyRemovalDecision, toast, fetchPoolStatusMap]);

  // Both togglers accept an optional target session (the context-menu path
  // passes the right-clicked row); they fall back to activeSession so the
  // header buttons keep working unchanged.
  const onTogglePin = useCallback(async (target) => {
    // Guard against a DOM event leaking in from a bare onClick={onTogglePin}.
    const sess = (target && target.id) ? target : activeSession;
    if (!sess) return;
    const next = !sess.pinned;
    try {
      await apiSend('/api/orchestrator/sessions/' + encodeURIComponent(sess.id), 'PATCH', { pinned: next });
      await refreshSessions();
      toast && toast(next ? 'Pinned' : 'Unpinned', 'ok');
    } catch (e) { toast && toast('Pin failed', 'err'); }
  }, [activeSession, refreshSessions, toast]);

  const onTogglePersistent = useCallback(async (target) => {
    const sess = (target && target.id) ? target : activeSession;
    if (!sess) return;
    const next = !sess.persistent;
    try {
      await apiSend('/api/orchestrator/sessions/' + encodeURIComponent(sess.id), 'PATCH', { persistent: next });
      await refreshSessions();
      toast && toast(next ? 'Keep-alive włączony — sesja nie wygasa' : 'Keep-alive wyłączony', 'ok');
    } catch (e) { toast && toast('Keep-alive nie zadziałał', 'err'); }
  }, [activeSession, refreshSessions, toast]);

  const onCancel = useCallback(async () => {
    if (!state.activeId) return;
    // Mark so the streaming-end effect skips one auto-drain cycle. Then drop
    // any pending queue items: stopping an in-flight turn is usually a
    // redirect, and silently firing follow-ups straight after would surprise
    // the user.
    cancelledRef.current = true;
    setQueue([]);
    if (tts && tts.cancel) try { tts.cancel(); } catch (e) {}
    try { await apiSend('/api/orchestrator/sessions/' + encodeURIComponent(state.activeId) + '/cancel', 'POST', null); }
    catch (e) { /* UI still cancels */ }
    closeStream();
    dispatch({ type: 'CLEAR_STREAMING' });
    toast && toast('Cancelled', 'ok');
  }, [state.activeId, closeStream, toast, tts]);

  const onSelectSession = useCallback((id) => {
    if (id === state.activeId) { setSheetOpen(false); return; }
    // Opening a session is an explicit "I want it live" — lift any recent
    // closed-guard so the terminal warms it again.
    if (id && window.HubSessionListOrder && window.HubSessionListOrder.closedGuard) {
      window.HubSessionListOrder.closedGuard.clear(id);
    }
    if (tts && tts.cancel) try { tts.cancel(); } catch (e) {}
    closeStream();
    dispatch({ type: 'CLEAR_STREAMING' });
    dispatch({ type: 'SELECT_SESSION', id });
    setSheetOpen(false);
    // Reflect the selected session in the URL so refresh / back works AND
    // localStorage lastPath updates so PWA cold-start lands here.
    // Goes through routerReplace (not raw history.replaceState) so the
    // _writeLastPath bookkeeping fires.
    if (id && typeof window.routerReplace === 'function' && typeof window.buildPath === 'function') {
      window.routerReplace(window.buildPath({ section: 'orchestrator', sessionId: id }));
    }
  }, [state.activeId, closeStream, tts]);

  // hub:open-session bridge — fires when SW relays a notification click,
  // OR from the cold-start URL replay below. Registered BEFORE the URL
  // replay so `addEventListener` runs first; otherwise the synchronous
  // dispatch from the next effect would land before the listener attaches
  // (React flushes effects in declaration order within a component) and
  // be silently dropped.
  useOpenSessionListener({ sessions: state.sessions, onSelectSession, refreshSessions });

  // Keyboard shortcuts (desktop):
  //   ⌘/Ctrl + N           → onNew (new session in current agent scope)
  //   ⌘/Ctrl + ↑ / ↓       → cycle to prev/next session of the CURRENT agent
  //   ⌘/Ctrl + ← / →       → switch AGENT (prev/next tab), to its top session
  //   (⌥ + ⇥ opens the all-sessions switcher overlay — see SessionSwitcher)
  // Bound to window keydown with capture: false so the message composer
  // textarea — which has its own enter/shift-enter handler — gets its
  // shot first. We skip when modifiers other than meta/ctrl are held
  // (e.g. ⌥/⇧) so we don't clobber browser-native combinations the user
  // expects.
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.shiftKey && !e.altKey && (e.key === 'n' || e.key === 'N')) {
        // Ctrl/⌘+Shift+N → open the agent-tab "add agent" modal (AgentTabs
        // listens for this event; only the mounted strip acts).
        e.preventDefault();
        window.dispatchEvent(new Event('hub:open-agent-modal'));
        return;
      }
      if (e.altKey || e.shiftKey) return;
      const k = e.key;
      if (k === 'n' || k === 'N') {
        // Browsers usually intercept ⌘N for "new window" before the page
        // sees it — this still works in Firefox / Electron contexts and
        // is harmless elsewhere. preventDefault is best-effort.
        e.preventDefault();
        if (typeof onNew === 'function') onNew();
        return;
      }
      if (k === 'u' || k === 'U') {
        // Upload a file to the session — terminal view clicks its file
        // input, chat composer opens its attach picker. Both listen for
        // this event; only the mounted one acts.
        e.preventDefault();
        window.dispatchEvent(new Event('hub:shortcut-upload'));
        return;
      }
      if (k === 'm' || k === 'M') {
        // Toggle voice recording (mic) — same listener split as upload.
        e.preventDefault();
        window.dispatchEvent(new Event('hub:shortcut-mic'));
        return;
      }
      // ↑/↓ cycle the CURRENT agent's sessions IN THE SAME ORDER + MEMBERSHIP the
      // sidebar shows them: archived dropped, then partitioned so we walk the
      // VISIBLE rows (open-first → recency; folded ">1 week" rows excluded, but
      // the active session is always kept visible). Matches what the user sees,
      // not the raw backend order. ↓ top→bottom, ↑ opposite; wrap at the edges.
      const isVert = (k === 'ArrowUp' || k === 'ArrowDown');
      if (isVert) {
        const HSL = window.HubSessionListOrder;
        const base = (filteredSessions || []).filter((s) => !s.archived);
        const list = (HSL && HSL.partition)
          ? HSL.partition(base, poolStatusById, Math.floor(Date.now() / 1000), { activeId: state.activeId }).visible.map((s) => s.id)
          : base.map((s) => s.id);
        if (list.length === 0) return;
        const curIdx = list.indexOf(state.activeId);
        const delta = (k === 'ArrowDown') ? 1 : -1;
        const baseIdx = curIdx === -1 ? 0 : curIdx;
        const nextId = list[(baseIdx + delta + list.length) % list.length];
        if (nextId && nextId !== state.activeId) {
          e.preventDefault();
          onSelectSession(nextId);
        }
        return;
      }
      // ←/→ switch the ACTIVE AGENT — one step per agent across the tab strip,
      // landing on that agent's top (most-recent) session. → next agent, ←
      // previous; wraps. preventDefault always (when there's an agent to move
      // to) so it overrides the browser's ⌘←/→ back/forward navigation.
      const isHoriz = (k === 'ArrowLeft' || k === 'ArrowRight');
      if (isHoriz) {
        const groups = agentGroups || [];
        if (groups.length === 0 || !window.HubAgentTabs) return;
        e.preventDefault();
        const dir = (k === 'ArrowRight') ? 1 : -1;
        const nextId = window.HubAgentTabs.cycleAgentTarget(groups, currentAgentKey, dir);
        if (nextId && nextId !== state.activeId) onSelectSession(nextId);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [filteredSessions, poolStatusById, agentGroups, currentAgentKey, state.activeId, onSelectSession, onNew]);

  // Poll the active-session pool so the agent tab strip + Cmd+←/→ have the
  // live per-agent grouping.
  useEffect(() => {
    refreshPool();
    const id = setInterval(refreshPool, 8000);
    return () => clearInterval(id);
  }, [refreshPool]);

  // Which sessions have an in-flight turn right now ("agent is thinking"), from
  // /api/orchestrator/turns/running. Polled fast (2s) since it's a tiny payload
  // and the indicator should feel live. Only registers dashboard/chat-initiated
  // turns — a turn typed straight into the ttyd terminal isn't tracked here.
  const [runningIds, setRunningIds] = useState([]);
  useEffect(() => {
    // Only poll while the orchestrator view is the active section AND the tab is
    // foregrounded — the indicator is only visible here, so polling on Tasks /
    // Logs / a backgrounded PWA would just drain battery.
    if (!isActive) return undefined;
    let cancelled = false;
    const load = async () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      try {
        const r = await fetch(apiUrl('/api/orchestrator/turns/running'));
        if (!r.ok) return;
        const d = await r.json();
        const ids = (Array.isArray(d && d.running) ? d.running : [])
          .map((x) => x && x.session_id).filter(Boolean).sort();
        // Only re-render when the set actually changes (avoid 2s churn).
        if (!cancelled) setRunningIds((prev) => (prev.join(',') === ids.join(',') ? prev : ids));
      } catch (_e) { /* tolerate offline */ }
    };
    load();
    const id = setInterval(load, 2000);
    const onVis = () => { if (document.visibilityState === 'visible') load(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { cancelled = true; clearInterval(id); document.removeEventListener('visibilitychange', onVis); };
  }, [isActive]);
  // The active session's live streaming state gives instant feedback (no 2s
  // wait); other sessions rely on the poll.
  const runningSet = useMemo(() => {
    const s = new Set(runningIds);
    if (isStreaming && state.activeId) s.add(state.activeId);
    return s;
  }, [runningIds, isStreaming, state.activeId]);

  // On mount: if URL carries /orchestrator/<sid>, replay it through the
  // session-open pipeline. Must run AFTER useOpenSessionListener for
  // the listener to be attached when we dispatch.
  useEffect(() => {
    if (typeof window.parseRoute !== 'function') return;
    const r = window.parseRoute(window.location.pathname);
    if (r && r.section === 'orchestrator' && r.sessionId) {
      window.dispatchEvent(new CustomEvent('hub:open-session', { detail: { session_id: r.sessionId } }));
    }
    // Run-once: only at first mount of the orchestrator view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const compactSession = useCallback(async () => {
    if (!state.activeId || compacting) return;
    const ok = window.confirm(
      'Skompaktować? Claude wygeneruje streszczenie obecnej rozmowy + zachowa zadania, '
      + 'a następnie utworzy nową sesję z tym streszczeniem jako pierwszą wiadomością. '
      + 'Stara sesja zostaje w liście. Operacja może potrwać 30-60s.'
    );
    if (!ok) return;
    setCompacting(true);
    try {
      const r = await apiSend('/api/orchestrator/sessions/' + state.activeId + '/compact', 'POST');
      if (!r || !r.ok || !r.new_session_id) {
        throw new Error((r && r.error) || 'compact failed');
      }
      if (toast) toast(`Skompaktowane (${r.n_tasks || 0} zadań przeniesionych)`, 'ok');
      await refreshSessions();
      onSelectSession(r.new_session_id);
    } catch (e) {
      if (toast) toast('Compact nieudany: ' + (e && e.message ? e.message : 'unknown'), 'err');
    } finally {
      setCompacting(false);
    }
  }, [state.activeId, compacting, refreshSessions, onSelectSession, toast]);

  // Refresh sessions after the model PATCH so activeSession.model picks up the
  // new value and the header badge re-renders. The PATCH itself happens in
  // ModelButton; this is just the post-success bookkeeping.
  const onModelChange = useCallback(async () => {
    try { await refreshSessions(); }
    catch (e) { /* refreshSessions already toasts on its own */ }
  }, [refreshSessions]);

  // ---- TTS: auto-speak finalized assistant messages -----------------------
  // Stable key per assistant turn so we read each one at most once. Falls back
  // to ts when turn_idx is missing (some legacy paths).
  const _msgKey = useCallback((m) => keyForMsg(m, state.activeId), [state.activeId]);

  // Pull the markdown text out of an assistant message's blocks, preserving
  // order. Skips code/image/choice/ask/etc — voice should read prose only.
  const _speechBlocksFor = useCallback((m) => {
    const blocks = Array.isArray(m && m.blocks) ? m.blocks : [];
    return blocks
      .filter((b) => b && (b.kind === 'markdown' || b.kind === 'text') && (b.text || b.content))
      .map((b) => ({ text: b.text != null ? b.text : (b.content || '') }));
  }, []);

  // Returns true if it actually had speakable prose to read (lets the manual
  // terminal button distinguish "spoke" from "nothing to read").
  const speakMessage = useCallback((msg) => {
    if (!tts || !tts.supported) return false;
    const blocks = _speechBlocksFor(msg);
    if (!blocks.length) return false;
    const key = _msgKey(msg);
    if (key) spokenKeysRef.current.add(key);
    tts.speakBlocks(blocks, { key });
    return true;
  }, [tts, _speechBlocksFor, _msgKey]);

  // Manual terminal speaker button → read the last assistant message aloud.
  // In terminal mode the user types straight into ttyd (no dashboard runner),
  // so state.messages can be stale — fetch a FRESH transcript and speak the
  // last assistant turn from it. Best-effort: a fetch failure surfaces a toast,
  // never throws into the click handler. iOS autoplay is unlocked first.
  const onSpeakLastInTerminal = useCallback(async () => {
    if (!tts || !tts.supported || !state.activeId) return;
    if (typeof tts.unlock === 'function') { try { tts.unlock(); } catch (_e) { /* ignore */ } }
    try {
      const d = await apiGet('/api/orchestrator/sessions/' + encodeURIComponent(state.activeId) + '/messages');
      const msgs = (d && d.messages) || [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i] && msgs[i].role === 'assistant') {
          // Always give feedback: speakMessage returns false when the turn has
          // no speakable prose, so the 🔊 click never looks like a dead button.
          if (!speakMessage(msgs[i]) && toast) toast('Brak treści do odczytania', 'warn');
          return;
        }
      }
      if (toast) toast('Brak wiadomości do odczytania', 'warn');
    } catch (_e) {
      if (toast) toast('Nie udało się pobrać wiadomości do odczytu', 'err');
    }
  }, [tts, state.activeId, speakMessage, toast]);

  // Mobile "show last assistant message" — fetches a FRESH transcript (same as
  // onSpeakLastInTerminal) and opens a selectable/copyable modal, because the
  // terminal/transcript copy badly on phones (#88, related #83). Display-only.
  const [lastMsgBlocks, setLastMsgBlocks] = useState(null);
  const [lastMsgOpen, setLastMsgOpen] = useState(false);
  const onShowLastInTerminal = useCallback(async () => {
    if (!state.activeId) return;
    try {
      const d = await apiGet('/api/orchestrator/sessions/' + encodeURIComponent(state.activeId) + '/messages');
      const msgs = (d && d.messages) || [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i] && msgs[i].role === 'assistant') {
          setLastMsgBlocks(msgs[i].blocks || []);
          setLastMsgOpen(true);
          return;
        }
      }
      if (toast) toast('Brak wiadomości', 'warn');
    } catch (_e) {
      if (toast) toast('Nie udało się pobrać wiadomości', 'err');
    }
  }, [state.activeId, toast]);

  // Passive read-aloud: speak a 'speak' frame pushed by the backend watcher
  // (orchestrator_read_aloud.py) when an assistant turn typed straight into
  // ttyd completes. De-dups via the SHARED spokenKeysRef, keyed by the
  // assistant uuid the watcher sends, so a reconnect replay — or an overlap
  // with the composer auto-speak effect — never double-reads a turn. The raw
  // markdown is stripped inside tts.speakBlocks, same as speakMessage.
  // Holds the conversation hook's return (assigned right below the
  // useConversation call) — a ref, NOT a dep: the conversation object is a
  // fresh memo ~20×/s while the mic level animates, and identity-chasing it
  // here would rebuild handleSpeakEvent constantly.
  const conversationRef = useRef(null);
  const handleSpeakEvent = useCallback(({ text, key, end_flow }) => {
    if (!tts || !tts.supported) return;
    // Stable per-session flow key so every block of the turn QUEUES (appends/
    // reopens) instead of cutting the previous one off; the canned ack seeds
    // the same key, and the backend's end_flow marker finalises the queue so
    // 'speaking' clears once it drains.
    const flowKey = 'ra:' + (state.activeId || 'x');
    const conv = conversationRef.current;
    const convActive = !!(conv && conv.active && viewMode === 'terminal');
    if (end_flow) {
      const finalize = () => {
        if (typeof tts.endStream === 'function') { try { tts.endStream({ key: flowKey }); } catch (_e) { /* ignore */ } }
      };
      if (convActive && typeof conv.feedTurnEnd === 'function') { conv.feedTurnEnd(finalize); return; }
      finalize();
      return;
    }
    if (!text || !key) return;
    if (spokenKeysRef.current.has(key)) return;
    const play = () => {
      if (typeof tts.streamChunk === 'function') tts.streamChunk(text, { key: flowKey });
      else tts.speakBlocks([{ text }], { key });  // fallback if streaming unavailable
    };
    // Conversation modal active → DELEGATE playback to its state machine
    // (play now / defer while the user speaks / drop a barged turn). Sits
    // BEFORE the on-voice latch: the modal must hear every block even when
    // the latch points at another session (e.g. modal opened mid-turn).
    // Every delegated block ends up played, deferred or explicitly dropped,
    // so marking it spoken here is correct (and prevents replays).
    if (convActive && typeof conv.feedSpeak === 'function') {
      spokenKeysRef.current.add(key);
      conv.feedSpeak(play);
      return;
    }
    // 'on-voice': read EVERY block of the dictated session — intermediate
    // progress prose, the final answer, AND the result reported back after
    // heavy work was offloaded to a background subagent (a later, unlatched
    // turn). The latch is NOT consumed (persists for the whole voice
    // conversation, cleared on session/mode change); spokenKeysRef de-dup stops
    // double-reads. 'always' reads everything regardless.
    if (voiceMode === 'on-voice' && terminalVoiceTurnRef.current !== state.activeId) {
      spokenKeysRef.current.add(key);  // not this voice session → mark + skip
      return;
    }
    spokenKeysRef.current.add(key);
    // Queue (append) so intermediate blocks read sequentially without cut-off.
    play();
  }, [tts, voiceMode, state.activeId, viewMode]);

  // Refs so the once-mounted listener below always reads the latest tts +
  // voiceMode without re-binding on every render.
  const _ttsRef = useRef(tts); _ttsRef.current = tts;
  const _voiceModeRef = useRef(voiceMode); _voiceModeRef.current = voiceMode;

  // The terminal mic (orchestrator-terminal-preview.jsx) signals voice-dictated
  // input here. Two jobs: (1) latch this session as a voice conversation so
  // 'on-voice' reads its turns (incl. the background-subagent result), and
  // (2) play an INSTANT cached ack ("Już robię.") to mask claude's think-time —
  // dictation is a real user gesture, so TTS is already unlocked. The real reply
  // (a few seconds later) cancels+replaces this ack via speakBlocks.
  useEffect(() => {
    const onVoiceInput = (e) => {
      const sid = e && e.detail && e.detail.sessionId;
      if (!sid) return;
      terminalVoiceTurnRef.current = sid;
      const t = _ttsRef.current;
      if (_voiceModeRef.current !== 'manual' && t && t.supported) {
        try {
          // New voice turn: seed this turn's TTS queue with the ack as its
          // first chunk — the assistant's prose blocks then APPEND to the same
          // flow key (key 'ra:'+sid), so nothing cuts off. Cancel only a
          // SILENT stale flow: cancelling mid-playback would clip an async
          // result that's still draining (streamChunk reopens/queues anyway).
          if (typeof t.cancel === 'function' && !t.speaking) t.cancel();
          if (typeof t.streamChunk === 'function') t.streamChunk('Już robię.', { key: 'ra:' + sid });
          else t.speakBlocks([{ text: 'Już robię.' }], { key: 'ack' });
        } catch (_e) { /* ignore */ }
      }
    };
    window.addEventListener('hub:terminal-voice-input', onVoiceInput);
    return () => window.removeEventListener('hub:terminal-voice-input', onVoiceInput);
  }, []);

  // The terminal also signals when the user TYPES by hand (printable keystroke,
  // not a dictation paste). That means they took keyboard control, so drop the
  // on-voice latch: 'on-voice' read-aloud must NOT auto-speak the reply to a
  // typed turn. (The latch is session-sticky so async background-subagent
  // results — which arrive with no typing — are still read; manual typing is
  // the one signal that ends the voice conversation.)
  useEffect(() => {
    const onTyped = () => { terminalVoiceTurnRef.current = null; };
    window.addEventListener('hub:terminal-keyboard-input', onTyped);
    return () => window.removeEventListener('hub:terminal-keyboard-input', onTyped);
  }, []);

  // iOS/Safari autoplay: prime the TTS unlock on the FIRST user gesture so auto
  // read-aloud ('always'/'on-voice') can play after a cold PWA reopen without a
  // per-turn tap. unlock() is idempotent (persisted to localStorage). A ref
  // holds the latest tts so the listener binds once on mount.
  const _ttsForUnlockRef = useRef(tts);
  _ttsForUnlockRef.current = tts;
  useEffect(() => {
    const onFirstGesture = () => {
      const t = _ttsForUnlockRef.current;
      if (t && typeof t.unlock === 'function') { try { t.unlock(); } catch (_e) { /* ignore */ } }
      window.removeEventListener('pointerdown', onFirstGesture);
      window.removeEventListener('touchstart', onFirstGesture);
    };
    window.addEventListener('pointerdown', onFirstGesture);
    window.addEventListener('touchstart', onFirstGesture);
    return () => {
      window.removeEventListener('pointerdown', onFirstGesture);
      window.removeEventListener('touchstart', onFirstGesture);
    };
  }, []);

  // Open the read-aloud SSE only in TERMINAL view — that's the gap it fills
  // (ttyd turns bypass the composer's auto-speak). Gating to terminal view
  // avoids double-reading composer turns, which the composer auto-speak effect
  // already handles in chat view (its de-dup keys by turn_idx, not the watcher's
  // uuid, so they can't share spokenKeysRef). Flag on + non-'manual' required;
  // 'always' reads every turn, 'on-voice' only dictated ones (gated in
  // handleSpeakEvent). window.useReadAloud loads before this file → stable hooks.
  // (useReadAloud is wired BELOW the conversation hook — its `enabled` needs
  // conversation.active so the modal can force the SSE open even in 'manual'.)

  // Streaming TTS was tried but the user-facing text morphs between SSE
  // events (each `structured_blocks` finalisation REPLACES the prose layout
  // emitted by deltas), causing audible cutoffs and re-reads. Reverted to
  // post-finalize playback only — the auto-speak effect below fires once
  // the full envelope lands in `state.messages`. Latency is ~250-600 ms
  // dominated by ElevenLabs Flash TTFB.

  // Cancel speech when the user *transitions* into manual mode mid-playback.
  // We need `tts` in the dep array so the effect can read its current ref,
  // but bare `tts` membership re-fires every time `useTts` returns a new memo
  // identity (which happens the moment speakBlocks calls setSpeaking(true) /
  // setActiveKey(key) — i.e. immediately after starting playback). In default
  // voiceMode='manual', that re-fire would cancel the playback we just kicked
  // off, killing the speaker-button + any future auto-speak. Guard with a
  // prev-mode ref so cancel only fires on the actual non-manual → manual
  // transition.
  const _prevVoiceModeRef = useRef(voiceMode);
  useEffect(() => {
    const prev = _prevVoiceModeRef.current;
    _prevVoiceModeRef.current = voiceMode;
    // A voice-mode change voids any pending on-voice latch — otherwise a latch
    // set under 'on-voice' could bias the next turn after switching modes.
    if (prev !== voiceMode) terminalVoiceTurnRef.current = null;
    if (prev !== 'manual' && voiceMode === 'manual' && tts && tts.cancel) {
      try { tts.cancel(); } catch (e) {}
    }
  }, [voiceMode, tts]);

  // Mark a message key as already-spoken so the auto-speak effect doesn't
  // replay it. Used by the conversation hook to claim ownership of every
  // assistant turn it speaks via its own playback path. Without this,
  // voiceOutput='always' would re-speak the SAME assistant message through
  // tts.speakBlocks the moment the conversation modal closes — duplicate
  // audio.
  const markAssistantSpoken = useCallback((key) => {
    if (key) spokenKeysRef.current.add(key);
  }, []);

  // Continuous voice conversation mode — fullscreen overlay that runs the
  // mic+VAD+STT+send+TTS+barge-in loop. Owns its own playback when active so
  // we suppress the regular auto-speak effect below to avoid double-speaking.
  const conversation = (window.useConversation || (() => null))({
    send,
    onCancelStream: onCancel,
    isStreaming,
    messages: state.messages,
    // ── terminalMode (ttyd / driving): send = paste+submit into the live tmux
    // pane (TerminalLiveView owns the iframe → bridged via a window event);
    // playback = the read-aloud flow key on the page tts (the hook routes /
    // defers / drops via feedSpeak — see handleSpeakEvent's delegation).
    terminalMode: viewMode === 'terminal',
    sendToTerminal: (text) => {
      // detail.ok is set SYNCHRONOUSLY by TerminalLiveView's listener
      // (dispatchEvent runs listeners in-tick) — false/unset means the paste
      // never reached an xterm; throwing lets the hook speak the failure and
      // NOT count a phantom in-flight turn.
      const detail = { text, ok: false };
      window.dispatchEvent(new CustomEvent('hub:conversation-terminal-send', { detail }));
      if (!detail.ok) throw new Error('terminal not ready');
    },
    getTtsSpeaking: () => !!(_ttsRef.current && _ttsRef.current.speaking),
    speakText: (text) => {
      const t = _ttsRef.current;
      if (!t || !t.supported) return;
      const k = 'ra:' + (state.activeId || 'x');
      if (typeof t.streamChunk === 'function') {
        t.streamChunk(text, { key: k });
        if (typeof t.endStream === 'function') { try { t.endStream({ key: k }); } catch (_e) { /* ignore */ } }
      } else {
        t.speakBlocks([{ text }], { key: 'conv-sys' });
      }
    },
    readAloudAvailable: !!readAloudFlag,
    language: ttsSettings.sttLanguage || 'auto',
    echoMode: ttsSettings.voiceConvEcho || 'aec',
    silenceMs: ttsSettings.voiceConvSilenceMs || 1200,
    bargeThreshold: ttsSettings.voiceConvBargeInThreshold || 0.045,
    bargeMinDurationMs: ttsSettings.voiceConvBargeMinDurationMs || 120,
    ttsEngine: ttsSettings.ttsEngine || 'elevenlabs',
    ttsVoice: ttsSettings.ttsVoice || null,
    ttsUnlock: tts && tts.unlock,
    markAssistantSpoken,
    cancelTtsExternal: tts && tts.cancel,
    speakBlocksFallback: tts && tts.speakBlocks ? ((rawText, options) => {
      // Browser-engine fallback: speakBlocks uses speechSynthesis. We poll
      // tts.speaking to know when it drains since useTts doesn't expose an
      // onDone hook.
      try { tts.speakBlocks([{ text: rawText }], { key: 'conv-' + Date.now() }); }
      catch (e) { if (options && options.onDone) options.onDone(); return; }
      if (!options || !options.onDone) return;
      const id = setInterval(() => {
        if (!tts.speaking) { clearInterval(id); options.onDone(); }
      }, 200);
    }) : null,
    ttsSupported: !!(tts && tts.supported),
    msgKey: _msgKey,
  });
  conversationRef.current = conversation;

  // Read-aloud SSE: open only in TERMINAL view — that's the gap it fills
  // (ttyd turns bypass the composer's auto-speak; chat view de-dups by
  // turn_idx, a different namespace). Required: server flag + either a
  // non-'manual' voice mode OR an ACTIVE conversation modal (the modal is
  // useless deaf, so it forces the channel open regardless of voiceOutput).
  // Wired below useConversation because `enabled` reads conversation.active.
  (window.useReadAloud || (() => {}))({
    sessionId: state.activeId,
    enabled: !!(readAloudFlag && viewMode === 'terminal' && state.activeId
      && (voiceMode !== 'manual' || (conversation && conversation.active))),
    onSpeak: handleSpeakEvent,
    onGiveUp: () => {
      // Eyes-free: an active conversation must HEAR about a dead channel —
      // and the phrase must go through the conversation's audio matrix
      // (speakSystem), never straight into tts: a direct play would land in
      // an OPEN mic during LISTENING and come back as a garbage send.
      const conv = conversationRef.current;
      if (conv && conv.active && typeof conv.speakSystem === 'function') {
        try { conv.speakSystem('Straciłem połączenie z serwerem, próbuję dalej.', { forceListen: true }); } catch (_e) { /* ignore */ }
      }
      if (toast) toast('Auto-czytanie niedostępne', 'err');
    },
  });

  // Auto-speak the latest assistant message when the mode allows. Runs after
  // every messages mutation, but spokenKeysRef de-dupes — the effect is a
  // no-op for messages we've already read (or messages that arrived while the
  // mode was different).
  useEffect(() => {
    if (!tts || !tts.supported) return;
    if (voiceMode === 'manual') return;
    // When the fullscreen conversation modal is active it owns playback.
    if (conversation && conversation.active) return;
    const msgs = state.messages || [];
    if (!msgs.length) return;
    // Walk backwards: find latest assistant message; the user message
    // immediately preceding it (skipping control echoes) tells us whether
    // the turn was voice-driven.
    let lastAssistantIdx = -1;
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      const m = msgs[i];
      if (m && m.role === 'assistant') { lastAssistantIdx = i; break; }
    }
    if (lastAssistantIdx < 0) return;
    const assistant = msgs[lastAssistantIdx];
    const key = _msgKey(assistant);
    if (!key || spokenKeysRef.current.has(key)) return;
    if (voiceMode === 'on-voice') {
      let triggeringUser = null;
      for (let i = lastAssistantIdx - 1; i >= 0; i -= 1) {
        const m = msgs[i];
        if (m && m.role === 'user' && !isControlEcho(m)) { triggeringUser = m; break; }
      }
      if (!triggeringUser || !triggeringUser.from_voice) {
        // Mark spoken anyway so we don't keep re-checking on every render.
        spokenKeysRef.current.add(key);
        return;
      }
    }
    speakMessage(assistant);
  }, [state.messages, voiceMode, tts, speakMessage, _msgKey, conversation]);

  // ---- Per-message actions: copy, reply, pin ----------------------------
  const replyingMsg = useMemo(() => {
    if (!replyingToKey) return null;
    return (state.messages || []).find(m => _msgKey(m) === replyingToKey) || null;
  }, [replyingToKey, state.messages, _msgKey]);

  const togglePin = useCallback(async (msg) => {
    const turnIdx = msg && msg.turn_idx;
    if (typeof turnIdx !== 'number' || turnIdx < 0) {
      if (toast) toast('Wiadomość nie jest jeszcze trwała — odśwież i spróbuj ponownie', 'err');
      return;
    }
    const cur = (activeSession && activeSession.pinned_turn_idxs) || [];
    const next = cur.includes(turnIdx)
      ? cur.filter(i => i !== turnIdx)
      : [...cur, turnIdx].sort((a, b) => a - b);
    try {
      await apiSend('/api/orchestrator/sessions/' + state.activeId, 'PATCH', { pinned_turn_idxs: next });
      await refreshSessions();
    } catch (e) {
      if (toast) toast('Pin nieudany: ' + (e && e.message ? e.message : 'unknown'), 'err');
    }
  }, [state.activeId, activeSession, refreshSessions, toast]);

  const copyMessage = useCallback(async (msg) => {
    const text = (msg && msg.blocks) ? blocksToCopyText(msg.blocks) : '';
    if (!text) { if (toast) toast('Nic do skopiowania', 'err'); return; }
    const ok = await window.HubClipboard.copyText(text);
    if (toast) toast(ok ? 'Skopiowano' : 'Kopiowanie nieudane', ok ? 'ok' : 'err');
  }, [toast]);

  const replyTo = useCallback((msg) => {
    setReplyingToKey(_msgKey(msg));
    if (inputRef.current) inputRef.current.focus();
  }, [_msgKey]);

  const messageActions = useMemo(() => ({
    copyMessage,
    replyTo,
    togglePin,
    isPinned: (msg) => msg && typeof msg.turn_idx === 'number' && pinnedTurnIdxs.has(msg.turn_idx),
    pinDisabled: (msg) => !msg || typeof msg.turn_idx !== 'number' || msg.turn_idx < 0,
  }), [copyMessage, replyTo, togglePin, pinnedTurnIdxs]);

  const onSuggest = useCallback((s) => { setInput(s); if (inputRef.current) inputRef.current.focus(); }, []);

  // Reference an artifact ("Skomentuj") into the session's ACTIVE surface so
  // the user can ask claude about it. Routes by the session's preferred view
  // (terminal is the default; only an explicit 'chat' entry in the view-mode
  // map flips it):
  //   • terminal → switch to the live tmux view and paste the text
  //     SERVER-SIDE into the pane via POST /term/paste (tmux paste-buffer, no
  //     Enter). The artifact's local path lands at claude's input exactly like
  //     an uploaded attachment; the user adds context / submits. Server-side
  //     paste is timing-independent — ttyd just reflects the pane — so it
  //     works even though TerminalLiveView is mid-remount (poking the browser
  //     xterm right after the view switch raced the not-yet-ready xterm and
  //     dropped the paste — UAT 2026-05-29).
  //   • chat → append to the composer on its own line + focus.
  // Fired by the gallery's "Skomentuj" via `orchestrator:artifact-reference`
  // (separate component tree, same window-CustomEvent pattern as
  // `orchestrator:artifacts-changed`).
  const referenceArtifact = useCallback((text) => {
    if (!text) return;
    const sid = state.activeId;
    if (!sid) return;
    let preferChat = false;
    try {
      const map = JSON.parse(localStorage.getItem('hub-orchestrator-view-mode') || '{}');
      preferChat = map[sid] === 'chat';
    } catch (_e) { /* default terminal */ }
    if (preferChat) {
      setViewMode('chat');
      setInput(prev => (prev && prev.trim() ? prev.replace(/\s*$/, '') + '\n' + text : text));
      setTimeout(() => { if (inputRef.current) { try { inputRef.current.focus(); } catch (e) { /* ignore */ } } }, 60);
      if (toast) toast('Wstawiono do inputu', 'ok');
    } else {
      setViewMode('terminal');
      apiSend('/api/orchestrator/sessions/' + encodeURIComponent(sid) + '/term/paste', 'POST', { text })
        .then(() => { if (toast) toast('Wstawiono do terminala', 'ok'); })
        .catch((e) => { if (toast) toast('Nie udało się wkleić: ' + ((e && e.message) || e), 'err'); });
    }
  }, [state.activeId, toast]);
  useEffect(() => {
    const onRef = (e) => { const t = e && e.detail && e.detail.text; if (t) referenceArtifact(t); };
    // `insert-input` kept as a back-compat alias for any other caller.
    window.addEventListener('orchestrator:artifact-reference', onRef);
    window.addEventListener('orchestrator:insert-input', onRef);
    return () => {
      window.removeEventListener('orchestrator:artifact-reference', onRef);
      window.removeEventListener('orchestrator:insert-input', onRef);
    };
  }, [referenceArtifact]);

  const _branchSuffix = (activeSession && activeSession.git_branch)
    ? ' · ' + activeSession.git_branch
    : '';
  // Subtitle priority (most-recent UAT 2026-05-28):
  //   1. streaming / connection state (transient, takes precedence)
  //   2. error (sticky)
  //   3. "<msgs> msg · <ago> · <branch>"  — the per-session identity
  //      line, ALWAYS shown when an active session is loaded.
  //
  // The earlier branch that overrode this with `terminalStatus`
  // ("live (interactive, slot warm)") when viewMode === 'terminal'
  // is gone: on mobile that line buried the msg count and branch
  // (the info the user actually scans the header for) behind
  // ttyd-pool plumbing detail. terminalStatus is now only surfaced
  // in TerminalLiveView's own status row on desktop, where there's
  // room for both.
  const subtitle = isStreaming
    ? (state.connectionStatus === 'retrying' ? 'reconnecting…'
       : state.connectionStatus === 'polling' ? 'polling for updates…'
       : 'streaming…')
    : (state.error ? state.error
       : activeSession ? (activeSession.msg_count || 0) + ' msg · ' + relTime(activeSession.updated_at) + _branchSuffix : 'idle');
  const header = (
    <div style={{
      // On compact, the mobile shell no longer renders a global top
      // bar (its hostname/uptime info duplicated the drawer header).
      // So this is the ONLY header on screen — reserve safe-area-top
      // for the iOS notch AND safe-area-left/right for landscape
      // Dynamic Island. The hamburger sits on the left at 8 px, so
      // the left inset matters for landscape on notched phones.
      padding: compact
        ? 'calc(8px + env(safe-area-inset-top)) calc(8px + env(safe-area-inset-right)) 8px calc(8px + env(safe-area-inset-left))'
        : '20px 24px 14px',
      minHeight: compact ? 0 : 71,
      borderBottom: '1px solid var(--hairline)',
      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexShrink: 0,
      boxSizing: 'border-box',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: compact ? 6 : 12, minWidth: 0 }}>
        {compact && typeof onOpenDrawer === 'function' && (
          <IconButton icon="menu" label="Menu" size={32} onClick={onOpenDrawer} />
        )}
        {!compact && (
          <div style={{
            width: 36, height: 36, borderRadius: 'var(--r-md)', flexShrink: 0,
            background: 'var(--accent-soft)', color: 'var(--accent)',
            border: '1px solid var(--accent-line)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: agentIcon ? 20 : 18, lineHeight: 1,
          }}>
            {agentIcon ? agentIcon : <Icon name="bot" size={18} />}
          </div>
        )}
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 'var(--t-body)', fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {agentName}{activeSession && activeSession.title ? ` · ${activeSession.title}` : ''}
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2, flexWrap: 'wrap' }}>
            <StatusDot status={isStreaming ? 'warn' : 'ok'} />
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{subtitle}</span>
            <RunnerModeBadge runnerMode={state.lastRunnerMode} />
            {/* Terminal status relocated here from the panel (desktop). */}
            {!compact && viewMode === 'terminal' && terminalStatus && (
              <>
                <span style={{ color: 'var(--fg-4)' }}>·</span>
                <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{terminalStatus.label}</span>
                {terminalStatus.detail && (
                  <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>· {terminalStatus.detail}</span>
                )}
              </>
            )}
          </div>
        </div>
      </div>
      <HeaderActions
        compact={compact} activeSession={activeSession}
        sessionId={state.activeId}
        onRename={() => onRename(state.activeId)}
        onCompact={compactSession} compacting={compacting}
        onClear={() => onDelete(state.activeId)}
        onCloseSession={() => onCloseSession(state.activeId)}
        onOpenSessions={() => setSheetOpen(true)}
        onNew={onNew}
        onTogglePin={onTogglePin}
        onTogglePersistent={onTogglePersistent}
        onTogglePanel={() => setPanelOpen(p => !p)} panelOpen={panelOpen}
        pinnedTurnIdxs={pinnedTurnIdxs}
        pinPopoverOpen={pinPopoverOpen}
        setPinPopoverOpen={setPinPopoverOpen}
        messages={state.messages}
        msgKey={_msgKey}
        togglePin={togglePin}
        onModelChange={onModelChange}
        onOpenConversation={() => {
          // Force terminal view BEFORE starting: both the read-aloud `enabled`
          // gate and handleSpeakEvent's convActive check require
          // viewMode==='terminal'. A session whose last view was 'chat'
          // (localStorage override) would otherwise open the modal with the
          // audio-return path disabled — the driver would hear nothing.
          if (viewMode !== 'terminal') setViewMode('terminal');
          if (conversation) conversation.start();
        }}
        conversationSupported={!!conversation && !!(tts && tts.supported)}
        viewMode={viewMode}
        onToggleViewMode={toggleViewMode}
        onShowGallery={showGallery}
        onOpenIssues={onOpenIssues}
        onOpenDrawer={onOpenDrawer}
      />
    </div>
  );

  const hint = state.error ? state.error : (isStreaming ? 'agent thinking…' : 'shift+return for newline');
  const chatColumn = (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, minWidth: 0, position: 'relative' }}>
      {header}
      <AgentTabs
        groups={agentGroups}
        activeKey={currentAgentKey}
        onSelect={onSelectSession}
        iconFor={iconForLibId}
        compact={compact}
        refresh={refreshSessions}
        refreshPool={refreshPool}
        runningById={runningSet}
        applyRemovalDecision={applyRemovalDecision}
        sessionActions={{ rename: onRename, close: onCloseSession, delete: onDelete }}
      />
      {compacting && (
        <div style={{
          position: 'absolute', inset: 0, zIndex: 50,
          background: 'rgba(14, 15, 18, 0.7)', backdropFilter: 'blur(2px)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          flexDirection: 'column', gap: 12,
          pointerEvents: 'auto',
        }}>
          <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg)', fontWeight: 500 }}>
            Compactowanie sesji…
          </div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
            Claude streszcza rozmowę i przenosi zadania. ~30-60s.
          </div>
        </div>
      )}
      {viewMode === 'gallery' && window.ArtifactGallery ? (
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          <window.ArtifactGallery sessionId={state.activeId} scope="session" compact={compact} />
        </div>
      ) : viewMode === 'terminal' && state.activeId && window.TerminalLiveView ? (
        <window.TerminalLiveView sessionId={state.activeId} compact={compact} keyboardOpen={keyboardOpen} onStatusChange={compact ? undefined : setTerminalStatus} tts={tts} onSpeakLast={onSpeakLastInTerminal} onShowLast={onShowLastInTerminal} featureFlagEnabled={readAloudFlag} isSpeaking={!!(tts && tts.speaking)} conversationActive={!!(conversation && conversation.active)} />
      ) : (
      <ChatTranscript
        scrollRef={scrollRef} messages={state.messages} streaming={state.streaming}
        suggestions={SUGGESTIONS} onSuggest={onSuggest} compact={compact}
        sessionId={state.activeId} onOpenLightbox={setLightboxFile}
        tts={tts} speakMessage={speakMessage} msgKey={_msgKey}
        messageActions={messageActions} agentLabel={agentLabel}
      />
      )}
      {viewMode === 'chat' && (
      <>
      {window.ScrollToBottomFab && (
        <window.ScrollToBottomFab scrollRef={scrollRef} compact={compact} />
      )}
      {replyingMsg && (
        <div style={{
          margin: compact ? '0 14px 6px' : '0 24px 6px',
          padding: '8px 12px',
          background: 'var(--surface-2)', border: '1px solid var(--accent-line)',
          borderRadius: 'var(--r-control)', display: 'flex', alignItems: 'center', gap: 8,
          fontSize: 'var(--t-cap)', color: 'var(--fg-2)',
        }}>
          <Icon name="corner-up-left" size={14} stroke={1.8} color="var(--accent)" />
          <span style={{
            flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            ↩ Odpowiedź na: <i>{previewText(
              (replyingMsg.blocks || []).map(b => (b && (b.text || b.content)) || '').join(' '),
              80
            )}</i>
          </span>
          <button onClick={() => setReplyingToKey(null)}
            style={{
              marginLeft: 'auto', background: 'transparent', border: 'none',
              color: 'var(--fg-3)', cursor: 'pointer', padding: 4, flexShrink: 0,
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            }}
            aria-label="Anuluj odpowiedź" title="Anuluj odpowiedź">
            <Icon name="close" size={14} stroke={1.8} />
          </button>
        </div>
      )}
      <QueueStrip
        items={queue} compact={compact}
        onEdit={editQueuedMessage} onRemove={removeQueuedMessage}
      />
      <MessageInput
        value={input} onChange={setInput} onSend={submit}
        disabled={!state.activeId}
        isStreaming={isStreaming} onCancel={onCancel}
        compact={compact} inputRef={inputRef} hint={hint}
        pendingAttachments={pendingAttachments}
        onFilesPicked={onFilesPicked}
        onRemovePending={removePending}
        onAttachClick={onAttachClick}
        fileInputRef={fileInputRef}
        onRecordingStart={onRecordingStart}
        onVoiceFinalDetected={onVoiceFinalDetected}
        contextTokens={activeSession && activeSession.last_context_tokens}
        contextModel={activeSession && activeSession.last_model}
      />
      </>
      )}
    </div>
  );

  const sessionListProps = {
    sessions: filteredSessions, activeId: state.activeId, onSelect: onSelectSession, onNew, poolStatusById,
    runningById: runningSet,
    // Per-row actions for the desktop right-click context menu (moved out of
    // the chat header). Each takes the specific session it was invoked on.
    onRenameSession: (sess) => onRename(sess.id),
    onDeleteSession: (sess) => onDelete(sess.id),
    onCloseSession: (sess) => onCloseSession(sess.id),
    onTogglePinSession: (sess) => onTogglePin(sess),
    onTogglePersistentSession: (sess) => onTogglePersistent(sess),
  };
  const lightbox = lightboxFile ? <Lightbox file={lightboxFile} onClose={() => setLightboxFile(null)} /> : null;
  const conversationModal = (conversation && window.ConversationModal) ? (
    <window.ConversationModal
      open={!!conversation.active}
      onClose={() => conversation.stop()}
      conversation={conversation}
      recentMessages={viewMode === 'terminal' ? [] : state.messages}
      msgKey={_msgKey}
      sessionId={state.activeId}
      activeModel={activeSession && activeSession.model}
      onModelChange={onModelChange}
    />
  ) : null;
  const lastMsgModal = window.LastMessageModal ? (
    <window.LastMessageModal
      open={lastMsgOpen}
      onClose={() => setLastMsgOpen(false)}
      blocks={lastMsgBlocks}
      compact={compact}
      onCopy={() => copyMessage({ blocks: lastMsgBlocks })}
    />
  ) : null;
  const renameTitle = (state.sessions || []).find(s => s.id === renameTarget)?.title;
  const renameModal = (
    <Modal open={!!renameTarget} onClose={() => setRenameTarget(null)} title="Rename session">
      <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <input
          autoFocus value={renameValue} onChange={e => setRenameValue(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') submitRename(); if (e.key === 'Escape') setRenameTarget(null); }}
          placeholder="New title"
          style={{
            background: 'var(--surface-3)', border: '1px solid var(--hairline)',
            borderRadius: 'var(--r-control)', padding: '10px 12px', fontSize: 'var(--t-md)', color: 'var(--fg)',
            fontFamily: 'inherit', outline: 'none',
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="ghost" onClick={() => setRenameTarget(null)}>Cancel</Button>
          <Button onClick={submitRename}>Save</Button>
        </div>
      </div>
    </Modal>
  );
  const newSessionModal = (
    <Modal open={confirmNew} onClose={() => setConfirmNew(false)} title="Nowa sesja">
      <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg-2)', lineHeight: 1.5 }}>
          Rozpocząć nową sesję{agentName && agentName !== 'Global' ? <> w agencie <strong style={{ color: 'var(--fg)' }}>{agentName}</strong></> : null}?
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="ghost" onClick={() => setConfirmNew(false)}>Anuluj</Button>
          <Button onClick={createSession}>Nowa sesja</Button>
        </div>
      </div>
    </Modal>
  );
  const artifactModal = (openArtifact && window.ArtifactViewerModal) ? (
    <window.ArtifactViewerModal
      artifact={openArtifact}
      scope={{ sessionId: state.activeId }}
      open
      onClose={() => setOpenArtifact(null)}
    />
  ) : null;
  const deleteTitle = (state.sessions || []).find(s => s.id === deleteTarget)?.title;
  const deleteModal = (
    <Modal open={!!deleteTarget} onClose={() => setDeleteTarget(null)} title="Delete session">
      <div style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg-2)', lineHeight: 1.5 }}>
          Delete <strong style={{ color: 'var(--fg)' }}>{deleteTitle || 'this session'}</strong> and its full transcript? This can't be undone.
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button variant="ghost" onClick={() => setDeleteTarget(null)}>Cancel</Button>
          <Button variant="danger" onClick={submitDelete}>Delete</Button>
        </div>
      </div>
    </Modal>
  );

  if (compact) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, position: 'relative' }}>
        {chatColumn}
        <BottomSheet open={sheetOpen} onClose={() => setSheetOpen(false)} title="Sessions" height="100%">
          <SessionList {...sessionListProps} onClose={() => setSheetOpen(false)} compact />
        </BottomSheet>
        {renameModal}{deleteModal}{newSessionModal}{lightbox}{conversationModal}{artifactModal}{lastMsgModal}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', height: '100%', minHeight: 0 }}>
      {chatColumn}
      {panelOpen && (
        <div style={{ width: 280, flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0, borderLeft: '1px solid var(--hairline)' }}>
          <SessionList {...sessionListProps} onClose={() => setPanelOpen(false)} compact={false} />
        </div>
      )}
      {renameModal}{deleteModal}{newSessionModal}{lightbox}{conversationModal}{artifactModal}{lastMsgModal}
    </div>
  );
}

Object.assign(window, { OrchestratorView, SessionList, normalizeStructuredBlock });
