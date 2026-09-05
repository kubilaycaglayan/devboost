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
7. **Docker Monitoring**:
   - Shows cached `docker ps` and `docker stats --no-stream` snapshots for the active SSH server.
   - Container rows support default alphabetical name sorting and shift-click multi-column sorting, including CPU and label count.
   - Open a persistent log watch by container name; it refreshes through rebuilds that reuse the same name.
   - Manage case-insensitive container-name label rules with custom colors; `dev` → `Dev` and `production` → `production` are included by default.
   - The dashboard refreshes in the background without blocking other local requests; the CLI exposes the same capability with `devboost docker`.
8. **AI quota and balance monitoring**:
   - Track multiple named accounts for Codex, Agy, Claude, OpenCode, or a custom provider.
   - Accounts can read JSON from an authenticated HTTPS balance endpoint or a locally installed CLI command.
   - The macOS menu bar exposes each account’s latest remaining quota and refreshes it every minute.

---

## 📁 Repository Structure

```text
devboost/
├── devboost.py           # Compatibility entry point, REST API wiring & CLI
├── dashboard/            # Dashboard document and browser assets
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── http.py            # Dashboard HTTP handler and API routing
│   └── __init__.py       # Asset loading and serving helpers
├── devboost_app/         # Python domain modules extracted from the legacy entry point
│   ├── __init__.py
│   ├── configuration.py  # Config persistence, atomic writes, and migrations
│   ├── docker_service.py # Docker discovery and container labels
│   ├── folder_sync.py    # Folder-sync orchestration and rsync lifecycle
│   ├── forwarding.py     # SSH forwards and LaunchAgent lifecycle
│   ├── cli.py            # CLI commands and help output
│   ├── history.py        # SSH discovery and port history
│   ├── local_ports.py    # Local listener inspection and safe process control
│   ├── server_helpers.py # Server-tab lookup and label helpers
│   ├── server_management.py # Server-tab lifecycle and migrations
│   ├── sync_helpers.py   # Pure folder-sync and SSH-config helpers
│   ├── usage.py          # Provider-agnostic usage normalization helpers
│   └── usage_service.py  # Local command, HTTPS, and provider API adapters
├── remote_transport.py   # Shared SSH subprocess transport
├── docker_monitor.py     # Cached remote Docker snapshots and parsers
├── build-app.sh          # Builds the self-contained macOS app bundle
├── run-app.sh            # Rebuilds and launches the development app
├── assets/                # Static web assets (favicon.png served at /favicon.png)
│   └── favicon.png        # Dashboard favicon (with embedded fallback in devboost.py)
├── .env.example           # Environment template for private credentials & host settings
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
│   ├── test_docker.py
│   ├── test_localports.py
│   └── test_sync.py
└── .github/workflows/      # Automated CI workflow running on macOS
```

---

## 🚀 Quick Start

### 1. Configure Environment
Copy the example environment file into DevBoost's per-user state directory:
```bash
mkdir -p "$HOME/Library/Application Support/DevBoost"
cp .env.example "$HOME/Library/Application Support/DevBoost/.env"
```
Edit that `.env` file to configure your remote SSH server:
```bash
DEVBOOST_SSH_HOST=my-remote-server
DEVBOOST_SERVER_NAME="My Remote Server"
DEVBOOST_SERVER_IP=192.168.1.100
DEVBOOST_DASHBOARD_PORT=3080
```

### 2. Build & Run
Build and launch the app from the current checkout:
```bash
./run-app.sh
```
This will:
- Rebuild `~/Applications/DevBoost.app` from the current source.
- Relaunch the dashboard on port 3080.

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

### AI usage accounts

Usage accounts live in the per-user `config.json`; secrets remain in environment variables. Add one with either a JSON-producing local command or an HTTPS endpoint:

```bash
curl -X POST http://127.0.0.1:3080/api/usage/accounts \
  -H 'Content-Type: application/json' \
  -d '{"provider":"claude","name":"Personal","balance_url":"https://api.example.com/balance","token_env":"MY_API_TOKEN"}'
curl -X POST http://127.0.0.1:3080/api/usage/refresh
```

The dashboard queries local-provider usage on the currently selected SSH server. The CLI can target one explicitly with `devboost usage --refresh --server <id-or-host>`.

Responses are normalized into `quotas` (`used`, `limit`, `remaining`, reset time) and `balances` (`remaining`, currency), allowing multiple accounts per provider.

