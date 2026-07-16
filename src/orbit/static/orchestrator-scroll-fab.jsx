// Floating "scroll to bottom" button.
// Appears in the lower-right of the chat column when the user scrolls more
// than ~240px above the bottom of the transcript. Click smooth-scrolls to
// the latest message. Hides itself when the user is already near the bottom.

const {
  useEffect: _sfUseEffect,
  useState: _sfUseState,
  useRef: _sfUseRef,
} = React;

// Distance (in px) from the bottom edge before we consider the user "scrolled
// up" enough to need the button. Lower → button appears for tiny scrolls
// (annoying); higher → button only shows when truly far. ~240 hits the sweet
// spot for both compact (mobile) and wide (desktop) layouts.
const _AT_BOTTOM_THRESHOLD = 240;

function ScrollToBottomFab({ scrollRef, compact }) {
  const [visible, setVisible] = _sfUseState(false);
  const rafRef = _sfUseRef(0);

  _sfUseEffect(() => {
    const sc = scrollRef && scrollRef.current;
    if (!sc) return undefined;
    const measure = () => {
      const dist = sc.scrollHeight - sc.scrollTop - sc.clientHeight;
      setVisible(dist > _AT_BOTTOM_THRESHOLD);
    };
    // rAF-throttle the listener — scroll fires per-frame and we don't need
    // every frame to re-evaluate visibility.
    const onScroll = () => {
      if (rafRef.current) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = 0;
        measure();
      });
    };
    measure();
    sc.addEventListener('scroll', onScroll, { passive: true });
    let ro = null;
    if (typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(measure);
      ro.observe(sc);
    }
    return () => {
      sc.removeEventListener('scroll', onScroll);
      if (ro) ro.disconnect();
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    };
  }, [scrollRef]);

  const onClick = () => {
    const sc = scrollRef && scrollRef.current;
    if (!sc) return;
    try {
      sc.scrollTo({ top: sc.scrollHeight, behavior: 'smooth' });
    } catch (e) {
      sc.scrollTop = sc.scrollHeight;
    }
  };

  // Bottom offset clears MessageInput + (on mobile) the iOS home-bar safe area.
  // Numbers picked empirically: input row is ~84-100px depending on textarea
  // size; the FAB lives just above that.
  const bottom = compact
    ? 'calc(env(safe-area-inset-bottom, 0px) + 96px)'
    : '104px';

  return (
    <button
      onClick={onClick}
      aria-label="Skocz do najnowszej wiadomości"
      title="Skocz do najnowszej wiadomości"
      style={{
        position: 'absolute',
        bottom,
        right: compact ? 16 : 24,
        width: 40, height: 40, borderRadius: 'var(--r-xl)',
        background: 'var(--surface-1)',
        color: 'var(--accent)',
        border: '1px solid var(--accent-line)',
        boxShadow: '0 6px 18px rgba(0,0,0,0.32)',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        cursor: 'pointer', padding: 0, zIndex: 20,
        opacity: visible ? 1 : 0,
        transform: visible ? 'translateY(0)' : 'translateY(8px)',
        pointerEvents: visible ? 'auto' : 'none',
        transition: 'opacity .18s ease, transform .18s ease',
      }}>
      <Icon name="chevron-d" size={20} stroke={2} />
    </button>
  );
}

Object.assign(window, { ScrollToBottomFab });
