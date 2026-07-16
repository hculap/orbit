// orchestrator-attachments.jsx — File-attachment UX for the Orchestrator chat.
// Pre-send chips (composer-local File objects) and post-send tiles (server-saved
// references) plus a fullscreen Lightbox modal for image preview. Extracted out
// of orchestrator.jsx to keep that file under 800 lines. Same publish-to-window
// pattern as orchestrator-blocks.jsx so the main file consumes these as bare
// globals.
//
// No imports from orchestrator.jsx — avoids circular load-order coupling.

const { useState, useEffect, useLayoutEffect, useRef } = React;

// — Helpers —
const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|svg|heic|bmp|avif)$/i;

function truncateName(name, max = 14) {
  if (!name) return '';
  const s = String(name);
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

function isImageFile(file) {
  if (!file) return false;
  if (typeof file.type === 'string' && file.type.startsWith('image/')) return true;
  return IMAGE_EXT_RE.test(file.name || '');
}

function isImageAttachment(att) {
  if (!att) return false;
  if (att.kind === 'image') return true;
  if (typeof att.type === 'string' && att.type.startsWith('image/')) return true;
  return IMAGE_EXT_RE.test(att.name || '');
}

// pickFiles — pull a flat File[] from drag/drop, paste, or input change events.
// Filters paste items to kind === 'file' (skips text/html clipboard payloads).
// Returns [] when nothing usable is present.
function pickFiles(event) {
  if (!event) return [];
  // <input type="file"> change
  const target = event.target;
  if (target && target.files && target.files.length) {
    return Array.from(target.files);
  }
  // Drag/drop
  if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length) {
    return Array.from(event.dataTransfer.files);
  }
  // Paste — clipboardData.items contains DataTransferItem entries
  if (event.clipboardData && event.clipboardData.items) {
    const out = [];
    for (const item of event.clipboardData.items) {
      if (item && item.kind === 'file') {
        const f = item.getAsFile();
        if (f) out.push(f);
      }
    }
    return out;
  }
  return [];
}

