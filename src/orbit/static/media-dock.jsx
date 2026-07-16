// media-dock.jsx — the persistent full-width bottom mini-bar.
//
// A pure VIEW over window.useMediaPlayer() composing the media-controls atoms.
// Mounted once inside EACH shell (MobileHub above the bottom nav, DesktopHub at
// the bottom of <main>) but only one shell exists at a time, so there is never
// more than one dock. Survival of playback is owned by the engine's persistent
// elements (above the router) — a remounting dock view is harmless.
//
// Mobile coordination: the dock reads keyboardOpen from context (pushed up by
// MobileHub.reportKeyboard) and renders display:none when it's true — mirroring
// the bottom-nav gate so the dock and the tmux soft-keyboard toolbar are never
// on screen together. Audio keeps playing untouched in the engine garage.
//
// Deps via window globals: Icon (components.jsx), useMediaPlayer
// (media-player-engine.jsx), HubMediaControls (media-controls.jsx).

const { useState: _dkState } = React;

function _DockThumb({ track, isVideo }) {
  const common = {
    width: 40, height: 40, flexShrink: 0, borderRadius: 'var(--r-control)', overflow: 'hidden',
    background: 'var(--surface-3)', display: 'flex', alignItems: 'center', justifyContent: 'center',
  };
  if (isVideo && track.poster) {
    return (
      <div style={common}>
        <img src={track.poster} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} onError={(e) => { e.currentTarget.style.display = 'none'; }} />
      </div>
    );
  }
  return (
    <div style={common}>
      <Icon name={isVideo ? 'video' : 'music'} size={20} color="var(--fg-3)" />
    </div>
  );
}

function _DockIconBtn({ icon, label, onClick }) {
  const [hover, setHover] = _dkState(false);
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        width: 34, height: 34, flexShrink: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer', padding: 0,
        background: hover ? 'var(--surface-3)' : 'transparent', color: 'var(--fg-3)', transition: 'background .12s',
      }}
    >
      <Icon name={icon} size={18} color="currentColor" stroke={1.8} />
    </button>
  );
}

// `atScreenBottom` — true only for the DesktopHub mount where the dock is the
// last element at the viewport bottom and must clear the home-indicator inset.
// On mobile the dock sits ABOVE the bottom nav, which already adds
// env(safe-area-inset-bottom); adding it here too double-padded the dock in the
// installed iOS PWA (invisible on desktop, where the inset is 0).
function MediaDock({ atScreenBottom = false } = {}) {
  const player = (typeof useMediaPlayer === 'function') ? useMediaPlayer() : null;
  const [queueOpen, setQueueOpen] = _dkState(false);
  if (!player || !player.track) return null;
  const C = window.HubMediaControls || {};
  const t = player.track;
  const isVideo = t.type === 'video';
  const queueLen = (player.queue && Array.isArray(player.queue.items)) ? player.queue.items.length : 0;
  const scopeLabel = t.scope && t.scope.sessionId ? 'sesja' : ((t.scope && t.scope.libId) || 'global');

  return (
    <div
      style={{
        display: player.keyboardOpen ? 'none' : 'flex',
        flexDirection: 'column', gap: 6, flexShrink: 0,
        background: 'var(--surface-1)', borderTop: '1px solid var(--hairline)',
        padding: atScreenBottom ? '8px 10px calc(8px + env(safe-area-inset-bottom))' : '8px 10px',
      }}
    >
      {player.blocked && (
        <Button variant="primary" icon="play" onClick={() => player.dismissBlocked()}>
          Dotknij, aby wznowić
        </Button>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <_DockThumb track={t} isVideo={isVideo} />
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
            <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {t.title}
            </span>
            <span className="mono" style={{ flexShrink: 0, fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--fg-3)', padding: '1px 6px', borderRadius: 'var(--r-pill)', background: 'var(--surface-2)', border: '1px solid var(--hairline)' }}>
              {scopeLabel}
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {C.ProgressScrubber && <div style={{ flex: 1, minWidth: 0 }}><C.ProgressScrubber player={player} compact /></div>}
            {C.TimeLabel && <C.TimeLabel />}
          </div>
        </div>
        {C.PlayPauseButton && <C.PlayPauseButton player={player} size={42} />}
      </div>

      {/* 3-column grid: speed/repeat pinned left · transport truly centered · close right. */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr auto 1fr', alignItems: 'center', gap: 4 }}>
        <div className="scroll-hide" style={{ display: 'flex', alignItems: 'center', gap: 4, justifyContent: 'flex-start', minWidth: 0, overflowX: 'auto' }}>
          {C.SpeedChip && <C.SpeedChip player={player} />}
          {C.AutoplayNextToggle && <C.AutoplayNextToggle player={player} />}
          {queueLen > 1 && <_DockIconBtn icon="menu" label={'Otwórz kolejkę (' + queueLen + ')'} onClick={() => setQueueOpen(true)} />}
          {isVideo && C.PipButton && <C.PipButton player={player} />}
          {isVideo && C.FullscreenButton && <C.FullscreenButton player={player} />}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4, justifyContent: 'center' }}>
          {C.PrevButton && <C.PrevButton player={player} />}
          {C.SkipBackButton && <C.SkipBackButton player={player} />}
          {C.SkipForwardButton && <C.SkipForwardButton player={player} />}
          {C.NextButton && <C.NextButton player={player} />}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }}>
          <_DockIconBtn icon="close" label="Zamknij odtwarzacz" onClick={() => player.stop()} />
        </div>
      </div>

      {window.MediaQueueModal && (
        <window.MediaQueueModal open={queueOpen} onClose={() => setQueueOpen(false)} player={player} />
      )}
    </div>
  );
}

Object.assign(window, { MediaDock });
