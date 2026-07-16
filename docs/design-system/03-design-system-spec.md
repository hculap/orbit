# Design System — Part 3: The Model (tokens + primitives)

> Status: **the spec** (prescriptive). This is the target model the design-system
> work builds toward. Token names below match `static/tokens.css` (v2)
> verbatim — use them exactly as written. Parts 1–2 describe what exists today;
> this part defines what we are converging on.

## Executive summary

The current system has good bones but a broken contract layer. `tokens.css`
defines a clean editorial-quiet dark palette (warm-white `--fg` ladder, oklch
violet accent, `--ok`/`--warn`/`--err`/`--info`, Helvetica + JetBrains Mono)
plus full spacing (`--s-1..10`), radius (`--r-sm`/`md`/`lg`/`xl`), and type
(`--t-display..xs`) scales — but the spacing scale was used **0×**, the type
scale **1×**, and ~879 hardcoded `fontSize` + ~1000 color literals bypassed them
entirely. The single highest-leverage fix is making the *existing* tokens
load-bearing, not adding more design.

Three correctness bugs had to ship first:

1. `--fg-1` was referenced **32×** but never defined (silently rendered an
   inherited color on emphasized titles across orchestrator/tasks/details).
2. `--danger`/`--warning`/`--accent-dim`/`--bg-2`/`--vvh`/`--mono` were
   referenced with hardcoded fallbacks and never defined — `tasks.jsx` in
   particular ran an entire parallel `#f87171`/`#fbbf24` red/amber family
   instead of `--err`/`--warn`.
3. `borderRadius:8` is the most common radius in the codebase (**106×**) with no
   token at all (`--r-sm` is 6, `--r-md` is 10).

There are genuinely two primitive systems — `window.*` (components.jsx,
`Object.assign`) and `window.HubSettings.*` (settings-primitives.jsx) — and they
are **not** redundant: components.jsx holds section-level chrome (Card, Modal,
BottomSheet, Button, Chip) while HubSettings holds form atoms (Toggle,
Segmented, SettingSelect, NumberField, StatusBanner, ScopeChip). The right move
is **selective promotion, not a merge**: lift Segmented / StatusBanner /
ScopeChip / NumberField / Select / Field to global, add the genuinely missing
global form primitives (Input, Textarea, Select, Field, Tabs, EmptyState,
Badge, Popover), and add a `disabled` prop to Button (its absence is the
documented root cause of `_ActionButton` being reinvented).

The biggest structural duplications are `PinSheet` + two `_MobileSheet` copies
reimplementing `BottomSheet`, two `_DesktopShell`/`Popover` copies (no global
Popover exists), `DrawerItem` vs `SidebarItem` (the same nav item twice), and
the page-scaffold `compact?16:28` / `compact?100:28` literal repeated ~26×. The
paradigm — inline style objects referencing `var()` tokens, components published
to `window`, no bundler — is sound and **must be preserved**; the entire fix
lands inside it.

---

## Token system v2

The v2 pass keeps every existing token and adds the steps/aliases the codebase
actually reaches for. **The 6 undefined-token fixes ship prominently and first**
— they un-break silently-broken references with zero render risk.

### The 6 undefined-token fixes (correctness, ship first)

| Token | Definition | Was |
|---|---|---|
| `--fg-1` | `#f5f3ee` (one notch brighter than `--fg`) | referenced 32×, undefined → inherited |
| `--danger` | `oklch(0.70 0.18 25)` (= `--err`) | undefined + `#f87171` fallbacks |
| `--warning` | `oklch(0.82 0.14 80)` (= `--warn`) | undefined + `#fbbf24` fallbacks |
| `--bg-2` | `#14161a` (= `--surface-1`) | undefined → `#1a1a1a` fallback (app.jsx UpdateToast) |
| `--mono` | `var(--font-mono)` | undefined → fallback (global-detail.jsx) |
| `--accent-dim` | `oklch(0.72 0.18 295 / 0.16)` (= `--accent-soft`) | undefined (tasks.jsx) |

