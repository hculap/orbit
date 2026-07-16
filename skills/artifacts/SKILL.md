---
name: artifacts
description: Twórz bogate, trwałe "artefakty" (wykresy, mapy, embedy YouTube, klipy audio/wideo, interaktywny HTML, wygenerowane obrazy, pliki do pobrania) zamiast wklejać surowe dane do czatu. Create rich, persistent artifacts — Chart.js charts, Leaflet maps, YouTube embeds, audio/video clips, interactive HTML, generated images, downloadable files — surfaced in the orbit as a toast/modal + gallery. Use whenever a deliverable is visual, interactive, playable, downloadable, or worth keeping: "make a chart", "show this on a map", "embed this video", "build an interactive page", "generate an image", "narysuj wykres", "pokaż na mapie", "zrób interaktywną stronę". DON'T use for plain prose, short replies, or code snippets the user copies inline.
metadata:
  emoji: 🎨
---

# Artifacts (artifacts)

## 1. What this is

Artifacts are **rich, persistent, committable deliverables** — not chat text.
Each one is a JSON manifest (`<id>.json`) plus an optional payload file
(`<id>.<ext>`), saved into the project's `.artifacts/` directory. The
orbit watches for them and surfaces each as a **toast** (or a
**modal** when `--open`) plus a persistent **gallery**.

Plain answers stay in chat as markdown. Reach for an artifact only when the
output is visual, interactive, playable, or downloadable — something worth
keeping around and re-opening later.

## 2. WHEN to make an artifact vs. just write markdown

| MAKE an artifact for…                                    | DON'T — keep it in chat                                  |
|----------------------------------------------------------|----------------------------------------------------------|
| A chart (Chart.js spec: line/bar/pie/doughnut/scatter)   | Plain prose / a normal answer                            |
| A map (Leaflet: markers, route, center)                  | A short reply or single fact                             |
| A YouTube embed                                          | A code snippet the user copies inline → fenced markdown  |
| An audio clip (TTS output, recording)                    | A tiny 2–3 row table                                     |
| A video clip                                             | A casual mention of a link                               |
| A rich / interactive HTML page                           | Anything you'd normally just say                         |
| A generated image                                        |                                                          |
| A downloadable file (CSV, PDF, zip, dataset…)            |                                                          |

Rule of thumb: if the user would want to **look at it, play it, interact with
it, or download it** — make an artifact. If they'd just **read it** — markdown.

## 3. Type taxonomy

| `--type`  | ext            | Content source                                            |
|-----------|----------------|-----------------------------------------------------------|
| `chart`   | — (inline)     | JSON Chart.js spec, stored inline in `manifest.extra`     |
| `map`     | — (inline)     | JSON Leaflet spec, stored inline in `manifest.extra`      |
| `youtube` | — (inline)     | JSON `{video_id,start}`, a bare 11-char id, or a URL      |
| `html`    | `.html`        | Raw HTML document written to `<id>.html` (≤ 200 KB)       |
| `image`   | (from file)    | Existing image file → **copied** into the dir             |
| `audio`   | (from file)    | Existing audio file → **copied** into the dir             |
| `video`   | (from file)    | Existing video file → **copied** into the dir             |
| `file`    | (from file)    | Any downloadable file → **copied** into the dir           |

`chart` / `map` / `youtube` have `src: null` — the spec lives inline. The other
types have a real payload file and `src: "<id>.<ext>"`.

## 4. Spec shapes

**chart** — a Chart.js config:

```json
{
  "chart_type": "line|bar|pie|doughnut|scatter",
  "data": { "labels": ["Jan", "Feb"], "datasets": [{ "label": "Sales", "data": [10, 20] }] },
  "options": { "plugins": { "title": { "display": true, "text": "Q1" } } }
}
```

Limits: ≤ 10 datasets, ≤ 500 points total. `options` must be JSON-serialisable —
**no JavaScript function strings** (the dashboard renders the spec as data).

**map** — a Leaflet config:

```json
{
  "center": [52.2297, 21.0122],
  "zoom": 12,
  "markers": [{ "lat": 52.2297, "lng": 21.0122, "label": "Warsaw" }],
  "route": [[52.2297, 21.0122], [52.4064, 16.9252]]
}
```

**html** — a complete document, externals via CDN only:

```html
<!doctype html><html><head><meta charset="utf-8"><title>…</title></head>
<body>…</body></html>
```

Must be ≤ 200 KB. Self-contained: inline CSS/JS or CDN `<script>`/`<link>` only.

## 5. How to invoke

