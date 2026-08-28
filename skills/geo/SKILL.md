---
name: geo
description: Zapytaj PRZEGLĄDARKĘ użytkownika, gdzie ten użytkownik fizycznie jest (navigator.geolocation przez dashboard), a potem zamień odczyt na adres albo listę najbliższych miejsc z OpenStreetMap. Ask the USER'S BROWSER for the user's device location, then reverse-geocode it or list nearby POIs. Use for "gdzie jestem", "moja lokalizacja", "gdzie się znajduję", "co jest najbliżej", "najbliższa apteka/stacja/bankomat", "ile mam do domu", "jaka jest tu pogoda", "where am I", "my location", "nearest X", "what's around me", "how far is X from me". DON'T use for the SERVER's location (this box is in a Helsinki datacenter — a geo fix says nothing about it), DON'T use when the user already named a city/address, and DON'T use for a place the user is only asking ABOUT.
metadata:
  emoji: 📍
---

# Geo — device geolocation (geo)

## 1. What this is

A round trip from an agent on this server to the **user's browser** and back:

```
agent → geo CLI → dashboard → SSE → browser (navigator.geolocation) → dashboard → CLI → agent
```

The CLI **blocks** until the browser answers, then prints ONE typed line. You
get a fix, or you get a refusal that tells you exactly what to do next — never
a hang, never a traceback.

Two extras ride on top of the fix (server-side, over public OSM services):
**reverse geocoding** (coordinates → street address) and **nearby** (POIs
sorted by distance).

## 2. WHEN to use — and when NOT

