"""SSH forwarding and LaunchAgent management service."""

import glob
import os
import plistlib
import re
import shlex
import signal
import subprocess
import time
import types
import uuid


class _RuntimeGlobals(dict):
    def __init__(self, runtime, base):
        super().__init__(base)
        self._runtime = runtime

    def __missing__(self, key):
        try:
            return getattr(self._runtime, key)
        except AttributeError as exc:
            raise KeyError(key) from exc


_FUNCTIONS = ["get_plist_label","get_plist_path","_infer_server_id_for_plist","is_server_reachable","get_listening_ports","_extract_ssh_destination","get_ssh_forwards","get_launchagents","get_launchagents_for_server","_scan_all_agents_raw","get_all_forwards_status","get_servers_status","create_launchagent","remove_launchagent","remove_launchagents_for_server","kill_port_processes","kill_server_processes","add_forward","remove_forward","toggle_always","clean_orphaned_tunnels"]


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

    # Cross-server local-port conflict detection: same local port bound by another host.
    # Maps port -> owner info so the UI can say *which* connection holds the port.
    conflicting_owners = {}
    try:
        all_procs = get_ssh_forwards()
        own_dests = {d for d in (server.get("ssh_host"), server.get("ip")) if d}
        # Known servers by ssh_host/ip for attribution
        dest_to_server = {}
        for srv in cfg.get("servers", []):
            if srv.get("id") == sid:
                continue
            for key in (srv.get("ssh_host"), srv.get("ip")):
                if key:
                    dest_to_server.setdefault(key, srv)
        for p in all_procs:
            if not p.get("is_listening"):
                continue
            dest = p.get("ssh_dest", "") or ""
            if dest and dest in own_dests:
                continue
            if not dest and own_dests and any(h in (p.get("cmd") or "") for h in own_dests):
                continue
            port = p["local_port"]
            if port in conflicting_owners:
                continue
            owner = dest_to_server.get(dest)
            if owner is not None:
                conflicting_owners[port] = {
                    "server_id": owner.get("id"),
                    "name": owner.get("name") or owner.get("ssh_host"),
                    "ssh_host": owner.get("ssh_host"),
                }
            elif dest:
                # Listening proc for an unknown host — still report the raw destination
                conflicting_owners[port] = {"server_id": None, "name": dest, "ssh_host": dest}
            # else: unattributed listener (no dest parsed) — leave unclaimed
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

        owner = conflicting_owners.get(port)
        results.append({
            "local_port": port,
            "remote_port": remote_port,
            "label": label,
            "always": is_always,
            "active": is_active,
            "pid": pid,
            "orphans": orphans,
            "plist_label": agents[port]["label"] if is_always else None,
            "conflict": owner is not None and not is_active,
            "conflict_with": owner if (owner is not None and not is_active) else None,
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
    sync_agents = {}
    try:
        sync_agents = _scan_sync_agents_raw()
    except Exception:
        sync_agents = {}
    try:
        sync_server_ids = {}
        for s in cfg.get("syncs", []):
            if isinstance(s, dict) and s.get("server_id"):
                sync_server_ids.setdefault(s["server_id"], []).append(s)
    except Exception:
        sync_server_ids = {}
    out = []
    for srv in servers:
        try:
            agents = get_launchagents_for_server(srv)
            procs = get_ssh_forwards(server=srv)
            active = sum(1 for p in procs if p.get("is_listening"))
            srv_syncs = sync_server_ids.get(srv.get("id"), [])
            auto_syncs = sum(1 for s in srv_syncs if s.get("always") and s.get("id") in sync_agents)
            out.append({
                "id": srv.get("id"),
                "ssh_host": srv.get("ssh_host"),
                "name": srv.get("name") or srv.get("ssh_host"),
                "ip": srv.get("ip", ""),
                "order": srv.get("order", 0),
                "pinned": bool(srv.get("pinned", False)),
                "always_count": len(agents),
                "active_count": active,
                "sync_count": len(srv_syncs),
                "auto_sync_count": auto_syncs,
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
                "sync_count": 0,
                "auto_sync_count": 0,
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
        "ProgramArguments": (
            [get_tunnel_executable()] +
            (["--tunnel"] if get_packaged_app_executable() else []) + [
            "-N",
            "-T",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            ssh_host
            ]
        ),
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
    # Any mocked function on the compatibility runtime is a deliberate seam.
    for name in _FUNCTIONS:
        candidate = getattr(runtime, name, None)
        # The packaged app executes devboost.py as __main__. Its compatibility
        # wrappers must not replace service implementations or recurse.
        candidate_module = getattr(candidate, "__module__", None)
        runtime_module = getattr(runtime, "__name__", "devboost")
        if candidate is not None and candidate_module not in ("devboost", runtime_module):
            namespace[name] = candidate
    return namespace


def invoke(name, runtime, *args, **kwargs):
    namespace = _bound_functions(runtime)
    return namespace[name](*args, **kwargs)
