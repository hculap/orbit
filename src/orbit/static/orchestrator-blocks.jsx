// orchestrator-blocks.jsx — Block-renderer family extracted from orchestrator.jsx.
// Pure presentational components for assistant message blocks (text/markdown,
// code, tool_use, tool_result, thinking, error, image) plus the control-echo
// filter (isControlEcho). The envelope pipeline was removed in the artifacts
// cutover — replies are plain markdown, so there's no choice/ask widget or
// media-fence lifting here anymore. Published to window so orchestrator.jsx can
// use them as bare globals (Babel-standalone leaks window globals into scope).
//
// No imports from orchestrator.jsx — avoids circular load-order coupling.

const { useState, useEffect, useMemo, useRef } = React;

function safeMd(text) {
  if (!text) return '';
  if (!window.marked || typeof window.marked.parse !== 'function') return null;
  const raw = window.marked.parse(String(text));
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    return window.DOMPurify.sanitize(raw);
  }
  return null;
}

// Patterns claude-cli injects as synthetic "user" turns that we don't want
// to render as if the user typed them:
//   - Skill prologue: when claude calls a Skill tool, the cli prepends the
//     full SKILL.md to the next user turn so the model sees the skill's
//     instructions inline. Always starts with the literal sentinel below.
//   - Background-task notification: when a `run_in_background` Bash or a
//     ScheduleWakeup fires, claude-cli injects a `<task-notification>…
//     </task-notification>` block as a user turn so the model can react.
const SKILL_PROLOGUE_RE = /^\s*Base directory for this skill:\s/;
const TASK_NOTIFICATION_RE = /^\s*<task-notification\b[\s\S]*<\/task-notification>\s*$/;

// Pull the visible text out of a message regardless of shape (text or block-based).
function messageText(m) {
  if (!m) return '';
  if (typeof m.text === 'string' && m.text) return m.text;
  const blocks = Array.isArray(m.blocks) ? m.blocks : [];
  const tb = blocks.find(b => b && (b.kind === 'text' || b.kind === 'markdown'));
  if (tb) return tb.text || tb.content || '';
  return '';
}

// True when this user message is purely a bookkeeping echo we don't want to show.
// Covers: explicit `is_control` flag from the backend; claude-cli's synthetic
// skill prologue and task-notification turns (CLI plumbing, not something the
// user actually typed).
function isControlEcho(m) {
  if (!m || m.role !== 'user') return false;
  if (m.is_control) return true;
  const body = (messageText(m) || '').trim();
  if (!body) return false;
  if (SKILL_PROLOGUE_RE.test(body)) return true;
  if (TASK_NOTIFICATION_RE.test(body)) return true;
  return false;
}

function TextBlock({ text }) {
  const html = useMemo(() => safeMd(text), [text]);
  const ref = useRef(null);
  useEffect(() => {
    // If marked emitted any <pre><code class="language-..."> from fenced blocks,
    // run hljs over them so even legacy markdown-fence content gets coloured.
    if (!ref.current || !window.hljs) return;
    ref.current.querySelectorAll('pre code').forEach(el => {
      try { window.hljs.highlightElement(el); } catch (e) { /* ignore */ }
    });
  }, [html]);
  if (html != null) {
    return <div ref={ref} className="md-render" style={{ fontSize: 'var(--t-md)', lineHeight: 1.6, color: 'var(--fg)' }}
      dangerouslySetInnerHTML={{ __html: html }} />;
  }
  return <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>{text}</pre>;
}

// Headline shown next to the tool name when collapsed. Pulls a single readable
// field out of typical claude tool inputs — Bash.command, Read/Edit/Write
// .file_path, Grep/Glob .pattern, etc. Falls back to first string-valued field.
function _toolHeadline(name, input) {
  if (!input || typeof input !== 'object') return '';
  const lc = String(name).toLowerCase();
  if (lc === 'bash') return input.command || input.cmd || '';
  if (lc === 'read' || lc === 'edit' || lc === 'write' || lc === 'multiedit') return input.file_path || input.path || '';
  if (lc === 'grep') return (input.pattern || '') + (input.path ? '  ' + input.path : '');
  if (lc === 'glob') return input.pattern || '';
  if (lc === 'webfetch' || lc === 'webfetch_url') return input.url || '';
  if (lc === 'tasklist' || lc === 'task') return input.description || input.subject || '';
  for (const k of Object.keys(input)) {
    const v = input[k];
    if (typeof v === 'string' && v) return v;
  }
  return '';
}

function _resultText(r) {
  if (!r) return '';
  if (r.stdout != null) return String(r.stdout);
  if (r.output != null) return String(r.output);
  if (r.text != null) return String(r.text);
  return '';
}

