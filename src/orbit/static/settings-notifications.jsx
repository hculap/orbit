// Settings → Powiadomienia tab body.
//
// Three SettingGroups:
//   1) "Na tym urządzeniu" (device)  — Web Push (useNotifications) + in-app ding
//   2) "Telegram (bot)" (secrets)    — probe/refresh, setup, test/detect,
//                                       + inline TELEGRAM_BOT_TOKEN / _CHAT_ID
//                                       writers (secrets PUT, captcha-on-overwrite),
//                                       and a detect-chat-id write-through.
//   3) "Wyciszenia tematów" (server) — per-topic mutes via the NO-CAPTCHA
//                                       /api/notify/mutes endpoint.
//
// Renders ONLY the tab body — the shell owns tab chrome. Per-device values come
// in as props { settings, updateSettings }; server flags would come from
// HubSettings.useServerSettings() (not needed here). Published to
// window.SettingsNotifications.

const {
  SettingCard: _nSettingCard,
  SettingRow: _nSettingRow,
  ToggleRow: _nToggleRow,
  Toggle: _nToggle,
  SettingGroup: _nSettingGroup,
  AdvancedDisclosure: _nAdvancedDisclosure,
  StatusBanner: _nStatusBanner,
  _useToast: _nUseToast,
} = window.HubSettings;

const { useState: _nUseState, useEffect: _nUseEffect, useCallback: _nUseCallback } = React;

const _N_Icon = window.Icon;

// window.Button does NOT support a `disabled` prop (it neither forwards it to
// the underlying <button> nor guards onClick), and these flows depend on
// disabled state (probe-gated Test push, busy guards). So we render native
// buttons with design-system styling instead, mirroring the original source.
function _ActionButton({ children, onClick, disabled, variant = 'ghost', icon }) {
  const palette = {
    primary: { bg: 'var(--accent)', fg: 'var(--accent-fg)', bd: 'transparent' },
    ghost: { bg: 'var(--surface-1)', fg: 'var(--fg)', bd: 'var(--hairline)' },
    quiet: { bg: 'var(--surface-3)', fg: 'var(--fg-2)', bd: 'var(--hairline)' },
  };
  const v = palette[variant] || palette.ghost;
  return (
    <button
      type="button"
      onClick={() => { if (!disabled && onClick) onClick(); }}
      disabled={!!disabled}
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
        minHeight: 32, padding: '6px 12px', borderRadius: 'var(--r-control)', fontSize: 'var(--t-sm)', fontWeight: 500,
        fontFamily: 'inherit', whiteSpace: 'nowrap',
        background: v.bg, color: v.fg, border: '1px solid ' + v.bd,
        cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.55 : 1,
        transition: 'opacity .12s',
      }}>
      {icon && _N_Icon ? <_N_Icon name={icon} size={14} stroke={1.8} /> : null}
      {children}
    </button>
  );
}

// ── topic metadata (server-side mutes) ──────────────────────────────────────
const _NOTIFY_TOPICS = [
  { key: 'cron',   label: 'Cron jobs',   desc: 'Awarie zaplanowanych zadań.' },
  { key: 'agent',  label: 'Agent skill', desc: 'Powiadomienia z sesji agenta.' },
  { key: 'chat',   label: 'Chat',        desc: 'Zakończenie tury z narzędziami gdy nie patrzysz.' },
  { key: 'tasks',  label: 'Zadania',     desc: 'Przypomnienia o zadaniach (tasks reminders).' },
  { key: 'system', label: 'System',      desc: 'Watchdog: dysk, usługi, TLS.' },
];

const _TELEGRAM_TOKEN_KEY = 'TELEGRAM_BOT_TOKEN';
const _TELEGRAM_CHATID_KEY = 'TELEGRAM_CHAT_ID';

// ── secrets helpers (captcha-on-overwrite write-through) ─────────────────────

