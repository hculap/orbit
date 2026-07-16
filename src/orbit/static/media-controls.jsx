// media-controls.jsx — shared, presentational transport atoms.
//
// Reused by BOTH the persistent dock (media-dock.jsx) and the inline rich
// surfaces in the artifact viewer (orchestrator-artifacts.jsx). Each atom is a
// pure <button>/<input> driven by a `player` prop (the window.useMediaPlayer()
// context value) — it owns no media state of its own. High-frequency
// position/duration comes from window.usePlayerProgress() (external store) so
// only the scrubber re-renders per tick.
//
// Deps via window globals: Icon (components.jsx), usePlayerProgress
// (media-player-engine.jsx). Loads after the engine, before the dock + artifacts.

const { useState: _mcState, useRef: _mcRef } = React;

function formatMediaTime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const pad = (n) => (n < 10 ? '0' + n : '' + n);
  return h > 0 ? `${h}:${pad(m)}:${pad(r)}` : `${m}:${pad(r)}`;
}

// Bare round icon button shared by the transport cluster.
function _McIconBtn({ icon, label, onClick, active, disabled, size, iconSize, iconStyle }) {
  const [hover, setHover] = _mcState(false);
  const dim = size || 34;
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        width: dim, height: dim, flexShrink: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-pill)', border: 'none', cursor: disabled ? 'default' : 'pointer',
        background: hover && !disabled ? 'var(--surface-3)' : 'transparent',
        color: active ? 'var(--accent)' : (disabled ? 'var(--fg-4)' : 'var(--fg-2)'),
        opacity: disabled ? 0.5 : 1, padding: 0, transition: 'background .12s',
      }}
    >
      <Icon name={icon} size={iconSize || 18} color="currentColor" stroke={1.8} style={iconStyle} />
    </button>
  );
}

// Primary play/pause — accent-filled circle.
function PlayPauseButton({ player, size }) {
  const dim = size || 40;
  const playing = !!(player && player.isPlaying);
  return (
    <button
      type="button"
      aria-label={playing ? 'Pauza' : 'Odtwórz'}
      title={playing ? 'Pauza' : 'Odtwórz'}
      onClick={() => player && player.toggle()}
      style={{
        width: dim, height: dim, flexShrink: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer',
        background: 'var(--accent)', color: 'var(--accent-fg)', padding: 0,
        boxShadow: '0 1px 6px oklch(0.6 0.13 264 / 0.35)',
      }}
    >
      <Icon name={playing ? 'pause' : 'play'} size={Math.round(dim * 0.5)} color="currentColor" stroke={2} style={playing ? undefined : { marginLeft: 2 }} />
    </button>
  );
}

function PrevButton({ player, size }) {
  return <_McIconBtn icon="skip" label="Poprzedni" size={size} onClick={() => player && player.prev()} disabled={!player} iconStyle={{ transform: 'scaleX(-1)' }} />;
}

function NextButton({ player, size }) {
  const many = !!(player && player.queue && player.queue.items.length > 1);
  return <_McIconBtn icon="skip" label="Następny" size={size} onClick={() => player && player.next()} disabled={!many} />;
}

function PrevNextButtons({ player, size }) {
  return (
    <>
      <PrevButton player={player} size={size} />
      <NextButton player={player} size={size} />
    </>
  );
}

// Rewind / forward by player.skipSeconds — circular arrow with the seconds
// number centered inside (podcast-app convention). The step is configurable in
// Settings → Czat → Odtwarzacz mediów.
function _SkipButton({ player, dir }) {
  const [hover, setHover] = _mcState(false);
  const secs = (player && player.skipSeconds) || 5;
  const back = dir < 0;
  return (
    <button
      type="button"
      aria-label={(back ? 'Cofnij o ' : 'Przewiń o ') + secs + ' s'}
      title={(back ? '−' : '+') + secs + ' s'}
      onClick={() => player && (back ? player.skipBack() : player.skipForward())}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        position: 'relative', width: 34, height: 34, flexShrink: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer', padding: 0,
        background: hover ? 'var(--surface-3)' : 'transparent', color: 'var(--fg-2)', transition: 'background .12s',
      }}
    >
      <Icon name={back ? 'rotate-ccw' : 'rotate-cw'} size={22} color="currentColor" stroke={1.7} />
      <span className="mono" aria-hidden="true" style={{
        position: 'absolute', top: '52%', left: '50%', transform: 'translate(-50%,-50%)',
        fontSize: secs >= 100 ? 7 : 9, fontWeight: 700, lineHeight: 1, color: 'currentColor', pointerEvents: 'none',
      }}>{secs}</span>
    </button>
  );
}

