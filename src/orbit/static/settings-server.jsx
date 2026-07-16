// SettingsServer — the "Serwer" tab body of the redesigned Settings.
//
// Server-scoped flags (~/.orchestrator/settings.json) that affect the WHOLE
// server / billing / processes, not just this device. All flag reads/writes go
// through the shared `window.HubSettings.useServerSettings()` store (single GET
// on mount + optimistic PATCH-with-revert), so this module never rolls its own
// fetch for the flags.
//
// (The Prompty editors moved to the Czat tab — see settings-chat.jsx.)
//
// The shell owns the per-device `useSettings` instance and the tab chrome; this
// component renders ONLY the tab body and uses SettingGroup for sub-grouping.

const { useState: _ssUseState, useEffect: _ssUseEffect, useCallback: _ssUseCallback } = React;

const _SS = window.HubSettings;
const {
  SettingCard: _ssSettingCard,
  SettingRow: _ssSettingRow,
  ToggleRow: _ssToggleRow,
  FieldRow: _ssFieldRow,
  Segmented: _ssSegmented,
  NumberField: _ssNumberField,
  SettingGroup: _ssSettingGroup,
  AdvancedDisclosure: _ssAdvancedDisclosure,
} = _SS;

// Shared runner-mode option labels — billing-relevant copy, reused by every
// runner-mode Segmented (chat / cron / titles / identity / skill).
const _SS_RUNNER_LABELS = { programmatic: 'Programmatic (-p)', interactive: 'Interactive (tmux)' };
const _SS_RUNNER_FALLBACK = ['programmatic', 'interactive'];

function _ssRunnerOptions(bounds, key) {
  const values = (bounds && Array.isArray(bounds[key]) && bounds[key].length) ? bounds[key] : _SS_RUNNER_FALLBACK;
  return values.map((v) => ({ value: v, label: _SS_RUNNER_LABELS[v] || v }));
}

// ─────────────────────────────────────────────────────────────
// Pool tuning card — pool_size + pool_idle_ttl_s, dimmed unless the chat
// runner is interactive (the pool only exists for the tmux warm-pool).
// ─────────────────────────────────────────────────────────────
function _ssPoolCard({ cfg, bounds, patch }) {
  const interactive = cfg.runner_mode === 'interactive';
  const [poolMin, poolMax] = bounds.pool_size || [1, 32];
  const [ttlMin, ttlMax] = bounds.pool_idle_ttl_s || [1, 86400];
  const poolSize = typeof cfg.pool_size === 'number' ? cfg.pool_size : poolMin;
  const ttl = typeof cfg.pool_idle_ttl_s === 'number' ? cfg.pool_idle_ttl_s : ttlMin;
  return (
    <_ssSettingCard>
      <_ssFieldRow
        label="Pula sesji (warm-pool tmux)"
        desc={interactive
          ? 'Rozgrzane sesje claude trzymane gotowe — szybszy pierwszy token.'
          : 'Aktywne tylko w trybie interactive — przełącz tryb runnera wyżej.'}>
        <_ssSettingRow
          label="Rozmiar puli"
          desc={'zakres ' + poolMin + '–' + poolMax}
          disabled={!interactive}
          control={
            <_ssNumberField
              value={poolSize} min={poolMin} max={poolMax} disabled={!interactive}
              onCommit={(n) => patch({ pool_size: n })}
            />
          }
        />
        <_ssSettingRow
          label="TTL bezczynności (s)"
          desc={'zakres ' + ttlMin + '–' + ttlMax}
          disabled={!interactive}
          control={
            <_ssNumberField
              value={ttl} min={ttlMin} max={ttlMax} disabled={!interactive} suffix="s" width={110}
              onCommit={(n) => patch({ pool_idle_ttl_s: n })}
            />
          }
        />
      </_ssFieldRow>
    </_ssSettingCard>
  );
}

// ─────────────────────────────────────────────────────────────
// Advanced — billing rollback levers (per-subsystem runner mode + prewarm).
// ─────────────────────────────────────────────────────────────
const _SS_ADVANCED_RUNNERS = [
  { key: 'titles_runner_mode', label: 'Runner auto-tytułów', desc: 'Runner auto-tytułów — dźwignia rollbacku rozliczeń' },
  { key: 'identity_runner_mode', label: 'Runner tożsamości agenta', desc: 'Runner tożsamości agenta — rollback rozliczeń' },
  { key: 'skill_runner_mode', label: 'Runner metadanych skilli', desc: 'Runner metadanych skilli — rollback rozliczeń' },
];

function _ssAdvancedSection({ cfg, bounds, patch, dim }) {
  return (
    <_ssAdvancedDisclosure label="Zaawansowane (dźwignie rollbacku rozliczeń)">
      {_SS_ADVANCED_RUNNERS.map(({ key, label, desc }) => (
        <_ssSettingCard key={key}>
          <_ssFieldRow label={label} desc={desc}>
            <_ssSegmented
              value={cfg[key] || _SS_RUNNER_FALLBACK[0]}
              options={_ssRunnerOptions(bounds, key)}
              onChange={(v) => patch({ [key]: v })}
              disabled={dim}
            />
          </_ssFieldRow>
        </_ssSettingCard>
      ))}
      <_ssToggleRow
        label="Prewarm puli przy starcie"
        desc="Rozgrzej pulę przy starcie — wczytuje ostatnie sesje na boot. ⚠ wymaga restartu."
        value={cfg.pool_prewarm_on_start === true}
        onChange={(v) => patch({ pool_prewarm_on_start: v })}
        disabled={dim}
      />
    </_ssAdvancedDisclosure>
  );
}

