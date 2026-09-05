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
SSH_CONFIG_PATH = os.path.expanduser("~/.ssh/config")


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


def slugify_server_id(ssh_host, existing_ids=None):
    """Converts an SSH host alias into a filesystem/plist-safe unique id."""
    base = re.sub(r"[^a-z0-9]+", "-", str(ssh_host or "server").lower()).strip("-") or "server"
    existing = set(existing_ids or [])
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def default_server_from_env(order=0):
    """Bootstraps the initial server entry from .env (legacy single-host settings)."""
    return {
        "id": slugify_server_id(SSH_HOST),
        "ssh_host": SSH_HOST,
        "name": SERVER_NAME,
        "ip": SERVER_IP,
        "order": order,
        "pinned": False,
    }


def ensure_servers_migrated(cfg):
    """Ensures cfg has a servers list; migrates legacy single-host .env config."""
    changed = False
    if not isinstance(cfg.get("servers"), list) or not cfg["servers"]:
        legacy = default_server_from_env(order=0)
        # Preserve any existing ids to avoid collision (unlikely on first migration)
        cfg["servers"] = [legacy]
        changed = True
    # Normalize server entries
    seen_ids = set()
    for idx, srv in enumerate(cfg["servers"]):
        if not isinstance(srv, dict):
            continue
        if not srv.get("ssh_host"):
            srv["ssh_host"] = SSH_HOST
        if not srv.get("id"):
            srv["id"] = slugify_server_id(srv.get("ssh_host", "server"), seen_ids)
        # De-duplicate ids
        if srv["id"] in seen_ids:
            srv["id"] = slugify_server_id(srv["id"], seen_ids)
        seen_ids.add(srv["id"])
        srv.setdefault("name", srv["ssh_host"])
        srv.setdefault("ip", "")
        srv.setdefault("order", idx)
        srv.setdefault("pinned", False)
    # Migrate legacy flat labels -> per-server labels for the bootstrap server.
    # Bootstrap = server matching current .env host, else first in file order
    # (stable: never the pinned/sorted tab order, so pinning can't move data).
    bootstrap_id = None
    for srv in cfg["servers"]:
        if isinstance(srv, dict) and srv.get("ssh_host") == SSH_HOST:
            bootstrap_id = srv.get("id")
            break
    if bootstrap_id is None:
        bootstrap_id = cfg["servers"][0]["id"] if isinstance(cfg["servers"][0], dict) else None
    if isinstance(cfg.get("labels"), dict) and cfg["labels"] and bootstrap_id:
        server_labels = cfg.setdefault("server_labels", {})
        if not server_labels.get(bootstrap_id):
            server_labels[bootstrap_id] = dict(cfg["labels"])
            changed = True
    # Tag legacy history entries missing server_id with the bootstrap server
    if isinstance(cfg.get("history"), list) and cfg["history"] and bootstrap_id:
        for h in cfg["history"]:
            if isinstance(h, dict) and not h.get("server_id"):
                h["server_id"] = bootstrap_id
                changed = True
    cfg.setdefault("server_labels", {})
    return cfg, changed


def load_config():
    ensure_dirs()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("labels", {})
                cfg.setdefault("rules", {})
                cfg.setdefault("history", [])
                cfg, changed = ensure_servers_migrated(cfg)
                if changed:
                    try:
                        with open(CONFIG_FILE, "w") as out:
                            json.dump(cfg, out, indent=2)
                    except Exception:
                        pass
                return cfg
        except Exception:
            pass
    cfg = {"labels": {}, "rules": {}, "history": [], "server_labels": {},
           "servers": [default_server_from_env(order=0)]}
    return cfg


def save_config(cfg):
    ensure_dirs()
    cfg, _ = ensure_servers_migrated(cfg)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def sort_servers(servers):
    """Pinned tabs first, then explicit order, then name."""
    return sorted(servers or [], key=lambda s: (not s.get("pinned", False), s.get("order", 0), (s.get("name") or s.get("ssh_host") or "").lower()))


