// orchestrator-artifacts.jsx — artifact gallery + viewer modal + persistent
// SSE notification channel.
//
// Artifacts are rich deliverables the agent creates via the `artifact` CLI,
// stored under <cwd>/.artifacts/ (or ~/.orchestrator/artifacts/global/) and
// served by /api/orchestrator/artifacts*. This file REUSES the widget
// renderers published by orchestrator-widgets.jsx (ChartBlock/MapBlock/
// YouTubeBlock/CustomHtmlBlock take pure-data props; audio/video/image/file
// get thin wrappers that source bytes from the artifact /file route — NOT
// previewUrl, which targets /uploads/ and would 404).
//
// Cross-file deps come via window globals (no bundler): WidgetFrame,
// ChartBlock, MapBlock, YouTubeBlock, CustomHtmlBlock, ErrorBlock (renderers);
// Card, Modal, KebabMenu, Button, Icon, useToast, apiUrl, relTime, fmtBytes
// (components.jsx). This file loads after both, before orchestrator.jsx.

const { useState: _aUseState, useEffect: _aUseEffect, useRef: _aUseRef, useCallback: _aUseCallback } = React;

const _ART_TYPE_ICON = {
  image: 'image', audio: 'volume', video: 'video', youtube: 'video',
  chart: 'sparkle', map: 'globe', html: 'globe', file: 'file',
};

const _ART_TYPE_LABEL = {
  image: 'Obraz', audio: 'Audio', video: 'Wideo', youtube: 'YouTube',
  chart: 'Wykres', map: 'Mapa', html: 'HTML', file: 'Plik',
};

// Sort options for the gallery toolbar. `cmp` operates on two artifacts.
const _ART_SORTS = {
  'date-desc': { label: 'Najnowsze', cmp: (a, b) => _artTime(b) - _artTime(a) },
  'date-asc': { label: 'Najstarsze', cmp: (a, b) => _artTime(a) - _artTime(b) },
  'name-asc': { label: 'Nazwa A→Z', cmp: (a, b) => _artName(a).localeCompare(_artName(b)) },
  'name-desc': { label: 'Nazwa Z→A', cmp: (a, b) => _artName(b).localeCompare(_artName(a)) },
};

function _artTime(a) { const t = Date.parse(a && a.created_at); return Number.isFinite(t) ? t : 0; }
function _artName(a) { return ((a && (a.title || a.id)) || '').toLowerCase(); }

// Scope → query string for the artifact routes. Session scope wins; agent
// scope sends lib_id (the __global__ sentinel maps to the global agent dir).
function _artScopeQs(scope) {
  if (scope && scope.sessionId) {
    return 'session_id=' + encodeURIComponent(scope.sessionId);
  }
  const lib = scope && scope.libId ? scope.libId : '__global__';
  return 'lib_id=' + encodeURIComponent(lib);
}

function artifactRawUrl(id, scope) {
  return apiUrl('/api/orchestrator/artifacts/' + encodeURIComponent(id) + '/file?' + _artScopeQs(scope));
}

// Full-page render URL for an html artifact (served as sandboxed text/html) —
// used to open it full-screen in a new tab.
function artifactViewUrl(id, scope) {
  return apiUrl('/api/orchestrator/artifacts/' + encodeURIComponent(id) + '/view?' + _artScopeQs(scope));
}

// Open an html artifact full-screen in a new tab. `noopener` severs the
// window.opener back-reference (the sandboxed doc can't reach back anyway).
function _artifactOpenFullscreen(artifact, scope) {
  window.open(artifactViewUrl(artifact.id, scope), '_blank', 'noopener');
}

function _artifactThumbUrl(id, scope, size) {
  return apiUrl('/api/orchestrator/artifacts/' + encodeURIComponent(id) + '/thumb?' + _artScopeQs(scope) + '&size=' + (size || 256));
}

function _relFromIso(iso) {
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? relTime(Math.floor(ms / 1000)) : '';
}

