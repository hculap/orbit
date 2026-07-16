// Transcript view for the Orchestrator chat: Meta header, MessageBubble,
// StreamingBubble, SuggestionChips, ChatTranscript. Loaded after
// orchestrator-blocks/-attachments and before orchestrator.jsx so the latter
// picks these up via window globals.

const { useMemo: _useMemo } = React;

function fmtTime(ts) {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(d.getTime())) return '';
  return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
}

function _stripAttachedBlock(text) {
  if (!text) return '';
  return String(text)
    .replace(/<attached>\s*([\s\S]*?)\s*<\/attached>/, '')
    // Voice-mode hint emitted by the conversation modal so claude knows to
    // reply in plain readable prose. Hidden in the user bubble for clarity.
    .replace(/<voice-mode>\s*([\s\S]*?)\s*<\/voice-mode>\s*/, '')
    .trimEnd();
}

function _attachmentsFromPaths(paths) {
  if (!Array.isArray(paths)) return [];
  return paths.map(p => {
    const name = String(p || '').split('/').pop() || '';
    const isImg = /\.(png|jpe?g|gif|webp|svg|heic|bmp|avif)$/i.test(name);
    return { name, server_path: p, kind: isImg ? 'image' : 'file' };
  });
}

function Meta({ who, t, action, agentLabel }) {
  // who='agent' uses agentLabel (e.g. "My Project agent" / "Dom agent" /
  // "Global agent") when provided. Falls back to a generic "agent" so any
  // legacy callsite that doesn't thread the prop still renders something
  // meaningful.
  const text = who === 'agent' ? (agentLabel || 'agent') : 'you';
  return (
    <div className="mono" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
      <span style={{
        width: 18, height: 18, borderRadius: 'var(--r-sm)',
        background: who === 'agent' ? 'var(--accent-soft)' : 'var(--surface-3)',
        color: who === 'agent' ? 'var(--accent)' : 'var(--fg-2)',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        border: '1px solid ' + (who === 'agent' ? 'var(--accent-line)' : 'var(--hairline)'),
        fontSize: 'var(--t-2xs)', fontWeight: 600,
      }}>{who === 'agent' ? '◆' : 'S'}</span>
      <span style={{
        color: who === 'agent' ? 'var(--accent)' : 'var(--fg-2)',
        fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase',
      }}>{text}</span>
      {t && <span>{t}</span>}
      {action && <span style={{ marginLeft: 'auto' }}>{action}</span>}
    </div>
  );
}

// Play/stop pill for assistant message bubbles. `active` flips the icon to a
// stop square when this particular message is the one currently being spoken.
// Hidden entirely when the runtime can't do TTS at all.
function SpeakButton({ active, onPlay, onStop, supported }) {
  if (!supported) return null;
  const handle = (e) => {
    e.stopPropagation();
    if (active) onStop && onStop();
    else onPlay && onPlay();
  };
  return (
    <button onClick={handle} aria-label={active ? 'Zatrzymaj czytanie' : 'Przeczytaj na głos'}
      title={active ? 'Zatrzymaj czytanie' : 'Przeczytaj na głos'}
      style={{
        width: 22, height: 22, borderRadius: 'var(--r-sm)', padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        background: active ? 'var(--accent-soft)' : 'transparent',
        color: active ? 'var(--accent)' : 'var(--fg-3)',
        border: '1px solid ' + (active ? 'var(--accent-line)' : 'transparent'),
        cursor: 'pointer', flexShrink: 0,
        opacity: active ? 1 : 0.65,
        transition: 'opacity .12s, color .12s, background .12s',
      }}
      onMouseEnter={(e) => { if (!active) e.currentTarget.style.opacity = '1'; }}
      onMouseLeave={(e) => { if (!active) e.currentTarget.style.opacity = '0.65'; }}>
      <Icon name={active ? 'square' : 'volume'} size={12} stroke={1.8} />
    </button>
  );
}