def get_servers(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return sort_servers(cfg.get("servers", []))


def get_server(cfg, server_ref):
    """Looks up a server by id or ssh_host. Returns None if not found."""
    if not server_ref:
        return None
    for srv in cfg.get("servers", []):
        if srv.get("id") == server_ref or srv.get("ssh_host") == server_ref:
            return srv
    return None


def get_default_server(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    servers = get_servers(cfg)
    if servers:
        return servers[0]
    return default_server_from_env(order=0)


def get_legacy_server(cfg=None):
    """Stable bootstrap server for backward compat (existing LaunchAgents/labels).

    Unlike get_default_server() (which follows pinned/sorted tab order for UX),
    this stays pinned to the .env host so legacy plist filenames never flip when
    the user pins or reorders tabs.
    """
    cfg = cfg if cfg is not None else load_config()
    servers = cfg.get("servers", [])
    if not servers:
        return default_server_from_env(order=0)
    for srv in servers:
        if srv.get("ssh_host") == SSH_HOST:
            return srv
    return min(servers, key=lambda s: (s.get("order", 0), s.get("id", "")))


def resolve_server(cfg, server_ref=None):
    """Resolves server_ref (id or ssh_host) to a server dict, falling back to default."""
    if server_ref:
        found = get_server(cfg, server_ref)
        if found:
            return found
    return get_default_server(cfg)


def get_server_labels(cfg, server_id):
    """Per-server labels with fallback to legacy flat labels for the default server."""
    labels = {}
    # Legacy flat labels act as fallback so old configs keep working
    if isinstance(cfg.get("labels"), dict):
        labels.update({str(k): v for k, v in cfg["labels"].items()})
    per = cfg.get("server_labels", {}).get(server_id, {})
    if isinstance(per, dict):
        labels.update({str(k): v for k, v in per.items()})
    return labels


def set_server_label(cfg, server_id, port, label):
    cfg.setdefault("server_labels", {}).setdefault(server_id, {})[str(port)] = label
    # Keep legacy flat labels in sync for the bootstrap server (backward compat)
    try:
        if get_legacy_server(cfg).get("id") == server_id:
            cfg.setdefault("labels", {})[str(port)] = label
    except Exception:
        pass


def remove_server_label(cfg, server_id, port):
    cfg.get("server_labels", {}).get(server_id, {}).pop(str(port), None)
    try:
        if get_legacy_server(cfg).get("id") == server_id:
            cfg.get("labels", {}).pop(str(port), None)
    except Exception:
        pass


def add_server(ssh_host, name=None, ip=""):
    """Adds a new server tab. Raises ValueError on empty/duplicate ssh_host."""
    ssh_host = (ssh_host or "").strip()
    if not ssh_host:
        raise ValueError("ssh_host is required")
    cfg = load_config()
    for srv in cfg.get("servers", []):
        if srv.get("ssh_host") == ssh_host:
            raise ValueError(f"Server '{ssh_host}' is already added")
    existing_ids = [s.get("id") for s in cfg.get("servers", [])]
    server_id = slugify_server_id(ssh_host, existing_ids)
    max_order = max([s.get("order", 0) for s in cfg.get("servers", [])] + [-1])
    server = {
        "id": server_id,
        "ssh_host": ssh_host,
        "name": (name or "").strip() or ssh_host,
        "ip": (ip or "").strip(),
        "order": max_order + 1,
        "pinned": False,
    }
    cfg["servers"].append(server)
    save_config(cfg)
    return server


def remove_server_entry(server_id):
    """Removes a server tab + its labels/history. Cleans its tunnels/agents. Returns True if removed."""
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return False
    if len(cfg.get("servers", [])) <= 1:
        raise ValueError("Cannot remove the last server")
    cfg["servers"] = [s for s in cfg["servers"] if s.get("id") != server["id"]]
    cfg.get("server_labels", {}).pop(server["id"], None)
    cfg["history"] = [h for h in cfg.get("history", []) if h.get("server_id") != server["id"]]
    save_config(cfg)
    # Best-effort cleanup of that server's tunnels (outside config write)
    try:
        remove_launchagents_for_server(server)
    except Exception:
        pass
    try:
        kill_server_processes(server)
    except Exception:
        pass
    return True


def update_server_entry(server_id, name=None, ip=None):
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return None
    if name is not None:
        server["name"] = name.strip() or server["ssh_host"]
    if ip is not None:
        server["ip"] = ip.strip()
    save_config(cfg)
    return server


def reorder_servers(order_ids):
    """Persists tab order from a list of server ids (tabs can be drag-sorted)."""
    cfg = load_config()
    id_to_server = {s.get("id"): s for s in cfg.get("servers", [])}
    order = 0
    for sid in order_ids or []:
        if sid in id_to_server:
            id_to_server[sid]["order"] = order
            order += 1
    # Any servers not mentioned keep their relative order at the end
    remaining = [s for s in sorted(cfg.get("servers", []), key=lambda s: s.get("order", 0)) if s.get("id") not in set(order_ids or [])]
    for srv in remaining:
        srv["order"] = order
        order += 1
    save_config(cfg)
    return get_servers(cfg)


def set_server_pinned(server_id, pinned):
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return None
    server["pinned"] = bool(pinned)
    save_config(cfg)
    return server


# -----------------------------
# ~/.ssh/config discovery
# -----------------------------

def parse_ssh_config_file(path):
    """Parses an OpenSSH config file into {alias: {hostname, user, port}}.

    Handles `Host` blocks with multiple patterns and `Include` directives.
    Wildcard patterns (* ? !) are skipped — they are not connectable tabs.
    """
    results = {}
    order = []

    def _parse_file(file_path, depth=0):
        if depth > 5:
            return
        try:
            with open(os.path.expanduser(file_path), "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return
        base_dir = os.path.dirname(os.path.expanduser(file_path))
        current_aliases = []
        current_opts = {}
        pending_aliases = None

        def _flush():
            if pending_aliases is None:
                return
            for alias in pending_aliases:
                if alias not in results:
                    results[alias] = {}
                    order.append(alias)
                for k, v in current_opts.items():
                    results[alias].setdefault(k, v)

        for raw in lines:
            # Strip comments (naive: cut at first #)
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" in line:
                # Support `Key=Value` style (e.g. HostName=example.com)
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
            else:
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                key, value = parts[0].strip(), parts[1].strip()
            key_low = key.lower()
            if key_low == "host":
                _flush()
                patterns = value.split()
                # A `Host` line opens a new block; concrete aliases become pending
                pending_aliases = [p for p in patterns if p and "*" not in p and "?" not in p and "!" not in p]
                current_aliases = pending_aliases
                current_opts = {}
                if not pending_aliases:
                    pending_aliases = []  # wildcard-only block: parse but don't record
            elif key_low == "include":
                _flush()
                pending_aliases = None
                current_opts = {}
                for pat in value.split():
                    full = pat if os.path.isabs(pat) else os.path.join(base_dir, pat)
                    for expanded in sorted(glob.glob(os.path.expanduser(full))):
                        _parse_file(expanded, depth + 1)
            elif key_low in ("hostname", "user", "port"):
                if pending_aliases:
                    current_opts[key_low] = value
            # Other keys ignored for tab suggestions
        _flush()

    _parse_file(path)
    hosts = []
    for alias in order:
        opts = results.get(alias, {})
        try:
            port = int(str(opts.get("port", 22)))
        except ValueError:
            port = 22
        hosts.append({
            "ssh_host": alias,
            "hostname": opts.get("hostname", ""),
            "user": opts.get("user", ""),
            "port": port,
        })
    return hosts


def get_ssh_config_hosts():
    """Lists connectable Host entries from ~/.ssh/config (empty list if missing)."""
    try:
        if not os.path.exists(os.path.expanduser(SSH_CONFIG_PATH)):
            return []
        return parse_ssh_config_file(SSH_CONFIG_PATH)
    except Exception:
        return []


def record_port_history(local_port, remote_port, label="", server_id=None):
    """Appends/updates the recently-used port history (most recent first, max 20 per server)."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        history = cfg.setdefault("history", [])
        # Remove existing entry for the same server+local port to re-insert at front
        kept = []
        for h in history:
            h_sid = h.get("server_id") or get_legacy_server(cfg).get("id")
            if h_sid == sid and int(h.get("local_port", -1)) == int(local_port):
                continue
            kept.append(h)
        kept.insert(0, {
            "server_id": sid,
            "local_port": int(local_port),
            "remote_port": int(remote_port),
            "label": label or DEFAULT_LABELS.get(int(local_port), f"Port {local_port}"),
            "last_used": time.time(),
        })
        # Cap at 20 per server (and 100 total safety)
        per_server_counts = {}
        capped = []
        for h in kept:
            key = h.get("server_id") or sid
            per_server_counts[key] = per_server_counts.get(key, 0) + 1
            if per_server_counts[key] <= 20:
                capped.append(h)
        cfg["history"] = capped[:100]
        save_config(cfg)
    except Exception:
        pass


def get_port_history(limit=8, exclude_ports=None, server_id=None):
    """Returns most-recently-used ports for a server, optionally excluding configured ones."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        history = cfg.get("history", [])
        excluded = set(exclude_ports or [])
        result = [h for h in history
                  if (h.get("server_id") or get_legacy_server(cfg).get("id")) == sid
                  and int(h.get("local_port", -1)) not in excluded]
        # Legacy entries without server_id belong to the default server (already tagged on migration)
        return result[:limit]
    except Exception:
        return []


def get_plist_label(port, server_id=None):
    # Legacy single-arg format preserved for the default server (backward compat with
    # existing tests and already-installed LaunchAgents): "<prefix>-<port>".
    # Additional servers are namespaced: "<prefix>-<server_id>-<port>" to avoid
    # filename collisions when two hosts forward the same local port.
    if server_id is None:
        return f"{AGENT_PREFIX}-{port}"
    try:
        cfg = load_config()
        legacy_id = get_legacy_server(cfg).get("id")
    except Exception:
        legacy_id = None
    if legacy_id is not None and server_id == legacy_id:
        # Keep legacy filename for the bootstrap server so existing agents survive
        # pinning/reordering tabs. Additional servers are namespaced below.
        return f"{AGENT_PREFIX}-{port}"
    safe_sid = re.sub(r"[^A-Za-z0-9_-]+", "-", str(server_id)) or "server"
    return f"{AGENT_PREFIX}-{safe_sid}-{port}"


def get_plist_path(port, server_id=None):
    return os.path.join(LAUNCH_AGENTS_DIR, f"{get_plist_label(port, server_id)}.plist")


def _infer_server_id_for_plist(data, ssh_dest, port):
    """Best-effort attribution of a LaunchAgent plist to a server id."""
    try:
        cfg = load_config()
        servers = cfg.get("servers", [])
    except Exception:
        servers = []
    # Primary: match ssh destination arg to a known server
    if ssh_dest:
        for srv in servers:
            if ssh_dest == srv.get("ssh_host") or (srv.get("ip") and ssh_dest == srv.get("ip")):
                return srv.get("id")
    # Secondary: parse namespaced label "<prefix>-<server_id>-<port>"
    try:
        label = data.get("Label", "")
        prefix = AGENT_PREFIX + "-"
        if label.startswith(prefix):
            rest = label[len(prefix):]
            # rest is either "<port>" (legacy default) or "<server_id>-<port>"
            if "-" in rest:
                maybe_sid, _, maybe_port = rest.rpartition("-")
                if maybe_port == str(port) and any(s.get("id") == maybe_sid for s in servers):
                    return maybe_sid
    except Exception:
        pass
    return None


def is_server_reachable(ssh_host=None, server=None):
    target = None
    if isinstance(server, dict):
        target = server.get("ssh_host")
    elif ssh_host:
        target = ssh_host
    else:
        target = SSH_HOST
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", target, "true"],
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


def _extract_ssh_destination(cmdline):
    """Best-effort extraction of the ssh destination host (last bare arg)."""
    try:
        tokens = cmdline.strip().split()
        # Skip argv[0] pid in ps output? ps -A -o pid,command includes pid first.
        # Walk backwards for first token not starting with '-' and not a value of known flags.
        skip_prev = {"-L", "-o", "-i", "-F", "-p", "-l"}
        prev = None
        for tok in reversed(tokens):
            if tok.startswith("-"):
                prev = tok
                continue
            if prev in skip_prev:
                prev = None
                continue
            # Skip -L values and host:port fragments
            if ":" in tok and re.search(r"\d+:\d+", tok):
                prev = None
                continue
            if tok in ("ssh", get_tunnel_executable(), "/usr/bin/ssh"):
                prev = None
                continue
            # First plausible bare host token from the end is the destination
            if re.match(r"^[\w.@-]+$", tok):
                return tok
            prev = None
    except Exception:
        pass
    return ""


def get_ssh_forwards(server=None, server_id=None):
    """Parses ps output to find SSH forwards, optionally filtered to one server.

    Without a server filter returns all DevBoost-like forwards (backward compat).
    With a server, matches destination host or configured IP.
    """
    target_hosts = set()
    if isinstance(server, dict):
        if server.get("ssh_host"):
            target_hosts.add(server["ssh_host"])
        if server.get("ip"):
            target_hosts.add(server["ip"])
    elif server_id:
        try:
            cfg = load_config()
            srv = get_server(cfg, server_id)
            if srv:
                if srv.get("ssh_host"):
                    target_hosts.add(srv["ssh_host"])
                if srv.get("ip"):
                    target_hosts.add(srv["ip"])
        except Exception:
            pass
    forwards = []
    listening_ports = get_listening_ports()
    try:
        res = subprocess.run(["ps", "-A", "-o", "pid,command"], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if "ssh" in line and "-L" in line:
                dest = _extract_ssh_destination(line)
                if target_hosts:
                    if dest not in target_hosts and not any(h in line for h in target_hosts):
                        continue
                else:
                    # Unfiltered (legacy): keep previous broad behavior so old
                    # installs/tests still see their tunnels.
                    if not (SSH_HOST in line or (SERVER_IP and SERVER_IP in line) or "localhost" in line or "127.0.0.1" in line):
                        continue
                m = re.search(r"-L\s+(?:127\.0\.0\.1:|localhost:)?(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", line)
                if m:
                    local_p = int(m.group(1))
                    remote_p = int(m.group(2))
                    try:
                        pid = int(line.strip().split()[0])
                    except ValueError:
                        continue
                    is_listen = (local_p in listening_ports and listening_ports[local_p]["pid"] == pid)
                    forwards.append({
                        "pid": pid,
                        "local_port": local_p,
                        "remote_port": remote_p,
                        "is_listening": is_listen,
                        "cmd": line.strip(),
                        "ssh_dest": dest,
                    })
    except Exception:
        pass
    return forwards


def get_launchagents(server_id=None):
    """Finds all LaunchAgents for port forwarding (both current prefix and legacy formats).

    Returns {port: agent} for backward compat when server_id is None and only one
    server exists; otherwise returns {(server_id, port): agent} flattened per server.
    For multi-server callers use get_launchagents_for_server(server).
    """
    # Full discovery with server attribution
    by_server_port = {}
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
                        ssh_dest = ""
                        args = data.get("ProgramArguments", [])
                        for i, arg in enumerate(args):
                            if arg == "-L" and i + 1 < len(args):
                                lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                                if lm:
                                    remote_port = int(lm.group(2))
                        if args:
                            ssh_dest = args[-1] if args[-1] and not args[-1].startswith("-") else ""
                        sid = _infer_server_id_for_plist(data, ssh_dest, port)
                        by_server_port.setdefault((sid, port), {
                            "port": port,
                            "remote_port": remote_port,
                            "label": data.get("Label", get_plist_label(port)),
                            "path": plist_path,
                            "ssh_dest": ssh_dest,
                            "server_id": sid,
                        })
                except Exception:
                    by_server_port.setdefault((None, port), {
                        "port": port,
                        "remote_port": port,
                        "label": get_plist_label(port),
                        "path": plist_path,
                        "ssh_dest": "",
                        "server_id": None,
                    })
    if server_id is not None:
        return {port: agent for (sid, port), agent in by_server_port.items() if sid == server_id}
    # Backward compat: legacy callers expect {port: agent}. If only the default
    # server exists (or attribution failed -> None), flatten None/default entries.
    try:
        default_id = get_legacy_server().get("id")
    except Exception:
        default_id = None
    try:
        cfg_servers = load_config().get("servers", [])
    except Exception:
        cfg_servers = []
    if len(cfg_servers) <= 1:
        flat = {}
        for (sid, port), agent in by_server_port.items():
            if sid is None or sid == default_id:
                flat.setdefault(port, agent)
        # Also surface namespaced entries whose sid didn't resolve (e.g. tests
        # creating mock plists with a foreign host) so legacy flows keep working.
        for (sid, port), agent in by_server_port.items():
            flat.setdefault(port, agent)
        return flat
    # Multi-server without filter: return only unattributed/default flattened to
    # avoid silently mixing hosts; per-server callers should pass server_id.
    flat = {}
    for (sid, port), agent in by_server_port.items():
        if sid is None or sid == default_id:
            flat.setdefault(port, agent)
    return flat


def get_launchagents_for_server(server):
    """LaunchAgents belonging to one server: {port: agent}."""
    sid = server.get("id") if isinstance(server, dict) else server
    # Attributed discovery filtered by sid (handles legacy None attribution)
    result = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {"servers": []}
    # Direct scan to avoid flattening ambiguity
    patterns = [os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist")]
    for plist_path in glob.glob(patterns[0]):
        m = re.search(r"-(\d+)\.plist$", plist_path)
        if not m:
            continue
        port = int(m.group(1))
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        args = data.get("ProgramArguments", [])
        ssh_dest = args[-1] if args and args[-1] and not args[-1].startswith("-") else ""
        inferred = _infer_server_id_for_plist(data, ssh_dest, port)
        # Attribute: explicit match, or legacy unattributed plist whose ssh_dest
        # matches this server, or legacy default-server filename for default server.
        is_legacy_name = os.path.basename(plist_path) == f"{get_plist_label(port)}.plist"
        default_id = None
        try:
            default_id = get_legacy_server(cfg).get("id")
        except Exception:
            pass
        match = False
        if inferred == sid:
            match = True
        elif inferred is None:
            if isinstance(server, dict):
                if ssh_dest in (server.get("ssh_host"), server.get("ip")):
                    match = True
                elif is_legacy_name and sid == default_id:
                    match = True
                elif is_legacy_name and sid is None:
                    match = True
        if match:
            remote_port = port
            for i, arg in enumerate(args):
                if arg == "-L" and i + 1 < len(args):
                    lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                    if lm:
                        remote_port = int(lm.group(2))
            result[port] = {"port": port, "remote_port": remote_port,
                            "label": data.get("Label", ""), "path": plist_path,
                            "ssh_dest": ssh_dest, "server_id": sid}
    # Fallback: include anything the generic scan attributed to this sid
    for (asid, aport), agent in _scan_all_agents_raw().items():
        if asid == sid:
            result.setdefault(aport, agent)
    return result


def _scan_all_agents_raw():
    """Raw {(server_id, port): agent} scan used by per-server resolution."""
    out = {}
    patterns = [
        os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "com.*.ssh-forward-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "*ssh-forward-*.plist"),
    ]
    seen = set()
    for pat in patterns:
        for plist_path in glob.glob(pat):
            if plist_path in seen:
                continue
            seen.add(plist_path)
            m = re.search(r"-(\d+)\.plist$", plist_path)
            if not m:
                continue
            port = int(m.group(1))
            try:
                with open(plist_path, "rb") as f:
                    data = plistlib.load(f)
                args = data.get("ProgramArguments", [])
                ssh_dest = args[-1] if args and args[-1] and not args[-1].startswith("-") else ""
                remote_port = port
                for i, arg in enumerate(args):
                    if arg == "-L" and i + 1 < len(args):
                        lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                        if lm:
                            remote_port = int(lm.group(2))
                sid = _infer_server_id_for_plist(data, ssh_dest, port)
                out.setdefault((sid, port), {"port": port, "remote_port": remote_port,
                                             "label": data.get("Label", ""), "path": plist_path,
                                             "ssh_dest": ssh_dest, "server_id": sid})
            except Exception:
                continue
    return out


def get_all_forwards_status(server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    ssh_procs = get_ssh_forwards(server=server)
    agents = get_launchagents_for_server(server)

    all_ports = set()
    all_ports.update(agents.keys())
    for f in ssh_procs:
        all_ports.add(f["local_port"])
    for p_str in get_server_labels(cfg, sid).keys():
        try:
            all_ports.add(int(p_str))
        except ValueError:
            pass

    proc_map = {}
    for proc in ssh_procs:
        lp = proc["local_port"]
        proc_map.setdefault(lp, []).append(proc)

    # Cross-server local-port conflict detection: same local port bound by another host
    conflicting_ports = set()
    try:
        all_procs = get_ssh_forwards()
        other_ports = set()
        for p in all_procs:
            dest = p.get("ssh_dest", "")
            if dest and dest not in (server.get("ssh_host"), server.get("ip")):
                # Only flag if that other proc is actually listening
                if p.get("is_listening"):
                    other_ports.add(p["local_port"])
        conflicting_ports = other_ports
    except Exception:
        pass

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

        label = get_server_labels(cfg, sid).get(str(port))
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
            "conflict": port in conflicting_ports and not is_active,
        })

    return {
        "server_id": sid,
        "server_name": server.get("name") or server.get("ssh_host"),
        "server_host": server.get("ssh_host"),
        "server_ip": server.get("ip", ""),
        "server_reachable": is_server_reachable(server=server),
        "forwards": results,
        "orphaned_count": orphaned_count,
        "history": get_port_history(limit=10, exclude_ports=all_ports, server_id=sid),
    }


def get_servers_status():
    """Lightweight per-tab summary for the tab bar (no per-port detail)."""
    cfg = load_config()
    servers = get_servers(cfg)
    out = []
    for srv in servers:
        try:
            agents = get_launchagents_for_server(srv)
            procs = get_ssh_forwards(server=srv)
            active = sum(1 for p in procs if p.get("is_listening"))
            out.append({
                "id": srv.get("id"),
                "ssh_host": srv.get("ssh_host"),
                "name": srv.get("name") or srv.get("ssh_host"),
                "ip": srv.get("ip", ""),
                "order": srv.get("order", 0),
                "pinned": bool(srv.get("pinned", False)),
                "always_count": len(agents),
                "active_count": active,
            })
        except Exception:
            out.append({
                "id": srv.get("id"),
                "ssh_host": srv.get("ssh_host"),
                "name": srv.get("name") or srv.get("ssh_host"),
                "ip": srv.get("ip", ""),
                "order": srv.get("order", 0),
                "pinned": bool(srv.get("pinned", False)),
                "always_count": 0,
                "active_count": 0,
            })
    return out


def create_launchagent(local_port, remote_port, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    ssh_host = server.get("ssh_host")
    plist_path = get_plist_path(local_port, sid)
    label = get_plist_label(local_port, sid)
    # Remove any legacy-named agent for the same port+server to avoid duplicates
    # after migrating to namespaced filenames.
    try:
        legacy_path = os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-{local_port}.plist")
        if legacy_path != plist_path and os.path.exists(legacy_path):
            try:
                with open(legacy_path, "rb") as f:
                    legacy_data = plistlib.load(f)
                legacy_args = legacy_data.get("ProgramArguments", [])
                legacy_dest = legacy_args[-1] if legacy_args else ""
                if legacy_dest == ssh_host and sid != get_legacy_server(cfg).get("id"):
                    pass  # namespaced server: keep legacy only if it belongs elsewhere
                elif legacy_dest == ssh_host:
                    subprocess.run(["launchctl", "unload", "-w", legacy_path],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.remove(legacy_path)
            except OSError:
                pass
    except Exception:
        pass
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
            ssh_host
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": os.path.join(LOG_DIR, f"devboost-forward-{sid}-{local_port}.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, f"devboost-forward-{sid}-{local_port}.err"),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    subprocess.run(["launchctl", "load", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return plist_path


def remove_launchagent(local_port, server_ref=None):
    if server_ref is None:
        # Legacy behavior: remove by port across the default view (keeps old tests green)
        agents = get_launchagents()
        if local_port in agents:
            plist_path = agents[local_port]["path"]
            subprocess.run(["launchctl", "unload", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                os.remove(plist_path)
            except OSError:
                pass
        return
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    agents = get_launchagents_for_server(server)
    if local_port in agents:
        plist_path = agents[local_port]["path"]
        subprocess.run(["launchctl", "unload", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.remove(plist_path)
        except OSError:
            pass


def remove_launchagents_for_server(server):
    agents = get_launchagents_for_server(server)
    for port, agent in agents.items():
        try:
            subprocess.run(["launchctl", "unload", "-w", agent["path"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(agent["path"])
        except OSError:
            pass


def kill_port_processes(local_port, server_ref=None):
    if server_ref is None:
        ssh_procs = get_ssh_forwards()
    else:
        cfg = load_config()
        ssh_procs = get_ssh_forwards(server=resolve_server(cfg, server_ref))
    killed = 0
    for p in ssh_procs:
        if p["local_port"] == local_port:
            try:
                os.kill(p["pid"], signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


def kill_server_processes(server):
    procs = get_ssh_forwards(server=server if isinstance(server, dict) else None,
                             server_id=None if isinstance(server, dict) else server)
    killed = 0
    for p in procs:
        try:
            os.kill(p["pid"], signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return killed


def add_forward(local_port, remote_port=None, label="", always=False, server_ref=None):
    if remote_port is None:
        remote_port = local_port
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    ssh_host = server.get("ssh_host")
    existing_labels = get_server_labels(cfg, sid)
    if label:
        set_server_label(cfg, sid, local_port, label)
    elif str(local_port) not in existing_labels:
        set_server_label(cfg, sid, local_port, DEFAULT_LABELS.get(int(local_port), f"Port {local_port}"))
    save_config(cfg)
    record_port_history(local_port, remote_port,
                        label=get_server_labels(load_config(), sid).get(str(local_port), ""),
                        server_id=sid)

    kill_port_processes(local_port, server_ref=sid)

    if always:
        create_launchagent(local_port, remote_port, server_ref=sid)
    else:
        remove_launchagent(local_port, server_ref=sid)
        cmd = [
            "/usr/bin/ssh",
            "-fN",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            ssh_host
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_forward(local_port, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    remove_launchagent(local_port, server_ref=sid)
    kill_port_processes(local_port, server_ref=sid)
    cfg = load_config()
    remove_server_label(cfg, sid, local_port)
    save_config(cfg)


def toggle_always(local_port, make_always, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    status = get_all_forwards_status(server.get("id"))
    current = next((f for f in status["forwards"] if f["local_port"] == local_port), None)
    remote_port = current["remote_port"] if current else local_port
    label = current["label"] if current else ""
    add_forward(local_port, remote_port, label=label, always=make_always, server_ref=server.get("id"))


def clean_orphaned_tunnels(server_ref=None):
    if server_ref:
        cfg = load_config()
        ssh_procs = get_ssh_forwards(server=resolve_server(cfg, server_ref))
    else:
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


def scan_remote_services(server_ref=None):
    cfg = load_config() if server_ref else None
    if isinstance(server_ref, dict):
        ssh_host = server_ref.get("ssh_host")
    elif server_ref and cfg is not None:
        ssh_host = resolve_server(cfg, server_ref).get("ssh_host")
    else:
        ssh_host = SSH_HOST
    services = []
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", ssh_host, "ss -tlnp 2>/dev/null"],
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

    .tabs-bar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 20px;
      padding: 10px 12px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .tab {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 7px 10px 7px 12px;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: #0d1117;
      color: var(--text);
      font-size: 13px;
      cursor: pointer;
      user-select: none;
      max-width: 260px;
    }
    .tab:hover { border-color: #8b949e; color: #fff; }
    .tab.active { border-color: var(--accent); background: rgba(88,166,255,0.12); color: #fff; }
    .tab.dragging { opacity: 0.45; }
    .tab .tab-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
    .tab .tab-meta { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }
    .tab .tab-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; }
    .tab .tab-dot.online { background: var(--success); box-shadow: 0 0 6px var(--success); }
    .tab button { background: transparent; border: none; color: var(--text-muted); cursor: pointer; font-size: 12px; padding: 0 2px; }
    .tab button:hover { color: #fff; }
    .tab-add {
      border-style: dashed;
      color: var(--text-muted);
      font-weight: 600;
    }
    .server-list { display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; margin-top: 8px; }
    .server-row {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; background: #0d1117;
    }
    .server-row .srv-main { min-width: 0; }
    .server-row .srv-host { font-family: var(--font-mono); font-weight: 600; color: #fff; font-size: 13px; }
    .server-row .srv-sub { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .search-input { margin-bottom: 4px; }

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

    <!-- SSH connection tabs -->
    <div class="tabs-bar" id="tabs-bar">
      <span style="font-size:12px; color:var(--text-muted);">Loading servers...</span>
    </div>

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

  <!-- Add Server Modal (selective import from ~/.ssh/config) -->
  <div class="modal-overlay" id="server-modal">
    <div class="modal" style="max-width:520px;">
      <div class="modal-header">
        <h3>Add SSH Connection Tab</h3>
        <button class="btn btn-sm" onclick="closeServerModal()" style="border:none; background:transparent;">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Search ~/.ssh/config hosts</label>
          <input type="text" id="server-search" class="search-input" placeholder="e.g. prod, ubuntu..." oninput="renderSshHostList()" />
        </div>
        <div class="server-list" id="ssh-host-list">
          <div style="color:var(--text-muted); font-size:13px;">Loading SSH hosts...</div>
        </div>
        <div style="border-top:1px solid var(--border); margin:16px 0;"></div>
        <div class="form-group">
          <label>Or add manually (SSH Host alias)</label>
          <input type="text" id="manual-ssh-host" placeholder="e.g. my-remote-server" />
        </div>
        <div class="form-group">
          <label>Display name (optional)</label>
          <input type="text" id="manual-server-name" placeholder="e.g. Production Server" />
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" onclick="closeServerModal()">Cancel</button>
        <button class="btn btn-primary" onclick="submitManualServer()">Add Manually</button>
      </div>
    </div>
  </div>

  <div id="toast"></div>

  <script>
    let currentForwards = [];
    let currentServerHost = "";
    let currentServerId = localStorage.getItem("devboost-active-server") || "";
    let cachedHistory = [];
    let allServers = [];
    let sshHostCache = [];
    let serverReachability = {};

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
      // Refresh history in background so quick-select is always fresh (per active tab)
      fetch("/api/history" + serverQuery()).then(r => r.json()).then(d => {
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

    function activeServer() {
      return (allServers || []).find(s => s.id === currentServerId) || allServers[0] || null;
    }

    function serverQuery() {
      return currentServerId ? `?server=${encodeURIComponent(currentServerId)}` : "";
    }

    async function fetchServers() {
      try {
        const res = await fetch("/api/servers");
        const data = await res.json();
        allServers = data.servers || [];
        if (!allServers.some(s => s.id === currentServerId)) {
          currentServerId = (allServers[0] || {}).id || "";
          localStorage.setItem("devboost-active-server", currentServerId);
        }
        renderTabs();
      } catch (err) {
        console.error(err);
      }
    }

    function renderTabs() {
      const bar = document.getElementById("tabs-bar");
      if (!allServers.length) {
        bar.innerHTML = '<span style="font-size:12px; color:var(--text-muted);">No servers yet.</span>';
        return;
      }
      bar.innerHTML = allServers.map(s => {
        const isActive = s.id === currentServerId;
        const dotCls = serverReachability[s.id] === true ? "tab-dot online" : (serverReachability[s.id] === false ? "tab-dot" : "tab-dot");
        const pinIcon = s.pinned ? "📍" : "📌";
        return `<div class="tab ${isActive ? 'active' : ''}" draggable="true" data-server-id="${s.id}"
            onclick="selectServer('${s.id}')"
            ondragstart="onTabDragStart(event, '${s.id}')" ondragover="onTabDragOver(event)" ondrop="onTabDrop(event, '${s.id}')" ondragend="onTabDragEnd(event)"
            title="${escapeHtml(s.ssh_host)}${s.ip ? ' (' + escapeHtml(s.ip) + ')' : ''} — drag to reorder">
          <span class="${dotCls}"></span>
          <span class="tab-name">${escapeHtml(s.name || s.ssh_host)}</span>
          <span class="tab-meta">${s.active_count || 0}● ${s.always_count || 0}📌</span>
          <button onclick="event.stopPropagation(); togglePin('${s.id}')" title="${s.pinned ? 'Unpin tab' : 'Pin tab (stays first)'}">${pinIcon}</button>
          <button onclick="event.stopPropagation(); removeServerTab('${s.id}')" title="Remove tab">✕</button>
        </div>`;
      }).join("") + `<button class="tab tab-add" onclick="openServerModal()">+ Add Server</button>`;
    }

    let draggedServerId = null;
    function onTabDragStart(e, sid) { draggedServerId = sid; e.currentTarget.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; }
    function onTabDragOver(e) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }
    function onTabDragEnd(e) { e.currentTarget.classList.remove("dragging"); }
    async function onTabDrop(e, targetId) {
      e.preventDefault();
      if (!draggedServerId || draggedServerId === targetId) return;
      const ids = allServers.map(s => s.id);
      const from = ids.indexOf(draggedServerId);
      const to = ids.indexOf(targetId);
      if (from < 0 || to < 0) return;
      ids.splice(to, 0, ids.splice(from, 1)[0]);
      allServers.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
      renderTabs();
      try {
        await fetch("/api/servers/reorder", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ order: ids }) });
        fetchServers();
      } catch (err) { console.error(err); }
      draggedServerId = null;
    }

    function selectServer(sid) {
      if (currentServerId === sid) return;
      currentServerId = sid;
      localStorage.setItem("devboost-active-server", sid);
      document.getElementById("remote-services-body").innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Click "Scan Ports" to detect running services on the remote server.</td></tr>';
      document.getElementById("stat-remote").innerText = "-";
      renderTabs();
      fetchStatus();
    }

    async function togglePin(sid) {
      const srv = allServers.find(s => s.id === sid);
      if (!srv) return;
      try {
        await fetch("/api/servers/pin", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid, pinned: !srv.pinned }) });
        fetchServers();
      } catch (err) { alert("Failed to pin tab: " + err); }
    }

    async function removeServerTab(sid) {
      const srv = allServers.find(s => s.id === sid);
      if (!srv) return;
      if (allServers.length <= 1) { alert("Cannot remove the last server tab."); return; }
      if (!confirm(`Remove tab "${srv.name || srv.ssh_host}"? Its tunnels and persistent agents will be stopped.`)) return;
      try {
        const res = await fetch("/api/servers/remove", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to remove server"); return; }
        if (currentServerId === sid) { currentServerId = ""; localStorage.removeItem("devboost-active-server"); }
        showToast(d.message || "Server removed");
        await fetchServers();
        fetchStatus();
      } catch (err) { alert("Failed to remove server: " + err); }
    }

    function openServerModal() {
      document.getElementById("server-search").value = "";
      document.getElementById("manual-ssh-host").value = "";
      document.getElementById("manual-server-name").value = "";
      document.getElementById("server-modal").style.display = "flex";
      loadSshHosts();
    }
    function closeServerModal() { document.getElementById("server-modal").style.display = "none"; }

    async function loadSshHosts() {
      const list = document.getElementById("ssh-host-list");
      list.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">Loading SSH hosts from ~/.ssh/config...</div>';
      try {
        const res = await fetch("/api/ssh-hosts");
        const data = await res.json();
        sshHostCache = data.hosts || [];
        renderSshHostList();
      } catch (err) {
        list.innerHTML = `<div style="color:var(--danger); font-size:13px;">Failed to load ~/.ssh/config: ${err}</div>`;
      }
    }

    function renderSshHostList() {
      const list = document.getElementById("ssh-host-list");
      const q = (document.getElementById("server-search").value || "").toLowerCase();
      const items = (sshHostCache || []).filter(h => !q || h.ssh_host.toLowerCase().includes(q) || (h.hostname || "").toLowerCase().includes(q));
      if (!items.length) {
        list.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">No matching SSH hosts. Add manually below.</div>';
        return;
      }
      filteredSshHosts = items;
      list.innerHTML = items.map((h, i) => {
        const sub = [h.user ? h.user + "@" : "", h.hostname || "", h.port && h.port !== 22 ? ":" + h.port : ""].join("");
        return `<div class="server-row">
          <div class="srv-main">
            <div class="srv-host">${escapeHtml(h.ssh_host)}</div>
            <div class="srv-sub">${escapeHtml(sub || "ssh-config entry")}</div>
          </div>
          ${h.added
            ? `<span class="badge badge-active">Added</span>`
            : `<button class="btn btn-sm btn-primary" onclick="addServerFromSshByIndex(${i})">+ Add Tab</button>`}
        </div>`;
      }).join("");
    }

    let filteredSshHosts = [];

    async function addServerFromSshByIndex(i) {
      const h = (filteredSshHosts || [])[i] || {};
      const sshHost = h.ssh_host;
      if (!sshHost) return;
      showToast(`Adding ${sshHost}...`);
      try {
        const res = await fetch("/api/servers", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ ssh_host: sshHost, name: sshHost, ip: h.hostname || "" }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to add server"); return; }
        closeServerModal();
        showToast(`Tab "${sshHost}" added`);
        await fetchServers();
        selectServer(d.server.id);
      } catch (err) { alert("Failed to add server: " + err); }
    }

    async function submitManualServer() {
      const sshHost = document.getElementById("manual-ssh-host").value.trim();
      const name = document.getElementById("manual-server-name").value.trim();
      if (!sshHost) { alert("Enter an SSH Host alias."); return; }
      try {
        const res = await fetch("/api/servers", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ ssh_host: sshHost, name: name || sshHost }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to add server"); return; }
        closeServerModal();
        await fetchServers();
        selectServer(d.server.id);
      } catch (err) { alert("Failed to add server: " + err); }
    }

    async function fetchStatus() {
      try {
        const res = await fetch("/api/status" + serverQuery());
        const data = await res.json();
        if (data.servers) { allServers = data.servers; if (!currentServerId && allServers[0]) { currentServerId = allServers[0].id; localStorage.setItem("devboost-active-server", currentServerId); } renderTabs(); }
        if (data.server_id) { serverReachability[data.server_id] = !!data.server_reachable; }
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
        if (data.server_id) { currentServerId = data.server_id; localStorage.setItem("devboost-active-server", currentServerId); }
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
        const conflictBadge = f.conflict ? `<span class="badge badge-inactive" title="Another server tab is already listening on this local port">⚠️ Port in use by another tab</span>` : "";
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
              <strong>${escapeHtml(f.label)}</strong><br/>${conflictBadge}
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
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort, remote_port: remotePort, label: label, always: always })
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
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort, always: makeAlways })
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
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort })
        });
        fetchStatus();
      } catch (err) {
        alert("Error removing forward: " + err);
      }
    }

    async function cleanOrphans() {
      showToast("Cleaning up duplicate/hung SSH processes...");
      try {
        const res = await fetch("/api/clean", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ server_id: currentServerId }) });
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
        const res = await fetch("/api/scan" + serverQuery());
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
                  : `<button class="btn btn-sm btn-primary" onclick='openAddModal(${s.port}, ${s.port}, ${JSON.stringify(s.label || "")})'>+ Forward</button>`}
              </td>
            </tr>
          `;
        }).join("");
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--danger); padding:20px;">Scan failed: ${err}</td></tr>`;
      }
    }

    fetchServers().then(fetchStatus);
    setInterval(fetchStatus, 4000);
    setInterval(fetchServers, 15000);
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

    def _server_param(self, query, body=None):
        # ?server=<id|ssh_host> on GET, or server_id/server field on POST
        srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
        if not srv and isinstance(body, dict):
            srv = body.get("server_id") or body.get("server")
        return srv

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif path == "/api/status":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            data = get_all_forwards_status(srv)
            data["servers"] = get_servers_status()
            self._send_json(data)
        elif path == "/api/servers":
            self._send_json({"servers": get_servers_status()})
        elif path == "/api/ssh-hosts":
            cfg = load_config()
            added_hosts = {s.get("ssh_host") for s in cfg.get("servers", [])}
            added_ids = {s.get("ssh_host"): s.get("id") for s in cfg.get("servers", [])}
            hosts = []
            for h in get_ssh_config_hosts():
                hosts.append({
                    **h,
                    "added": h["ssh_host"] in added_hosts,
                    "server_id": added_ids.get(h["ssh_host"]),
                })
            self._send_json({"hosts": hosts})
        elif path == "/api/scan":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"services": scan_remote_services(srv)})
        elif path == "/api/history":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"history": get_port_history(limit=10, server_id=srv)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json()
        except Exception:
            body = {}

        if path == "/api/forward":
            lp = body.get("local_port")
            rp = body.get("remote_port", lp)
            label = body.get("label", "")
            always = body.get("always", False)
            srv = body.get("server_id") or body.get("server")
            add_forward(lp, rp, label=label, always=always, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} forwarded successfully"})
        elif path == "/api/remove":
            lp = body.get("local_port")
            srv = body.get("server_id") or body.get("server")
            remove_forward(lp, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} removed"})
        elif path == "/api/toggle":
            lp = body.get("local_port")
            always = body.get("always", False)
            srv = body.get("server_id") or body.get("server")
            toggle_always(lp, always, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} persistence updated"})
        elif path == "/api/clean":
            srv = body.get("server_id") or body.get("server")
            killed = clean_orphaned_tunnels(server_ref=srv)
            self._send_json({"ok": True, "killed": killed})
        elif path == "/api/servers":
            ssh_host = (body.get("ssh_host") or "").strip()
            name = (body.get("name") or "").strip()
            ip = (body.get("ip") or "").strip()
            if not ssh_host:
                self._send_json({"ok": False, "message": "ssh_host is required"}, status=400)
                return
            try:
                server = add_server(ssh_host, name=name or ssh_host, ip=ip)
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            self._send_json({"ok": True, "server": server, "message": f"Server '{ssh_host}' added"})
        elif path == "/api/servers/remove":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            if not sid:
                self._send_json({"ok": False, "message": "server id is required"}, status=400)
                return
            try:
                ok = remove_server_entry(sid)
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            if not ok:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "message": "Server removed"})
        elif path == "/api/servers/reorder":
            order = body.get("order", [])
            servers = reorder_servers(order)
            self._send_json({"ok": True, "servers": servers})
        elif path == "/api/servers/pin":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            pinned = body.get("pinned", True)
            server = set_server_pinned(sid, pinned)
            if not server:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "server": server})
        elif path == "/api/servers/update":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            server = update_server_entry(sid, name=body.get("name"), ip=body.get("ip"))
            if not server:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "server": server})
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

