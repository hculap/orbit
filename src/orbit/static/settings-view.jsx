// Settings shell — tabbed container for the redesigned Settings page.
//
// The flat 1885-line settings-view was split into:
//   settings-primitives.jsx       — useSettings/useServerSettings + UI atoms (the contract)
//   settings-notifications.jsx    — Powiadomienia tab  (window.SettingsNotifications)
//   settings-voice.jsx            — Głos tab           (window.SettingsVoice)
//   settings-chat.jsx             — Czat tab           (window.SettingsChat — display prefs + Prompty)
//   settings-server.jsx           — Serwer tab         (window.SettingsServer)
//   settings-shortcuts-editor.jsx — shortcuts drawer   (window.SettingsShortcutsEditor)
//   settings-terminal.jsx         — Terminal tab       (window.SettingsTerminal)
//   settings-view.jsx (this file) — shell + tabs
//
// Per-device settings (localStorage) are owned HERE via a single useSettings()
// instance and passed to the device-scoped tabs as props; server flags are
// fetched per-tab via useServerSettings(). Tabs are URL-synced
// (/settings/<tab>) so they're deep-linkable + restored on PWA reopen, without
// touching the core router (parseRoute treats the extra segment as harmless).

const { useState: _svUseState, useCallback: _svUseCallback } = React;

const _SETTINGS_TABS = [
  { id: 'powiadomienia', label: 'Powiadomienia', icon: 'bell' },
  { id: 'glos',          label: 'Głos',          icon: 'mic' },
  { id: 'czat',          label: 'Czat',          icon: 'bot' },
  { id: 'serwer',        label: 'Serwer',        icon: 'cpu' },
  { id: 'terminal',      label: 'Terminal',      icon: 'terminal' },
];
const _SETTINGS_TAB_IDS = new Set(_SETTINGS_TABS.map((t) => t.id));
const _LAST_TAB_KEY = 'hub:settings-tab';

// Third path segment of /settings/<tab>, validated. parseRoute keeps
// section='settings' for the whole path, so reading it here is safe.
function _tabFromPath() {
  try {
    const parts = (window.location.pathname || '').split('/').filter(Boolean);
    const i = parts.indexOf('settings');
    const seg = i >= 0 ? parts[i + 1] : null;
    return seg && _SETTINGS_TAB_IDS.has(seg) ? seg : null;
  } catch (_) { return null; }
}
function _readLastTab() {
  try {
    const v = localStorage.getItem(_LAST_TAB_KEY);
    return v && _SETTINGS_TAB_IDS.has(v) ? v : null;
  } catch (_) { return null; }
}

// Canonical app tab strip (mirrors global-detail.jsx): horizontally scrollable,
// 2px accent underline on the active tab. Sticky so it stays visible while the
// tab body scrolls on mobile.
function _SettingsTabs({ active, onPick }) {
  return (
    <div className="scroll-hide" style={{
      position: 'sticky', top: 0, zIndex: 5, background: 'var(--bg)',
      display: 'flex', gap: 4, borderBottom: '1px solid var(--hairline)',
      overflowX: 'auto', marginBottom: 16,
    }}>
      {_SETTINGS_TABS.map((t) => {
        const on = t.id === active;
        return (
          <button key={t.id} onClick={() => onPick(t.id)} style={{
            display: 'inline-flex', alignItems: 'center', gap: 7,
            padding: '11px 14px', minHeight: 44, background: 'transparent',
            border: 'none', cursor: 'pointer', flexShrink: 0,
            color: on ? 'var(--fg)' : 'var(--fg-3)',
            fontSize: 'var(--t-sm)', fontWeight: 500, fontFamily: 'inherit',
            borderBottom: '2px solid ' + (on ? 'var(--accent)' : 'transparent'),
            marginBottom: -1,
          }}>
            {window.Icon ? <window.Icon name={t.icon} size={15} color={on ? 'var(--accent)' : 'var(--fg-3)'} /> : null}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

// Czat tab — small enough to live inline. Transcript-display preferences.
function _MissingTab({ name }) {
  return (
    <div className="mono" style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-4)', padding: '24px 4px' }}>
      ({name} niedostępne — moduł się nie załadował)
    </div>
  );
}

function SettingsView({ compact }) {
  const [settings, updateSettings] = window.useSettings();
  // One server-settings store for the whole page (passed to the Serwer + Terminal
  // tabs) — one GET per Settings-open, no per-tab refetch, no shared module cache.
  const server = window.HubSettings.useServerSettings();
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;

  const [tab, setTab] = _svUseState(() => _tabFromPath() || _readLastTab() || 'powiadomienia');

  const pick = _svUseCallback((id) => {
    setTab(id);
    try { localStorage.setItem(_LAST_TAB_KEY, id); } catch (_) { /* quota / private mode */ }
    // Sync URL so the tab is deep-linkable + restored on reopen. replace (not
    // push) keeps the back button clean — one Back exits Settings, not cycles
    // tabs. No-op gracefully if the router isn't available.
    if (router && typeof router.replace === 'function') {
      try { router.replace('/settings/' + id); } catch (_) { /* ignore */ }
    }
  }, [router]);

  const body = (() => {
    switch (tab) {
      case 'powiadomienia':
        return window.SettingsNotifications
          ? <window.SettingsNotifications settings={settings} updateSettings={updateSettings} />
          : <_MissingTab name="Powiadomienia" />;
      case 'glos':
        return window.SettingsVoice
          ? <window.SettingsVoice settings={settings} updateSettings={updateSettings} />
          : <_MissingTab name="Głos" />;
      case 'czat':
        return window.SettingsChat
          ? <window.SettingsChat settings={settings} updateSettings={updateSettings} />
          : <_MissingTab name="Czat" />;
      case 'serwer':
        return window.SettingsServer ? <window.SettingsServer server={server} /> : <_MissingTab name="Serwer" />;
      case 'terminal':
        return window.SettingsTerminal ? <window.SettingsTerminal server={server} /> : <_MissingTab name="Terminal" />;
      default:
        return null;
    }
  })();

  return (
    // Left-aligned, full-width page — matches Credentials / Project detail (no
    // centered max-width column). `.settings-root` stays so tokens.css can bump
    // form controls to 16px on ≤960px (kills iOS auto-zoom).
    <div className="settings-root" style={{ padding: compact ? 16 : 28, paddingBottom: compact ? 100 : 28 }}>
      <window.SectionHeader eyebrow="Konfiguracja" title="Ustawienia" />
      <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 8, marginBottom: 18, lineHeight: 1.5 }}>
        Część dotyczy tylko tego urządzenia, część całego serwera — plakietka przy każdej grupie pokazuje zasięg.
      </div>
      <_SettingsTabs active={tab} onPick={pick} />
      <div key={tab} className="fade-in" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {body}
      </div>
    </div>
  );
}

Object.assign(window, { SettingsView, useSettings: window.useSettings });
