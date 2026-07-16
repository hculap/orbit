# Contributing to Orbit

Thanks for your interest — PRs and issues are welcome.

## Dev setup

Orbit is a [uv](https://docs.astral.sh/uv/)-managed Python 3.12 project.

```bash
git clone https://github.com/hculap/orbit
cd orbit
uv sync                 # install deps into .venv
uv run pytest           # run the test suite
```

Run the app locally while you work:

```bash
uv run python -m orbit --host 127.0.0.1 --port 8766
```

## Conventions

- **Keep the no-build-step frontend.** The UI is CDN React 18 + Babel-standalone with plain `.jsx` modules in `src/orbit/static/` — no bundler, no transpile step, no `node_modules`. Sibling modules share code by publishing to `window`. Please don't introduce a build toolchain.
- **Small, focused files.** Prefer many small modules over a few large ones.
- **Tests come with the change.** Add or update tests under `tests/` and make sure `uv run pytest` is green before you open a PR.

## Pull requests

1. Fork and branch off `main`.
2. Make your change, with tests.
3. Run `uv run pytest`.
4. Open a PR with a clear description of what and why.