// Block kinds that disappear when the user has `showToolActions=false` in
// settings. Mirrored from BlockRenderer; kept in sync manually since the two
// files don't share a runtime module boundary.
const _TOOL_ACTION_KINDS = new Set(['tool_use', 'tool_result', 'thinking']);

// Skip empty assistant bubbles so claude's bare meta-headers between tool
// round-trips don't pollute the transcript. Errors are always shown.
function blockHasContent(b) {
  if (!b || !b.kind) return false;
  if (b.kind === 'text' || b.kind === 'markdown' || b.kind === 'code' || b.kind === 'thinking') {
    const t = b.text != null ? b.text : (b.content || '');
    return !!String(t).trim();
  }
  if (b.kind === 'tool_result') {
    if (b.is_error) return true;
    const out = b.stdout != null ? b.stdout : (b.text != null ? b.text : (b.output || ''));
    return !!String(out).trim();
  }
  return true;
}

function messageHasVisibleContent(msg) {
  if (!msg) return false;
  if (msg.role === 'user') return true;
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
  return blocks.some(blockHasContent);
}

// Resolve the parent's per-msg action bundle (predicate fns) into the
// flat shape MessageActionRow expects (booleans for `isPinned`/`pinDisabled`).
function _resolveMessageActions(actions, msg) {
  if (!actions) return null;
  return {
    ...actions,
    isPinned: typeof actions.isPinned === 'function' ? !!actions.isPinned(msg) : !!actions.isPinned,
    pinDisabled: typeof actions.pinDisabled === 'function' ? !!actions.pinDisabled(msg) : !!actions.pinDisabled,
  };
}

