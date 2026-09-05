"""Folder synchronization service implementation.

Functions are cloned into a runtime-bound namespace by invoke(). This keeps the
service independent from process-wide configuration while retaining the legacy
entry point's patchable collaborators during migration.
"""

import glob
import os
import plistlib
import posixpath
import re
import shlex
import subprocess
import sys
import time
import types
import uuid

SYNC_DIRECTIONS = ("two-way", "push", "pull")
SYNC_DEFAULT_DIRECTION = "two-way"
SYNC_DEFAULT_INTERVAL = 15
SYNC_MIN_INTERVAL = 5
SYNC_MAX_INTERVAL = 600


class _RuntimeGlobals(dict):
    def __init__(self, runtime, base):
        super().__init__(base)
        self._runtime = runtime

    def __missing__(self, key):
        try:
            return getattr(self._runtime, key)
        except AttributeError as exc:
            raise KeyError(key) from exc


_FUNCTIONS = ["ensure_syncs_migrated","get_sync","get_syncs_for_server","get_sync_plist_label","get_sync_plist_path","get_sync_executable_args","_scan_sync_agents_raw","create_sync_agent","restore_auto_sync_agents","restore_packaged_forward_agents","remove_sync_agent","remove_sync_agents_for_server","_ssh_remote_cmd","_ensure_remote_dir","check_rsync_prereqs","run_sync","add_sync","remove_sync_entry","toggle_sync_always","update_sync","get_syncs_status","record_folder_history","get_folder_history","browse_local","browse_remote","_validate_new_folder_name","mkdir_local","mkdir_remote"]
_PATCHABLE_COLLABORATORS = {"_ssh_remote_cmd", "_ensure_remote_dir", "check_rsync_prereqs", "build_rsync_commands", "explain_rsync_output"}


def ensure_syncs_migrated(cfg):
    """Ensures cfg has syncs + folder_history lists; normalizes entries."""
    changed = False
    if not isinstance(cfg.get("syncs"), list):
        cfg["syncs"] = []
        changed = True
    if not isinstance(cfg.get("folder_history"), list):
        cfg["folder_history"] = []
        changed = True
    seen_ids = set()
    server_ids = set()
    try:
        server_ids = {s.get("id") for s in cfg.get("servers", []) if isinstance(s, dict) and s.get("id")}
    except Exception:
        pass
    for s in cfg["syncs"]:
        if not isinstance(s, dict):
            continue
        if not s.get("id"):
            s["id"] = uuid.uuid4().hex[:8]
            changed = True
        if s["id"] in seen_ids:
            s["id"] = uuid.uuid4().hex[:8]
            changed = True
        seen_ids.add(s["id"])
        if s.get("direction") not in SYNC_DIRECTIONS:
            s["direction"] = SYNC_DEFAULT_DIRECTION
            changed = True
        # Mirror is only meaningful for one-way; force off for two-way.
        if s.get("direction") == "two-way" and s.get("mirror"):
            s["mirror"] = False
            changed = True
        else:
            s["mirror"] = bool(s.get("mirror", False))
        s["always"] = bool(s.get("always", False))
        try:
            iv = int(s.get("interval", SYNC_DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            iv = SYNC_DEFAULT_INTERVAL
        iv = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, iv))
        if s.get("interval") != iv:
            s["interval"] = iv
            changed = True
        s.setdefault("local_path", "")
        s.setdefault("remote_path", "")
        s.setdefault("server_id", "")
        s.setdefault("created_at", time.time())
        s.setdefault("last_sync", None)
        s.setdefault("last_status", "never")
        s.setdefault("last_message", "")
        # Drop syncs pointing at removed servers? Keep them but they resolve
        # to default on read; cleanup happens on remove_server_entry.
        _ = server_ids
    # Normalize folder_history entries
    kept = []
    for h in cfg["folder_history"]:
        if isinstance(h, dict) and (h.get("local_path") or h.get("remote_path")):
            h.setdefault("server_id", "")
            h.setdefault("last_used", 0)
            kept.append(h)
    if len(kept) != len(cfg["folder_history"]):
        cfg["folder_history"] = kept
        changed = True
    return cfg, changed

def get_sync(cfg, sync_id):
    for s in cfg.get("syncs", []):
        if isinstance(s, dict) and s.get("id") == sync_id:
            return s
    return None

