"""SSH-host discovery and recently-used port history."""

import os
import time


def get_ssh_config_hosts(runtime):
    try:
        if not os.path.exists(os.path.expanduser(runtime.SSH_CONFIG_PATH)):
            return []
        return runtime.parse_ssh_config_file(runtime.SSH_CONFIG_PATH)
    except Exception:
        return []


def record_port_history(runtime, local_port, remote_port, label="", server_id=None):
    try:
        cfg = runtime.load_config()
        server = runtime.resolve_server(cfg, server_id)
        sid = server.get("id")
        history = cfg.setdefault("history", [])
        kept = []
        for entry in history:
            entry_sid = entry.get("server_id") or runtime.get_legacy_server(cfg).get("id")
            if entry_sid == sid and int(entry.get("local_port", -1)) == int(local_port):
                continue
            kept.append(entry)
        kept.insert(0, {
            "server_id": sid,
            "local_port": int(local_port),
            "remote_port": int(remote_port),
            "label": label or runtime.DEFAULT_LABELS.get(int(local_port), f"Port {local_port}"),
            "last_used": time.time(),
        })
        per_server_counts = {}
        capped = []
        for entry in kept:
            key = entry.get("server_id") or sid
            per_server_counts[key] = per_server_counts.get(key, 0) + 1
            if per_server_counts[key] <= 20:
                capped.append(entry)
        cfg["history"] = capped[:100]
        runtime.save_config(cfg)
    except Exception:
        pass


def get_port_history(runtime, limit=8, exclude_ports=None, server_id=None):
    try:
        cfg = runtime.load_config()
        server = runtime.resolve_server(cfg, server_id)
        sid = server.get("id")
        excluded = set(exclude_ports or [])
        return [entry for entry in cfg.get("history", [])
                if (entry.get("server_id") or runtime.get_legacy_server(cfg).get("id")) == sid
                and int(entry.get("local_port", -1)) not in excluded][:limit]
    except Exception:
        return []
