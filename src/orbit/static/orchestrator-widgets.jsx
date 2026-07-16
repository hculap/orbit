// orchestrator-widgets.jsx — agent-emitted widget block renderers.
//
// All seven widgets render only POST envelope-finalize (structured_blocks SSE
// event), never half-rendered during streaming. Each component is wrapped in
// `WidgetFrame` for visual parity with ToolUseBlock (surface-1 + 12px radius
// + hairline border). NONE of these kinds belong in `_TOOL_ACTION_KINDS` —
// they are user-facing content that must render even when "Pokazuj akcje
// modelu" is OFF.
//
// Cross-file imports come via window globals (no bundler).

const { useEffect: _useEffect, useMemo: _useMemo, useRef: _useRef, useState: _useState } = React;

// ─────────────────────────────────────────────────────────────
// WidgetFrame — shared chrome for every widget block.
// surface-1 background, hairline border, 12px radius. Optional title chip
// rendered as small mono uppercase header above the children.
// ─────────────────────────────────────────────────────────────
function WidgetFrame({ title, children }) {
  return (
    <div style={{
      background: 'var(--surface-1)',
      border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-widget)',
      overflow: 'hidden',
    }}>
      {title && (
        <div className="mono" style={{
          padding: '8px 12px',
          fontSize: 'var(--t-2xs)',
          color: 'var(--fg-3)',
          textTransform: 'uppercase',
          letterSpacing: '0.06em',
          borderBottom: '1px solid var(--hairline)',
        }}>{title}</div>
      )}
      {children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// AudioBlock — native <audio controls> with optional alt caption.
// ─────────────────────────────────────────────────────────────
function AudioBlock({ block, sessionId }) {
  if (!block || typeof block.path !== 'string' || !block.path) {
    return <ErrorBlock block={{ text: 'audio block: missing path' }} />;
  }
  const src = previewUrl(sessionId, { name: block.path });
  return (
    <WidgetFrame title={block.title}>
      <div style={{ padding: 12 }}>
        <audio controls preload="metadata" src={src}
          style={{ width: '100%', display: 'block' }} />
        {block.alt && (
          <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', marginTop: 6 }}>
            {block.alt}
          </div>
        )}
      </div>
    </WidgetFrame>
  );
}

// ─────────────────────────────────────────────────────────────
// DownloadBlock — file card with icon, filename, size, mime pill.
// ─────────────────────────────────────────────────────────────
function DownloadBlock({ block, sessionId }) {
  if (!block || typeof block.path !== 'string' || !block.path) {
    return <ErrorBlock block={{ text: 'download block: missing path' }} />;
  }
  const [hover, setHover] = _useState(false);
  const url = previewUrl(sessionId, { name: block.path });
  const filename = block.filename || block.path;
  const sizeLabel = (block.size != null) ? fmtBytes(block.size) : '';
  return (
    <WidgetFrame title={block.title}>
      <a href={url} download={filename} target="_blank" rel="noopener"
        onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        style={{
          display: 'flex', alignItems: 'center', gap: 12,
          padding: '10px 12px',
          background: hover ? 'var(--surface-2)' : 'transparent',
          color: 'var(--fg)', textDecoration: 'none',
          transition: 'background .12s',
        }}>
        <Icon name="download" size={20} color="var(--fg-2)" stroke={1.6} />
        <span className="mono" style={{
          flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', color: 'var(--fg)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>{filename}</span>
        {block.mime && (
          <span className="mono" style={{
            fontSize: 'var(--t-2xs)', color: 'var(--fg-3)',
            padding: '2px 6px', borderRadius: 'var(--r-pill)',
            background: 'var(--surface-2)', border: '1px solid var(--hairline)',
            whiteSpace: 'nowrap',
          }}>{block.mime}</span>
        )}
        {sizeLabel && (
          <span className="mono" style={{
            fontSize: 'var(--t-xs)', color: 'var(--fg-3)', whiteSpace: 'nowrap',
          }}>{sizeLabel}</span>
        )}
      </a>
    </WidgetFrame>
  );
}

// ─────────────────────────────────────────────────────────────
// VideoBlock — native <video controls>. Native fullscreen handles itself.
// ─────────────────────────────────────────────────────────────
function VideoBlock({ block, sessionId }) {
  if (!block || typeof block.path !== 'string' || !block.path) {
    return <ErrorBlock block={{ text: 'video block: missing path' }} />;
  }
  const src = previewUrl(sessionId, { name: block.path });
  const poster = block.poster_path ? previewUrl(sessionId, { name: block.poster_path }) : undefined;
  return (
    <WidgetFrame title={block.title}>
      <video controls preload="metadata" poster={poster} src={src}
        style={{
          width: '100%', maxHeight: 480, borderRadius: 'var(--r-control)',
          background: '#000', display: 'block',
        }} />
    </WidgetFrame>
  );
}

// ─────────────────────────────────────────────────────────────
// YouTubeBlock — privacy-enhanced (youtube-nocookie) iframe, 16:9.
// ─────────────────────────────────────────────────────────────
const _YT_ID_RE = /^[A-Za-z0-9_-]{11}$/;

function YouTubeBlock({ block }) {
  if (!block || typeof block.video_id !== 'string' || !_YT_ID_RE.test(block.video_id)) {
    return <ErrorBlock block={{ text: 'youtube block: invalid video_id' }} />;
  }
  const id = block.video_id;
  const start = (block.start != null && Number.isFinite(Number(block.start)) && Number(block.start) > 0)
    ? Math.floor(Number(block.start)) : 0;
  const src = `https://www.youtube-nocookie.com/embed/${id}${start ? `?start=${start}` : ''}`;
  return (
    <WidgetFrame title={block.title}>
      <div style={{ position: 'relative', paddingBottom: '56.25%', height: 0 }}>
        <iframe loading="lazy"
          src={src}
          allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
          allowFullScreen
          referrerPolicy="strict-origin-when-cross-origin"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0 }}
        />
      </div>
    </WidgetFrame>
  );
}

// ─────────────────────────────────────────────────────────────
// ChartBlock — Chart.js 4.x. Stable JSON key gates re-instantiation.
// ─────────────────────────────────────────────────────────────
function ChartBlock({ block }) {
  if (!window.Chart) {
    return <ErrorBlock block={{ text: 'Chart.js failed to load' }} />;
  }
  if (!block || typeof block.chart_type !== 'string' || !block.chart_type) {
    return <ErrorBlock block={{ text: 'chart block: missing chart_type' }} />;
  }
  if (!block.data || !Array.isArray(block.data.datasets)) {
    return <ErrorBlock block={{ text: 'chart block: missing data.datasets' }} />;
  }
  const canvasRef = _useRef(null);
  const instanceRef = _useRef(null);
  const stableKey = _useMemo(
    () => JSON.stringify({ t: block.chart_type, d: block.data, o: block.options }),
    [block.chart_type, block.data, block.options],
  );
  _useEffect(() => {
    if (!canvasRef.current || !window.Chart) return undefined;
    if (instanceRef.current) {
      try { instanceRef.current.destroy(); } catch (e) { /* ignore */ }
      instanceRef.current = null;
    }
    try {
      instanceRef.current = new window.Chart(canvasRef.current, {
        type: block.chart_type,
        data: block.data,
        options: {
          responsive: true,
          maintainAspectRatio: false,
          ...(block.options || {}),
        },
      });
    } catch (e) {
      // Chart.js throws synchronously on bad config — leave canvas blank.
    }
    return () => {
      if (instanceRef.current) {
        try { instanceRef.current.destroy(); } catch (e) { /* ignore */ }
        instanceRef.current = null;
      }
    };
  }, [stableKey]);
  return (
    <WidgetFrame title={block.title}>
      <div style={{ position: 'relative', height: 360, padding: 8 }}>
        <canvas ref={canvasRef} />
      </div>
    </WidgetFrame>
  );
}

// ─────────────────────────────────────────────────────────────
// MapBlock — Leaflet tiles + markers + optional polyline route.
// ─────────────────────────────────────────────────────────────
function MapBlock({ block }) {
  if (!window.L) {
    return <ErrorBlock block={{ text: 'Leaflet failed to load' }} />;
  }
  if (!block || !Array.isArray(block.center) || block.center.length !== 2) {
    return <ErrorBlock block={{ text: 'map block: invalid center' }} />;
  }
  const divRef = _useRef(null);
  const mapRef = _useRef(null);
  const stableKey = _useMemo(
    () => JSON.stringify({ c: block.center, z: block.zoom, m: block.markers, r: block.route }),
    [block.center, block.zoom, block.markers, block.route],
  );
  _useEffect(() => {
    if (!divRef.current || !window.L) return undefined;
    if (mapRef.current) {
      try { mapRef.current.remove(); } catch (e) { /* ignore */ }
      mapRef.current = null;
    }
    const map = window.L.map(divRef.current, { scrollWheelZoom: false })
      .setView(block.center, block.zoom != null ? block.zoom : 13);
    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap',
      maxZoom: 19,
    }).addTo(map);
    (block.markers || []).forEach(m => {
      if (!m || typeof m.lat !== 'number' || typeof m.lng !== 'number') return;
      const marker = window.L.marker([m.lat, m.lng]).addTo(map);
      if (m.label) marker.bindPopup(String(m.label));
    });
    if (Array.isArray(block.route) && block.route.length >= 2) {
      const accent = (getComputedStyle(document.documentElement).getPropertyValue('--accent') || '#a78bfa').trim();
      window.L.polyline(block.route, { color: accent, weight: 3 }).addTo(map);
    }
    mapRef.current = map;
    // Tile size depends on the container being laid out. Defer one tick so
    // Leaflet picks up the actual width/height.
    setTimeout(() => { try { map.invalidateSize(); } catch (e) { /* ignore */ } }, 0);
    return () => {
      try { map.remove(); } catch (e) { /* ignore */ }
      mapRef.current = null;
    };
  }, [stableKey]);
  return (
    <WidgetFrame title={block.title}>
      <div ref={divRef} style={{ height: 360, width: '100%' }} />
    </WidgetFrame>
  );
}

