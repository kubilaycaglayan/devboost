"""Docker capability detection and cached remote container snapshots."""

import copy
import json
import subprocess
import threading
import time

from remote_transport import run_ssh_command


_SECTION_MARKER = "__DEVBOOST_DOCKER_STATS__"
_PS_FORMAT = "{{json .}}"
_STATS_FORMAT = "{{json .}}"


def _json_lines(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def parse_docker_output(stdout):
    """Parse the stable JSON-lines sections emitted by collect_docker_snapshot."""
    before, marker, after = (stdout or "").partition(_SECTION_MARKER)
    if not marker:
        return [], []
    return _json_lines(before), _json_lines(after)


def _normalise_container(row):
    return {
        "id": row.get("ID", ""),
        "name": row.get("Names") or row.get("Name") or row.get("ID", ""),
        "image": row.get("Image", ""),
        "command": row.get("Command", ""),
        "status": row.get("Status", ""),
        "ports": row.get("Ports", ""),
        "networks": row.get("Networks", ""),
    }


def _normalise_stats(row):
    return {
        "container": row.get("Container", ""),
        "name": row.get("Name", ""),
        "cpu_percent": row.get("CPUPerc", ""),
        "memory_usage": row.get("MemUsage", ""),
        "memory_percent": row.get("MemPerc", ""),
        "network_io": row.get("NetIO", ""),
        "block_io": row.get("BlockIO", ""),
        "pids": row.get("PIDs", ""),
    }


def collect_docker_snapshot(host, timeout=30):
    """Collect one remote Docker snapshot without requiring a Docker SDK."""
    command = (
        "if ! command -v docker >/dev/null 2>&1; then "
        "echo 'Docker CLI is not installed on the server' >&2; exit 127; "
        "fi; "
        f"docker ps --format {_PS_FORMAT!r} && "
        f"printf '\\n{_SECTION_MARKER}\\n' && "
        f"docker stats --no-stream --format {_STATS_FORMAT!r}"
    )
    now = time.time()
    try:
        result = run_ssh_command(host, command, timeout=timeout, connect_timeout=5)
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "available": False, "containers": [], "stats": [],
            "updated_at": now, "message": f"Docker query timed out on {host}",
        }
    except Exception as exc:
        return {
            "ok": False, "available": False, "containers": [], "stats": [],
            "updated_at": now, "message": f"Docker query failed on {host}: {exc}",
        }

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "remote command failed").strip()
        return {
            "ok": False, "available": False, "containers": [], "stats": [],
            "updated_at": now, "message": detail[:500],
        }

    containers_raw, stats_raw = parse_docker_output(result.stdout)
    containers = [_normalise_container(row) for row in containers_raw]
    stats_by_key = {}
    for row in stats_raw:
        stats = _normalise_stats(row)
        stats_by_key[stats["container"]] = stats
        stats_by_key[stats["name"]] = stats

    for container in containers:
        stats = stats_by_key.get(container["id"]) or stats_by_key.get(container["name"])
        container["stats"] = stats or {
            "container": container["id"], "name": container["name"],
            "cpu_percent": "", "memory_usage": "", "memory_percent": "",
            "network_io": "", "block_io": "", "pids": "",
        }

    return {
        "ok": True,
        "available": True,
        "containers": containers,
        "stats": [_normalise_stats(row) for row in stats_raw],
        "updated_at": now,
        "message": "",
    }


class DockerMonitor:
    """One lightweight refresh worker per configured server."""

    def __init__(self, interval=5):
        self.interval = max(2, int(interval))
        self._lock = threading.RLock()
        self._entries = {}

    def _entry(self, server_id, host):
        with self._lock:
            entry = self._entries.get(server_id)
            if entry is None:
                entry = {
                    "host": host,
                    "snapshot": None,
                    "refreshing": False,
                    "stop": threading.Event(),
                }
                self._entries[server_id] = entry
                threading.Thread(
                    target=self._worker,
                    args=(server_id,),
                    name=f"devboost-docker-{server_id}",
                    daemon=True,
                ).start()
            else:
                entry["host"] = host
            return entry

    def _worker(self, server_id):
        while True:
            with self._lock:
                entry = self._entries.get(server_id)
                if entry is None or entry["stop"].is_set():
                    return
                host = entry["host"]
                entry["refreshing"] = True
            snapshot = collect_docker_snapshot(host)
            with self._lock:
                entry = self._entries.get(server_id)
                if entry is None:
                    return
                entry["snapshot"] = snapshot
                entry["refreshing"] = False
                stop = entry["stop"]
            if stop.wait(self.interval):
                return

    def get(self, server_id, host):
        entry = self._entry(str(server_id), host)
        with self._lock:
            snapshot = copy.deepcopy(entry["snapshot"])
            refreshing = bool(entry["refreshing"])
        if snapshot is None:
            return {
                "ok": True, "available": None, "containers": [], "stats": [],
                "updated_at": None, "refreshing": refreshing,
                "message": "Checking Docker on the server...",
            }
        snapshot["refreshing"] = refreshing
        if snapshot.get("updated_at"):
            snapshot["age_seconds"] = max(0, int(time.time() - snapshot["updated_at"]))
        return snapshot

    def stop(self):
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry["stop"].set()
