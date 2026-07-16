// media-queue.jsx — playback-queue modal for the bottom media player.
//
// Lists window.HubPlayer's queue: the now-playing track is highlighted, items
// already played (index < current) are dimmed, and each row has reorder (▲▼),
// remove (✕), and click-to-jump. A pure VIEW over the player facade
// (window.HubPlayer.{reorder,removeAt,jumpTo}); queue state flows in via the
// `player` prop the dock passes from window.useMediaPlayer().
//
// Deps (window globals): Modal, Icon (components.jsx). Loaded before media-dock.jsx.

const { useState: _mqState } = React;

function _QBtn({ icon, label, disabled, onClick }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={!!disabled}
      onClick={onClick}
      style={{
        width: 30, height: 30, flexShrink: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        border: 'none', background: 'transparent', borderRadius: 'var(--r-pill)', padding: 0,
        cursor: disabled ? 'default' : 'pointer', color: disabled ? 'var(--fg-4)' : 'var(--fg-3)', opacity: disabled ? 0.4 : 1,
      }}
    >
      <Icon name={icon} size={16} color="currentColor" />
    </button>
  );
}

function _QueueRow({ item, i, index, total }) {
  const [hover, setHover] = _mqState(false);
  const played = i < index;
  const current = i === index;
  const P = window.HubPlayer || {};
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={() => { if (!current && P.jumpTo) P.jumpTo(i); }}
      style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
        borderRadius: 'var(--r-control)', cursor: current ? 'default' : 'pointer',
        background: current ? 'var(--surface-3)' : (hover ? 'var(--surface-2)' : 'transparent'),
        borderLeft: '3px solid ' + (current ? 'var(--accent)' : 'transparent'),
        opacity: played ? 0.45 : 1, transition: 'background .12s, opacity .12s',
      }}
    >
      <Icon
        name={current ? 'play' : (item.type === 'video' ? 'video' : 'music')}
        size={16}
        color={current ? 'var(--accent)' : 'var(--fg-3)'}
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 'var(--t-sm)', fontWeight: current ? 700 : 500, color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {item.title || item.id || '—'}
        </div>
        {current && <div style={{ fontSize: 9, textTransform: 'uppercase', letterSpacing: '.05em', color: 'var(--accent)' }}>teraz odtwarzane</div>}
        {played && <div style={{ fontSize: 9, textTransform: 'uppercase', letterSpacing: '.05em', color: 'var(--fg-3)' }}>odsłuchane</div>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
        <_QBtn icon="arrow-up" label="W górę" disabled={i === 0} onClick={() => P.reorder && P.reorder(i, i - 1)} />
        <_QBtn icon="arrow-down" label="W dół" disabled={i === total - 1} onClick={() => P.reorder && P.reorder(i, i + 1)} />
        <_QBtn icon="trash" label="Usuń z kolejki" onClick={() => P.removeAt && P.removeAt(i)} />
      </div>
    </div>
  );
}

function MediaQueueModal({ open, onClose, player }) {
  if (!open) return null;
  const q = (player && player.queue) || { items: [], index: 0 };
  const items = Array.isArray(q.items) ? q.items : [];
  const idx = Number.isInteger(q.index) ? q.index : 0;
  return (
    <Modal open={open} onClose={onClose} title={'Kolejka (' + items.length + ')'} width={520}>
      <div style={{ padding: '4px 12px 14px', display: 'flex', flexDirection: 'column', gap: 4, maxHeight: '60vh', overflowY: 'auto' }}>
        {items.length === 0
          ? <div style={{ padding: 20, textAlign: 'center', color: 'var(--fg-3)', fontSize: 'var(--t-sm)' }}>Kolejka pusta</div>
          : items.map((it, i) => (
            <_QueueRow key={(it && it.key) || i} item={it || {}} i={i} index={idx} total={items.length} />
          ))}
      </div>
    </Modal>
  );
}

Object.assign(window, { MediaQueueModal });