// ─────────────────────────────────────────────────────────────
// SettingsServer — the "Serwer" tab body.
// ─────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────
// Teleport card — exposes the install URL (paste to a LOCAL agent so it can
// self-install the teleport skill) + the artifact token (copied separately).
// ─────────────────────────────────────────────────────────────
function _ssCopyField({ label, value, mono = true }) {
  const [copied, setCopied] = _ssUseState(false);
  const onCopy = _ssUseCallback(() => {
    if (!value || !window.HubClipboard) return;
    window.HubClipboard.copyText(value).then((ok) => {
      if (ok) { setCopied(true); setTimeout(() => setCopied(false), 1500); }
    });
  }, [value]);
  return (
    <_ssSettingRow
      label={label}
      control={
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', minWidth: 0, flex: 1 }}>
          <input
            type="text"
            readOnly
            value={value || ''}
            onFocus={(e) => e.target.select()}
            style={{
              flex: 1, minWidth: 0, fontFamily: mono ? 'var(--font-mono, monospace)' : 'inherit',
              fontSize: 13, padding: '8px 10px', borderRadius: 8,
              border: '1px solid var(--border, #333)', background: 'var(--surface-2, #1a1a1a)',
              color: 'var(--text, #eee)',
            }}
          />
          <button
            type="button"
            onClick={onCopy}
            title="Kopiuj"
            style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '8px 12px',
              borderRadius: 8, border: '1px solid var(--border, #333)',
              background: copied ? 'var(--accent, #6b5bd6)' : 'var(--surface-2, #1a1a1a)',
              color: copied ? '#fff' : 'var(--text, #eee)', cursor: 'pointer', whiteSpace: 'nowrap',
            }}>
            <window.Icon name={copied ? 'check' : 'copy'} size={13} />
            {copied ? 'Skopiowano' : 'Kopiuj'}
          </button>
        </div>
      }
    />
  );
}

function _ssTeleportCard() {
  const [info, setInfo] = _ssUseState(null);
  _ssUseEffect(() => {
    let alive = true;
    fetch(window.apiUrl('/api/orchestrator/teleport/info'))
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d) setInfo(d); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  const origin = (window.location && window.location.origin) || '';
  const installUrl = (info && info.install_url) || (origin + window.apiUrl('/api/orchestrator/teleport/install'));
  const installPrompt = (info && info.install_prompt)
    || ('Przeczytaj i zainstaluj skill `hetzner-teleport` opisany pod ' + installUrl
        + ' — wykonaj kroki instalacji. Jeśli już zainstalowany, zaktualizuj go.');
  return (
    <_ssSettingCard>
      <_ssFieldRow
        label="Teleport sesji — skill dla lokalnego agenta"
        desc="Skopiuj poniższy prompt i wklej go lokalnemu agentowi (Claude Code na Twoim komputerze) — od razu zainstaluje (lub zaktualizuje) skill /hetzner-teleport. Token skopiuj osobno do config.json skilla.">
        <_ssCopyField label="Prompt instalacyjny" value={installPrompt} mono={false} />
        <_ssCopyField label="Artifact token" value={info ? info.token : ''} />
      </_ssFieldRow>
    </_ssSettingCard>
  );
}

function SettingsServer({ server }) {
  // The single server-settings store is owned by the shell and passed in.
  const { cfg = {}, bounds = {}, loaded = false, busy = false, patch = () => {} } = server || {};
  const dim = !loaded || busy;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <_ssSettingGroup
        scope="server"
        title="Serwer"
        desc="Ustawienia działają dla CAŁEGO serwera — wszystkich urządzeń. Wpływają na rozliczenia/procesy."
        style={{ opacity: dim ? 0.6 : 1, transition: 'opacity .15s' }}>

        <_ssToggleRow
          label="Auto-tytuły sesji"
          desc="Haiku generuje krótki tytuł po 1, 5, 25 i 100 wiadomościach użytkownika. Ręczne renamy i tak są respektowane."
          value={cfg.auto_titles !== false}
          onChange={(v) => patch({ auto_titles: v })}
          disabled={dim}
        />

        <_ssSettingCard>
          <_ssFieldRow
            label="Tryb runnera (czat)"
            desc="Interactive = długo-żyjąca sesja tmux pod subskrypcją Max. Programmatic = legacy `claude -p` per turę (rozliczane przez API).">
            <_ssSegmented
              value={cfg.runner_mode || _SS_RUNNER_FALLBACK[0]}
              options={_ssRunnerOptions(bounds, 'runner_mode')}
              onChange={(v) => patch({ runner_mode: v })}
              disabled={dim}
            />
          </_ssFieldRow>
        </_ssSettingCard>

        <_ssPoolCard cfg={cfg} bounds={bounds} patch={patch} />

        <_ssSettingCard>
          <_ssFieldRow
            label="Tryb runnera (cron)"
            desc="Niezależnie od czatu — jak crony odpalają claude'a.">
            <_ssSegmented
              value={cfg.cron_runner_mode || _SS_RUNNER_FALLBACK[0]}
              options={_ssRunnerOptions(bounds, 'cron_runner_mode')}
              onChange={(v) => patch({ cron_runner_mode: v })}
              disabled={dim}
            />
          </_ssFieldRow>
        </_ssSettingCard>

        <_ssTeleportCard />

        <_ssAdvancedSection cfg={cfg} bounds={bounds} patch={patch} dim={dim} />
      </_ssSettingGroup>
    </div>
  );
}

window.SettingsServer = SettingsServer;