def _extract_server_flag(args):
    """Extracts --server <id|host> / -s <id|host> from CLI args. Returns (server_ref, remaining_args)."""
    server_ref = None
    remaining = []
    i = 0
    while i < len(args):
        if args[i] in ("--server", "-s") and i + 1 < len(args):
            server_ref = args[i + 1]
            i += 2
        elif args[i].startswith("--server="):
            server_ref = args[i].split("=", 1)[1]
            i += 1
        else:
            remaining.append(args[i])
            i += 1
    return server_ref, remaining


def cli_list(server_ref=None, show_all=False):
    cfg = load_config()
    servers = get_servers(cfg)
    targets = servers if show_all else [resolve_server(cfg, server_ref)]
    for status in [get_all_forwards_status(s.get("id")) for s in targets]:
        reachable = "🟢 Online" if status["server_reachable"] else "🔴 Offline"
        display_title = f"{status['server_name']} ({status['server_host']})"
        print(f"\n{display_title} • {reachable}")
        print("=" * 80)
        forwards = status["forwards"]
        if not forwards:
            print("No active or configured port forwards.")
            print("Use 'devboost add <port>' to forward a port.")
            continue

        header = f"{'LOCAL':<10}{'REMOTE':<10}{'MODE':<12}{'STATUS':<18}{'SERVICE / LABEL':<25}"
        print(header)
        print("-" * 80)
        for f in forwards:
            local_str = f":{f['local_port']}"
            remote_str = f":{f['remote_port']}"
            mode_str = "ALWAYS" if f["always"] else "SESSION"
            status_str = f"ACTIVE (PID {f['pid']})" if f["active"] else "STOPPED"
            label_str = (f["label"] or "")[:24]
            flag = " ⚠️" if f.get("conflict") else ""
            print(f"{local_str:<10}{remote_str:<10}{mode_str:<12}{status_str:<18}{label_str:<25}{flag}")
        print("=" * 80)
        if status["orphaned_count"] > 0:
            print(f"⚠️  {status['orphaned_count']} duplicate/orphaned SSH processes detected. Run 'devboost clean' to clean them up.")
    print(f"Dashboard: http://localhost:{DEFAULT_DASHBOARD_PORT}\n")


