// scheduler-create-modal-trigger.jsx — Trigger step (Step 2) of the cron
// wizard. Extracted from scheduler-create-modal.jsx so the main wizard file
// stays under the 800 LOC budget.
//
// Exposes:
//   window._ScmStepTrigger — the step 2 component used by SchedulerCreateModal.
//
// Depends on globals from the main modal file (loaded after this file but
// these helpers are only invoked at render time, so the order at script-eval
// time doesn't matter for them):
//   window._scmInputStyle — shared input style helper
// And on globals from components.jsx:
//   Icon
//
// Tabs: Preset (X-min, X-hr, daily, weekly, monthly), Cron, One-shot date.
// Plus timezone select and a collapsible end-condition (max_runs / until).

const { useState: _scmTrigUseState } = React;

const _SCM_TZ_OPTIONS = [
  'Europe/Warsaw', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
  'UTC', 'America/New_York', 'America/Los_Angeles', 'America/Chicago',
  'Asia/Tokyo', 'Asia/Singapore', 'Asia/Dubai', 'Australia/Sydney',
];
const _SCM_WEEKDAYS = [
  { v: '1', l: 'Mon' }, { v: '2', l: 'Tue' }, { v: '3', l: 'Wed' },
  { v: '4', l: 'Thu' }, { v: '5', l: 'Fri' }, { v: '6', l: 'Sat' }, { v: '0', l: 'Sun' },
];

function _scmTrigInputStyle(extra) {
  // Delegate to the main modal's helper if present; otherwise fall back to a
  // local copy so this file remains independently testable.
  if (typeof window._scmInputStyle === 'function') return window._scmInputStyle(extra);
  return {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
    ...(extra || {}),
  };
}

function _ScmTriggerTabs({ tab, onChange }) {
  const tabs = [
    { k: 'preset', l: 'Preset' },
    { k: 'cron', l: 'Cron' },
    { k: 'date', l: 'One-shot' },
  ];
  return (
    <div style={{
      display: 'flex', gap: 4, padding: 3, flexWrap: 'wrap',
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', alignSelf: 'flex-start',
    }}>
      {tabs.map((t) => (
        <button type="button" key={t.k} onClick={() => onChange(t.k)} style={{
          padding: '6px 14px', borderRadius: 'var(--r-sm)',
          background: tab === t.k ? 'var(--accent-soft)' : 'transparent',
          color: tab === t.k ? 'var(--accent)' : 'var(--fg-2)',
          border: '1px solid ' + (tab === t.k ? 'var(--accent-line)' : 'transparent'),
          fontSize: 'var(--t-cap)', fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
        }}>{t.l}</button>
      ))}
    </div>
  );
}

function _ScmPresetForm({ preset, setPreset }) {
  const update = (patch) => setPreset({ ...preset, ...patch });
  const toggleWeekday = (v) => {
    const cur = new Set(preset.weekdays || []);
    if (cur.has(v)) cur.delete(v); else cur.add(v);
    update({ weekdays: Array.from(cur) });
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <select
        value={preset.kind}
        onChange={(e) => update({ kind: e.target.value })}
        style={_scmTrigInputStyle()}
      >
        <option value="every-min">Every X minutes</option>
        <option value="every-hour">Every X hours</option>
        <option value="daily">Daily at HH:MM</option>
        <option value="weekly">Weekly on weekday(s) at HH:MM</option>
        <option value="monthly">Monthly on day N at HH:MM</option>
      </select>
      {preset.kind === 'every-min' && (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Every</span>
          <input type="number" min={1} max={59} value={preset.value || 5}
            onChange={(e) => update({ value: e.target.value })}
            style={_scmTrigInputStyle({ width: 80 })} />
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>minutes</span>
        </label>
      )}
      {preset.kind === 'every-hour' && (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Every</span>
          <input type="number" min={1} max={23} value={preset.value || 1}
            onChange={(e) => update({ value: e.target.value })}
            style={_scmTrigInputStyle({ width: 80 })} />
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>hours</span>
        </label>
      )}
      {(preset.kind === 'daily' || preset.kind === 'weekly' || preset.kind === 'monthly') && (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>At</span>
          <input type="number" min={0} max={23} value={preset.hour ?? 9}
            onChange={(e) => update({ hour: e.target.value })}
            style={_scmTrigInputStyle({ width: 70 })} />
          <span>:</span>
          <input type="number" min={0} max={59} value={preset.minute ?? 0}
            onChange={(e) => update({ minute: e.target.value })}
            style={_scmTrigInputStyle({ width: 70 })} />
        </label>
      )}
      {preset.kind === 'weekly' && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {_SCM_WEEKDAYS.map((d) => {
            const on = (preset.weekdays || []).includes(d.v);
            return (
              <button type="button" key={d.v} onClick={() => toggleWeekday(d.v)} style={{
                padding: '6px 10px', borderRadius: 'var(--r-sm)',
                background: on ? 'var(--accent-soft)' : 'var(--surface-2)',
                color: on ? 'var(--accent)' : 'var(--fg-2)',
                border: '1px solid ' + (on ? 'var(--accent-line)' : 'var(--hairline)'),
                fontSize: 'var(--t-cap)', cursor: 'pointer', fontFamily: 'inherit',
              }}>{d.l}</button>
            );
          })}
        </div>
      )}
      {preset.kind === 'monthly' && (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Day of month</span>
          <input type="number" min={1} max={28} value={preset.day || 1}
            onChange={(e) => update({ day: e.target.value })}
            style={_scmTrigInputStyle({ width: 80 })} />
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>(1–28)</span>
        </label>
      )}
    </div>
  );
}

