# Design System — Part 1: The Current System, As-Is

> Status: **as-built audit** (descriptive, not prescriptive). This is the honest
> snapshot the design-system work starts from. Measurements taken on branch
> `feature/design-system` across `src/orbit/static/` (64 JSX files +
> `tokens.css`, ~34.5k lines).

## 1. How styling works today

The app is **CDN React 18 + Babel-standalone with no build step**. There is no
Tailwind, no CSS-in-JS library, no bundler. Styling is done two ways:

1. **`tokens.css`** (167 lines) — a single global stylesheet of CSS custom
   properties (the design tokens) + a base reset + ~8 utility classes
   (`.mono`, `.muted`, `.kbd`, `.scroll-hide`, `.skel`, `.live-dot`, `.caret`,
   `.fade-in`) + a few keyframes + live-app viewport overrides.
2. **Inline `style={{}}` objects** in every JSX file, referencing the tokens via
   `var(--…)`. Components publish themselves to `window` so sibling JSX files
   (loaded as ordered `<script type="text/babel">` tags in `index.html`) can use
   them without imports.

This paradigm is deliberate and **must be preserved** by the design system — the
no-build, window-published, inline-style model is the constraint, not a target
for replacement.

## 2. The token foundation (`tokens.css`)

Genuinely good bones. Editorial-quiet, dark-first, violet accent, Helvetica +
JetBrains Mono.

| Group | Tokens |
|---|---|
| Surfaces | `--bg`, `--surface-1/2/3`, `--hairline`, `--hairline-strong` |
| Text | `--fg`, `--fg-2`, `--fg-3`, `--fg-4` |
| Accent | `--accent` (oklch violet), `--accent-soft`, `--accent-line`, `--accent-fg` |
| Status | `--ok`, `--warn`, `--err`, `--info` |
| Type family | `--font-sans`, `--font-mono` |
| **Spacing** | `--s-1`…`--s-10` (4 → 72px) |
| **Radii** | `--r-sm:6`, `--r-md:10`, `--r-lg:14`, `--r-xl:20` |
| Shadows | `--shadow-1`, `--shadow-2` |
| **Type scale** | `--t-display:40`, `--t-h1:28`, `--t-h2:20`, `--t-h3:16`, `--t-body:15`, `--t-sm:13`, `--t-xs:11` |

## 3. The component layer (TWO parallel systems)

### 3a. `components.jsx` — the global primitive library (739 lines)
Published to `window`. ~24 primitives:

- **Icon** — one big `switch` with ~110 stroke icons, 20px default.
- **Button** — variants `primary | ghost | quiet | danger`; sizes `sm | md | lg`.
- **IconButton** — square ghost button with a delayed custom tooltip.
- **Card**, **Chip**, **StatusDot**, **ProgressBar**, **Sparkline**,
  **SectionHeader**, **SearchInput**, **FileIconBox**.
- **Modal**, **BottomSheet**, **KebabMenu**, **PullToRefresh**, **Toast**
  (`ToastProvider`/`useToast`), **ComingSoonView**.
- Utilities: `fmtBytes`, `relTime`, `apiUrl`, plus the `SECTIONS` registry.

### 3b. `settings-primitives.jsx` — a SECOND atom set (429 lines)
Published under `window.HubSettings.*` (a few also to bare `window`). Built for
the Settings redesign but conceptually overlaps the global layer:

- **Toggle** (44px tap target), **Segmented**, **SettingSelect**, **NumberField**.
- **SettingCard**, **SettingRow**, **ToggleRow**, **FieldRow** (label+control+hint
  layouts), **SettingGroup**, **ScopeChip**, **AdvancedDisclosure**,
  **StatusBanner**.

These are real, well-built form/layout primitives — but they exist as a
Settings-only island. The same needs (an input, a select, a card, a labelled
field row, a banner) recur app-wide and are re-implemented inline elsewhere.

## 4. The gap between tokens and reality (measured)

The tokens exist; most code **bypasses them**. Hard counts across all `*.jsx`:

| Signal | Count | Verdict |
|---|---:|---|
| Inline `style={{}}` blocks | **1,931** | the styling surface area |
| `var(--token)` references | ~1,996 | tokens *are* used… |
| Hardcoded `fontSize: N` | **879** | …but type is hardcoded |
| `--t-*` type-scale used in JSX | **1** | **type scale is dead** |
| `--s-*` spacing-scale used in JSX | **0** | **spacing scale is dead** |
| `borderRadius: 8` (literal) | **106** | most common radius — **not a token** |
| `borderRadius: 6 / 999 / 4` (literal) | 78 / 54 / 19 | radius tokens half-bypassed |
| `var(--fg-1)` references | **32** | **undefined token** (only `--fg`/`-2/-3/-4` exist) → silently renders inherited color |
| `#818cf8` (literal) | **28** | the violet accent, re-hardcoded instead of `var(--accent)` |
| `#f87171` (literal) | 11 | error red, re-hardcoded |
| `oklch()` / `rgba()` / `#hex` literals | 81 / 41 / 79 | scattered raw colors |
| Raw `<button>` | **156** | re-implement `Button`/`IconButton` |
| Raw `<input>` | **84** | no global input primitive exists |
| Raw `<textarea>` | **26** | no global textarea primitive exists |
| Raw `<select>` | **22** | only `SettingSelect` (Settings-scoped) exists |

Most common hardcoded font sizes: `12` (273×), `11` (245×), `13` (172×),
`10` (96×), `14` (35×). Most common hardcoded radii: `8` (106×), `6` (78×),
`999` (54×).

## 5. The five structural problems

1. **Dead scales.** The `--t-*` type scale and `--s-*` spacing scale are defined
   but unused. Designers' intent (an 8-step type ramp, a 4-based spacing ramp)
   never reached the components — everyone hardcodes `fontSize: 12` / `gap: 8`.
2. **Token bypass for values that ARE tokenized.** Radius `8` (the most common)
   isn't in the scale; the accent and error colors are re-hardcoded as hex.
3. **A broken token in production.** `var(--fg-1)` (32 uses) is undefined.
4. **Missing primitives.** No global `Input`, `Textarea`, `Select`, `Field`,
   `Label`, `Tabs`, `EmptyState`, `Badge`, `Divider`, `Tooltip`, `Spinner`,
   `Skeleton`, `Drawer`, or layout helpers (`Stack`/`Inline`). So 156 `<button>`,
   84 `<input>`, etc. are bespoke.
5. **Two parallel systems.** `components.jsx` and `settings-primitives.jsx`
   solve overlapping problems independently, so the same concept (a card, a row,
   a select, a banner) looks/behaves differently inside vs outside Settings.

## 6. What is deliberately good (keep)

- The token *vocabulary* (surfaces / text / accent / status / shadows) is
  coherent and worth keeping verbatim.
- `Button`, `IconButton`, `Card`, `Modal`, `BottomSheet`, `KebabMenu`, `Toast`,
  `Icon` are solid and widely adopted (`Button` 107×, `Card` 74×, `IconButton`
  38×, `Modal` 20×).
- The `window`-publish + ordered-`<script>` model works and is the right shape
  to extend.
- The Settings atoms (`Toggle`, `SettingRow`, `FieldRow`, `SettingGroup`,
  `ScopeChip`, `AdvancedDisclosure`) are good designs — candidates for promotion,
  not deletion.

*(Per-file bespoke-pattern catalog — appended from the parallel audit in
Part 2.)*
