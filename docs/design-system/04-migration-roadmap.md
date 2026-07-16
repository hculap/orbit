# Design System — Part 4: Adoption & Migration Roadmap

> Status: **the plan** (sequencing + guardrails). How the model from Part 3 lands
> in the codebase without a big-bang rewrite. The ordering principle: ship the
> zero-risk foundation and the root-cause primitive fixes now; stage the
> high-volume literal sweeps as per-bucket, browser-verified follow-up.

## The tranche plan

Ordered by leverage and blast radius — each tranche is independently shippable,
and every step gives the next one real tokens/primitives to target.

- [ ] **TRANCHE 0 — tokens.css v2 (token fixes only).** Define `--fg-1`; alias
  `--danger`/`--warning`/`--bg-2`/`--mono`/`--accent-dim`; add `--r-control:8`/
  `--r-widget:12`/`--r-pill`; add `--t-2xs`/`--t-md`/`--t-code`; add status
  soft/line/bg, `--scrim`, control-heights, the z-index scale, motion tokens.
  Pure additive CSS, **no JSX changes, zero render risk** — instantly un-breaks
  the 32 `--fg-1` sites and gives every later step real tokens to target.

- [ ] **TRANCHE 1 — enhance the 3 root-cause primitives.** Add `disabled`/`busy`
  to Button, `step` to NumberField, `gap`/`padding`/`variant` to Card. Smallest
  diff, deletes the most-visible reimplementations (`_ActionButton`,
  `_FloatField`, the voice/terminal fallbacks).

- [ ] **TRANCHE 2 — prove it in SETTINGS first.** It's the best-built bucket,
  lowest blast radius, self-contained, with the strongest existing primitive
  discipline. Promote StatusBanner/Segmented/ScopeChip/Select/Field to global
  with HubSettings aliases; swap label-size drift to `--t-md`. Validates the
  promotion+alias pattern without touching high-traffic chrome.

- [ ] **TRANCHE 3 — add the new global form primitives.** Add Input, Textarea,
  Select, Field, Tabs, EmptyState, Badge, Popover in components.jsx load order,
  then adopt them in LIBRARY (the most form-heavy, highest-density bucket —
  biggest ROI per primitive). `_LAP_INPUT_BASE` is already the de-facto standard
  there, so swap-in risk is low.

- [ ] **TRANCHE 4 — structural dedup (highest LOC payoff, zero behavior change).**
  Migrate PinSheet + both `_MobileSheet` → BottomSheet, and all desktop popovers
  → Popover (orch-media + orch-terminal).

- [ ] **TRANCHE 5 — unify the shell nav.** NavItem/NavGroup/Avatar/PageScaffold +
  wire the dead unread Badge. This is the most-rendered chrome, so it goes last
  — after the patterns are battle-tested.

- [ ] **TRANCHE 6 — sweep the long tail.** ListRow/EmptyState/Tabs across
  tasks-scheduler, details, secrets-skills; replace `#818cf8` and the
  fontSize/spacing literals bucket by bucket.

---

## What ships on this branch vs what is staged

### Ships now on `feature/design-system` (for review)

| Item | Tranche | Why it's safe now |
|---|---|---|
| **tokens.css v2** | 0 | Pure additive CSS, no JSX touched — un-breaks the 6 undefined tokens with zero render risk. |
| **Button / NumberField / Card enhancements** | 1 | Root-cause fixes; small diffs; deletes the most-visible reimplementations. |
| **The new global primitives library** | 3 (defs) | Adds Input/Textarea/Select/Field/Tabs/EmptyState/Badge/Popover to `window` in correct load order — additive; nothing breaks until adopted. |
| **The living `/design` styleguide** | — | Renders every token + primitive on one page so reviewers can eyeball the system and future authors can copy from it. |
| **Safe settings reconciliation (promote + alias)** | 2 | Promotion keeps thin `HubSettings.*` aliases, so the external settings contract (read by orchestrator-unread/-conversation) stays intact. |
| **A representative proof-of-adoption** | 2–3 | One bucket (settings, plus a library form) converted end-to-end to prove the primitives in real use, not just in the styleguide. |

### Staged follow-up (per-bucket, browser-verified)

The **per-file literal sweeps** — `fontSize` → `--t-*`, spacing → semantic
aliases, `#818cf8`/`#f87171`/`#fbbf24` → tokens, across all ~70 files — are
**STAGED**, not shipped in one pass.

