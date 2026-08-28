// orchestrator-session-overlays.jsx — the two panels the chat header opens
// as MODALS layered over the live session: the artifact gallery and the
// project/area Issues list.
//
// Why a modal and not a view/route swap (which is what both used to do): the
// gallery replaced <TerminalLiveView> in the chat body, and Issues navigated
// to /projects/<x>/issues — either way the live ttyd attach was torn down and
// the session vanished from the screen. As a modal the terminal stays mounted
// and visible behind the scrim, and the user closes with the X, the overlay,
// Escape or Back.
//
// Openness is NOT state here — it is driven by the route (/chat/<sid>/gallery
// | /issues), resolved in orchestrator.jsx. This component is dumb glue.
//
// Both bodies are self-fetching standalone panels with their own loading /
// error / empty states and their own nested overlays, so there is nothing to
// wire beyond ids. window.* is read at RENDER time only — this file loads
// after orchestrator.jsx but so does library-github-panels.jsx, and neither
// may be touched at module-eval time.

// Explicit box heights: Modal's body is `flex:1` (flex-basis 0), so a tall
// child inside an overflow:auto body does NOT grow the box — a caller that
// wants a tall modal must size it (precedent: settings-terminal.jsx). Without
// this both panels would size to content and jump when the fetch lands.
const _SOV_HEIGHT = '86vh';
// LibraryIssueList's 2-column grid keys on window.innerWidth < 880, NOT on its
// container, and it takes no `compact` prop — so on any desktop window we get
// two columns inside whatever width we give it and cannot force one column
// from outside. That breakpoint is the panel declaring "each column needs
// ~440px"; at the old flat 1100 it got 518px each, while the detail pane's
// own header measures 613px of min-content and the list table 689px. Hence a
// VIEWPORT-RELATIVE ceiling: on a 1800px screen this is 1698px → 818px
// columns, so the table fits outright (its `.scroll-hide` wrapper would
// otherwise scroll behind a permanently invisible scrollbar) and the header
// clears with room to spare. Modal drops a string straight into style.width
// (components.jsx) and still clamps at maxWidth:100%, so `96vw` keeps a scrim
// margin on every viewport and this stays a ceiling, not a floor.
// ⚠ This is ergonomics ONLY — the horizontal-scroll bug is fixed in
// _ghPanelContainer (minmax(0,1fr)); widening alone never reached zero
// overflow at any width measured. Gallery is left alone: its grid min is
// already definite and every tile Card carries minWidth:0.
const _SOV_WIDTH = { issues: 'min(1700px, 96vw)', gallery: 900 };

function SessionOverlayModal({ kind, open, onClose, sessionId, libKind, libId, title, compact, resolving }) {
  const Modal = window.Modal;
  // No Modal ⇒ components.jsx didn't load and the whole app is broken anyway;
  // rendering nothing is better than throwing inside OrchestratorView's tree.
  if (!Modal) return null;

  const Banner = window.StatusBanner;
  const Spinner = window.Spinner;
  const missing = (what) => (Banner
    ? <div style={{ padding: 18 }}><Banner variant="err" label={`Nie udało się załadować widoku (${what}).`} inline /></div>
    : <div style={{ padding: 18, fontSize: 'var(--t-sm)', color: 'var(--err)' }}>{`Nie udało się załadować widoku (${what}).`}</div>);

  let body = null;
  if (kind === 'gallery') {
    body = window.ArtifactGallery
      ? <window.ArtifactGallery sessionId={sessionId} scope="session" compact={compact} />
      : missing('ArtifactGallery');
  } else if (kind === 'issues') {
    // Cold deep link: the agent's kind/lib_id come from the ACTIVE SESSION's
    // cwd, which isn't known on the first paint. Show a spinner rather than
    // mounting the panel with an empty libId (that would fire a bogus fetch
    // and render a wrong/empty issue list).
    if (resolving || !libKind || !libId) {
      body = (
        <div style={{ padding: 24 }}>
          {Spinner ? <Spinner label="szukam agenta…" inline /> : null}
        </div>
      );
    } else {
      // kind/libId land exactly as library-detail.jsx passes them: the PLURAL
      // section ('projects'/'areas') and the BARE leaf. No adapter needed.
      // onSendToSession is what turns on the panel's "Rozwiąż" / "Dopracuj"
      // buttons — see LibraryIssueList. We are the only caller that has a
      // session on screen, and we already know WHICH one, so the target rides
      // along in the event detail instead of being re-derived from
      // state.activeId (which lags by a frame right after a session switch).
      // The handler in orchestrator.jsx closes this modal first, then pastes
      // into the live pane / chat composer — reusing the artifact "Skomentuj"
      // pathway wholesale, no second mechanism.
      body = window.LibraryIssueList
        ? (
          <window.LibraryIssueList
            kind={libKind} libId={libId} name={libId}
            onSendToSession={sessionId ? ((text) => {
              window.dispatchEvent(new CustomEvent('orchestrator:artifact-reference', {
                detail: { text, sessionId },
              }));
            }) : null}
          />
        )
        : missing('LibraryIssueList');
    }
  } else {
    return null;
  }

  return (
    <Modal
      open={!!open}
      onClose={onClose}
      title={title || (kind === 'gallery' ? 'Artefakty sesji' : 'Issues')}
      width={_SOV_WIDTH[kind] || 760}
      height={_SOV_HEIGHT}
      closeOnEscape
    >
      {/* No padding of our own — both panels apply their own (24px, 16px on
          compact). A wrapper here would double it. */}
      {body}
    </Modal>
  );
}

Object.assign(window, { SessionOverlayModal });
