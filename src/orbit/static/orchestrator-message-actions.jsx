// Per-message action row for the Orchestrator transcript.
// Exposes:
//   - stripMarkdownForCopy(text): markdown -> human-readable plain text for clipboard
//   - MessageActionButton: small icon button matching SpeakButton styling
//   - MessageActionRow: full action row (speak / copy / reply / pin)
//
// All published to window so transcript/orchestrator pick them up without imports.

const { useState: _maUseState } = React;

// Markdown -> plain text optimized for human reading on the clipboard.
// Tuned differently from stripMarkdownForSpeech: we preserve fenced code and
// inline code content, list bullets, and inline URLs (so a pasted reply still
// reads naturally without the punctuation soup).
function stripMarkdownForCopy(input) {
  if (!input) return '';
  let s = String(input);
  // Fenced code: keep content, drop the ``` fences and optional language tag.
  s = s.replace(/```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```/g, '$1');
  // Stray fences without trailing newline (defensive).
  s = s.replace(/```[ \t]*[A-Za-z0-9_+-]*[ \t]*/g, '');
  s = s.replace(/```/g, '');
  // Inline code: keep content, drop backticks.
  s = s.replace(/`([^`]+)`/g, '$1');
  // Images: drop entirely (alt-text alone reads weirdly without context).
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
  // [text](url) -> "text (url)" so the link survives.
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1 ($2)');
  // Headings: drop only the leading hashes + the space, keep the title text.
  s = s.replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, '');
  // Blockquote markers.
  s = s.replace(/^[ \t]{0,3}>[ \t]?/gm, '');
  // Horizontal rules.
  s = s.replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, '');
  // Bold / italics / strike — markers only, content preserved.
  s = s.replace(/(\*\*|__)(.+?)\1/g, '$2');
  s = s.replace(/(\*|_)(.+?)\1/g, '$2');
  s = s.replace(/~~(.+?)~~/g, '$1');
  // Raw HTML tags.
  s = s.replace(/<[^>]+>/g, '');
  // Trim trailing spaces per line.
  s = s.replace(/[ \t]+$/gm, '');
  // Collapse 3+ blank lines into 2 (one blank line between paragraphs max).
  s = s.replace(/\n{3,}/g, '\n\n');
  return s.trim();
}

// Small icon button. Visual parity with SpeakButton in orchestrator-transcript.jsx.
function MessageActionButton({ icon, title, onClick, active = false, danger = false, disabled = false }) {
  const [hover, setHover] = _maUseState(false);
  const accent = danger ? 'var(--err)' : 'var(--accent)';
  const accentSoft = danger ? 'var(--err-soft)' : 'var(--accent-soft)';
  const accentLine = danger ? 'var(--err-line)' : 'var(--accent-line)';
  let opacity;
  if (disabled) opacity = 0.3;
  else if (active) opacity = 1;
  else if (hover) opacity = 1;
  else opacity = 0.65;
  const bg = active ? accentSoft : 'transparent';
  const fg = active ? accent : 'var(--fg-3)';
  const border = '1px solid ' + (active ? accentLine : 'transparent');
  const handle = (e) => {
    e.stopPropagation();
    if (disabled) return;
    if (typeof onClick === 'function') onClick(e);
  };
  return (
    <button
      type="button"
      onClick={handle}
      aria-label={title}
      title={title}
      disabled={disabled}
      onMouseEnter={() => { if (!disabled) setHover(true); }}
      onMouseLeave={() => setHover(false)}
      style={{
        width: 22, height: 22, borderRadius: 'var(--r-sm)', padding: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        background: bg, color: fg, border, opacity,
        cursor: disabled ? 'not-allowed' : 'pointer', flexShrink: 0,
        transition: 'opacity .12s, color .12s, background .12s',
      }}
    >
      <Icon name={icon} size={12} stroke={1.8} />
    </button>
  );
}

// Does this message contain anything worth showing the action row for?
// Mirrors the gate used by MessageBubble: if assistant has speakable prose, OR
// any message has plain text we could copy, render the row.
function _hasCopyableText(msg) {
  if (!msg) return false;
  if (msg.role === 'user') {
    const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
    const fromBlocks = blocks.find(b => b && (b.kind === 'text' || b.kind === 'markdown'));
    const txt = fromBlocks ? (fromBlocks.text || fromBlocks.content || '') : (msg.text || '');
    return !!String(txt).trim();
  }
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
  return blocks.some(b => b && (b.kind === 'markdown' || b.kind === 'text' || b.kind === 'code') && (b.text || b.content));
}

function MessageActionRow({ msg, msgKey, role, tts, speakMessage, isSpeakingThis, ttsSupported, actions }) {
  const isUser = role === 'user';
  const safeActions = actions || {};
  const hasText = _hasCopyableText(msg);
  // Hide entirely when there is nothing to act on (no speakable prose for
  // assistant + no copyable user text).
  if (!ttsSupported && !hasText) return null;

  // iOS / Safari autoplay: unlock synchronously inside the click before the
  // async speakMessage path resolves a fetch (mirrors MessageBubble.handlePlay).
  const handlePlay = () => {
    if (tts && !tts.unlocked && typeof tts.unlock === 'function') {
      try { tts.unlock(); } catch (e) { /* ignore */ }
    }
    if (typeof speakMessage === 'function') speakMessage(msg);
  };
  const handleStop = () => {
    if (tts && typeof tts.cancel === 'function') {
      try { tts.cancel(); } catch (e) { /* ignore */ }
    }
  };

  const speakTitle = isSpeakingThis ? 'Zatrzymaj czytanie' : 'Przeczytaj na głos';
  const pinTitle = safeActions.isPinned ? 'Odepnij' : 'Przypnij';

  return (
    <div style={{
      display: 'flex', flexDirection: 'row', gap: 4,
      justifyContent: 'flex-start', alignItems: 'center',
      marginTop: 6,
    }}>
      {!isUser && ttsSupported && (
        <MessageActionButton
          icon={isSpeakingThis ? 'square' : 'volume'}
          title={speakTitle}
          active={isSpeakingThis}
          onClick={isSpeakingThis ? handleStop : handlePlay}
        />
      )}
      {hasText && typeof safeActions.copyMessage === 'function' && (
        <MessageActionButton
          icon="copy"
          title="Skopiuj"
          onClick={() => { safeActions.copyMessage(msg); }}
        />
      )}
      {!isUser && typeof safeActions.replyTo === 'function' && (
        <MessageActionButton
          icon="corner-up-left"
          title="Odpowiedz na tę wiadomość"
          onClick={() => { safeActions.replyTo(msg); }}
        />
      )}
      {typeof safeActions.togglePin === 'function' && (
        <MessageActionButton
          icon={safeActions.isPinned ? 'pin-fill' : 'pin'}
          title={pinTitle}
          active={!!safeActions.isPinned}
          disabled={!!safeActions.pinDisabled}
          onClick={() => { safeActions.togglePin(msg); }}
        />
      )}
    </div>
  );
}

Object.assign(window, { stripMarkdownForCopy, MessageActionButton, MessageActionRow });