// ── media engine handoff (audio/video) ───────────────────────────────
//
// Build a media-engine track from an artifact — resolves the bytes URL, the
// (video) poster, and a stable resume key matching the engine's keying.
function makeArtifactTrack(artifact, scope) {
  const type = artifact.type;
  return {
    id: artifact.id,
    scope,
    type,
    title: artifact.title || artifact.id,
    url: artifactRawUrl(artifact.id, scope),
    poster: type === 'video' ? _artifactThumbUrl(artifact.id, scope, 256) : null,
    key: window.makeMediaTrackKey
      ? window.makeMediaTrackKey(type, scope, artifact.id)
      : (type + '|' + (scope && scope.sessionId ? 's:' + scope.sessionId : 'l:' + ((scope && scope.libId) || '__global__')) + '|' + artifact.id),
  };
}

// AUTO-handoff on Play: promote the clicked artifact to the global player with a
// queue snapshot of its same-type siblings (drives autoplay-next / prev / next).
function _mediaPlay(artifact, scope, siblings) {
  if (!window.HubPlayer) return;
  const sameType = (siblings || []).filter((a) => a && a.type === artifact.type);
  const list = sameType.length ? sameType : [artifact];
  const index = Math.max(0, list.findIndex((a) => a.id === artifact.id));
  window.HubPlayer.play(makeArtifactTrack(artifact, scope), {
    queue: list.map((a) => makeArtifactTrack(a, scope)),
    index,
  });
}

// Rich inline audio surface (shown when this audio artifact is the active track).
function _InlineAudioControls({ player }) {
  const C = window.HubMediaControls || {};
  return (
    <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {C.ProgressScrubber && <div style={{ flex: 1, minWidth: 0 }}><C.ProgressScrubber player={player} /></div>}
        {C.TimeLabel && <C.TimeLabel />}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {C.PrevNextButtons && <C.PrevNextButtons player={player} />}
        {C.SkipBackButton && <C.SkipBackButton player={player} />}
        {C.PlayPauseButton && <C.PlayPauseButton player={player} size={44} />}
        {C.SkipForwardButton && <C.SkipForwardButton player={player} />}
        {C.SpeedChip && <C.SpeedChip player={player} />}
        {C.AutoplayNextToggle && <C.AutoplayNextToggle player={player} />}
      </div>
    </div>
  );
}

// Inline video surface — hosts the singleton <video> (native controls) via
// owner-token adoption; releases back to the garage on unmount so PiP/audio
// survive navigation. The extra row adds speed/autoplay/PiP/fullscreen.
//
// Known minor limitations (low severity, audio always keeps playing, all
// recoverable by reopening the artifact):
//  1. Tapping Prev/Next on the dock while this modal shows the OLD video makes
//     that video go audio-only (this surface unmounts → element returns to the
//     garage) until you open the now-current artifact. The modal doesn't follow
//     the dock's queue navigation.
//  2. If TWO viewer modals (the gallery's + the SSE `artifact open` one) host the
//     SAME active video at once, the owner-token lets only the first show the
//     picture; the second shows a black slot (audio still plays).
function _InlineVideoSurface({ player }) {
  const slotRef = _aUseRef(null);
  const tokenRef = _aUseRef(null);
  if (!tokenRef.current) tokenRef.current = {};
  React.useLayoutEffect(() => {
    const token = tokenRef.current;
    const slot = slotRef.current;
    if (window.HubPlayer) window.HubPlayer.adoptVideo(slot, token);
    return () => { if (window.HubPlayer) window.HubPlayer.releaseVideo(token); };
  }, []);
  const C = window.HubMediaControls || {};
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 12 }}>
      <div ref={slotRef} style={{ position: 'relative', width: '100%', aspectRatio: '16 / 9', background: '#000', borderRadius: 'var(--r-control)', overflow: 'hidden' }} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        {C.SkipBackButton && <C.SkipBackButton player={player} />}
        {C.SkipForwardButton && <C.SkipForwardButton player={player} />}
        {C.SpeedChip && <C.SpeedChip player={player} />}
        {C.AutoplayNextToggle && <C.AutoplayNextToggle player={player} />}
        {C.PipButton && <C.PipButton player={player} />}
        {C.FullscreenButton && <C.FullscreenButton player={player} />}
      </div>
    </div>
  );
}