function SkipBackButton({ player }) { return <_SkipButton player={player} dir={-1} />; }
function SkipForwardButton({ player }) { return <_SkipButton player={player} dir={1} />; }

function SpeedChip({ player }) {
  const [hover, setHover] = _mcState(false);
  const speed = player ? player.speed : 1;
  const label = (speed % 1 === 0 ? speed.toFixed(0) : String(speed)) + '×';
  return (
    <button
      type="button"
      aria-label={'Prędkość ' + label}
      title="Prędkość odtwarzania"
      onClick={() => player && player.cycleSpeed()}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className="mono"
      style={{
        minWidth: 42, height: 30, padding: '0 8px', flexShrink: 0,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        borderRadius: 'var(--r-pill)', cursor: 'pointer', fontSize: 'var(--t-cap)', fontWeight: 600, fontFamily: 'inherit',
        border: '1px solid var(--hairline)',
        background: hover ? 'var(--surface-3)' : 'var(--surface-2)',
        color: speed === 1 ? 'var(--fg-2)' : 'var(--accent)',
      }}
    >
      {label}
    </button>
  );
}

function AutoplayNextToggle({ player }) {
  const on = !!(player && player.autoplayNext);
  return (
    <_McIconBtn
      icon="refresh"
      label={on ? 'Auto-następny: wł.' : 'Auto-następny: wył.'}
      active={on}
      onClick={() => player && player.toggleAutoplayNext()}
    />
  );
}

function PipButton({ player }) {
  return <_McIconBtn icon="pip" label="Picture-in-Picture" active={!!(player && player.pip)} onClick={() => player && player.togglePip()} />;
}

function FullscreenButton({ player }) {
  return <_McIconBtn icon="maximize" label="Pełny ekran" onClick={() => player && player.enterFullscreen()} />;
}

// Range scrubber. Drag state suppresses store-driven thumb jumps; commit seek on
// release. accent-color paints the native range with the theme accent.
function ProgressScrubber({ player, compact }) {
  const prog = usePlayerProgress();
  const [drag, setDrag] = _mcState(null);
  const draggingRef = _mcRef(false);
  const dur = prog.duration || 0;
  const pos = drag != null ? drag : (prog.position || 0);
  const commit = () => {
    if (draggingRef.current && drag != null && player) player.seek(drag);
    draggingRef.current = false;
    setDrag(null);
  };
  return (
    <input
      type="range"
      min={0}
      max={dur > 0 ? dur : 0}
      step="0.1"
      value={dur > 0 ? Math.min(pos, dur) : 0}
      disabled={dur <= 0}
      aria-label="Pozycja odtwarzania"
      onChange={(e) => { draggingRef.current = true; setDrag(Number(e.target.value)); }}
      onPointerUp={commit}
      onPointerCancel={() => { draggingRef.current = false; setDrag(null); }}
      onKeyUp={commit}
      onBlur={commit}
      style={{
        width: '100%', height: compact ? 16 : 18, margin: 0, cursor: dur > 0 ? 'pointer' : 'default',
        accentColor: 'var(--accent)', background: 'transparent',
      }}
    />
  );
}

function TimeLabel({ separator }) {
  const prog = usePlayerProgress();
  return (
    <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
      {formatMediaTime(prog.position)}{separator || ' / '}{formatMediaTime(prog.duration)}
    </span>
  );
}

Object.assign(window, {
  HubMediaControls: {
    PlayPauseButton, PrevNextButtons, PrevButton, NextButton,
    SkipBackButton, SkipForwardButton, SpeedChip, AutoplayNextToggle,
    PipButton, FullscreenButton, ProgressScrubber, TimeLabel, formatMediaTime,
  },
  formatMediaTime,
});