function ToolUseBlock({ block, result }) {
  const [open, setOpen] = useState(false);
  const name = block.name || block.tool || 'tool';
  const input = block.input != null ? block.input : (block.input_partial || {});
  const headline = _toolHeadline(name, input);
  const trimmed = headline.length > 80 ? headline.slice(0, 80) + '…' : headline;
  const resultText = _resultText(result);
  const resultSize = result ? (resultText ? fmtBytes(new Blob([resultText]).size) : '0 B') : null;
  const isErr = !!(result && result.is_error);
  return (
    <div style={{
      background: 'var(--surface-1)',
      border: '1px solid ' + (isErr ? 'var(--err-line)' : 'var(--hairline)'),
      borderRadius: 'var(--r-control)', overflow: 'hidden',
    }}>
      <button onClick={() => setOpen(o => !o)} className="mono" style={{
        width: '100%', textAlign: 'left',
        padding: '6px 10px', display: 'flex', alignItems: 'center', gap: 8,
        background: 'transparent', border: 'none', cursor: 'pointer',
        color: 'var(--fg-2)', fontSize: 'var(--t-cap)',
      }}>
        <Icon name={open ? 'chevron-d' : 'chevron-r'} size={12} />
        {block.running
          ? <Icon name="spinner" size={13} color="var(--accent)" />
          : isErr
            ? <Icon name="close" size={13} color="var(--err)" />
            : <Icon name="check" size={13} color="var(--ok)" />}
        <span style={{ color: 'var(--accent)' }}>{name}</span>
        {trimmed && (
          <span style={{ color: 'var(--fg-3)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {trimmed}
          </span>
        )}
        {resultSize && <span style={{ color: isErr ? 'var(--err)' : 'var(--fg-4)', fontSize: 'var(--t-2xs)', marginLeft: 'auto' }}>{resultSize}</span>}
        {block.ms != null && <span style={{ color: 'var(--fg-4)', fontSize: 'var(--t-2xs)', marginLeft: resultSize ? 8 : 'auto' }}>{block.ms}ms</span>}
      </button>
      {open && (
        <div style={{ borderTop: '1px solid var(--hairline)', padding: 10 }}>
          {Object.keys(input || {}).length === 0
            ? <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>(no input)</div>
            : Object.entries(input).map(([k, v]) => {
                const isStr = typeof v === 'string';
                const valStr = isStr ? v : JSON.stringify(v, null, 2);
                const multiline = valStr.includes('\n') || valStr.length > 60;
                return (
                  <div key={k} style={{ marginBottom: 6, fontSize: 'var(--t-cap)' }}>
                    <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 2 }}>{k}</div>
                    <pre className="mono" style={{
                      margin: 0, fontSize: 'var(--t-cap)', color: 'var(--fg)',
                      whiteSpace: multiline ? 'pre-wrap' : 'nowrap',
                      wordBreak: 'break-word', overflowWrap: 'anywhere',
                      overflowX: multiline ? 'auto' : 'hidden',
                      background: 'var(--surface-2)', padding: '6px 8px', borderRadius: 4,
                    }}>{valStr}</pre>
                  </div>
                );
              })
          }
          {result && (
            <div style={{ marginTop: 8 }}>
              <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 2 }}>output{isErr ? ' (error)' : ''}</div>
              <pre className="mono" style={{
                margin: 0, padding: '6px 8px',
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere',
                fontSize: 'var(--t-xs)', color: isErr ? 'var(--err)' : 'var(--fg)',
                background: 'var(--surface-2)', borderRadius: 4,
                maxHeight: 320, overflow: 'auto',
              }}>{resultText || '(empty)'}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ToolResultBlock({ block }) {
  const [open, setOpen] = useState(false);
  // SSE emits `stdout`; JSONL hydration emits `output`; legacy paths used `text`.
  const stdout = block.stdout != null ? String(block.stdout)
               : block.output != null ? String(block.output)
               : block.text != null ? String(block.text)
               : '';
  const isErr = !!block.is_error;
  const sizeLabel = stdout ? fmtBytes(new Blob([stdout]).size) : '0 B';
  return (
    <div style={{
      background: 'var(--surface-1)',
      border: '1px solid ' + (isErr ? 'var(--err-line)' : 'var(--hairline)'),
      borderRadius: 'var(--r-control)', overflow: 'hidden',
    }}>
      <button onClick={() => setOpen(o => !o)} className="mono" style={{
        width: '100%', textAlign: 'left',
        padding: '6px 10px', display: 'flex', alignItems: 'center', gap: 8,
        background: 'transparent', border: 'none', cursor: 'pointer',
        color: 'var(--fg-2)', fontSize: 'var(--t-cap)',
      }}>
        <Icon name={open ? 'chevron-d' : 'chevron-r'} size={12} />
        {isErr ? <Icon name="close" size={12} color="var(--err)" /> : <Icon name="check" size={12} color="var(--ok)" />}
        <span>output ({sizeLabel})</span>
        {block.ms != null && <span style={{ color: 'var(--fg-4)', marginLeft: 'auto' }}>{block.ms}ms</span>}
      </button>
      {open && (
        <pre className="mono" style={{
          margin: 0, padding: '8px 12px', borderTop: '1px solid var(--hairline)',
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          fontSize: 'var(--t-xs)', color: isErr ? 'var(--err)' : 'var(--fg-2)',
          maxHeight: 320, overflow: 'auto',
        }}>{stdout || '(empty)'}</pre>
      )}
    </div>
  );
}

function ThinkingBlock({ block }) {
  return (
    <div style={{
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', padding: '8px 12px',
      fontStyle: 'italic', fontSize: 'var(--t-cap)', color: 'var(--fg-3)', lineHeight: 1.5,
    }}>{block.text || ''}</div>
  );
}

function ErrorBlock({ block }) {
  // Surface a reload affordance on every error so the user has a one-click
  // recovery path when the SSE stream dies or the runner emits a fatal
  // `error` event (e.g. fork EAGAIN, prompt-pipe failure, JSONL timeout).
  // The orchestrator listens for the custom event and re-attaches the
  // stream + re-fetches messages. No-op if no session is active.
  const onReload = () => {
    try {
      window.dispatchEvent(new CustomEvent('orchestrator:reload'));
    } catch (_e) { /* ignore */ }
  };
  return (
    <div style={{
      background: 'var(--err-bg)',
      border: '1px solid var(--err-line)',
      borderRadius: 'var(--r-control)', padding: '8px 12px',
      fontSize: 'var(--t-cap)', color: 'var(--err)',
      display: 'flex', alignItems: 'flex-start', gap: 8,
    }}>
      <Icon name="close" size={14} color="var(--err)" />
      <span style={{ flex: 1, whiteSpace: 'pre-wrap' }}>{block.text || block.message || 'error'}</span>
      <button
        type="button"
        onClick={onReload}
        title="Wczytaj ponownie strumień i wiadomości"
        style={{
          flexShrink: 0,
          background: 'transparent',
          border: '1px solid oklch(0.70 0.18 25 / 0.45)',
          color: 'var(--err)',
          borderRadius: 'var(--r-sm)',
          padding: '3px 10px',
          fontSize: 'var(--t-xs)',
          fontFamily: 'inherit',
          cursor: 'pointer',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
        }}
      >
        <Icon name="refresh" size={12} color="var(--err)" />
        Reload
      </button>
    </div>
  );
}

function CodeBlock({ block }) {
  const text = block.text != null ? block.text : (block.content || '');
  const lang = block.lang || '';
  const ref = useRef(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!ref.current || !window.hljs) return;
    ref.current.removeAttribute('data-highlighted');
    try { window.hljs.highlightElement(ref.current); } catch (e) { /* unknown lang → leave plain */ }
  }, [text, lang]);
  const onCopy = async (e) => {
    e.stopPropagation();
    const ok = await window.HubClipboard.copyText(text);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    }
  };
  const className = lang ? `language-${lang}` : '';
  return (
    <div style={{
      background: 'var(--surface-2)', borderRadius: 'var(--r-control)', padding: 12, paddingTop: 28,
      border: '1px solid var(--hairline)', position: 'relative',
      maxWidth: '100%', overflow: 'hidden',
    }}>
      <div style={{
        position: 'absolute', top: 6, right: 6, display: 'flex', alignItems: 'center', gap: 6,
        zIndex: 1,
      }}>
        {lang && (
          <span className="mono" style={{
            fontSize: 'var(--t-2xs)', color: 'var(--fg-3)',
            fontFamily: 'var(--font-mono)', textTransform: 'lowercase', letterSpacing: '0.04em',
            padding: '2px 4px',
          }}>{lang}</span>
        )}
        <button onClick={onCopy} title={copied ? 'Copied' : 'Copy to clipboard'}
          aria-label={copied ? 'Copied' : 'Copy to clipboard'}
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 24, height: 24, borderRadius: 4, padding: 0,
            border: '1px solid ' + (copied ? 'var(--ok)' : 'var(--hairline)'),
            background: copied ? 'var(--ok-soft)' : 'var(--surface-1)',
            color: copied ? 'var(--ok)' : 'var(--fg-3)',
            cursor: 'pointer', fontFamily: 'inherit',
            transition: 'background .12s, color .12s, border-color .12s',
          }}>
          <Icon name={copied ? 'check' : 'file'} size={12} stroke={1.6} />
        </button>
      </div>
      <pre style={{
        margin: 0, fontSize: 'var(--t-sm)', fontFamily: 'var(--font-mono)',
        whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere',
        background: 'transparent', overflowX: 'auto', maxWidth: '100%',
      }}><code ref={ref} className={className}>{text}</code></pre>
    </div>
  );
}