async function _notifyIssueCaptcha() {
  const r = await fetch(window.apiUrl('/api/secrets/captcha/issue'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  if (!r.ok) throw new Error(`captcha http ${r.status}`);
  return r.json();
}

// List which global env keys currently exist (so we know whether a PUT is an
// overwrite, which the backend gates behind a captcha).
async function _notifyListEnvKeys() {
  const r = await fetch(window.apiUrl('/api/secrets/global/env'));
  if (!r.ok) throw new Error(`http ${r.status}`);
  const body = await r.json();
  const items = Array.isArray(body && body.items) ? body.items : [];
  return new Set(items.map((it) => it && it.key).filter(Boolean));
}

// Write a single global env secret. When `isOverwrite` is true the backend
// requires a captcha block, so we issue one first and attach it.
async function _notifyWriteSecret(key, value, isOverwrite) {
  const body = { value };
  if (isOverwrite) {
    const c = await _notifyIssueCaptcha();
    body.captcha = { token: c.token, code: c.code };
  }
  const r = await fetch(window.apiUrl('/api/secrets/global/env/' + key), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || err.message || `http ${r.status}`);
  }
  return r.json().catch(() => ({}));
}

// ── Web Push row (per-device) ───────────────────────────────────────────────
// Wraps useNotifications. The toggle is async + permission-gated, so its
// visible state can briefly disagree with the click; a status-aware Polish
// description sits under the label.
function _WebPushRow() {
  const useNotifsHook = (typeof window !== 'undefined') ? window.useNotifications : null;
  const toast = _nUseToast();
  const fallback = { status: 'unsupported', toggle: () => {} };
  const { status, toggle } = useNotifsHook ? useNotifsHook(toast) : fallback;
  const subscribed = status === 'subscribed';
  const busy = status === 'subscribing';
  const blocked = status === 'unsupported' || status === 'denied';
  const desc = (() => {
    if (status === 'unsupported') return 'Ta przeglądarka nie wspiera Web Push.';
    if (status === 'denied') return 'Zablokowane w ustawieniach przeglądarki — odblokuj tam ręcznie.';
    if (subscribed) return 'Włączone — dostaniesz powiadomienie gdy agent skończy turę.';
    if (busy) return 'Przełączanie…';
    return 'Wyłączone — kliknij aby włączyć (przeglądarka poprosi o uprawnienia).';
  })();
  return (
    <_nSettingCard style={{ opacity: blocked ? 0.6 : 1 }}>
      <_nSettingRow
        label="Powiadomienia push"
        desc={desc}
        control={
          <_nToggle
            checked={subscribed}
            disabled={busy || blocked}
            onChange={() => { if (!busy && !blocked) toggle(); }}
          />
        }
      />
    </_nSettingCard>
  );
}

// ── inline Telegram secret editor (token / chat id) ──────────────────────────
function _SecretInputRow({ label, hint, placeholder, value, onChange, onSave, busy, disabled }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div>
        <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>{label}</div>
        {hint && <div className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2, lineHeight: 1.45 }}>{hint}</div>}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <Input
          value={value}
          placeholder={placeholder}
          disabled={disabled || busy}
          onChange={onChange}
          onKeyDown={(e) => { if (e.key === 'Enter' && value.trim()) onSave(); }}
          autoComplete="off"
          mono
          style={{ flex: '1 1 200px', minWidth: 0 }}
        />
        <_ActionButton
          variant="primary"
          disabled={disabled || busy || !value.trim()}
          onClick={onSave}
        >
          {busy ? 'zapisuję…' : 'Zapisz'}
        </_ActionButton>
      </div>
    </div>
  );
}