### Token groups + changes

| Group | Change | Rationale |
|---|---|---|
| **fix** | Define `--fg-1: #f5f3ee` | Hard bug: 32 silently-broken title refs across orch/tasks/details. |
| **fix** | `--danger`/`--warning` as `--err`/`--warn` aliases; sweep tasks.jsx `#f87171`/`#fbbf24` (12 sites) | tasks.jsx ran a parallel red/amber family; aliasing unifies without a risky one-shot find/replace. |
| **fix** | `--bg-2 = --surface-1`, `--mono = --font-mono`, `--accent-dim = --accent-soft` | Three more undefined-with-fallback tokens; aliasing to the real token is zero-risk. |
| **radius** | Add `--r-control: 8px` (the single most common radius, 106×); keep `--r-sm:6`, `--r-md:10`, `--r-lg:14`, `--r-xl:20` | `borderRadius:8` had no token and is dominant; map inputs/buttons/small controls/badges/non-pill chips to it. |
| **radius** | Add `--r-widget: 12px`, `--r-pill: 999px`, `--r-sheet: 16px` | 12 and 999 are heavily-used bespoke values; naming them lets media/widget/sheet surfaces converge. |
| **type** | Add `--t-2xs: 10px` (96×), `--t-md: 14px` (172×), `--t-code: 12.5px`; keep `--t-xs:11`, `--t-sm:13`, `--t-body:15` | The 6-step scale had gaps exactly where usage concentrates (10/14/12.5); the top-5 sizes must each map to a token. |
| **spacing** | Keep `--s-1..--s-10`; add semantic aliases `--page-pad:28`, `--page-pad-compact:16`, `--nav-clearance` (env-aware), `--safe-bottom`, `--card-gap:12`, `--inter-card-gap:14`, `--field-gap:6` | The raw 4-based scale was used 0× because authors think in semantic terms; named aliases on top of the scale get adopted. |
| **control** | Add `--control-h:38`, `--control-h-sm:36`, `--control-h-touch:44`; add `--panel-width:280`, `--sidebar-width:240`, `--drawer-width:280`, `--kanban-col:280` | Control heights drift 28/32/34/36/38/42 → row misalignment; one canonical height + an explicit touch min fixes it. Layout widths were hardcoded in the shell. |
| **color** | Add status soft/line/bg families: `--ok-soft`/`--ok-line`, `--warn-soft`/`--warn-line`, `--err-soft`/`--err-line`/`--err-bg`, `--info-soft`/`--info-line` | The error-tint `oklch(0.30 0.10 25 / 0.18)` and err border `oklch(0.70 0.18 25 / 0.35)` are copy-pasted across 8+ files; only accent had soft/line tokens before. |
| **color** | Add `--scrim: rgba(0,0,0,0.5)`, `--scrim-heavy: rgba(0,0,0,0.92)`, `--terminal-bg: #0e0f12` (+ JS `HUB_TERMINAL_BG` mirror for injected iframe CSS); drop `#818cf8` (28×) → `--accent`; `#fff` in accent contexts → `--accent-fg` | Backdrop opacity had 3 competing values; terminal bg had 3 dark literals; `#818cf8` predates the oklch `--accent`. |
| **shadow** | Add `--shadow-sm`, `--shadow-fab`, `--shadow-pop`, `--shadow-sheet` (upward, for bottom sheets), `--shadow-accent` (featured tab glow); keep `--shadow-1`/`--shadow-2` | `--shadow-1/2` are too coarse for FABs, popovers, the upward sheet shadow, and the accent-tinted featured-tab glow. |
| **zIndex** | Add ordered scale: `--z-sticky:5`, `--z-dropdown:30`, `--z-overlay:40`, `--z-sheet:50`, `--z-drawer:60`, `--z-modal:100`, `--z-toast:120`, `--z-fullscreen:200` | z-index was scattered as raw ints with no stack order; the UpdateToast at 9999 could collide with modals/sheets. |
| **motion** | Add `--dur-fast:120ms`, `--dur-base:150ms`, `--dur-mid:180ms`, `--dur-slow:220ms`; `--ease-out: cubic-bezier(.2,.7,.3,1)`, `--ease-spring: cubic-bezier(.2,.7,.2,1)` | No motion tokens existed; `.12` vs `.15` for the same hover and two near-identical spring curves already drift. |

