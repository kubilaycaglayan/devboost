# DevBoost • SSH Port Forward Manager & Discovery Dashboard

A lightweight, zero-dependency port forward manager and discovery dashboard for macOS. It manages both persistent (auto-restarting macOS `launchd` LaunchAgents) and ad-hoc session-based SSH port forwards to your remote development servers.

---

## 🎯 Features

1. **Full Discoverability**:
   - Web Dashboard (`http://localhost:3080`) and CLI (`devboost`) to inspect all forwarded ports, PIDs, and active states.
2. **Persistent ("Always Forward") vs Session Mode**:
   - **Persistent (LaunchAgent)**: Starts automatically on login/boot and automatically reconnects if Wi-Fi drops, the machine wakes from sleep, or the remote server restarts.
   - **Session (Temporary)**: Lightweight background SSH tunnels for temporary tasks.
   - **One-Click Toggle**: Switch between persistent and session mode with a single button.
3. **Remote Port Discovery**:
   - Scans remote server listening ports (e.g. Grafana, Ollama, Prometheus, Docker containers) via SSH and provides a 1-click forward button for unforwarded services.
4. **Zombie / Orphan Cleanup**:
   - Identifies and terminates duplicate, hung, or orphaned `ssh -fN` processes that fail to bind ports.
5. **Folder Sync (two-way mirror)**:
   - Real-time-ish bidirectional folder mirror between this Mac and any server tab, over `rsync` + SSH (zero new dependencies).
   - Tracked syncs are managed just like ports: `Once` (one-time, Session equivalent) vs `Auto` (persistent LaunchAgent, Always equivalent), with Sync Now / Make Auto / Make Once / Delete actions.
   - Dual file-system quick select: browse local *and* remote folders from the dashboard, with recently-synced pairs remembered per tab.
6. **Zero External Dependencies**:
   - Built exclusively with Python standard library (`http.server`, `urllib`, `plistlib`, `subprocess`).

---

## 📁 Repository Structure

```text
port_tracker/
├── devboost.py           # Core CLI, REST API & embedded Web Dashboard SPA
├── app/                  # UNTRACKED local state (gitignored): app/.env, app/config.json
│                         # Executable stays in ~/.config/devboost (TCC-safe); only state lives here
├── assets/                 # Static web assets (favicon.png served at /favicon.png)
│   └── favicon.png         # Dashboard favicon (with embedded fallback in devboost.py)
├── install.sh              # Installation & deployment script
├── .env.example            # Environment template for private credentials & host settings
├── config.example.json     # Sample port-to-service label definitions
├── launchagents/           # LaunchAgent plist templates
│   ├── dashboard.plist.template   # Dashboard background daemon template
│   └── tunnel.plist.template      # Auto-restarting SSH tunnel template
├── bin/                    # Scoped wrappers so macOS shows DevBoost-* instead of ssh/python3
│   ├── DevBoost-dashboard         # execs python3 devboost.py (dashboard agent entry point)
│   └── DevBoost-tunnel            # execs ssh (tunnel agents entry point)
├── tests/                  # Automated unit test suite (100% stdlib unittest)
│   ├── test_api.py
│   ├── test_config.py
│   ├── test_env.py
│   ├── test_manager.py
│   └── test_sync.py
└── .github/workflows/      # Automated CI workflow running on macOS
```

---

## 🚀 Quick Start

### 1. Configure Environment
Copy the example environment file into the untracked state dir:
```bash
mkdir -p app && cp .env.example app/.env
```
Edit `app/.env` to configure your remote SSH server:
```bash
DEVBOOST_SSH_HOST=my-remote-server
DEVBOOST_SERVER_NAME="My Remote Server"
DEVBOOST_SERVER_IP=192.168.1.100
DEVBOOST_DASHBOARD_PORT=3080
```