// Poster + big Play overlay for a not-yet-playing video artifact.
function _VideoPosterPlay({ artifact, scope, onPlay }) {
  const poster = _artifactThumbUrl(artifact.id, scope, 512);
  return (
    <div onClick={onPlay} style={{ position: 'relative', width: '100%', aspectRatio: '16 / 9', background: '#000', borderRadius: 'var(--r-control)', overflow: 'hidden', cursor: 'pointer', margin: 12, marginLeft: 0, marginRight: 0 }}>
      <img src={poster} alt={artifact.title || ''} style={{ width: '100%', height: '100%', objectFit: 'contain' }} onError={(e) => { e.currentTarget.style.display = 'none'; }} />
      <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <span style={{ width: 56, height: 56, borderRadius: 'var(--r-pill)', background: 'var(--accent)', color: 'var(--accent-fg)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 12px rgba(0,0,0,.4)' }}>
          <Icon name="play" size={26} color="currentColor" style={{ marginLeft: 3 }} />
        </span>
      </div>
    </div>
  );
}

// Play prompt for a not-yet-playing audio artifact.
function _AudioPlayPrompt({ onPlay }) {
  const [hover, setHover] = _aUseState(false);
  return (
    <button type="button" onClick={onPlay}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%', padding: 12, border: 'none', cursor: 'pointer', fontFamily: 'inherit', background: hover ? 'var(--surface-2)' : 'transparent', color: 'var(--fg)' }}>
      <span style={{ width: 40, height: 40, flexShrink: 0, borderRadius: 'var(--r-pill)', background: 'var(--accent)', color: 'var(--accent-fg)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Icon name="play" size={20} color="currentColor" style={{ marginLeft: 2 }} />
      </span>
      <span style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 2, minWidth: 0 }}>
        <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600 }}>Odtwórz audio</span>
        <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>w trwałym odtwarzaczu</span>
      </span>
    </button>
  );
}

// ── viewers (reuse the widget renderers / thin wrappers) ─────────────

function _ArtifactHtml({ artifact, scope }) {
  const [html, setHtml] = _aUseState(null);
  const [err, setErr] = _aUseState(false);
  _aUseEffect(() => {
    let cancelled = false;
    fetch(artifactRawUrl(artifact.id, scope))
      .then(r => (r.ok ? r.text() : Promise.reject(r.status)))
      .then(t => { if (!cancelled) setHtml(t); })
      .catch(() => { if (!cancelled) setErr(true); });
    return () => { cancelled = true; };
  }, [artifact.id]);
  if (err) return <ErrorBlock block={{ text: 'artifact html: load failed' }} />;
  if (html == null) return <Spinner label="ładowanie…" inline />;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <CustomHtmlBlock block={{ html, title: artifact.title, height: (artifact.extra && artifact.extra.height) || 400 }} />
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button icon="maximize" onClick={() => _artifactOpenFullscreen(artifact, scope)}>Pełny ekran</Button>
      </div>
    </div>
  );
}

function _ArtifactImage({ artifact, scope }) {
  const url = artifactRawUrl(artifact.id, scope);
  return (
    <WidgetFrame title={artifact.title}>
      <img
        src={url}
        alt={(artifact.extra && artifact.extra.alt) || artifact.title || ''}
        onClick={() => window.open(url, '_blank', 'noopener')}
        style={{ width: '100%', display: 'block', cursor: 'zoom-in', maxHeight: 600, objectFit: 'contain', background: 'var(--surface-2)' }}
      />
    </WidgetFrame>
  );
}

