// Tasks tab — Kanban + List views, CRUD over /api/tasks/*, reminders editor.
// Single-file v1; split into tasks-{board,list,editor}.jsx if it grows past ~1k LOC.

const { useEffect, useMemo, useReducer, useState, useRef, useCallback } = React;

const TASK_STATE_KEY = 'hub:tasks:view';
const TASK_PRESET_KEY = 'hub:tasks:preset';
const TASK_INBOX_COL_KEY = 'hub:tasks:show-inbox';
const TASK_IDEAS_COL_KEY = 'hub:tasks:show-ideas';

// Tasks tab presets — collapse the 8-column GH Project schema into a few
// workflow phases the user actually thinks in. Inbox/Ideas live in their own
// sidebar sections (separate from this kanban); Archive is its own preset.
const TASK_PRESETS = {
  full:    { label: 'Full',    statuses: ['Backlog', 'This Week', 'Today', 'In Progress', 'Done'] },
  week:    { label: 'Week',    statuses: ['This Week', 'Today', 'In Progress', 'Done'] },
  today:   { label: 'Today',   statuses: ['Today', 'In Progress', 'Done'] },
  archive: { label: 'Archive', statuses: ['Archived'] },
};

// "Advance to next column" mapping for the mobile single-button replacement of
// the per-card status dropdown. Inbox / Ideas are entry points that funnel
// into Backlog; from there it's the canonical Full preset flow. Done /
// Archived are terminal — the button is hidden, the user uses the detail
// panel or DnD on desktop if they really need to back out.
const TASK_NEXT_STATUS = {
  'Inbox':       'Backlog',
  'Ideas':       'Backlog',
  'Backlog':     'This Week',
  'This Week':   'Today',
  'Today':       'In Progress',
  'In Progress': 'Done',
};

// Desktop / mobile breakpoint — kept in sync with app.jsx's 960px so the same
// resize event reaches every consumer.
const TASK_MOBILE_BREAKPOINT_PX = 960;

function useIsMobile(breakpoint = TASK_MOBILE_BREAKPOINT_PX) {
  const [isMobile, setIsMobile] = useState(
    typeof window !== 'undefined' && window.innerWidth < breakpoint
  );
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onResize = () => setIsMobile(window.innerWidth < breakpoint);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [breakpoint]);
  return isMobile;
}

const INITIAL_STATE = {
  preset: 'full',  // 'full' | 'week' | 'today' | 'archive'
  showInbox: false,  // additive Inbox column toggle (overlays current preset)
  showIdeas: false,  // additive Ideas column toggle
  tasks: [],
  config: null,
  areas: [],
  projects: [],
  view: 'kanban',
  filters: {
    area: [], project: [], priority: [], status: [], category: [], dueBucket: [], query: '', state: 'open',
  },
  selectedId: null,
  createOpen: false,
  loading: 'idle', // 'idle' | 'loading' | 'refreshing' | 'error'
  error: null,
  patchSeq: 0,
};

function tasksReducer(state, action) {
  switch (action.type) {
    case 'LOAD_BEGIN':
      return { ...state, loading: state.tasks.length ? 'refreshing' : 'loading', error: null };
    case 'LOAD_OK':
      return {
        ...state,
        loading: 'idle',
        error: null,
        tasks: action.tasks,
        config: action.config ?? state.config,
        areas: action.areas ?? state.areas,
        projects: action.projects ?? state.projects,
      };
    case 'LOAD_ERR':
      return { ...state, loading: 'error', error: action.error };
    case 'SET_VIEW':
      try { localStorage.setItem(TASK_STATE_KEY, action.view); } catch (_) {}
      return { ...state, view: action.view };
    case 'SET_PRESET':
      try { localStorage.setItem(TASK_PRESET_KEY, action.preset); } catch (_) {}
      return { ...state, preset: action.preset };
    case 'TOGGLE_INBOX_COL': {
      const next = !state.showInbox;
      try { localStorage.setItem(TASK_INBOX_COL_KEY, next ? '1' : '0'); } catch (_) {}
      return { ...state, showInbox: next };
    }
    case 'TOGGLE_IDEAS_COL': {
      const next = !state.showIdeas;
      try { localStorage.setItem(TASK_IDEAS_COL_KEY, next ? '1' : '0'); } catch (_) {}
      return { ...state, showIdeas: next };
    }
    case 'SET_FILTER':
      return { ...state, filters: { ...state.filters, ...action.patch } };
    case 'CLEAR_FILTERS':
      return { ...state, filters: { ...INITIAL_STATE.filters, state: state.filters.state } };
    case 'OPEN_TASK':
      return { ...state, selectedId: action.id };
    case 'CLOSE_DRAWER':
      return { ...state, selectedId: null };
    case 'OPEN_CREATE':
      return { ...state, createOpen: true };
    case 'CLOSE_CREATE':
      return { ...state, createOpen: false };
    case 'PATCH_ITEM': {
      const tasks = state.tasks.map(t => t.issue_node_id === action.id ? { ...t, ...action.patch } : t);
      return { ...state, tasks, patchSeq: state.patchSeq + 1 };
    }
    case 'REPLACE_ITEM': {
      const tasks = state.tasks.map(t => t.issue_node_id === action.id ? action.item : t);
      return { ...state, tasks };
    }
    case 'REMOVE_ITEM':
      return {
        ...state,
        tasks: state.tasks.filter(t => t.issue_node_id !== action.id),
        selectedId: state.selectedId === action.id ? null : state.selectedId,
      };
    case 'INSERT_ITEM':
      return { ...state, tasks: [action.item, ...state.tasks] };
    default:
      return state;
  }
}

// ── utils ──────────────────────────────────────────────────────

function apiUrl(path) { return (window.HUB_BASE_PATH || '') + path; }