### 2. Install & Deploy
Run the installer script:
```bash
./install.sh
```
This will:
- Install the CLI command `devboost` into `~/.local/bin/` (make sure it's in your `$PATH`).
- Render and load the macOS LaunchAgent so the dashboard runs in the background.

---

## 🌐 Web Dashboard

Once deployed, access the dashboard anytime at:
👉 **http://localhost:3080** (or run `devboost ui`).

- **Live Status Dots**: 🟢 Connected / 🔴 Offline status for the remote server.
- **Port Links**: Click on any forwarded port to open `http://localhost:<port>` directly in your browser.
- **Make Always / Make Session**: Toggle persistence with one click.
- **Folder Syncs**: Mirror a local folder with the active server (two-way merge, push, or pull), one-time or continuously via a persistent agent.
- **Scan Ports**: Remotely detect listening services on your server and forward them with one click.
- **Clean Orphans**: Kill lingering duplicate SSH tunnel processes with one click.

---

## 📁 Folder Sync — semantics

Only the logical parameter combinations are exposed (the names map 1:1 to the port concepts):

| Parameter | Options | Meaning |
|---|---|---|
| Direction | `two-way` (default), `push` (local → remote), `pull` (remote → local) | What gets copied. Two-way runs `rsync --update` both ways so the newer file wins. |
| Mirror | on / off (one-way only) | Off (default) = safe merge, extra files on the destination are kept. On = exact copy via `rsync --delete`, deletions propagate. **Disabled for two-way**, which is always a safe merge — delete-propagation in both directions via plain rsync is order-dependent and lossy. |
| Run mode | `Once` (one-time sync) / `Auto` (always) | Once = runs now + on-demand via Sync Now, no background agent (the Session equivalent). Auto = persistent LaunchAgent with `WatchPaths` (instant local triggers) + polling every `interval` seconds for remote changes (the Always equivalent). |
| Interval | 5–600 s (default 15, Auto only) | How often the background agent re-syncs (covers remote-side changes). |

Syncs are stored per server tab in `app/config.json` (`syncs` + `folder_history`), shown in the **Folder Syncs** section below **Configured Port Forwards**, and removable together with their tab. Transport is `rsync -az --update` over the same key-based SSH the tunnels use; the remote folder is created automatically (`mkdir -p`).

> **macOS privacy (TCC) note**: browsing or syncing protected folders (`~/Documents`, `~/Desktop`, …) fails with *Operation not permitted* unless the dashboard process is allowed to read them. Fix: System Settings → Privacy & Security → **Full Disk Access** → add your terminal app (Terminal, iTerm, VS Code, …) and restart the dashboard. If the dashboard runs as a background LaunchAgent, add the Python binary that runs it instead.

---

## 💻 CLI Reference (`devboost`)

```bash
# List all forwarded ports, PIDs, and status
devboost

# Forward a port for the current session
devboost add 8080 "Docker Web App"

# Forward a port persistently (starts on boot, auto-reconnects)
devboost add 3030 --always "Grafana Dashboard"

# Forward with custom remote port (e.g. local 9000 -> remote 8080)
devboost add 9000 8080 --always "Custom API"

# Remove a port forward and stop its tunnel / LaunchAgent
devboost rm 8080

# Folder syncs (tracked mirrors, per server tab)
devboost sync                                   # list syncs for the default tab
devboost sync add ~/projects/app ~/projects/app --auto   # two-way Auto mirror (runs now + continuously)
devboost sync add ~/data /srv/data --direction push --mirror --no-run  # one-way exact copy, don't run yet
devboost sync run <id>                          # run a sync now
devboost sync edit <id> [--local PATH] [--remote PATH] [--direction ...] [--mirror|--no-mirror] [--auto|--once] [--interval N]
devboost sync auto <id> --off                   # switch Auto -> Once (removes background agent)
devboost sync rm <id>                           # remove a sync (files kept, tracking + agent removed)

# Scan listening services on remote server
devboost scan

# Clean up duplicate / orphaned background SSH processes
devboost clean

# Open the web dashboard in your browser
devboost ui
```

---

## 🧪 Testing

Run the automated test suite locally:
```bash
python3 -m unittest discover tests -v
```

---

## 🛠️ Development (always-latest dashboard)

`devboost serve` runs the **installed** copy (`~/.config/devboost/devboost.py`), so repo edits need a reinstall first. For development, use the dev script instead — it always execs the repo file, shares your live state, and defaults to port 3081 so it runs alongside the background dashboard on 3080:

```bash
./serve-dev.sh                # latest code on http://localhost:3080 (stops the existing occupant first)
./serve-dev.sh --port 3090    # custom port instead
# Ctrl-C to stop, rerun to pick up edits — no build step, no reinstall
```

Rerunning takes the port: a previous dev server on that port is stopped automatically. If the background dashboard itself holds the port, it is unloaded via `launchctl` and **reloaded automatically when the dev server exits**, so the system returns to its prior state. (`install.sh` stays manual — the dev script never installs anything.)

---

## 🔒 Privacy & macOS Permissions

- All private server hostnames, IPs, and custom agent domains are configured via `app/.env` (untracked via `/app/` in `.gitignore`; legacy root `.env` still works as fallback).
- Runtime state (`app/config.json`) is also untracked in `app/`.
- **macOS TCC Sandbox Compliance**: Background daemons managed by macOS `launchd` are restricted from reading directly inside `~/Documents/` or `~/Desktop/`. The `install.sh` script deploys the runtime executable to `~/.config/devboost/` so background services operate smoothly without macOS privacy sandbox errors.

---

## 📄 License
MIT License. Free for personal and commercial use.