function ImageBlock({ block, sessionId, onOpenLightbox }) {
  const url = previewUrl(sessionId, { name: block.path });
  const alt = block.alt || 'generated image';
  return (
    <div style={{ margin: '8px 0', maxWidth: 480, position: 'relative', display: 'inline-block' }}>
      <img src={url} alt={alt}
        onClick={() => onOpenLightbox && onOpenLightbox({ url, name: block.path })}
        style={{
          maxWidth: '100%', borderRadius: 'var(--r-control)', cursor: 'zoom-in',
          border: '1px solid var(--hairline)', display: 'block',
        }} />
      <a href={url} download={block.path} aria-label="Download image"
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'absolute', top: 8, right: 8,
          width: 32, height: 32, borderRadius: 'var(--r-control)',
          background: 'rgba(14,15,18,0.65)', backdropFilter: 'blur(6px)',
          color: 'var(--fg)', textDecoration: 'none',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          border: '1px solid rgba(255,255,255,0.12)',
        }}>
        <Icon name="download" size={16} stroke={2} />
      </a>
      {block.alt && (
        <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 4 }}>{block.alt}</div>
      )}
    </div>
  );
}

// Block kinds elided when `settings.showToolActions === false`. Everything
// else (text/markdown/code/image/error/compacted_from + the media widget
// kinds historical transcripts can still carry) stays so the reply is readable.
const _TOOL_ACTION_KINDS = new Set(['tool_use', 'tool_result', 'thinking']);

