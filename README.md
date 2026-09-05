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
5. **Zero External Dependencies**:
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
│   └── test_manager.py
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
- **Scan Ports**: Remotely detect listening services on your server and forward them with one click.
- **Clean Orphans**: Kill lingering duplicate SSH tunnel processes with one click.

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

## 🔒 Privacy & macOS Permissions

- All private server hostnames, IPs, and custom agent domains are configured via `app/.env` (untracked via `/app/` in `.gitignore`; legacy root `.env` still works as fallback).
- Runtime state (`app/config.json`) is also untracked in `app/`.
- **macOS TCC Sandbox Compliance**: Background daemons managed by macOS `launchd` are restricted from reading directly inside `~/Documents/` or `~/Desktop/`. The `install.sh` script deploys the runtime executable to `~/.config/devboost/` so background services operate smoothly without macOS privacy sandbox errors.

---

## 📄 License
MIT License. Free for personal and commercial use.
