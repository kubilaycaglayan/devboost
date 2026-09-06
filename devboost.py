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
import secrets
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
import datetime
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from docker_monitor import DockerMonitor, collect_docker_snapshot, collect_docker_logs
from remote_transport import run_ssh_command
from dashboard import load_dashboard_html, read_asset
from dashboard.http import make_handler
from devboost_app import configuration
from devboost_app import usage as usage_helpers
from devboost_app import usage_service
from devboost_app import folder_sync
from devboost_app import forwarding
from devboost_app import cli
from devboost_app import server_management
from devboost_app import usage_accounts
from devboost_app import history
from devboost_app import sync_helpers
from devboost_app import docker_service
from devboost_app import local_ports
from devboost_app import server_helpers


def _code_dir():
    """Directory containing this file: repo root in dev, ~/.config/devboost when installed."""
    return os.path.dirname(os.path.realpath(__file__))


def _default_app_dir():
    """Per-user state kept separate from both source and the app bundle."""
    return os.path.expanduser("~/Library/Application Support/DevBoost")


def load_env(app_dir=None):
    """Loads key-value pairs from .env files without requiring external libraries."""
    code_dir = _code_dir()
    resolved_app = app_dir or _default_app_dir()
    candidates = []
    # Explicit override via environment always wins (set externally, before .env load).
    for _override_key in ("DEVBOOST_APP_DIR", "PORT_TRACKER_APP_DIR",
                          "DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR"):
        _override_val = os.getenv(_override_key, "")
        if _override_val:
            candidates.append(os.path.join(os.path.expanduser(_override_val), ".env"))
    candidates += [
        # Canonical state. This is writable by the packaged app and does not
        # make its background work depend on access to Documents.
        os.path.join(resolved_app, ".env"),
        # Legacy fallbacks (migration period)
        os.path.join(code_dir, "app", ".env"),
        os.path.join(code_dir, ".env"),
        os.path.expanduser("~/.config/devboost/app/.env"),
        os.path.expanduser("~/.config/devboost/.env"),
        os.path.expanduser("~/.config/port-tracker/.env"),
        os.path.join(os.getcwd(), "app", ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    seen = set()
    for env_path in candidates:
        if env_path in seen:
            continue
        seen.add(env_path)
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        if not k:
                            continue
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
# Folder-sync LaunchAgent prefix, derived from the tunnel prefix so custom
# AGENT_DOMAIN / AGENT_PREFIX installs stay namespaced (default ...-sync).
# Explicit override via DEVBOOST_SYNC_PREFIX (legacy PORT_TRACKER_SYNC_PREFIX).
_SYNC_PREFIX_OVERRIDE = _env("DEVBOOST_SYNC_PREFIX", "PORT_TRACKER_SYNC_PREFIX", "")
if _SYNC_PREFIX_OVERRIDE:
    SYNC_AGENT_PREFIX = _SYNC_PREFIX_OVERRIDE
elif AGENT_PREFIX.endswith("-forward"):
    SYNC_AGENT_PREFIX = AGENT_PREFIX[: -len("-forward")] + "-sync"
else:
    SYNC_AGENT_PREFIX = AGENT_PREFIX + "-sync"
# Folder-sync semantics (see README + dashboard modal for user-facing docs):
# - direction: "two-way" (merge, newer wins, deletions never propagate),
#   "push" (local -> remote), "pull" (remote -> local).
# - mirror (bool): one-way only — when True the destination becomes an exact
#   copy via rsync --delete. Ignored for two-way (always a safe merge), because
#   delete-propagation in both directions via plain rsync is order-dependent
#   and lossy. The dashboard disables the checkbox for two-way.
# - always (bool): False = "Once" (one-time sync, runs now + on-demand, no
#   background agent; Session equivalent). True = "Auto" (persistent LaunchAgent
#   with WatchPaths for instant local triggers + StartInterval polling for
#   remote changes; Always equivalent).
SYNC_DIRECTIONS = ("two-way", "push", "pull")
SYNC_DEFAULT_DIRECTION = "two-way"
SYNC_DEFAULT_INTERVAL = 15
SYNC_MIN_INTERVAL = 5
SYNC_MAX_INTERVAL = 600
# CODE_DIR holds the executable (repo root in development, app Resources when packaged).
CODE_DIR = _code_dir()
# APP_DIR holds per-user state (.env, config.json), separate from source code
# and the read-only app bundle. DEVBOOST_APP_DIR / DEVBOOST_CONFIG_DIR (legacy)
# override it explicitly (tests and custom deployments).
_APP_DIR_OVERRIDE = _env("DEVBOOST_APP_DIR", "PORT_TRACKER_APP_DIR", "")
_CONFIG_DIR_OVERRIDE = _env("DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR", "")
if _APP_DIR_OVERRIDE:
    APP_DIR = os.path.expanduser(_APP_DIR_OVERRIDE)
elif _CONFIG_DIR_OVERRIDE:
    APP_DIR = os.path.expanduser(_CONFIG_DIR_OVERRIDE)
else:
    APP_DIR = _default_app_dir()
# Backward compat: CONFIG_DIR aliases the state dir (old code/tests patch it).
CONFIG_DIR = APP_DIR
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
# Legacy state locations checked once for one-time migration into APP_DIR.
LEGACY_CONFIG_FILES = [
    os.path.expanduser("~/.config/devboost/app/config.json"),
    os.path.join(CODE_DIR, "app", "config.json"),
    os.path.join(CODE_DIR, "config.json"),
    os.path.expanduser("~/.config/devboost/config.json"),
    os.path.join(os.getcwd(), "config.json"),
]
BIN_DIR = os.path.join(CODE_DIR, "bin")
DASHBOARD_WRAPPER_NAME = "DevBoost-dashboard"
TUNNEL_WRAPPER_NAME = "DevBoost-tunnel"
LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
LOG_DIR = os.path.expanduser(_env("DEVBOOST_LOG_DIR", "PORT_TRACKER_LOG_DIR",
                                 os.path.join(APP_DIR, "logs")))
SSH_CONFIG_PATH = os.path.expanduser("~/.ssh/config")

# Favicon (assets/favicon.png) served at /favicon.png + /favicon.ico.
# _FAVICON_FALLBACK_B64 is a 64x64 embedded copy so the icon works even when
# the installed copy under CONFIG_DIR/assets/ is missing (e.g. old installs).
_FAVICON_FALLBACK_B64 = (
    "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"
)


def get_favicon_bytes():
    """Returns favicon PNG bytes, preferring on-disk assets, else embedded fallback."""
    candidates = [
        os.path.join(CODE_DIR, "assets", "favicon.png"),
        os.path.join(APP_DIR, "assets", "favicon.png"),
        os.path.join(os.getcwd(), "assets", "favicon.png"),
    ]
    for fav_path in candidates:
        try:
            if os.path.exists(fav_path):
                with open(fav_path, "rb") as f:
                    data = f.read()
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    return data
                # Accept any non-empty file; handler sends as image/png
                if data:
                    return data
        except Exception:
            continue
    import base64 as _b64
    try:
        return _b64.b64decode(_FAVICON_FALLBACK_B64)
    except Exception:
        return b""


def get_tunnel_executable():
    """Scoped wrapper so macOS Background Items shows DevBoost-tunnel instead of ssh."""
    packaged = get_packaged_app_executable()
    if packaged:
        return packaged
    return os.path.join(BIN_DIR, TUNNEL_WRAPPER_NAME)


def get_dashboard_executable():
    """Scoped wrapper so macOS Background Items shows DevBoost-dashboard instead of python3."""
    packaged = get_packaged_app_executable()
    if packaged:
        return packaged
    return os.path.join(BIN_DIR, DASHBOARD_WRAPPER_NAME)


def get_packaged_app_executable():
    """Returns the native DevBoost launcher when code is running from its app bundle."""
    resources = os.path.realpath(CODE_DIR)
    if os.path.basename(resources) != "Resources":
        return ""
    contents = os.path.dirname(resources)
    candidate = os.path.join(contents, "MacOS", "DevBoost")
    try:
        return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else ""
    except OSError:
        return ""


def _installed_code_dir():
    """Where the legacy installed (launchd-safe) copy lives."""
    override = _env("DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR", "")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.config/devboost")


def is_tcc_protected_path(path):
    """True for locations macOS privacy (TCC) shields from background daemons.

    launchd agents can neither EXECUTE code nor freely READ/WRITE here without
    an explicit access grant — ~/Documents, ~/Desktop, ~/Downloads, iCloud Drive.
    """
    try:
        p = os.path.normpath(os.path.expanduser(path or ""))
    except Exception:
        return False
    home = os.path.expanduser("~")
    for protected in ("Documents", "Desktop", "Downloads"):
        base = os.path.join(home, protected)
        if p == base or p.startswith(base + os.sep):
            return True
    icloud = os.path.join(home, "Library", "Mobile Documents")
    if p == icloud or p.startswith(icloud + os.sep):
        return True
    return False


def _is_launchable(path):
    """Whether launchd can exec this path (exists, executable, not TCC-shielded)."""
    try:
        return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK) \
            and not is_tcc_protected_path(path)
    except OSError:
        return False

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
    # Primary state dir follows CONFIG_FILE so tests/custom overrides stay isolated.
    try:
        state_dir = os.path.dirname(os.path.abspath(CONFIG_FILE))
    except OSError:
        state_dir = APP_DIR
    os.makedirs(state_dir, exist_ok=True)
    # CONFIG_DIR is a backward-compat alias of APP_DIR; makedirs is idempotent.
    try:
        if os.path.abspath(CONFIG_DIR) != os.path.abspath(state_dir):
            os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError:
        pass
    if not get_packaged_app_executable():
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
        except OSError:
            pass
    os.makedirs(LAUNCH_AGENTS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def slugify_server_id(*args, **kwargs):
    return server_management.invoke("slugify_server_id", sys.modules[__name__], *args, **kwargs)


def default_server_from_env(*args, **kwargs):
    return server_management.invoke("default_server_from_env", sys.modules[__name__], *args, **kwargs)


def ensure_servers_migrated(*args, **kwargs):
    return server_management.invoke("ensure_servers_migrated", sys.modules[__name__], *args, **kwargs)

def load_config():
    return configuration.load_config(sys.modules[__name__])


def save_config(cfg):
    configuration.save_config(sys.modules[__name__], cfg)


def sort_servers(servers):
    return server_helpers.sort_servers(servers)


def get_servers(*args, **kwargs):
    return server_management.invoke("get_servers", sys.modules[__name__], *args, **kwargs)


def get_server(cfg, server_ref):
    return server_helpers.get_server(cfg, server_ref)


def get_default_server(*args, **kwargs):
    return server_management.invoke("get_default_server", sys.modules[__name__], *args, **kwargs)


def get_legacy_server(*args, **kwargs):
    return server_management.invoke("get_legacy_server", sys.modules[__name__], *args, **kwargs)


def resolve_server(*args, **kwargs):
    return server_management.invoke("resolve_server", sys.modules[__name__], *args, **kwargs)


def get_server_labels(cfg, server_id):
    return server_helpers.get_server_labels(cfg, server_id)


def set_server_label(*args, **kwargs):
    return server_management.invoke("set_server_label", sys.modules[__name__], *args, **kwargs)


def remove_server_label(*args, **kwargs):
    return server_management.invoke("remove_server_label", sys.modules[__name__], *args, **kwargs)


def add_server(*args, **kwargs):
    return server_management.invoke("add_server", sys.modules[__name__], *args, **kwargs)


def remove_server_entry(*args, **kwargs):
    return server_management.invoke("remove_server_entry", sys.modules[__name__], *args, **kwargs)


def update_server_entry(*args, **kwargs):
    return server_management.invoke("update_server_entry", sys.modules[__name__], *args, **kwargs)


def reorder_servers(*args, **kwargs):
    return server_management.invoke("reorder_servers", sys.modules[__name__], *args, **kwargs)


def set_server_pinned(*args, **kwargs):
    return server_management.invoke("set_server_pinned", sys.modules[__name__], *args, **kwargs)


# -----------------------------
# Folder sync (two-way mirror)
# -----------------------------
# Semantics (user-facing, also in README + dashboard modal):
# - direction "two-way" (default): bidirectional merge, newer file wins
#   (rsync --update both ways). Deletions NEVER propagate — safe merge.
# - direction "push"/"pull": one-way. mirror=False (default) keeps extra files
#   on the destination; mirror=True makes the destination an exact copy
#   (rsync --delete, deletions propagate). Mirror is ignored for two-way.
# - always False = "Once": one-time sync (runs now + on-demand via Sync Now,
#   no background agent; the Session equivalent for ports).
# - always True = "Auto": persistent LaunchAgent (WatchPaths for instant local
#   triggers + StartInterval polling every `interval` seconds for remote
#   changes; the Always equivalent for ports).

def ensure_syncs_migrated(*args, **kwargs):
    return folder_sync.invoke("ensure_syncs_migrated", sys.modules[__name__], *args, **kwargs)


def _normalize_sync_path(*args, **kwargs):
    return sync_helpers.normalize_sync_path(*args, **kwargs)


def validate_sync_paths(*args, **kwargs):
    return sync_helpers.validate_sync_paths(*args, **kwargs)


def get_sync(*args, **kwargs):
    return folder_sync.invoke("get_sync", sys.modules[__name__], *args, **kwargs)


def get_syncs_for_server(*args, **kwargs):
    return folder_sync.invoke("get_syncs_for_server", sys.modules[__name__], *args, **kwargs)


def get_sync_plist_label(*args, **kwargs):
    return folder_sync.invoke("get_sync_plist_label", sys.modules[__name__], *args, **kwargs)


def get_sync_plist_path(*args, **kwargs):
    return folder_sync.invoke("get_sync_plist_path", sys.modules[__name__], *args, **kwargs)


def get_sync_executable_args(*args, **kwargs):
    return folder_sync.invoke("get_sync_executable_args", sys.modules[__name__], *args, **kwargs)


def _scan_sync_agents_raw(*args, **kwargs):
    return folder_sync.invoke("_scan_sync_agents_raw", sys.modules[__name__], *args, **kwargs)


def create_sync_agent(*args, **kwargs):
    return folder_sync.invoke("create_sync_agent", sys.modules[__name__], *args, **kwargs)


def restore_auto_sync_agents(*args, **kwargs):
    return folder_sync.invoke("restore_auto_sync_agents", sys.modules[__name__], *args, **kwargs)


def restore_packaged_forward_agents(*args, **kwargs):
    return folder_sync.invoke("restore_packaged_forward_agents", sys.modules[__name__], *args, **kwargs)


def remove_sync_agent(*args, **kwargs):
    return folder_sync.invoke("remove_sync_agent", sys.modules[__name__], *args, **kwargs)


def remove_sync_agents_for_server(*args, **kwargs):
    return folder_sync.invoke("remove_sync_agents_for_server", sys.modules[__name__], *args, **kwargs)


def _ssh_remote_cmd(*args, **kwargs):
    return folder_sync.invoke("_ssh_remote_cmd", sys.modules[__name__], *args, **kwargs)


def _ensure_remote_dir(*args, **kwargs):
    return folder_sync.invoke("_ensure_remote_dir", sys.modules[__name__], *args, **kwargs)


def check_rsync_prereqs(*args, **kwargs):
    return folder_sync.invoke("check_rsync_prereqs", sys.modules[__name__], *args, **kwargs)


def explain_rsync_output(*args, **kwargs):
    return sync_helpers.explain_rsync_output(*args, **kwargs)


def build_rsync_commands(*args, **kwargs):
    return sync_helpers.build_rsync_commands(*args, **kwargs)


def run_sync(*args, **kwargs):
    return folder_sync.invoke("run_sync", sys.modules[__name__], *args, **kwargs)


def add_sync(*args, **kwargs):
    return folder_sync.invoke("add_sync", sys.modules[__name__], *args, **kwargs)


def remove_sync_entry(*args, **kwargs):
    return folder_sync.invoke("remove_sync_entry", sys.modules[__name__], *args, **kwargs)


def toggle_sync_always(*args, **kwargs):
    return folder_sync.invoke("toggle_sync_always", sys.modules[__name__], *args, **kwargs)


def update_sync(*args, **kwargs):
    return folder_sync.invoke("update_sync", sys.modules[__name__], *args, **kwargs)


def get_syncs_status(*args, **kwargs):
    return folder_sync.invoke("get_syncs_status", sys.modules[__name__], *args, **kwargs)


def record_folder_history(*args, **kwargs):
    return folder_sync.invoke("record_folder_history", sys.modules[__name__], *args, **kwargs)


def get_folder_history(*args, **kwargs):
    return folder_sync.invoke("get_folder_history", sys.modules[__name__], *args, **kwargs)


def browse_local(*args, **kwargs):
    return folder_sync.invoke("browse_local", sys.modules[__name__], *args, **kwargs)


def browse_remote(*args, **kwargs):
    return folder_sync.invoke("browse_remote", sys.modules[__name__], *args, **kwargs)


def _validate_new_folder_name(*args, **kwargs):
    return folder_sync.invoke("_validate_new_folder_name", sys.modules[__name__], *args, **kwargs)


def mkdir_local(*args, **kwargs):
    return folder_sync.invoke("mkdir_local", sys.modules[__name__], *args, **kwargs)


def mkdir_remote(*args, **kwargs):
    return folder_sync.invoke("mkdir_remote", sys.modules[__name__], *args, **kwargs)


# -----------------------------
# ~/.ssh/config discovery
# -----------------------------

def parse_ssh_config_file(*args, **kwargs):
    return sync_helpers.parse_ssh_config_file(*args, **kwargs)


def get_ssh_config_hosts():
    return history.get_ssh_config_hosts(sys.modules[__name__])


def record_port_history(local_port, remote_port, label="", server_id=None):
    return history.record_port_history(sys.modules[__name__], local_port, remote_port, label, server_id)


def get_port_history(limit=8, exclude_ports=None, server_id=None):
    return history.get_port_history(sys.modules[__name__], limit, exclude_ports, server_id)


def get_plist_label(*args, **kwargs):
    return forwarding.invoke("get_plist_label", sys.modules[__name__], *args, **kwargs)


def get_plist_path(*args, **kwargs):
    return forwarding.invoke("get_plist_path", sys.modules[__name__], *args, **kwargs)


def _infer_server_id_for_plist(*args, **kwargs):
    return forwarding.invoke("_infer_server_id_for_plist", sys.modules[__name__], *args, **kwargs)


def is_server_reachable(*args, **kwargs):
    return forwarding.invoke("is_server_reachable", sys.modules[__name__], *args, **kwargs)


def get_listening_ports(*args, **kwargs):
    return forwarding.invoke("get_listening_ports", sys.modules[__name__], *args, **kwargs)


def _extract_ssh_destination(*args, **kwargs):
    return forwarding.invoke("_extract_ssh_destination", sys.modules[__name__], *args, **kwargs)


def get_ssh_forwards(*args, **kwargs):
    return forwarding.invoke("get_ssh_forwards", sys.modules[__name__], *args, **kwargs)


def get_launchagents(*args, **kwargs):
    return forwarding.invoke("get_launchagents", sys.modules[__name__], *args, **kwargs)


def get_launchagents_for_server(*args, **kwargs):
    return forwarding.invoke("get_launchagents_for_server", sys.modules[__name__], *args, **kwargs)


def _scan_all_agents_raw(*args, **kwargs):
    return forwarding.invoke("_scan_all_agents_raw", sys.modules[__name__], *args, **kwargs)


def get_all_forwards_status(*args, **kwargs):
    return forwarding.invoke("get_all_forwards_status", sys.modules[__name__], *args, **kwargs)


def get_servers_status(*args, **kwargs):
    return forwarding.invoke("get_servers_status", sys.modules[__name__], *args, **kwargs)


def create_launchagent(*args, **kwargs):
    return forwarding.invoke("create_launchagent", sys.modules[__name__], *args, **kwargs)


def remove_launchagent(*args, **kwargs):
    return forwarding.invoke("remove_launchagent", sys.modules[__name__], *args, **kwargs)


def remove_launchagents_for_server(*args, **kwargs):
    return forwarding.invoke("remove_launchagents_for_server", sys.modules[__name__], *args, **kwargs)


def kill_port_processes(*args, **kwargs):
    return forwarding.invoke("kill_port_processes", sys.modules[__name__], *args, **kwargs)


def kill_server_processes(*args, **kwargs):
    return forwarding.invoke("kill_server_processes", sys.modules[__name__], *args, **kwargs)


def add_forward(*args, **kwargs):
    return forwarding.invoke("add_forward", sys.modules[__name__], *args, **kwargs)


def remove_forward(*args, **kwargs):
    return forwarding.invoke("remove_forward", sys.modules[__name__], *args, **kwargs)


def toggle_always(*args, **kwargs):
    return forwarding.invoke("toggle_always", sys.modules[__name__], *args, **kwargs)


def clean_orphaned_tunnels(*args, **kwargs):
    return forwarding.invoke("clean_orphaned_tunnels", sys.modules[__name__], *args, **kwargs)


DOCKER_MONITOR = DockerMonitor(interval=5)


def get_docker_labels():
    return docker_service.get_labels(sys.modules[__name__])


def apply_docker_labels(containers, labels):
    return docker_service.apply_labels(containers, labels)


def get_docker_status(server_ref=None):
    return docker_service.get_status(sys.modules[__name__], server_ref)


def get_docker_logs(server_ref, container, tail=200):
    return docker_service.get_logs(sys.modules[__name__], server_ref, container, tail)


def scan_remote_services(server_ref=None):
    return docker_service.scan_services(sys.modules[__name__], server_ref)

# -----------------------------
# Local listening ports (this Mac)
# -----------------------------

def get_local_listening_ports():
    return local_ports.get_listening_ports(sys.modules[__name__])


def _pid_is_dead(pid):
    return local_ports._pid_is_dead(pid)


def kill_listening_process(pid):
    return local_ports.kill_listening_process(sys.modules[__name__], pid)

# -----------------------------
# AI PROVIDER USAGE MONITORING
# -----------------------------

USAGE_PROVIDERS = ("codex", "agy", "claude", "opencode")
USAGE_TIMEOUT = 20


def _provider_default_command(*args, **kwargs):
    return usage_helpers.provider_default_command(*args, **kwargs)


def _local_usage_path(account, provider):
    return usage_service.local_usage_path(account, provider)


def _read_local_transcript_usage(account):
    return usage_service.read_local_transcript_usage(sys.modules[__name__], account)


def _read_remote_transcript_usage(account, ssh_host):
    return usage_service.read_remote_transcript_usage(sys.modules[__name__], account, ssh_host)


def _read_codex_api(account, server=None):
    return usage_service.read_codex_api(sys.modules[__name__], account, server=server)


def _read_agy_usage(account, server=None):
    return usage_service.read_agy_usage(sys.modules[__name__], account, server=server)


def _usage_account_id(*args, **kwargs):
    return usage_helpers.usage_account_id(*args, **kwargs)


def _safe_number(*args, **kwargs):
    return usage_helpers.safe_number(*args, **kwargs)


def _first(*args, **kwargs):
    return usage_helpers.first(*args, **kwargs)


def _normalize_usage_payload(*args, **kwargs):
    return usage_helpers.normalize_usage_payload(*args, **kwargs)


def _read_usage_command(account, server=None):
    return usage_service.read_usage_command(sys.modules[__name__], account, server=server)


def _parse_opencode_stats(*args, **kwargs):
    return usage_helpers.parse_opencode_stats(*args, **kwargs)


def _read_usage_http(account):
    return usage_service.read_usage_http(sys.modules[__name__], account)


def _read_provider_api(account):
    return usage_service.read_provider_api(sys.modules[__name__], account)


def get_usage_accounts(*args, **kwargs):
    return usage_accounts.invoke("get_usage_accounts", sys.modules[__name__], *args, **kwargs)


def refresh_usage_account(*args, **kwargs):
    return usage_accounts.invoke("refresh_usage_account", sys.modules[__name__], *args, **kwargs)


def get_usage_status(*args, **kwargs):
    return usage_accounts.invoke("get_usage_status", sys.modules[__name__], *args, **kwargs)


def save_usage_account(*args, **kwargs):
    return usage_accounts.invoke("save_usage_account", sys.modules[__name__], *args, **kwargs)


def remove_usage_account(*args, **kwargs):
    return usage_accounts.invoke("remove_usage_account", sys.modules[__name__], *args, **kwargs)


def reorder_usage_accounts(*args, **kwargs):
    return usage_accounts.invoke("reorder_usage_accounts", sys.modules[__name__], *args, **kwargs)


# -----------------------------
# HTML & JS DASHBOARD TEMPLATE
# -----------------------------
HTML_DASHBOARD = load_dashboard_html()
DashboardHandler = make_handler(sys.modules[__name__])


class ReusableHTTPServer(ThreadingHTTPServer):
    """Threaded local server that can immediately retake its dashboard port."""
    allow_reuse_address = True
    daemon_threads = True


def serve(port=DEFAULT_DASHBOARD_PORT):
    # Auto sync agents execute the app's native launcher. Recreate them on
    # each packaged-app start so an update always refreshes their path.
    if get_packaged_app_executable():
        restore_packaged_forward_agents()
        restore_auto_sync_agents()
    server = ReusableHTTPServer(("127.0.0.1", port), DashboardHandler)
    # The dashboard controls local SSH, sync, Docker, and process-management
    # operations. A fresh per-launch token prevents unrelated local web/API
    # clients from invoking those endpoints without first loading the UI.
    server.auth_token = secrets.token_urlsafe(32)
    print(f"DevBoost Dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")


# -----------------------------
# CLI COMMANDS
# -----------------------------

def _extract_server_flag(*args, **kwargs):
    return cli.invoke("_extract_server_flag", sys.modules[__name__], *args, **kwargs)


def cli_list(*args, **kwargs):
    return cli.invoke("cli_list", sys.modules[__name__], *args, **kwargs)


def cli_servers(*args, **kwargs):
    return cli.invoke("cli_servers", sys.modules[__name__], *args, **kwargs)


def cli_scan(*args, **kwargs):
    return cli.invoke("cli_scan", sys.modules[__name__], *args, **kwargs)


def cli_docker(*args, **kwargs):
    return cli.invoke("cli_docker", sys.modules[__name__], *args, **kwargs)


def cli_lazydocker(*args, **kwargs):
    return cli.invoke("cli_lazydocker", sys.modules[__name__], *args, **kwargs)


def print_history_hint(*args, **kwargs):
    return cli.invoke("print_history_hint", sys.modules[__name__], *args, **kwargs)


def _format_sync_age(*args, **kwargs):
    return cli.invoke("_format_sync_age", sys.modules[__name__], *args, **kwargs)


def cli_sync_list(*args, **kwargs):
    return cli.invoke("cli_sync_list", sys.modules[__name__], *args, **kwargs)


def cli_usage(*args, **kwargs):
    return cli.invoke("cli_usage", sys.modules[__name__], *args, **kwargs)


def print_help(*args, **kwargs):
    return cli.invoke("print_help", sys.modules[__name__], *args, **kwargs)


def main(*args, **kwargs):
    return cli.invoke("main", sys.modules[__name__], *args, **kwargs)


if __name__ == "__main__":
    main()