// ─────────────────────────────────────────────────────────────
// CustomHtmlBlock — sandboxed iframe (allow-scripts allow-same-origin).
// 200KB cap matches the envelope-side validation; defence in depth.
// ─────────────────────────────────────────────────────────────
const _CUSTOM_HTML_MAX = 200 * 1024;

function CustomHtmlBlock({ block }) {
  if (!block || typeof block.html !== 'string' || !block.html) {
    return <ErrorBlock block={{ text: 'custom_html block: missing html' }} />;
  }
  if (block.html.length > _CUSTOM_HTML_MAX) {
    return <ErrorBlock block={{ text: 'custom_html block: html exceeds 200KB' }} />;
  }
  const height = (typeof block.height === 'number' && block.height > 0) ? block.height : 400;
  return (
    <WidgetFrame title={block.title}>
      <iframe srcDoc={block.html}
        sandbox="allow-scripts allow-same-origin"
        referrerPolicy="no-referrer"
        style={{
          width: '100%', height, border: 0,
          display: 'block', background: 'white',
        }} />
    </WidgetFrame>
  );
}

// Publish to window so orchestrator-blocks.jsx can use these as bare globals
// from its BlockRenderer switch.
Object.assign(window, {
  WidgetFrame, AudioBlock, DownloadBlock, VideoBlock,
  YouTubeBlock, ChartBlock, MapBlock, CustomHtmlBlock,
});
