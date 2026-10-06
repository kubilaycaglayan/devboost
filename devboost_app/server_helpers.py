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
    """Return labels migrated to this server's namespace.

    Legacy flat labels are assigned to the bootstrap server by
    ``ensure_servers_migrated``. Reading them here would expose them on every
    server tab, so this lookup only uses the per-server map.
    """
    labels = {}
    per_server = cfg.get("server_labels", {}).get(server_id, {})
    if isinstance(per_server, dict):
        labels.update({str(key): value for key, value in per_server.items()})
    return labels
