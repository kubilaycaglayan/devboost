# AGENTS.md — working instructions for AI assistants in this repo

## Layout (post-move)
- Repo lives at `~/dev/devboost` — deliberately OUTSIDE `~/Documents/Desktop/Downloads`
  so launchd agents can exec it (TCC blocks exec from protected locations → exit 126).
- Runtime state (`~/.config/devboost/app`: `.env`, `config.json`) is separate from code and
  shared by every runtime. The stale installed *code* (`~/.config/devboost/devboost.py`, `bin/`)
  was retired; only `app/` (state) and `assets/` remain there.
- `~/.local/bin/devboost` symlinks at the repo file.

## Running the app
- The primary development launcher is `./run-app.sh`; it migrates state, rebuilds the
  packaged app, takes over the configured dashboard port (default `:3080`), and relaunches
  DevBoost detached by default.
- Run `./run-app.sh --foreground` when launcher or backend output should stay attached to
  the terminal for debugging.
- Detached app output is written to `~/Library/Application Support/DevBoost/logs/devboost-app.log`;
  the native app PID is stored in `~/Library/Application Support/DevBoost/devboost-app.pid`.
- Building or relaunching the packaged app is the user's job. Do not run `./run-app.sh` while
  developing unless the user explicitly asks for a relaunch.

## Repo facts
- Single-file app: everything lives in `devboost.py` (CLI + REST API + embedded dashboard SPA).
- **Stdlib only** — no new dependencies, ever (`http.server`, `plistlib`, `subprocess`, …).
- Runtime state (`app/.env`, `app/config.json`) is untracked and per-checkout; the installed copy
  lives at `~/.config/devboost`. `devboost serve` runs the installed copy — check with
  `diff devboost.py ~/.config/devboost/devboost.py` when behavior looks stale.
- Sync LaunchAgents re-exec `devboost.py sync-run <id>` on every run, so agents pick up code
  changes automatically too.
- Sync agent executables must be launchable by launchd: never point them under `~/Documents/`
  (TCC blocks exec → exit 126). `get_sync_executable_args` falls back to the installed copy.

## Before finishing a change
- `python3 -W error::SyntaxWarning -m py_compile devboost.py`
- JS check: extract `<script>` from `HTML_DASHBOARD` and run `node --check` on it.
- `python3 -m unittest discover tests -v` — all green (add tests for new backend behavior in `tests/`).
- For UI/API changes, smoke-test against a live server with an isolated `DEVBOOST_APP_DIR`.

## Product sensitivities (don't regress these)
- **Permissions**: never propose granting Full Disk Access (or similar) to system-wide binaries
  like `/usr/bin/python3` — any access grant must be scoped to DevBoost only. Prefer solutions
  that need no grants at all (e.g. folders outside `~/Documents/Desktop/Downloads`).
- **Folder-sync semantics** (see README): `two-way` is always a safe merge (newer wins, deletions
  never propagate); `mirror --delete` is one-way only; `Once` vs `Auto` map to ports' Session vs Always.
- **Error messages**: rsync failure hints are exclusive and ordered — a conclusive local cause
  (e.g. TCC `Operation not permitted`) must suppress vaguer remote-side guesses.