def cli_servers():
    servers = get_servers_status()
    print("\nConfigured SSH connections (tabs):")
    print("=" * 78)
    print(f"{'ID':<24}{'NAME':<24}{'SSH HOST':<20}{'TABS':<10}")
    print("-" * 78)
    for s in servers:
        flags = f"{s['active_count']} active"
        if s.get("pinned"):
            flags += " 📍"
        print(f"{s['id']:<24}{(s['name'] or '')[:23]:<24}{(s['ssh_host'] or '')[:19]:<20}{flags:<10}")
    print("=" * 78)
    hosts = get_ssh_config_hosts()
    if hosts:
        cfg = load_config()
        added = {srv.get("ssh_host") for srv in cfg.get("servers", [])}
        available = [h for h in hosts if h["ssh_host"] not in added]
        if available:
            print("\nAvailable in ~/.ssh/config (add with: devboost server add <host>):")
            for h in available[:20]:
                detail = h.get("hostname", "")
                print(f"  {h['ssh_host']:<24} {detail}")
    print("")


def cli_scan(server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    print(f"\nScanning listening services on {server.get('ssh_host')}...")
    services = scan_remote_services(server.get("id"))
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


def print_history_hint(server_ref=None):
    history = get_port_history(limit=10, server_id=server_ref)
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
  devboost                       List forwards for the default server tab
  devboost ls [--server ID] [--all]   List forwards (one tab or all tabs)
  devboost add <port> [remote] [--server ID] [--always]   Forward a port
  devboost add                   Show previously used ports for quick select
  devboost rm <port> [--server ID]    Remove a port forward and stop its tunnel
  devboost clean [--server ID]   Kill lingering duplicate/orphaned SSH processes
  devboost scan [--server ID]    Scan listening ports on a remote server
  devboost server list           List SSH connection tabs
  devboost server add <ssh-host> [display-name]   Add a tab (host should exist in ~/.ssh/config)
  devboost server rm <id>        Remove a tab (stops its tunnels)
  devboost server pin <id> [--off]    Pin/unpin a tab (pinned tabs sort first)
  devboost ui / dashboard        Open the web dashboard in Chrome/browser
  devboost serve [--port 3080]   Run the web dashboard server

Tabs: the dashboard shows one tab per SSH connection. Add tabs from
~/.ssh/config hosts (selective — nothing is auto-added), then drag to
reorder and pin important ones. Per-command target a tab with --server.
""")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ls", "list", "status"):
        server_ref, rest = _extract_server_flag(args[1:] if args else [])
        show_all = "--all" in rest
        # `devboost` bare with no servers configured still works via migration
        cli_list(server_ref=server_ref, show_all=show_all)
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
    elif cmd == "server":
        if len(args) < 2:
            cli_servers()
            return
        sub = args[1]
        if sub == "list":
            cli_servers()
        elif sub == "add":
            if len(args) < 3:
                print("Error: Specify an SSH host. e.g. 'devboost server add my-remote-server'")
                print("Available hosts in ~/.ssh/config:")
                for h in get_ssh_config_hosts()[:20]:
                    print(f"  {h['ssh_host']}")
                sys.exit(1)
            ssh_host = args[2]
            name = args[3] if len(args) > 3 else ssh_host
            try:
                server = add_server(ssh_host, name=name)
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            print(f"Server tab '{server['name']}' ({server['ssh_host']}) added.")
        elif sub in ("rm", "remove", "del", "delete"):
            if len(args) < 3:
                print("Error: Specify a server id. e.g. 'devboost server rm my-host'")
                sys.exit(1)
            try:
                ok = remove_server_entry(args[2])
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            print("Server tab removed." if ok else "Server not found.")
        elif sub == "pin":
            if len(args) < 3:
                print("Error: Specify a server id. e.g. 'devboost server pin my-host'")
                sys.exit(1)
            pinned = "--off" not in args
            server = set_server_pinned(args[2], pinned)
            print(f"Server '{args[2]}' {'pinned 📍' if pinned else 'unpinned'}." if server else "Server not found.")
        else:
            print_help()
    elif cmd == "clean":
        server_ref, _ = _extract_server_flag(args[1:])
        killed = clean_orphaned_tunnels(server_ref=server_ref)
        print(f"Cleaned up {killed} orphaned SSH forward process(es).")
    elif cmd == "scan":
        server_ref, _ = _extract_server_flag(args[1:])
        cli_scan(server_ref=server_ref)
    elif cmd in ("add", "forward"):
        server_ref, filtered = _extract_server_flag(args[1:])
        fargs = [cmd] + filtered
        if len(fargs) < 2:
            print("Error: Specify at least a port number. e.g. 'devboost add 8080'")
            print_history_hint(server_ref=server_ref)
            sys.exit(1)
        lp = int(fargs[1])
        rp = lp
        always = "--always" in fargs or "-a" in fargs
        name = ""
        remaining = [a for a in fargs[2:] if a not in ("--always", "-a")]
        if remaining:
            if remaining[0].isdigit():
                rp = int(remaining[0])
                remaining = remaining[1:]
        if remaining:
            name = " ".join(remaining)
        add_forward(lp, rp, label=name, always=always, server_ref=server_ref)
        cfg = load_config()
        ssh_host = resolve_server(cfg, server_ref).get("ssh_host")
        mode = "persistent (ALWAYS)" if always else "temporary (SESSION)"
        print(f"Port {lp} -> {ssh_host}:{rp} forwarded [{mode}].")
    elif cmd in ("rm", "remove", "del", "delete"):
        server_ref, filtered = _extract_server_flag(args[1:])
        fargs = [cmd] + filtered
        if len(fargs) < 2:
            print("Error: Specify a port number to remove. e.g. 'devboost rm 8080'")
            sys.exit(1)
        lp = int(fargs[1])
        remove_forward(lp, server_ref=server_ref)
        print(f"Port {lp} forward removed.")
    else:
        print_help()


if __name__ == "__main__":
    main()