// ── Telegram bot group (secrets) ─────────────────────────────────────────────
function _TelegramGroup() {
  const toast = _nUseToast();

  // probe — whether TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are present and the
  // bot is reachable from this box.
  const [probe, setProbe] = _nUseState(null);
  const [probing, setProbing] = _nUseState(false);
  const [testing, setTesting] = _nUseState(false);
  const [detecting, setDetecting] = _nUseState(false);

  // inline secret drafts + the set of existing keys (to decide captcha-on-write)
  const [tokenDraft, setTokenDraft] = _nUseState('');
  const [chatDraft, setChatDraft] = _nUseState('');
  const [savingToken, setSavingToken] = _nUseState(false);
  const [savingChat, setSavingChat] = _nUseState(false);

  const loadProbe = _nUseCallback(async () => {
    setProbing(true);
    try {
      const r = await fetch(window.apiUrl('/api/notify/probe'));
      const data = r.ok ? await r.json() : { ok: false, error: `http ${r.status}` };
      setProbe(data);
    } catch (e) {
      setProbe({ ok: false, error: (e && e.message) || 'probe failed' });
    } finally {
      setProbing(false);
    }
  }, []);

  _nUseEffect(() => { loadProbe(); }, [loadProbe]);

  // Write a secret (token / chat id) through the secrets API with the
  // captcha-on-overwrite dance, then re-probe.
  const saveSecret = _nUseCallback(async (key, value, setSaving, setDraft) => {
    const trimmed = value.trim();
    if (!trimmed) return;
    setSaving(true);
    try {
      let existing;
      try {
        existing = await _notifyListEnvKeys();
      } catch (e) {
        existing = new Set(); // best-effort: backend will still gate overwrite by 400
      }
      const isOverwrite = existing.has(key);
      await _notifyWriteSecret(key, trimmed, isOverwrite);
      setDraft('');
      if (toast) toast('Zapisano ' + key, 'ok');
      await loadProbe();
    } catch (e) {
      if (toast) toast('Zapis ' + key + ' nieudany: ' + ((e && e.message) || 'błąd'), 'err');
    } finally {
      setSaving(false);
    }
  }, [toast, loadProbe]);

  const handleTest = _nUseCallback(async () => {
    if (testing) return;
    setTesting(true);
    try {
      const r = await fetch(window.apiUrl('/api/notify/test'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic: 'system', message: 'Test push z dashboardu — jeśli to widzisz, działa.' }),
      });
      const data = await r.json().catch(() => ({}));
      if (data.ok) {
        if (toast) toast('Test wysłany — sprawdź Telegrama', 'ok');
      } else if (toast) {
        toast('Test nieudany: ' + (data.reason || data.error || 'unknown'), 'err');
      }
    } catch (e) {
      if (toast) toast('Test nieudany: ' + ((e && e.message) || 'błąd'), 'err');
    } finally {
      setTesting(false);
    }
  }, [testing, toast]);

  // Detect chat_id, then write it through the secrets API (captcha if the key
  // already exists) and re-probe — self-contained setup.
  const handleDetectChatId = _nUseCallback(async () => {
    if (detecting) return;
    setDetecting(true);
    try {
      const r = await fetch(window.apiUrl('/api/notify/detect-chat-id'), { method: 'POST' });
      const data = await r.json().catch(() => ({}));
      if (data.ok && data.chat_id) {
        const chatId = String(data.chat_id);
        try {
          let existing;
          try {
            existing = await _notifyListEnvKeys();
          } catch (e) {
            existing = new Set();
          }
          const isOverwrite = existing.has(_TELEGRAM_CHATID_KEY);
          await _notifyWriteSecret(_TELEGRAM_CHATID_KEY, chatId, isOverwrite);
          if (toast) toast(`Zapisano chat_id ${chatId} (${data.from || '?'})`, 'ok');
          await loadProbe();
        } catch (e) {
          // detection worked but the write failed — surface the raw chat_id so
          // the user can paste it manually.
          if (toast) toast(`Znaleziono chat_id ${chatId}, ale zapis nieudany: ${(e && e.message) || 'błąd'}`, 'err');
          setChatDraft(chatId);
        }
      } else if (toast) {
        toast(data.error || 'Nie znaleziono — wyślij /start do bota i spróbuj ponownie', 'err');
      }
    } catch (e) {
      if (toast) toast('Detect nieudany: ' + ((e && e.message) || 'błąd'), 'err');
    } finally {
      setDetecting(false);
    }
  }, [detecting, toast, loadProbe]);

  // status badge: green when probe.ok, red when missing creds, gray loading
  const probeOk = probe && probe.ok;
  const probeError = probe && !probe.ok ? probe.error : null;
  const statusColor = probing
    ? 'var(--fg-3)'
    : probeOk ? 'var(--ok, #5fbf6c)' : 'var(--err)';
  const statusLabel = probing
    ? 'sprawdzam…'
    : probeOk
      ? `OK · @${probe.bot_username || '?'} · chat ${probe.chat_id}`
      : `nieskonfigurowane · ${probeError || 'brak danych'}`;

  const refreshAction = (
    <_ActionButton variant="quiet" disabled={probing} icon="refresh" onClick={loadProbe}>
      {probing ? '…' : 'odśwież'}
    </_ActionButton>
  );

  return (
    <_nSettingGroup
      title="Telegram (bot)"
      scope="secrets"
      desc="Push przez Telegrama — żadnych otwartych portów, działa na każdym urządzeniu z Telegramem."
    >
      <_nSettingCard>
        <_nStatusBanner color={statusColor} label={statusLabel} action={refreshAction} />

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <_ActionButton variant="primary" disabled={testing || !probeOk} onClick={handleTest}>
            {testing ? 'wysyłam…' : 'Test push'}
          </_ActionButton>
          <_ActionButton variant="ghost" disabled={detecting} onClick={handleDetectChatId}>
            {detecting ? 'szukam…' : 'Wykryj chat_id'}
          </_ActionButton>
        </div>

        <_SecretInputRow
          label="Token bota"
          hint={`Zapisywany jako ${_TELEGRAM_TOKEN_KEY} w ~/.env. Nadpisanie istniejącego wymaga captchy.`}
          placeholder="123456:ABC-DEF…"
          value={tokenDraft}
          busy={savingToken}
          onChange={setTokenDraft}
          onSave={() => saveSecret(_TELEGRAM_TOKEN_KEY, tokenDraft, setSavingToken, setTokenDraft)}
        />

        <_SecretInputRow
          label="Chat ID"
          hint={`Zapisywany jako ${_TELEGRAM_CHATID_KEY}. Możesz też kliknąć „Wykryj chat_id" powyżej.`}
          placeholder="np. 123456789"
          value={chatDraft}
          busy={savingChat}
          onChange={setChatDraft}
          onSave={() => saveSecret(_TELEGRAM_CHATID_KEY, chatDraft, setSavingChat, setChatDraft)}
        />

        <_nAdvancedDisclosure label="Pierwsze uruchomienie / setup">
          <ol className="mono" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7, fontSize: 'var(--t-cap)', color: 'var(--fg-2)' }}>
            <li>W Telegramie napisz do <code>@BotFather</code> komendę <code>/newbot</code>, podążaj instrukcją, skopiuj token.</li>
            <li>Wklej token w pole <em>Token bota</em> powyżej i kliknij <em>Zapisz</em>.</li>
            <li>Otwórz w Telegramie świeżo utworzonego bota i wyślij mu <code>/start</code>.</li>
            <li>Kliknij <em>Wykryj chat_id</em> — numer chat zapisze się automatycznie.</li>
            <li>Kliknij <em>Test push</em>. Jeśli przyszło — gotowe.</li>
          </ol>
        </_nAdvancedDisclosure>
      </_nSettingCard>
    </_nSettingGroup>
  );
}