function _ArtifactAudio({ artifact, scope, siblings }) {
  const player = window.useMediaPlayer ? window.useMediaPlayer() : null;
  // Degrade to the bare native player when the engine is absent.
  if (!window.HubPlayer || !player) {
    return (
      <WidgetFrame title={artifact.title}>
        <div style={{ padding: 12 }}>
          <audio controls preload="metadata" src={artifactRawUrl(artifact.id, scope)} style={{ width: '100%', display: 'block' }} />
        </div>
      </WidgetFrame>
    );
  }
  const isCur = window.HubPlayer.isCurrent(artifact.id, scope);
  return (
    <WidgetFrame title={artifact.title}>
      {isCur
        ? <_InlineAudioControls player={player} />
        : <_AudioPlayPrompt onPlay={() => _mediaPlay(artifact, scope, siblings)} />}
    </WidgetFrame>
  );
}

function _ArtifactVideo({ artifact, scope, siblings }) {
  const player = window.useMediaPlayer ? window.useMediaPlayer() : null;
  if (!window.HubPlayer || !player) {
    return (
      <WidgetFrame title={artifact.title}>
        <video controls preload="metadata" src={artifactRawUrl(artifact.id, scope)}
          style={{ width: '100%', maxHeight: 480, borderRadius: 'var(--r-control)', background: '#000', display: 'block' }} />
      </WidgetFrame>
    );
  }
  const isCur = window.HubPlayer.isCurrent(artifact.id, scope);
  return (
    <WidgetFrame title={artifact.title}>
      {isCur
        ? <_InlineVideoSurface player={player} />
        : <_VideoPosterPlay artifact={artifact} scope={scope} onPlay={() => _mediaPlay(artifact, scope, siblings)} />}
    </WidgetFrame>
  );
}

