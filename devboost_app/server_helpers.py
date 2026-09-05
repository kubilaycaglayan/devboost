"""Pure server-tab lookup and label helpers."""


def sort_servers(servers):
    """Pinned tabs first, then explicit order, then name."""
    return sorted(
        servers or [],
        key=lambda server: (
            not server.get("pinned", False),
            server.get("order", 0),
            (server.get("name") or server.get("ssh_host") or "").lower(),
        ),
    )


def get_server(cfg, server_ref):
    """Look up a server by stable id or SSH host."""
    if not server_ref:
        return None
    for server in cfg.get("servers", []):
        if server.get("id") == server_ref or server.get("ssh_host") == server_ref:
            return server
    return None


def get_server_labels(cfg, server_id):
    """Merge legacy flat labels with per-server overrides."""
    labels = {}
    if isinstance(cfg.get("labels"), dict):
        labels.update({str(key): value for key, value in cfg["labels"].items()})
    per_server = cfg.get("server_labels", {}).get(server_id, {})
    if isinstance(per_server, dict):
        labels.update({str(key): value for key, value in per_server.items()})
    return labels