**Why staged (from the risks):** there is no type checker (JSX runs via
Babel-standalone at runtime), so a wrong token name **fails silently** — it
renders nothing or inherits, with no compile error. Mass literal→token edits are
high-volume mechanical changes where a single typo is invisible until someone
looks at that exact screen. Therefore: do them **per bucket, verify in-browser,
never as one giant find/replace.** The tranche order (4 → 5 → 6) sequences these
sweeps behind the now-shipped foundation so each batch targets real, defined
tokens and battle-tested primitives.

---

## Remove / consolidate checklist

- [ ] **Dead/undefined tokens** — `--fg-1` (define), `--danger`/`--warning`
  (alias to `--err`/`--warn`, sweep tasks.jsx fallbacks), `--bg-2` (alias
  `--surface-1`, drop app.jsx fallback), `--mono` (alias `--font-mono`),
  `--accent-dim` (alias `--accent-soft`), `--vvh` (define or remove the 3
  app.jsx refs).
- [ ] **`#818cf8` (28×)** — the pre-oklch hardcoded violet; delete every
  occurrence, replace with `var(--accent)`.
- [ ] **Type-scale gap literals** — add `--t-2xs`/`--t-md`/`--t-code`, then sweep
  the top-5 fontSize literals (12/11/13/10/14) to tokens (the single largest
  literal cleanup, ~879 → tokens).
- [ ] **`_ActionButton`** + the two defensive `window.Button ? : <button>`
  fallbacks — once Button has `disabled`.
- [ ] **`_FloatField`** — once NumberField gains `step`/`parseFloat`.
- [ ] **Both `_MobileSheet` copies + `PinSheet`** — replace with
  `window.BottomSheet` (~250 LOC).
- [ ] **`_DesktopShell`/`_DesktopPopover`/`DesktopPopover`** — replace with
  `window.Popover`.
- [ ] **Local input-style factories** (`_SECInputStyle`, `_simInputStyle`,
  `_scmInputStyle`, `_sdvInputStyle` + its window delegate, `_LAP_INPUT_BASE`)
  → one `window.Input`.
- [ ] **Local `SpeakButton` + `MessageActionButton` + `_McIconBtn` +
  `_DockIconBtn`** → IconButton with active/disabled.
- [ ] **`bytesShort` + `fpBytesShort`** → canonical `window.fmtBytes`.
- [ ] **3 `ThinkingDots` implementations** → one `window.ThinkingDots(size)`.
- [ ] **4 bespoke Segmented + ~15 tab strips** → `window.Tabs`/`window.Segmented`.
- [ ] **Inline `<style>{@keyframes}` blocks** (ThinkingDots, ConversationModal)
  → move into tokens.css to avoid duplicate injection on re-render.
- [ ] **Imperative hover style mutations** (logs-view, file-tree, KebabMenu) →
  React-state pattern inside the extracted primitives.

---

## Risks & guardrails