> **Deliberate non-collapse:** `--r-control:8` is added as a *new* token rather
> than remapping `--r-md:10`→8, to avoid silently shifting every current
> `--r-md` card to 8. Do not collapse them.

---

## The primitive catalog

`status` is one of **exists-keep** (correct as-is, the fix is consumption),
**exists-enhance** (extend the existing primitive), or **new**.

| Primitive | Status | Purpose | Key props | Replaces |
|---|---|---|---|---|
| **Button** | exists-enhance | Text button (primary/ghost/quiet/danger × sm/md/lg). THE fix: add `disabled` (guards onClick, opacity 0.5 + not-allowed) + `busy`. | children, onClick, href, variant, size, icon, disabled, busy, type, style | `_ActionButton` (~5), voice/terminal raw-`<button>` fallbacks, ~30 raw accent `<button>` in tasks.jsx |
| **IconButton** | exists-enhance | Square icon-only button. Add `active` (accent-soft toggle) + `disabled` + a touch size enforcing 44px on compact. | icon, label, size, active, disabled, onClick, compact | `_McIconBtn`, `_DockIconBtn`, `_TermFsButton`, `_TermSelectButton`, SpeakButton, MessageActionButton, MicButton (~10 @ 22–34px) |
| **Card** | exists-enhance | Surface-1 bordered container. Add `gap`/`padding` + `variant='section'|'inset'`; unify radius on `--r-lg`. | children, padding, gap, variant, onClick, style | `SettingCard` (~20) once it delegates |
| **Chip** | exists-enhance | Pill/tag. Add `size='xs'`, `mono`, semantic `variant`. | children, variant, size, mono, onClick, active | RunnerModeBadge, SuggestionChips, compacted_from, mime pills, scope labels, PAUSED/FAILING/GLOBAL, `_GhLabelChip`, tasks Pill/priorityBadge (~30) |
| **Modal** | exists-enhance | Centered dialog shell. Add `ModalBody` + `ModalFooter` slots. | open, onClose, title, children; ModalFooter({onCancel,onConfirm,confirmLabel,variant,busy}) | ~7 bespoke modal body/footer layouts |
| **BottomSheet** | exists-keep | Mobile slide-up sheet (drag handle, backdrop, scroll-lock, Esc). Already correct — the fix is CONSUMPTION. Ensure title/subtitle/footer slots. | open, onClose, title, subtitle, children, footer | `PinSheet`, two `_MobileSheet` copies (~250 LOC) |
| **ConfirmModal** | new | Destructive-action dialog: title + message + Cancel/Confirm; rename variant adds an Input. | open, onClose, title, message, confirmLabel, cancelLabel, onConfirm, variant, busy | 3 near-identical orch modal bodies + library/scheduler delete confirms |
| **Popover** | new | Anchor-relative desktop dropdown (surface-1/hairline/`--r-md`/`--shadow-pop`, outside-click + Esc, `--z-dropdown`). GLOBAL — no equivalent exists. | open, onClose, anchorRef, placement, minWidth, maxWidth, maxHeight, children | DesktopPopover, `_DesktopShell`/`_DesktopPopover`, link-picker, FilterChipGroup (~5) |
| **Input** | new | Single-line text/search/password. surface-2 bg, hairline, `--r-control`, `--control-h`, `--t-sm`. THE most-needed missing primitive. | value, onChange, type, placeholder, disabled, error, mono, size, autoFocus, onKeyDown, onCommit | 84 raw `<input>` + `_SECInputStyle`/`_simInputStyle`/`_scmInputStyle`/`_sdvInputStyle`/`_LAP_INPUT_BASE`/`_FloatField`/rename/search (~14 factories) |
| **Textarea** | new | Multi-line; mono default (`--t-code`, lineHeight 1.6), resize, rows, auto-resize. | value, onChange, rows, placeholder, mono, error, resize, minHeight, onPaste, onDrop | 26 raw `<textarea>` (composer, commit/issue/prompt/file editors) |
| **Select** | new | Styled native `<select>` (generalize HubSettings.SettingSelect). | value, onChange, options, placeholder, disabled, mono, size | 22 raw `<select>` (tasks local Select ×18, logs-view ×2, share/gallery sort, scheduler) |
| **Field** | new | Form-field layout: label (`--t-md`/500) + control + hint(`--t-xs`/fg-3)/error. Unify HubSettings.FieldRow; fix 14-vs-13 label drift. | label, hint, error, required, layout, children | ~40 hand-rolled label+control stacks |
| **Tabs** | new | ONE tab primitive: `variant='underline'` (2px accent border-bottom, marginBottom:-1) and `variant='pills'` (Segmented style). Optional sticky/icon/count, URL-sync-ready. | tabs, active, onChange, variant, sticky, size | settings tab strip, 3 detail TabBars, `_GhSegmented`, `_SimTabStrip`, `_GlobalTabs`, `_SdvTabStrip`, `_ScmTriggerTabs`, TasksSubTabs, scheduler subTabStrip, 4 Segmented copies (~15 — the single biggest gap) |
| **Segmented** | exists-enhance | Promote HubSettings.Segmented to `window.*` (becomes the pills engine for Tabs, or stays as the small toggle-group atom). | value, onChange, options, size | 4 bespoke segmented controls in library + scheduler |
| **Badge** | new | Small count/dot/label with absolute-overlay mode + inline mode. Distinct from Chip by the absolute count overlay. | variant, value, color, max, absolute | `_Badge`, `_PinTrigger` count, model short-label, UnreadBadge — ~5 + wires up the dead unread feature |
| **StatusBanner / AlertBanner** | exists-enhance | Promote HubSettings.StatusBanner to components.jsx. Two shapes: dot+label+action row; paragraph variant using `--err-soft`/`--err-line`. | variant, label, sub, action, inline | ~25 bespoke error/info banner divs (library 8, secrets/skills 8, tasks/scheduler 8, settings, details) |
| **EmptyState** | new | Zero-state: icon + message + sub + action. Lighter than ComingSoonView. | icon, message, sub, action, centered, padded | ~40 bare empty `<div>` (shell 8, library 15, + details/orch/secrets/tasks/scheduler) |
| **ListRow** | new | Standard data row: leading + title + mono-sub + spacer + trailing + divider; enforces `--control-h-touch` on compact. | leading, title, sub, trailing, onClick, divider, compact | services/peers/tmux rows, archive, SessionItem, scheduler/task rows, branch/commit/file rows, download rows (~30) |
| **WidgetFrame** | exists-enhance | Surface-1 + hairline + `--r-widget` container with optional mono-uppercase header. Promote earlier in load order; tokenize radius:12. | title, header, children, padding | inline frame wrappers in artifacts/widgets; canonical media/chart/map container |
| **NavItem** | new | Single nav link for sidebar + drawer: SPA anchor, icon+label+badge, active accent-soft, `size='sm'|'md'`. Accepts a SectionDef. | section, active, onClick, unread, size, href | `DrawerItem` + `SidebarItem` + inline NavBadge |
| **SectionLabel / Eyebrow** | new | Uppercase mono eyebrow: `--t-xs`, `--fg-3`, letterSpacing 0.08em. | children, spacing | ~25 inline uppercase-mono label divs (nav groups, card headers) |
| **PageScaffold** | new | Outer scroll wrapper owning `--page-pad`/`--page-pad-compact` + `--nav-clearance`; PullToRefresh-compatible. | compact, children, noPadding | `padding:compact?16:28` + `paddingBottom:compact?100:28` (~26×) |
| **Avatar** | new | Rounded monogram/emoji square/circle (accent-soft bg + accent-line border), size variants, optional status dot. | icon, initials, size, shape, statusDot | DrawerHeader/SidebarHeader monogram, agent/skill avatars (~7) |
| **LogPane** | new | Terminal-style scrollable pane: `--terminal-bg`, mono, fixed height, auto-scroll, React-state hover. | lines, height, onClickLine, loading, emptyText | 3 terminal/log panes (logs-view, service detail, context log) |
| **MediaPlayButton** | new | Circular accent-filled play/pause overlay (one size prop), correct marginLeft:2 glyph + consistent shadow. | size, playing, onClick | 3 divergent play circles (56/40/44px) in artifacts |
| **Spinner / LoadingText** | new | One loading affordance: small Icon spinner + optional mono caption (`--fg-4`). | label, size, inline | ~15 ad-hoc loading-text divs (library/secrets/skills/settings/session-switcher) |