| USE `geo` for…                                       | DON'T — this is not a geo question                        |
|------------------------------------------------------|-----------------------------------------------------------|
| „gdzie jestem?" / „where am I?"                       | **Where is the SERVER?** → it's a Hetzner box in Helsinki (`hel1-dc2`, in CLAUDE.md). A browser fix is the USER, not the machine. |
| „moja lokalizacja", „gdzie się znajduję"              | The user already named a place („pogoda w Krakowie") → just use that city |
| „co jest najbliżej?", „najbliższa apteka/stacja/bankomat" | „gdzie jest Wawel?" — a question ABOUT a place, not about the user |
| „ile mam stąd do domu?" (needs the *stąd*)            | An address the user typed a moment ago → reuse it, don't re-ask |
| „jaka jest tu pogoda?" (needs *tu*)                   | Anything you can answer without knowing where the human stands |
| „nearest X", „what's around me", „how far am I from…" | A file/repo/project question that merely contains the word *location* |

Rule of thumb: reach for `geo` **only when the answer changes depending on
where the human is standing right now.** Every call can cost the user an OS
permission prompt — treat it like a tap you're spending.

Preflight with `geo status` before any **speculative** use: it never prompts.

## 3. How to invoke

```bash
CLI=~/.orchestrator/skills-registry/geo/scripts/geo_cli.py

# Preflight — is a browser even attached? Never prompts anybody.
python3 $CLI status

# The round trip. Address is ON by default (when geocoding is enabled).
python3 $CLI where --reason "najbliższa apteka"
python3 $CLI where --no-address                  # raw lat/lon, zero OSM egress
python3 $CLI where --accuracy coarse             # ~1 km rounding, faster fix
python3 $CLI where --max-age 0                   # force a FRESH measurement
python3 $CLI where --map --open                  # + a Leaflet map artifact

# What's around the user (resolves the fix cache-first, then asks OSM)
python3 $CLI nearby "apteka"
python3 $CLI nearby "kawiarnia" --radius 800 --limit 5 --map
python3 $CLI nearby "paczkomat" --json

# The last known fix — reads the server cache, NEVER prompts
python3 $CLI last

# Privacy lever: drop the cached fix + the geocode caches
python3 $CLI forget
```

Verbs and flags:

| Verb     | Flags                                                                                     | Prompts the user? |
|----------|-------------------------------------------------------------------------------------------|-------------------|
| `status` | `--session <uuid>` `--json`                                                                 | never             |
| `where`  | `--reason <s>` `--accuracy fine\|coarse` `--max-age <s>` `--timeout <s>` `--address`/`--no-address` `--map` `--open` `--session` `--json` | only on a cache miss |
| `nearby` | `<query>` `--radius <m>` `--limit <n>` + all of `where`'s fix flags                          | only on a cache miss |
| `last`   | `--json`                                                                                    | never (read-only route) |
| `forget` | `--json`                                                                                    | never             |

`--reason` is shown to the human — keep it short, honest and Polish
(„najbliższa apteka", not „geo request"). It is capped at 200 characters.

## 4. Output contract

One parseable echo line per call. Parse it; don't paste JSON into the chat.

```
fix · status=fixed lat=52.22968 lon=21.01223 acc=18.4m age=1.4s src=browser-gps prec=fine
addr · Marszałkowska 1, 00-001 Warszawa
map · id=art-20260801T184455-a3f9c1
map · UWAGA: manifest .artifacts/<id>.json ZAWIERA współrzędne i zostaje na dysku — `geo forget` go nie usuwa
```

```
nearby · query=apteka found=3 source=overpass radius=1500m cached=no
  1. Apteka Dbam o Zdrowie · 210 m · pharmacy · 52.23011,21.01402
```

```
no-fix · status=denied retryable=no
Użytkownik odrzucił prośbę o lokalizację.
hint: Nie pytaj ponownie w tej turze. Poproś o lokalizację tekstem.
```

`--json` prints the full typed payload instead. Exit codes:

| code | meaning                                                        |
|------|----------------------------------------------------------------|
| `0`  | a fix (`fixed` fresh, or `cached`), or a successful lookup      |
| `1`  | transport — the dashboard could not be reached at all           |
| `2`  | the human refused (`denied` / `device_opt_out` / `dismissed`)   |
| `3`  | asking is pointless right now (`no_browser`, `insecure_context`, `no_api`, `disabled`, no session, empty cache) |
| `4`  | nobody answered in time / transient device failure (`timeout`, `expired`, `aborted`, `unavailable`, `rate_limited`) |

## 5. THE ONE HARD RULE

> **`retryable=no` ⇒ do NOT call `geo` again this turn.**
> Read the `hint` line and say it to the user, in your own words.

`retryable=yes` means **one** more attempt is reasonable — typically with
`--accuracy coarse` — not a loop. Never retry more than once per turn, and
never retry a refusal at all.

Every status the server can hand you, verbatim:

| status | reason | retryable | message | hint |
|---|---|:-:|---|---|
| `fixed` | browser_fix | no | Pobrano lokalizację z przeglądarki użytkownika. | *(none)* |
| `cached` | cached_fix | no | Zwrócono ostatnio znaną lokalizację z pamięci serwera. | Podaj wiek odczytu (age_s). --max-age 0 wymusza świeży pomiar. |
| `pending` | awaiting_browser | yes | Prośba trafiła do przeglądarki — czekam na odpowiedź. | Odpytuj /wait tym samym request_id aż do odpowiedzi terminalnej. |
| `denied` | permission_denied | no | Użytkownik odrzucił prośbę o lokalizację. | Nie pytaj ponownie w tej turze. Poproś o lokalizację tekstem. |
| `device_opt_out` | device_opt_out | no | Na tym urządzeniu geolokalizacja jest wyłączona w ustawieniach. | Nie ponawiaj. Powiedz, że można to włączyć w Ustawienia → Serwer. |
| `dismissed` | user_dismissed | no | Użytkownik zamknął prośbę bez decyzji. | Nie ponawiaj automatycznie — zapytaj w czacie, czy podać lokalizację. |
| `insecure_context` | insecure_context | no | Przeglądarka działa po http:// — API geolokalizacji jest zablokowane. | Poproś użytkownika, żeby otworzył https://your-dashboard.example/. |
| `no_api` | no_geolocation_api | no | Przeglądarka nie udostępnia API geolokalizacji. | Nie ponawiaj — poproś o lokalizację tekstem. |
| `unavailable` | position_unavailable | yes | Urządzenie nie ustaliło pozycji (brak GPS/WiFi). | Spróbuj RAZ z --accuracy coarse. |
| `timeout` | browser_timeout | yes | Przeglądarka nie zdążyła ustalić pozycji. | Jedna próba z --accuracy coarse --timeout 30. |
| `expired` | request_expired | no | Nikt nie odpowiedział na prośbę o lokalizację. | Użytkownik nie zareagował — zapytaj o lokalizację w czacie. |
| `aborted` | request_aborted | no | Prośba o lokalizację została anulowana. | Ponów tylko jeśli użytkownik znów o to poprosi. |
| `no_browser` | no_browser_attached | no | Żadna przeglądarka nie jest podłączona do dashboardu. | Otwórz https://your-dashboard.example/ i poproś ponownie, albo poproś użytkownika o lokalizację tekstem. |
| `not_found` | unknown_request | no | Taka prośba o lokalizację nie istnieje (wygasła lub anulowana). | Zacznij od nowa — utwórz świeżą prośbę. |
| `rate_limited` | too_many_requests | yes | Za dużo próśb o lokalizację naraz. | Odczekaj retry_after_s sekund i spróbuj raz jeszcze. |
| `disabled` | feature_disabled | no | Geolokalizacja jest wyłączona na serwerze. | Włącz w Ustawienia → Serwer. |
| `geocode_disabled` | geocode_disabled | no | Reverse-geocoding jest wyłączony na serwerze. | Podaj surowe współrzędne albo włącz w Ustawienia → Serwer. |
| `geocode_failed` | upstream_error | yes | Serwis OSM nie odpowiedział poprawnie. | Spróbuj raz później albo podaj surowe współrzędne. |

The CLI adds a few rows the server cannot send (a socket error has no body):
`dashboard_unreachable`, `bad_response`, `route_missing`, `unauthorized`,
`no_session`, `no_cached_fix`, `no_artifact_cli`, `map_failed`. They carry the
same `retryable` + `hint` fields — read them the same way. `route_missing`
means the running dashboard predates this skill (it needs a restart) — it is
NOT „your request expired", so do not start a fresh one.

`pending` never reaches you: the CLI absorbs the non-terminal poll timeouts and
keeps polling the same request until something terminal arrives or `--timeout`
(default 180 s) runs out.

## 6. Staleness — say how old the reading is

The server keeps ONE fix in memory for 15 minutes. A follow-up question inside
that window answers instantly with `status=cached`, **no prompt at all**.

- `--max-age <s>` (default 300) — the oldest cached fix you'll accept. It is
  measured from when the DEVICE measured the position, not from when the server
  heard about it, so `age_s` is the real age of the reading.
- `--max-age 0` — force a fresh measurement (spends a prompt; use it only when
  the user says they moved).
- Always tell the user the age when it's `cached`: *„wg odczytu sprzed 47 s…"*.
  Reporting a 12-minute-old fix as *„jesteś tutaj"* is a lie.
- `geo last` reads the cache without ever prompting; `geo forget` clears it.
- Precision only ever goes DOWN: a `fine` fix in the cache is re-rounded to
  answer `--accuracy coarse`, but a `coarse` one never answers a `fine` ask —
  that request re-measures (and so spends a prompt). So don't reach for
  `--accuracy coarse` "just in case": it makes the next precise question cost a
  prompt.

## 7. Privacy — be honest about this

- The dashboard **never writes coordinates to disk by itself**. The fix lives
  in server memory only, for 15 minutes, and `geo forget` (or the Settings
  button) drops it immediately.
- **`--map` is the one exception, and it is permanent.** It shells the
  `artifacts` CLI, which writes `<agent cwd>/.artifacts/<id>.json` (global agent
  → `~/.orchestrator/artifacts/global/`) with the **exact lat/lon of the user
  and of every POI** embedded in the manifest. That file has no TTL, survives a
  restart, is NOT removed by `geo forget`, and in a repo that doesn't gitignore
  `.artifacts/` the next `git add -A` commits the user's home coordinates.
  Pass `--map` only when the user asked for a map, and say that it was saved.
- **But it DOES land in this session's transcript** the moment you print or
  quote it — the transcript is a normal `.jsonl` file on the box. Don't echo
  full precision into chat when the answer doesn't need it.
- **With geocoding on, the coordinates leave the box**: reverse-geocode and
  `nearby` send them to public OpenStreetMap servers (Nominatim / Overpass).
  `--no-address` keeps everything local; the server flag
  `geolocation_geocode_enabled` (Ustawienia → Serwer) turns the egress off
  entirely, and then the CLI just degrades to raw lat/lon.
- Prefer `--accuracy coarse` (~1 km) whenever a city-level answer is enough.
- Never store a fix in a file, a note, an issue or a commit unless the user
  explicitly asks for it.

## Storage & resolution

- **This skill keeps no state of its own** — no state file, no cache on disk.
  The single thing it can WRITE is a `--map` artifact, and that one contains
  coordinates (see Privacy above).
- Session id: `HD_SESSION_ID` → `ORCHESTRATOR_SESSION_ID` → the tmux session
  name (`hd-<uuid>` → `<uuid>`). Override with `--session <uuid>`.
- Dashboard URL: `HD_NOTIFY_URL` → `config.json` → `http://localhost:8766`.
- Auth token (sent as `X-Geo-Token`): `HD_ARTIFACT_TOKEN_FILE` →
  `~/.orchestrator/artifact_token` — the SAME token the artifacts skill uses.
- `--map` shells the `artifacts` skill's CLI (`HD_ARTIFACT_CLI` →
  `~/.orchestrator/skills-registry/artifacts/scripts/artifacts_cli.py`); if it
  isn't installed you get `no_artifact_cli` and the fix itself still prints.

## Resources

- **scripts/geo_cli.py** — argparse CLI (the entrypoint above).
- **scripts/geo_lib.py** — importable, stdlib-only Python API.
- **scripts/bootstrap.sh** — idempotent setup + an OFFLINE smoke test that
  pins the real invariant: no dashboard ⇒ typed answer, non-zero exit, no
  traceback.
- **config.json** — `{"dashboard_url": "..."}`.
