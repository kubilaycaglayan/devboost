"""HTTP request handler for the DevBoost dashboard API."""

import json
import os
import re
import secrets
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler


class DashboardHandler(BaseHTTPRequestHandler):
    _MAX_JSON_BYTES = 1024 * 1024
    _SESSION_COOKIE = "devboost_session"

    def _api_authenticated(self):
        """Require the per-launch session cookie when the server has one."""
        expected = getattr(self.server, "auth_token", "")
        if not expected:
            return True
        values = {}
        for cookie in self.headers.get("Cookie", "").split(";"):
            name, separator, value = cookie.strip().partition("=")
            if separator:
                values[name] = value
        return secrets.compare_digest(values.get(self._SESSION_COOKIE, ""), expected)

    def _local_browser_request(self):
        """Allow dashboard-origin requests, while blocking cross-site browser calls.

        The server is intentionally localhost-only, but another local web page
        can still attempt to CSRF a localhost service.  CLI clients generally
        omit Origin, so absence remains allowed; browser requests must originate
        from this exact dashboard port.
        """
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urllib.parse.urlparse(origin)
                expected_port = self.server.server_port
                if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1"):
                    return False
                return parsed.port == expected_port
            except ValueError:
                return False
        return True

    def _reject_untrusted_request(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"message":"Request origin is not allowed"}')

    def _reject_unauthenticated_request(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"message":"Dashboard session required"}')

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _read_json(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            raise ValueError("invalid Content-Length")
        if content_length < 0 or content_length > self._MAX_JSON_BYTES:
            raise ValueError("request body is too large")
        if content_length > 0:
            raw = self.rfile.read(content_length)
            return json.loads(raw.decode("utf-8"))
        return {}

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def _server_param(self, query, body=None):
        # ?server=<id|ssh_host> on GET, or server_id/server field on POST
        srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
        if not srv and isinstance(body, dict):
            srv = body.get("server_id") or body.get("server")
        return srv

    def _server_is_connected(self, server_ref):
        cfg = load_config()
        server = resolve_server(cfg, server_ref)
        return bool(server and is_server_connected(cfg, server.get("id")))

    def _require_server_connected(self, server_ref):
        if self._server_is_connected(server_ref):
            return True
        self._send_json({"ok": False, "message": "Server is disconnected; connect it before using this action"}, status=409)
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._api_authenticated():
                self._reject_unauthenticated_request()
                return
            if not self._local_browser_request():
                self._reject_untrusted_request()
                return
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # The dashboard is assembled from separate assets. Do not let a
            # previously packaged inline dashboard keep running after an
            # upgrade (that was the source of misleading syntax errors).
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            auth_token = getattr(self.server, "auth_token", "")
            if auth_token:
                self.send_header(
                    "Set-Cookie",
                    f"{self._SESSION_COOKIE}={auth_token}; Path=/; HttpOnly; SameSite=Strict",
                )
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif path.startswith("/dashboard/"):
            try:
                asset_name = path.rsplit("/", 1)[-1]
                data, content_type = read_asset(asset_name)
            except (FileNotFoundError, UnicodeError):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
        elif path in ("/favicon.png", "/favicon.ico", "/favicon-32x32.png", "/assets/favicon.png"):
            try:
                data = get_favicon_bytes()
            except Exception:
                data = b""
            if not data:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/status":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            data = get_all_forwards_status(srv)
            data["servers"] = get_servers_status()
            data["runtime_active"] = self._server_is_connected(srv)
            self._send_json(data)
        elif path == "/api/usage":
            try:
                aid = query.get("account", [None])[0]
                refresh = query.get("refresh", ["0"])[0].lower() in ("1", "true", "yes")
                srv = self._server_param(query)
                kwargs = {"refresh": refresh, "account_id": aid}
                if srv:
                    kwargs["server_ref"] = srv
                    if not self._server_is_connected(srv):
                        kwargs["cached_only"] = True
                self._send_json(get_usage_status(**kwargs))
            except Exception as e:
                self._send_json({"accounts": [], "snapshots": {}, "message": str(e)}, status=500)
        elif path == "/api/servers":
            cfg = load_config()
            # Older configs did not persist a selected server. Preserve the
            # existing first-server behavior once, then keep an explicit empty
            # value for a deliberate disconnect.
            changed = False
            if "active_server_id" not in cfg:
                servers = get_servers(cfg)
                cfg["active_server_id"] = servers[0].get("id") if servers else ""
                changed = True
            if "last_connected_server_id" not in cfg:
                cfg["last_connected_server_id"] = cfg.get("active_server_id") or ""
                changed = True
            if changed:
                save_config(cfg)
            connected = set(cfg.get("connected_server_ids") or [])
            servers = get_servers_status()
            for server in servers:
                server["connected"] = server.get("id") in connected
            self._send_json({
                "servers": servers,
                "active_server_id": cfg.get("active_server_id") or "",
                "last_connected_server_id": cfg.get("last_connected_server_id") or "",
                "connected_server_ids": sorted(connected),
            })
        elif path == "/api/servers/active":
            cfg = load_config()
            connected_ids = set(get_connected_server_ids(cfg))
            servers = []
            for server in get_servers(cfg):
                # This endpoint feeds the menubar's remote-source picker.
                # Use DevBoost's explicit connection state rather than an SSH
                # reachability probe: a configured/reachable server is not a
                # currently connected server.
                if server.get("id") in connected_ids:
                    servers.append({"id": server.get("id"), "name": server.get("name") or server.get("ssh_host"), "ssh_host": server.get("ssh_host")})
            active = resolve_server(cfg, cfg.get("active_server_id")) if cfg.get("active_server_id") else None
            active_id = active.get("id") if active and active.get("id") in connected_ids else None
            self._send_json({"servers": servers, "active_server_id": active_id})
        elif path == "/api/settings/menubar":
            self._send_json({"menubar_enabled": bool(load_config().get("menubar_enabled", True))})
        elif path == "/api/settings/active-server":
            cfg = load_config()
            active = resolve_server(cfg, cfg.get("active_server_id")) if cfg.get("active_server_id") else None
            self._send_json({"active_server_id": active.get("id") if active else None})
        elif path == "/api/ssh-hosts":
            cfg = load_config()
            added_hosts = {s.get("ssh_host") for s in cfg.get("servers", [])}
            added_ids = {s.get("ssh_host"): s.get("id") for s in cfg.get("servers", [])}
            hosts = []
            for h in get_ssh_config_hosts():
                hosts.append({
                    **h,
                    "added": h["ssh_host"] in added_hosts,
                    "server_id": added_ids.get(h["ssh_host"]),
                })
            self._send_json({"hosts": hosts})
        elif path == "/api/scan":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            if not self._server_is_connected(srv):
                self._send_json({"services": [], "runtime_active": False, "message": "Server is disconnected"})
                return
            self._send_json({"services": scan_remote_services(srv)})
        elif path == "/api/docker":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            if not self._server_is_connected(srv):
                self._send_json({"server_id": srv or "", "runtime_active": False, "containers": [], "message": "Server is disconnected"})
                return
            self._send_json(get_docker_status(srv))
        elif path == "/api/docker/logs":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            container = query.get("container", [""])[0]
            if not container:
                self._send_json({"ok": False, "message": "container is required", "logs": ""}, status=400)
            elif not self._server_is_connected(srv):
                self._send_json({"ok": False, "message": "Server is disconnected", "logs": ""}, status=409)
            else:
                self._send_json(get_docker_logs(srv, container, query.get("tail", [200])[0]))
        elif path == "/api/docker/labels":
            self._send_json({"labels": get_docker_labels()})
        elif path == "/api/history":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"history": get_port_history(limit=10, server_id=srv)})
        elif path == "/api/local-ports":
            try:
                self._send_json({"ports": get_local_listening_ports()})
            except Exception as e:
                self._send_json({"ports": [], "message": str(e)})
        elif path == "/api/syncs":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            try:
                result = get_syncs_status(srv)
                result["runtime_active"] = self._server_is_connected(srv)
                self._send_json(result)
            except Exception as e:
                self._send_json({"syncs": [], "folder_history": [], "message": str(e)})
        elif path == "/api/folder-history":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"history": get_folder_history(limit=10, server_id=srv)})
        elif path == "/api/browse/local":
            p = query.get("path", [""])[0]
            try:
                self._send_json(browse_local(p))
            except Exception as e:
                self._send_json({"ok": False, "path": p, "parent": "/", "home": os.path.expanduser("~"), "entries": [], "message": str(e)})
        elif path == "/api/browse/remote":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            p = query.get("path", [""])[0]
            if not self._server_is_connected(srv):
                self._send_json({"ok": False, "path": p, "entries": [], "message": "Server is disconnected"}, status=409)
                return
            try:
                self._send_json(browse_remote(srv, p))
            except Exception as e:
                self._send_json({"ok": False, "path": p, "parent": "/", "entries": [], "message": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/") and not self._api_authenticated():
            self._reject_unauthenticated_request()
            return
        if not self._local_browser_request() or not self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/json":
            self._reject_untrusted_request()
            return
        try:
            body = self._read_json()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "message": f"Invalid JSON request: {exc}"}, status=400)
            return

        if path == "/api/forward":
            lp = body.get("local_port")
            rp = body.get("remote_port", lp)
            label = body.get("label", "")
            always = body.get("always", False)
            srv = body.get("server_id") or body.get("server")
            if not self._require_server_connected(srv):
                return
            try:
                add_forward(lp, rp, label=label, always=always, server_ref=srv)
            except ValueError as exc:
                self._send_json({"ok": False, "message": str(exc)}, status=409)
                return
            self._send_json({"ok": True, "message": f"Port {lp} forwarded successfully"})
        elif path == "/api/forward/label":
            lp = body.get("local_port")
            label = body.get("label", "")
            srv = body.get("server_id") or body.get("server")
            if not self._require_server_connected(srv):
                return
            if not isinstance(label, str):
                self._send_json({"ok": False, "message": "label must be a string"}, status=400)
                return
            try:
                update_forward_label(lp, label, server_ref=srv)
            except (TypeError, ValueError):
                self._send_json({"ok": False, "message": "local_port must be a valid port number"}, status=400)
                return
            self._send_json({"ok": True, "message": f"Port {lp} label updated"})
        elif path == "/api/remove":
            lp = body.get("local_port")
            srv = body.get("server_id") or body.get("server")
            if not self._require_server_connected(srv):
                return
            remove_forward(lp, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} removed"})
        elif path == "/api/toggle":
            lp = body.get("local_port")
            always = body.get("always", False)
            srv = body.get("server_id") or body.get("server")
            if not self._require_server_connected(srv):
                return
            toggle_always(lp, always, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} persistence updated"})
        elif path == "/api/clean":
            srv = body.get("server_id") or body.get("server")
            if not self._require_server_connected(srv):
                return
            killed = clean_orphaned_tunnels(server_ref=srv)
            self._send_json({"ok": True, "killed": killed})
        elif path == "/api/docker/labels":
            labels = body.get("labels")
            if not isinstance(labels, list):
                self._send_json({"ok": False, "message": "labels must be a list"}, status=400)
                return
            clean = []
            seen = set()
            for raw in labels:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                match = str(raw.get("match") or "").strip()
                color = str(raw.get("color") or "#8b949e").strip()
                if not name or not match:
                    continue
                ident = str(raw.get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or uuid.uuid4().hex[:8])
                if ident in seen:
                    continue
                seen.add(ident)
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    color = "#8b949e"
                clean.append({"id": ident, "name": name[:80], "match": match[:120], "color": color, "enabled": bool(raw.get("enabled", True))})
            cfg = load_config()
            existing_labels = cfg.get("docker_labels", [])
            if existing_labels and not clean:
                self._send_json({"ok": False, "message": "Refusing to replace existing Docker labels with an empty list"}, status=400)
                return
            cfg["docker_labels"] = clean
            save_config(cfg)
            self._send_json({"ok": True, "labels": clean})
        elif path == "/api/docker/action":
            operation = str(body.get("action") or "").strip().lower()
            container = str(body.get("container") or body.get("name") or body.get("id") or "").strip()
            if operation not in ("start", "stop", "restart"):
                self._send_json({"ok": False, "message": "action must be start, stop, or restart"}, status=400)
                return
            if not container:
                self._send_json({"ok": False, "message": "container is required"}, status=400)
                return
            srv = body.get("server_id") or body.get("server")
            if not self._require_server_connected(srv):
                return
            try:
                result = docker_container_action(srv, container, operation)
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, status=500)
                return
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/usage/accounts":
            try:
                self._send_json({"ok": True, "account": save_usage_account(body)})
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
        elif path == "/api/usage/accounts/reorder":
            try:
                accounts = reorder_usage_accounts(body.get("order", []))
                self._send_json({"ok": True, "accounts": accounts})
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
        elif path == "/api/usage/refresh":
            if not self._require_server_connected(self._server_param({}, body)):
                return
            try:
                kwargs = {"refresh": True, "account_id": body.get("account_id")}
                srv = self._server_param({}, body)
                if srv:
                    kwargs["server_ref"] = srv
                self._send_json({"ok": True, **get_usage_status(**kwargs)})
            except Exception as e:
                self._send_json({"ok": False, "message": str(e)}, status=500)
        elif path == "/api/usage/accounts/remove":
            aid = body.get("id") or body.get("account_id")
            if not aid or not remove_usage_account(aid):
                self._send_json({"ok": False, "message": "Usage account not found"}, status=404)
            else:
                self._send_json({"ok": True})
        elif path == "/api/settings/active-server":
            server_id = body.get("server_id") or body.get("server")
            cfg = load_config()
            # Compatibility for older callers/config fixtures that predate
            # multi-server connection state. Real configs are migrated before
            # reaching this endpoint.
            if "connected_server_ids" not in cfg:
                if not server_id:
                    cfg["active_server_id"] = ""
                    save_config(cfg)
                    self._send_json({"ok": True, "active_server_id": None,
                                     "last_connected_server_id": cfg.get("last_connected_server_id") or None})
                    return
                server = next((item for item in get_servers(cfg)
                               if item.get("id") == server_id or item.get("ssh_host") == server_id), None)
                if not server:
                    self._send_json({"ok": False, "message": "Server not found"}, status=404)
                    return
                cfg["active_server_id"] = server["id"]
                cfg["last_connected_server_id"] = server["id"]
                save_config(cfg)
                self._send_json({"ok": True, "active_server_id": server["id"],
                                 "last_connected_server_id": server["id"]})
                return
            if not server_id:
                self._send_json({"ok": False, "message": "server id is required"}, status=400)
                return
            result = set_server_connected(server_id, bool(body.get("connected", True)))
            if not result:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            cfg = load_config()
            self._send_json({"ok": True, **result,
                             "active_server_id": cfg.get("active_server_id") or "",
                             "last_connected_server_id": cfg.get("last_connected_server_id") or ""})
        elif path == "/api/local-ports/kill":
            pid = body.get("pid")
            if pid is None:
                self._send_json({"ok": False, "message": "pid is required"}, status=400)
                return
            result = kill_listening_process(pid)
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/servers":
            ssh_host = (body.get("ssh_host") or "").strip()
            name = (body.get("name") or "").strip()
            ip = (body.get("ip") or "").strip()
            if not ssh_host:
                self._send_json({"ok": False, "message": "ssh_host is required"}, status=400)
                return
            try:
                server = add_server(ssh_host, name=name or ssh_host, ip=ip)
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            self._send_json({"ok": True, "server": server, "message": f"Server '{ssh_host}' added"})
        elif path == "/api/settings/menubar":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json({"ok": False, "message": "enabled must be a boolean"}, status=400)
                return
            cfg = load_config()
            cfg["menubar_enabled"] = enabled
            save_config(cfg)
            self._send_json({"ok": True, "menubar_enabled": enabled})
        elif path == "/api/servers/remove":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            if not sid:
                self._send_json({"ok": False, "message": "server id is required"}, status=400)
                return
            try:
                ok = remove_server_entry(sid)
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            if not ok:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "message": "Server removed"})
        elif path == "/api/servers/reorder":
            order = body.get("order", [])
            servers = reorder_servers(order)
            self._send_json({"ok": True, "servers": servers})
        elif path == "/api/servers/pin":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            pinned = body.get("pinned", True)
            server = set_server_pinned(sid, pinned)
            if not server:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "server": server})
        elif path == "/api/servers/update":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            server = update_server_entry(sid, name=body.get("name"), ip=body.get("ip"))
            if not server:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "server": server})
        elif path == "/api/syncs":
            if not self._require_server_connected(body.get("server_id") or body.get("server")):
                return
            try:
                result = add_sync(
                    server_ref=body.get("server_id") or body.get("server"),
                    local_path=body.get("local_path", ""),
                    remote_path=body.get("remote_path", ""),
                    direction=body.get("direction", SYNC_DEFAULT_DIRECTION),
                    mirror=bool(body.get("mirror", False)),
                    always=bool(body.get("always", False)),
                    interval=body.get("interval", SYNC_DEFAULT_INTERVAL),
                    run_now=bool(body.get("run_now", True)),
                )
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            except Exception as e:
                self._send_json({"ok": False, "message": f"Failed to add sync: {e}"}, status=500)
                return
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/syncs/remove":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            sync = get_sync(load_config(), sid)
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            if not self._require_server_connected(sync.get("server_id")):
                return
            ok = remove_sync_entry(sid)
            if not ok:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            self._send_json({"ok": True, "message": "Folder sync removed"})
        elif path == "/api/syncs/run":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            sync = get_sync(load_config(), sid)
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            if not self._require_server_connected(sync.get("server_id")):
                return
            res = run_sync(sid)
            self._send_json({"ok": bool(res.get("ok")), "message": res.get("message", "")},
                            status=200 if res.get("ok") else 500)
        elif path == "/api/syncs/toggle":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            sync = get_sync(load_config(), sid)
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            if not self._require_server_connected(sync.get("server_id")):
                return
            sync = toggle_sync_always(sid, bool(body.get("always", False)),
                                      interval=body.get("interval"))
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            mode = "Auto (persistent)" if sync.get("always") else "Once (one-time)"
            self._send_json({"ok": True, "sync": sync, "message": f"Sync mode: {mode}"})
        elif path == "/api/syncs/update":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            sync = get_sync(load_config(), sid)
            if sync and not self._require_server_connected(sync.get("server_id")):
                return
            try:
                sync = update_sync(
                    sid,
                    local_path=body.get("local_path") if "local_path" in body else None,
                    remote_path=body.get("remote_path") if "remote_path" in body else None,
                    direction=body.get("direction") if "direction" in body else None,
                    mirror=body.get("mirror") if "mirror" in body else None,
                    always=body.get("always") if "always" in body else None,
                    interval=body.get("interval") if "interval" in body else None,
                )
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            result = {"ok": True, "sync": dict(sync), "message": "Folder sync updated"}
            if bool(body.get("run_now", True)):
                res = run_sync(sid)
                try:
                    sync2 = get_sync(load_config(), sid)
                    if sync2:
                        result["sync"] = dict(sync2)
                except Exception:
                    pass
                result["run"] = res
                result["message"] = res.get("message", result["message"])
                result["ok"] = bool(res.get("ok"))
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/browse/mkdir":
            which = (body.get("which") or "").strip().lower()
            if which == "local":
                try:
                    result = mkdir_local(body.get("path", ""), body.get("name", ""))
                except ValueError as e:
                    self._send_json({"ok": False, "message": str(e)}, status=400)
                    return
                self._send_json(result, status=200 if result.get("ok") else 500)
            elif which == "remote":
                if not self._require_server_connected(body.get("server_id") or body.get("server")):
                    return
                try:
                    result = mkdir_remote(body.get("server_id") or body.get("server"),
                                          body.get("path", ""), body.get("name", ""))
                except ValueError as e:
                    self._send_json({"ok": False, "message": str(e)}, status=400)
                    return
                self._send_json(result, status=200 if result.get("ok") else 500)
            else:
                self._send_json({"ok": False, "message": "which must be 'local' or 'remote'"}, status=400)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def make_handler(runtime):
    """Bind the compatibility runtime and return the dashboard handler class."""
    # The handler remains a normal BaseHTTPRequestHandler subclass, while its
    # endpoint functions are supplied by the legacy runtime during migration.
    globals().update(vars(runtime))
    return DashboardHandler
