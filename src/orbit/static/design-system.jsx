// design-system.jsx — the living styleguide for the HUB design system (v2).
//
// Mounted at /design (window.DesignSystemView, registered in app.jsx getSection
// + SECTIONS). This is the design-system MODEL made reviewable: it renders every
// token and every primitive with live examples, so it doubles as a runtime smoke
// test of the whole library. Written ON TOP of the primitives it documents
// (dogfooding). See docs/design-system/ for the written spec.

const { useState: _dsvUseState, useRef: _dsvUseRef, useEffect: _dsvUseEffect } = React;

// ── local layout helpers for the guide itself ──
function _Section({ id, title, desc, children }) {
  const SectionLabel = window.SectionLabel;
  return (
    <section id={id} style={{ marginTop: 36 }}>
      <div style={{ borderBottom: '1px solid var(--hairline)', paddingBottom: 10, marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 'var(--t-h2)', fontWeight: 500, letterSpacing: '-0.01em', color: 'var(--fg-1)' }}>{title}</h2>
        {desc && <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 4, lineHeight: 1.5 }}>{desc}</div>}
      </div>
      {children}
    </section>
  );
}

function _Demo({ label, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      {label && <div className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 8 }}>{label}</div>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>{children}</div>
    </div>
  );
}

function _Swatch({ token, sample }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, width: 132 }}>
      <div style={{ height: 52, borderRadius: 'var(--r-md)', border: '1px solid var(--hairline)', background: sample || `var(${token})` }} />
      <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{token}</code>
    </div>
  );
}

// ── token data ──
const _SURFACES = ['--bg', '--surface-1', '--surface-2', '--surface-3', '--hairline', '--hairline-strong'];
const _TEXT = ['--fg-1', '--fg', '--fg-2', '--fg-3', '--fg-4'];
const _ACCENT = ['--accent', '--accent-soft', '--accent-line', '--accent-fg'];
const _STATUS = ['--ok', '--ok-soft', '--ok-line', '--warn', '--warn-soft', '--warn-line', '--err', '--err-soft', '--err-line', '--err-bg', '--info', '--info-soft', '--info-line'];
const _TYPE = [
  ['--t-display', 'Display 40'], ['--t-h1', 'Heading 1 · 28'], ['--t-h2', 'Heading 2 · 20'],
  ['--t-h3', 'Heading 3 · 16'], ['--t-body', 'Body · 15'], ['--t-md', 'Medium · 14'],
  ['--t-sm', 'Small · 13'], ['--t-cap', 'Caption · 12'], ['--t-code', 'Code · 12.5'], ['--t-xs', 'XS · 11'], ['--t-2xs', '2XS · 10'],
];
const _RADII = ['--r-sm', '--r-control', '--r-md', '--r-widget', '--r-lg', '--r-sheet', '--r-xl', '--r-pill'];
const _SHADOWS = ['--shadow-sm', '--shadow-1', '--shadow-2', '--shadow-fab', '--shadow-pop', '--shadow-accent'];
const _SPACE = ['--s-1', '--s-2', '--s-3', '--s-4', '--s-5', '--s-6', '--s-7', '--s-8', '--s-9', '--s-10'];
const _SPACE_SEMANTIC = ['--field-gap', '--card-gap', '--inter-card-gap', '--page-pad-compact', '--page-pad'];
const _ICONS = ['menu','close','search','cmd','chevron-r','chevron-l','chevron-d','arrow-up','plus','send','sparkle','help','home','folder','box','globe','book','share','cpu','logs','bot','upload','download','trash','refresh','filter','check','dots','dots-v','image','file','video','pdf','archive','cog','circle','spinner','mic','headphones','attach','eye','pencil','panel-r','tasks','star','bell','volume','square','copy','pin','notepad','list-checks','clock','inbox','bulb','terminal','maximize','minimize','pip','play','pause','stop-circle','skip','heart','flag','bookmark','tag','link','lock','unlock','key','shield','wifi','cloud','database','server','code','git-branch','zap','flame','rocket','calendar','map-pin','message','at-sign','hash','sliders','grid','layers','save','external','sun','moon','coffee','music','compass','gift'];