def get_syncs_for_server(cfg, server_id):
    return [s for s in cfg.get("syncs", []) if isinstance(s, dict) and s.get("server_id") == server_id]

def get_sync_plist_label(sync_id):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(sync_id)) or "sync"
    return f"{SYNC_AGENT_PREFIX}-{safe}"

def get_sync_plist_path(sync_id):
    return os.path.join(LAUNCH_AGENTS_DIR, f"{get_sync_plist_label(sync_id)}.plist")

def get_sync_executable_args(sync_id):
    """ProgramArguments for a sync LaunchAgent: runs `devboost.py sync-run <id>`.

    Prefers the running copy (latest code), but NEVER points launchd at an
    executable under a TCC-protected location (e.g. a repo checkout inside
    ~/Documents) — launchd cannot exec there and the agent would die with
    exit 126. Falls back to the installed copy, which exists exactly for that.
    """
    sid = str(sync_id)
    candidates = [get_dashboard_executable()]
    inst = _installed_code_dir()
    if os.path.abspath(inst) != os.path.abspath(CODE_DIR):
        candidates.append(os.path.join(inst, "bin", DASHBOARD_WRAPPER_NAME))
    for wrapper in candidates:
        if _is_launchable(wrapper):
            return [wrapper, "sync-run", sid]
    # Last resort: direct python on the installed script (or running copy).
    for script in (os.path.join(inst, "devboost.py"),
                   os.path.join(CODE_DIR, "devboost.py")):
        if os.path.isfile(script) and not is_tcc_protected_path(script):
            return [sys.executable or "/usr/bin/python3", script, "sync-run", sid]
    return [get_dashboard_executable(), "sync-run", sid]

def _scan_sync_agents_raw():
    """Scans LaunchAgents for sync plists -> {sync_id: agent}."""
    out = {}
    pat = os.path.join(LAUNCH_AGENTS_DIR, f"{SYNC_AGENT_PREFIX}-*.plist")
    for plist_path in glob.glob(pat):
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        label = data.get("Label", "")
        prefix = SYNC_AGENT_PREFIX + "-"
        sid = label[len(prefix):] if label.startswith(prefix) else None
        if not sid:
            m = re.search(rf"{re.escape(SYNC_AGENT_PREFIX)}-(.+)\.plist$", plist_path)
            sid = m.group(1) if m else None
        if sid:
            out.setdefault(sid, {"label": label, "path": plist_path, "data": data})
    return out