function BlockRenderer({ block, toolResultMap, sessionId, onOpenLightbox }) {
  // settings-view.jsx is guaranteed loaded before this file (index.html load
  // order). Hook runs unconditionally so all BlockRenderer instances re-render
  // when the user flips the toggle.
  const [settings] = window.useSettings();
  if (!block || !block.kind) return null;
  if (settings.showToolActions === false && _TOOL_ACTION_KINDS.has(block.kind)) {
    return null;
  }
  switch (block.kind) {
    case 'text':
    case 'markdown': {
      // Plain markdown (the envelope pipeline is gone — Claude writes prose).
      // TextBlock renders via marked + DOMPurify, so fenced ```code``` blocks
      // get syntax highlighting for free. Drop empty/whitespace-only bodies.
      const tt = block.text || block.content || '';
      if (!tt.trim()) return null;
      return <TextBlock text={tt} />;
    }
    case 'image':        return <ImageBlock block={block} sessionId={sessionId} onOpenLightbox={onOpenLightbox} />;
    case 'code':         return <CodeBlock block={block} />;
    case 'tool_use':     return <ToolUseBlock block={block} result={toolResultMap && block.tool_use_id ? toolResultMap[block.tool_use_id] : null} />;
    case 'tool_result':
      // Already absorbed into the matching tool_use header (when one exists).
      if (toolResultMap && block.tool_use_id && toolResultMap[block.tool_use_id]) return null;
      return <ToolResultBlock block={block} />;
    case 'thinking':     return <ThinkingBlock block={block} />;
    case 'error':        return <ErrorBlock block={block} />;
    case 'audio':       return <AudioBlock block={block} sessionId={sessionId} />;
    case 'download':    return <DownloadBlock block={block} sessionId={sessionId} />;
    case 'video':       return <VideoBlock block={block} sessionId={sessionId} />;
    case 'youtube':     return <YouTubeBlock block={block} />;
    case 'chart':       return <ChartBlock block={block} />;
    case 'map':         return <MapBlock block={block} />;
    case 'custom_html': return <CustomHtmlBlock block={block} />;
    case 'compacted_from': {
      const sid = block.session_id || '';
      const short = sid.slice(0, 8);
      return (
        <button onClick={() => {
          try {
            window.dispatchEvent(new CustomEvent('hub:open-session', { detail: { session_id: sid } }));
          } catch (e) { /* ignore */ }
        }}
          title={'Kontynuacja sesji ' + sid}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '4px 10px', borderRadius: 'var(--r-pill)',
            background: 'var(--accent-soft)', color: 'var(--accent)',
            border: '1px solid var(--accent-line)',
            fontSize: 'var(--t-xs)', fontFamily: 'inherit', cursor: 'pointer',
            marginBottom: 4, alignSelf: 'flex-start',
          }}>
          <Icon name="corner-up-left" size={12} stroke={1.8} />
          <span>Kontynuacja: {short}…</span>
        </button>
      );
    }
    default:             return null;
  }
}

Object.assign(window, {
  BlockRenderer, TextBlock, CodeBlock, ToolUseBlock, ToolResultBlock,
  ThinkingBlock, ErrorBlock, ImageBlock, safeMd, isControlEcho,
});
