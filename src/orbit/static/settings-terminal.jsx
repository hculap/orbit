// Settings → Terminal tab.
//
// Server-scoped flags for the interactive ttyd terminal + the mobile
// soft-keyboard shortcut manager. All flags live in
// /api/orchestrator/settings; the full shortcut LAYOUT lives in
// /api/orchestrator/terminal-shortcuts (edited by SettingsShortcutsEditor,
// opened here in a drawer). Reads flags via window.HubSettings.useServerSettings
// (optimistic PATCH + revert-on-fail).
//
// Ported from the old flat settings-view.jsx — ServerTtydRow + the
// enable/summary parts of TerminalShortcutsCard — re-grouped onto the shared
// design-system primitives.

const { useState: _stUseState } = React;

const HS_T = window.HubSettings;
const {
  SettingGroup: _SettingGroup,
  SettingCard: _SettingCard,
  SettingRow: _SettingRow,
  ToggleRow: _ToggleRow,
  FieldRow: _FieldRow,
  NumberField: _NumberField,
  AdvancedDisclosure: _AdvancedDisclosure,
} = HS_T;

const _PORT_MIN = 1024;
const _PORT_MAX = 65535;

// Numeric field inside a card that dims when its parent feature is off.
function _DimNumberCard({ label, desc, value, min, max, suffix, disabled, onCommit }) {
  return (
    <_SettingCard style={{ opacity: disabled ? 0.45 : 1 }}>
      <_FieldRow label={label} desc={desc}>
        <_NumberField
          value={typeof value === 'number' ? value : min}
          min={min} max={max} suffix={suffix}
          disabled={disabled}
          onCommit={onCommit}
        />
      </_FieldRow>
    </_SettingCard>
  );
}

// "Edytuj układ" host — opens SettingsShortcutsEditor in a roomy Modal. The
// Modal's backdrop is full-viewport (window.Modal uses position:fixed), so the
// editor's own Button/View edit modals open over a full-page overlay and center
// correctly instead of being clipped inside this modal's box.
function _ShortcutsDrawer({ open, onClose }) {
  const Editor = window.SettingsShortcutsEditor;
  if (!open) return null;
  // height makes the modal BOX itself tall (the body is flex:1, so a tall child
  // alone wouldn't grow it — it would just scroll in a short box). The editor
  // then fills the height and scrolls within it.
  return (
    <window.Modal open={open} onClose={onClose} title="Edytor skrótów" width={760} height="86vh">
      <div style={{ padding: 18 }}>
        {Editor
          ? <Editor onDirtyChange={() => {}} />
          : <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Edytor skrótów niedostępny.</div>}
      </div>
    </window.Modal>
  );
}

// Summary card shown when the shortcut manager is enabled — a short stat + the
// button that opens the full layout editor in the drawer.
function _ShortcutsSummary() {
  const [open, setOpen] = _stUseState(false);
  return (
    <_SettingCard>
      <_SettingRow
        label="Skróty terminala"
        desc="Zarządzaj widokami i przyciskami paska mobilnej klawiatury. Zapis działa od razu, bez restartu sesji."
        control={
          window.Button
            ? <window.Button variant="primary" size="sm" icon="pencil" onClick={() => setOpen(true)}>Edytuj układ</window.Button>
            : <button onClick={() => setOpen(true)} style={{
                padding: '8px 14px', borderRadius: 'var(--r-control)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', cursor: 'pointer',
                background: 'var(--accent)', color: 'var(--accent-fg)', border: '1px solid var(--accent-line)',
              }}>Edytuj układ</button>
        }
      />
      <_ShortcutsDrawer open={open} onClose={() => setOpen(false)} />
    </_SettingCard>
  );
}