async function jsonFetch(path, opts = {}) {
  const r = await fetch(apiUrl(path), {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const text = await r.text();
  let data; try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { error: text }; }
  if (!r.ok) {
    const err = (data && (data.detail?.error || data.detail || data.error)) || r.statusText;
    const e = new Error(typeof err === 'string' ? err : JSON.stringify(err));
    e.status = r.status; e.detail = data?.detail || data;
    throw e;
  }
  return data;
}

// Launch a new orchestrator chat session with a prefilled first message.
// Accepts optional `cwd` (must be under $HOME) and `lib_id` (informational
// pointer — used by findOrCreateTaskSession to find the right session later).
async function launchAgentSession({ title, prompt, cwd, lib_id }) {
  const body = { title: title || 'New session' };
  if (cwd) body.cwd = cwd;
  if (lib_id) body.lib_id = lib_id;
  const create = await jsonFetch('/api/orchestrator/sessions', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  const sid = create.session_id || create.id;
  if (!sid) throw new Error('no session_id in response');
  await jsonFetch(`/api/orchestrator/sessions/${encodeURIComponent(sid)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ text: prompt }),
  });
  const target = (window.HUB_BASE_PATH || '') + '/orchestrator/' + encodeURIComponent(sid);
  window.location.assign(target);
}

// "Discuss this task" entry point — looks up an existing chat session for the
// task by `lib_id == "task:<issue_node_id>"` and jumps there if found.
// Otherwise spawns a new session with cwd resolved from task.proj_slug or
// task.area_slug (falling back to Global agent / cwd=null), prefilled prompt
// asking the agent to ask clarifying questions first.
async function findOrCreateTaskSession(task, areas, projects, opts) {
  const libId = `task:${task.issue_node_id}`;
  // 1. Existing session?
  try {
    const list = await jsonFetch('/api/orchestrator/sessions');
    const sessions = Array.isArray(list) ? list : (list.sessions || []);
    const existing = sessions.find(s => s.lib_id === libId && !s.archived);
    if (existing) {
      const sid = existing.id || existing.session_id;
      if (sid) {
        window.location.assign((window.HUB_BASE_PATH || '') + '/orchestrator/' + encodeURIComponent(sid));
        return;
      }
    }
  } catch (_) {}
  // 2. Resolve cwd: project takes precedence over area; Global if neither.
  let cwd = null;
  if (task.proj_slug) {
    const p = (projects || []).find(x => x.slug === task.proj_slug);
    if (p && p.path) cwd = p.path;
  }
  if (!cwd && task.area_slug) {
    const a = (areas || []).find(x => x.slug === task.area_slug);
    if (a && a.path) cwd = a.path;
  }
  // 3. Build a sensible default prompt (caller can override).
  const titleText = task.title || '(no title)';
  const defaultPrompt =
    `Omówmy ten task: "${titleText}".\n\n` +
    `Status: ${task.status || '-'}\n` +
    `Priorytet: ${task.priority || '-'}\n` +
    `Kategoria: ${task.category || '-'}\n` +
    `Due: ${task.due_date || '-'}\n` +
    `Area / Project: ${task.area_slug || '-'} / ${task.proj_slug || '-'}\n` +
    `Body: ${task.body || '(puste)'}\n` +
    `URL: ${task.url}\n\n` +
    `**Zanim cokolwiek zaproponujesz** — zadaj mi 2–4 pytania doprecyzowujące. ` +
    `Cel rozmowy: doprecyzować zakres, ustalić następny konkretny krok, ` +
    `albo (jeśli to większy zakres) zaproponować rozbicie na podzadania. ` +
    `Po ustaleniach możesz zaplikować zmiany przez skill \`gh-tasks update --issue ` +
    `${task.number || '?'} ...\`.`;
  await launchAgentSession({
    title: (opts && opts.title) || `Task: ${titleText}`,
    prompt: (opts && opts.prompt) || defaultPrompt,
    cwd,
    lib_id: libId,
  });
}

function dueBucket(due) {
  if (!due) return 'no-due';
  const [y, m, d] = due.split('-').map(Number);
  const today = new Date(); today.setHours(0,0,0,0);
  const target = new Date(y, (m||1)-1, d||1);
  const days = Math.round((target - today) / 86400000);
  if (days < 0) return 'overdue';
  if (days === 0) return 'today';
  if (days <= 7) return 'this-week';
  return 'later';
}

function dueRelative(due) {
  if (!due) return null;
  const b = dueBucket(due);
  if (b === 'overdue') {
    const [y,m,d] = due.split('-').map(Number);
    const today = new Date(); today.setHours(0,0,0,0);
    const target = new Date(y,(m||1)-1,d||1);
    const days = Math.round((today - target) / 86400000);
    return { label: `${days}d overdue`, color: 'var(--danger)' };
  }
  if (b === 'today') return { label: 'today', color: 'var(--warning)' };
  if (b === 'this-week') {
    const [y,m,d] = due.split('-').map(Number);
    const today = new Date(); today.setHours(0,0,0,0);
    const target = new Date(y,(m||1)-1,d||1);
    return { label: `in ${Math.round((target-today)/86400000)}d`, color: 'var(--fg-2)' };
  }
  return { label: due, color: 'var(--fg-3)' };
}

const PRIORITY_COLORS = {
  'P0-Critical': '#dc2626',
  'P1-Must':     '#ef4444',
  'P2-Important':'#f59e0b',
  'P3-Nice':     '#10b981',
  'Idea':        '#60a5fa',
};

function priorityBadgeStyle(p) {
  const color = PRIORITY_COLORS[p] || 'var(--fg-3)';
  return {
    fontSize: 'var(--t-2xs)', fontWeight: 600, color, border: `1px solid ${color}`,
    borderRadius: 'var(--r-pill)', padding: '1px 6px', whiteSpace: 'nowrap',
  };
}

function applyFilters(tasks, filters) {
  const q = (filters.query || '').trim().toLowerCase();
  return tasks.filter(t => {
    if (filters.state === 'open'   && (t.state || 'OPEN').toUpperCase() !== 'OPEN')   return false;
    if (filters.state === 'closed' && (t.state || 'OPEN').toUpperCase() !== 'CLOSED') return false;
    if (filters.area.length && !filters.area.includes(t.area_slug)) return false;
    if (filters.project.length && !filters.project.includes(t.proj_slug)) return false;
    if (filters.priority.length && !filters.priority.includes(t.priority)) return false;
    if (filters.status.length && !filters.status.includes(t.status)) return false;
    if (filters.category.length && !filters.category.includes(t.category)) return false;
    if (filters.dueBucket.length && !filters.dueBucket.includes(dueBucket(t.due_date))) return false;
    if (q) {
      const hay = [(t.title || ''), ...(t.labels || [])].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

// ── small UI atoms ─────────────────────────────────────────────

function Pill({ children, color = 'var(--fg-2)', onClick, title }) {
  return (
    <span
      onClick={onClick}
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontSize: 'var(--t-xs)', color, padding: '2px 8px',
        background: 'var(--surface-2)', borderRadius: 'var(--r-pill)',
        cursor: onClick ? 'pointer' : 'default', whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

function Select({ value, onChange, options, placeholder, disabled, style }) {
  return (
    <select
      value={value || ''}
      disabled={disabled}
      onChange={e => onChange(e.target.value || null)}
      style={{
        background: 'var(--surface-2)', color: 'var(--fg-1)',
        border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
        padding: '6px 8px', fontSize: 'var(--t-cap)', fontFamily: 'inherit',
        ...style,
      }}
    >
      {placeholder && <option value="">{placeholder}</option>}
      {options.map(o => {
        const v = typeof o === 'string' ? o : o.value;
        const l = typeof o === 'string' ? o : o.label;
        return <option key={v} value={v}>{l}</option>;
      })}
    </select>
  );
}

function FilterChipGroup({ id, label, options, selected, onChange, openId, setOpenId }) {
  const wrapperRef = useRef(null);
  const open = openId === id;
  useEffect(() => {
    if (!open) return;
    const onDown = (e) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target)) {
        setOpenId(prev => (prev === id ? null : prev));
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open, id, setOpenId]);
  if (!options || options.length === 0) return null;
  const count = selected.length;
  return (
    <div ref={wrapperRef} style={{ position: 'relative' }}>
      <button
        onClick={() => setOpenId(open ? null : id)}
        style={{
          fontSize: 'var(--t-cap)', padding: '6px 10px', borderRadius: 'var(--r-pill)',
          background: count ? 'var(--accent-dim, rgba(99,102,241,.15))' : 'var(--surface-2)',
          color: count ? 'var(--accent)' : 'var(--fg-2)',
          border: '1px solid var(--hairline)', cursor: 'pointer', whiteSpace: 'nowrap',
        }}
      >
        {label}{count > 0 ? ` · ${count}` : ''}
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: 'calc(100% + 4px)', left: 0, zIndex: 30,
          background: 'var(--surface-1)', border: '1px solid var(--hairline-strong)',
          borderRadius: 'var(--r-md)', padding: 8, minWidth: 180,
          boxShadow: 'var(--shadow-1)', maxHeight: 280, overflowY: 'auto',
        }}>
          {options.map(opt => {
            const v = typeof opt === 'string' ? opt : opt.value;
            const l = typeof opt === 'string' ? opt : opt.label;
            const on = selected.includes(v);
            return (
              <label key={v} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 6px', cursor: 'pointer', fontSize: 'var(--t-cap)' }}>
                <input
                  type="checkbox" checked={on}
                  onChange={() => onChange(on ? selected.filter(x => x !== v) : [...selected, v])}
                />
                <span style={{ color: 'var(--fg-1)' }}>{l}</span>
              </label>
            );
          })}
          {selected.length > 0 && (
            <button onClick={() => onChange([])} style={{
              marginTop: 4, fontSize: 'var(--t-xs)', color: 'var(--fg-3)', background: 'transparent',
              border: 'none', cursor: 'pointer', padding: '4px 6px',
            }}>Clear</button>
          )}
        </div>
      )}
    </div>
  );
}

// ── reminder editor ────────────────────────────────────────────

const REMINDER_KIND_OPTIONS = [
  { value: 'at_period', label: 'At period' },
  { value: 'at_time',   label: 'At specific time' },
];
const REMINDER_OFFSET_OPTIONS = [
  { value: '0',   label: 'Day of due' },
  { value: '-1',  label: '1 day before' },
  { value: '-2',  label: '2 days before' },
  { value: '-3',  label: '3 days before' },
  { value: '-7',  label: '1 week before' },
  { value: '-14', label: '2 weeks before' },
];
const REMINDER_PERIOD_OPTIONS = [
  { value: 'morning',   label: 'Morning · 9:00' },
  { value: 'noon',      label: 'Noon · 12:00' },
  { value: 'afternoon', label: 'Afternoon · 15:00' },
  { value: 'evening',   label: 'Evening · 21:00' },
];
const REMINDER_UNITS = [
  { value: 'min',  label: 'min' },
  { value: 'hour', label: 'h' },
  { value: 'day',  label: 'd' },
];

function describeLegacyReminder(r) {
  if (r.kind === 'morning_of') return 'morning of due (legacy)';
  if (r.kind === 'exact')      return 'at due time (legacy)';
  if (r.kind === 'before')     return `${r.value} ${r.unit} before (legacy)`;
  return r.kind;
}

function ReminderRow({ reminder, onChange, onRemove, hasDue, taskHasTime }) {
  const r = reminder;
  const isLegacy = r.kind === 'morning_of' || r.kind === 'exact' || (r.kind === 'before');
  if (isLegacy) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 6px', background: 'var(--surface-2)', borderRadius: 'var(--r-md)', opacity: 0.85 }}>
        <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{describeLegacyReminder(r)}</span>
        {r.kind === 'before' && !taskHasTime && (
          <span style={{ fontSize: 'var(--t-xs)', color: 'var(--warning)' }} title="No task hour set; anchors to default 09:00">
            ⚠ no task time
          </span>
        )}
        <button onClick={onRemove} style={{ marginLeft: 'auto', background: 'transparent', border: 'none', color: 'var(--fg-3)', cursor: 'pointer' }} title="Remove">×</button>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <Select
        value={r.kind || 'at_period'}
        onChange={k => {
          if (k === 'at_period') onChange({ kind: 'at_period', offset_days: r.offset_days ?? 0, period: r.period || 'morning' });
          else if (k === 'at_time') onChange({ kind: 'at_time', offset_days: r.offset_days ?? 0, time: r.time || '17:00' });
        }}
        options={REMINDER_KIND_OPTIONS}
        disabled={!hasDue}
      />
      <Select
        value={String(r.offset_days ?? 0)}
        onChange={v => onChange({ ...r, offset_days: parseInt(v, 10) })}
        options={REMINDER_OFFSET_OPTIONS}
        disabled={!hasDue}
      />
      {r.kind === 'at_period' && (
        <Select
          value={r.period || 'morning'}
          onChange={p => onChange({ ...r, period: p })}
          options={REMINDER_PERIOD_OPTIONS}
          disabled={!hasDue}
        />
      )}
      {r.kind === 'at_time' && (
        <input
          type="time"
          value={r.time || '17:00'}
          onChange={e => onChange({ ...r, time: e.target.value })}
          disabled={!hasDue}
          style={{
            padding: '6px 8px', fontSize: 'var(--t-cap)', fontFamily: 'inherit',
            background: 'var(--surface-2)', color: 'var(--fg-1)',
            border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
          }}
        />
      )}
      <button onClick={onRemove} style={{ marginLeft: 'auto', background: 'transparent', border: 'none', color: 'var(--fg-3)', cursor: 'pointer' }} title="Remove">×</button>
    </div>
  );
}

function ReminderEditor({ reminders, onChange, hasDue, taskHasTime }) {
  const list = reminders || [];
  const update = (idx, next) => onChange(list.map((r, i) => i === idx ? next : r));
  const remove = idx => onChange(list.filter((_, i) => i !== idx));
  const add = () => onChange([...list, { kind: 'at_period', offset_days: 0, period: 'morning' }]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {!hasDue && (
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', fontStyle: 'italic' }}>
          Set a due date to add reminders.
        </div>
      )}
      {list.map((r, idx) => (
        <ReminderRow
          key={idx} reminder={r}
          onChange={next => update(idx, next)}
          onRemove={() => remove(idx)}
          hasDue={hasDue} taskHasTime={taskHasTime}
        />
      ))}
      <button
        onClick={add}
        disabled={!hasDue}
        style={{
          alignSelf: 'flex-start', fontSize: 'var(--t-cap)', padding: '4px 8px',
          background: 'transparent', color: hasDue ? 'var(--accent)' : 'var(--fg-3)',
          border: '1px dashed var(--hairline-strong)', borderRadius: 'var(--r-md)',
          cursor: hasDue ? 'pointer' : 'not-allowed', opacity: hasDue ? 1 : 0.5,
        }}
      >+ Add reminder</button>
    </div>
  );
}

// ── task card ─────────────────────────────────────────────────

function TaskCard({ task, onOpen, onPatch, onAgent }) {
  const due = dueRelative(task.due_date);
  const isMobile = useIsMobile();
  const nextStatus = TASK_NEXT_STATUS[task.status];
  return (
    <div
      onClick={() => onOpen(task)}
      draggable
      onDragStart={e => {
        e.dataTransfer.setData('text/task-id', task.issue_node_id);
        e.dataTransfer.effectAllowed = 'move';
      }}
      style={{
        background: 'var(--surface-1)', border: '1px solid var(--hairline)',
        borderRadius: 'var(--r-md)', padding: 10, cursor: 'grab',
        display: 'flex', flexDirection: 'column', gap: 6,
      }}
    >
      <div style={{ fontSize: 'var(--t-sm)', lineHeight: 1.3, color: 'var(--fg-1)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
        {task.title}
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
        {task.priority && <span style={priorityBadgeStyle(task.priority)}>{task.priority.replace(/-.*/, '')}</span>}
        {task.area_slug && <Pill>area:{task.area_slug}</Pill>}
        {task.proj_slug && <Pill>proj:{task.proj_slug}</Pill>}
        {task.category && <Pill>{task.category}</Pill>}
        {due && <Pill color={due.color} title={task.due_date}>{due.label}</Pill>}
        {task.reminders_count > 0 && <Pill title="Reminders set">🔔 {task.reminders_count}</Pill>}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} onClick={e => e.stopPropagation()}>
        {/* Desktop: DnD handles status moves (column drop zones in Kanban).   */}
        {/* Mobile: a single-tap "advance to next column" button replaces the */}
        {/* per-card status dropdown. Hidden in terminal states (Done /      */}
        {/* Archived) — use the detail panel to back out from there.         */}
        {isMobile && nextStatus && (
          <button
            onClick={() => onPatch(task.issue_node_id, { status: nextStatus })}
            title={`Move to "${nextStatus}"`}
            style={{
              fontSize: 'var(--t-xs)', padding: '3px 8px', borderRadius: 'var(--r-pill)',
              background: 'var(--surface-2)', color: 'var(--fg-1)',
              border: '1px solid var(--hairline)', cursor: 'pointer',
              display: 'inline-flex', alignItems: 'center', gap: 4,
            }}
          >→ {nextStatus}</button>
        )}
        {onAgent && (
          <button
            onClick={() => onAgent(task)}
            title="Discuss this task with an agent (reuses an existing chat if you've started one)"
            style={{
              fontSize: 'var(--t-xs)', padding: '2px 6px', borderRadius: 'var(--r-pill)',
              background: 'transparent', color: 'var(--accent)',
              border: '1px solid var(--accent)', cursor: 'pointer',
            }}
          >🤖</button>
        )}
        <a href={task.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-3)' }} onClick={e => e.stopPropagation()}>
          #{task.number}
        </a>
      </div>
    </div>
  );
}

// ── kanban ────────────────────────────────────────────────────

function TasksKanban({ tasks, config, presetStatuses, onOpen, onPatch, onAgent }) {
  // When `presetStatuses` is passed we render exactly those columns in that
  // order; otherwise fall back to the project's full Status field for the
  // legacy/full view.
  const statuses = presetStatuses && presetStatuses.length ? presetStatuses : (config?.status_options || []);
  const [dragOver, setDragOver] = useState(null);
  const { byStatus, noStatus } = useMemo(() => {
    const out = Object.fromEntries(statuses.map(s => [s, []]));
    const noSt = [];
    const known = new Set(statuses);
    for (const t of tasks) {
      if (t.status && known.has(t.status)) out[t.status].push(t);
      else noSt.push(t);
    }
    return { byStatus: out, noStatus: noSt };
  }, [tasks, statuses]);

  const columns = [
    ...statuses.map(s => ({ name: s, items: byStatus[s] || [], droppable: true })),
    ...(noStatus.length ? [{ name: '(no status)', items: noStatus, droppable: false }] : []),
  ];

  const handleDrop = (col) => (e) => {
    e.preventDefault();
    setDragOver(null);
    if (!col.droppable) return;
    const id = e.dataTransfer.getData('text/task-id');
    if (!id) return;
    const current = tasks.find(t => t.issue_node_id === id);
    if (!current || current.status === col.name) return;
    onPatch(id, { status: col.name });
  };

  return (
    <div style={{ display: 'flex', gap: 12, overflowX: 'auto', padding: '8px 0', flex: 1, minHeight: 0 }}>
      {columns.map(col => (
        <div
          key={col.name}
          onDragOver={col.droppable ? (e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setDragOver(col.name); }) : undefined}
          onDragLeave={() => setDragOver(prev => prev === col.name ? null : prev)}
          onDrop={handleDrop(col)}
          style={{
            minWidth: 260, maxWidth: 280, flex: '0 0 auto',
            background: 'var(--surface-2)', borderRadius: 'var(--r-lg)',
            padding: 8, display: 'flex', flexDirection: 'column', gap: 8,
            maxHeight: '100%',
            outline: dragOver === col.name ? '2px solid var(--accent)' : 'none',
            outlineOffset: -2,
            transition: 'outline-color .12s',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 4px' }}>
            <span style={{ fontWeight: 500, fontSize: 'var(--t-cap)', color: 'var(--fg-1)' }}>{col.name}</span>
            <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{col.items.length}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto', flex: 1 }}>
            {col.items.map(t => (
              <TaskCard key={t.issue_node_id} task={t} onOpen={onOpen} onPatch={onPatch} onAgent={onAgent} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── list ──────────────────────────────────────────────────────

function TasksList({ tasks, statusOptions, presetStatuses, onOpen, onPatch, onAgent }) {
  const visibleStatuses = presetStatuses && presetStatuses.length
    ? new Set(presetStatuses)
    : null;
  if (visibleStatuses) {
    tasks = tasks.filter(t => visibleStatuses.has(t.status));
  }
  if (tasks.length === 0) return null;
  return (
    <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--t-cap)' }}>
        <thead>
          <tr style={{ position: 'sticky', top: 0, background: 'var(--surface-1)', zIndex: 1 }}>
            {['Title','Status','Priority','Area','Project','Due','🔔','🤖'].map(h => (
              <th key={h} style={{ textAlign: 'left', padding: '8px 10px', borderBottom: '1px solid var(--hairline)', color: 'var(--fg-3)', fontWeight: 500 }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {tasks.map(t => {
            const due = dueRelative(t.due_date);
            return (
              <tr key={t.issue_node_id} onClick={() => onOpen(t)} style={{ cursor: 'pointer', borderBottom: '1px solid var(--hairline)' }}>
                <td style={{ padding: '8px 10px', color: 'var(--fg-1)' }}>{t.title}</td>
                <td style={{ padding: '8px 10px' }} onClick={e => e.stopPropagation()}>
                  <Select value={t.status || ''} onChange={v => onPatch(t.issue_node_id, { status: v })} options={statusOptions} placeholder="—" />
                </td>
                <td style={{ padding: '8px 10px' }}>
                  {t.priority ? <span style={priorityBadgeStyle(t.priority)}>{t.priority}</span> : <span style={{ color: 'var(--fg-3)' }}>—</span>}
                </td>
                <td style={{ padding: '8px 10px', color: 'var(--fg-2)' }}>{t.area_slug || '—'}</td>
                <td style={{ padding: '8px 10px', color: 'var(--fg-2)' }}>{t.proj_slug || '—'}</td>
                <td style={{ padding: '8px 10px', color: due?.color || 'var(--fg-3)' }}>{due?.label || '—'}</td>
                <td style={{ padding: '8px 10px', color: 'var(--fg-2)' }}>{t.reminders_count || ''}</td>
                <td style={{ padding: '8px 10px' }} onClick={e => e.stopPropagation()}>
                  {onAgent && (
                    <button
                      onClick={() => onAgent(t)}
                      title="Discuss this task with an agent"
                      style={{
                        fontSize: 'var(--t-xs)', padding: '2px 6px', borderRadius: 'var(--r-pill)',
                        background: 'transparent', color: 'var(--accent)',
                        border: '1px solid var(--accent)', cursor: 'pointer',
                      }}
                    >🤖</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── create form ───────────────────────────────────────────────

function TaskCreateForm({ open, onClose, config, areas, projects, onCreated, defaultStatus = 'Inbox' }) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [area, setArea] = useState('');
  const [proj, setProj] = useState('');
  const [status, setStatus] = useState(defaultStatus);
  const [priority, setPriority] = useState('');
  const [category, setCategory] = useState('');
  const [due, setDue] = useState('');
  const [dueTime, setDueTime] = useState('');
  const [reminders, setReminders] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!open) {
      setTitle(''); setBody(''); setArea(''); setProj(''); setStatus(defaultStatus);
      setPriority(''); setCategory(''); setDue(''); setDueTime(''); setReminders([]); setErr(null);
    }
  }, [open, defaultStatus]);

  // Auto-suggest reminders the first time due is set with empty list
  useEffect(() => {
    if (due && reminders.length === 0 && config?.reminder_defaults) {
      setReminders(config.reminder_defaults.map(r => ({ ...r })));
    }
  }, [due]); // eslint-disable-line

  const projOptions = useMemo(() => {
    if (!area) return projects;
    return projects.filter(p => !p.area_slug || p.area_slug === area);
  }, [area, projects]);

  // Hooks above; conditional return below.
  const Modal = window.Modal;
  if (!Modal) return null;

  const submit = async () => {
    if (!title.trim()) { setErr('Title is required'); return; }
    setSubmitting(true); setErr(null);
    try {
      const payload = {
        title: title.trim(), body,
        area_slug: area || null,
        proj_slug: proj || null,
        status: status || defaultStatus,
        priority: priority || null,
        category: category || null,
        due_date: due || null,
        due_time: dueTime || null,
        reminders: reminders.length ? reminders : null,
      };
      const r = await jsonFetch('/api/tasks', { method: 'POST', body: JSON.stringify(payload) });
      onCreated && onCreated(r.item);
      onClose();
    } catch (e) {
      setErr(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="New task" width={560}>
      <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {err && <StatusBanner variant="err" label={err} inline />}
        <Input autoFocus value={title} onChange={setTitle} placeholder="Title" size="md" />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <Select value={area} onChange={v => { setArea(v || ''); setProj(''); }} options={areas.map(a => ({ value: a.slug, label: a.label }))} placeholder="Area (or global)" />
          <Select value={proj} onChange={v => setProj(v || '')} options={projOptions.map(p => ({ value: p.slug, label: p.label }))} placeholder="Project (optional)" />
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
          <Select value={status} onChange={v => setStatus(v || defaultStatus)} options={config?.status_options || []} />
          <Select value={priority} onChange={v => setPriority(v || '')} options={config?.priority_options || []} placeholder="Priority" />
          <Select value={category} onChange={v => setCategory(v || '')} options={config?.category_options || []} placeholder="Category" />
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="date" value={due} onChange={e => setDue(e.target.value)} placeholder="Due date"
            style={{ flex: 1, background: 'var(--surface-2)', color: 'var(--fg-1)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit' }}
          />
          <input
            type="time" value={dueTime} onChange={e => setDueTime(e.target.value)} placeholder="optional"
            style={{ width: 110, background: 'var(--surface-2)', color: 'var(--fg-1)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit' }}
            title="Optional task hour (HH:MM) — enables legacy 'before X' reminders"
          />
        </div>
        <Textarea value={body} onChange={setBody} placeholder="Body (markdown)…" rows={4} mono={false} />
        <div>
          <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Reminders</div>
          <ReminderEditor reminders={reminders} onChange={setReminders} hasDue={!!due} taskHasTime={!!dueTime} />
        </div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 8 }}>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={submitting || !title.trim()} busy={submitting}>
            {submitting ? 'Creating…' : 'Create'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ── detail drawer ──────────────────────────────────────────────

function TaskDetailDrawer({ taskId, config, areas, projects, onClose, onChanged, onRemoved }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!taskId) { setDetail(null); return; }
    let alive = true;
    setLoading(true); setErr(null);
    jsonFetch(`/api/tasks/${encodeURIComponent(taskId)}`)
      .then(r => { if (alive) setDetail(r.item); })
      .catch(e => { if (alive) setErr(e.message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [taskId]);

  const patch = useCallback(async (body) => {
    if (!detail) return;
    setBusy(true); setErr(null);
    // Optimistic local merge so the editor doesn't flash back to the previous
    // state while the PATCH round-trips.
    if (body.reminders !== undefined) {
      setDetail(prev => prev ? { ...prev, reminders: body.reminders } : prev);
    }
    try {
      const r = await jsonFetch(`/api/tasks/${encodeURIComponent(detail.issue_node_id)}`, {
        method: 'PATCH', body: JSON.stringify(body),
      });
      if (r.item) {
        setDetail(prev => ({ ...prev, ...r.item }));
        onChanged && onChanged(r.item);
      }
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }, [detail, onChanged]);

  const projOptions = useMemo(() => {
    if (!detail) return projects;
    const a = detail.area_slug;
    if (!a) return projects;
    return projects.filter(p => !p.area_slug || p.area_slug === a);
  }, [detail?.area_slug, projects]);

  // Markdown → sanitized HTML is expensive (~50–200 ms on long bodies);
  // memoise on body text so every keystroke / saving-flag flip doesn't
  // re-parse. Mirrors TextBlock in orchestrator-blocks.jsx.
  const bodyHtml = useMemo(() => {
    if (!detail?.body || !window.marked || !window.DOMPurify) return null;
    return window.DOMPurify.sanitize(window.marked.parse(detail.body));
  }, [detail?.body]);

  // Hooks above; conditional returns below to keep hook count stable.
  if (!taskId) return null;
  const Modal = window.Modal;
  if (!Modal) return null;

  const body = (() => {
    if (loading) return <div style={{ padding: 24 }}><Spinner label="Loading…" /></div>;
    if (err) return <div style={{ padding: 16 }}><StatusBanner variant="err" label={err} /></div>;
    if (!detail) return null;
    const due = detail.due_date || '';
    const html = bodyHtml;
    return (
      <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <a href={detail.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>#{detail.number}</a>
          <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>{detail.repo}</span>
          {busy && <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>saving…</span>}
        </div>
        <textarea
          defaultValue={detail.title}
          rows={1}
          ref={el => { if (el) { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; } }}
          onInput={e => { e.target.style.height = 'auto'; e.target.style.height = e.target.scrollHeight + 'px'; }}
          onBlur={e => {
            const v = e.target.value.trim();
            if (v && v !== detail.title) patch({ title: v });
          }}
          style={{
            background: 'transparent', border: 'none',
            borderBottom: '1px solid var(--hairline)',
            padding: '6px 0', fontSize: 'var(--t-h3)', color: 'var(--fg-1)',
            fontFamily: 'inherit', resize: 'none', lineHeight: 1.3,
            width: '100%', overflow: 'hidden',
          }}
        />

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
          <Select value={detail.status || ''} onChange={v => patch({ status: v || null })} options={config?.status_options || []} placeholder="Status" />
          <Select value={detail.priority || ''} onChange={v => patch({ priority: v || '' })} options={config?.priority_options || []} placeholder="Priority" />
          <Select value={detail.category || ''} onChange={v => patch({ category: v || '' })} options={config?.category_options || []} placeholder="Category" />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <Select value={detail.area_slug || ''} onChange={v => patch({ area_slug: v || '' })} options={areas.map(a => ({ value: a.slug, label: a.label }))} placeholder="Area" />
          <Select value={detail.proj_slug || ''} onChange={v => patch({ proj_slug: v || '' })} options={projOptions.map(p => ({ value: p.slug, label: p.label }))} placeholder="Project" />
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <label style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>Due</label>
          <input
            type="date" defaultValue={due}
            onBlur={e => { if (e.target.value !== (detail.due_date || '')) patch({ due_date: e.target.value }); }}
            style={{ background: 'var(--surface-2)', color: 'var(--fg-1)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', padding: '6px 8px', fontSize: 'var(--t-cap)', fontFamily: 'inherit' }}
          />
          <input
            type="time" defaultValue={detail.due_time || ''}
            onBlur={e => { if (e.target.value !== (detail.due_time || '')) patch({ due_time: e.target.value }); }}
            title="Optional task hour (HH:MM)"
            style={{ width: 100, background: 'var(--surface-2)', color: 'var(--fg-1)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', padding: '6px 8px', fontSize: 'var(--t-cap)', fontFamily: 'inherit' }}
          />
        </div>

        <div>
          <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Reminders</div>
          <ReminderEditor
            reminders={detail.reminders || []}
            onChange={rs => patch({ reminders: rs })}
            hasDue={!!detail.due_date}
            taskHasTime={!!detail.due_time}
          />
        </div>

        {html && (
          <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-1)', borderTop: '1px solid var(--hairline)', paddingTop: 12 }}
               dangerouslySetInnerHTML={{ __html: html }} />
        )}

        <div style={{ display: 'flex', gap: 8, marginTop: 12, borderTop: '1px solid var(--hairline)', paddingTop: 12 }}>
          {(detail.state || 'OPEN').toUpperCase() === 'OPEN' ? (
            <>
              <Button variant="ghost"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await jsonFetch(`/api/tasks/${encodeURIComponent(detail.issue_node_id)}/close`, { method: 'POST' });
                    onRemoved && onRemoved(detail.issue_node_id);
                    onClose();
                  } catch (e) { setErr(e.message); } finally { setBusy(false); }
                }}
              >Mark done</Button>
              <Button variant="danger"
                disabled={busy}
                onClick={async () => {
                  if (!confirm('Close this task as not-planned (archive)?')) return;
                  setBusy(true);
                  try {
                    await jsonFetch(`/api/tasks/${encodeURIComponent(detail.issue_node_id)}?archive=true`, { method: 'DELETE' });
                    onRemoved && onRemoved(detail.issue_node_id);
                    onClose();
                  } catch (e) { setErr(e.message); } finally { setBusy(false); }
                }}
              >Archive</Button>
            </>
          ) : (
            <Button variant="ghost"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await jsonFetch(`/api/tasks/${encodeURIComponent(detail.issue_node_id)}/reopen`, { method: 'POST' });
                  setDetail(prev => ({ ...prev, state: 'OPEN' }));
                  onChanged && onChanged({ ...detail, state: 'OPEN' });
                } catch (e) { setErr(e.message); } finally { setBusy(false); }
              }}
            >Reopen</Button>
          )}
        </div>
      </div>
    );
  })();

  return (
    <Modal open={!!taskId} onClose={onClose} title="Task" width={560}>
      {body}
    </Modal>
  );
}

// ── toolbar ────────────────────────────────────────────────────

function TasksToolbar({ state, dispatch, onRefresh, onNew, loading, areas, projects, config }) {
  const f = state.filters;
  const [openFilter, setOpenFilter] = useState(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const isMobile = useIsMobile();

  // ── primitives reused by both desktop toolbar and mobile modal ──
  const searchInput = (
    <Input
      value={f.query}
      onChange={v => dispatch({ type: 'SET_FILTER', patch: { query: v } })}
      placeholder="Search…"
      icon="search"
      size="sm"
      style={{ width: isMobile ? '100%' : 160, minWidth: 0 }}
    />
  );

  const filterChips = (
    <>
      <FilterChipGroup id="status"   openId={openFilter} setOpenId={setOpenFilter} label="Status"   options={config?.status_options || []}   selected={f.status}   onChange={v => dispatch({ type: 'SET_FILTER', patch: { status: v } })} />
      <FilterChipGroup id="priority" openId={openFilter} setOpenId={setOpenFilter} label="Priority" options={config?.priority_options || []} selected={f.priority} onChange={v => dispatch({ type: 'SET_FILTER', patch: { priority: v } })} />
      <FilterChipGroup id="category" openId={openFilter} setOpenId={setOpenFilter} label="Category" options={config?.category_options || []} selected={f.category} onChange={v => dispatch({ type: 'SET_FILTER', patch: { category: v } })} />
      <FilterChipGroup
        id="area" openId={openFilter} setOpenId={setOpenFilter}
        label="Area" selected={f.area}
        options={[{ value: '', label: '(no area)' }, ...areas.map(a => ({ value: a.slug, label: a.label }))]}
        onChange={v => dispatch({ type: 'SET_FILTER', patch: { area: v } })}
      />
      <FilterChipGroup
        id="project" openId={openFilter} setOpenId={setOpenFilter}
        label="Project" selected={f.project}
        options={[{ value: '', label: '(no project)' }, ...projects.map(p => ({ value: p.slug, label: p.label }))]}
        onChange={v => dispatch({ type: 'SET_FILTER', patch: { project: v } })}
      />
      <FilterChipGroup
        id="due" openId={openFilter} setOpenId={setOpenFilter}
        label="Due" selected={f.dueBucket}
        options={[
          { value: 'overdue',   label: 'Overdue' },
          { value: 'today',     label: 'Today' },
          { value: 'this-week', label: 'This week' },
          { value: 'later',     label: 'Later' },
          { value: 'no-due',    label: 'No due date' },
        ]}
        onChange={v => dispatch({ type: 'SET_FILTER', patch: { dueBucket: v } })}
      />
      <Select
        value={f.state}
        onChange={v => dispatch({ type: 'SET_FILTER', patch: { state: v || 'open' } })}
        options={[
          { value: 'open',   label: 'Open' },
          { value: 'closed', label: 'Closed' },
          { value: 'all',    label: 'All' },
        ]}
      />
    </>
  );

  const presetSwitcher = (
    <div style={{ display: 'flex', gap: 4, alignItems: 'center', background: 'var(--surface-2)', borderRadius: 'var(--r-pill)', padding: 3 }} title="Preset workflow scope">
      {Object.entries(TASK_PRESETS).map(([k, p]) => (
        <button key={k} onClick={() => dispatch({ type: 'SET_PRESET', preset: k })} style={{
          padding: '4px 10px', fontSize: 'var(--t-cap)', borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer',
          background: state.preset === k ? 'var(--surface-1)' : 'transparent',
          color: state.preset === k ? 'var(--fg-1)' : 'var(--fg-3)',
        }}>{p.label}</button>
      ))}
    </div>
  );

  const inboxToggle = (
    <button
      onClick={() => dispatch({ type: 'TOGGLE_INBOX_COL' })}
      title="Overlay the Inbox column on the current preset"
      style={{
        padding: '4px 10px', fontSize: 'var(--t-cap)', borderRadius: 'var(--r-pill)',
        background: state.showInbox ? 'var(--accent-soft, rgba(129,140,248,.15))' : 'var(--surface-2)',
        color: state.showInbox ? 'var(--accent)' : 'var(--fg-3)',
        border: '1px solid ' + (state.showInbox ? 'var(--accent)' : 'var(--hairline)'),
        cursor: 'pointer', whiteSpace: 'nowrap',
      }}
    >+ Inbox</button>
  );

  const ideasToggle = (
    <button
      onClick={() => dispatch({ type: 'TOGGLE_IDEAS_COL' })}
      title="Overlay the Ideas column on the current preset"
      style={{
        padding: '4px 10px', fontSize: 'var(--t-cap)', borderRadius: 'var(--r-pill)',
        background: state.showIdeas ? 'var(--accent-soft, rgba(129,140,248,.15))' : 'var(--surface-2)',
        color: state.showIdeas ? 'var(--accent)' : 'var(--fg-3)',
        border: '1px solid ' + (state.showIdeas ? 'var(--accent)' : 'var(--hairline)'),
        cursor: 'pointer', whiteSpace: 'nowrap',
      }}
    >+ Ideas</button>
  );

  const viewSwitcher = (
    <div style={{ display: 'flex', gap: 4, alignItems: 'center', background: 'var(--surface-2)', borderRadius: 'var(--r-pill)', padding: 3 }}>
      {['kanban','list'].map(v => (
        <button key={v} onClick={() => dispatch({ type: 'SET_VIEW', view: v })} style={{
          padding: '4px 10px', fontSize: 'var(--t-cap)', borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer',
          background: state.view === v ? 'var(--surface-1)' : 'transparent',
          color: state.view === v ? 'var(--fg-1)' : 'var(--fg-3)', textTransform: 'capitalize',
        }}>{v}</button>
      ))}
    </div>
  );

  const refreshBtn = (
    <button onClick={onRefresh} disabled={loading === 'loading'} title="Refresh" style={{
      padding: '6px 10px', background: 'var(--surface-2)', color: 'var(--fg-2)',
      border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', cursor: 'pointer', fontSize: 'var(--t-cap)',
    }}>{loading === 'refreshing' ? '…' : '↻'}</button>
  );

  const newBtn = (
    <Button variant="primary" icon="plus" onClick={onNew} size="sm">New</Button>
  );

  // Mobile: collapse filters + presets + toggles + refresh behind a single
  // "⋯" button that opens a modal. Keeps only the essentials in the header
  // row: search, kanban/list toggle, the more-options trigger, and + New.
  if (isMobile) {
    return (
      <>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', borderBottom: '1px solid var(--hairline)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>{searchInput}</div>
          {viewSwitcher}
          <button
            onClick={() => setMoreOpen(true)}
            title="View options · filters, preset, columns"
            style={{
              padding: '6px 10px', background: 'var(--surface-2)', color: 'var(--fg-2)',
              border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
              cursor: 'pointer', fontSize: 'var(--t-md)', lineHeight: 1,
            }}
          >⋯</button>
          {newBtn}
        </div>
        <Modal open={moreOpen} onClose={() => setMoreOpen(false)} title="View options" width={420}>
          <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div>
              <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Filters</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                {filterChips}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Preset</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                {presetSwitcher}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Extra columns</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                {inboxToggle}
                {ideasToggle}
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>{refreshBtn}</div>
          </div>
        </Modal>
      </>
    );
  }

  // Desktop: original wrap-and-fill layout.
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', borderBottom: '1px solid var(--hairline)', flexWrap: 'wrap' }}>
      {searchInput}
      {filterChips}
      <div style={{ flex: 1 }} />
      {presetSwitcher}
      {inboxToggle}
      {ideasToggle}
      {viewSwitcher}
      {refreshBtn}
      {newBtn}
    </div>
  );
}

// ── reminders view ─────────────────────────────────────────────

function TasksSubTabs({ subTab, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 4, padding: '8px 12px 0', borderBottom: '1px solid var(--hairline)', background: 'var(--surface-1)' }}>
      {[{ key: 'tasks', label: 'Tasks' }, { key: 'reminders', label: 'Reminders' }].map(t => {
        const active = subTab === t.key;
        return (
          <button
            key={t.key}
            onClick={() => onChange(t.key)}
            style={{
              background: 'transparent',
              color: active ? 'var(--fg-1)' : 'var(--fg-3)',
              border: 'none',
              padding: '8px 14px',
              fontSize: 'var(--t-sm)',
              fontWeight: 500,
              cursor: 'pointer',
              borderBottom: active ? '2px solid var(--accent)' : '2px solid transparent',
              marginBottom: -1,
            }}
          >{t.label}</button>
        );
      })}
    </div>
  );
}

function groupRemindersByDate(reminders) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const order = ['Past', 'Today', 'Tomorrow', 'This week', 'Later', 'No date'];
  const groups = Object.fromEntries(order.map(k => [k, []]));
  for (const r of reminders) {
    if (!r.fire_at) { groups['No date'].push(r); continue; }
    let fa;
    try { fa = new Date(r.fire_at); } catch (_) { groups['No date'].push(r); continue; }
    const day = new Date(fa); day.setHours(0, 0, 0, 0);
    const diffDays = Math.round((day - today) / 86400000);
    if (diffDays < 0) groups['Past'].push(r);
    else if (diffDays === 0) groups['Today'].push(r);
    else if (diffDays === 1) groups['Tomorrow'].push(r);
    else if (diffDays <= 7) groups['This week'].push(r);
    else groups['Later'].push(r);
  }
  return order.filter(k => groups[k].length).map(k => ({ label: k, items: groups[k] }));
}

function ReminderListItem({ reminder, onRemove, onEdit }) {
  const r = reminder;
  const fired = !!r.fired_at;
  const dt = r.fire_at ? new Date(r.fire_at) : null;
  const dtLabel = dt && !isNaN(dt) ? dt.toLocaleString('pl-PL', { dateStyle: 'short', timeStyle: 'short' }) : '—';
  const firedLabel = fired ? new Date(r.fired_at).toLocaleString('pl-PL', { dateStyle: 'short', timeStyle: 'short' }) : null;
  const editable = r.kind === 'standalone' && !fired;
  return (
    <div
      onClick={editable ? () => onEdit(r) : undefined}
      style={{
        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
        padding: '8px 10px', borderBottom: '1px solid var(--hairline)',
        opacity: fired ? 0.65 : 1,
        cursor: editable ? 'pointer' : 'default',
      }}
      title={editable ? 'Click to edit' : undefined}
    >
      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', fontFamily: '"JetBrains Mono", monospace', minWidth: 130 }}>
        {dtLabel}
        {firedLabel && <span style={{ marginLeft: 6, color: 'var(--fg-3)' }}>· fired {firedLabel}</span>}
      </span>
      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-2)', minWidth: 100, background: 'var(--surface-2)', padding: '2px 6px', borderRadius: 4 }}>{r.spec_label}</span>
      <div style={{ flex: 1, minWidth: 200 }}>
        <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-1)' }}>{r.title}</div>
        {r.body && <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2 }}>{r.body.slice(0, 100)}{r.body.length > 100 ? '…' : ''}</div>}
      </div>
      {r.priority && <span style={priorityBadgeStyle(r.priority)}>{r.priority}</span>}
      {r.area_slug && <Pill>area:{r.area_slug}</Pill>}
      {r.proj_slug && <Pill>proj:{r.proj_slug}</Pill>}
      {r.task && r.task.url && (
        <a href={r.task.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }} onClick={e => e.stopPropagation()}>#{r.task.number}</a>
      )}
      {r.kind === 'standalone' && (
        <button
          onClick={e => { e.stopPropagation(); onRemove(); }}
          style={{ background: 'transparent', border: 'none', color: 'var(--fg-3)', cursor: 'pointer', fontSize: 'var(--t-h3)' }}
          title="Delete reminder"
        >×</button>
      )}
    </div>
  );
}

function ReminderCreateForm({ open, onClose, areas, projects, tasks, config, onCreated, existing }) {
  const Modal = window.Modal;
  const isEdit = !!existing;
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [fireDate, setFireDate] = useState('');
  const [fireTime, setFireTime] = useState('17:00');
  const [priority, setPriority] = useState('');
  const [area, setArea] = useState('');
  const [proj, setProj] = useState('');
  const [taskLink, setTaskLink] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!open) {
      setTitle(''); setBody(''); setPriority(''); setArea(''); setProj(''); setTaskLink(''); setErr(null);
      return;
    }
    if (existing) {
      setTitle(existing.title || '');
      setBody(existing.body || '');
      setPriority(existing.priority || '');
      setArea(existing.area_slug || '');
      setProj(existing.proj_slug || '');
      setTaskLink((existing.task && existing.task.issue_node_id) || '');
      if (existing.fire_at) {
        const dt = new Date(existing.fire_at);
        if (!isNaN(dt)) {
          const pad = n => String(n).padStart(2, '0');
          setFireDate(`${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`);
          setFireTime(`${pad(dt.getHours())}:${pad(dt.getMinutes())}`);
        }
      }
    } else {
      const d = new Date();
      setFireDate(d.toISOString().slice(0, 10));
      setFireTime('17:00');
    }
  }, [open, existing]);

  if (!Modal) return null;

  const quickPick = (offsetDays, time) => {
    const d = new Date();
    d.setDate(d.getDate() + offsetDays);
    setFireDate(d.toISOString().slice(0, 10));
    setFireTime(time);
  };

  const submit = async () => {
    if (!title.trim()) { setErr('Title is required'); return; }
    if (!fireDate || !fireTime) { setErr('When?'); return; }
    setSubmitting(true); setErr(null);
    try {
      const local = new Date(`${fireDate}T${fireTime}:00`);
      const offMin = -local.getTimezoneOffset();
      const sign = offMin >= 0 ? '+' : '-';
      const oh = String(Math.floor(Math.abs(offMin) / 60)).padStart(2, '0');
      const om = String(Math.abs(offMin) % 60).padStart(2, '0');
      const fire_at = `${fireDate}T${fireTime}:00${sign}${oh}:${om}`;
      const payload = {
        title: title.trim(),
        body: body || null,
        fire_at,
        priority: priority || null,
        area_slug: area || null,
        proj_slug: proj || null,
        task_link: taskLink || null,
      };
      let r;
      if (isEdit) {
        const rid = existing.id.split(':')[1];
        r = await jsonFetch(`/api/reminders/${rid}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        r = await jsonFetch('/api/reminders', { method: 'POST', body: JSON.stringify(payload) });
      }
      onCreated && onCreated(r);
      onClose();
    } catch (e) {
      setErr(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const inputStyle = {
    background: 'var(--surface-2)', color: 'var(--fg-1)',
    border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
    padding: '8px 10px', fontSize: 'var(--t-sm)', fontFamily: 'inherit',
  };
  const chipBtnStyle = {
    fontSize: 'var(--t-xs)', padding: '4px 8px', borderRadius: 'var(--r-pill)',
    background: 'var(--surface-2)', color: 'var(--fg-2)',
    border: '1px solid var(--hairline)', cursor: 'pointer',
  };

  const quickPicks = [
    ['Today 09:00', 0, '09:00'],
    ['Today 12:00', 0, '12:00'],
    ['Today 15:00', 0, '15:00'],
    ['Today 21:00', 0, '21:00'],
    ['Tomorrow 09:00', 1, '09:00'],
    ['Tomorrow 15:00', 1, '15:00'],
  ];

  return (
    <Modal open={open} onClose={onClose} title={isEdit ? "Edit reminder" : "New reminder"} width={520}>
      <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {err && <StatusBanner variant="err" label={err} inline />}
        <Input autoFocus value={title} onChange={setTitle} placeholder="What to remind?" size="md" />
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
          <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginRight: 4 }}>Quick:</span>
          {quickPicks.map(([label, off, t]) => (
            <button key={label} onClick={() => quickPick(off, t)} style={chipBtnStyle}>{label}</button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <input type="date" value={fireDate} onChange={e => setFireDate(e.target.value)} style={{ ...inputStyle, flex: 1 }} />
          <input type="time" value={fireTime} onChange={e => setFireTime(e.target.value)} style={{ ...inputStyle, width: 120 }} />
        </div>
        <Textarea value={body} onChange={setBody} rows={2} placeholder="Body (optional)…" mono={false} />
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <Select value={priority} onChange={v => setPriority(v || '')} options={config?.priority_options || []} placeholder="Priority" />
          <Select value={area} onChange={v => setArea(v || '')} options={areas.map(a => ({ value: a.slug, label: a.label }))} placeholder="Area" />
        </div>
        <Select value={proj} onChange={v => setProj(v || '')} options={projects.map(p => ({ value: p.slug, label: p.label }))} placeholder="Project (optional)" />
        <Select
          value={taskLink}
          onChange={v => setTaskLink(v || '')}
          options={(tasks || []).slice(0, 100).map(t => ({ value: t.issue_node_id, label: `#${t.number} ${(t.title || '').slice(0, 50)}` }))}
          placeholder="Link to task (optional)"
        />
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 }}>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={submitting || !title.trim()} busy={submitting}>
            {submitting ? (isEdit ? 'Saving…' : 'Creating…') : (isEdit ? 'Save' : 'Create')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function RemindersToolbar({ filters, setFilters, openFilter, setOpenFilter, loading, onRefresh, onNew, areas, projects }) {
  const [moreOpen, setMoreOpen] = useState(false);
  const isMobile = useIsMobile();
  const inputStyle = {
    background: 'var(--surface-2)', color: 'var(--fg-1)',
    border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
    padding: '6px 10px', fontSize: 'var(--t-cap)', fontFamily: 'inherit',
  };

  const searchInput = (
    <Input
      value={filters.query}
      onChange={v => setFilters(f => ({ ...f, query: v }))}
      placeholder="Search…"
      icon="search"
      size="sm"
      style={{ width: isMobile ? '100%' : 150, minWidth: 0 }}
    />
  );

  const stateToggle = (
    <div style={{ display: 'flex', background: 'var(--surface-2)', borderRadius: 'var(--r-pill)', padding: 3 }}>
      {[['pending', 'Pending'], ['fired', 'History'], ['all', 'All']].map(([v, l]) => (
        <button key={v} onClick={() => setFilters(f => ({ ...f, state: v }))} style={{
          padding: '4px 10px', fontSize: 'var(--t-cap)', borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer',
          background: filters.state === v ? 'var(--surface-1)' : 'transparent',
          color: filters.state === v ? 'var(--fg-1)' : 'var(--fg-3)',
        }}>{l}</button>
      ))}
    </div>
  );

  const windowAndKindSelects = (
    <>
      <Select
        value={filters.within}
        onChange={v => setFilters(f => ({ ...f, within: v || '7d' }))}
        options={[{ value: '24h', label: 'Within 24h' }, { value: '7d', label: 'Within 7 days' }, { value: '30d', label: 'Within 30 days' }, { value: 'all', label: 'All time' }]}
      />
      <Select
        value={filters.kind}
        onChange={v => setFilters(f => ({ ...f, kind: v || 'all' }))}
        options={[{ value: 'all', label: 'All kinds' }, { value: 'task', label: 'Task-attached' }, { value: 'standalone', label: 'Standalone' }]}
      />
    </>
  );

  const scopeChips = (
    <>
      <FilterChipGroup id="rem-area" openId={openFilter} setOpenId={setOpenFilter} label="Area" selected={filters.area} options={areas.map(a => ({ value: a.slug, label: a.label }))} onChange={v => setFilters(f => ({ ...f, area: v }))} />
      <FilterChipGroup id="rem-project" openId={openFilter} setOpenId={setOpenFilter} label="Project" selected={filters.project} options={projects.map(p => ({ value: p.slug, label: p.label }))} onChange={v => setFilters(f => ({ ...f, project: v }))} />
    </>
  );

  const refreshBtn = (
    <button onClick={onRefresh} disabled={loading === 'loading'} title="Refresh" style={{
      padding: '6px 10px', background: 'var(--surface-2)', color: 'var(--fg-2)',
      border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', cursor: 'pointer', fontSize: 'var(--t-cap)',
    }}>{loading === 'refreshing' ? '…' : '↻'}</button>
  );

  const newBtn = (
    <Button variant="primary" icon="plus" onClick={onNew} size="sm" style={{ whiteSpace: 'nowrap' }}>New</Button>
  );

  if (isMobile) {
    return (
      <>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', borderBottom: '1px solid var(--hairline)' }}>
          <div style={{ flex: 1, minWidth: 0 }}>{searchInput}</div>
          {stateToggle}
          <button
            onClick={() => setMoreOpen(true)}
            title="View options · time window, kind, scope"
            style={{
              padding: '6px 10px', background: 'var(--surface-2)', color: 'var(--fg-2)',
              border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)',
              cursor: 'pointer', fontSize: 'var(--t-md)', lineHeight: 1,
            }}
          >⋯</button>
          {newBtn}
        </div>
        <Modal open={moreOpen} onClose={() => setMoreOpen(false)} title="View options" width={420}>
          <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div>
              <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Window &amp; kind</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                {windowAndKindSelects}
              </div>
            </div>
            <div>
              <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginBottom: 6 }}>Scope</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                {scopeChips}
              </div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>{refreshBtn}</div>
          </div>
        </Modal>
      </>
    );
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', borderBottom: '1px solid var(--hairline)', flexWrap: 'wrap' }}>
      {searchInput}
      {stateToggle}
      {windowAndKindSelects}
      {scopeChips}
      <div style={{ flex: 1 }} />
      {refreshBtn}
      <Button variant="primary" icon="plus" onClick={onNew} size="sm">New reminder</Button>
    </div>
  );
}

function RemindersView({ areas, projects, tasks, config }) {
  const [reminders, setReminders] = useState([]);
  const [loading, setLoading] = useState('loading');
  const [error, setError] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editingRem, setEditingRem] = useState(null);
  const [filters, setFilters] = useState({
    state: 'pending', within: '7d', kind: 'all', area: [], project: [], query: '',
  });
  const [openFilter, setOpenFilter] = useState(null);

  const load = useCallback(async () => {
    setLoading(prev => reminders.length ? 'refreshing' : 'loading');
    setError(null);
    const params = new URLSearchParams();
    params.set('state', filters.state);
    params.set('within', filters.within);
    params.set('kind', filters.kind);
    (filters.area || []).forEach(a => params.append('area', a));
    (filters.project || []).forEach(p => params.append('project', p));
    if (filters.query) params.set('q', filters.query);
    try {
      const r = await jsonFetch('/api/reminders?' + params.toString());
      setReminders(r.reminders || []);
      setLoading('idle');
    } catch (e) {
      setError(e.message);
      setLoading('error');
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.state, filters.within, filters.kind, filters.area, filters.project, filters.query]);

  useEffect(() => { load(); }, [load]);

  const groups = useMemo(() => groupRemindersByDate(reminders), [reminders]);

  const remove = async (rem) => {
    if (rem.kind !== 'standalone') return;
    const rid = rem.id.split(':')[1];
    if (!confirm(`Delete reminder "${rem.title}"?`)) return;
    try {
      await jsonFetch(`/api/reminders/${rid}`, { method: 'DELETE' });
      setReminders(prev => prev.filter(r => r.id !== rem.id));
    } catch (e) {
      alert(`Delete failed: ${e.message}`);
    }
  };

  let body;
  if (loading === 'loading' && reminders.length === 0) {
    body = (
      <div style={{ flex: 1, padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        {Array.from({ length: 8 }).map((_, i) => <div key={i} className="skel" style={{ height: 38 }} />)}
      </div>
    );
  } else if (loading === 'error' && reminders.length === 0) {
    body = <div style={{ padding: 24 }}><StatusBanner variant="err" label={`Failed: ${error}`} action={<Button variant="ghost" size="sm" onClick={load}>Retry</Button>} /></div>;
  } else if (reminders.length === 0) {
    body = (
      <div style={{ margin: 'auto', padding: 24, color: 'var(--fg-3)', fontSize: 'var(--t-sm)', textAlign: 'center' }}>
        Nothing matches these filters.
        <div style={{ marginTop: 8 }}>
          <button onClick={() => setCreateOpen(true)} style={{ color: 'var(--accent)', background: 'transparent', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}>Create one</button>
        </div>
      </div>
    );
  } else {
    body = (
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 12px 24px' }}>
        {groups.map(g => (
          <div key={g.label}>
            <h3 style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', textTransform: 'uppercase', margin: '12px 0 6px', letterSpacing: 0.6, fontWeight: 500 }}>{g.label} · {g.items.length}</h3>
            {g.items.map(r => <ReminderListItem key={r.id} reminder={r} onRemove={() => remove(r)} onEdit={setEditingRem} />)}
          </div>
        ))}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <RemindersToolbar
        filters={filters} setFilters={setFilters}
        openFilter={openFilter} setOpenFilter={setOpenFilter}
        loading={loading} onRefresh={load} onNew={() => setCreateOpen(true)}
        areas={areas} projects={projects}
      />
      {body}
      <ReminderCreateForm
        open={createOpen} onClose={() => setCreateOpen(false)}
        areas={areas} projects={projects} tasks={tasks} config={config}
        onCreated={() => load()}
      />
      <ReminderCreateForm
        open={!!editingRem} onClose={() => setEditingRem(null)}
        areas={areas} projects={projects} tasks={tasks} config={config}
        existing={editingRem}
        onCreated={() => load()}
      />
    </div>
  );
}

// ── skeletons (initial load) ──────────────────────────────────

function TasksSkeleton({ view }) {
  if (view === 'list') {
    return (
      <div style={{ flex: 1, padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 8, overflow: 'hidden' }}>
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="skel" style={{ height: 38 }} />
        ))}
      </div>
    );
  }
  // Kanban skeleton: 5 columns, 3 cards each — matches Sync's faded card grid.
  return (
    <div style={{ flex: 1, padding: '8px 12px', display: 'flex', gap: 12, overflowX: 'auto', overflowY: 'hidden' }}>
      {Array.from({ length: 5 }).map((_, c) => (
        <div key={c} style={{
          minWidth: 260, maxWidth: 280, flex: '0 0 auto',
          background: 'var(--surface-2)', borderRadius: 'var(--r-lg)', padding: 8,
          display: 'flex', flexDirection: 'column', gap: 8,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 4px' }}>
            <div className="skel" style={{ height: 12, width: 90 }} />
            <div className="skel" style={{ height: 12, width: 22 }} />
          </div>
          {Array.from({ length: 3 + (c % 2) }).map((_, i) => (
            <div key={i} className="skel" style={{ height: 78 + (i % 2) * 12, borderRadius: 'var(--r-control)' }} />
          ))}
        </div>
      ))}
    </div>
  );
}

// ── main view ──────────────────────────────────────────────────

function TasksView() {
  const initialView = (() => {
    try { return localStorage.getItem(TASK_STATE_KEY) || INITIAL_STATE.view; } catch (_) { return INITIAL_STATE.view; }
  })();
  const initialPreset = (() => {
    try {
      const p = localStorage.getItem(TASK_PRESET_KEY);
      return (p && TASK_PRESETS[p]) ? p : INITIAL_STATE.preset;
    } catch (_) { return INITIAL_STATE.preset; }
  })();
  const initialShowInbox = (() => {
    try { return localStorage.getItem(TASK_INBOX_COL_KEY) === '1'; } catch (_) { return false; }
  })();
  const initialShowIdeas = (() => {
    try { return localStorage.getItem(TASK_IDEAS_COL_KEY) === '1'; } catch (_) { return false; }
  })();
  const [state, dispatch] = useReducer(tasksReducer, {
    ...INITIAL_STATE,
    view: initialView, preset: initialPreset,
    showInbox: initialShowInbox, showIdeas: initialShowIdeas,
  });
  // Effective columns = optional Inbox/Ideas overlay + the preset's own statuses.
  const presetStatuses = useMemo(() => {
    const base = TASK_PRESETS[state.preset]?.statuses || TASK_PRESETS.full.statuses;
    const out = [];
    if (state.showInbox && !base.includes('Inbox')) out.push('Inbox');
    if (state.showIdeas && !base.includes('Ideas')) out.push('Ideas');
    return [...out, ...base];
  }, [state.preset, state.showInbox, state.showIdeas]);

  const load = useCallback(async (opts = {}) => {
    dispatch({ type: 'LOAD_BEGIN' });
    try {
      const [list, cfg] = await Promise.all([
        jsonFetch('/api/tasks?state=' + encodeURIComponent(opts.state || state.filters.state || 'open') + '&limit=500'),
        jsonFetch('/api/tasks/config'),
      ]);
      dispatch({
        type: 'LOAD_OK',
        tasks: list.items || [],
        config: cfg.config || null,
        areas: cfg.config?.areas || [],
        projects: cfg.config?.projects || [],
      });
    } catch (e) {
      dispatch({ type: 'LOAD_ERR', error: e.message || String(e) });
    }
  }, [state.filters.state]);

  useEffect(() => { load(); }, []);  // eslint-disable-line
  useEffect(() => { load({ state: state.filters.state }); }, [state.filters.state]); // eslint-disable-line

  const refresh = useCallback(async () => {
    try {
      const r = await jsonFetch('/api/tasks/refresh', { method: 'POST' });
      dispatch({ type: 'LOAD_OK', tasks: r.items || [] });
    } catch (e) {
      dispatch({ type: 'LOAD_ERR', error: e.message || String(e) });
    }
  }, []);

  const patchTask = useCallback(async (id, patch) => {
    const prev = state.tasks.find(t => t.issue_node_id === id);
    if (!prev) return;
    dispatch({ type: 'PATCH_ITEM', id, patch });
    try {
      const r = await jsonFetch(`/api/tasks/${encodeURIComponent(id)}`, {
        method: 'PATCH', body: JSON.stringify(patch),
      });
      if (r.item) dispatch({ type: 'REPLACE_ITEM', id, item: r.item });
    } catch (e) {
      dispatch({ type: 'REPLACE_ITEM', id, item: prev });
      console.error('PATCH failed:', e);
      // best-effort toast — fall back to alert if useToast wasn't wired
      try {
        const fn = window.__hubToast;
        if (fn) fn(`Update failed: ${e.message}`, 'err');
        else console.warn('Update failed:', e.message);
      } catch (_) {}
    }
  }, [state.tasks]);

  // Apply preset's status whitelist BEFORE the user-driven filter chips so the
  // toolbar's "0 results" path doesn't lie about why something's hidden.
  const presetSet = useMemo(() => new Set(presetStatuses), [presetStatuses]);
  const presetFiltered = useMemo(
    () => state.tasks.filter(t => presetSet.has(t.status)),
    [state.tasks, presetSet],
  );
  const filtered = useMemo(
    () => applyFilters(presetFiltered, state.filters),
    [presetFiltered, state.filters],
  );

  const handleAgent = useCallback(async (task) => {
    try {
      await findOrCreateTaskSession(task, state.areas, state.projects);
    } catch (e) {
      alert(`Agent session failed: ${e.message || e}`);
    }
  }, [state.areas, state.projects]);

  // Initial load — toolbar visible (disabled), shimmering skeleton below.
  if (state.loading === 'loading' && state.tasks.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }} className="fade-in">
        <TasksToolbar
          state={state} dispatch={dispatch}
          onRefresh={() => {}} onNew={() => {}}
          loading="loading"
          areas={[]} projects={[]} config={null}
        />
        <TasksSkeleton view={state.view} />
      </div>
    );
  }
  if (state.loading === 'error' && state.tasks.length === 0) {
    return (
      <div style={{ padding: 24 }}>
        <StatusBanner variant="err" label={`Failed to load tasks: ${state.error}`} action={<Button variant="ghost" size="sm" onClick={() => load()}>Retry</Button>} />
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <TasksToolbar
        state={state} dispatch={dispatch}
        onRefresh={refresh}
        onNew={() => dispatch({ type: 'OPEN_CREATE' })}
        loading={state.loading}
        areas={state.areas} projects={state.projects} config={state.config}
      />
      <div style={{ flex: 1, minHeight: 0, padding: '8px 12px', overflow: 'hidden', display: 'flex' }}>
        {filtered.length === 0 ? (
          <div style={{ margin: 'auto', color: 'var(--fg-3)', fontSize: 'var(--t-sm)', textAlign: 'center' }}>
            {presetFiltered.length === 0
              ? <>Nothing in this preset ({TASK_PRESETS[state.preset].label}). <button onClick={() => dispatch({ type: 'OPEN_CREATE' })} style={{ color: 'var(--accent)', background: 'transparent', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}>Create a task.</button></>
              : <>Nothing matches these filters. <button onClick={() => dispatch({ type: 'CLEAR_FILTERS' })} style={{ color: 'var(--accent)', background: 'transparent', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}>Clear filters.</button></>
            }
          </div>
        ) : state.view === 'kanban' ? (
          <TasksKanban
            tasks={filtered}
            config={state.config}
            presetStatuses={presetStatuses}
            onOpen={t => dispatch({ type: 'OPEN_TASK', id: t.issue_node_id })}
            onPatch={patchTask}
            onAgent={handleAgent}
          />
        ) : (
          <TasksList
            tasks={filtered}
            statusOptions={state.config?.status_options || []}
            presetStatuses={presetStatuses}
            onOpen={t => dispatch({ type: 'OPEN_TASK', id: t.issue_node_id })}
            onPatch={patchTask}
            onAgent={handleAgent}
          />
        )}
      </div>

      <TaskDetailDrawer
        taskId={state.selectedId}
        config={state.config}
        areas={state.areas}
        projects={state.projects}
        onClose={() => dispatch({ type: 'CLOSE_DRAWER' })}
        onChanged={item => dispatch({ type: 'REPLACE_ITEM', id: item.issue_node_id, item })}
        onRemoved={id => dispatch({ type: 'REMOVE_ITEM', id })}
      />

      <TaskCreateForm
        open={state.createOpen}
        onClose={() => dispatch({ type: 'CLOSE_CREATE' })}
        config={state.config}
        areas={state.areas}
        projects={state.projects}
        onCreated={item => item && dispatch({ type: 'INSERT_ITEM', item })}
      />
    </div>
  );
}

// ── status-filtered views (Inbox / Ideas / Archive) ─────────────

function StatusListRow({ task, quickActions, rowAgentBtn, onAction, onAgent, onOpen }) {
  const due = dueRelative(task.due_date);
  // Track which action is currently in-flight so we can disable the whole
  // row + show a spinner where it was tapped. GH writes can take 1-2s; without
  // this the user re-taps thinking nothing happened, or worse, second-guesses
  // their first tap mid-roundtrip.
  const [busyIdx, setBusyIdx] = useState(null);
  const busy = busyIdx !== null;

  const runAction = async (a, idx) => {
    if (busy) return;
    if (a.confirm) {
      const msg = typeof a.confirm === 'function' ? a.confirm(task) : a.confirm;
      if (!window.confirm(msg)) return;
    }
    setBusyIdx(idx);
    try {
      await onAction(a);
    } finally {
      setBusyIdx(null);
    }
  };

  return (
    <div
      onClick={busy ? undefined : onOpen}
      style={{
        display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
        padding: '10px 12px', borderBottom: '1px solid var(--hairline)',
        cursor: busy ? 'wait' : 'pointer',
        opacity: busy ? 0.6 : 1,
        transition: 'opacity .15s',
      }}
    >
      <div style={{ flex: 1, minWidth: 200 }}>
        <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-1)' }}>{task.title}</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4, alignItems: 'center' }}>
          {task.priority && <span style={priorityBadgeStyle(task.priority)}>{task.priority}</span>}
          {task.area_slug && <Pill>area:{task.area_slug}</Pill>}
          {task.proj_slug && <Pill>proj:{task.proj_slug}</Pill>}
          {task.category && <Pill>{task.category}</Pill>}
          {due && <Pill color={due.color}>{due.label}</Pill>}
          {task.reminders_count > 0 && <Pill>🔔 {task.reminders_count}</Pill>}
        </div>
      </div>
      <div onClick={e => e.stopPropagation()} style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
        {(quickActions || []).map((a, i) => {
          const isBusy = busyIdx === i;
          const accent = a.danger
            ? { bg: 'rgba(248,113,113,.12)', fg: 'var(--danger)', border: 'var(--danger)' }
            : a.accent
              ? { bg: 'var(--accent-soft, rgba(99,102,241,.12))', fg: 'var(--accent)', border: 'var(--hairline)' }
              : { bg: 'var(--surface-2)', fg: 'var(--fg-2)', border: 'var(--hairline)' };
          return (
            <button
              key={i}
              onClick={() => runAction(a, i)}
              disabled={busy}
              style={{
                fontSize: 'var(--t-xs)', padding: '4px 8px', borderRadius: 'var(--r-pill)',
                background: accent.bg, color: accent.fg,
                border: '1px solid ' + accent.border,
                cursor: busy ? 'wait' : 'pointer', whiteSpace: 'nowrap',
                minWidth: isBusy ? 50 : undefined,
              }}
              title={a.title || a.label}
            >{isBusy ? '…' : a.label}</button>
          );
        })}
        {rowAgentBtn && (
          <button
            onClick={onAgent}
            disabled={busy}
            style={{
              fontSize: 'var(--t-xs)', padding: '4px 8px', borderRadius: 'var(--r-pill)',
              background: 'transparent', color: 'var(--accent)',
              border: '1px solid var(--accent)',
              cursor: busy ? 'wait' : 'pointer', whiteSpace: 'nowrap',
            }}
            title="Open new agent chat about this item"
          >🤖 {rowAgentBtn.label}</button>
        )}
      </div>
    </div>
  );
}

function StatusListView({
  status,
  title,
  subtitle,
  emptyText,
  globalAgentBtn,
  quickActions,
  rowAgentBtn,
  sortable = false,
  defaultSort = 'date',
}) {
  const [tasks, setTasks] = useState([]);
  const [config, setConfig] = useState(null);
  const [areas, setAreas] = useState([]);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState('loading');
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [sortBy, setSortBy] = useState(defaultSort);
  const [createOpen, setCreateOpen] = useState(false);

  const sortedTasks = useMemo(() => {
    if (!sortable) return tasks;
    const arr = tasks.slice();
    if (sortBy === 'name') {
      arr.sort((a, b) => (a.title || '').localeCompare(b.title || '', 'pl'));
    } else {
      // 'date' → newest-first by created_at, fallback updated_at
      arr.sort((a, b) => {
        const ka = a.created_at || a.updated_at || '';
        const kb = b.created_at || b.updated_at || '';
        return kb.localeCompare(ka);
      });
    }
    return arr;
  }, [tasks, sortBy, sortable]);

  const load = useCallback(async () => {
    setLoading(prev => (prev === 'loading' ? 'loading' : 'refreshing'));
    setError(null);
    try {
      const [list, cfg] = await Promise.all([
        jsonFetch(`/api/tasks?state=all&limit=500&status=${encodeURIComponent(status)}`),
        jsonFetch('/api/tasks/config'),
      ]);
      setTasks(list.items || []);
      const c = cfg.config || null;
      setConfig(c);
      setAreas(c?.areas || []);
      setProjects(c?.projects || []);
      setLoading('idle');
    } catch (e) {
      setError(e.message || String(e));
      setLoading('error');
    }
  }, [status]);

  useEffect(() => { load(); }, [load]);

  const applyAction = async (task, action) => {
    const id = task.issue_node_id;
    try {
      if (action.kind === 'set-status') {
        await jsonFetch(`/api/tasks/${encodeURIComponent(id)}`, {
          method: 'PATCH', body: JSON.stringify({ status: action.value }),
        });
        // Out of this view's status window now.
        setTasks(prev => prev.filter(t => t.issue_node_id !== id));
      } else if (action.kind === 'set-priority') {
        const r = await jsonFetch(`/api/tasks/${encodeURIComponent(id)}`, {
          method: 'PATCH', body: JSON.stringify({ priority: action.value }),
        });
        if (r.item) setTasks(prev => prev.map(t => t.issue_node_id === id ? r.item : t));
      } else if (action.kind === 'close') {
        await jsonFetch(`/api/tasks/${encodeURIComponent(id)}/close?reason=${action.value || 'not_planned'}`, { method: 'POST' });
        setTasks(prev => prev.filter(t => t.issue_node_id !== id));
      } else if (action.kind === 'archive') {
        await jsonFetch(`/api/tasks/${encodeURIComponent(id)}?archive=true`, { method: 'DELETE' });
        setTasks(prev => prev.filter(t => t.issue_node_id !== id));
      } else if (action.kind === 'reopen') {
        await jsonFetch(`/api/tasks/${encodeURIComponent(id)}/reopen`, { method: 'POST' });
        setTasks(prev => prev.filter(t => t.issue_node_id !== id));
      }
    } catch (e) {
      alert(`Action failed: ${e.message || e}`);
    }
  };

  const handleAgent = async (task) => {
    if (!rowAgentBtn) return;
    try {
      // Always go through findOrCreateTaskSession so Inbox/Ideas "Discuss" /
      // "Brainstorm" returns to an existing chat on subsequent clicks instead
      // of spawning duplicate sessions.
      await findOrCreateTaskSession(task, areas, projects, {
        title: rowAgentBtn.titleFor ? rowAgentBtn.titleFor(task) : `Discuss: ${task.title}`,
        prompt: rowAgentBtn.makePrompt(task),
      });
    } catch (e) {
      alert(`Agent session failed: ${e.message || e}`);
    }
  };

  const handleGlobalAgent = async () => {
    if (!globalAgentBtn) return;
    try {
      await launchAgentSession({ title: globalAgentBtn.title, prompt: globalAgentBtn.prompt });
    } catch (e) {
      alert(`Agent session failed: ${e.message || e}`);
    }
  };

  const header = (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', borderBottom: '1px solid var(--hairline)' }}>
      <div>
        <div style={{ fontSize: 'var(--t-h3)', fontWeight: 500, color: 'var(--fg-1)' }}>{title}</div>
        {subtitle && <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)', marginTop: 2 }}>{subtitle}</div>}
      </div>
      <div style={{ flex: 1 }} />
      {sortable && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>Sort</span>
          <div style={{ display: 'flex', background: 'var(--surface-2)', borderRadius: 'var(--r-pill)', padding: 3 }}>
            {[['date', 'Date'], ['name', 'Name']].map(([v, l]) => (
              <button key={v} onClick={() => setSortBy(v)} style={{
                padding: '4px 10px', fontSize: 'var(--t-xs)', borderRadius: 'var(--r-pill)', border: 'none', cursor: 'pointer',
                background: sortBy === v ? 'var(--surface-1)' : 'transparent',
                color: sortBy === v ? 'var(--fg-1)' : 'var(--fg-3)',
              }}>{l}</button>
            ))}
          </div>
        </div>
      )}
      <button
        onClick={load} disabled={loading === 'loading'} title="Refresh"
        style={{
          padding: '6px 10px', background: 'var(--surface-2)', color: 'var(--fg-2)',
          border: '1px solid var(--hairline)', borderRadius: 'var(--r-md)', cursor: 'pointer', fontSize: 'var(--t-cap)',
        }}
      >{loading === 'refreshing' ? '…' : '↻'}</button>
      <Button variant="primary" icon="plus" size="sm" onClick={() => setCreateOpen(true)} style={{ whiteSpace: 'nowrap' }}>New</Button>
      {globalAgentBtn && (
        <Button variant="primary" size="sm"
          onClick={handleGlobalAgent}
          title={globalAgentBtn.prompt.slice(0, 200) + (globalAgentBtn.prompt.length > 200 ? '…' : '')}
        >{globalAgentBtn.hideIcon ? '' : '🤖 '}{globalAgentBtn.label}</Button>
      )}
    </div>
  );

  if (loading === 'loading' && tasks.length === 0) {
    return (
      <div className="fade-in" style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        {header}
        <div style={{ flex: 1, padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: 6 }}>
          {Array.from({ length: 6 }).map((_, i) => <div key={i} className="skel" style={{ height: 48 }} />)}
        </div>
      </div>
    );
  }
  if (loading === 'error' && tasks.length === 0) {
    return (
      <div className="fade-in" style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        {header}
        <div style={{ padding: 24 }}>
          <StatusBanner variant="err" label={`Failed: ${error}`} action={<Button variant="ghost" size="sm" onClick={load}>Retry</Button>} />
        </div>
      </div>
    );
  }

  return (
    <div className="fade-in" style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      {header}
      {tasks.length === 0 ? (
        <div style={{ margin: 'auto', padding: 24, color: 'var(--fg-3)', fontSize: 'var(--t-sm)', textAlign: 'center' }}>
          {emptyText || `Nothing in ${title}.`}
        </div>
      ) : (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {sortedTasks.map(t => (
            <StatusListRow
              key={t.issue_node_id}
              task={t}
              quickActions={quickActions}
              rowAgentBtn={rowAgentBtn}
              onAction={(a) => applyAction(t, a)}
              onAgent={() => handleAgent(t)}
              onOpen={() => setSelectedId(t.issue_node_id)}
            />
          ))}
        </div>
      )}
      <TaskDetailDrawer
        taskId={selectedId}
        config={config}
        areas={areas}
        projects={projects}
        onClose={() => setSelectedId(null)}
        onChanged={item => {
          if (item.status === status) {
            setTasks(prev => prev.map(t => t.issue_node_id === item.issue_node_id ? item : t));
          } else {
            // Status moved out of this view
            setTasks(prev => prev.filter(t => t.issue_node_id !== item.issue_node_id));
          }
        }}
        onRemoved={id => setTasks(prev => prev.filter(t => t.issue_node_id !== id))}
      />
      <TaskCreateForm
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        config={config}
        areas={areas}
        projects={projects}
        defaultStatus={status}
        onCreated={item => {
          // Only inject if it actually landed in this view's status window —
          // the form lets the user override the prefilled status, so a "+ New"
          // from Inbox could end up in Backlog and shouldn't show up here.
          if (item && item.status === status) {
            setTasks(prev => [item, ...prev]);
          }
        }}
      />
    </div>
  );
}

function InboxView() {
  return (
    <StatusListView
      status="Inbox"
      title="Inbox"
      emptyText="Inbox is empty. Brain-dump straight into Tasks → New, or have an agent push items here."
      globalAgentBtn={{
        label: 'Triage',
        hideIcon: true,
        title: 'Inbox triage',
        prompt:
          "Użyj skilla `gh-tasks` żeby wylistować wszystkie otwarte Inbox issues z skonfigurowanego projektu " +
          "(`python3 ~/.claude/skills/gh-tasks/scripts/gh_tasks.py list --status Inbox --state open --json`).\n\n" +
          "Dla każdej pozycji zaproponuj:\n" +
          "  • priorytet (P0-Critical / P1-Must / P2-Important / P3-Nice / Idea),\n" +
          "  • kategorię (Family / Home / Health / Finance / Work / Dev / Learning),\n" +
          "  • gdzie ma trafić: Backlog (akcjonowalne wkrótce), Ideas (parking), " +
          "albo close as not_planned.\n\n" +
          "Podaj rekomendacje jako numerowaną listę z #issue i swoim uzasadnieniem. " +
          "Zanim cokolwiek zaplikujesz — poczekaj na moją akceptację. " +
          "Jeśli któraś pozycja jest niejasna z samego tytułu, zadaj mi pytanie zamiast zgadywać.",
      }}
      sortable={true}
      defaultSort="date"
      quickActions={[
        { label: '→ Backlog', kind: 'set-status', value: 'Backlog', accent: true },
        { label: '→ Ideas',   kind: 'set-status', value: 'Ideas' },
        { label: 'Drop',      kind: 'archive', danger: true, title: 'Move to Archive (Status=Archived + close)', confirm: t => `Drop "${t.title}"?\n\nIt'll be moved to Archive (closed as not planned). Retrievable from the Archive preset.` },
      ]}
      rowAgentBtn={{
        label: 'Discuss',
        titleFor: t => `Discuss: ${t.title}`,
        makePrompt: t => (
          `Pomóż mi przemyśleć tę pozycję z Inbox zanim ją przetrioguję.\n\n` +
          `Tytuł: ${t.title}\n` +
          `Body: ${t.body || '(puste)'}\n` +
          `URL: ${t.url}\n` +
          `Priorytet (jeśli ustalony): ${t.priority || '(brak)'}\n` +
          `Kategoria (jeśli ustalona): ${t.category || '(brak)'}\n` +
          `Area / Project labels: ${t.area_slug || '-'} / ${t.proj_slug || '-'}\n\n` +
          `**Zanim cokolwiek zarekomendujesz** — zadaj mi 2–4 pytania doprecyzowujące, ` +
          `żeby zrozumieć kontekst i moje rzeczywiste potrzeby (deadline, wpływ, ` +
          `gdzie to się wpisuje, czy w ogóle warto). Nie zakładaj, że wszystko wynika ` +
          `z samego tytułu.\n\n` +
          `Po moich odpowiedziach zarekomenduj:\n` +
          `  • priorytet (P0-Critical / P1-Must / P2-Important / P3-Nice / Idea),\n` +
          `  • kategorię (Family / Home / Health / Finance / Work / Dev / Learning),\n` +
          `  • pierwszy konkretny krok,\n` +
          `  • do której kolejki to powinno trafić: Backlog / This Week / Today / ` +
          `Ideas / drop (close not_planned).\n\n` +
          `Częsty docelowy efekt rozmowy:\n` +
          `  • doprecyzowany task w Backlog (możesz to od razu zrobić przez skill ` +
          `\`gh-tasks update --issue ${t.number} ...\`),\n` +
          `  • założenie projektu lub area (PARA), jeśli pozycja okazuje się większa.`
        ),
      }}
    />
  );
}

function IdeasView() {
  return (
    <StatusListView
      status="Ideas"
      title="Ideas"
      emptyText="No ideas captured yet. Anything you're not ready to act on but don't want to forget belongs here."
      sortable={true}
      defaultSort="date"
      quickActions={[
        { label: '→ Backlog', kind: 'set-status', value: 'Backlog', accent: true },
        { label: 'Drop',      kind: 'archive', danger: true, title: 'Move to Archive (Status=Archived + close)', confirm: t => `Drop "${t.title}"?\n\nIt'll be moved to Archive (closed as not planned). Retrievable from the Archive preset.` },
      ]}
      rowAgentBtn={{
        label: 'Brainstorm',
        titleFor: t => `Brainstorm: ${t.title}`,
        makePrompt: t => (
          `Pomóż mi rozwinąć ten pomysł.\n\n` +
          `Tytuł: ${t.title}\n` +
          `Body: ${t.body || '(puste)'}\n` +
          `URL: ${t.url}\n\n` +
          `**Zanim zaczniesz cokolwiek sugerować** — zadaj mi 2–4 pytania ` +
          `doprecyzowujące, żeby zrozumieć moje rzeczywiste potrzeby i kontekst. ` +
          `Nie zakładaj, że wszystko wynika z samego tytułu. ` +
          `Dopiero po moich odpowiedziach:\n\n` +
          `  (1) opisz motywację/realny problem za tym pomysłem,\n` +
          `  (2) zaproponuj najmniejszy konkretny pierwszy krok,\n` +
          `  (3) wymień prerekwizyty, które muszą być spełnione, żeby promować ` +
          `to z "Idea" do "Backlog",\n` +
          `  (4) wymień ryzyka albo powody, by tego NIE robić.\n\n` +
          `Częsty docelowy efekt rozmowy:\n` +
          `  • doprecyzowany task wrzucony do Backlog (Status=Backlog, z priorytetem ` +
          `i kategorią) — możesz to zrobić przez skill \`gh-tasks update --issue ` +
          `${t.number} --status Backlog --priority P2-Important --category Dev\`,\n` +
          `  • założenie nowego projektu lub area (PARA), jeśli pomysł jest większy ` +
          `niż pojedynczy task.\n\n` +
          `Bądź opiniotwórczy, ale dopiero PO tym jak zadasz mi pytania.`
        ),
      }}
    />
  );
}

// Wrapper for the Scheduler tab — fetches dependencies once and renders the
// existing RemindersView component (which is also reused inline elsewhere).
function StandaloneRemindersView() {
  const [data, setData] = useState({ tasks: [], config: null, areas: [], projects: [] });
  const [loading, setLoading] = useState('loading');
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    Promise.all([
      jsonFetch('/api/tasks?state=open&limit=500'),
      jsonFetch('/api/tasks/config'),
    ]).then(([list, cfg]) => {
      if (!alive) return;
      const c = cfg.config || null;
      setData({ tasks: list.items || [], config: c, areas: c?.areas || [], projects: c?.projects || [] });
      setLoading('idle');
    }).catch(e => {
      if (!alive) return;
      setError(e.message || String(e));
      setLoading('error');
    });
    return () => { alive = false; };
  }, []);
  if (loading === 'loading') {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        <div style={{ flex: 1, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 6 }}>
          {Array.from({ length: 6 }).map((_, i) => <div key={i} className="skel" style={{ height: 42 }} />)}
        </div>
      </div>
    );
  }
  if (loading === 'error') {
    return <div style={{ padding: 24 }}><StatusBanner variant="err" label={`Failed to load reminders: ${error}`} /></div>;
  }
  return <RemindersView areas={data.areas} projects={data.projects} tasks={data.tasks} config={data.config} />;
}

Object.assign(window, {
  TasksView, InboxView, IdeasView, RemindersView, StandaloneRemindersView,
  // Exposed so other sections (e.g. Scheduler runs diagnose button) can spawn
  // a Global-agent chat with prefilled context without duplicating the call
  // chain (POST sessions → POST messages → navigate).
  launchAgentSession,
});
