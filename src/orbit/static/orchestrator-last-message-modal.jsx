// orchestrator-last-message-modal.jsx — mobile "Pokaż ostatnią wiadomość"
// ─────────────────────────────────────────────────────────────────────────
// The terminal/transcript copy badly on phones (issue #88, related #83). This
// renders the last assistant turn as plain, NATIVELY-SELECTABLE, copyable text
// in a bottom-sheet (mobile) / modal (desktop). Pure display — no backend, no
// feature flag — exactly like the unflagged select-text button.
//
// Reuses: window.BottomSheet/Modal (components.jsx), window.safeMd
// (orchestrator-blocks.jsx). Copy is delegated to the parent's `onCopy` which
// already builds clipboard text + toasts ("Skopiowano"). Published to window
// (repo convention — no imports/bundler). Loads after orchestrator-blocks.jsx
// + components.jsx, before orchestrator.jsx.

function LastMessageModal({ open, onClose, blocks, compact, onCopy }) {
  const Shell = compact ? window.BottomSheet : window.Modal;
  if (!Shell) return null;

  const list = Array.isArray(blocks) ? blocks : [];
  const hasContent = list.length > 0;

  const renderBody = () => {
    if (!hasContent) {
      return (
        <div style={{ padding: '8px 4px', color: 'var(--fg-2)', fontSize: 'var(--t-body)' }}>
          Brak wiadomości
        </div>
      );
    }
    return (
      <div
        style={{
          // userSelect:text is the core fix — iOS lets the user long-press and
          // copy from this block, unlike the xterm canvas / transcript.
          userSelect: 'text', WebkitUserSelect: 'text',
          display: 'flex', flexDirection: 'column', gap: 10,
        }}
      >
        {list.map((b, i) => {
          const text = (b && (b.text != null ? b.text : b.content)) || '';
          if (b && b.kind === 'code') {
            return (
              <pre
                key={i}
                style={{
                  margin: 0, padding: '10px 12px', overflowX: 'auto',
                  background: 'var(--surface-2)', border: '1px solid var(--hairline)',
                  borderRadius: 'var(--r-sm)', fontFamily: 'var(--font-mono, monospace)',
                  fontSize: 'var(--t-sm)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  userSelect: 'text', WebkitUserSelect: 'text',
                }}
              >{text}</pre>
            );
          }
          // markdown / text → sanitized HTML via the shared pipeline.
          return (
            <div
              key={i}
              className="md-body"
              style={{ userSelect: 'text', WebkitUserSelect: 'text', lineHeight: 1.55 }}
              dangerouslySetInnerHTML={{ __html: window.safeMd ? window.safeMd(text) : text }}
            />
          );
        })}
      </div>
    );
  };

  return (
    <Shell open={open} onClose={onClose} title="Ostatnia wiadomość">
      {renderBody()}
      {hasContent && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
          <Button
            variant="primary"
            icon="copy"
            onClick={() => { if (typeof onCopy === 'function') onCopy(); }}
          >
            Kopiuj
          </Button>
        </div>
      )}
    </Shell>
  );
}

Object.assign(window, { LastMessageModal });