function SettingsTerminal({ server }) {
  // The single server-settings store is owned by the shell and passed in.
  const { cfg = {}, bounds = {}, loaded = false, busy = false, patch = () => {} } = server || {};
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;

  const ttydOn = cfg.ttyd_enabled === true;
  const shortcutsOn = cfg.terminal_shortcuts_enabled === true;
  const switcherOn = cfg.session_switcher_enabled === true;

  const [ttlMin, ttlMax] = bounds.ttyd_idle_ttl_s || [30, 86400];
  const [portMinLo, portMinHi] = bounds.ttyd_port_min || [_PORT_MIN, _PORT_MAX];
  const [portMaxLo, portMaxHi] = bounds.ttyd_port_max || [_PORT_MIN, _PORT_MAX];

  // The shortcut-manager flag PATCH must also re-sync a live toolbar: the
  // /settings PATCH doesn't touch the shortcut store, so we force a HubShortcuts
  // refresh after a successful flip so an open toolbar re-reads `enabled`.
  const toggleShortcuts = async (v) => {
    const ok = await patch({ terminal_shortcuts_enabled: v });
    if (ok && window.HubShortcuts && typeof window.HubShortcuts.refresh === 'function') {
      try { await window.HubShortcuts.refresh(); } catch (e) { console.error('shortcut refresh failed:', e); }
    }
  };

  // The /settings PATCH doesn't notify the (separately-mounted) switcher
  // overlay, so nudge it to re-read its flag after a successful flip — no reload.
  const toggleSwitcher = async (v) => {
    const ok = await patch({ session_switcher_enabled: v });
    if (ok) { try { window.dispatchEvent(new Event('hub:session-switcher-change')); } catch (e) {} }
  };

  // Port range is validated as a pair client-side: min<max — the backend would
  // accept either field on its own and could leave min>=max between two PATCHes.
  const commitPortMin = (n) => {
    const hi = typeof cfg.ttyd_port_max === 'number' ? cfg.ttyd_port_max : portMaxHi;
    if (n >= hi) { if (toast) toast('Port min musi być < port max', 'err'); return; }
    patch({ ttyd_port_min: n });
  };
  const commitPortMax = (n) => {
    const lo = typeof cfg.ttyd_port_min === 'number' ? cfg.ttyd_port_min : portMinLo;
    if (n <= lo) { if (toast) toast('Port max musi być > port min', 'err'); return; }
    patch({ ttyd_port_max: n });
  };

  const dim = !loaded || busy;

  return (
    <_SettingGroup
      scope="server"
      title="Terminal"
      desc="Interaktywny terminal + pasek skrótów mobilnej klawiatury. Układ skrótów zapisany na serwerze (synchronizuje urządzenia)."
      style={{ opacity: dim ? 0.7 : 1, pointerEvents: dim ? 'none' : 'auto' }}
    >
      <_ToggleRow
        label="Interaktywny terminal (ttyd)"
        desc="Włącza live xterm.js w podglądzie terminala — można pisać do claude'a (modale /login, theme picker, OAuth). Wymaga `ttyd` na serwerze. Wyłączone = stary read-only podgląd SSE."
        value={ttydOn}
        onChange={(v) => patch({ ttyd_enabled: v })}
      />

      <_DimNumberCard
        label="TTL bezczynności ttyd (s)"
        desc={'Po tylu sekundach bez aktywności sesja terminala wygasa. Zakres ' + ttlMin + '–' + ttlMax + '.'}
        value={cfg.ttyd_idle_ttl_s}
        min={ttlMin} max={ttlMax} suffix="s"
        disabled={!ttydOn}
        onCommit={(n) => patch({ ttyd_idle_ttl_s: n })}
      />

      <_ToggleRow
        label="Skróty terminala (mobile)"
        desc="Pasek soft-keyboard nad terminalem na telefonie — zarządzalne widoki i przyciski (klawisze, raw, /komendy, akcje). Synchronizuje się między urządzeniami. Wyłączone = statyczny domyślny pasek."
        value={shortcutsOn}
        onChange={toggleShortcuts}
      />

      {shortcutsOn && <_ShortcutsSummary />}

      <_ToggleRow
        label="Przełącznik sesji (⌥+⇥, desktop)"
        desc="Globalny modal na desktopie — lista aktywnych sesji. Przytrzymaj ⌥ (Option), stukaj ⇥ Tab aby przełączać (⇧⇥ wstecz, działają też strzałki ↑/↓), puść ⌥ by skoczyć; Esc/Enter/klik też działają. Wyłączone = tylko ⌘←/→ ślepe przełączanie."
        value={switcherOn}
        onChange={toggleSwitcher}
      />

      <_ToggleRow
        label="Czytanie odpowiedzi na głos (TTS) w terminalu"
        desc="Pokazuje przycisk 🔊 w pasku terminala (czyta ostatnią wiadomość) oraz — gdy na tym urządzeniu w zakładce Głos wybierzesz tryb inny niż „ręcznie” — automatycznie czyta każdą odpowiedź. Wymaga skonfigurowanego silnika TTS (zakładka Głos)."
        value={cfg.read_aloud_tmux_enabled === true}
        onChange={(v) => patch({ read_aloud_tmux_enabled: v })}
      />

      <_AdvancedDisclosure label="Zaawansowane (wymaga restartu / rollback)">
        <_ToggleRow
          label="Natychmiastowe podłączenie terminala"
          desc="Natychmiastowe podłączenie terminala — bez czekania na gotowość claude (domyślnie wł.; OFF = stary blokujący spawn)."
          value={cfg.terminal_instant_attach === true}
          onChange={(v) => patch({ terminal_instant_attach: v })}
        />
        <_DimNumberCard
          label="ttyd_port_min"
          desc={'⚠ wymaga restartu. Dolny port puli ttyd. Zakres ' + portMinLo + '–' + portMinHi + '.'}
          value={cfg.ttyd_port_min}
          min={Math.max(portMinLo, _PORT_MIN)} max={Math.min(portMinHi, _PORT_MAX)}
          disabled={false}
          onCommit={commitPortMin}
        />
        <_DimNumberCard
          label="ttyd_port_max"
          desc={'⚠ wymaga restartu. Górny port puli ttyd. Zakres ' + portMaxLo + '–' + portMaxHi + '.'}
          value={cfg.ttyd_port_max}
          min={Math.max(portMaxLo, _PORT_MIN)} max={Math.min(portMaxHi, _PORT_MAX)}
          disabled={false}
          onCommit={commitPortMax}
        />
      </_AdvancedDisclosure>
    </_SettingGroup>
  );
}

window.SettingsTerminal = SettingsTerminal;