Codex accounts query current subscription quotas through the documented [Codex app-server account API](https://learn.chatgpt.com/docs/app-server#auth-endpoints), using the selected host's installed Codex CLI and existing ChatGPT sign-in. The dashboard shows each returned quota bucket, remaining percentage, reset time, plan and credits when available. Earned resets are shown only when reported by the API. No model turn is started and DevBoost does not read or store Codex credentials. Remote queries require Python 3 and Codex on the selected SSH host; they do not require a remote DevBoost installation.

Codex values refresh every minute, and **Refresh** forces a live query. Cached Codex snapshots older than a minute are also refreshed on page load. If the API fails, the last successful API values (or historical rollout records if no API snapshot exists) are marked **Stale**, with the last successful query age preserved. An elapsed reset in historical records is unknown, never an assumed 100% allowance. Set `codex_home` (Codex profile directory) for separate authenticated profiles; otherwise the host's `CODEX_HOME` or `~/.codex` is used. Names are display labels, not account selectors. An explicit `local_path` retains records-only mode; clear it to enable live queries. Explicit JSON commands, HTTPS URLs and admin API mode still override the default Codex adapter.

Claude accounts read observed token usage from `~/.claude/projects`; OpenCode accounts use `opencode stats`; Agy accounts use `agy-quota --json` when that helper is installed. In the dashboard, these local-provider adapters run on the selected SSH server. Set `local_path` per account for separate data roots. Claude token totals are observed usage, not a provider-reported remaining subscription quota. Provider HTTPS/admin API accounts continue to query the provider directly from DevBoost.

For API-backed Codex or Claude accounts, enable `api_mode` and set `token_env`. DevBoost then queries the provider’s organization usage report for the configured `api_days` window. These require provider-admin credentials and report usage/spend; they do not imply remaining subscription credits.

---

## 📁 Folder Sync — semantics

Only the logical parameter combinations are exposed (the names map 1:1 to the port concepts):

| Parameter | Options | Meaning |
|---|---|---|
| Direction | `two-way` (default), `push` (local → remote), `pull` (remote → local) | What gets copied. Two-way runs `rsync --update` both ways so the newer file wins. |
| Mirror | on / off (one-way only) | Off (default) = safe merge, extra files on the destination are kept. On = exact copy via `rsync --delete`, deletions propagate. **Disabled for two-way**, which is always a safe merge — delete-propagation in both directions via plain rsync is order-dependent and lossy. |
| Run mode | `Once` (one-time sync) / `Auto` (always) | Once = runs now + on-demand via Sync Now, no background agent (the Session equivalent). Auto = persistent LaunchAgent with `WatchPaths` (instant local triggers) + polling every `interval` seconds for remote changes (the Always equivalent). |
| Interval | 5–600 s (default 15, Auto only) | How often the background agent re-syncs (covers remote-side changes). |

Syncs are stored per server tab in `~/Library/Application Support/DevBoost/config.json` (`syncs` + `folder_history`), shown in the **Folder Syncs** section below **Configured Port Forwards**, and removable together with their tab. Transport is `rsync -az --update` over the same key-based SSH the tunnels use; the remote folder is created automatically (`mkdir -p`).

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

# Show Docker containers and resource snapshots on the remote server
devboost docker [--server ID]

# Show AI provider quotas and API balances (use --refresh for a live check)
devboost usage [--refresh]

# Open the interactive lazydocker terminal UI on the remote server
devboost lazydocker [--server ID]

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

Source can live anywhere outside macOS protected folders. Run the packaged app
from the source checkout; it rebuilds `~/Applications/DevBoost.app` from the latest changes,
uses port 3080, and relaunches detached by default. The app remains in the menu bar until
you choose **Quit DevBoost**:

```bash
./run-app.sh
```

Use `./run-app.sh --foreground` when launcher or backend output should stay attached to the terminal.

Rerunning takes over port 3080 and replaces the prior app. Auto sync workers execute through
the installed app bundle, so they always use the same packaged version as the dashboard.

---

## 🔒 Privacy & macOS Permissions

- Private server settings, runtime configuration, and logs live in `~/Library/Application Support/DevBoost/`, outside the source checkout and app bundle.
- DevBoost runs from `~/Applications/DevBoost.app`. Grant that app access in **System Settings → Privacy & Security** when an Auto sync needs to read or write protected folders such as Documents, Desktop, Downloads, or iCloud Drive.

---

## 📄 License
MIT License. Free for personal and commercial use.
