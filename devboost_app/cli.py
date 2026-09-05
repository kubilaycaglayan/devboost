"""Command-line interface for DevBoost."""

import subprocess
import sys
import time
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


_FUNCTIONS = ["_extract_server_flag","cli_list","cli_servers","cli_scan","cli_docker","cli_lazydocker","print_history_hint","_format_sync_age","cli_sync_list","cli_usage","print_help","main"]


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
            owner = (f.get("conflict_with") or {}).get("name") or (f.get("conflict_with") or {}).get("ssh_host")
            flag = f" ⚠️ in use by {owner}" if f.get("conflict") and owner else (" ⚠️" if f.get("conflict") else "")
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

def cli_docker(server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    print(f"\nChecking Docker on {server.get('ssh_host')}...")
    # CLI users expect a result now; the dashboard uses the cached worker path.
    snapshot = collect_docker_snapshot(server.get("ssh_host"))
    if snapshot.get("available") is not True:
        print(snapshot.get("message") or "Docker is unavailable on the server.")
        return
    containers = snapshot.get("containers", [])
    if not containers:
        print("Docker is available, but no containers are running.")
        return
    print("=" * 120)
    print(f"{'NAME':<24}{'STATUS':<28}{'CPU':<10}{'MEMORY':<24}{'NET I/O':<24}{'IMAGE'}")
    print("-" * 120)
    for container in containers:
        stats = container.get("stats") or {}
        print(
            f"{container.get('name', '')[:23]:<24}"
            f"{container.get('status', '')[:27]:<28}"
            f"{stats.get('cpu_percent', ''):<10}"
            f"{stats.get('memory_usage', '')[:23]:<24}"
            f"{stats.get('network_io', '')[:23]:<24}"
            f"{container.get('image', '')}"
        )
    print("=" * 120)

def cli_lazydocker(server_ref=None):
    """Open the interactive lazydocker TUI on the selected SSH server."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    host = server.get("ssh_host")
    print(f"Opening lazydocker on {host}...")
    result = subprocess.run([
        "ssh", "-t", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        host, "lazydocker",
    ])
    if result.returncode != 0:
        print("Could not start lazydocker. Check that it is installed on the server and that SSH works.")

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

def _format_sync_age(ts):
    if not ts:
        return "never"
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "never"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

def cli_sync_list(server_ref=None, show_all=False):
    cfg = load_config()
    servers = get_servers(cfg)
    targets = servers if show_all else [resolve_server(cfg, server_ref)]
    for srv in targets:
        status = get_syncs_status(srv.get("id"))
        print(f"\nFolder syncs • {status['server_name']} ({status['server_host']})")
        print("=" * 100)
        if not status["syncs"]:
            print("No folder syncs configured.")
            print("Use 'devboost sync add <local> <remote> [--auto]' to add one.")
            continue
        print(f"{'ID':<10}{'DIRECTION':<10}{'MODE':<10}{'LAST SYNC':<14}{'LOCAL':<28}{'REMOTE'}")
        print("-" * 100)
        for s in status["syncs"]:
            mode = "AUTO" if s["always"] else "ONCE"
            if s["mirror"] and s["direction"] in ("push", "pull"):
                mode += "+MIRROR"
            last = _format_sync_age(s.get("last_sync"))
            st = (s.get("last_status") or "never").upper()
            print(f"{s['id']:<10}{s['direction']:<10}{mode:<10}{last:<14}{(s['local_path'] or '')[:27]:<28}{s['remote_path']}  [{st}]")
        print("=" * 100)
    print(f"Dashboard: http://localhost:{DEFAULT_DASHBOARD_PORT}\n")

def cli_usage(refresh=False, server_ref=None):
    status = get_usage_status(refresh=refresh, server_ref=server_ref)
    if not status["accounts"]:
        print("No AI usage accounts configured. Add them from the dashboard API.")
        return
    for account in status["accounts"]:
        snapshot = status["snapshots"].get(account["id"], {})
        print(f"\n{account['name']} ({account['provider']})")
        if not snapshot.get("ok"):
            print(f"  unavailable: {snapshot.get('message', 'not checked')}")
            continue
        for quota in snapshot.get("quotas", []):
            print(f"  {quota['name']}: {quota.get('remaining')} {quota.get('unit', '')} remaining")
        for balance in snapshot.get("balances", []):
            print(f"  balance: {balance.get('remaining')} {balance.get('currency', 'USD')}")

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
  devboost docker [--server ID]  Show cached Docker container stats on a remote server
  devboost usage [--refresh] [--server id|host]
                                Show AI provider quotas from the selected server
  devboost lazydocker [--server ID]  Open the interactive lazydocker TUI over SSH
  devboost server list           List SSH connection tabs
  devboost server add <ssh-host> [display-name]   Add a tab (host should exist in ~/.ssh/config)
  devboost server rm <id>        Remove a tab (stops its tunnels)
  devboost server pin <id> [--off]    Pin/unpin a tab (pinned tabs sort first)
  devboost sync [--server ID] [--all]   List folder syncs (tracked mirrors)
  devboost sync add <local> <remote> [--server ID] [--direction two-way|push|pull] [--mirror] [--auto] [--interval N] [--no-run]
                                      Add a folder sync (runs once now unless --no-run)
  devboost sync run <id>         Run a folder sync now
  devboost sync edit <id> [--local PATH] [--remote PATH] [--direction two-way|push|pull] [--mirror|--no-mirror] [--auto|--once] [--interval N] [--no-run]
                                      Edit a folder sync (runs once now unless --no-run)
  devboost sync rm <id>          Remove a folder sync (stops its agent)
  devboost sync auto <id> [--off] [--interval N]   Make a sync Auto (persistent) or Once (one-time)
  devboost ui / dashboard        Open the web dashboard in Chrome/browser
  devboost serve [--port 3080]   Run the web dashboard server

Folder sync modes (see dashboard ? help):
  direction two-way (default) = bidirectional merge, newer wins, deletions never propagate.
  direction push/pull + --mirror = exact copy (rsync --delete, deletions propagate).
  --auto = persistent background agent (WatchPaths + polling every --interval sec).
  Without --auto the sync is one-time (runs now + on-demand via Sync Now).

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
    elif cmd == "docker":
        server_ref, _ = _extract_server_flag(args[1:])
        cli_docker(server_ref=server_ref)
    elif cmd == "lazydocker":
        server_ref, _ = _extract_server_flag(args[1:])
        cli_lazydocker(server_ref=server_ref)
    elif cmd == "usage":
        server_ref, usage_args = _extract_server_flag(args[1:])
        cli_usage(refresh="--refresh" in usage_args, server_ref=server_ref)
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
    elif cmd == "sync-run":
        # Hidden entry point for sync LaunchAgents: `devboost.py sync-run <id>`
        if len(args) < 2 or not args[1].strip():
            print("Error: sync-run requires a sync id.")
            sys.exit(1)
        res = run_sync(args[1].strip())
        print(res.get("message", ""))
        sys.exit(0 if res.get("ok") else 1)
    elif cmd == "refresh-agents":
        # Hidden entry point used by the packaged runner after each rebuild.
        forwards = restore_packaged_forward_agents()
        restore_auto_sync_agents()
        print(f"Refreshed {forwards} persistent forward agent(s).")
    elif cmd == "sync":
        server_ref, filtered = _extract_server_flag(args[1:])
        rest = filtered[1:] if filtered and filtered[0] == "sync" else filtered
        # `devboost sync` bare == list
        sub = rest[0] if rest else "list"
        if sub in ("list", "ls", "status"):
            show_all = "--all" in rest
            cli_sync_list(server_ref=server_ref, show_all=show_all)
        elif sub == "add":
            positional = [a for a in rest[1:] if not a.startswith("--")]
            flags = {a for a in rest[1:] if a.startswith("--")}
            direction = SYNC_DEFAULT_DIRECTION
            interval = SYNC_DEFAULT_INTERVAL
            for a in rest[1:]:
                if a.startswith("--direction="):
                    direction = a.split("=", 1)[1]
                elif a.startswith("--interval="):
                    try:
                        interval = int(a.split("=", 1)[1])
                    except ValueError:
                        pass
            if len(positional) >= 2 and positional[0].startswith("--direction"):
                pass
            # Support `--direction X` (space-separated) form
            if "--direction" in rest[1:]:
                try:
                    direction = rest[rest.index("--direction") + 1]
                except IndexError:
                    pass
            if "--interval" in rest[1:]:
                try:
                    interval = int(rest[rest.index("--interval") + 1])
                except (IndexError, ValueError):
                    pass
            if len(positional) < 2:
                print("Error: Specify local and remote folders. e.g. 'devboost sync add ~/projects/app ~/projects/app'")
                print("Previously used folders:")
                for h in get_folder_history(limit=10, server_id=server_ref):
                    print(f"  local={h.get('local_path')} remote={h.get('remote_path')}")
                sys.exit(1)
            mirror = "--mirror" in flags
            always = "--auto" in flags or "--always" in flags
            run_now = "--no-run" not in flags
            try:
                result = add_sync(server_ref=server_ref, local_path=positional[0],
                                 remote_path=positional[1], direction=direction,
                                 mirror=mirror, always=always, interval=interval,
                                 run_now=run_now)
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            print(result.get("message", "Folder sync added"))
            if not result.get("ok"):
                sys.exit(1)
        elif sub in ("rm", "remove", "del", "delete"):
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync rm a1b2c3d4'")
                sys.exit(1)
            ok = remove_sync_entry(rest[1])
            print("Folder sync removed." if ok else "Sync not found.")
        elif sub == "run":
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync run a1b2c3d4'")
                sys.exit(1)
            res = run_sync(rest[1])
            print(res.get("message", ""))
            if not res.get("ok"):
                sys.exit(1)
        elif sub == "auto":
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync auto a1b2c3d4'")
                sys.exit(1)
            make_always = "--off" not in rest
            interval = None
            for a in rest:
                if a.startswith("--interval="):
                    try:
                        interval = int(a.split("=", 1)[1])
                    except ValueError:
                        pass
            if "--interval" in rest:
                try:
                    interval = int(rest[rest.index("--interval") + 1])
                except (IndexError, ValueError):
                    pass
            sync = toggle_sync_always(rest[1], make_always, interval=interval)
            if not sync:
                print("Sync not found.")
                sys.exit(1)
            print(f"Sync '{rest[1]}' is now {'AUTO (persistent)' if make_always else 'ONCE (one-time)'}.")
        elif sub == "edit":
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync edit a1b2c3d4 --remote ~/new-path'")
                sys.exit(1)
            args = rest[1:]
            sid = args[0]
            kwargs = {}
            i = 1
            while i < len(args):
                a = args[i]
                if a.startswith("--local="):
                    kwargs["local_path"] = a.split("=", 1)[1]
                elif a == "--local" and i + 1 < len(args):
                    kwargs["local_path"] = args[i + 1]
                    i += 1
                elif a.startswith("--remote="):
                    kwargs["remote_path"] = a.split("=", 1)[1]
                elif a == "--remote" and i + 1 < len(args):
                    kwargs["remote_path"] = args[i + 1]
                    i += 1
                elif a.startswith("--direction="):
                    kwargs["direction"] = a.split("=", 1)[1]
                elif a == "--direction" and i + 1 < len(args):
                    kwargs["direction"] = args[i + 1]
                    i += 1
                elif a == "--mirror":
                    kwargs["mirror"] = True
                elif a == "--no-mirror":
                    kwargs["mirror"] = False
                elif a.startswith("--interval="):
                    kwargs["interval"] = a.split("=", 1)[1]
                elif a == "--interval" and i + 1 < len(args):
                    kwargs["interval"] = args[i + 1]
                    i += 1
                elif a in ("--auto", "--always"):
                    kwargs["always"] = True
                elif a == "--once":
                    kwargs["always"] = False
                i += 1
            run_now = "--no-run" not in args
            if "interval" in kwargs:
                try:
                    kwargs["interval"] = int(kwargs["interval"])
                except (TypeError, ValueError):
                    print("Error: --interval must be a number of seconds.")
                    sys.exit(1)
            try:
                sync = update_sync(sid, **kwargs)
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            if not sync:
                print("Sync not found.")
                sys.exit(1)
            if run_now:
                res = run_sync(sid)
                print(res.get("message", "Folder sync updated."))
                if not res.get("ok"):
                    sys.exit(1)
            else:
                print(f"Sync '{sid}' updated.")
        else:
            print_help()
    else:
        print_help()


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