function _ArtifactDownload({ artifact, scope }) {
  const [hover, setHover] = _aUseState(false);
  const url = artifactRawUrl(artifact.id, scope);
  const filename = artifact.src || artifact.title || artifact.id;
  const sizeLabel = artifact.size != null ? fmtBytes(artifact.size) : '';
  return (
    <WidgetFrame title={artifact.title}>
      <a href={url} download={filename} target="_blank" rel="noopener"
        onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '10px 12px',
          background: hover ? 'var(--surface-2)' : 'transparent', color: 'var(--fg)', textDecoration: 'none' }}>
        <Icon name="download" size={20} color="var(--fg-2)" stroke={1.6} />
        <span className="mono" style={{ flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{filename}</span>
        {artifact.mime && (
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', padding: '2px 6px', borderRadius: 'var(--r-pill)', background: 'var(--surface-2)', border: '1px solid var(--hairline)', whiteSpace: 'nowrap' }}>{artifact.mime}</span>
        )}
        {sizeLabel && <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', whiteSpace: 'nowrap' }}>{sizeLabel}</span>}
      </a>
    </WidgetFrame>
  );
}

function renderArtifactBody(artifact, scope, siblings) {
  const ex = artifact.extra || {};
  switch (artifact.type) {
    case 'chart':
      return <ChartBlock block={{ chart_type: ex.chart_type, data: ex.data, options: ex.options, title: artifact.title }} />;
    case 'map':
      return <MapBlock block={{ center: ex.center, zoom: ex.zoom, markers: ex.markers, route: ex.route, title: artifact.title }} />;
    case 'youtube':
      return <YouTubeBlock block={{ video_id: ex.video_id, start: ex.start, title: artifact.title }} />;
    case 'html':
      return <_ArtifactHtml artifact={artifact} scope={scope} />;
    case 'image':
      return <_ArtifactImage artifact={artifact} scope={scope} />;
    case 'audio':
      return <_ArtifactAudio artifact={artifact} scope={scope} siblings={siblings} />;
    case 'video':
      return <_ArtifactVideo artifact={artifact} scope={scope} siblings={siblings} />;
    default:
      return <_ArtifactDownload artifact={artifact} scope={scope} />;
  }
}

// ── mutations (shared by card + modal) ───────────────────────────────

function _artifactDownload(artifact, scope) {
  const link = document.createElement('a');
  link.href = artifactRawUrl(artifact.id, scope);
  link.download = artifact.src || artifact.title || artifact.id;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function _artifactDuplicate(artifact, scope, toast) {
  try {
    const r = await fetch(apiUrl('/api/orchestrator/artifacts/' + encodeURIComponent(artifact.id) + '/duplicate?' + _artScopeQs(scope)), { method: 'POST' });
    if (!r.ok) throw new Error(String(r.status));
    toast && toast('Zduplikowano', 'ok');
    window.dispatchEvent(new CustomEvent('orchestrator:artifacts-changed'));
    return true;
  } catch (e) {
    toast && toast('Duplikacja nieudana', 'err');
    return false;
  }
}

async function _artifactDelete(artifact, scope, toast) {
  try {
    const r = await fetch(apiUrl('/api/orchestrator/artifacts/' + encodeURIComponent(artifact.id) + '?' + _artScopeQs(scope)), { method: 'DELETE' });
    if (!r.ok) throw new Error(String(r.status));
    toast && toast('Usunięto', 'ok');
    window.dispatchEvent(new CustomEvent('orchestrator:artifacts-changed'));
    return true;
  } catch (e) {
    toast && toast('Usunięcie nieudane', 'err');
    return false;
  }
}

// ── comment (drop the artifact's local path into the live session) ───

// Pure-spec types (chart/map/youtube) have no payload file on disk → there's
// no path to paste; reference them by id instead.
const _ART_SPEC_TYPES = { chart: true, map: true, youtube: true };

// "Skomentuj": hand the artifact to the live session so the user can ask
// claude about it. Resolves the artifact's ABSOLUTE local path (claude runs
// on the box and Reads files by path — exactly like an uploaded attachment)
// and fires `orchestrator:artifact-reference`. OrchestratorView routes it to
// the session's active surface: pasted into the live tmux terminal (the
// default), or appended to the chat composer if the session is in chat mode.
// No clipboard, no view-mode hijack to chat.
async function _artifactComment(artifact, scope, toast) {
  let text;
  if (_ART_SPEC_TYPES[artifact.type]) {
    text = 'artefakt „' + (artifact.title || artifact.id) + '” (' + artifact.type + ', id: ' + artifact.id + ')';
  } else {
    try {
      const r = await fetch(apiUrl('/api/orchestrator/artifacts/' + encodeURIComponent(artifact.id) + '/path?' + _artScopeQs(scope)));
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      text = d && d.path ? d.path : null;
    } catch (e) {
      toast && toast('Nie udało się pobrać ścieżki artefaktu', 'err');
      return;
    }
    if (!text) { toast && toast('Brak pliku artefaktu', 'err'); return; }
  }
  window.dispatchEvent(new CustomEvent('orchestrator:artifact-reference', { detail: { text } }));
}

// ── card ─────────────────────────────────────────────────────────────

function ArtifactCard({ artifact, scope, onOpen, siblings }) {
  const toast = useToast();
  const [confirming, setConfirming] = _aUseState(false);
  const a = artifact;
  const thumbUrl = a.type === 'image' ? _artifactThumbUrl(a.id, scope, 256) : null;
  const isMedia = a.type === 'audio' || a.type === 'video';
  const items = [
    { icon: 'eye', label: 'Otwórz', onClick: () => onOpen(a) },
    ...(a.type === 'html'
      ? [{ icon: 'maximize', label: 'Pełny ekran', onClick: () => _artifactOpenFullscreen(a, scope) }]
      : []),
    ...(isMedia && window.HubPlayer
      ? [{ icon: 'plus', label: 'Dodaj do kolejki', onClick: () => window.HubPlayer.enqueue(makeArtifactTrack(a, scope)) }]
      : []),
    { icon: 'corner-up-left', label: 'Skomentuj', onClick: () => _artifactComment(a, scope, toast) },
    { icon: 'download', label: 'Pobierz', onClick: () => _artifactDownload(a, scope) },
    { icon: 'copy', label: 'Duplikuj', onClick: () => _artifactDuplicate(a, scope, toast) },
    { icon: 'trash', label: 'Usuń', onClick: () => setConfirming(true), danger: true },
  ];
  return (
    <>
      {/* No overflow:hidden on the Card — it would clip the kebab dropdown
          (position:absolute). The thumbnail clips itself + rounds its own top
          corners to match the card radius instead. */}
      <Card hover padding={0} onClick={() => onOpen(a)} style={{ minWidth: 0 }}>
        <div style={{ position: 'relative', height: 120, background: 'var(--surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden', borderTopLeftRadius: 'calc(var(--r-lg) - 1px)', borderTopRightRadius: 'calc(var(--r-lg) - 1px)' }}>
          {(a.type === 'video' || thumbUrl)
            ? <img src={a.type === 'video' ? _artifactThumbUrl(a.id, scope, 256) : thumbUrl} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} onError={e => { e.currentTarget.style.display = 'none'; }} />
            : <Icon name={_ART_TYPE_ICON[a.type] || 'file'} size={32} color="var(--fg-3)" />}
          {isMedia && window.HubPlayer && (
            <button type="button" aria-label="Odtwórz" onClick={(e) => { e.stopPropagation(); _mediaPlay(a, scope, siblings); }}
              style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'transparent', border: 'none', cursor: 'pointer', padding: 0 }}>
              <span style={{ width: 44, height: 44, borderRadius: 'var(--r-pill)', background: 'var(--accent)', color: 'var(--accent-fg)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 10px rgba(0,0,0,.35)' }}>
                <Icon name="play" size={22} color="currentColor" style={{ marginLeft: 2 }} />
              </span>
            </button>
          )}
          {isMedia && window.HubPlayer && (
            <button type="button" aria-label="Dodaj do kolejki" title="Dodaj do kolejki"
              onClick={(e) => { e.stopPropagation(); window.HubPlayer.enqueue(makeArtifactTrack(a, scope)); }}
              style={{ position: 'absolute', top: 6, right: 6, width: 30, height: 30, borderRadius: 'var(--r-pill)', background: 'oklch(0.20 0.012 264 / 0.74)', border: '1px solid var(--hairline)', color: 'var(--fg)', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', padding: 0, backdropFilter: 'blur(2px)' }}>
              <Icon name="plus" size={16} color="currentColor" />
            </button>
          )}
        </div>
        <div style={{ padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span className="mono" style={{ fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--fg-3)', padding: '2px 6px', borderRadius: 'var(--r-pill)', background: 'var(--surface-2)', border: '1px solid var(--hairline)' }}>{a.type}</span>
            <span style={{ flex: 1 }} />
            <span onClick={e => e.stopPropagation()}><KebabMenu items={items} /></span>
          </div>
          <div style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.title || a.id}</div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{_relFromIso(a.created_at)}</div>
        </div>
      </Card>
      {confirming && (
        <Modal open onClose={() => setConfirming(false)} title="Usunąć artefakt?" width={360}>
          <div style={{ padding: '4px 16px 16px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-2)' }}>„{a.title || a.id}” zostanie trwale usunięty.</div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Button onClick={() => setConfirming(false)}>Anuluj</Button>
              <Button variant="danger" icon="trash" onClick={async () => { const ok = await _artifactDelete(a, scope, toast); if (ok) setConfirming(false); }}>Usuń</Button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

// ── viewer modal ─────────────────────────────────────────────────────

function ArtifactViewerModal({ artifact, scope, open, onClose, siblings }) {
  const toast = useToast();
  if (!artifact) return null;
  return (
    <Modal open={open} onClose={onClose} title={artifact.title || artifact.type} width={760}>
      <div style={{ padding: '4px 16px 16px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        {renderArtifactBody(artifact, scope, siblings)}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          {artifact.type === 'html' && (
            <Button icon="maximize" onClick={() => _artifactOpenFullscreen(artifact, scope)}>Pełny ekran</Button>
          )}
          {(artifact.type === 'audio' || artifact.type === 'video') && window.HubPlayer && (
            <Button icon="plus" onClick={() => { window.HubPlayer.enqueue(makeArtifactTrack(artifact, scope)); }}>Do kolejki</Button>
          )}
          <Button icon="corner-up-left" onClick={() => _artifactComment(artifact, scope, toast)}>Skomentuj</Button>
          <Button icon="download" onClick={() => _artifactDownload(artifact, scope)}>Pobierz</Button>
          <Button icon="copy" onClick={() => _artifactDuplicate(artifact, scope, toast)}>Duplikuj</Button>
          <Button variant="danger" icon="trash" onClick={async () => { const ok = await _artifactDelete(artifact, scope, toast); if (ok) onClose(); }}>Usuń</Button>
        </div>
      </div>
    </Modal>
  );
}

// ── gallery toolbar (type filter + sort + search) ────────────────────

function _GalleryToolbar({ counts, selected, onToggle, onClearTypes, sort, onSort, query, onQuery }) {
  const types = Object.keys(counts);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '12px 12px 0' }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <div style={{ flex: 1, minWidth: 140 }}>
          <Input
            value={query} onChange={onQuery} placeholder="Szukaj po nazwie…"
            icon="search" size="sm"
            style={{ width: '100%' }}
          />
        </div>
        <Select
          value={sort} onChange={onSort}
          options={Object.keys(_ART_SORTS).map(k => ({ value: k, label: _ART_SORTS[k].label }))}
          size="sm"
        />
      </div>
      {types.length > 1 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
          {types.map(t => {
            const on = selected.has(t);
            return (
              <button key={t} onClick={() => onToggle(t)} title={_ART_TYPE_LABEL[t] || t}
                style={{
                  display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer',
                  fontSize: 'var(--t-xs)', padding: '4px 9px', borderRadius: 'var(--r-pill)', fontFamily: 'inherit',
                  border: '1px solid ' + (on ? 'var(--accent)' : 'var(--hairline)'),
                  background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
                  color: on ? 'var(--accent)' : 'var(--fg-2)',
                }}>
                <Icon name={_ART_TYPE_ICON[t] || 'file'} size={12} color="currentColor" />
                {_ART_TYPE_LABEL[t] || t}
                <span style={{ opacity: 0.6 }}>{counts[t]}</span>
              </button>
            );
          })}
          {selected.size > 0 && (
            <button onClick={onClearTypes} style={{
              fontSize: 'var(--t-xs)', padding: '4px 9px', borderRadius: 'var(--r-pill)', cursor: 'pointer', fontFamily: 'inherit',
              border: '1px solid var(--hairline)', background: 'transparent', color: 'var(--fg-3)',
            }}>Wyczyść</button>
          )}
        </div>
      )}
    </div>
  );
}

// ── gallery ──────────────────────────────────────────────────────────

function ArtifactGallery({ sessionId, libId, scope, compact }) {
  const [items, setItems] = _aUseState(null);
  const [err, setErr] = _aUseState(null);
  const [nonce, setNonce] = _aUseState(0);
  const [viewing, setViewing] = _aUseState(null);
  const [selectedTypes, setSelectedTypes] = _aUseState(() => new Set());
  const [sort, setSort] = _aUseState('date-desc');
  const [query, setQuery] = _aUseState('');
  const scopeObj = scope === 'session' ? { sessionId } : { libId: libId || null };

  _aUseEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    const url = scope === 'session'
      ? apiUrl('/api/orchestrator/artifacts?session_id=' + encodeURIComponent(sessionId || ''))
      : apiUrl('/api/orchestrator/artifacts?lib_id=' + encodeURIComponent(libId || '__global__'));
    fetch(url, { signal: ctrl.signal })
      .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(d => { if (!cancelled) { setItems(Array.isArray(d.artifacts) ? d.artifacts : []); setErr(null); } })
      .catch(e => { if (!cancelled && e && e.name !== 'AbortError') setErr(String(e)); });
    const onChanged = () => setNonce(n => n + 1);
    window.addEventListener('orchestrator:artifacts-changed', onChanged);
    return () => { cancelled = true; ctrl.abort(); window.removeEventListener('orchestrator:artifacts-changed', onChanged); };
  }, [sessionId, libId, scope, nonce]);

  if (err) {
    return <StatusBanner variant="err" label={'Błąd ładowania artefaktów: ' + err} inline />;
  }
  if (items === null) {
    return <Spinner label="ładowanie…" inline />;
  }
  if (!items.length) {
    return (
      <EmptyState icon="sparkle" message="Brak artefaktów." sub={'Agent tworzy je poleceniem artifact create'} centered padded />
    );
  }
  // Type counts over the FULL set (so a chip's count is stable regardless of
  // the active search/type filter). Visible = type filter ∩ name search,
  // then sorted by the chosen key.
  const counts = items.reduce((acc, a) => { acc[a.type] = (acc[a.type] || 0) + 1; return acc; }, {});
  const toggleType = (t) => setSelectedTypes(prev => {
    const next = new Set(prev);
    if (next.has(t)) next.delete(t); else next.add(t);
    return next;
  });
  const q = query.trim().toLowerCase();
  const visible = items
    .filter(a => selectedTypes.size === 0 || selectedTypes.has(a.type))
    .filter(a => !q || _artName(a).includes(q))
    .slice()
    .sort((_ART_SORTS[sort] || _ART_SORTS['date-desc']).cmp);

  return (
    <>
      <_GalleryToolbar
        counts={counts} selected={selectedTypes} onToggle={toggleType}
        onClearTypes={() => setSelectedTypes(new Set())}
        sort={sort} onSort={setSort} query={query} onQuery={setQuery}
      />
      {visible.length === 0 ? (
        <EmptyState message="Brak artefaktów pasujących do filtra." centered padded />
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : 'repeat(auto-fill, minmax(min(200px, 100%), 1fr))', gap: 12, padding: 12 }}>
          {visible.map(a => <ArtifactCard key={a.id} artifact={a} scope={scopeObj} onOpen={setViewing} siblings={visible} />)}
        </div>
      )}
      {viewing && <ArtifactViewerModal artifact={viewing} scope={scopeObj} open onClose={() => setViewing(null)} siblings={visible} />}
    </>
  );
}

