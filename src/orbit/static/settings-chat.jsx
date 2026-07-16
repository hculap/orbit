// SettingsChat — the "Czat" tab body of the redesigned Settings.
//
// Two groups:
//   • Wyświetlanie czatu — device-scoped transcript prefs (showToolActions).
//   • Prompty — server-side shared prompt files (general + orchestrator),
//     GET/PATCH /api/settings/prompts, 32 KB cap each. Edited INLINE — the
//     project Files-tab pattern (Edytuj → expand a textarea in place →
//     Zapisz/Anuluj), NOT a modal. Moved here from the Serwer tab.
//
// The shell owns the per-device `useSettings` instance and passes
// {settings, updateSettings}; the prompts sub-tree talks to its own API.

const { useState: _scUseState, useEffect: _scUseEffect, useCallback: _scUseCallback } = React;

const _SC = window.HubSettings;
const _scByteLength = _SC._sByteLength;
const _SC_PROMPT_MAX_BYTES = 32 * 1024;

// Inline editor for one prompt file — mirrors the project Files tab's
// edit-in-place card: a header (label + sublabel + byte meter + Edytuj, or
// Anuluj/Zapisz while editing) over a textarea that only appears while
// editing. Owns its own draft so Anuluj restores the persisted text.
function _scPromptEntry({ field, label, sublabel, persisted, onSaved }) {
  const [editing, setEditing] = _scUseState(false);
  const [draft, setDraft] = _scUseState(persisted || '');
  const [busy, setBusy] = _scUseState(false);
  const [err, setErr] = _scUseState(null);
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;
  const Card = window.Card;
  const Button = window.Button;

  // Keep the draft aligned with persisted text while NOT editing (e.g. another
  // device saved). While editing we never clobber the user's in-progress draft.
  _scUseEffect(() => { if (!editing) setDraft(persisted || ''); }, [persisted, editing]);

  const savedBytes = _scByteLength(persisted || '');
  const draftBytes = _scByteLength(draft);
  const overLimit = draftBytes > _SC_PROMPT_MAX_BYTES;
  const dirty = draft !== (persisted || '');
  const canSave = dirty && !busy && !overLimit;
  const bytes = editing ? draftBytes : savedBytes;
  const meterColor = (editing && overLimit) ? 'var(--err)' : 'var(--fg-3)';

  const startEdit = () => { setDraft(persisted || ''); setErr(null); setEditing(true); };
  const cancel = () => { if (busy) return; setDraft(persisted || ''); setErr(null); setEditing(false); };

  const save = async () => {
    if (!canSave) return;
    setBusy(true); setErr(null);
    try {
      const r = await fetch(window.apiUrl('/api/settings/prompts'), {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ [field]: draft }),
      });
      if (!r.ok) {
        let msg = 'http ' + r.status;
        try {
          const d = await r.json();
          if (d && (d.error || d.detail)) msg = String(d.error || d.detail).slice(0, 240);
        } catch (_) { /* ignore non-JSON error body */ }
        throw new Error(msg);
      }
      const data = await r.json().catch(() => ({}));
      const next = typeof data[field] === 'string' ? data[field] : draft;
      if (typeof onSaved === 'function') onSaved(next);
      if (toast) toast(label + ' zapisany', 'ok');
      setEditing(false);
    } catch (e) {
      const msg = (e && e.message) || 'zapis nieudany';
      setErr(msg);
      if (toast) toast('Zapis nieudany: ' + msg, 'err');
    } finally { setBusy(false); }
  };

  return (
    <Card padding={0}>
      <div style={{
        display: 'flex', alignItems: 'flex-start', gap: 10,
        padding: '12px 14px', flexWrap: 'wrap',
        borderBottom: editing ? '1px solid var(--hairline)' : 'none',
      }}>
        <div style={{ flex: 1, minWidth: 150 }}>
          <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>{label}</div>
          <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2, lineHeight: 1.45 }}>{sublabel}</div>
        </div>
        <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: meterColor, whiteSpace: 'nowrap', alignSelf: 'center' }}
              title={bytes + ' z ' + _SC_PROMPT_MAX_BYTES + ' bajtów'}>
          {bytes} / {_SC_PROMPT_MAX_BYTES} B
        </span>
        {!editing
          ? <Button onClick={startEdit} variant="ghost" size="sm" icon="pencil">Edytuj</Button>
          : (
            <div style={{ display: 'flex', gap: 6, alignSelf: 'center' }}>
              <Button onClick={cancel} variant="quiet" size="sm">Anuluj</Button>
              <Button onClick={save} variant="primary" size="sm" icon={busy ? 'spinner' : 'check'}
                style={canSave ? undefined : { opacity: 0.6, pointerEvents: busy ? 'none' : 'auto' }}>
                {busy ? 'Zapisuję…' : 'Zapisz'}
              </Button>
            </div>
          )}
      </div>
      {editing && (
        <React.Fragment>
          <div className="mono" style={{
            padding: '8px 14px 0', fontSize: 'var(--t-2xs)', color: 'var(--fg-4)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            ~/.orchestrator/agent-prompts/{field}.md
          </div>
          <Textarea
            value={draft}
            onChange={setDraft}
            autoFocus
            rows={Math.min(20, Math.max(8, (draft.match(/\n/g) || []).length + 2))}
            placeholder="Treść promptu (markdown)."
            mono
            style={{ display: 'block', width: '100%', boxSizing: 'border-box', minHeight: 160 }}
          />
          {err && (
            <_SC.StatusBanner variant="err" label={err} inline />
          )}
        </React.Fragment>
      )}
    </Card>
  );
}

