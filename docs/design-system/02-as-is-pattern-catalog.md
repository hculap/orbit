# Design System — Part 2: As-Is Pattern Catalog (per bucket)

> Status: **as-built audit** (descriptive). This is the file-by-file record that
> complements Part 1's codebase-wide measurements. Nine buckets were reviewed
> across `src/orbit/static/`. For each bucket: the files reviewed,
> the notable bespoke re-implementations, the repeated layout patterns, and the
> worst hardcoded hotspots + inconsistencies. The *fixes* live in Part 3 (the
> model) and Part 4 (the roadmap) — this part only catalogs what exists today.

## Cross-bucket summary: the most-duplicated patterns

The same handful of patterns is re-invented in nearly every bucket. In rough
order of how widely they recur:

- **Page scaffold** — `padding: compact ? 16 : 28` side padding +
  `paddingBottom: compact ? 100 : 28` mobile-nav clearance, hardcoded **~26×**
  across sections, details, library tab bodies, scheduler, secrets/skills.
- **Tab strip** — implemented **~15 different ways** (underline in
  details/tasks/library; pills in scheduler/skills/settings; `_GhSegmented`,
  `_SimTabStrip`, `_GlobalTabs`, `_SdvTabStrip`, `_ScmTriggerTabs`), with 1–2px
  padding/font drift and `marginBottom:-1` present on some, absent on others.
- **Text input / textarea / select** — no global primitives exist, so 84 raw
  `<input>`, 26 `<textarea>`, 22 `<select>` are hand-styled. Local style
  factories proliferate: `_LAP_INPUT_BASE`, `_SECInputStyle`, `_simInputStyle`,
  `_scmInputStyle`, `_sdvInputStyle`, `inputStyle` objects.
- **Error / status banner** — the warm-red tint `oklch(0.30 0.10 25 / 0.18)` +
  `var(--err)` border is copy-pasted in 8+ files; `StatusBanner` exists but is
  locked inside `window.HubSettings`.
- **Empty state** — ~40 bare `<div>` zero-states (mono fg-3/fg-4 text) across
  every bucket.
- **List row** — `[leading | title + mono-sub | spacer | trailing]` recurs as
  services/peers/tmux rows, SessionItem, branch/commit/file rows, task/reminder
  rows (~30 structurally identical instances).
- **Bottom sheet & desktop popover** — `BottomSheet` exists but is reimplemented
  3× (`PinSheet`, two `_MobileSheet`); no global `Popover` exists, so it is
  hand-rolled 5× (`_DesktopShell`, `_DesktopPopover`, `DesktopPopover`,
  link-picker, `FilterChipGroup`).
- **Form field** — `label + control + hint` flex-column stack (~40 hand-rolled).
- **Icon button & pill/chip** — `IconButton`/`Chip` exist but small bespoke
  copies abound (`_McIconBtn`, `_DockIconBtn`, `SpeakButton`,
  `MessageActionButton`; RunnerModeBadge, mime pills, status spans).

Two latent token bugs show up in almost every bucket: `var(--fg-1)` (referenced
but undefined → silently inherits) and `borderRadius: 8` (the most common
radius, 106×, with no token).

---

## 1. shell-nav

