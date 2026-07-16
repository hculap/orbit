---
name: generate-image
description: Generate an image from a text prompt via Gemini. Use when user says "wygeneruj obrazek", "zrób obrazek", "generate image", "stwórz grafikę", "make a picture", or asks for any image to be created from a description.
metadata: {"clawdbot":{"emoji":"🎨","requires":{"bins":["uv"],"env":["GEMINI_API_KEY","ORCHESTRATOR_SESSION_ID"]},"primaryEnv":"GEMINI_API_KEY"}}
---

# generate-image

Generate an image from a text prompt (with optional reference images already attached
to the current session) via Gemini, save it to the per-session uploads dir, and emit
a single `MEDIA: <abs-path>` line on stdout for the orchestrator to pick up.

## How it works

1. Resolves `~/.orchestrator/uploads/<ORCHESTRATOR_SESSION_ID>/` (creates if missing).
2. Validates each `--reference` arg lives **inside** that session dir (no traversal,
   no leaking arbitrary server files), opens it as RGB via PIL.
3. Calls Gemini `generate_content` with `response_modalities=["TEXT","IMAGE"]` and
   `image_config(image_size, aspect_ratio)`. Primary model is
   `gemini-3.1-flash-image-preview`; on `503/unavailable` the script transparently
   retries on `gemini-3-pro-image` (without `image_config`, mirroring pati-photo).
4. Extracts the first `inline_data.data` part from the response, base64-decodes if
   needed, flattens RGBA→RGB on white, saves PNG into the session uploads dir as
   `gen-<utc-iso>-<6char-uuid>.png`.
5. Prints `MEDIA: <abs-path>` to stdout (single contract line). Diagnostics → stderr.

## Examples

Basic text-to-image:

```bash
uv run {baseDir}/scripts/generate_image.py \
  --prompt "pomarańczowy kot w skafandrze, fotorealistyczny, neutralne tło"
```

Override the model:

```bash
uv run {baseDir}/scripts/generate_image.py \
  --prompt "low-poly fox on a snowy hill, soft lighting" \
  --model gemini-3-pro-image
```

Image-to-image with two references (user-attached photos blended into the prompt):

```bash
uv run {baseDir}/scripts/generate_image.py \
  --prompt "blend these two in a noir, high-contrast, black-and-white style" \
  --reference $HOME/.orchestrator/uploads/<sid>/photo-a.jpg \
  --reference $HOME/.orchestrator/uploads/<sid>/photo-b.png
```

## Parameters

| Flag             | Required | Default                          | Notes                                                                  |
|------------------|----------|----------------------------------|------------------------------------------------------------------------|
| `--prompt`       | yes      | —                                | Free-form description of the desired image.                            |
| `--model`        | no       | `gemini-3.1-flash-image-preview` | Override the Gemini image model.                                       |
| `--aspect-ratio` | no       | `1:1`                            | E.g. `1:1`, `3:4`, `4:3`, `16:9`, `9:16`.                              |
| `--resolution`   | no       | `1K`                             | One of `1K`, `2K`, `4K`.                                               |
| `--reference`    | no       | (none)                           | Repeatable. Absolute path to an image **inside** the session uploads dir. |

## When to use references

If the current turn's `<attached>` block contains image files
(`.png` / `.jpg` / `.jpeg` / `.webp` / `.gif`), pass each absolute path as a separate
`--reference <abs-path>` flag. Multiple references are combined with the prompt by
Gemini (images first, then the text prompt).

Skip non-image attachments (PDF / text / etc. — those still go through the normal
`Read` tool flow). Don't pass `--reference` paths that aren't already in the session
uploads dir; the script will reject them with a clear error.

## Output contract

After running the script, parse the single `MEDIA: <abs-path>` line from stdout, then
**register the PNG as an artifact** (do NOT emit any JSON block — there is no chat
envelope anymore; raw JSON renders as text):

```bash
python3 ~/.orchestrator/skills-registry/artifacts/scripts/artifacts_cli.py \
  create --type image --title "<short caption>" --open "<abs-path-from-MEDIA>"
```

That pops the image in the dashboard (a modal via `--open`) and adds it to the
gallery. Then write a one-line plain-markdown reply (e.g. "Gotowe — <caption>."). Do
**not** paste the file path or any `{"type":"image",...}` JSON into your reply.

## Anti-patterns

- Don't pass `--reference` paths outside the active session uploads dir. The script
  rejects them; even a `..`-traversal attempt is caught after `.resolve()`.
- **Don't emit a `{"type":"image",...}` JSON block or paste the file path** — register
  the image with the `artifacts` CLI (above). Raw JSON just shows as text in the terminal.
- Don't generate an image for casual mentions ("zdjęcie kota by się przydało") —
  only when the user explicitly asks for one to be created
  ("wygeneruj…", "zrób obrazek…", "stwórz grafikę…", "generate me…", "make a picture of…").
- Don't loop the skill to re-roll variations without an explicit user request —
  one image per explicit ask.