// ── persistent SSE notification channel ──────────────────────────────
//
// Owns its OWN EventSource (eventsEsRef) — never shares the turn-stream ref,
// so closeStream()/session-switch teardown can't kill it (and vice-versa).
// Stays open while the panel is mounted for a session; self-heals on drop.
function usePersistentEvents({ sessionId, onCreated, onOpen }) {
  const cbRef = _aUseRef({ onCreated, onOpen });
  cbRef.current = { onCreated, onOpen };
  _aUseEffect(() => {
    if (!sessionId) return undefined;
    let closed = false;
    let es = null;
    let reopenTimer = null;
    let attempt = 0;
    const seen = new Set();
    const onEvt = (kind) => (e) => {
      if (e.lastEventId) {
        if (seen.has(e.lastEventId)) return;
        seen.add(e.lastEventId);
      }
      let data = null;
      try { data = JSON.parse(e.data); } catch (_) { return; }
      const cb = cbRef.current[kind === 'artifact_created' ? 'onCreated' : 'onOpen'];
      if (cb && data && data.artifact) cb(data.artifact);
    };
    const open = () => {
      if (closed) return;
      es = new EventSource(apiUrl('/api/orchestrator/sessions/' + encodeURIComponent(sessionId) + '/events'));
      es.addEventListener('artifact_created', onEvt('artifact_created'));
      es.addEventListener('artifact_open', onEvt('artifact_open'));
      es.onopen = () => { attempt = 0; };
      es.onerror = () => {
        try { es.close(); } catch (_) { /* ignore */ }
        if (closed) return;
        const delay = Math.min(1000 * Math.pow(2, attempt++), 15000);
        reopenTimer = setTimeout(open, delay);
      };
    };
    open();
    return () => {
      closed = true;
      if (reopenTimer) clearTimeout(reopenTimer);
      if (es) { try { es.close(); } catch (_) { /* ignore */ } }
    };
  }, [sessionId]);
}

Object.assign(window, {
  ArtifactGallery, ArtifactCard, ArtifactViewerModal,
  artifactRawUrl, artifactViewUrl, renderArtifactBody, usePersistentEvents,
});
