---
name: hetzner-teleport
description: Przenoś sesje Claude Code MIĘDZY tym serwerem (orbit) a lokalnym Claude. Dwa kierunki w jednej komendzie /hetzner-teleport, wykrywane z argumentu. (1) IMPORT — podajesz URL/ID sesji z serwera (+opcjonalnie nazwę) → ściąga tę sesję i zapisuje LOKALNIE do bieżącego projektu (wznawialną przez `claude --resume`). (2) EXPORT — podajesz nazwę agenta/projektu (projects/x, areas/x, global) (+opcjonalnie nazwę) → wypycha BIEŻĄCĄ lokalną sesję na serwer do tego agenta. Move Claude Code sessions between this dashboard server and the local Claude. Triggers: "/hetzner-teleport", "teleportuj sesję", "ściągnij sesję z serwera", "wypchnij sesję na serwer", "przenieś sesję tu/tam", "import session from server", "export this session to <agent>".
metadata:
  emoji: 🛸
---

# hetzner-teleport

Przenosi sesje **między dashboardem `orbit` a lokalnym Claude Code**.
Jedna komenda, dwa kierunki — **wykryj kierunek z argumentu, NIE pytaj zbędnie**.

CLI: `~/.claude/skills/hetzner-teleport/scripts/teleport_cli.py`

## Wykrywanie kierunku (WAŻNE)

Spójrz na pierwszy argument:

| Argument wygląda jak… | Kierunek | Co robisz |
|---|---|---|
| URL serwera (`https://…/chat/<uuid>`) **lub** samo UUID | **IMPORT** (server → tu) | `import <arg>` |
| `projects/<x>`, `areas/<x>`, albo `global` | **EXPORT** (tu → server) | `export <arg>` |

- Reszta argumentów (wolny tekst / `name …`) = **nazwa sesji** → `--title`.
- Użytkownik może podać czasownik wprost (`import …` / `export …`) — uszanuj go.
- **Przy IMPORCIE NIE pytaj o agenta docelowego** — celem jest „tutaj" (bieżący
  katalog). Pytanie o `--agent` przy imporcie to błąd.
- Pytaj o brakującą informację **tylko** gdy kierunku NIE da się wywnioskować.

## 1. IMPORT — ściągnij sesję z serwera TUTAJ

Pobiera sesję z dashboardu i zapisuje ją jako **nową, wznawialną** sesję w
**bieżącym projekcie lokalnym** (cwd, w którym działa Claude). Bez tokenu (GET).

```bash
CLI=~/.claude/skills/hetzner-teleport/scripts/teleport_cli.py
python3 "$CLI" import https://<your-dashboard>/chat/<UUID>
python3 "$CLI" import <UUID> --title "Robota nad X"
```

Wypisze ID nowej lokalnej sesji + `claude --resume <id>`.

## 2. EXPORT — wypchnij bieżącą lokalną sesję na serwer

Bierze **bieżącą** lokalną sesję (albo `--session <id>`) i wgrywa ją na serwer
do wskazanego agenta. Wymaga tokenu (POST).

```bash
python3 "$CLI" export projects/orbit
python3 "$CLI" export areas/Dom --title "Wątek o domu"
python3 "$CLI" export global                 # agent globalny ($HOME na serwerze)
python3 "$CLI" export projects/x --session <LOCAL_ID>   # konkretna lokalna sesja
```

Wypisze ID nowej sesji utworzonej na serwerze.

## 3. Przykłady mapowania komendy

- `/hetzner-teleport https://<your-dashboard>/chat/9080…  Robota` →
  `import 9080… --title "Robota"` (ściąga tu).
- `/hetzner-teleport projects/my-project  notatki` →
  `export projects/my-project --title "notatki"` (wypycha bieżącą sesję).

## 4. Konfiguracja off-box (laptop)

Token z dashboardu: **Settings → Serwer → Teleport**. Zapisz do
`~/.claude/skills/hetzner-teleport/config.json`:

```json
{"dashboard_url": "https://<your-dashboard>", "artifact_token": "<TOKEN>"}
```

CLI czyta `dashboard_url` + `artifact_token` z tego pliku, gdy nie ma zmiennych
`HD_*` (czyli zawsze poza serwerem). IMPORT działa bez tokenu; EXPORT go wymaga.
