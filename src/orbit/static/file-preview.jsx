// file-preview.jsx — preview pane for files in ~/Sync. Used by the Sync section
// in mobile bottom sheet and desktop modal. Markdown bodies are sanitized via
// DOMPurify before being injected as HTML.

const { useState: useFpState, useEffect: useFpEffect } = React;

function fpBytesShort(b) {
  if (b == null) return '—';
  if (b > 1e12) return (b / 1e12).toFixed(2) + ' TB';
  if (b > 1e9)  return (b / 1e9).toFixed(2)  + ' GB';
  if (b > 1e6)  return (b / 1e6).toFixed(0)  + ' MB';
  if (b > 1e3)  return (b / 1e3).toFixed(0)  + ' KB';
  return b + ' B';
}

function renderMarkdown(textBody) {
  if (!window.marked || typeof window.marked.parse !== 'function') return null;
  const rawHtml = window.marked.parse(textBody);
  // DOMPurify is loaded via CDN in index.html; if it's missing we fall back to
  // plain-text rendering rather than injecting unsanitized HTML (Sync files are
  // user-writable across all Syncthing peers, so untrusted HTML is a real risk).
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    return window.DOMPurify.sanitize(rawHtml);
  }
  return null;
}

function FilePreview({ file }) {
  const [textBody, setTextBody] = useFpState(null);
  const [loading, setLoading] = useFpState(false);
  const toast = useToast();

  if (!file) return null;
  const name = file.name || '';
  const path = file.path || file.rel || name;
  const kind = file.kind || '';
  const ext = (file.ext || ('.' + (name.split('.').pop() || ''))).toLowerCase();
  const isImg = kind === 'image' || /\.(png|jpe?g|heic|webp|gif|bmp|svg)$/i.test(name);
  const isMd  = ext === '.md' || ext === '.markdown';
  const isPdf = kind === 'pdf' || /\.pdf$/i.test(name);
  const isVid = kind === 'video' || /\.(mp4|mov|webm|m4v)$/i.test(name);
  const isText = kind === 'text' && !isMd;

  const previewUrl = apiUrl('/share/preview/' + encodePath(path));
  const downloadUrl = apiUrl('/share/download/' + encodePath(path));

  useFpEffect(() => {
    if (!isMd && !isText) return;
    let cancelled = false;
    setLoading(true);
    setTextBody(null);
    fetch(previewUrl)
      .then(r => r.text())
      .then(t => { if (!cancelled) setTextBody(t); })
      .catch(() => { if (!cancelled) setTextBody('(failed to load)'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [previewUrl, isMd, isText]);

  const onRename = async () => {
    const newName = prompt('New name:', name);
    if (!newName || newName === name) return;
    try {
      const r = await fetch(apiUrl('/api/share/rename'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rel: path, new_name: newName }),
      });
      const d = await r.json();
      if (!d.ok) { toast && toast('Rename failed: ' + (d.error || ''), 'err'); return; }
      toast && toast('Renamed', 'ok');
      window.dispatchEvent(new CustomEvent('share:reload'));
    } catch (e) { toast && toast('Rename failed', 'err'); }
  };

  const onDelete = async () => {
    if (!confirm(`Delete "${name}"? (recovery via Syncthing .stversions/)`)) return;
    try {
      const r = await fetch(apiUrl('/api/share/' + encodePath(path)), { method: 'DELETE' });
      const d = await r.json();
      if (!d.ok) { toast && toast('Delete failed: ' + (d.error || ''), 'err'); return; }
      toast && toast('Deleted', 'ok');
      window.dispatchEvent(new CustomEvent('share:reload'));
    } catch (e) { toast && toast('Delete failed', 'err'); }
  };

  const safeMd = (isMd && textBody != null) ? renderMarkdown(textBody) : null;

  return (
    <div style={{ padding: 16 }}>
      <div style={{
        width: '100%', aspectRatio: isImg ? '4/3' : (isPdf || isVid ? '4/3' : '3/4'),
        borderRadius: 'var(--r-widget)',
        background: 'var(--surface-2)', border: '1px solid var(--hairline)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        marginBottom: 14, overflow: 'hidden', position: 'relative',
      }}>
        {isImg && (
          <img src={previewUrl} alt={name}
               style={{ width: '100%', height: '100%', objectFit: 'contain', background: '#000' }}/>
        )}
        {isVid && (
          <video controls preload="metadata" src={previewUrl}
                 style={{ width: '100%', height: '100%' }} />
        )}
        {isPdf && (
          <embed src={previewUrl} type="application/pdf"
                 style={{ width: '100%', height: '100%' }} />
        )}
        {isMd && (
          <div style={{ padding: 18, color: 'var(--fg-2)', overflow: 'auto', width: '100%', height: '100%', fontSize: 'var(--t-sm)', lineHeight: 1.6 }}>
            {loading && <Spinner label="Loading…" inline size={16} />}
            {!loading && textBody != null && (
              safeMd != null
                ? <div className="md-render" dangerouslySetInnerHTML={{ __html: safeMd }} />
                : <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{textBody}</pre>
            )}
          </div>
        )}
        {isText && (
          <div style={{ padding: 18, width: '100%', height: '100%', overflow: 'auto' }}>
            {loading && <Spinner label="Loading…" inline size={16} />}
            {!loading && textBody != null && (
              <pre className="mono" style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 'var(--t-cap)', color: 'var(--fg-2)' }}>{textBody}</pre>
            )}
          </div>
        )}
        {!isImg && !isVid && !isPdf && !isMd && !isText && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, color: 'var(--fg-3)' }}>
            <FileIconBox name={name} size={64}/>
            <div style={{ fontSize: 'var(--t-cap)' }}>no inline preview</div>
          </div>
        )}
      </div>
      <div style={{ fontSize: 'var(--t-h3)', fontWeight: 500, wordBreak: 'break-all' }}>{name}</div>
      <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 4 }}>
        {fpBytesShort(file.size)} · {relTime(file.mtime)}
      </div>
      <div style={{ display: 'flex', gap: 8, marginTop: 16, flexWrap: 'wrap' }}>
        <a href={downloadUrl} download={name} style={{ textDecoration: 'none' }}>
          <Button icon="download">Download</Button>
        </a>
        <Button icon="dots" onClick={onRename}>Rename</Button>
        <Button variant="danger" icon="trash" onClick={onDelete}>Delete</Button>
      </div>
    </div>
  );
}

window.FilePreview = FilePreview;
