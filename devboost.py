#!/usr/bin/env python3
"""
DevBoost • SSH Port Forward Manager & Discovery Dashboard
CLI & Web Dashboard for managing persistent (launchd) and session-based SSH port forwards.
"""

import sys
import os
import re
import json
import glob
import time
import signal
import plistlib
import subprocess
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler


def load_env():
    """Loads key-value pairs from .env files without requiring external libraries."""
    candidates = [
        os.path.join(os.path.dirname(os.path.realpath(__file__)), ".env"),
        os.path.expanduser("~/.config/devboost/.env"),
        os.path.expanduser("~/.config/port-tracker/.env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for env_path in candidates:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


# Initialize environment configuration
load_env()


def _env(new_key, legacy_key, default):
    """Reads DevBoost-prefixed env var with fallback to legacy PORT_TRACKER_ name."""
    return os.getenv(new_key, os.getenv(legacy_key, default))


SSH_HOST = _env("DEVBOOST_SSH_HOST", "PORT_TRACKER_SSH_HOST", "remote-server")
SERVER_NAME = _env("DEVBOOST_SERVER_NAME", "PORT_TRACKER_SERVER_NAME", "Remote Server")
SERVER_IP = _env("DEVBOOST_SERVER_IP", "PORT_TRACKER_SERVER_IP", "")
DEFAULT_DASHBOARD_PORT = int(_env("DEVBOOST_DASHBOARD_PORT", "PORT_TRACKER_DASHBOARD_PORT", "3080"))
AGENT_DOMAIN = _env("DEVBOOST_AGENT_DOMAIN", "PORT_TRACKER_AGENT_DOMAIN", "com.user.devboost")
AGENT_PREFIX = _env("DEVBOOST_AGENT_PREFIX", "PORT_TRACKER_AGENT_PREFIX", "com.user.devboost-forward")
CONFIG_DIR = os.path.expanduser(_env("DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR", "~/.config/devboost"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
BIN_DIR = os.path.join(CONFIG_DIR, "bin")
DASHBOARD_WRAPPER_NAME = "DevBoost-dashboard"
TUNNEL_WRAPPER_NAME = "DevBoost-tunnel"
LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
LOG_DIR = os.path.expanduser(_env("DEVBOOST_LOG_DIR", "PORT_TRACKER_LOG_DIR", "~/Library/Logs"))


def get_tunnel_executable():
    """Scoped wrapper so macOS Background Items shows DevBoost-tunnel instead of ssh."""
    return os.path.join(BIN_DIR, TUNNEL_WRAPPER_NAME)


def get_dashboard_executable():
    """Scoped wrapper so macOS Background Items shows DevBoost-dashboard instead of python3."""
    return os.path.join(BIN_DIR, DASHBOARD_WRAPPER_NAME)

DEFAULT_LABELS = {
    3030: "Grafana Dashboard",
    3000: "Docker Web / App",
    8080: "Docker Proxy / Web App",
    9090: "Prometheus Metrics",
    9100: "Node Exporter",
    9835: "NVIDIA GPU Exporter",
    11434: "Ollama LLM API",
    15432: "PostgreSQL Docker",
    15433: "PostgreSQL Docker 2",
    18082: "Docker Service",
    43127: "WXT Extension Hot-Reload",
    50000: "App Port 50000",
    50001: "App Port 50001",
}


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(BIN_DIR, exist_ok=True)
    os.makedirs(LAUNCH_AGENTS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def load_config():
    ensure_dirs()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("labels", {})
                cfg.setdefault("rules", {})
                cfg.setdefault("history", [])
                return cfg
        except Exception:
            pass
    return {"labels": {}, "rules": {}, "history": []}


def save_config(cfg):
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def record_port_history(local_port, remote_port, label=""):
    """Appends/updates the recently-used port history (most recent first, max 20)."""
    try:
        cfg = load_config()
        history = cfg.setdefault("history", [])
        # Remove existing entry for the same local port to re-insert at front
        history = [h for h in history if int(h.get("local_port", -1)) != int(local_port)]
        history.insert(0, {
            "local_port": int(local_port),
            "remote_port": int(remote_port),
            "label": label or DEFAULT_LABELS.get(int(local_port), f"Port {local_port}"),
            "last_used": time.time(),
        })
        cfg["history"] = history[:20]
        save_config(cfg)
    except Exception:
        pass


def get_port_history(limit=8, exclude_ports=None):
    """Returns most-recently-used ports, optionally excluding currently-configured ones."""
    try:
        cfg = load_config()
        history = cfg.get("history", [])
        excluded = set(exclude_ports or [])
        result = [h for h in history if int(h.get("local_port", -1)) not in excluded]
        return result[:limit]
    except Exception:
        return []


def get_plist_label(port):
    return f"{AGENT_PREFIX}-{port}"


def get_plist_path(port):
    return os.path.join(LAUNCH_AGENTS_DIR, f"{get_plist_label(port)}.plist")


def is_server_reachable():
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", SSH_HOST, "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_listening_ports():
    """Returns dict of {port: {'pid': int, 'cmd': str, 'node': str}}."""
    ports = {}
    try:
        res = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 9:
                cmd, pid, node = parts[0], parts[1], parts[8]
                m = re.search(r":(\d+)$", node)
                if m:
                    p = int(m.group(1))
                    ports[p] = {"cmd": cmd, "pid": int(pid), "node": node}
    except Exception:
        pass
    return ports


def get_ssh_forwards():
    """Parses ps output to find SSH forwards."""
    forwards = []
    listening_ports = get_listening_ports()
    try:
        res = subprocess.run(["ps", "-A", "-o", "pid,command"], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if "ssh" in line and "-L" in line:
                if SSH_HOST in line or (SERVER_IP and SERVER_IP in line) or "localhost" in line:
                    m = re.search(r"-L\s+(?:127\.0\.0\.1:|localhost:)?(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", line)
                    if m:
                        local_p = int(m.group(1))
                        remote_p = int(m.group(2))
                        pid = int(line.strip().split()[0])
                        is_listen = (local_p in listening_ports and listening_ports[local_p]["pid"] == pid)
                        forwards.append({
                            "pid": pid,
                            "local_port": local_p,
                            "remote_port": remote_p,
                            "is_listening": is_listen,
                            "cmd": line.strip()
                        })
    except Exception:
        pass
    return forwards


def get_launchagents():
    """Finds all LaunchAgents for port forwarding (both current prefix and legacy formats)."""
    agents = {}
    patterns = [
        os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "com.*.ssh-forward-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "*ssh-forward-*.plist"),
    ]
    seen_paths = set()
    for pat in patterns:
        for plist_path in glob.glob(pat):
            if plist_path in seen_paths:
                continue
            seen_paths.add(plist_path)
            m = re.search(r"-(\d+)\.plist$", plist_path)
            if m:
                port = int(m.group(1))
                try:
                    with open(plist_path, "rb") as f:
                        data = plistlib.load(f)
                        remote_port = port
                        args = data.get("ProgramArguments", [])
                        for i, arg in enumerate(args):
                            if arg == "-L" and i + 1 < len(args):
                                lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                                if lm:
                                    remote_port = int(lm.group(2))
                        agents[port] = {
                            "port": port,
                            "remote_port": remote_port,
                            "label": data.get("Label", get_plist_label(port)),
                            "path": plist_path
                        }
                except Exception:
                    agents[port] = {
                        "port": port,
                        "remote_port": port,
                        "label": get_plist_label(port),
                        "path": plist_path
                    }
    return agents


def get_all_forwards_status():
    cfg = load_config()
    listening = get_listening_ports()
    ssh_procs = get_ssh_forwards()
    agents = get_launchagents()

    all_ports = set()
    all_ports.update(agents.keys())
    for f in ssh_procs:
        all_ports.add(f["local_port"])
    for p_str in cfg.get("labels", {}).keys():
        try:
            all_ports.add(int(p_str))
        except ValueError:
            pass

    proc_map = {}
    for proc in ssh_procs:
        lp = proc["local_port"]
        proc_map.setdefault(lp, []).append(proc)

    results = []
    orphaned_count = 0

    for port in sorted(all_ports):
        is_always = port in agents
        procs = proc_map.get(port, [])
        active_proc = next((p for p in procs if p["is_listening"]), None)
        orphans = [p["pid"] for p in procs if not p["is_listening"]]
        orphaned_count += len(orphans)

        remote_port = port
        if is_always:
            remote_port = agents[port]["remote_port"]
        elif procs:
            remote_port = procs[0]["remote_port"]

        label = cfg.get("labels", {}).get(str(port))
        if not label:
            label = DEFAULT_LABELS.get(port, f"Port {port}")

        is_active = active_proc is not None
        pid = active_proc["pid"] if active_proc else None

        results.append({
            "local_port": port,
            "remote_port": remote_port,
            "label": label,
            "always": is_always,
            "active": is_active,
            "pid": pid,
            "orphans": orphans,
            "plist_label": agents[port]["label"] if is_always else None,
        })

    return {
        "server_name": SERVER_NAME,
        "server_host": SSH_HOST,
        "server_ip": SERVER_IP,
        "server_reachable": is_server_reachable(),
        "forwards": results,
        "orphaned_count": orphaned_count,
        "history": get_port_history(limit=10, exclude_ports=all_ports),
    }


def create_launchagent(local_port, remote_port):
    plist_path = get_plist_path(local_port)
    label = get_plist_label(local_port)
    data = {
        "Label": label,
        "ProgramArguments": [
            get_tunnel_executable(),
            "-N",
            "-T",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            SSH_HOST
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": os.path.join(LOG_DIR, f"devboost-forward-{local_port}.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, f"devboost-forward-{local_port}.err"),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    subprocess.run(["launchctl", "load", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return plist_path


def remove_launchagent(local_port):
    agents = get_launchagents()
    if local_port in agents:
        plist_path = agents[local_port]["path"]
        subprocess.run(["launchctl", "unload", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.remove(plist_path)
        except OSError:
            pass


def kill_port_processes(local_port):
    ssh_procs = get_ssh_forwards()
    killed = 0
    for p in ssh_procs:
        if p["local_port"] == local_port:
            try:
                os.kill(p["pid"], signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


def add_forward(local_port, remote_port=None, label="", always=False):
    if remote_port is None:
        remote_port = local_port
    cfg = load_config()
    if label:
        cfg.setdefault("labels", {})[str(local_port)] = label
    elif str(local_port) not in cfg.get("labels", {}):
        cfg.setdefault("labels", {})[str(local_port)] = DEFAULT_LABELS.get(local_port, f"Port {local_port}")
    save_config(cfg)
    record_port_history(local_port, remote_port, label=cfg.get("labels", {}).get(str(local_port), ""))

    kill_port_processes(local_port)

    if always:
        create_launchagent(local_port, remote_port)
    else:
        remove_launchagent(local_port)
        cmd = [
            "/usr/bin/ssh",
            "-fN",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            SSH_HOST
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_forward(local_port):
    remove_launchagent(local_port)
    kill_port_processes(local_port)
    cfg = load_config()
    cfg.get("labels", {}).pop(str(local_port), None)
    save_config(cfg)


def toggle_always(local_port, make_always):
    status = get_all_forwards_status()
    current = next((f for f in status["forwards"] if f["local_port"] == local_port), None)
    remote_port = current["remote_port"] if current else local_port
    label = current["label"] if current else ""
    add_forward(local_port, remote_port, label=label, always=make_always)


def clean_orphaned_tunnels():
    ssh_procs = get_ssh_forwards()
    killed = 0
    for p in ssh_procs:
        if not p["is_listening"]:
            try:
                os.kill(p["pid"], signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


def scan_remote_services():
    services = []
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", SSH_HOST, "ss -tlnp 2>/dev/null"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if "LISTEN" in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        addr_port = parts[3]
                        m = re.search(r":(\d+)$", addr_port)
                        if m:
                            port = int(m.group(1))
                            if port in (22, 53, 54):
                                continue
                            proc = "Unknown"
                            proc_match = re.search(r'users:\(\("([^"]+)"', line)
                            if proc_match:
                                proc = proc_match.group(1)
                            label = DEFAULT_LABELS.get(port, proc)
                            services.append({
                                "port": port,
                                "address": addr_port,
                                "process": proc,
                                "label": label
                            })
    except Exception:
        pass
    return services


# -----------------------------
# HTML & JS DASHBOARD TEMPLATE
# -----------------------------

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DevBoost • Port Forward Manager</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --text-muted: #8b949e;
      --accent: #58a6ff;
      --success: #3fb950;
      --warning: #d29922;
      --danger: #f85149;
      --btn-bg: #21262d;
      --btn-hover: #30363d;
      --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      padding: 24px;
      line-height: 1.5;
    }
    .container { max-width: 1060px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
      flex-wrap: wrap;
      gap: 16px;
    }
    .title-group h1 { font-size: 22px; font-weight: 600; color: #fff; display: flex; align-items: center; gap: 8px; }
    .server-tag {
      font-size: 13px;
      color: var(--text-muted);
      font-family: var(--font-mono);
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 4px;
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%; display: inline-block;
    }
    .status-dot.online { background-color: var(--success); box-shadow: 0 0 8px var(--success); }
    .status-dot.offline { background-color: var(--danger); }
    
    .stats-bar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    .stat-card .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-card .value { font-size: 26px; font-weight: 600; color: #fff; margin-top: 4px; font-family: var(--font-mono); }

    .btn {
      background: var(--btn-bg);
      color: #c9d1d9;
      border: 1px solid var(--border);
      padding: 7px 14px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
      text-decoration: none;
    }
    .btn:hover { background: var(--btn-hover); color: #fff; border-color: #8b949e; }
    .btn-primary { background: #238636; color: #fff; border-color: rgba(240,246,252,0.1); }
    .btn-primary:hover { background: #2ea043; border-color: rgba(240,246,252,0.1); }
    .btn-danger { color: var(--danger); }
    .btn-danger:hover { background: rgba(248, 81, 73, 0.15); border-color: var(--danger); }
    .btn-sm { padding: 4px 8px; font-size: 12px; }

    .section-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 24px;
      overflow: hidden;
    }
    .section-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(255, 255, 255, 0.02);
    }
    .section-header h2 { font-size: 16px; font-weight: 600; color: #fff; }

    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { padding: 12px 18px; font-size: 12px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }
    td { padding: 14px 18px; font-size: 13px; border-bottom: 1px solid rgba(48, 54, 61, 0.4); vertical-align: middle; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255, 255, 255, 0.015); }

    .badge {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 12px;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      white-space: nowrap;
    }
    .badge-always { background: rgba(88, 166, 255, 0.15); color: #58a6ff; border: 1px solid rgba(88, 166, 255, 0.3); }
    .badge-session { background: rgba(210, 153, 34, 0.15); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.3); }
    .badge-active { background: rgba(63, 185, 80, 0.15); color: #3fb950; border: 1px solid rgba(63, 185, 80, 0.3); }
    .badge-inactive { background: rgba(248, 81, 73, 0.15); color: #f85149; border: 1px solid rgba(248, 81, 73, 0.3); }

    .port-link {
      font-family: var(--font-mono);
      font-weight: 600;
      color: var(--accent);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .port-link:hover { text-decoration: underline; }

    .actions-cell { display: flex; gap: 8px; justify-content: flex-end; }

    .modal-overlay {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.7);
      display: none; align-items: center; justify-content: center; z-index: 1000;
    }
    .modal {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      width: 100%;
      max-width: 440px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
      overflow: hidden;
    }
    .modal-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-header h3 { font-size: 16px; color: #fff; }
    .modal-body { padding: 20px; }
    .form-group { margin-bottom: 16px; }
    .form-group label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500; }
    .form-group input[type="text"], .form-group input[type="number"] {
      width: 100%;
      padding: 8px 12px;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 14px;
      font-family: var(--font-mono);
    }
    .form-group input:focus { outline: none; border-color: var(--accent); }
    .form-checkbox { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
    .recent-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
    .recent-chip {
      background: #0d1117;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 16px;
      padding: 5px 12px;
      font-size: 12px;
      font-family: var(--font-mono);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .recent-chip:hover { border-color: var(--accent); color: #fff; background: rgba(88,166,255,0.12); }
    .recent-chip small { color: var(--text-muted); margin-left: 4px; }
    .recent-hint { font-size: 12px; color: var(--text-muted); margin-bottom: 2px; }
    .modal-footer {
      padding: 14px 20px;
      background: rgba(255, 255, 255, 0.02);
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }

    #toast {
      position: fixed; bottom: 20px; right: 20px;
      background: #1f242c;
      color: #fff;
      border: 1px solid var(--border);
      padding: 12px 18px;
      border-radius: 8px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.4);
      display: none;
      z-index: 2000;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1 id="header-server-title">DevBoost • Port Forward Manager</h1>
        <div class="server-tag">
          <span class="status-dot" id="server-status-dot"></span>
          <span id="server-status-text">Checking server...</span> •
          <span id="header-server-host">Connecting...</span>
        </div>
      </div>
      <div style="display:flex; gap:10px;">
        <button class="btn" onclick="cleanOrphans()" id="clean-orphans-btn" title="Clean up lingering duplicate ssh processes">
          🧹 Clean Orphans <span id="orphans-badge" style="font-size:11px; opacity:0.8;"></span>
        </button>
        <button class="btn btn-primary" onclick="openAddModal()">
          + Add Port Forward
        </button>
      </div>
    </header>

    <!-- Stats -->
    <div class="stats-bar">
      <div class="stat-card">
        <div class="label">Active Forwards</div>
        <div class="value" id="stat-active">0</div>
      </div>
      <div class="stat-card">
        <div class="label">Always Forward (Persistent)</div>
        <div class="value" id="stat-always">0</div>
      </div>
      <div class="stat-card">
        <div class="label">Discovered Remote Ports</div>
        <div class="value" id="stat-remote">-</div>
      </div>
    </div>

    <!-- Active Port Forwards Table -->
    <div class="section-card">
      <div class="section-header">
        <h2>Configured Port Forwards</h2>
        <button class="btn btn-sm" onclick="fetchStatus()">↻ Refresh</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>LOCAL PORT</th>
            <th>REMOTE</th>
            <th>SERVICE / LABEL</th>
            <th>MODE</th>
            <th>STATUS</th>
            <th style="text-align:right;">ACTIONS</th>
          </tr>
        </thead>
        <tbody id="forwards-body">
          <tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:30px;">Loading forwards...</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Discovered Services on Remote Machine -->
    <div class="section-card">
      <div class="section-header">
        <h2 id="remote-section-title">Discovered Services on Remote Server</h2>
        <button class="btn btn-sm" onclick="scanRemoteServices()">🔍 Scan Ports</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>PORT</th>
            <th>PROCESS</th>
            <th>IDENTIFIED SERVICE</th>
            <th>FORWARD STATUS</th>
            <th style="text-align:right;">ACTION</th>
          </tr>
        </thead>
        <tbody id="remote-services-body">
          <tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Click "Scan Ports" to detect running services on the remote server.</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Add Forward Modal -->
  <div class="modal-overlay" id="add-modal">
    <div class="modal">
      <div class="modal-header">
        <h3>Add Port Forward</h3>
        <button class="btn btn-sm" onclick="closeAddModal()" style="border:none; background:transparent;">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group" id="recent-ports-section" style="display:none;">
          <label class="recent-hint" id="recent-ports-title">Previously used — click to quick select</label>
          <div class="recent-chips" id="recent-ports-chips"></div>
        </div>
        <div class="form-group">
          <label>Local Port</label>
          <input type="number" id="modal-local-port" placeholder="e.g. 3030" oninput="syncRemotePort(); maybeAutofillLabel()" />
        </div>
        <div class="form-group">
          <label id="modal-remote-label">Remote Port</label>
          <input type="number" id="modal-remote-port" placeholder="defaults to local port" />
        </div>
        <div class="form-group">
          <label>Service Description / Label</label>
          <input type="text" id="modal-label" placeholder="e.g. Grafana Dashboard" oninput="this.dataset.autoFilled='false'" />
        </div>
        <div class="form-group">
          <label class="form-checkbox">
            <input type="checkbox" id="modal-always" checked />
            <span>Always forward (Persistent LaunchAgent / Auto-reconnect)</span>
          </label>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" onclick="closeAddModal()">Cancel</button>
        <button class="btn btn-primary" onclick="submitAddForward()">Forward Port</button>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    let currentForwards = [];
    let currentServerHost = "";
    let cachedHistory = [];

    function escapeHtml(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    }

    function showToast(msg) {
      const t = document.getElementById("toast");
      t.innerText = msg;
      t.style.display = "block";
      setTimeout(() => { t.style.display = "none"; }, 3500);
    }

    function syncRemotePort() {
      const lp = document.getElementById("modal-local-port").value;
      const rp = document.getElementById("modal-remote-port");
      if (!rp.value || rp.dataset.autoSynced === "true") {
        rp.value = lp;
        rp.dataset.autoSynced = "true";
      }
    }

    function openAddModal(local = "", remote = "", label = "") {
      document.getElementById("modal-local-port").value = local;
      document.getElementById("modal-remote-port").value = remote || local;
      document.getElementById("modal-remote-port").dataset.autoSynced = "";
      const labelEl = document.getElementById("modal-label");
      labelEl.value = label;
      // If a label was explicitly passed (e.g. from Scan), treat as manual;
      // otherwise allow auto-fill from stored SERVICE/LABEL mapping.
      labelEl.dataset.autoFilled = label ? "false" : "true";
      if (!label && local) maybeAutofillLabel();
      document.getElementById("modal-always").checked = true;
      document.getElementById("modal-remote-label").innerText = "Remote Port (on " + (currentServerHost || 'server') + ")";
      renderRecentChips(local);
      document.getElementById("add-modal").style.display = "flex";
      document.getElementById("modal-local-port").focus();
      // Refresh history in background so quick-select is always fresh
      fetch("/api/history").then(r => r.json()).then(d => {
        if (d.history) {
          const forwarded = new Set((currentForwards || []).map(f => f.local_port));
          const cur = document.getElementById("modal-local-port").value;
          cachedHistory = d.history.filter(h => !forwarded.has(h.local_port) && String(h.local_port) !== String(cur));
          renderRecentChips(cur);
        }
      }).catch(() => {});
    }

    function renderRecentChips(preselectLocal = "") {
      const section = document.getElementById("recent-ports-section");
      const container = document.getElementById("recent-ports-chips");
      const title = document.getElementById("recent-ports-title");
      let items = (cachedHistory || []).filter(h => String(h.local_port) !== String(preselectLocal));
      // Fallback to common ports when no history yet
      if (!cachedHistory || cachedHistory.length === 0) {
        const forwarded = new Set((currentForwards || []).map(f => f.local_port));
        const common = [
          {local_port: 3000, remote_port: 3000, label: "Docker Web / App"},
          {local_port: 3030, remote_port: 3030, label: "Grafana Dashboard"},
          {local_port: 8080, remote_port: 8080, label: "Docker Proxy / Web App"},
          {local_port: 9090, remote_port: 9090, label: "Prometheus Metrics"},
          {local_port: 11434, remote_port: 11434, label: "Ollama LLM API"},
        ].filter(c => !forwarded.has(c.local_port) && String(c.local_port) !== String(preselectLocal));
        if (common.length === 0) { section.style.display = "none"; return; }
        title.innerText = "Common ports — click to quick select";
        items = common;
      } else {
        title.innerText = "Previously used — click to quick select";
      }
      if (items.length === 0) { section.style.display = "none"; return; }
      section.style.display = "block";
      quickSelectCache = items;
      container.innerHTML = items.map((h, i) => {
        const remoteSuffix = (h.remote_port && h.remote_port !== h.local_port) ? ` → :${h.remote_port}` : "";
        return `<button class="recent-chip" onclick="quickSelectRecent(${i})" title="${escapeHtml(h.label || '')}">:${h.local_port}${remoteSuffix}<small>${escapeHtml((h.label || '').slice(0, 22))}</small></button>`;
      }).join("");
    }

    let quickSelectCache = [];

    function lookupStoredLabel(port) {
      const key = String(port == null ? "" : port).trim();
      if (!key) return "";
      const pools = [cachedHistory || [], quickSelectCache || [], currentForwards || []];
      for (const pool of pools) {
        const hit = pool.find(x => String(x.local_port) === key);
        if (hit && hit.label) return hit.label;
      }
      return "";
    }

    function maybeAutofillLabel() {
      const lp = document.getElementById("modal-local-port").value;
      const labelEl = document.getElementById("modal-label");
      if (!lp) return;
      // Don't clobber text the user typed manually
      if (labelEl.value && labelEl.dataset.autoFilled !== "true") return;
      const found = lookupStoredLabel(lp);
      if (found) {
        labelEl.value = found;
        labelEl.dataset.autoFilled = "true";
      }
    }

    function quickSelectRecent(i) {
      const h = quickSelectCache[i];
      if (!h) return;
      document.getElementById("modal-local-port").value = h.local_port;
      document.getElementById("modal-remote-port").value = h.remote_port || h.local_port;
      document.getElementById("modal-remote-port").dataset.autoSynced = "";
      const labelEl = document.getElementById("modal-label");
      labelEl.value = h.label || "";
      labelEl.dataset.autoFilled = "false";
      renderRecentChips(h.local_port);
      document.getElementById("modal-local-port").focus();
    }

    function closeAddModal() {
      document.getElementById("add-modal").style.display = "none";
    }

    async function fetchStatus() {
      try {
        const res = await fetch("/api/status");
        const data = await res.json();
        renderStatus(data);
      } catch (err) {
        console.error(err);
      }
    }

    function renderStatus(data) {
      const dot = document.getElementById("server-status-dot");
      const txt = document.getElementById("server-status-text");
      if (data.server_reachable) {
        dot.className = "status-dot online";
        txt.innerText = "Connected";
      } else {
        dot.className = "status-dot offline";
        txt.innerText = "Unreachable / Offline";
      }

      if (data.server_name) {
        document.getElementById("header-server-title").innerText = `DevBoost • ${data.server_name} • Port Forwards`;
        document.getElementById("remote-section-title").innerText = `Discovered Services on ${data.server_name}`;
        document.title = `DevBoost • ${data.server_name} • Port Forward Manager`;
      }
      if (data.server_host) {
        currentServerHost = data.server_host;
        const ipPart = data.server_ip ? ` (${data.server_ip})` : "";
        document.getElementById("header-server-host").innerText = `Host: ${data.server_host}${ipPart}`;
      }

      currentForwards = data.forwards;
      cachedHistory = data.history || [];
      const activeCount = data.forwards.filter(f => f.active).length;
      const alwaysCount = data.forwards.filter(f => f.always).length;
      document.getElementById("stat-active").innerText = activeCount;
      document.getElementById("stat-always").innerText = alwaysCount;

      const orphanBtn = document.getElementById("clean-orphans-btn");
      const orphanBadge = document.getElementById("orphans-badge");
      if (data.orphaned_count > 0) {
        orphanBadge.innerText = `(${data.orphaned_count} found)`;
        orphanBtn.style.borderColor = "var(--warning)";
      } else {
        orphanBadge.innerText = "";
        orphanBtn.style.borderColor = "var(--border)";
      }

      const tbody = document.getElementById("forwards-body");
      if (data.forwards.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:30px;">No port forwards configured. Click "+ Add Port Forward" above.</td></tr>';
        return;
      }

      tbody.innerHTML = data.forwards.map(f => {
        const url = `http://localhost:${f.local_port}`;
        const activeBadge = f.active 
          ? `<span class="badge badge-active">🟢 Active ${f.pid ? '(PID ' + f.pid + ')' : ''}</span>`
          : `<span class="badge badge-inactive">🔴 Stopped</span>`;
        const modeBadge = f.always
          ? `<span class="badge badge-always" title="Managed by LaunchAgent">📌 Always</span>`
          : `<span class="badge badge-session" title="Temporary SSH tunnel">⚡ Session</span>`;

        return `
          <tr>
            <td>
              <a href="${url}" target="_blank" class="port-link">
                :${f.local_port} ↗
              </a>
            </td>
            <td style="font-family:var(--font-mono); color:var(--text-muted);">
              :${f.remote_port}
            </td>
            <td>
              <strong>${f.label}</strong>
            </td>
            <td>${modeBadge}</td>
            <td>${activeBadge}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm" onclick="toggleAlways(${f.local_port}, ${!f.always})" title="${f.always ? 'Change to temporary session' : 'Make persistent (Always Forward)'}">
                  ${f.always ? 'Make Session' : 'Make Always'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteForward(${f.local_port})" title="Remove forward">
                  Delete
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function submitAddForward() {
      const localPort = parseInt(document.getElementById("modal-local-port").value, 10);
      let remotePort = parseInt(document.getElementById("modal-remote-port").value, 10);
      if (isNaN(remotePort)) remotePort = localPort;
      const label = document.getElementById("modal-label").value.trim();
      const always = document.getElementById("modal-always").checked;

      if (isNaN(localPort) || localPort <= 0) {
        alert("Please specify a valid port number.");
        return;
      }

      closeAddModal();
      showToast(`Setting up forward for port ${localPort}...`);
      try {
        const res = await fetch("/api/forward", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ local_port: localPort, remote_port: remotePort, label: label, always: always })
        });
        const d = await res.json();
        showToast(d.message || "Forward created!");
        fetchStatus();
      } catch (err) {
        alert("Failed to add forward: " + err);
      }
    }

    async function toggleAlways(localPort, makeAlways) {
      showToast(`Updating persistence rule for port ${localPort}...`);
      try {
        await fetch("/api/toggle", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ local_port: localPort, always: makeAlways })
        });
        fetchStatus();
      } catch (err) {
        alert("Error toggling persistence: " + err);
      }
    }

    async function deleteForward(localPort) {
      if (!confirm(`Are you sure you want to remove forward for port ${localPort}?`)) return;
      showToast(`Removing forward for port ${localPort}...`);
      try {
        await fetch("/api/remove", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ local_port: localPort })
        });
        fetchStatus();
      } catch (err) {
        alert("Error removing forward: " + err);
      }
    }

    async function cleanOrphans() {
      showToast("Cleaning up duplicate/hung SSH processes...");
      try {
        const res = await fetch("/api/clean", { method: "POST" });
        const d = await res.json();
        showToast(`Cleaned up ${d.killed} orphaned process(es).`);
        fetchStatus();
      } catch (err) {
        alert("Error cleaning orphans: " + err);
      }
    }

    async function scanRemoteServices() {
      const tbody = document.getElementById("remote-services-body");
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Scanning remote ports on ${currentServerHost || 'remote server'}...</td></tr>`;
      try {
        const res = await fetch("/api/scan");
        const data = await res.json();
        document.getElementById("stat-remote").innerText = data.services.length;
        if (data.services.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">No listening ports detected or server unreachable.</td></tr>';
          return;
        }

        tbody.innerHTML = data.services.map(s => {
          const isForwarded = currentForwards.some(f => f.remote_port === s.port);
          return `
            <tr>
              <td style="font-family:var(--font-mono); font-weight:600; color:#fff;">:${s.port}</td>
              <td style="font-family:var(--font-mono); color:var(--text-muted);">${s.process}</td>
              <td><strong>${s.label}</strong></td>
              <td>
                ${isForwarded 
                  ? '<span class="badge badge-active">Forwarded</span>' 
                  : '<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">Not forwarded</span>'}
              </td>
              <td style="text-align:right;">
                ${isForwarded
                  ? `<a href="http://localhost:${s.port}" target="_blank" class="btn btn-sm">Open ↗</a>`
                  : `<button class="btn btn-sm btn-primary" onclick="openAddModal(${s.port}, ${s.port}, '${s.label}')">+ Forward</button>`}
              </td>
            </tr>
          `;
        }).join("");
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--danger); padding:20px;">Scan failed: ${err}</td></tr>`;
      }
    }

    fetchStatus();
    setInterval(fetchStatus, 4000);
  </script>
</body>
</html>
"""


# -----------------------------
# HTTP REQUEST HANDLER
# -----------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            raw = self.rfile.read(content_length)
            return json.loads(raw.decode("utf-8"))
        return {}

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif path == "/api/status":
            self._send_json(get_all_forwards_status())
        elif path == "/api/scan":
            self._send_json({"services": scan_remote_services()})
        elif path == "/api/history":
            self._send_json({"history": get_port_history(limit=10)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json()

        if path == "/api/forward":
            lp = body.get("local_port")
            rp = body.get("remote_port", lp)
            label = body.get("label", "")
            always = body.get("always", False)
            add_forward(lp, rp, label=label, always=always)
            self._send_json({"ok": True, "message": f"Port {lp} forwarded successfully"})
        elif path == "/api/remove":
            lp = body.get("local_port")
            remove_forward(lp)
            self._send_json({"ok": True, "message": f"Port {lp} removed"})
        elif path == "/api/toggle":
            lp = body.get("local_port")
            always = body.get("always", False)
            toggle_always(lp, always)
            self._send_json({"ok": True, "message": f"Port {lp} persistence updated"})
        elif path == "/api/clean":
            killed = clean_orphaned_tunnels()
            self._send_json({"ok": True, "killed": killed})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def serve(port=DEFAULT_DASHBOARD_PORT):
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"DevBoost Dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")


# -----------------------------
# CLI COMMANDS
# -----------------------------

def cli_list():
    status = get_all_forwards_status()
    reachable = "🟢 Online" if status["server_reachable"] else "🔴 Offline"
    display_title = f"{status['server_name']} ({status['server_host']})"
    print(f"\n{display_title} • {reachable}")
    print("=" * 80)
    forwards = status["forwards"]
    if not forwards:
        print("No active or configured port forwards.")
        print("Use 'devboost add <port>' to forward a port.")
        return

    header = f"{'LOCAL':<10}{'REMOTE':<10}{'MODE':<12}{'STATUS':<18}{'SERVICE / LABEL':<25}"
    print(header)
    print("-" * 80)
    for f in forwards:
        local_str = f":{f['local_port']}"
        remote_str = f":{f['remote_port']}"
        mode_str = "ALWAYS" if f["always"] else "SESSION"
        status_str = f"ACTIVE (PID {f['pid']})" if f["active"] else "STOPPED"
        label_str = f["label"][:24]
        print(f"{local_str:<10}{remote_str:<10}{mode_str:<12}{status_str:<18}{label_str:<25}")
    print("=" * 80)
    if status["orphaned_count"] > 0:
        print(f"⚠️  {status['orphaned_count']} duplicate/orphaned SSH processes detected. Run 'devboost clean' to clean them up.")
    print(f"Dashboard: http://localhost:{DEFAULT_DASHBOARD_PORT}\n")


def cli_scan():
    print(f"\nScanning listening services on {SSH_HOST}...")
    services = scan_remote_services()
    if not services:
        print("No remote services found or server unreachable.")
        return
    print("=" * 70)
    print(f"{'PORT':<10}{'PROCESS':<20}{'IDENTIFIED SERVICE':<35}")
    print("-" * 70)
    for s in services:
        print(f":{s['port']:<9}{s['process']:<20}{s['label']:<35}")
    print("=" * 70)
    print("To forward any port, run: devboost add <port> [--always]\n")


def print_history_hint():
    history = get_port_history(limit=10)
    if not history:
        return
    print("\nPreviously used ports (quick select):")
    print("-" * 60)
    for h in history:
        remote = f" -> :{h['remote_port']}" if h.get("remote_port") != h.get("local_port") else ""
        print(f"  :{h['local_port']}{remote:<12} {h.get('label', '')}")
    print("\nReuse with: devboost add <port> [--always]\n")


def print_help():
    print("""DevBoost • SSH Port Forward Manager

Usage:
  devboost                       List all forwarded ports & status
  devboost ls / list             List all forwarded ports & status
  devboost add <port> [remote]   Forward a port (defaults to temporary session)
  devboost add <port> --always   Forward a port persistently (starts on boot, auto-reconnects)
  devboost add                   Show previously used ports for quick select
  devboost rm / remove <port>    Remove a port forward and stop its tunnel
  devboost clean                 Kill lingering duplicate/orphaned SSH processes
  devboost scan                  Scan listening ports and services on remote server
  devboost ui / dashboard        Open the web dashboard in Chrome/browser
  devboost serve [--port 3080]   Run the web dashboard server
""")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ls", "list", "status"):
        cli_list()
        return

    cmd = args[0]
    if cmd in ("-h", "--help", "help"):
        print_help()
    elif cmd in ("ui", "dashboard", "open"):
        subprocess.run(["open", f"http://localhost:{DEFAULT_DASHBOARD_PORT}"])
    elif cmd == "serve":
        port = DEFAULT_DASHBOARD_PORT
        if len(args) >= 3 and args[1] in ("-p", "--port"):
            port = int(args[2])
        serve(port)
    elif cmd == "clean":
        killed = clean_orphaned_tunnels()
        print(f"Cleaned up {killed} orphaned SSH forward process(es).")
    elif cmd == "scan":
        cli_scan()
    elif cmd in ("add", "forward"):
        if len(args) < 2:
            print("Error: Specify at least a port number. e.g. 'devboost add 8080'")
            print_history_hint()
            sys.exit(1)
        lp = int(args[1])
        rp = lp
        always = "--always" in args or "-a" in args
        name = ""
        remaining = [a for a in args[2:] if a not in ("--always", "-a")]
        if remaining:
            if remaining[0].isdigit():
                rp = int(remaining[0])
                remaining = remaining[1:]
        if remaining:
            name = " ".join(remaining)
        add_forward(lp, rp, label=name, always=always)
        mode = "persistent (ALWAYS)" if always else "temporary (SESSION)"
        print(f"Port {lp} -> {SSH_HOST}:{rp} forwarded [{mode}].")
    elif cmd in ("rm", "remove", "del", "delete"):
        if len(args) < 2:
            print("Error: Specify a port number to remove. e.g. 'devboost rm 8080'")
            sys.exit(1)
        lp = int(args[1])
        remove_forward(lp)
        print(f"Port {lp} forward removed.")
    else:
        print_help()


if __name__ == "__main__":
    main()