// ── per-topic mute row ───────────────────────────────────────────────────────
function _MuteRow({ label, desc, muted, busy, onToggle }) {
  return (
    <_nSettingCard style={{ padding: '10px 12px', background: 'var(--surface-2)' }}>
      <_nSettingRow
        label={label}
        desc={desc}
        control={<_nToggle checked={!muted} disabled={busy} onChange={() => { if (!busy) onToggle(); }} />}
      />
    </_nSettingCard>
  );
}

// ── topic mutes group (server, NO captcha) ───────────────────────────────────
function _MutesGroup() {
  const toast = _nUseToast();
  const [disabled, setDisabled] = _nUseState([]); // array of muted topic keys
  const [loaded, setLoaded] = _nUseState(false);
  const [busy, setBusy] = _nUseState(false);
  const [readError, setReadError] = _nUseState(null); // non-null = baseline unknown

  const loadMutes = _nUseCallback(async () => {
    try {
      const r = await fetch(window.apiUrl('/api/notify/mutes'));
      if (!r.ok) throw new Error(`http ${r.status}`);
      const body = await r.json();
      // The endpoint returns HTTP 200 with {ok:false,...} when the read fails —
      // checking only r.ok would render an empty baseline as "everything enabled"
      // and let a toggle silently wipe the real mutes. Treat ok:false as an error.
      if (!body || body.ok === false) throw new Error((body && body.error) || 'odczyt nieudany');
      const list = Array.isArray(body.disabled) ? body.disabled.filter((t) => typeof t === 'string') : [];
      setDisabled(list);
      setReadError(null);
      setLoaded(true);
    } catch (e) {
      setReadError((e && e.message) || 'błąd');
      if (toast) toast('Nie udało się odczytać wyciszeń: ' + ((e && e.message) || 'błąd'), 'err');
      setLoaded(true);
    }
  }, [toast]);

  _nUseEffect(() => { loadMutes(); }, [loadMutes]);

  // Toggling a topic ON (un-mute) removes it from `disabled`; OFF adds it.
  // Optimistic: compute the next full list, render it, PUT it, revert on fail.
  const handleToggle = _nUseCallback(async (topic) => {
    // Don't write on an unknown baseline — a full-list PUT computed from a failed
    // read would wipe the real mutes. The rows are also disabled in that state.
    if (busy || readError) return;
    const prev = disabled;
    const isMuted = prev.includes(topic);
    const next = isMuted ? prev.filter((t) => t !== topic) : [...prev, topic];
    setDisabled(next);
    setBusy(true);
    try {
      const r = await fetch(window.apiUrl('/api/notify/mutes'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled: next }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `http ${r.status}`);
      }
      const body = await r.json().catch(() => ({}));
      if (Array.isArray(body && body.disabled)) {
        setDisabled(body.disabled.filter((t) => typeof t === 'string'));
      }
      if (toast) toast(isMuted ? `Włączono: ${topic}` : `Wyciszono: ${topic}`, 'ok');
    } catch (e) {
      setDisabled(prev); // revert
      if (toast) toast('Zapis nieudany: ' + ((e && e.message) || 'błąd'), 'err');
    } finally {
      setBusy(false);
    }
  }, [busy, disabled, readError, toast]);

  return (
    <_nSettingGroup
      title="Wyciszenia tematów"
      scope="server"
      desc="Wyłącz wybrane źródła powiadomień. Dotyczy całego serwera — nie tylko tego urządzenia."
    >
      {_NOTIFY_TOPICS.map((t) => (
        <_MuteRow
          key={t.key}
          label={t.label}
          desc={t.desc}
          muted={loaded && !readError && disabled.includes(t.key)}
          busy={busy || !loaded || !!readError}
          onToggle={() => handleToggle(t.key)}
        />
      ))}
      {!loaded && (
        <Spinner label="Ładowanie stanu…" inline size={14} />
      )}
      {loaded && readError && (
        <_nStatusBanner variant="err" label={'Nie udało się odczytać wyciszeń (' + readError + ') — przełączniki zablokowane, by nie nadpisać stanu.'} inline />
      )}
    </_nSettingGroup>
  );
}

// ── tab body ─────────────────────────────────────────────────────────────────
function SettingsNotifications({ settings, updateSettings }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <_nSettingGroup title="Na tym urządzeniu" scope="device">
        <_WebPushRow />
        <_nToggleRow
          label="Dźwięk powiadomień"
          desc="Krótki ding gdy agent skończy odpowiadać."
          value={settings.unreadSound}
          onChange={(v) => updateSettings({ unreadSound: v })}
        />
      </_nSettingGroup>

      <_TelegramGroup />

      <_MutesGroup />
    </div>
  );
}

window.SettingsNotifications = SettingsNotifications;
