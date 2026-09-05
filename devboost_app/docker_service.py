"""Docker discovery and labeling service."""

import re
import subprocess

from docker_monitor import collect_docker_logs


def get_labels(runtime):
    cfg = runtime.load_config()
    return [dict(label) for label in cfg.get("docker_labels", []) if isinstance(label, dict)]


def apply_labels(containers, labels):
    """Attach all matching case-insensitive name rules, preserving rule order."""
    for container in containers:
        name = str(container.get("name") or "").lower()
        matched = []
        for label in labels:
            needle = str(label.get("match") or "").strip().lower()
            if label.get("enabled", True) and needle and needle in name:
                matched.append({
                    "id": str(label.get("id") or label.get("name") or needle),
                    "name": str(label.get("name") or needle),
                    "color": str(label.get("color") or "#8b949e"),
                })
        container["labels"] = matched
    return containers


def get_status(runtime, server_ref=None):
    cfg = runtime.load_config()
    server = runtime.resolve_server(cfg, server_ref)
    snapshot = runtime.DOCKER_MONITOR.get(server.get("id"), server.get("ssh_host"))
    apply_labels(snapshot.get("containers", []), get_labels(runtime))
    snapshot.update({
        "server_id": server.get("id"),
        "server_name": server.get("name") or server.get("ssh_host"),
        "server_host": server.get("ssh_host"),
    })
    return snapshot


def get_logs(runtime, server_ref, container, tail=200):
    cfg = runtime.load_config()
    server = runtime.resolve_server(cfg, server_ref)
    result = collect_docker_logs(server.get("ssh_host"), container, tail=tail)
    result.update({"server_id": server.get("id"), "container": container})
    return result


def scan_services(runtime, server_ref=None):
    cfg = runtime.load_config() if server_ref else None
    if isinstance(server_ref, dict):
        ssh_host = server_ref.get("ssh_host")
    elif server_ref and cfg is not None:
        ssh_host = runtime.resolve_server(cfg, server_ref).get("ssh_host")
    else:
        ssh_host = runtime.SSH_HOST
    services = []
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", ssh_host, "ss -tlnp 2>/dev/null"],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if "LISTEN" not in line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                addr_port = parts[3]
                match = re.search(r":(\d+)$", addr_port)
                if not match:
                    continue
                port = int(match.group(1))
                if port in (22, 53, 54):
                    continue
                proc = "Unknown"
                proc_match = re.search(r'users:\(\("([^"]+)"', line)
                if proc_match:
                    proc = proc_match.group(1)
                services.append({
                    "port": port, "address": addr_port, "process": proc,
                    "label": runtime.DEFAULT_LABELS.get(port, proc),
                })
    except Exception:
        pass
    return services