// Prompts group — GET both files on mount, render one inline editor per file.
function _scPromptsSection() {
  const [data, setData] = _scUseState(null); // { general, orchestrator } | null while loading
  const [error, setError] = _scUseState(null);

  _scUseEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(window.apiUrl('/api/settings/prompts'));
        if (!r.ok) throw new Error('http ' + r.status);
        const d = await r.json();
        if (cancelled) return;
        setData({
          general: typeof d.general === 'string' ? d.general : (d.general && d.general.content) || '',
          orchestrator: typeof d.orchestrator === 'string' ? d.orchestrator : (d.orchestrator && d.orchestrator.content) || '',
        });
      } catch (e) {
        if (cancelled) return;
        console.error('prompts load failed:', e);
        setError((e && e.message) || 'nie udało się załadować');
        setData({ general: '', orchestrator: '' });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const onGeneralSaved = _scUseCallback((next) => setData((p) => (p ? { ...p, general: next } : p)), []);
  const onOrchestratorSaved = _scUseCallback((next) => setData((p) => (p ? { ...p, orchestrator: next } : p)), []);

  return (
    <_SC.SettingGroup
      scope="server"
      title="Prompty"
      desc="Współdzielone pliki promptów używane przez każdą sesję claude-cli. Limit 32 KB na plik.">
      {data === null && (
        <Spinner label="Ładuję prompty…" inline size={14} />
      )}
      {data !== null && error && (
        <_SC.StatusBanner variant="err" label={error} inline />
      )}
      {data !== null && (
        <React.Fragment>
          <_scPromptEntry
            field="general"
            label="Ogólny"
            sublabel="Reguły formatu / komunikacji — doklejane do każdej sesji."
            persisted={data.general}
            onSaved={onGeneralSaved}
          />
          <_scPromptEntry
            field="orchestrator"
            label="Orchestrator (tylko agent globalny)"
            sublabel="Kontekst serwera + ścieżki robocze — doklejane tylko gdy cwd to null / $HOME."
            persisted={data.orchestrator}
            onSaved={onOrchestratorSaved}
          />
        </React.Fragment>
      )}
    </_SC.SettingGroup>
  );
}

function SettingsChat({ settings, updateSettings }) {
  const HS = window.HubSettings;
  // Media player skip step lives in the engine's own prefs (hub.media.prefs,
  // device-scoped) alongside speed/autoplay; read it from the player context and
  // write via the imperative facade. Falls back gracefully if the engine is absent.
  const player = window.useMediaPlayer ? window.useMediaPlayer() : null;
  const skip = player && player.skipSeconds != null ? player.skipSeconds : 5;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <HS.SettingGroup title="Wyświetlanie czatu" scope="device">
        <HS.ToggleRow
          label="Pokazuj akcje modelu"
          desc="Bash / Read / thinking — bloki z narzędzi i wewnętrznego rozumowania. Wyłącz, aby widzieć tylko same odpowiedzi."
          value={settings.showToolActions !== false}
          onChange={(v) => updateSettings({ showToolActions: v })}
        />
      </HS.SettingGroup>
      <HS.SettingGroup title="Odtwarzacz mediów" scope="device">
        <HS.SettingRow
          label="Przeskok w tył / w przód"
          desc="O ile sekund cofają i przewijają przyciski ↺ / ↻ w odtwarzaczu audio i wideo."
          control={(
            <HS.NumberField
              value={skip} min={1} max={120} suffix="s" disabled={!player}
              onCommit={(n) => { if (window.HubPlayer) window.HubPlayer.setSkipSeconds(n); }}
            />
          )}
        />
      </HS.SettingGroup>
      <_scPromptsSection />
    </div>
  );
}

window.SettingsChat = SettingsChat;