// AttachmentChip — pre-send pending attachment with × remove button.
function AttachmentChip({ file, onRemove }) {
  const isImage = isImageFile(file);
  const [thumbUrl, setThumbUrl] = useState(null);
  useEffect(() => {
    if (!isImage) return undefined;
    const url = URL.createObjectURL(file);
    setThumbUrl(url);
    return () => {
      try { URL.revokeObjectURL(url); } catch (e) { /* ignore */ }
    };
  }, [file, isImage]);

  const ext = (file.name || '').split('.').pop() || '';
  const sizeLabel = typeof fmtBytes === 'function' ? fmtBytes(file.size || 0) : (file.size + ' B');
  return (
    <div style={{
      position: 'relative',
      display: 'inline-flex', flexDirection: 'column', gap: 4,
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', padding: 6,
      width: 96, flexShrink: 0,
    }}>
      <div style={{
        width: 80, height: 80, borderRadius: 'var(--r-sm)', overflow: 'hidden',
        background: 'var(--surface-3)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        {isImage && thumbUrl ? (
          <img src={thumbUrl} alt={file.name}
            style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
        ) : (
          <FileIconBox ext={ext} name={file.name || ''} size={56} />
        )}
      </div>
      <div title={file.name} style={{
        fontSize: 'var(--t-xs)', color: 'var(--fg)', lineHeight: 1.3,
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
      }}>{truncateName(file.name)}</div>
      <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{sizeLabel}</div>
      <div style={{ position: 'absolute', top: -6, right: -6 }}>
        <IconButton icon="close" label="Remove" size={20} onClick={onRemove}
          style={{ background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)' }} />
      </div>
    </div>
  );
}

// PendingAttachmentsStrip — horizontal scroll row of AttachmentChip.
function PendingAttachmentsStrip({ attachments, onRemove }) {
  if (!attachments || attachments.length === 0) return null;
  return (
    <div className="scroll-hide" style={{
      display: 'flex', gap: 8, marginTop: 8,
      overflowX: 'auto', overflowY: 'hidden',
      paddingTop: 8, paddingBottom: 4,
    }}>
      {attachments.map((f, i) => (
        <AttachmentChip key={(f.name || 'f') + ':' + (f.size || 0) + ':' + i}
          file={f} onRemove={() => onRemove(i)} />
      ))}
    </div>
  );
}

// AttachmentTile — post-send tile rendered inside a user message bubble.
// `attachment` shape: {name, size?, type?, server_path, kind?}
function previewUrl(sessionId, attachment) {
  if (!sessionId || !attachment || !attachment.name) return '#';
  // Defensive: if the caller passes an absolute path or a backslash-separated
  // Windows-style path (the agent occasionally emits these in image/audio
  // block `path` fields despite the spec saying basename-only), reduce to the
  // basename. Otherwise encodeURIComponent escapes the slashes to %2F and the
  // backend's path matcher 404s on a perfectly-resolvable filename.
  const basename = attachment.name.split('/').pop().split('\\').pop();
  if (!basename) return '#';
  return apiUrl('/api/orchestrator/uploads/' + encodeURIComponent(sessionId) + '/' + encodeURIComponent(basename));
}

function AttachmentTile({ attachment, sessionId, onOpen }) {
  if (!attachment) return null;
  const isImage = isImageAttachment(attachment);
  const url = previewUrl(sessionId, attachment);
  const ext = (attachment.name || '').split('.').pop() || '';
  const sizeLabel = (attachment.size != null && typeof fmtBytes === 'function')
    ? fmtBytes(attachment.size)
    : '';

  if (isImage) {
    return (
      <button onClick={() => onOpen && onOpen({ url, name: attachment.name })}
        title={attachment.name}
        aria-label={'Open ' + attachment.name}
        style={{
          width: 100, height: 100, padding: 0, flexShrink: 0,
          borderRadius: 'var(--r-control)', overflow: 'hidden',
          border: '1px solid var(--hairline)', background: 'var(--surface-2)',
          cursor: 'pointer',
        }}>
        <img src={url} alt={attachment.name}
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
      </button>
    );
  }
  return (
    <a href={url} target="_blank" rel="noopener noreferrer"
      title={attachment.name}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 8, flexShrink: 0,
        background: 'var(--surface-2)', border: '1px solid var(--hairline)',
        borderRadius: 'var(--r-control)', padding: '6px 10px',
        color: 'var(--fg)', textDecoration: 'none',
        maxWidth: 220,
      }}>
      <FileIconBox ext={ext} name={attachment.name || ''} size={32} />
      <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <span style={{
          fontSize: 'var(--t-cap)', color: 'var(--fg)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>{attachment.name}</span>
        {sizeLabel && (
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{sizeLabel}</span>
        )}
      </span>
    </a>
  );
}

// AttachmentRow — flex-wrap container of AttachmentTile.
function AttachmentRow({ attachments, sessionId, onOpen }) {
  if (!attachments || attachments.length === 0) return null;
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8,
      maxWidth: '100%',
    }}>
      {attachments.map((a, i) => (
        <AttachmentTile key={(a.name || 'a') + ':' + i}
          attachment={a} sessionId={sessionId} onOpen={onOpen} />
      ))}
    </div>
  );
}

// Lightbox — fullscreen image preview with Esc + backdrop close.
function Lightbox({ file, onClose }) {
  useEffect(() => {
    if (!file) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') onClose && onClose(); };
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [file, onClose]);

  if (!file) return null;
  const onBackdropClick = (e) => {
    if (e.target === e.currentTarget) onClose && onClose();
  };
  return (
    <div onClick={onBackdropClick}
      style={{
        position: 'fixed', inset: 0, zIndex: 100,
        background: 'rgba(0,0,0,0.92)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        padding: 24,
      }}>
      <div style={{
        position: 'absolute', top: 12, right: 12,
        display: 'flex', alignItems: 'center', gap: 10,
        color: 'var(--fg)', fontSize: 'var(--t-cap)',
        background: 'rgba(20,20,22,0.7)', border: '1px solid var(--hairline)',
        borderRadius: 'var(--r-control)', padding: '6px 10px',
        maxWidth: 'calc(100vw - 24px)',
      }}>
        <span title={file.name} style={{
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          maxWidth: 320,
        }}>{file.name || 'image'}</span>
        <a href={file.url} download={file.name || 'image'} aria-label="Download"
          onClick={(e) => e.stopPropagation()}
          style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 28, height: 28, borderRadius: 'var(--r-sm)',
            color: 'var(--fg)', textDecoration: 'none',
          }}>
          <Icon name="download" size={14} stroke={2} />
        </a>
        <IconButton icon="close" label="Close" size={28} onClick={onClose} />
      </div>
      <img src={file.url} alt={file.name || ''}
        style={{
          maxWidth: '90vw', maxHeight: '90vh',
          objectFit: 'contain', display: 'block',
          borderRadius: 'var(--r-sm)',
        }} />
    </div>
  );
}

