"""Configured SSH server-tab lifecycle service."""

import re
import time
import uuid
import types


class _RuntimeGlobals(dict):
    def __init__(self, runtime, base):
        super().__init__(base)
        self._runtime = runtime

    def __missing__(self, key):
        try:
            return getattr(self._runtime, key)
        except AttributeError as exc:
            raise KeyError(key) from exc


_FUNCTIONS = ["slugify_server_id","default_server_from_env","ensure_servers_migrated","get_servers","get_default_server","get_legacy_server","resolve_server","set_server_label","remove_server_label","add_server","remove_server_entry","update_server_entry","reorder_servers","set_server_pinned"]


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

def get_servers(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return sort_servers(cfg.get("servers", []))

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
    """Removes a server tab + its labels/history/syncs. Cleans tunnels/sync agents. Returns True if removed."""
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return False
    if len(cfg.get("servers", [])) <= 1:
        raise ValueError("Cannot remove the last server")
    cfg["servers"] = [s for s in cfg["servers"] if s.get("id") != server["id"]]
    cfg.get("server_labels", {}).pop(server["id"], None)
    cfg["history"] = [h for h in cfg.get("history", []) if h.get("server_id") != server["id"]]
    sync_ids = [s.get("id") for s in cfg.get("syncs", []) if isinstance(s, dict) and s.get("server_id") == server["id"]]
    cfg["syncs"] = [s for s in cfg.get("syncs", []) if not (isinstance(s, dict) and s.get("server_id") == server["id"])]
    cfg["folder_history"] = [h for h in cfg.get("folder_history", []) if h.get("server_id") != server["id"]]
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
    for sid in sync_ids:
        try:
            remove_sync_agent(sid)
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


def _bound_functions(runtime):
    base = dict(globals())
    namespace = _RuntimeGlobals(runtime, base)
    for name in _FUNCTIONS:
        original = base[name]
        namespace[name] = types.FunctionType(
            original.__code__, namespace, original.__name__,
            original.__defaults__, original.__closure__,
        )
        namespace[name].__kwdefaults__ = original.__kwdefaults__
    return namespace


def invoke(name, runtime, *args, **kwargs):
    namespace = _bound_functions(runtime)
    return namespace[name](*args, **kwargs)