---

## Layout helpers

These are the composition helpers ~1900 inline style blocks reinvent. Several
double as primitives (ListRow, Field, SectionLabel, PageScaffold).

| Helper | Signature | Kills |
|---|---|---|
| **PageScaffold** | `{compact, children, noPadding}` | `compact?16:28` / `compact?100:28` (~26×) |
| **Stack / Inline** | `{gap, align, children}` / `{gap, align, wrap, children}` | the two flex-column / flex-row-with-gap idioms ~1900 inline blocks reinvent; gap maps to spacing tokens |
| **TwoCol** | `{compact, ratio='1.4fr 1fr', gap, children}` | the responsive two-column detail layout (collapses to 1fr) used ~6× |
| **PanelHeader** | `{leading, title, subtitle, trailing, compact}` | the flex + hairline-bottom + safe-area-top header used by orch/library/scheduler panels |
| **Toolbar** | `{left, filters, right}` | search + filter-chips + spacer + actions row (tasks/reminders/scheduler/gallery) |
| **ListRow** | (also a primitive) leading/title/sub/trailing/divider + compact tap-target | structurally-identical data rows |
| **Field** | (also a primitive) label + control + hint/error, `layout='col'|'row'` | hand-rolled form-field stacks |
| **ModalBody / ModalFooter** | padding/gap-tokenized body + right-aligned Cancel/Confirm footer (on Modal) | re-coded modal footers |
| **MetaGrid** | `{cells:[{k,v}], cols=2, compact}` | uppercase-mono key+value grids for stats/log-fields/service-status |
| **KVRow** | `{label, value, mono}` | single key-value review/detail row (wizard review, breadcrumbs) |
| **CardGrid** | `{compact, minCol=280}` | `auto-fill minmax(280px,1fr)` card grid (agents/skills/library directories) |
| **SectionLabel / Eyebrow** | uppercase-mono section label (fg-3 + 0.08em) | nav-group + card-header labels |
| **TransportBar** | `{left, center, right, compact}` | media transport row (dock grid vs inline flex) |
| **KbdHint** | `{shortcuts:[{keys,label}]}` | keyboard-shortcut legend row (session-switcher footer, command palette) |