| Risk | Guardrail |
|---|---|
| **No bundler / no imports** — every primitive must publish to `window` and load before consumers. | Verify index.html/template load order before moving anything; promote into components.jsx (loads first); keep `HubSettings.*` aliases. |
| **Settings contract is external API** — `useSettings`/`useServerSettings` + `HubSettings.*` are read off `window` by orchestrator-unread/-conversation. | Promotions must leave thin aliases in place; deleting the `HubSettings` names would break unread/conversation silently. |
| **`--danger`→`--err` is a hue shift** — tasks.jsx red moves from `#f87171` (sRGB) to `oklch(0.70 0.18 25)`; same for `#818cf8`→`--accent`. | Acceptable (it's the unification goal) but eyeball it. |
| **Defining `--fg-1` brightens 32 sites** that currently inherit `--fg`. | Intended, but a real visual change across orch/tasks/details titles — review screenshots. |
| **Mass literal→token sweeps fail silently** (no type checker; wrong token name renders nothing/inherits). | Do per-bucket, verify in-browser, never one giant find/replace. |
| **`compact` is the only breakpoint mechanism** (no media queries in JSX). | PageScaffold and every layout helper MUST keep accepting/threading `compact`, not hardcode a single layout. |
| **44px tap-target bumps can break dense mobile layouts** (soft-keyboard 38px keys, 22px message buttons). | 38px key height is a conscious tradeoff — treat 44px enforcement as **opt-in per surface**, not a blanket change. |
| **BottomSheet migration must preserve per-sheet behaviors** (PinSheet drag handle, safe-area footer, the MessageActionRow opacity-fade-not-display-none pattern). | Verify each sheet's specifics survive the swap. |
| **Injected-iframe terminal CSS can't use `var()`.** | `--terminal-bg` needs a parallel JS constant (`HUB_TERMINAL_BG`); don't assume the CSS token reaches the iframe. |
| **`--r-control:8` is a new token, not a remap of `--r-md`.** | Deliberate — to avoid silently shifting all current `--r-md:10` card usages to 8. Do not collapse them or cards change shape. |

---

## Top inconsistencies tracker

`status` = **fixed in tokens v2** for token-level issues resolved by Tranche 0;
**staged** for issues that need JSX/primitive work in later tranches.

| Issue | Impact | Fix | Status |
|---|---|---|---|
| `--fg-1` referenced 32× but never defined → silently inherited on emphasized titles | high | Add `--fg-1: #f5f3ee` (one notch brighter than `--fg`) | fixed in tokens v2 |
| tasks.jsx runs a parallel red/amber family (`#f87171` ×12, `#fbbf24`) via undefined `--danger`/`--warning` | high | Define `--danger:var(--err)`/`--warning:var(--warn)`; sweep tasks.jsx fallbacks | fixed in tokens v2 (token) / staged (fallback sweep) |
| `borderRadius:8` most common radius (106×) with no token | high | Add `--r-control:8`; map inputs/buttons/badges/chips | fixed in tokens v2 (token) / staged (sweep) |
| `window.Button` has no `disabled` prop → `_ActionButton` reinvented + voice/terminal fallbacks + ~30 raw accent buttons in tasks | high | Add `disabled`/`busy` to Button; delete reimplementations | staged (Tranche 1) |
| BottomSheet reimplemented 3× (PinSheet + two `_MobileSheet`) — ~250 LOC of drift-prone boilerplate | high | Migrate all three to `window.BottomSheet` | staged (Tranche 4) |
| No global Popover → 5 hand-rolled outside-click+Esc+positioning | high | Add `window.Popover`; route all 5 through it | staged (Tranche 4) |
| Tab strip implemented ~15 ways with padding/font drift + some missing `marginBottom:-1` | high | One `window.Tabs` (underline/pills); promote settings' sticky URL-synced base | staged (Tranche 2/6) |
| `DrawerItem` vs `SidebarItem` — same nav item twice, silent divergence | high | One `window.NavItem` + NavBadge | staged (Tranche 5) |
| Type scale dead (1×) while 879 fontSize literals exist; gaps at most-used sizes (10/14) | high | Add `--t-2xs`/`--t-md`/`--t-code`, then sweep top-5 sizes | fixed in tokens v2 (tokens) / staged (sweep) |
| Spacing scale used 0×; `compact?16:28` + `compact?100:28` repeated ~26× | high | Add `--page-pad`/`--page-pad-compact`/`--nav-clearance` + PageScaffold | fixed in tokens v2 (tokens) / staged (PageScaffold) |
| Error/ok/warn tint colors copy-pasted across 8+ files; no soft/line/bg status tokens | high | Add `--err-soft`/`--err-line`/`--err-bg`, `--ok-soft`/`--ok-line`, `--warn-soft`/`--warn-line` | fixed in tokens v2 (tokens) / staged (sweep) |
| Control heights drift 28/32/34/36/38/42; tap targets 22/32/36/38 below 44px | med | Add `--control-h:38`/`--control-h-sm:36`/`--control-h-touch:44`; enforce touch min on compact | fixed in tokens v2 (tokens) / staged (enforcement) |
| Backdrop opacity 3 values; terminal bg 3 dark literals | med | Add `--scrim`/`--scrim-heavy` + `--terminal-bg` (+ JS mirror) | fixed in tokens v2 (tokens) / staged (sweep) |
| Unread plumbing fully wired but never rendered — dead feature | med | Render via Badge(`variant='count'`) on NavItem | staged (Tranche 5) |
| Hover two ways — React useState vs imperative `e.currentTarget.style` mutation | med | Standardize on useState hover inside the extracted ListRow/LogPane | staged (Tranche 4/6) |