function _ScmEndCondition({ endCondition, setEndCondition }) {
  const [open, setOpen] = _scmTrigUseState(!!(endCondition && (endCondition.max_runs || endCondition.until)));
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <button type="button" onClick={() => setOpen((v) => !v)} style={{
        display: 'flex', alignItems: 'center', gap: 8, alignSelf: 'flex-start',
        background: 'transparent', border: 'none', padding: '4px 0',
        color: 'var(--fg-3)', fontSize: 'var(--t-cap)', fontFamily: 'inherit', cursor: 'pointer',
      }}>
        <Icon name={open ? 'chevron-d' : 'chevron-r'} size={12} />
        <span>End condition (optional)</span>
      </button>
      {open && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingLeft: 18 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', minWidth: 130 }}>Max runs</span>
            <input type="number" min={1} value={endCondition.max_runs || ''}
              onChange={(e) => setEndCondition({ ...endCondition, max_runs: e.target.value ? Number(e.target.value) : null })}
              placeholder="unlimited"
              style={_scmTrigInputStyle({ width: 140 })} />
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)', minWidth: 130 }}>Run until</span>
            <input type="datetime-local" value={endCondition.until || ''}
              onChange={(e) => setEndCondition({ ...endCondition, until: e.target.value || null })}
              style={_scmTrigInputStyle({ width: 220 })} />
          </label>
        </div>
      )}
    </div>
  );
}

function _ScmStepTrigger({ trigger, setTrigger, endCondition, setEndCondition, livePreview }) {
  // `trigger` here is the wizard state ({tab, preset, cronExpr, dateValue, tz}).
  const update = (patch) => setTrigger({ ...trigger, ...patch });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <_ScmTriggerTabs tab={trigger.tab} onChange={(k) => update({ tab: k })} />
      {trigger.tab === 'preset' && (
        <_ScmPresetForm preset={trigger.preset} setPreset={(p) => update({ preset: p })} />
      )}
      {trigger.tab === 'cron' && (
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Cron expression</span>
          <input
            value={trigger.cronExpr}
            onChange={(e) => update({ cronExpr: e.target.value })}
            placeholder="0 7 * * 1-5"
            spellCheck={false}
            style={_scmTrigInputStyle({ fontFamily: 'JetBrains Mono, ui-monospace, monospace' })}
          />
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            5-field cron (min hour dom mon dow). E.g. 0 7 * * 1-5 for weekdays at 7am.{' '}
            <a href="https://crontab.guru" target="_blank" rel="noreferrer"
               style={{ color: 'var(--accent)', textDecoration: 'underline' }}>crontab.guru</a>
          </span>
        </label>
      )}
      {trigger.tab === 'date' && (
        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Fire once at</span>
          <input
            type="datetime-local"
            value={trigger.dateValue}
            onChange={(e) => update({ dateValue: e.target.value })}
            style={_scmTrigInputStyle({ width: 260 })}
          />
        </label>
      )}
      <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Timezone</span>
        <select value={trigger.tz}
          onChange={(e) => update({ tz: e.target.value })}
          style={_scmTrigInputStyle({ width: 280 })}>
          {_SCM_TZ_OPTIONS.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
        </select>
      </label>
      <_ScmEndCondition endCondition={endCondition} setEndCondition={setEndCondition} />
      {livePreview}
    </div>
  );
}

Object.assign(window, { _ScmStepTrigger });