---

## Reconciling the two systems

**Do NOT merge the two systems into one** — they serve different tiers and the
settings contract depends on the split. `components.jsx` (`window.*`,
`Object.assign`) is the section/chrome layer (Card, Modal, BottomSheet, Button,
Chip, Icon, IconButton). `settings-primitives.jsx` (`window.HubSettings.*`) is
the form-atom + per-device/server-settings layer whose stable surface
(`useSettings`, `useServerSettings`, the `HubSettings.*` atoms) is read off
`window` by external consumers like `orchestrator-unread`/`-conversation`.
Breaking that contract would ripple.

The plan is **selective promotion + bridging** in three independently-shippable
tranches. The mechanism for every promotion is: move the implementation into
components.jsx, then re-export the old name as a thin alias so
`window.HubSettings.X` still resolves — **zero call-site breakage**.

### PROMOTE (move to components.jsx, keep a thin HubSettings alias)

| Atom | Promote to | Alias kept |
|---|---|---|
| **StatusBanner** | `window.StatusBanner` (8+ buckets reinvent it because it's locked in HubSettings) | `HubSettings.StatusBanner = window.StatusBanner` |
| **Segmented** | `window.Segmented` (becomes the 'pills' variant engine for the new Tabs) | `HubSettings.Segmented` |
| **ScopeChip** | `window.ScopeChip` (useful anywhere blast-radius matters) | `HubSettings.ScopeChip` |
| **SettingSelect** | generalized into the new global **Select** | `HubSettings.SettingSelect` becomes a styled wrapper over it |
| **FieldRow** | folded into the new global **Field** (`layout='col'`) | `HubSettings.FieldRow` aliases it |

### ENHANCE-IN-PLACE (fix the root cause so HubSettings stops needing copies)

- Add `disabled`/`busy` to `window.Button` → deletes `_ActionButton` and the
  defensive `window.Button ? … : <button>` fallbacks in settings-voice/-terminal.
- Add `step` + `parseFloat` to `NumberField` → deletes `_FloatField`.
- Add `gap`/`padding`/`variant='inset'` to `Card` → `SettingCard` becomes
  `<Card variant='inset' gap={12}>`, eliminating the `--r-lg`(14) vs 10 drift.

### KEEP-SCOPED (do not promote — legitimately domain-specific)

- `useSettings`/`useServerSettings` hooks + the module-level server-settings
  cache (the actual settings contract).
- `Toggle`, `AdvancedDisclosure`, `SettingGroup`, `SettingRow`, `ToggleRow`
  (settings-page semantics).
- secrets `CaptchaGate`/`_secUseCaptcha`/`RevealModal` and the kanban DnD
  outline (genuinely bespoke).

Net result: one canonical implementation per concept, two namespaces preserved
as a thin compatibility layer, settings contract intact.

---

## Conventions

- **No bundler, no imports.** CDN React 18 + Babel-standalone; JSX modules load
  as ordered `<script type="text/babel">` tags. There is no build step and no
  type checker.
- **Window-published.** Every primitive publishes to `window` (chrome →
  `window.*` via `Object.assign`; form atoms → `window.HubSettings.*`). Sibling
  files pick components up off `window` — no `import`.
- **Inline styles reference `var()` tokens.** Styling is plain JS style objects
  that reach for CSS custom properties (`var(--…)`), never raw literals. The
  token layer in `tokens.css` is the single source of visual truth.
- **Load-order rule.** New shared primitives (Input/Textarea/Select/Field/Tabs/
  EmptyState/Badge/Popover) and the promoted ones MUST live in or load **right
  after `components.jsx`** (which already loads first), **before
  `settings-primitives.jsx` and before every consumer**. settings-primitives.jsx
  then references `window.*` primitives guaranteed present and can drop its
  defensive existence checks.
- **`compact` is the breakpoint mechanism.** There are no media queries in JSX;
  responsiveness is the threaded `compact` boolean. Every primitive and layout
  helper MUST accept/thread `compact` rather than hardcoding a single layout.
- **Injected-iframe caveat.** The terminal iframe's injected CSS cannot use
  `var()`; `--terminal-bg` has a parallel JS constant (`HUB_TERMINAL_BG`).
