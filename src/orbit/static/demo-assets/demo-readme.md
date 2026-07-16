# Showroom widgetów — demo README

Ten plik jest pre-staged w katalogu `~/.orchestrator/uploads/<session-id>/`
przez skrypt `create_demo_session.py` i służy jako cel bloku `download`
w demo-sesji "Showroom widgetów".

## Co prezentuje sesja

Sesja przechodzi przez 7 nowych typów bloków, jeden widget na turę:

1. **audio** — TTS wygenerowany przez skill `generate-audio`
   (silnik elevenlabs/openai/gemini, cache w `~/.orchestrator/tts-cache/`).
2. **download** — kafelek do pobrania pliku (ten dokument).
3. **video** — natywny `<video>` z poster framem i kontrolkami fullscreen.
4. **youtube** — embed przez `youtube-nocookie.com` (bez ciasteczek).
5. **chart** — Chart.js bar chart z liczbą linii kodu na widget.
6. **map** — Leaflet + OpenStreetMap, markery + polyline route
   Warszawa → Poznań.
7. **custom_html** — sandbox iframe z animowanym CSS gradient i jednym
   przyciskiem JS modyfikującym tło.

## Re-generacja

Po zmianach w schema bloków lub w system promptcie:

```bash
uv run python -m orbit.scripts.create_demo_session --replace
```

Skrypt:

- Sprawdza czy sesja "Showroom widgetów" już istnieje (sidecar
  `title_manual=True`) i bez `--replace` kończy się 0 z komunikatem skip.
- Mintuje UUID, kopiuje 3 statyczne assety z
  `src/orbit/static/demo-assets/` do uploads dir nowej sesji.
- Driveuje 1 turę seed + 7 tur per-widget przez `ClaudeRunner`.
- Stempluje sidecar: `title="Showroom widgetów"`, `title_manual=True`,
  `pinned=True`, `model="opus"`.

## Format

To zwykły markdown (mime: `text/markdown`). Frontend renderuje kafelek
z ikoną, nazwą pliku i rozmiarem; klik wywołuje natywne pobieranie.