function MessageBubbleImpl({ msg, sessionId, onOpenLightbox, showMeta = true, tightBelow = false, toolResultMap, tts, speakMessage, msgKey, messageActions, agentLabel }) {
  const [hubSettings] = window.useSettings();
  const hideToolActions = hubSettings && hubSettings.showToolActions === false;
  const isUser = msg.role === 'user';
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
  const t = fmtTime(msg.ts);
  const myKey = (typeof msgKey === 'function') ? msgKey(msg) : null;
  const bubbleId = myKey ? ('msg-' + myKey) : undefined;
  if (isUser) {
    const textBlock = blocks.find(b => b && (b.kind === 'text' || b.kind === 'markdown'));
    const rawText = textBlock ? (textBlock.text || textBlock.content || '') : (msg.text || '');
    const text = _stripAttachedBlock(rawText);
    const fromBlock = blocks.find(b => b && b.kind === 'attachments');
    const attachments = (msg.attachments && msg.attachments.length)
      ? msg.attachments
      : (fromBlock ? _attachmentsFromPaths(fromBlock.paths) : []);
    const sid = msg.session_id || sessionId;
    const userActions = _resolveMessageActions(messageActions, msg);
    return (
      <div id={bubbleId} className="fade-in" style={{ marginBottom: tightBelow ? 12 : 22 }}>
        <Meta who="you" t={t} />
        {text && (
          <div style={{
            fontSize: 'var(--t-md)', lineHeight: 1.55, color: 'var(--fg)',
            marginTop: 4,
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>{text}</div>
        )}
        <AttachmentRow attachments={attachments} sessionId={sid} onOpen={onOpenLightbox} />
        {window.MessageActionRow && userActions && (
          <window.MessageActionRow
            msg={msg} msgKey={myKey} role={msg.role}
            tts={tts} speakMessage={speakMessage}
            isSpeakingThis={false} ttsSupported={false}
            actions={userActions}
          />
        )}
      </div>
    );
  }
  const visibleBlocks = blocks
    .filter(blockHasContent)
    .filter(b => !hideToolActions || !_TOOL_ACTION_KINDS.has(b && b.kind));
  if (visibleBlocks.length === 0) return null;
  // Speak-aloud control — only meaningful when there's prose to read.
  const hasSpeakableProse = visibleBlocks.some(b => b && (b.kind === 'markdown' || b.kind === 'text') && (b.text || b.content));
  const ttsSupported = !!(tts && tts.supported && hasSpeakableProse && typeof speakMessage === 'function');
  const isSpeakingThis = !!(tts && tts.speaking && myKey && tts.activeKey === myKey);
  const agentActions = _resolveMessageActions(messageActions, msg);
  return (
    <div id={bubbleId} className="fade-in" style={{ marginBottom: tightBelow ? 12 : 26, minWidth: 0, maxWidth: '100%' }}>
      {showMeta && <Meta who="agent" t={t} agentLabel={agentLabel} />}
      <div style={{ marginTop: showMeta ? 6 : 0, display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>
        {visibleBlocks.map((b, i) => (
          <BlockRenderer key={i} block={b}
            toolResultMap={toolResultMap}
            sessionId={msg.session_id || sessionId} onOpenLightbox={onOpenLightbox} />
        ))}
      </div>
      {window.MessageActionRow && (
        <window.MessageActionRow
          msg={msg} msgKey={myKey} role={msg.role}
          tts={tts} speakMessage={speakMessage}
          isSpeakingThis={isSpeakingThis} ttsSupported={ttsSupported}
          actions={agentActions}
        />
      )}
    </div>
  );
}

function ThinkingDots() {
  return (
    <span style={{ display: 'inline-flex', gap: 3, alignItems: 'center', marginLeft: 4 }}>
      {[0, 1, 2].map(i => (
        <span key={i} style={{
          width: 4, height: 4, borderRadius: 'var(--r-pill)', background: 'var(--accent)',
          animation: `dots 1.2s ease-in-out ${i * 0.18}s infinite`,
        }} />
      ))}
      <style>{`@keyframes dots{0%,80%,100%{opacity:.3;transform:scale(.85)}40%{opacity:1;transform:scale(1.15)}}`}</style>
    </span>
  );
}

// Self-ticking elapsed-seconds counter for the SpawningSkeleton below. Pattern
// mirrors `RelativeTime` in sections.jsx: the interval re-renders ONLY this
// span, not the whole StreamingBubble, so the rest of the transcript stays
// quiet.
function SpawnElapsed({ startedAtMs }) {
  const [, setTick] = React.useState(0);
  React.useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  const startedAt = typeof startedAtMs === 'number' ? startedAtMs : Date.now();
  const secs = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  return <span>{secs}s</span>;
}

function StreamingBubble({ streaming, sessionId, onOpenLightbox, agentLabel }) {
  if (!streaming) return null;
  const blocks = streaming.partial_blocks || [];
  const partial = streaming.partial_text || '';
  // Spawning skeleton wins over thinking when the backend has emitted
  // `spawning` but no `init` yet — i.e. we're inside the cold-start window
  // for an interactive-mode tmux slot (~10-20 s). INIT_EVENT clears
  // `spawning` so this branch self-dissolves the moment claude is ready.
  const isSpawning = !!streaming.spawning && !streaming.init;
  const showThinking = !isSpawning && !partial && blocks.length === 0;
  const toolResultMap = {};
  for (const b of blocks) {
    if (b && b.kind === 'tool_result' && b.tool_use_id) toolResultMap[b.tool_use_id] = b;
  }
  return (
    <div className="fade-in" style={{ marginBottom: 26 }}>
      <Meta who="agent" t="" agentLabel={agentLabel} />
      <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 10 }}>
        {isSpawning && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              spawning interactive session
            </span>
            <ThinkingDots />
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
              <SpawnElapsed startedAtMs={streaming.spawning.started_at_ms} />
            </span>
          </div>
        )}
        {streaming.init && (
          <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            init · {streaming.init.model || 'claude'}
          </div>
        )}
        {blocks.map((b, i) => <BlockRenderer key={i} block={b} toolResultMap={toolResultMap} sessionId={sessionId} onOpenLightbox={onOpenLightbox} />)}
        {partial && <TextBlock text={partial} />}
        {showThinking && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>thinking</span>
            <ThinkingDots />
          </div>
        )}
      </div>
    </div>
  );
}