// MessageInput — composer card with drag/paste/attach + pending strip.
// Lives here (not orchestrator.jsx) because it's tightly coupled to the
// attachment UX. Send is enabled when text or any pending attachment exists.
// Context window per model id. The Hetzner CLI install runs Claude Opus 4.x
// (`claude-opus-4-7`, `claude-opus-4-8`, …) with the 1M-token context beta;
// Sonnet/Haiku fall back to the standard 200k. We match the whole `opus-4`
// family rather than a single version so a model bump doesn't silently revert
// to 200k — that regression made a 597k-token Opus 4.8 session read as 100%
// (red) instead of its real ~60%.
function _modelContextWindow(modelId) {
  if (typeof modelId !== 'string' || !modelId) return 200000;
  if (modelId.indexOf('opus-4') !== -1) return 1000000;
  return 200000;
}

function _formatTokens(n) {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(n >= 100_000 ? 0 : 0) + 'k';
  return String(n);
}

// Shared usage descriptor — also consumed by SessionItem in orchestrator.jsx
// via window.computeContextUsage. Returns null when there's nothing to render
// (no assistant turns yet) so callers can early-exit.
function computeContextUsage(tokens, model) {
  const t = Number(tokens) || 0;
  if (t <= 0) return null;
  const win = _modelContextWindow(model);
  const pct = Math.min(100, Math.round((t / win) * 100));
  // Tier colours: muted < 60, accent 60-79, error 80+. Aligned with the user
  // mental model where ~80% is "compact soon".
  const color = pct >= 80 ? 'var(--err)'
              : pct >= 60 ? 'var(--accent)'
              :             'var(--fg-3)';
  const title = _formatTokens(t) + ' / ' + _formatTokens(win) + ' tokens'
              + (model ? ' · ' + model : '');
  return { pct, color, title, tokens: t, window: win };
}

function _ContextUsage({ tokens, model }) {
  const u = computeContextUsage(tokens, model);
  if (!u) return null;
  return (
    <>
      <span style={{ color: 'var(--fg-4)' }}> · </span>
      <span title={u.title} style={{ color: u.color }}>kontekst {u.pct}%</span>
    </>
  );
}