function DesignSystemView({ compact: compactProp }) {
  const [compact, setCompact] = _dsvUseState(!!compactProp);
  _dsvUseEffect(() => {
    if (compactProp != null) { setCompact(!!compactProp); return; }
    const mq = window.matchMedia('(max-width: 960px)');
    const on = () => setCompact(mq.matches);
    on(); mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [compactProp]);

  // primitives off window
  const { Icon, Button, IconButton, Card, Chip, Modal, BottomSheet, KebabMenu,
    Input, Textarea, Select, Segmented, Tabs, Badge,
    Popover, StatusBanner, ConfirmModal, ModalFooter,
    Stack, Inline, Divider, SectionLabel, Spinner, EmptyState, Avatar, Field, ListRow,
    StatusDot, ProgressBar } = window;
  const HubSettings = window.HubSettings || {};

  // interactive demo state
  const [tab, setTab] = _dsvUseState('overview');
  const [pillTab, setPillTab] = _dsvUseState('a');
  const [seg, setSeg] = _dsvUseState('list');
  const [text, setText] = _dsvUseState('');
  const [area, setArea] = _dsvUseState('');
  const [sel, setSel] = _dsvUseState('opt2');
  const [toggle, setToggle] = _dsvUseState(true);
  const [num, setNum] = _dsvUseState(12);
  const [modalOpen, setModalOpen] = _dsvUseState(false);
  const [sheetOpen, setSheetOpen] = _dsvUseState(false);
  const [confirmOpen, setConfirmOpen] = _dsvUseState(false);
  const [popOpen, setPopOpen] = _dsvUseState(false);
  const popAnchor = _dsvUseRef(null);

  const Toggle = HubSettings.Toggle;
  const NumberField = HubSettings.NumberField;

  return (
    <PageScaffoldSafe compact={compact}>
      <div style={{ maxWidth: 920, margin: '0 auto' }}>
        {/* Header */}
        <div className="mono" style={{ display: 'inline-flex', alignItems: 'center', gap: 7, fontSize: 'var(--t-2xs)', color: 'var(--fg-3)', textTransform: 'uppercase', letterSpacing: '0.1em' }}><span className="orbit-core" /> Design System · v2</div>
        <h1 className="orbit-gradient-text is-animated" style={{ margin: '6px 0 0', fontSize: 'var(--t-h1)', fontWeight: 500, letterSpacing: '-0.02em' }}>HUB UI Library</h1>
        <p style={{ fontSize: 'var(--t-md)', color: 'var(--fg-2)', lineHeight: 1.6, marginTop: 8, maxWidth: 640 }}>
          The single source of truth for the visual language: tokens in <code className="mono" style={{ color: 'var(--accent)' }}>tokens.css</code>, primitives in <code className="mono" style={{ color: 'var(--accent)' }}>components.jsx</code> + <code className="mono" style={{ color: 'var(--accent)' }}>ds-*.jsx</code>. Everything below is live — it renders the real components. Written rationale lives in <code className="mono" style={{ color: 'var(--fg-3)' }}>docs/design-system/</code>.
        </p>

        {/* ── ORBIT · COSMIC ── */}
        <_Section title="Orbit · Cosmic" desc="The cosmic-orbit accent layer: blue→violet→magenta nebula gradients, plasma-core glows, gradient text + borders, the signature CTA, and orbital motion. Dark-first — gradients are accents, edges, and glows, never legibility-killing fills.">
          <_Demo label="Gradients">
            {['--grad-cosmic', '--grad-cosmic-soft', '--grad-cosmic-line', '--grad-core', '--grad-nebula-bg'].map(tok => (
              <div key={tok} style={{ display: 'flex', flexDirection: 'column', gap: 6, width: 132 }}>
                <div style={{ height: 52, borderRadius: 'var(--r-md)', border: '1px solid var(--hairline)', background: `var(${tok}), var(--surface-1)` }} />
                <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{tok}</code>
              </div>
            ))}
          </_Demo>

          <_Demo label="Glows (box-shadow)">
            {['--glow-soft', '--glow-accent', '--glow-strong', '--glow-core'].map(tok => (
              <div key={tok} style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'center' }}>
                <div style={{ width: 88, height: 56, borderRadius: 'var(--r-md)', background: 'var(--surface-2)', boxShadow: `var(${tok})` }} />
                <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{tok}</code>
              </div>
            ))}
          </_Demo>

          <_Demo label="Gradient text (.orbit-gradient-text)">
            <span className="orbit-gradient-text" style={{ fontSize: 'var(--t-h2)', fontWeight: 600, letterSpacing: '-0.01em' }}>Static gradient</span>
            <span className="orbit-gradient-text is-animated" style={{ fontSize: 'var(--t-h2)', fontWeight: 600, letterSpacing: '-0.01em' }}>Animated shimmer</span>
            <span className="orbit-gradient-text" style={{ fontSize: 'var(--t-display)', fontWeight: 600, lineHeight: 1 }}>128</span>
          </_Demo>

          <_Demo label="Plasma core & live dots (.orbit-core)">
            <Inline gap={12}>
              <span className="orbit-core" /> brand / live
              <span className="orbit-core" style={{ width: 14, height: 14 }} /> larger
              <StatusDot status="ok" /> keep semantic green for real status
            </Inline>
          </_Demo>

          <_Demo label="Signature CTA (.cosmic-btn) vs ghost">
            <button className="cosmic-btn" style={{ padding: '10px 18px', borderRadius: 'var(--r-control)', border: 'none', fontFamily: 'inherit', fontSize: 'var(--t-md)', fontWeight: 500, cursor: 'pointer' }}>Cosmic action</button>
            <Button variant="ghost" icon="rocket">Ghost</Button>
          </_Demo>

          <_Demo label="Gradient border (.orbit-gradient-border) & glows">
            <div className="orbit-gradient-border" style={{ '--gb-bg': 'var(--surface-1)', padding: '14px 16px', borderRadius: 'var(--r-md)', background: 'var(--surface-1)', color: 'var(--fg-2)', fontSize: 'var(--t-sm)', maxWidth: 200 }}>Hairline gradient edge — for hover / selected cards.</div>
            <div className="orbit-gradient-border is-animated" style={{ '--gb-bg': 'var(--surface-1)', padding: '14px 16px', borderRadius: 'var(--r-md)', background: 'var(--surface-1)', color: 'var(--fg-2)', fontSize: 'var(--t-sm)', maxWidth: 200 }}>Animated variant.</div>
            <div className="orbit-glow" style={{ width: 88, height: 56, borderRadius: 'var(--r-md)', background: 'var(--surface-2)' }} />
            <div className="orbit-glow-pulse" style={{ width: 88, height: 56, borderRadius: 'var(--r-md)', background: 'var(--surface-2)' }} />
          </_Demo>

          <_Demo label="Cosmic progress / orbital motion">
            <div style={{ width: 220, height: 8, borderRadius: 'var(--r-pill)', background: 'var(--surface-2)', overflow: 'hidden', border: '1px solid var(--hairline)' }}>
              <div style={{ width: '68%', height: '100%', background: 'var(--grad-cosmic)', boxShadow: 'var(--glow-soft)' }} />
            </div>
            <svg className="orbit-ring-spin" width={48} height={48} viewBox="0 0 48 48" aria-hidden="true">
              <circle cx={24} cy={24} r={18} fill="none" stroke="var(--accent-line)" strokeWidth={2} />
              <circle cx={24} cy={6} r={3.5} fill="var(--cosmic-magenta)" />
            </svg>
            <svg className="orbit-ring-spin-mid" width={48} height={48} viewBox="0 0 48 48" aria-hidden="true">
              <circle cx={24} cy={24} r={12} fill="none" stroke="var(--accent-line)" strokeWidth={2} />
              <circle cx={24} cy={12} r={3} fill="var(--cosmic-cyan)" />
            </svg>
          </_Demo>

          <_Demo label="Aurora strip (.orbit-aurora-bg) — featured / hero only">
            <div className="orbit-aurora-bg" style={{ width: '100%', minHeight: 64, borderRadius: 'var(--r-lg)', display: 'flex', alignItems: 'center', padding: '0 18px', boxShadow: 'var(--glow-soft)' }}>
              <span style={{ fontSize: 'var(--t-md)', color: 'var(--accent-fg)', fontWeight: 500, mixBlendMode: 'plus-lighter' }}>Hero / featured strip — accent surfaces only, never long prose.</span>
            </div>
          </_Demo>
        </_Section>

        {/* ── COLOR ── */}
        <_Section title="Color" desc="Editorial-quiet, dark-first, warm-white text ladder, oklch violet accent, oklch status family with soft/line tints.">
          <_Demo label="Surfaces">{_SURFACES.map(t => <_Swatch key={t} token={t} />)}</_Demo>
          <_Demo label="Text">{_TEXT.map(t => <_Swatch key={t} token={t} sample={`var(${t})`} />)}</_Demo>
          <_Demo label="Accent">{_ACCENT.map(t => <_Swatch key={t} token={t} />)}</_Demo>
          <_Demo label="Status (main / soft / line)">{_STATUS.map(t => <_Swatch key={t} token={t} />)}</_Demo>
        </_Section>

        {/* ── TYPE ── */}
        <_Section title="Typography" desc="Helvetica Neue (sans) + JetBrains Mono. The revived type scale — the steps the codebase actually uses (note the new --t-2xs/md/code).">
          <Stack gap={10}>
            {_TYPE.map(([tok, lbl]) => (
              <div key={tok} style={{ display: 'flex', alignItems: 'baseline', gap: 16 }}>
                <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', width: 120, flexShrink: 0 }}>{tok}</code>
                <span style={{ fontSize: `var(${tok})`, color: 'var(--fg)', lineHeight: 1.2 }}>{lbl}</span>
              </div>
            ))}
          </Stack>
        </_Section>

        {/* ── SPACING ── */}
        <_Section title="Spacing" desc="4-based raw scale (--s-N) + the semantic aliases authors reach for (these are what actually get adopted).">
          <_Demo label="Raw scale">
            <Stack gap={6}>
              {_SPACE.map(t => (
                <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', width: 56 }}>{t}</code>
                  <div style={{ height: 12, width: `var(${t})`, background: 'var(--accent)', borderRadius: 2 }} />
                </div>
              ))}
            </Stack>
          </_Demo>
          <_Demo label="Semantic">
            <Stack gap={6}>
              {_SPACE_SEMANTIC.map(t => (
                <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', width: 150 }}>{t}</code>
                  <div style={{ height: 12, width: `var(${t})`, background: 'var(--accent-line)', borderRadius: 2 }} />
                </div>
              ))}
            </Stack>
          </_Demo>
        </_Section>

        {/* ── RADII + SHADOW ── */}
        <_Section title="Radii & Elevation">
          <_Demo label="Radii">
            {_RADII.map(t => (
              <div key={t} style={{ display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'center' }}>
                <div style={{ width: 56, height: 56, borderRadius: `var(${t})`, background: 'var(--surface-3)', border: '1px solid var(--hairline-strong)' }} />
                <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{t}</code>
              </div>
            ))}
          </_Demo>
          <_Demo label="Shadows">
            {_SHADOWS.map(t => (
              <div key={t} style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'center' }}>
                <div style={{ width: 88, height: 56, borderRadius: 'var(--r-md)', background: 'var(--surface-2)', boxShadow: `var(${t})` }} />
                <code className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }}>{t}</code>
              </div>
            ))}
          </_Demo>
        </_Section>

        {/* ── BUTTONS ── */}
        <_Section title="Button" desc="variant: primary · ghost · quiet · danger — size: sm · md · lg — plus the v2 disabled / busy states.">
          <_Demo label="Variants (md)">
            <Button variant="primary" icon="check">Primary</Button>
            <Button variant="ghost" icon="refresh">Ghost</Button>
            <Button variant="quiet">Quiet</Button>
            <Button variant="danger" icon="trash">Danger</Button>
          </_Demo>
          <_Demo label="Sizes">
            <Button size="sm" variant="primary">Small</Button>
            <Button size="md" variant="primary">Medium</Button>
            <Button size="lg" variant="primary">Large</Button>
          </_Demo>
          <_Demo label="States (v2)">
            <Button variant="primary" disabled icon="check">Disabled</Button>
            <Button variant="primary" busy>Busy</Button>
            <Button variant="ghost" disabled>Disabled ghost</Button>
          </_Demo>
        </_Section>

        {/* ── ICON BUTTON ── */}
        <_Section title="IconButton" desc="Square icon button with tooltip; v2 adds active / disabled / compact (44px touch).">
          <_Demo>
            <IconButton icon="pencil" label="Edit" />
            <IconButton icon="star" label="Active" active />
            <IconButton icon="trash" label="Disabled" disabled />
            <IconButton icon="mic" label="Touch (compact)" compact />
            <KebabMenu items={[{ icon: 'pencil', label: 'Rename', onClick: () => {} }, { icon: 'trash', label: 'Delete', danger: true, onClick: () => {} }]} />
          </_Demo>
        </_Section>

        {/* ── FORM CONTROLS ── */}
        <_Section title="Form controls" desc="Input · Textarea · Select · Segmented · Field — the new global primitives replacing 84 <input>, 26 <textarea>, 22 <select>. Plus the settings atoms (Toggle / NumberField) now delegating here.">
          <Stack gap={14} style={{ maxWidth: 460 }}>
            <Field label="Input" hint="single-line · onChange(value) · icon + error variants">
              <Input value={text} onChange={setText} placeholder="Type something…" icon="search" />
            </Field>
            <Field label="Input — error">
              <Input value={text} onChange={setText} placeholder="invalid" error />
            </Field>
            <Field label="Textarea" hint="mono by default, --t-code">
              <Textarea value={area} onChange={setArea} rows={3} placeholder="multi-line…" />
            </Field>
            <Field label="Select">
              <Select value={sel} onChange={setSel} options={[{ value: 'opt1', label: 'Option one' }, { value: 'opt2', label: 'Option two' }, { value: 'opt3', label: 'Option three' }]} />
            </Field>
            <Field label="Segmented" layout="row">
              <Segmented value={seg} onChange={setSeg} options={[{ value: 'list', label: 'List' }, { value: 'grid', label: 'Grid' }, { value: 'kanban', label: 'Kanban' }]} />
            </Field>
            {Toggle && <Field label="Toggle (HubSettings → 44px tap)" layout="row"><Toggle checked={toggle} onChange={setToggle} /></Field>}
            {NumberField && <Field label="NumberField (HubSettings, v2 step)" layout="row"><NumberField value={num} min={0} max={100} onCommit={setNum} suffix="px" /></Field>}
          </Stack>
        </_Section>

        {/* ── TABS ── */}
        <_Section title="Tabs" desc="ONE primitive, two variants — replaces ~15 hand-rolled tab strips.">
          <_Demo label="underline">
            <div style={{ width: '100%' }}>
              <Tabs variant="underline" active={tab} onChange={setTab}
                tabs={[{ id: 'overview', label: 'Overview' }, { id: 'files', label: 'Files', count: 12 }, { id: 'agent', label: 'Agent', icon: 'bot' }, { id: 'settings', label: 'Settings', icon: 'cog' }]} />
            </div>
          </_Demo>
          <_Demo label="pills">
            <Tabs variant="pills" active={pillTab} onChange={setPillTab}
              tabs={[{ id: 'a', label: 'Day' }, { id: 'b', label: 'Week' }, { id: 'c', label: 'Month' }]} />
          </_Demo>
        </_Section>

        {/* ── CHIPS / BADGES / STATUS ── */}
        <_Section title="Chips, Badges & Status">
          <_Demo label="Chip variants">
            <Chip>default</Chip>
            <Chip variant="accent">accent</Chip>
            <Chip variant="ok" icon={<Icon name="check" size={12} />}>ok</Chip>
            <Chip variant="warn">warn</Chip>
            <Chip variant="danger">danger</Chip>
            <Chip variant="muted" size="xs">xs muted</Chip>
          </_Demo>
          <_Demo label="Badge">
            <Badge variant="count" value={3} />
            <Badge variant="count" value={128} max={99} />
            <Badge variant="dot" color="err" />
            <Badge variant="label" value="GLOBAL" color="muted" />
            <span style={{ position: 'relative', display: 'inline-flex' }}>
              <IconButton icon="bell" label="Notifications" />
              <Badge variant="count" value={5} color="err" absolute />
            </span>
          </_Demo>
          <_Demo label="StatusDot / live">
            <Inline gap={12}><StatusDot status="ok" /> ok <StatusDot status="warn" /> warn <StatusDot status="err" /> err <span className="live-dot" /> live</Inline>
          </_Demo>
          <_Demo label="StatusBanner / Alert (inline variants)">
            <Stack gap={8} style={{ width: '100%', maxWidth: 520 }}>
              <StatusBanner variant="ok" inline label="Connected to my-server" />
              <StatusBanner variant="warn" inline label="Token expires soon" sub="Refresh within 3 days to avoid interruption." />
              <StatusBanner variant="err" inline label="Failed to reach the API" action={<Button size="sm" variant="quiet">Retry</Button>} />
              <StatusBanner color="var(--info)" label="Legacy {color,label} API still works" />
            </Stack>
          </_Demo>
        </_Section>

        {/* ── CARDS / ROWS ── */}
        <_Section title="Cards & Rows" desc="Card (section / inset variants) · ListRow · ProgressBar.">
          <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : '1fr 1fr', gap: 'var(--inter-card-gap)' }}>
            <Card>
              <SectionLabel>Section card</SectionLabel>
              <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg)', marginTop: 6 }}>Default surface, --r-lg.</div>
              <div style={{ marginTop: 12 }}><ProgressBar value={62} label="Disk" sub="62%" /></div>
            </Card>
            <Card variant="inset" gap={10}>
              <SectionLabel>Inset card</SectionLabel>
              <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg)' }}>Tighter padding, --r-md (the old SettingCard, now delegating).</div>
            </Card>
          </div>
          <Card style={{ marginTop: 'var(--inter-card-gap)', padding: 0, overflow: 'hidden' }}>
            <ListRow leading={<Avatar icon="server" size="sm" />} title="my-server" sub="tailscale · 12d uptime" trailing={<StatusDot status="ok" />} onClick={() => {}} />
            <ListRow leading={<Avatar icon="box" size="sm" color="info" />} title="orbit" sub="~/Projects/orbit" trailing={<Chip size="xs" variant="accent">proj</Chip>} onClick={() => {}} divider />
            <ListRow leading={<Avatar icon="globe" size="sm" color="muted" />} title="example.com" sub="nginx · :443" trailing={<Icon name="chevron-r" size={16} color="var(--fg-3)" />} onClick={() => {}} divider />
          </Card>
        </_Section>

        {/* ── ATOMS ── */}
        <_Section title="Atoms" desc="EmptyState · Spinner · Avatar · Divider · SectionLabel.">
          <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : '1fr 1fr', gap: 'var(--inter-card-gap)' }}>
            <Card><EmptyState icon="inbox" message="Nothing here yet" sub="Items you create will show up here." action={<Button size="sm" variant="primary" icon="plus">New</Button>} /></Card>
            <Card>
              <Stack gap={16}>
                <Spinner label="Loading…" inline />
                <Inline gap={10}><Avatar emoji="🤖" size="sm" /><Avatar initials="SP" /><Avatar icon="bot" size="lg" color="info" statusDot /></Inline>
                <Divider />
                <SectionLabel>section label</SectionLabel>
              </Stack>
            </Card>
          </div>
        </_Section>

        {/* ── OVERLAYS ── */}
        <_Section title="Overlays" desc="Modal · ModalFooter · ConfirmModal · BottomSheet · Popover — one canonical implementation each.">
          <_Demo>
            <Button variant="ghost" icon="maximize" onClick={() => setModalOpen(true)}>Modal</Button>
            <Button variant="ghost" icon="panel-r" onClick={() => setSheetOpen(true)}>BottomSheet</Button>
            <Button variant="danger" icon="trash" onClick={() => setConfirmOpen(true)}>ConfirmModal</Button>
            <span ref={popAnchor} style={{ display: 'inline-flex' }}>
              <Button variant="ghost" icon="chevron-d" onClick={() => setPopOpen(o => !o)}>Popover</Button>
            </span>
          </_Demo>

          <Modal open={modalOpen} onClose={() => setModalOpen(false)} title="Example modal" width={460}>
            <div style={{ padding: '16px 18px', fontSize: 'var(--t-md)', color: 'var(--fg-2)', lineHeight: 1.6 }}>
              A centered dialog. The footer below is the shared <code className="mono">ModalFooter</code> helper.
            </div>
            <ModalFooter onCancel={() => setModalOpen(false)} onConfirm={() => setModalOpen(false)} confirmLabel="Save" cancelLabel="Cancel" />
          </Modal>

          <BottomSheet open={sheetOpen} onClose={() => setSheetOpen(false)} title="Example sheet">
            <Stack gap={10}>
              <div style={{ fontSize: 'var(--t-md)', color: 'var(--fg-2)' }}>Mobile slide-up sheet (drag handle, backdrop, scroll-lock).</div>
              <Button variant="primary" onClick={() => setSheetOpen(false)}>Done</Button>
            </Stack>
          </BottomSheet>

          <ConfirmModal open={confirmOpen} onClose={() => setConfirmOpen(false)} onConfirm={() => setConfirmOpen(false)}
            title="Delete session?" message="This frees the slot but keeps the transcript. This cannot be undone."
            confirmLabel="Delete" variant="danger" />

          <Popover open={popOpen} onClose={() => setPopOpen(false)} anchorRef={popAnchor} placement="bottom-start" minWidth={200}>
            <Stack gap={2}>
              {['Rename', 'Duplicate', 'Archive'].map(l => (
                <button key={l} onClick={() => setPopOpen(false)} style={{ textAlign: 'left', padding: '8px 10px', borderRadius: 'var(--r-sm)', border: 'none', background: 'transparent', color: 'var(--fg)', fontFamily: 'inherit', fontSize: 'var(--t-sm)', cursor: 'pointer' }}>{l}</button>
              ))}
            </Stack>
          </Popover>
        </_Section>

        {/* ── ICONS ── */}
        <_Section title="Icons" desc={`The ${_ICONS.length}-glyph stroke set (window.Icon). Click any to copy its name.`}>
          <div style={{ display: 'grid', gridTemplateColumns: `repeat(auto-fill, minmax(${compact ? 64 : 76}px, 1fr))`, gap: 8 }}>
            {_ICONS.map(name => (
              <button key={name} title={name}
                onClick={() => { window.HubClipboard.copyText(name); }}
                style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, padding: '10px 4px', background: 'var(--surface-1)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)', color: 'var(--fg-2)', cursor: 'pointer', fontFamily: 'inherit' }}>
                <Icon name={name} size={20} />
                <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>{name}</span>
              </button>
            ))}
          </div>
        </_Section>

        <div style={{ height: 60 }} />
      </div>
    </PageScaffoldSafe>
  );
}

// PageScaffold may not exist if ds-layout failed to load — degrade gracefully so
// the guide still renders (and surfaces the problem) rather than blanking.
function PageScaffoldSafe({ compact, children }) {
  const PS = window.PageScaffold;
  if (PS) return React.createElement(PS, { compact }, children);
  return <div className="scroll-hide" style={{ height: '100%', overflowY: 'auto', padding: compact ? 16 : 28 }}>{children}</div>;
}

window.DesignSystemView = DesignSystemView;