```bash
CLI=~/.orchestrator/skills-registry/artifacts/scripts/artifacts_cli.py

# Chart (inline spec from a file), pop the modal
python3 $CLI create --type chart --title "Q1 Sales" --spec-file spec.json --open

# Map (inline spec from stdin)
echo '{"center":[52.23,21.01],"zoom":11,"markers":[]}' \
  | python3 $CLI create --type map --title "Warsaw" --stdin

# YouTube (bare id, URL, or {video_id,start} all work)
python3 $CLI create --type youtube --title "Demo" dQw4w9WgXcQ

# Interactive HTML page (raw HTML from a file)
python3 $CLI create --type html --title "Dashboard" --spec-file page.html --open

# Image / audio / video / file — pass an EXISTING path; it gets copied
python3 $CLI create --type image --title "Diagram" /tmp/diagram.png --open
python3 $CLI create --type audio --title "Narration" /tmp/voice.mp3
python3 $CLI create --type file  --title "Export"   /tmp/data.csv

# Manage existing artifacts
python3 $CLI open   art-20260529T183001-a3f9c1
python3 $CLI list                       # all artifacts in the dir (human table)
python3 $CLI list --session --json      # only this session, as JSON
python3 $CLI dup    art-20260529T183001-a3f9c1
python3 $CLI edit   art-20260529T183001-a3f9c1 --title "New title"
python3 $CLI delete art-20260529T183001-a3f9c1
```

Spec / HTML input is one of: a positional arg (path **or** raw text),
`--spec-file <path>`, or `--stdin`.

## 6. Auto-open / toast semantics

- **`--open`** → the dashboard pops a **modal** in the browser immediately.
- **without `--open`** → the user gets a **toast** notification and finds the
  artifact later in the **gallery**.

Either way the artifact is persisted to disk first; the dashboard notify is
best-effort. If the dashboard is offline the echo prints `pushed=no` and the
artifact still exists on disk (it gets picked up on the next scan).

## 7. The generate-image flow

To produce an image artifact, first generate the file, then register it:

```bash
# 1. run the image generator (skill / tool) → it prints a path
#    e.g. the generate-image skill emits  MEDIA: /tmp/out.png
# 2. register the produced file as an artifact
python3 $CLI create --type image --title "Sunset over the Tatras" /tmp/out.png --open
```

Same pattern for audio (e.g. the `generate-audio` skill's `MEDIA: <path>`):
`create --type audio --title "…" <produced-path>`.

## 8. Output contract

`create` prints **exactly one** echo line — parse it, don't paste file paths
into the chat reply:

```
saved · id=art-20260529T183001-a3f9c1 · type=chart · pushed=yes
```

- `id` — the artifact id (use it for `open` / `dup` / `edit` / `delete`).
- `type` — the artifact type.
- `pushed` — `yes` if the dashboard accepted the notify, `no` if it was offline
  (the artifact still exists on disk).

Other commands echo `opened · …`, `duplicated · …`, `edited · …`, `deleted · …`.

**Never** paste the `.artifacts/<id>.json` path into the chat. The dashboard
shows the artifact; just tell the user what you made.

## Storage & resolution

The directory is keyed on **`HD_LIB_ID`**, **not** the process's live cwd — so
artifacts always land where the dashboard scans, even if you've `cd`'d into a
scratchpad or elsewhere before running the CLI. **You do not need to `cd`
anywhere first.**

- **Per-agent session** (`HD_LIB_ID` set, e.g. `areas/Praca`): artifacts go in
  the agent's PARA dir → `~/Areas/Praca/.artifacts/` (committable with the
  project). The dashboard derives the same dir from the same lib_id, so the
  gallery always finds them.
- **Global agent** (`HD_LIB_ID` unset/empty) — or a lib_id that doesn't resolve
  to a real PARA dir — artifacts go in `~/.orchestrator/artifacts/global/`. A
  `.artifacts/` dir is **never** created inside `$HOME` or a stray cwd.

Session id is discovered from `HD_SESSION_ID` → `ORCHESTRATOR_SESSION_ID` →
the tmux session name (`hd-<uuid>` → `<uuid>`). The dashboard URL comes from
`HD_NOTIFY_URL` → `config.json` → `http://localhost:8766`. An optional auth
token is read from `HD_ARTIFACT_TOKEN_FILE` → `~/.orchestrator/artifact_token`
and sent as the `X-Artifact-Token` header.

## Resources

- **scripts/artifacts_cli.py** — argparse CLI (the entrypoint above).
- **scripts/artifacts_lib.py** — importable, stdlib-only Python API.
- **scripts/bootstrap.sh** — idempotent setup + offline smoke test.
- **config.json** — `{"dashboard_url": "..."}`.