def create_sync_agent(sync):
    """Installs/refreshes the persistent LaunchAgent for an Auto sync."""
    sid = sync.get("id")
    interval = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, int(sync.get("interval", SYNC_DEFAULT_INTERVAL))))
    plist_path = get_sync_plist_path(sid)
    data = {
        "Label": get_sync_plist_label(sid),
        "ProgramArguments": get_sync_executable_args(sid),
        "RunAtLoad": True,
        "StartInterval": interval,
        "ThrottleInterval": 10,
        "StandardOutPath": os.path.join(LOG_DIR, f"devboost-sync-{sid}.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, f"devboost-sync-{sid}.err"),
    }
    # WatchPaths gives near-real-time triggers for local changes; polling
    # (StartInterval) covers remote changes. Only set when the local dir exists.
    try:
        local = sync.get("local_path", "")
        if local and os.path.isdir(os.path.expanduser(local)):
            data["WatchPaths"] = [os.path.expanduser(local)]
    except Exception:
        pass
    # Reload if already loaded (unload errors are fine — first install).
    try:
        subprocess.run(["launchctl", "unload", "-w", plist_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    try:
        subprocess.run(["launchctl", "load", "-w", plist_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return plist_path

def restore_auto_sync_agents():
    """Refresh persistent sync agents after the packaged app is updated."""
    cfg = load_config()
    for sync in cfg.get("syncs", []):
        if isinstance(sync, dict) and sync.get("always") and sync.get("id"):
            try:
                create_sync_agent(sync)
            except Exception:
                pass

def restore_packaged_forward_agents():
    """Move legacy DevBoost tunnel agents to the packaged app launcher."""
    packaged = get_packaged_app_executable()
    if not packaged:
        return 0
    patterns = [
        os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "com.*.ssh-forward-*.plist"),
    ]
    migrated = 0
    seen = set()
    for pattern in patterns:
        for plist_path in glob.glob(pattern):
            if plist_path in seen:
                continue
            seen.add(plist_path)
            try:
                with open(plist_path, "rb") as f:
                    data = plistlib.load(f)
                args = data.get("ProgramArguments", [])
                if not args:
                    continue
                already_packaged = os.path.abspath(args[0]) == os.path.abspath(packaged)
                # Only rewrite DevBoost's own wrapper; never touch another app's SSH agent.
                if not already_packaged and os.path.basename(args[0]) != TUNNEL_WRAPPER_NAME:
                    continue
                tunnel_args = args[1:] if not already_packaged else args[1:]
                if tunnel_args and tunnel_args[0] == "--tunnel":
                    continue
                data["ProgramArguments"] = [packaged, "--tunnel"] + tunnel_args
                for key in ("StandardOutPath", "StandardErrorPath"):
                    previous = data.get(key, "")
                    if os.path.basename(previous).startswith("devboost-forward-"):
                        data[key] = os.path.join(LOG_DIR, os.path.basename(previous))
                subprocess.run(["launchctl", "unload", "-w", plist_path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with open(plist_path, "wb") as f:
                    plistlib.dump(data, f)
                subprocess.run(["launchctl", "load", "-w", plist_path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                migrated += 1
            except Exception:
                continue
    return migrated

def remove_sync_agent(sync_id):
    plist_path = get_sync_plist_path(sync_id)
    # Also catch legacy/renamed files for the same id (glob by suffix).
    candidates = {plist_path}
    try:
        for p in glob.glob(os.path.join(LAUNCH_AGENTS_DIR, f"*{sync_id}*.plist")):
            if os.path.basename(p).startswith(SYNC_AGENT_PREFIX) or sync_id in p:
                candidates.add(p)
    except Exception:
        pass
    for p in candidates:
        try:
            subprocess.run(["launchctl", "unload", "-w", p],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

def remove_sync_agents_for_server(server_id):
    cfg = load_config()
    for s in get_syncs_for_server(cfg, server_id):
        try:
            remove_sync_agent(s.get("id"))
        except Exception:
            pass

def _ssh_remote_cmd(host, remote_cmd):
    """Runs a remote shell command via ssh. Returns CompletedProcess."""
    return run_ssh_command(host, remote_cmd, timeout=30, connect_timeout=5)

def _ensure_remote_dir(host, remote_path):
    quoted = shlex.quote(remote_path)
    res = _ssh_remote_cmd(host, f"mkdir -p -- {quoted} && echo OK")
    return res.returncode == 0

def check_rsync_prereqs(ssh_host):
    """Verifies rsync exists locally and on the remote host.

    Returns None when fine, else an actionable error message. Code-12
    protocol errors almost always mean one of these two is missing.
    """
    if not shutil.which("rsync"):
        return ("rsync not found on this Mac "
                "(install with `brew install rsync`)")
    try:
        res = _ssh_remote_cmd(ssh_host, "command -v rsync")
    except Exception as e:
        return f"Cannot reach {ssh_host} to check for rsync: {e}"
    if res.returncode != 0 or not (res.stdout or "").strip():
        return (f"rsync not found on server '{ssh_host}' "
                f"(install with `sudo apt install rsync`)")
    return None

def run_sync(sync_id, timeout=300):
    """Runs one sync pass now (used by Sync Now, Once creation, and LaunchAgent).

    Returns {"ok": bool, "message": str}. Updates last_sync/last_status and
    folder_history in config. Never raises (errors are captured in message).
    """
    try:
        cfg = load_config()
    except Exception as e:
        return {"ok": False, "message": f"Cannot load config: {e}"}
    sync = get_sync(cfg, sync_id)
    if not sync:
        return {"ok": False, "message": f"Sync '{sync_id}' not found"}
    server = get_server(cfg, sync.get("server_id"))
    if not server:
        # Server tab removed but sync row survived: resolve to default so the
        # error message names a concrete host instead of failing silently.
        server = get_default_server(cfg)
    ssh_host = server.get("ssh_host")
    local = sync.get("local_path", "")
    remote = sync.get("remote_path", "")

    def _fail(msg):
        sync["last_status"] = "error"
        sync["last_message"] = msg
        try:
            save_config(cfg)
        except Exception:
            pass
        return {"ok": False, "message": msg}

    # Local side: create on pull/two-way so first sync just works.
    try:
        os.makedirs(os.path.expanduser(local), exist_ok=True)
    except Exception as e:
        return _fail(f"Cannot create local folder {local}: {e}")
    if not _ensure_remote_dir(ssh_host, remote):
        return _fail(f"Cannot reach {ssh_host} or create {remote} (check SSH keys)")
    # Preflight: code-12 protocol errors almost always mean rsync is missing
    # on one side — fail fast with the fix instead of cryptic stderr.
    prereq_msg = check_rsync_prereqs(ssh_host)
    if prereq_msg:
        return _fail(prereq_msg)
    cmds = build_rsync_commands(sync, ssh_host)
    errors = []
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            errors.append("rsync not found (install rsync on this Mac)")
            break
        except subprocess.TimeoutExpired:
            errors.append(f"rsync timed out after {timeout}s")
            break
        except Exception as e:
            errors.append(str(e))
            break
        if res.returncode != 0:
            lines = [l for l in (res.stderr or res.stdout or f"exit {res.returncode}").strip().splitlines() if l.strip()]
            # The cause is usually the first line(s); the last line is just the
            # "code 12" summary — keep up to 3 lines so the cause survives.
            shown = " / ".join(lines[:2] + ([lines[-1]] if len(lines) > 2 and lines[-1] not in lines[:2] else []))
            if not shown:
                shown = f"rsync exit {res.returncode}"
            errors.append(explain_rsync_output(shown, ssh_host))
            break
    now = time.time()
    sync["last_sync"] = now
    if errors:
        sync["last_status"] = "error"
        sync["last_message"] = "; ".join(errors)[:500]
    else:
        sync["last_status"] = "ok"
        n = len(cmds)
        sync["last_message"] = f"Synced {local} <-> {ssh_host}:{remote} ({sync.get('direction')})"
    try:
        save_config(cfg)
    except Exception:
        pass
    try:
        record_folder_history(sync.get("server_id"), local, remote)
    except Exception:
        pass
    if errors:
        return {"ok": False, "message": "; ".join(errors)[:500]}
    return {"ok": True, "message": sync["last_message"]}

def add_sync(server_ref=None, local_path="", remote_path="", direction="two-way",
             mirror=False, always=False, interval=SYNC_DEFAULT_INTERVAL, run_now=True):
    """Creates a sync entry, installs agent if Auto, optionally runs once now."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    local, remote = validate_sync_paths(local_path, remote_path)
    direction = (direction or SYNC_DEFAULT_DIRECTION).strip().lower()
    if direction not in SYNC_DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(SYNC_DIRECTIONS)}")
    if direction == "two-way":
        mirror = False  # safe merge only (see semantics above)
    try:
        interval = int(interval or SYNC_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = SYNC_DEFAULT_INTERVAL
    interval = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, interval))
    # De-dupe: same server + same pair reuses the row (updates params instead).
    for s in get_syncs_for_server(cfg, sid):
        if s.get("local_path") == local and s.get("remote_path") == remote:
            s["direction"] = direction
            s["mirror"] = bool(mirror)
            s["always"] = bool(always)
            s["interval"] = interval
            save_config(cfg)
            sync = s
            break
    else:
        sync = {
            "id": uuid.uuid4().hex[:8],
            "server_id": sid,
            "local_path": local,
            "remote_path": remote,
            "direction": direction,
            "mirror": bool(mirror),
            "always": bool(always),
            "interval": interval,
            "created_at": time.time(),
            "last_sync": None,
            "last_status": "never",
            "last_message": "",
        }
        cfg.setdefault("syncs", []).append(sync)
        save_config(cfg)
    try:
        record_folder_history(sid, local, remote)
    except Exception:
        pass
    # Ensure the local dir exists now so the Auto agent can WatchPaths it.
    try:
        os.makedirs(os.path.expanduser(local), exist_ok=True)
    except Exception:
        pass
    if sync.get("always"):
        try:
            create_sync_agent(sync)
        except Exception:
            pass
    else:
        try:
            remove_sync_agent(sync.get("id"))
        except Exception:
            pass
    result = {"ok": True, "sync": dict(sync), "message": "Folder sync added"}
    if run_now:
        res = run_sync(sync.get("id"))
        # Re-read for fresh last_sync/status after the run.
        try:
            cfg2 = load_config()
            sync2 = get_sync(cfg2, sync.get("id"))
            if sync2:
                result["sync"] = dict(sync2)
        except Exception:
            pass
        result["run"] = res
        result["message"] = res.get("message", result["message"])
        result["ok"] = bool(res.get("ok"))
    return result

def remove_sync_entry(sync_id):
    cfg = load_config()
    sync = get_sync(cfg, sync_id)
    if not sync:
        return False
    cfg["syncs"] = [s for s in cfg.get("syncs", []) if s.get("id") != sync_id]
    save_config(cfg)
    try:
        remove_sync_agent(sync_id)
    except Exception:
        pass
    return True

def toggle_sync_always(sync_id, make_always, interval=None):
    cfg = load_config()
    sync = get_sync(cfg, sync_id)
    if not sync:
        return None
    sync["always"] = bool(make_always)
    if interval is not None:
        try:
            sync["interval"] = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, int(interval)))
        except (TypeError, ValueError):
            pass
    save_config(cfg)
    try:
        if sync["always"]:
            create_sync_agent(sync)
        else:
            remove_sync_agent(sync_id)
    except Exception:
        pass
    return sync

def update_sync(sync_id, local_path=None, remote_path=None, direction=None,
                mirror=None, always=None, interval=None):
    """Edits a sync entry's paths/params (server tab never changes).

    Only arguments that are not None are updated. Returns the updated sync
    dict, None if not found. Raises ValueError on invalid input.
    """
    cfg = load_config()
    sync = get_sync(cfg, sync_id)
    if not sync:
        return None
    if local_path is not None or remote_path is not None:
        local, remote = validate_sync_paths(
            local_path if local_path is not None else sync.get("local_path", ""),
            remote_path if remote_path is not None else sync.get("remote_path", ""))
        sync["local_path"] = local
        sync["remote_path"] = remote
    if direction is not None:
        direction = (direction or "").strip().lower()
        if direction not in SYNC_DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(SYNC_DIRECTIONS)}")
        sync["direction"] = direction
    if mirror is not None:
        sync["mirror"] = bool(mirror)
    if sync.get("direction") == "two-way":
        sync["mirror"] = False  # safe merge only (see semantics above)
    if always is not None:
        sync["always"] = bool(always)
    if interval is not None:
        try:
            sync["interval"] = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, int(interval)))
        except (TypeError, ValueError):
            pass
    save_config(cfg)
    try:
        record_folder_history(sync.get("server_id"), sync.get("local_path"), sync.get("remote_path"))
    except Exception:
        pass
    # Ensure the local dir exists so the Auto agent can WatchPaths it.
    try:
        os.makedirs(os.path.expanduser(sync.get("local_path", "")), exist_ok=True)
    except Exception:
        pass
    try:
        if sync.get("always"):
            create_sync_agent(sync)
        else:
            remove_sync_agent(sync_id)
    except Exception:
        pass
    return sync

def get_syncs_status(server_ref=None):
    """Sync rows for one server tab, with agent presence (like forwards)."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    agents = _scan_sync_agents_raw()
    rows = []
    for s in get_syncs_for_server(cfg, sid):
        sid_row = s.get("id")
        agent = agents.get(sid_row)
        rows.append({
            "id": sid_row,
            "server_id": sid,
            "local_path": s.get("local_path", ""),
            "remote_path": s.get("remote_path", ""),
            "direction": s.get("direction", SYNC_DEFAULT_DIRECTION),
            "mirror": bool(s.get("mirror", False)),
            "always": bool(s.get("always", False)),
            "agent_installed": agent is not None,
            "active": bool(s.get("always", False)) and agent is not None,
            "local_protected": is_tcc_protected_path(s.get("local_path", "")),
            "interval": s.get("interval", SYNC_DEFAULT_INTERVAL),
            "created_at": s.get("created_at"),
            "last_sync": s.get("last_sync"),
            "last_status": s.get("last_status", "never"),
            "last_message": s.get("last_message", ""),
        })
    rows.sort(key=lambda r: (r.get("local_path") or "", r.get("remote_path") or ""))
    return {
        "server_id": sid,
        "server_name": server.get("name") or server.get("ssh_host"),
        "server_host": server.get("ssh_host"),
        "packaged_app": bool(get_packaged_app_executable()),
        "syncs": rows,
        "folder_history": get_folder_history(limit=10, server_id=sid),
    }

def record_folder_history(server_id, local_path, remote_path):
    """Remembers used folder pairs for quick-select (max 20 per server)."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        local = (local_path or "").strip()
        remote = (remote_path or "").strip()
        if not local and not remote:
            return
        history = cfg.setdefault("folder_history", [])
        kept = []
        for h in history:
            if not isinstance(h, dict):
                continue
            if (h.get("server_id") or sid) == sid and (h.get("local_path") or "") == local and (h.get("remote_path") or "") == remote:
                continue
            kept.append(h)
        kept.insert(0, {"server_id": sid, "local_path": local, "remote_path": remote, "last_used": time.time()})
        per_counts = {}
        capped = []
        for h in kept:
            key = h.get("server_id") or sid
            per_counts[key] = per_counts.get(key, 0) + 1
            if per_counts[key] <= 20:
                capped.append(h)
        cfg["folder_history"] = capped[:100]
        save_config(cfg)
    except Exception:
        pass

def get_folder_history(limit=10, server_id=None):
    """Most-recently-used folder pairs for a server (for quick-select chips)."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        out = [h for h in cfg.get("folder_history", []) if (h.get("server_id") or sid) == sid]
        return out[:limit]
    except Exception:
        return []

def browse_local(path=""):
    """Lists local directories for the folder picker. Returns dict with entries."""
    raw = (path or "").strip() or os.path.expanduser("~")
    raw = os.path.expanduser(raw)
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.expanduser("~"), raw)
    target = os.path.normpath(raw)
    home = os.path.expanduser("~")
    if not os.path.exists(target):
        return {"ok": False, "path": target, "parent": os.path.dirname(target),
                "home": home, "entries": [], "message": f"Path does not exist: {target}"}
    if not os.path.isdir(target):
        target = os.path.dirname(target)
    try:
        names = sorted(os.listdir(target), key=lambda n: n.lower())
    except OSError as e:
        # Errno 1 (EPERM) / 13 (EACCES) on ~/Documents, ~/Desktop, ... is
        # macOS TCC privacy protection, not a missing folder: the dashboard
        # process needs Full Disk Access (see README + dashboard help).
        if getattr(e, "errno", None) in (1, 13):
            return {"ok": False, "denied": True, "path": target,
                    "parent": os.path.dirname(target),
                    "home": home, "entries": [],
                    "message": f"macOS blocked access to {target} (privacy protection)"}
        return {"ok": False, "path": target, "parent": os.path.dirname(target),
                "home": home, "entries": [], "message": f"Cannot list {target}: {e}"}
    entries = []
    for name in names:
        full = os.path.join(target, name)
        try:
            if os.path.isdir(full):
                entries.append({"name": name, "path": full,
                                "hidden": name.startswith(".")})
        except OSError:
            continue
    parent = os.path.dirname(target.rstrip("/")) or "/"
    # Root's parent is itself; avoid empty.
    if not parent:
        parent = "/"
    return {"ok": True, "path": target, "parent": parent, "home": home, "entries": entries}

def browse_remote(server_ref=None, path=""):
    """Lists remote directories over SSH for the folder picker."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    ssh_host = server.get("ssh_host")
    want = (path or "").strip() or "~"
    # Resolve ~ to absolute once so parent navigation works with posixpath.
    if want in ("~", "~/", "$HOME"):
        probe = _ssh_remote_cmd(ssh_host, "pwd && echo OK")
        if probe.returncode == 0:
            lines = [l for l in (probe.stdout or "").splitlines() if l.strip()]
            if lines:
                want = lines[0].strip()
            else:
                want = "~"
        else:
            return {"ok": False, "path": want, "parent": "~", "entries": [],
                    "message": f"Cannot reach {ssh_host} (check SSH keys)"}
    if want.startswith("~"):
        # ~/sub -> ask remote shell to expand (keep display absolute when possible)
        probe = _ssh_remote_cmd(ssh_host, f"cd -- {shlex.quote(want)} 2>/dev/null && pwd")
        if probe.returncode == 0 and (probe.stdout or "").strip():
            want = (probe.stdout or "").strip().splitlines()[0].strip()
    # List with ls -1pa -a so hidden/dot folders (.output, .git, ...) are
    # visible too (dirs end with /). Quote for the remote shell.
    res = _ssh_remote_cmd(ssh_host, f"ls -1pa -- {shlex.quote(want)} 2>&1")
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        # ls writes the error to stdout/stderr; surface the last line.
        lines = [l for l in out.splitlines() if l.strip()]
        msg = lines[-1] if lines else f"Cannot list {want}"
        return {"ok": False, "path": want, "parent": posixpath.dirname(want.rstrip("/")) or "/",
                "entries": [], "message": msg[:300]}
    entries = []
    for line in (res.stdout or "").splitlines():
        name = line.rstrip("\n")
        if not name or name in ("./", "../"):
            continue
        is_dir = name.endswith("/")
        name = name[:-1] if is_dir else name
        if name in (".", "..") or not name:
            continue
        if is_dir:
            full = posixpath.join(want.rstrip("/"), name)
            entries.append({"name": name, "path": full,
                            "hidden": name.startswith(".")})
    entries.sort(key=lambda e: e["name"].lower())
    parent = posixpath.dirname(want.rstrip("/")) or "/"
    return {"ok": True, "path": want, "parent": parent, "entries": entries}

def _validate_new_folder_name(name):
    """Validates a new folder name. Returns stripped name or raises ValueError."""
    clean = (name or "").strip().strip("'\"")
    if not clean or clean in (".", ".."):
        raise ValueError("Enter a folder name")
    if len(clean) > 255:
        raise ValueError("Folder name is too long")
    if "/" in clean or "\\" in clean or "\x00" in clean:
        raise ValueError(f"Invalid folder name: {clean!r} (no slashes)")
    return clean

def mkdir_local(parent="", name=""):
    """Creates a local folder for the picker. Returns {ok, path, ...}."""
    clean = _validate_new_folder_name(name)
    base = (parent or "").strip() or "~"
    base = os.path.expanduser(base)
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), base)
    new = os.path.normpath(os.path.join(base, clean))
    try:
        if os.path.isdir(new):
            return {"ok": True, "path": new, "message": "Folder already exists"}
        os.makedirs(new, exist_ok=True)
    except OSError as e:
        if getattr(e, "errno", None) in (1, 13):
            return {"ok": False, "denied": True, "path": new,
                    "message": f"macOS blocked creating {new} (privacy protection)"}
        return {"ok": False, "path": new, "message": f"Cannot create {new}: {e}"}
    return {"ok": True, "path": new, "message": f"Created {new}"}

def mkdir_remote(server_ref=None, parent="", name=""):
    """Creates a remote folder over SSH for the picker. Returns {ok, path, ...}."""
    clean = _validate_new_folder_name(name)
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    ssh_host = server.get("ssh_host")
    base = (parent or "").strip() or "~"
    if base.startswith("~") or base in ("$HOME", "") or not base.startswith("/"):
        home_probe = _ssh_remote_cmd(ssh_host, "pwd")
        if home_probe.returncode != 0:
            return {"ok": False, "path": base,
                    "message": f"Cannot reach {ssh_host} (check SSH keys)"}
        home = (home_probe.stdout or "").strip().splitlines()[0].strip()
        if base in ("~", "~/", "$HOME", ""):
            base = home
        elif base.startswith("~/"):
            base = posixpath.join(home, base[2:])
        else:
            base = posixpath.join(home, base)
    new = posixpath.normpath(posixpath.join(base, clean))
    res = _ssh_remote_cmd(ssh_host, f"mkdir -p -- {shlex.quote(new)} 2>&1")
    if res.returncode != 0:
        lines = [l for l in ((res.stdout or "") + (res.stderr or "")).splitlines() if l.strip()]
        return {"ok": False, "path": new,
                "message": (lines[-1] if lines else f"Cannot create {new}")[:300]}
    return {"ok": True, "path": new, "message": f"Created {ssh_host}:{new}"}


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
    # Preserve test and integration replacements on the compatibility runtime.
    # Normal wrappers are intentionally ignored so they cannot recurse.
    for name in _PATCHABLE_COLLABORATORS:
        candidate = getattr(runtime, name, None)
        candidate_module = getattr(candidate, "__module__", None)
        runtime_module = getattr(runtime, "__name__", "devboost")
        if candidate is not None and candidate_module not in ("devboost", runtime_module):
            namespace[name] = candidate
    return namespace


def invoke(name, runtime, *args, **kwargs):
    namespace = _bound_functions(runtime)
    return namespace[name](*args, **kwargs)