function MessageInput({
  value, onChange, onSend, disabled, hint, compact, inputRef,
  pendingAttachments, onFilesPicked, onRemovePending, onAttachClick, fileInputRef,
  isStreaming, onCancel,
  onRecordingStart, onVoiceFinalDetected,
  contextTokens, contextModel,
}) {
  const hasAttachments = Array.isArray(pendingAttachments) && pendingAttachments.length > 0;
  const canSend = !disabled && (value.trim() || hasAttachments);
  const [dragging, setDragging] = useState(false);
  const TA_MAX = 200;
  // Auto-send-after-voice setting (per-device, localStorage). Read here so
  // the closure passed to MicButton always sees the latest value.
  const [voiceSettings] = (window.useSettings || (() => [{ autoSendVoice: false }, () => {}]))();
  // Final-transcript handler: replace input value, then optionally fire send.
  // setTimeout(0) defers send to after React commits the onChange — otherwise
  // onSend reads stale state from the parent's closure.
  const onVoiceFinal = (text) => {
    onChange(text);
    // Tell the parent that the next send is voice-driven so the TTS layer
    // can decide whether to read the reply aloud (mode === 'on-voice').
    if (text && text.trim() && typeof onVoiceFinalDetected === 'function') {
      try { onVoiceFinalDetected(text); } catch (e) { /* swallow */ }
    }
    if (voiceSettings && voiceSettings.autoSendVoice && text && text.trim()) {
      // Pass the final transcript explicitly. setInput above is async and
      // setTimeout(0) fires before React has guaranteed-flushed the state,
      // so calling onSend() with no args would read stale `input` from the
      // parent's closure and ship a partial transcript. The submit chain
      // (orchestrator.jsx:submit → enqueueMessage / send) accepts an
      // explicitText override that bypasses the state read.
      const finalText = text;
      setTimeout(() => { if (!disabled) onSend(finalText); }, 0);
    }
  };
  useLayoutEffect(() => {
    const el = inputRef && inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, TA_MAX) + 'px';
  }, [value, inputRef]);

  const isFileDrag = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  const handleDragEnter = (e) => {
    if (disabled || !isFileDrag(e)) return;
    e.preventDefault(); e.stopPropagation(); setDragging(true);
  };
  const handleDragOver = (e) => {
    if (disabled || !isFileDrag(e)) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  };
  const handleDragLeave = (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false);
  };
  const handleDrop = (e) => {
    e.preventDefault(); e.stopPropagation(); setDragging(false);
    if (disabled) return;
    const files = pickFiles(e);
    if (files.length && onFilesPicked) onFilesPicked(files);
  };
  const handlePaste = (e) => {
    if (disabled) return;
    const files = pickFiles(e);
    if (files.length) { e.preventDefault(); onFilesPicked && onFilesPicked(files); }
  };
  const handleInputChange = (e) => {
    const files = pickFiles(e);
    if (files.length && onFilesPicked) onFilesPicked(files);
    e.target.value = ''; // reset so the same file can be picked twice
  };

  // Cmd+U / Cmd+M shortcuts (dispatched by OrchestratorView). The composer
  // handles them in chat view; TerminalLiveView handles them in terminal
  // view — only one is mounted at a time so they don't both fire. Upload →
  // open the attach picker; mic → click the MicButton (its own onClick
  // owns the start/stop state).
  const micWrapRef = useRef(null);
  useEffect(() => {
    const onUpload = () => { if (!disabled && typeof onAttachClick === 'function') onAttachClick(); };
    const onMic = () => {
      const btn = micWrapRef.current && micWrapRef.current.querySelector('button');
      if (btn) btn.click();
    };
    window.addEventListener('hub:shortcut-upload', onUpload);
    window.addEventListener('hub:shortcut-mic', onMic);
    return () => {
      window.removeEventListener('hub:shortcut-upload', onUpload);
      window.removeEventListener('hub:shortcut-mic', onMic);
    };
  }, [disabled, onAttachClick]);

  return (
    <div style={{
      // No safe-area-inset-bottom here — the bottom-nav below already
      // reserves it; doubling it leaves a tall dead band on iPhone.
      padding: compact ? '8px 12px 10px' : '14px 24px 22px',
      borderTop: '1px solid var(--hairline)', flexShrink: 0, background: 'var(--bg)',
    }}>
      <div
        onDragEnter={handleDragEnter} onDragOver={handleDragOver}
        onDragLeave={handleDragLeave} onDrop={handleDrop}
        style={{
          display: 'flex', flexDirection: 'column',
          background: 'var(--surface-1)',
          border: '1px solid ' + (dragging ? 'var(--accent)' : 'var(--hairline-strong)'),
          borderRadius: 'var(--r-lg)', padding: '8px 8px 8px 14px',
          opacity: disabled ? 0.7 : 1,
          transition: 'border-color .12s',
        }}>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8 }}>
          <IconButton icon="attach" label="Attach files" size={32}
            onClick={() => onAttachClick && onAttachClick()} />
          <input
            type="file" multiple ref={fileInputRef}
            style={{ display: 'none' }} onChange={handleInputChange}
          />
          <textarea
            ref={inputRef} value={value} onChange={e => onChange(e.target.value)}
            onPaste={handlePaste}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!disabled) onSend(); }
            }}
            placeholder={disabled ? 'Wybierz sesję aby pisać…' : (isStreaming ? 'Dopisz aby dodać do kolejki…' : 'Ask the agent…')}
            rows={1} disabled={disabled}
            style={{
              flex: 1, background: 'transparent', border: 'none', outline: 'none', resize: 'none',
              color: 'var(--fg)', fontFamily: 'inherit', fontSize: 'var(--t-md)', lineHeight: '20px',
              // Match the textarea row height to the send/mic button so single-line
              // text sits centered with the icons (no apparent vertical shift on
              // mobile where buttons are 44px tall).
              padding: compact ? '12px 0' : '7px 0',
              maxHeight: TA_MAX, minWidth: 0, overflowY: 'auto',
            }}
          />
          <span ref={micWrapRef} style={{ display: 'inline-flex', flexShrink: 0 }}>
            <MicButton
              onTranscript={(text) => onChange(text)}
              onFinalTranscript={onVoiceFinal}
              onRecordingStart={onRecordingStart}
              disabled={disabled || isStreaming}
              currentInput={value}
              language={(voiceSettings && voiceSettings.sttLanguage) || 'auto'}
            />
          </span>
          {/* Show stop only when there's an in-flight turn AND the composer is
              empty. Once the user starts typing during streaming, swap to the
              send icon so the message can be queued. After enqueue the input
              clears, canSend goes false, and we revert to stop automatically. */}
          {isStreaming && !canSend ? (
            <button onClick={() => onCancel && onCancel()} aria-label="Cancel"
              style={{
                width: compact ? 44 : 34, height: compact ? 44 : 34,
                borderRadius: compact ? 'var(--r-widget)' : 'var(--r-md)', flexShrink: 0,
                background: 'var(--err-soft)',
                color: 'var(--err)',
                border: '1px solid var(--err-line)',
                cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                transition: 'background .15s',
                // Eliminate iOS 300ms tap delay + double-tap zoom on the button.
                touchAction: 'manipulation',
                WebkitTapHighlightColor: 'transparent',
              }}>
              <Icon name="close" size={compact ? 18 : 16} stroke={2} />
            </button>
          ) : (
            // Wrap onSend so the click event doesn't get passed as `explicitText`.
            // submit(event) → send(event) → event.trim() throws and the message
            // never goes out — visible mainly on mobile where Enter isn't used.
            <button onClick={() => onSend()} disabled={!canSend}
              aria-label={isStreaming ? 'Add to queue' : 'Send'}
              title={isStreaming ? 'Dodaj do kolejki' : 'Wyślij'}
              style={{
                width: compact ? 44 : 34, height: compact ? 44 : 34,
                borderRadius: compact ? 'var(--r-widget)' : 'var(--r-md)', flexShrink: 0,
                background: canSend ? 'var(--accent)' : 'var(--surface-3)',
                color: canSend ? 'var(--accent-fg)' : 'var(--fg-3)',
                border: 'none', cursor: canSend ? 'pointer' : 'default',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                transition: 'background .15s',
                touchAction: 'manipulation',
                WebkitTapHighlightColor: 'transparent',
              }}>
              <Icon name="arrow-up" size={compact ? 18 : 16} stroke={2} />
            </button>
          )}
        </div>
        <PendingAttachmentsStrip attachments={pendingAttachments} onRemove={onRemovePending} />
      </div>
      <div className="mono" style={{
        marginTop: 8, fontSize: 'var(--t-xs)', color: 'var(--fg-3)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap',
      }}>
        <span>
          {hint || 'shift+return for newline · drag/paste/attach files'}
          <_ContextUsage tokens={contextTokens} model={contextModel} />
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span className="live-dot" style={{ width: 6, height: 6 }} />claude on server
        </span>
      </div>
    </div>
  );
}

// uploadAttachments — POST a FormData of `files` to the session uploads route.
// Returns the saved descriptors on success; throws an Error on HTTP failure.
async function uploadAttachments(sessionId, files) {
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  const r = await fetch(
    apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/uploads'),
    { method: 'POST', body: fd },
  );
  if (!r.ok) {
    let msg = 'upload failed (' + r.status + ')';
    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  const j = await r.json();
  return Array.isArray(j && j.saved) ? j.saved : [];
}

// Publish to window so orchestrator.jsx can use these as bare globals.
Object.assign(window, {
  AttachmentChip, PendingAttachmentsStrip, AttachmentTile, AttachmentRow,
  Lightbox, MessageInput, pickFiles, previewUrl, uploadAttachments,
  computeContextUsage,
});