**Files reviewed:** `app.jsx`, `sections.jsx`, `router.jsx`
(router.jsx is pure routing logic — zero inline styles, leave alone.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| `DrawerItem` vs `SidebarItem` (same nav item twice) | app.jsx:1150–1168, 1316–1342 | NavItem | 2 |
| `DrawerGroup` vs `SidebarGroup` | app.jsx:1141–1148, 1307–1313 | NavGroup | 2 |
| `DrawerHeader` vs `DesktopSidebarHeader` (server monogram) | app.jsx:1113–1138, 1286–1305 | SidebarBrand | 2 |
| Nav group label (uppercase mono eyebrow) | app.jsx:1144, 1310, 1452, 468, 1357 | NavGroupLabel/Eyebrow | 5 |
| Fake search trigger (desktop top bar) | app.jsx:1402–1411 | SearchTrigger | 1 |
| Fake search trigger (mobile drawer) | app.jsx:1082–1095 | SearchTrigger | 1 |
| Raw `<input>` in CommandPalette (bare mode) | app.jsx:1440–1442 | SearchInput | 1 |
| Raw `<select>` for file sort (ShareView) | sections.jsx:257–266 | Select | 1 |
| Raw `<button>` checkbox in FileCard | sections.jsx:383–397 | Checkbox | 1 |
| Empty-state plain-text divs | sections.jsx:314/603/657/812/898/946/976, app.jsx:1472 | EmptyState | 8 |
| Archive list-row (icon+title+meta+action) | app.jsx:471–488 | ListRow | 1 |

### Repeated layout patterns

- Section page scaffold (`compact?16:28` + `compact?100:28` + header + card grid) — ShareView/SystemView/AreasView/ProjectsView/AppsView/ResourcesView.
- Two-column list row `[StatusDot | text-block | spacer | right-slot]` — services/peers/tmux rows.
- Library card `[avatar | name+path | description | chips]` — AreaCard (44px avatar) / ProjectCard (32px avatar).
- Desktop app shell: sidebar(240px) + main(flex:1).
- Top bar / page header bar (borderBottom hairline + padding + content) — MobileSectionHeader / DesktopTopBar / SubHeader.
- Host-identity monogram; metric row (label + value + progress bar).

### Worst hotspots + inconsistencies

- **color (high):** `var(--bg-2, #1a1a1a)` — `--bg-2` undefined → renders the literal `#1a1a1a`; `#fff` button text; raw status oklch tints.
- **radius (high):** `borderRadius:8` (UpdateToast, drawer search, monograms, top-bar search), `12` (FileCard), `16` (featured BottomTab) — none tokenized.
- **font (high):** ~10 distinct fontSize literals; `10px` (BottomTab/nav-group labels) had no token.
- **spacing (high):** all gap/padding/margin hardcoded; spacing scale used 0×.
- **DrawerItem ≠ SidebarItem (high):** 14/13px font, 8/7 radius, 18/16 icon, JS-hover only on Sidebar; badges diverge (10px/2px-5px/r4 vs 9px/1px-4px/r3).
- **Dead `unread` prop (high):** `UnreadProvider`/`useUnreadCounts` compute counts correctly but no nav component ever renders the indicator — a functional gap.
- **`var(--bg-2)` undefined (high):** fallback `#1a1a1a` sits between `--bg` and `--surface-1`; intends `--surface-1`.

---

## 2. shared-details

**Files reviewed:** `components.jsx`, `details.jsx`, `global-detail.jsx`,
`file-preview.jsx`, `logs-view.jsx`, `tokens.css`
(`components.jsx` is the strongest file in the codebase — well-structured
primitives with React-state hover and token colors; its one gap is `borderRadius:8`.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| Back-nav underline tab bar (raw `<button>` + border-bottom) | details.jsx:105–115, 548–558, global-detail.jsx:193–201 | TabBar (none exists) | 3 (6 codebase-wide) |
| Section page scaffold (`compact?16:28` / `compact?100:28`) | details.jsx:77/311/432/520, global-detail.jsx:175, logs-view.jsx:127 | PageScaffold | 26 |
| Card-with-subheader (Card pad=0 + SubHeader + scroll body) | details.jsx:455–499, 579–602, 634–653 | SectionCard | 5 |
| Raw `<select>` styled by hand | logs-view.jsx:140–147, 148–153 | Select | 2 |
| Raw `<textarea>` styled inline | global-detail.jsx:129–138 | Textarea | 1 |
| Inline service status banner | details.jsx:528–545 | StatusBanner (HubSettings-only) | 1 |
| Byte-format triplicated (`fmtBytes`/`bytesShort`/`fpBytesShort`) | components.jsx:703–711, sections.jsx:9–16, file-preview.jsx:7–14 | window.fmtBytes | 3 |

### Repeated layout patterns

- Detail page scaffold; detail header (back btn + eyebrow + title + sub + action).
- Two-column responsive grid `compact ? '1fr' : '1.4fr 1fr'`, gap:14.
- Key-value metadata grid (uppercase-mono label + mono value).
- Tab bar (2px accent underline, `marginBottom:-1`).
- Terminal/log pane (dark bg, mono, fixed height, auto-scroll).
- Eyebrow / section label (11px, fg-3, uppercase, 0.08em) — 8+ times.

### Worst hotspots + inconsistencies

- **color (high):** `#0a0b0d` hardcoded terminal black; status-banner oklch tints inline; two backdrop opacities (BottomSheet `0.55` vs Modal `0.6`); imperative `rgba(255,255,255,0.03)` row hover.
- **radius (high):** `borderRadius:8` (back btn, IconButton, Button, SearchInput, FileIconBox), `12` (FileCardLite, status banner, terminal pane, preview box).
- **font/spacing (high):** detail title `compact?22:26`; type scale used exactly 1× (SectionHeader h1); spacing scale 0×.
- **DetailHeader back button is raw `<button>` (med)** but global-detail uses `IconButton` for the same role — divergent hover/tap-target/aria.
- **Tab `marginBottom:-1` present in global-detail, absent in details (med)** → active tab not flush with hairline on some views.
- **`SubHeader` defined in sections.jsx, consumed in details/library via load order (med)** — implicit contract.
- **No aria on tabs/log-rows/checkboxes (high).**
- **Imperative hover mutation in logs-view & KebabMenu (med/low)** — a re-render silently wipes it.

---

## 3. orch-core

**Files reviewed:** `orchestrator.jsx`, `orchestrator-conversation.jsx`,
`orchestrator-transcript.jsx`, `orchestrator-blocks.jsx`,
`orchestrator-stream.jsx`, `orchestrator-message-actions.jsx`
(Largest/most complex bucket. `compact` threading is the responsive mechanism —
preserve it. `orchestrator-stream.jsx` is pure logic, no DS work.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| 22×22 per-message icon button | message-actions.jsx:49–87, transcript.jsx:63–88 | IconButton | 2 |
| Raw `<input>` (rename modal, session search) | orchestrator.jsx:2305–2318, 748–751 | Input | 2 |
| Pill badges (RunnerModeBadge, pin count, queue #n, compacted_from, SuggestionChips) | orchestrator.jsx:238–252/293–299/530–534, blocks.jsx:409–428, transcript.jsx:282–290 | Chip / Badge | 5 |
| Bespoke modal body (pad:18 + col gap:14 + footer) | orchestrator.jsx:2304–2321, 2322–2333, 2344–2355 | ConfirmModal / ModalFooter | 3 |
| Compacting full-bleed overlay | orchestrator.jsx:2199–2213 | LoadingOverlay | 1 |

### Repeated layout patterns

- Two-column chat layout (transcript + 280px session panel).
- Panel header row (flex + gap + hairline-bottom, desktop minHeight 71).
- Session list row (active accent-soft, title 13/500 + mono sub + 2-line preview).
- Empty/zero-state inline div (12px fg-3).
- Toolbar footer (new-session full-width accent button).
- Confirmation modal (title + body + Cancel/Confirm).

### Worst hotspots + inconsistencies

- **color (high):** error border/bg oklch literals across blocks; copy-ok `oklch(0.78 0.14 155 / 0.16)`; image overlay `rgba(14,15,18,0.65)`; waveform glow `rgba(140,90,255,0.15)`.
- **font (high):** 15/22/14/13/12px hardcoded; type scale never used in bucket.
- **spacing (high):** `compact?'0 14px 6px':'0 24px 6px'` in 4+ places; 280px panel width.
- **radius (high):** `borderRadius:8` dominant; 4/5/10/12 for avatars/bubbles.
- **Three `ThinkingDots` (med)** — 4px vs 6px, different keyframes.
- **SpeakButton ≈ MessageActionButton (high)** — identical 22×22, separate code.
- **Error styling inconsistent across block types (med)** — some inline oklch, some only `var(--err)` text.
- **Inline `<style>` keyframes inside render bodies (low)** — duplicate injection risk.

---

## 4. orch-terminal

**Files reviewed:** `orchestrator-terminal-preview.jsx`,
`orchestrator-shortcuts.jsx`, `settings-shortcuts-editor.jsx`,
`orchestrator-pin-popover.jsx`
(Highest density of interaction primitives — effectively a self-contained
mini-DS for terminal controls. Build around `renderButton()`/`dispatchButton()`.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| Soft-keyboard key-cap buttons (46×38, r6) | terminal-preview.jsx:670–760 | KeyCap | 20 |
| Floating overlay icon buttons (`_TermFsButton`/`_TermSelectButton`, 30×30) | terminal-preview.jsx:817–879 | IconButton/OverlayIconButton | 2 |
| `_TermScrollFab` (40×40 pill FAB) | terminal-preview.jsx:969–993 | Fab | 1 |
| Voice + upload indicator pills (r999) | terminal-preview.jsx:1374–1390, 1392–1407 | FloatingPill/StatusPill | 2 |
| Status-dot + label rows | terminal-preview.jsx:1412–1423, 1946–1953 | StatusRow | 2 |
| `_scBtnGhost`/`_scBtnPrimary` style objects | settings-shortcuts-editor.jsx:22–33 | Button | 6 |
| `fieldStyle` input/select/textarea factory | settings-shortcuts-editor.jsx:239, 343 | Input/Field | 5 |
| `_IconPicker` (36×36 grid) | settings-shortcuts-editor.jsx:132–168 | IconPicker | 1 |
| `_ColorPicker` (28×28 swatches) | settings-shortcuts-editor.jsx:172–204 | ColorPicker | 1 |
| `DesktopPopover` | pin-popover.jsx:139–165 | Popover | 1 |
| `PinSheet` (full bottom-sheet reimpl) | pin-popover.jsx:167–272 | BottomSheet | 1 |
| Modifier-toggle pills (Ctrl/Alt/Shift/Cmd) | settings-shortcuts-editor.jsx:265–270 | Chip/ToggleChip | 4 |
| Session-switcher buttons | terminal-preview.jsx:705–723 | KeyCap | 4 |

### Repeated layout patterns

- Status-dot row; horizontal scrolling key-cap toolbar; editor list row `[chip][label+meta][Toggle][Kebab]`; form field (label above + control); modal body (pad:18 col gap:14 + save/cancel); floating overlay button; bottom-sheet structure; muted empty-state text.

### Worst hotspots + inconsistencies

- **color (high):** `#0b0d11` (SSE pre bg) vs `#0e0f12` (iframe/fullscreen) — terminal bg split; `oklch(0.18 0.05 25 / 0.30)` error bg; shadow literals.
- **font (high):** three mono font-stack spellings vs `var(--font-mono)` (0 uses); 11/12/13/14/22px hardcoded.
- **Control height drift (high):** soft-keys 38, icon pickers 36, mini-btn 36, PinRow `compact?44:0` — no token; 38/36 below 44px touch min.
- **`borderRadius:8` most common, no token (high).**
- **PinSheet reimplements BottomSheet (high).**
- **Terminal bg split `#0b0d11` / `#0e0f12` (med).**

---

## 5. orch-media

**Files reviewed:** `orchestrator-artifacts.jsx`, `orchestrator-widgets.jsx`,
`orchestrator-voice.jsx`, `orchestrator-tts.jsx`, `orchestrator-mic-button.jsx`,
`orchestrator-attachments.jsx`, `orchestrator-state-modals.jsx`,
`orchestrator-model-modal.jsx`, `orchestrator-scroll-fab.jsx`,
`orchestrator-notifications.jsx`, `orchestrator-unread.jsx`,
`orchestrator-aec-loopback.jsx`, `media-dock.jsx`, `media-controls.jsx`,
`media-player-engine.jsx`
(Most mechanically consistent architecture — thin shells over window-global
hooks. Duplication here is structural copy-paste, not spaghetti.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| Round ghost icon button (34/32, r999) | media-controls.jsx:26–49, media-dock.jsx:38–57, mic-button.jsx:97–132, attachments.jsx:469–504 | IconButton | 4 |
| Mobile bottom sheet | state-modals.jsx:138–221, model-modal.jsx:69–141 | BottomSheet | 2 |
| Desktop popover | state-modals.jsx:92–136, model-modal.jsx:31–67 | Popover (none) | 2 |
| Pill/chip badge (9–11px, r999, mono) | artifacts.jsx:411, media-dock.jsx:104–105, widgets.jsx:93–98, attachments.jsx:276 | Chip | 4 |
| Accent play-button overlay | artifacts.jsx:161–167, 177–179, 402–406 | MediaPlayButton | 3 |
| Count/notification badge (abs, r7, accent) | state-modals.jsx:296–305, model-modal.jsx:251–259 | Badge | 2 |
| Raw `<textarea>` composer | attachments.jsx:434–451 | Textarea | 1 |
| Raw `<select>` for sort | artifacts.jsx:474 | Select | 1 |
| Search input | artifacts.jsx:465–472 | SearchInput | 1 |
| Hover-toggled download/file row | widgets.jsx:68–108, artifacts.jsx:263–282 | FileRow | 2 |

### Repeated layout patterns

- Gallery toolbar (search + sort + filter chips); media transport cluster (dock grid vs inline flex); WidgetFrame (surface-1 + r12 + mono header); modal action footer; centered empty-state; two-line media row; attachment thumbnail chip.

### Worst hotspots + inconsistencies

- **color (high):** error-red expressed 3 ways — `var(--err)` (mic icon), `oklch(0.70 0.18 25 / α)` (button border/bg), `#e35d5d` (favicon dot); scrim split `0.5` / `0.92`.
- **radius (high):** 6/10/12/14/16/20; WidgetFrame r12 and sheet r16 need tokens.
- **font (high):** 9/13/11/22/10/12px; new `--t-2xs` (9–10) needed.
- **spacing (high):** safe-area `env()` repeated raw; FAB `right: compact?16:24`.
- **Two `_MobileSheet` copies identical, not shared (high).**
- **Two desktop popovers identical (high).**
- **Play circle built 3× at 56/40/44px (med).**
- **Tag pill size varies 9/10/11px (med); icon-btn tap target 34/34/32 (med).**

---

## 6. settings

**Files reviewed:** `settings-primitives.jsx`, `settings-view.jsx`,
`settings-notifications.jsx`, `settings-voice.jsx`, `settings-chat.jsx`,
`settings-server.jsx`, `settings-terminal.jsx`
(The **better-designed** half. `settings-primitives.jsx` is a genuinely
well-built, purpose-made atom set with good a11y. The systemic issue is the
missing `disabled` prop on global `Button`.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| `_ActionButton` (full Button reimpl with disabled) | settings-notifications.jsx | window.Button | 5 |
| Defensive `window.Button ? … : <button>` fallbacks | settings-voice.jsx, settings-terminal.jsx | window.Button | 2 |
| `_FloatField` (float NumberField) | settings-voice.jsx | NumberField | 1 |
| `_SecretInputRow` (label+hint+input+save) | settings-notifications.jsx | FieldRow + TextInput | 2 |
| `SettingCard` vs global `Card` (r10 vs r-lg/14) | settings-primitives.jsx | Card | 20 |

### Repeated layout patterns

- Tab strip (sticky, Icon+label, URL-synced — the most complete version).
- Form field layout (label 14/500 + mono desc 11/fg-3).
- Section header with ScopeChip + description (SettingGroup).
- Inline error banner (oklch red tint + 11px mono).
- Loading placeholder (mono fg-3 11–12px).

### Worst hotspots + inconsistencies

- **Button disabled gap (high):** root cause of `_ActionButton` + the voice/terminal fallbacks; notifications file documents the gap at lines 32–35.
- **Label font-size drift (high/med):** 14px in primitives vs 13px in `_SecretInputRow` and `_scPromptEntry` for the same conceptual label.
- **Tab strip built 3 ways (high):** settings (sticky/Icon/URL), details ×2, tasks (`var(--accent, #818cf8)` fallback).
- **SettingCard r10 vs global Card r-lg/14 (med).**
- **Control-height drift (med):** SettingSelect/NumberField 38, Segmented 36, _ActionButton 32, Button sm 28 → misalign on shared rows.
- **ScopeChip `server` and `secrets` both use `var(--warn)` (low).**

---

## 7. library

**Files reviewed:** `library-detail.jsx`, `library-agent-panel.jsx`,
`library-github-panels.jsx`, `library-branch-panel.jsx`,
`library-overview-panels.jsx`, `library-overview-extras.jsx`,
`library-create-modal.jsx`, `library-md-editor.jsx`, `library-file-tree.jsx`,
`library-link-picker.jsx`, `library-secrets-panel.jsx`
(The most form-heavy bucket — highest density of raw inputs/textareas/selects.
`_LAP_INPUT_BASE` is the de-facto input standard but is file-local.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| Segmented control | github-panels.jsx:147–170, create-modal.jsx:189–209/303–326, github-panels.jsx:975–983 | HubSettings.Segmented | 4 |
| Text input (surface-2 + hairline + r8) | detail.jsx:759–769, agent-panel.jsx:62–67, github-panels.jsx:391–395/880–885, branch-panel.jsx:295–300, overview-extras.jsx:217–224, create-modal.jsx:178–184 | Input (none) | 10 |
| Textarea (mono + resize:vertical) | agent-panel, github-panels (×3), branch-panel (×2), overview-panels, md-editor, create-modal | Textarea (none) | 9 |
| Error/status banner | agent-panel:73–76, github-panels:174–185, branch-panel:237–249/333–338, overview-extras:151–160, secrets-panel:60–64, create-modal:329–335, md-editor:302–312 | StatusBanner (HubSettings-only) | 8 |
| Card section header eyebrow (11px uppercase 0.08em) | agent-panel, branch-panel, overview-panels/extras (multiple) | CardLabel/Eyebrow | 15 |
| Inline status badge (PAUSED/FAILING/GLOBAL…) | agent-panel:416–428/599–604, branch-panel:477/611–612 | Chip | 5 |

### Repeated layout patterns

- Two-column detail scaffold (1.4fr/1fr, 320px/1fr, 1fr/1fr).
- Card panel header row (title + spacer + count + actions) — 6+ times.
- List row `[icon/badge][title][meta chips][action]`.
- Form field (eyebrow label + control, gap:6) — 12+ times.
- Empty/unavailable state (mono fg-4, 12px, padding:22) — 15+ times.

### Worst hotspots + inconsistencies

- **color (high):** `oklch(0.30 0.10 25 / 0.18)` error bg in 8 files; `oklch(0.30 0.10 145 / 0.18)` success bg; `oklch(0.55 0.13 145)` success border; `#${color}33` GH label chip.
- **font (high):** 12.5 (mono textarea, 5+ files), 11.5 (commit text), 20 (detail title), 22 (icon input).
- **spacing (high):** `compact?16:24` body pad + `compact?100:24` bottom in 7 tab bodies; gap:14 inter-card; `12px 14px` panel header.
- **radius (high):** `borderRadius:8` dominant; 6/999/4 secondary.
- **Tab bar built 3 ways (high); Segmented 4 implementations differing 1–2px (high).**
- **Input height inconsistent `8px 12px` vs `10px 12px` (med); loading 3 idioms (med); file-tree imperative hover (low); tap targets ~24/30px below 44 (med).**

---

## 8. tasks-scheduler

**Files reviewed:** `tasks.jsx`, `scheduler-create-modal.jsx`,
`scheduler-detail.jsx`, `scheduler-directory.jsx`, `scheduler-detail-runs.jsx`,
`scheduler-create-modal-trigger.jsx`, `next-fires-preview.jsx`
(The most token-hungry bucket. Leans heavily on `--accent`/`--accent-soft`/
`--accent-line` for selected states — ~60 uses. Also runs its own parallel
red/amber color family.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| Local `Select` wrapper (raw `<select>`) | tasks.jsx (lines 317–338) | Select (none) | 18 |
| `_scmInputStyle` / `_sdvInputStyle` factories | scheduler-create-modal, scheduler-detail, scheduler-create-modal-trigger | Input (none) | 3 |
| RadioCard (radio styled as card) | scheduler-create-modal, scheduler-detail (402–418, 581–603) | RadioCard | 4 |
| Local Chip toggle / filter pill | scheduler-detail-runs:291, tasks.jsx, scheduler-create-modal | window.Chip | 5 |
| Accent pill / status badge (mono r999) | tasks.jsx:300/271, scheduler-directory:196, scheduler-detail-runs:107 | Badge | 12 |
| Empty-state block (icon+headline+body+CTA) | scheduler-directory:406–429, tasks.jsx:1851–1856/2177–2180 | EmptyState | 3 |
| Inline error banner | tasks.jsx, scheduler-create-modal, scheduler-detail-runs, scheduler-directory | AlertBanner | 8 |
| Segment-style tab strip from scratch | tasks.jsx, scheduler-directory, scheduler-detail, scheduler-create-modal-trigger | Tabs/TabStrip | 4 |

### Repeated layout patterns

- Page scaffold (header + sub-tab strip + scroll body).
- Toolbar (search + filter chips + spacer + actions).
- Form field (label above + control + hint).
- Kanban column (260–280px, surface-2, r-lg, DnD accent outline).
- List row `[meta][title+badges][actions]`.
- Review/detail KV row; modal form body (pad 16–18 + col gap 12 + error + fields + footer).

### Worst hotspots + inconsistencies

- **color (high):** PRIORITY_COLORS = 5 hex literals (`#dc2626`,`#ef4444`,`#f59e0b`,`#10b981`,`#60a5fa`); `var(--danger, #f87171)` + `var(--accent, #818cf8)` + `#fff` literals; scheduler raw oklch status tints.
- **font (high):** 13/12/15px hardcoded; `"JetBrains Mono", monospace` inline stack.
- **spacing (high):** card/modal/wizard pad 10/16/18; `compact?16:28` + `compact?100:28`.
- **radius (high):** `borderRadius:8` ubiquitous (inputs, tab strips, radio cards); 14 (icon box).
- **Four tab-strip implementations, none consistent (high).**
- **`var(--fg-1)` used heavily in tasks.jsx (Select/card title/input), undefined (high).**
- **Primary action button built two ways — raw `<button>` vs window.Button (high).**
- **Input height/padding inconsistent across forms (high); search bg surface-1 (scheduler) vs surface-2 (tasks) (med).**

---

## 9. secrets-skills-misc

**Files reviewed:** `secrets-components.jsx`, `secrets-view.jsx`,
`skills-directory.jsx`, `skills-detail.jsx`, `skills-install-modal.jsx`,
`agents-directory.jsx`, `session-switcher.jsx`
(Most sophisticated bespoke work — `secrets-components.jsx` is a full
self-contained DS for secrets. CaptchaGate / `_secUseCaptcha` / RevealModal are
legitimately domain-specific — keep private.)

### Bespoke re-implementations

| What | Files | Duplicates | ~count |
|---|---|---|---:|
| Inline text input (surface-2 + hairline + r8) | secrets-components, secrets-view, skills-install-modal | Input (none) | 14 |
| Segmented/pill tab strip | skills-install-modal (`_SimTabStrip`), secrets-view (`_GlobalTabs`), skills-detail | HubSettings.Segmented | 3 |
| Agent/item avatar circle (44–56px, accent-soft) | agents-directory:48–52, skills-directory:93–98, skills-detail:75–80 | AgentAvatar | 5 |
| Modal body/footer layout | secrets-components (`_SECStyles.modalBody`/`modalFooter`) | ModalBody/ModalFooter | 7 |
| Error box (oklch warm-red + `var(--err)`) | secrets-components, secrets-view, skills-directory, skills-install-modal, skills-detail | ErrorBox/StatusBanner | 8 |
| CheckRow (checkbox + icon + text, accent-soft active) | skills-install-modal:187–220, skills-detail:188–221 | CheckRow | 2 |

### Repeated layout patterns

- Page scaffold (`compact?16:28` / `compact?100:28` + SectionHeader).
- Card grid `repeat(auto-fill, minmax(280px,1fr))` gap:14.
- Section eyebrow label (mono, 11px, fg-3/fg-4, uppercase, 0.08em).
- Form field (flex-col gap:6 + label 12px fg-3).
- Back-nav breadcrumb row (chevron IconButton + mono breadcrumb).
- Loading text (mono fg-4 12–14px).
- Keyboard-shortcut footer (kbd chips + labels — session-switcher).

### Worst hotspots + inconsistencies

- **color (high):** `oklch(0.30 0.10 25 / 0.18)` error tint copy-pasted 5× in this bucket alone.
- **font (high):** 12/11/16/20/14/13px; type scale dead.
- **spacing (high):** gap 8/12/14; `compact?16:28` page pad in 4 views.
- **Two different tab-strip patterns for the same concept (high)** — `_GlobalTabs` underline vs `_SimTabStrip` pills.
- **Disabled button affordance 3 ways (high):** opacity 0.5/0.6 + `cursor:not-allowed` vs `pointerEvents:none`.
- **Loading state rendered 4 ways (med); section eyebrow color fg-3 vs fg-4 inconsistent (med); 3 mono font-stack spellings (low).**