function SuggestionChips({ suggestions, onSuggest }) {
  return (
    <div style={{ marginTop: 24, display: 'flex', flexDirection: 'column', gap: 14, alignItems: 'flex-start' }}>
      <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-3)' }}>Fresh session — try one of these:</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {suggestions.map(s => (
          <button key={s} onClick={() => onSuggest(s)} style={{
            fontSize: 'var(--t-cap)', padding: '6px 10px', borderRadius: 'var(--r-pill)',
            background: 'var(--surface-1)', border: '1px solid var(--hairline)',
            color: 'var(--fg-2)', cursor: 'pointer',
          }}>{s}</button>
        ))}
      </div>
    </div>
  );
}

function ChatTranscript({ messages, streaming, scrollRef, suggestions, onSuggest, compact, sessionId, onOpenLightbox, tts, speakMessage, msgKey, messageActions, agentLabel }) {
  // Absorb every tool_result into the matching tool_use header (by id) and
  // drop messages whose only blocks were absorbed.
  const { visibleMessages, toolResultMap } = _useMemo(() => {
    const all = (messages || []).filter(m => !isControlEcho(m));
    const map = {};
    for (const m of all) for (const b of (m.blocks || [])) {
      if (b && b.kind === 'tool_result' && b.tool_use_id) map[b.tool_use_id] = b;
    }
    const isAbsorbed = (b) => b && b.kind === 'tool_result' && b.tool_use_id && map[b.tool_use_id];
    const visible = all.filter(m => {
      if (!messageHasVisibleContent(m)) return false;
      const blocks = m.blocks || [];
      return !(blocks.length > 0 && blocks.every(isAbsorbed));
    });
    return { visibleMessages: visible, toolResultMap: map };
  }, [messages]);
  const showSuggestions = visibleMessages.length === 0 && !streaming && suggestions && suggestions.length > 0;
  return (
    <div ref={scrollRef} className="scroll-hide" style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: compact ? '14px 14px' : '20px 24px', minHeight: 0, minWidth: 0 }}>
      {showSuggestions && <SuggestionChips suggestions={suggestions} onSuggest={onSuggest} />}
      {visibleMessages.map((m, i) => {
        const prev = visibleMessages[i - 1];
        const next = visibleMessages[i + 1];
        // Hydrated transcripts split each agent turn into role=assistant (tool_use
        // / text) and role=tool_result — group them all under one header.
        const isAgent = (r) => r === 'assistant' || r === 'tool_result';
        const showMeta = !isAgent(m.role) || !prev || !isAgent(prev.role);
        const tightBelow = isAgent(m.role) && next && isAgent(next.role);
        // Key MUST include the array index. Multiple entries can share
        // the same (turn_idx, role) pair: a single logical turn often
        // emits several tool_result rows (one per parallel tool_use), and
        // multi-emit assistant turns can ship thinking + prose as
        // separate entries under the same turn_idx. Without `i` React
        // collapses or scrambles them, which surfaces as "duplicated"
        // bubbles to the user.
        return (
          <MessageBubble key={i + ':' + (m.turn_idx ?? '_') + ':' + (m.role || 'x')}
            msg={m}
            sessionId={sessionId} onOpenLightbox={onOpenLightbox}
            showMeta={showMeta} tightBelow={tightBelow}
            toolResultMap={toolResultMap}
            tts={tts} speakMessage={speakMessage} msgKey={msgKey}
            messageActions={messageActions} agentLabel={agentLabel} />
        );
      })}
      <StreamingBubble streaming={streaming}
        sessionId={sessionId} onOpenLightbox={onOpenLightbox} agentLabel={agentLabel} />
    </div>
  );
}

// Memoise MessageBubble so the ChatTranscript map() doesn't re-render
// every bubble when only one prop (e.g. resolvedChoices for a new
// streaming turn, or activeSession identity flip from refreshSessions)
// changes. The full-transcript re-render storm was visible as scroll
// jank during streaming on long conversations.
const MessageBubble = React.memo(MessageBubbleImpl);

Object.assign(window, {
  Meta, MessageBubble, StreamingBubble, ThinkingDots, SuggestionChips, ChatTranscript,
  blockHasContent, messageHasVisibleContent, SpeakButton,
});
