import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
import sys
import os
import types
import subprocess
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestDashboardAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start server on dynamic random port
        cls.server = HTTPServer(("127.0.0.1", 0), devboost.DashboardHandler)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_dashboard_html(self):
        req = urllib.request.Request(self.base_url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            html = resp.read().decode("utf-8")
            self.assertIn("DevBoost", html)
            self.assertIn('/dashboard/styles.css', html)
            self.assertIn('/dashboard/app.js', html)
            self.assertIn('id="header-server-name"', html)

    def test_server_selector_is_global_and_precedes_all_pages(self):
        with urllib.request.urlopen(self.base_url + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertEqual(html.count('id="tabs-bar"'), 1)
        tabs_position = html.index('id="tabs-bar"')
        page_positions = [html.index(f'id="page-{page}"') for page in (
            "home", "forwards", "docker", "syncs", "services", "usage"
        )]
        self.assertLess(tabs_position, min(page_positions))

    def test_workspace_tabs_follow_server_selector_and_track_pages(self):
        with urllib.request.urlopen(self.base_url + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn('id="workspace-tabs"', html)
        self.assertLess(html.index('id="tabs-bar"'), html.index('id="workspace-tabs"'))
        for page in ("home", "forwards", "docker", "syncs", "services", "usage"):
            self.assertIn(f'data-page="{page}"', html)
        self.assertIn(">Quotas</button>", html)
        self.assertLess(html.index(">Quotas</button>"), html.index(">Services</button>"))

        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn('document.querySelectorAll(".workspace-tab")', app_js)
        self.assertIn('tab.dataset.page === valid', app_js)
        self.assertIn('document.getElementById("header-server-name").innerText = data.server_name', app_js)
        self.assertIn('header-server-name', app_js)

    def test_mirror_syncs_warn_before_destructive_runs(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn("function confirmMirrorSync(direction, localPath, remotePath)", app_js)
        script = r'''const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const match = source.match(/function confirmMirrorSync[\s\S]*?\n    \}/);
if (!match) throw new Error("mirror confirmation helper missing");
const confirmMirrorSync = eval("(" + match[0] + ")");
let prompts = 0;
let lastPrompt = "";
global.confirm = message => { prompts++; lastPrompt = message; return global.answer; };
const result = {};
global.answer = false;
result.cancelPush = confirmMirrorSync("push", "/source", "~/destination");
global.answer = true;
result.acceptPull = confirmMirrorSync("pull", "/local", "/remote");
result.promptNamesPaths = lastPrompt.includes("Source: /remote") && lastPrompt.includes("Destination: /local");
result.twoWay = confirmMirrorSync("two-way", "/local", "/remote");
result.nonMirror = confirmMirrorSync("push");
result.prompts = prompts;
process.stdout.write(JSON.stringify(result));'''
        result = subprocess.run(
            ["node", "-e", script, os.path.join(os.path.dirname(__file__), "..", "dashboard", "app.js")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), {
            "cancelPush": False, "acceptPull": True, "twoWay": True,
            "promptNamesPaths": True, "nonMirror": True, "prompts": 2,
        })

    def test_quotas_can_query_this_mac(self):
        with urllib.request.urlopen(self.base_url + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn('id="usage-tab-remote"', html)
        self.assertIn('id="usage-tab-local"', html)
        self.assertIn("💻 This Mac", html)

        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn('function switchUsageTab(which)', app_js)
        self.assertIn('usageTab === "remote" && currentServerId', app_js)
        self.assertIn('if (valid === "usage") fetchUsage(true)', app_js)

    def test_usage_bar_assets_are_served_and_rendered(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/usage_metrics.js") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("usageGrade", resp.read().decode("utf-8"))
        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn("role=\"meter\"", app_js)
        self.assertIn("usageMetrics.usageGrade", app_js)
        self.assertIn('const account = (accounts || [])[0];', app_js)
        self.assertIn('const index = quotas.findIndex(quota => usagePercent(quota) != null);', app_js)
        self.assertNotIn('const codex = (accounts || []).find', app_js)
        self.assertIn("${resetCount}X RESETS", app_js)
        self.assertIn('a.provider === "codex" ? (s.available_resets || 0) : 0', app_js)
        self.assertNotIn("usageResetCount", app_js)
        self.assertIn("function formatTokenCount(value)", app_js)
        self.assertIn('scale[0] === 1e3', app_js)
        self.assertIn('const tokenUsage = quota.used != null && /^tokens?$/i.test(unit);', app_js)
        self.assertIn("${formatTokenCount(quota.used)} tokens used", app_js)
        with urllib.request.urlopen(self.base_url + "/dashboard/usage_metrics.js") as resp:
            metrics_js = resp.read().decode("utf-8")
        self.assertIn("usage-grade-green", metrics_js)
        with urllib.request.urlopen(self.base_url + "/dashboard/styles.css") as resp:
            css = resp.read().decode("utf-8")
        for grade in ("green", "yellow", "orange", "red"):
            self.assertIn(f".usage-grade-{grade} span", css)

    def test_home_tiles_use_readable_emphasized_summary_parts(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn("setHomeSummaryParts", app_js)
        for class_name in ("tile-summary-count", "tile-port", "tile-docker", "tile-sync", "tile-usage"):
            self.assertIn(class_name, app_js)
        with urllib.request.urlopen(self.base_url + "/dashboard/styles.css") as resp:
            css = resp.read().decode("utf-8")
        self.assertIn("font-size: 19px", css)
        self.assertIn("font-size: 14px", css)
        self.assertIn("font-weight: 800", css)

    def test_server_tabs_and_management_view_can_be_reordered(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn("editServer", app_js)
        self.assertIn('class="server-drag-handle" draggable="true"', app_js)
        self.assertIn("onServerCardDragOver", app_js)
        self.assertIn("onServerCardDrop", app_js)
        self.assertIn('data-server-id="${s.id}" draggable="true"', app_js)
        self.assertIn("onServerTabDragStart", app_js)
        self.assertIn("onServerTabDragOver", app_js)
        self.assertIn("onServerTabDrop", app_js)
        self.assertIn("Manage servers", app_js)
        self.assertIn("renderServerManagement", app_js)
        self.assertIn("onclick=\"editServer('${s.id}')\"", app_js)
        self.assertIn("onclick=\"removeServerTab('${s.id}')\"", app_js)
        self.assertNotIn("title=\"Edit server\"", app_js)
        self.assertIn("server-card-drop-shadow", app_js)
        self.assertIn("server-tab-drop-shadow", app_js)
        self.assertNotIn("s.active_count || 0", app_js)
        self.assertNotIn("s.always_count || 0", app_js)
        self.assertNotIn("togglePin('${s.id}')", app_js)

        with urllib.request.urlopen(self.base_url + "/dashboard/styles.css") as resp:
            css = resp.read().decode("utf-8")
        self.assertIn(".server-card-drop-shadow", css)
        self.assertIn(".server-tab-drop-shadow", css)
        self.assertIn("box-shadow:", css)

    def test_docker_log_modal_keeps_pre_scrollable(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/styles.css") as resp:
            css = resp.read().decode("utf-8")
        self.assertIn("#docker-log-modal .modal-body", css)
        self.assertIn("display: flex", css)
        self.assertIn("overflow: hidden", css)
        self.assertIn(".docker-log-output", css)
        self.assertIn("overflow: auto", css)

    def test_usage_account_modal_scrolls_when_too_tall(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/styles.css") as resp:
            css = resp.read().decode("utf-8")
        with urllib.request.urlopen(self.base_url + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("#usage-modal { padding: 16px; }", css)
        self.assertIn("#usage-modal .modal {", css)
        self.assertIn("#usage-modal .modal-body { min-height: 0; overflow-y: auto; }", css)
        self.assertIn("usage-form-section--source", html)
        self.assertIn("usage-form-section--advanced", html)
        self.assertIn(".usage-form-section-title", css)
        self.assertIn("font-size: 20px", css)

    def test_usage_rows_can_be_dragged_and_reordered(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn('class="usage-drag-handle" draggable="true"', app_js)
        self.assertIn("onUsageRowDragOver", app_js)
        self.assertIn("onUsageRowDrop", app_js)
        self.assertIn("/api/usage/accounts/reorder", app_js)
        self.assertIn('aria-label="Edit account"', app_js)
        self.assertIn('aria-label="Remove account"', app_js)
        self.assertIn("usage-actions", app_js)
        self.assertNotIn("usage-nav-icon", app_js)
        with urllib.request.urlopen(self.base_url + "/dashboard/styles.css") as resp:
            css = resp.read().decode("utf-8")
        self.assertIn("#usage-body td { font-size: 14px; padding: 10px 12px; }", css)
        self.assertIn(".usage-quota { min-width: 165px;", css)
        self.assertIn(".usage-meter { position: relative; height: 24px;", css)
        self.assertIn("#page-usage table { table-layout: fixed; }", css)
        self.assertIn("#page-usage th:first-child, #page-usage td:first-child { width: 7%; }", css)
        self.assertIn("#page-usage th:nth-child(3), #page-usage td:nth-child(3) { width: 28%; }", css)
        self.assertIn("#page-usage th:last-child, #page-usage td:last-child { width: 132px;", css)
        self.assertIn(".usage-row-drop-shadow", css)
        self.assertIn(".usage-actions", css)

    def test_usage_account_name_defaults_from_provider_until_edited(self):
        with urllib.request.urlopen(self.base_url + "/dashboard/app.js") as resp:
            app_js = resp.read().decode("utf-8")
        with urllib.request.urlopen(self.base_url + "/") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("usageNameAuto = !account", app_js)
        self.assertIn(": usageProviderLabel()", app_js)
        self.assertIn("if (usageNameAuto) document.getElementById(\"usage-name\").value = usageProviderLabel()", app_js)
        self.assertIn('oninput="usageNameChanged()"', html)

    def test_stale_server_response_guard_unit(self):
        app_js = os.path.join(os.path.dirname(__file__), "..", "dashboard", "app.js")
        with open(app_js, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("responseBelongsToServer(requestedServerId, currentServerId", source)
        script = r'''const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const match = source.match(/function responseBelongsToServer[\s\S]*?\n    \}/);
if (!match) throw new Error("guard function missing");
const guard = eval("(" + match[0] + ")");
process.stdout.write(JSON.stringify([
  guard("one", "one", "one"),
  guard("one", "two", "one"),
  guard("one", "one", "two"),
  guard("one", "one", "")
]));'''
        result = subprocess.run(
            ["node", "-e", script, app_js], capture_output=True, text=True, check=True
        )
        self.assertEqual(json.loads(result.stdout), [True, False, False, True])

    def test_favicon_and_head_routes(self):
        with urllib.request.urlopen(self.base_url + "/favicon.png") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Type"), "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))
        request = urllib.request.Request(self.base_url + "/", method="HEAD")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type"))

    def test_api_rejects_cross_origin_requests(self):
        request = urllib.request.Request(
            f"{self.base_url}/api/status",
            headers={"Origin": "https://attacker.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 403)

        request = urllib.request.Request(
            f"{self.base_url}/api/clean",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://localhost:{self.port}",
            },
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)

    def test_api_rejects_non_json_posts(self):
        request = urllib.request.Request(
            f"{self.base_url}/api/clean",
            data=b"{}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 403)

        request = urllib.request.Request(
            f"{self.base_url}/api/clean",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 400)

    def test_dashboard_assets_are_served_separately(self):
        for path, marker, content_type in (
            ("/dashboard/styles.css", "--bg:", "text/css"),
            ("/dashboard/app.js", "fetchServers", "text/javascript"),
        ):
            with urllib.request.urlopen(self.base_url + path) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(content_type, resp.headers.get("Content-Type"))
                asset = resp.read().decode("utf-8")
                self.assertIn(marker, asset)
                if path.endswith("app.js"):
                    for unit in ("second", "minute", "hour", "day", "last_valid_query_at"):
                        self.assertIn(unit, asset)

    def test_api_status(self):
        req = urllib.request.Request(f"{self.base_url}/api/status")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("server_reachable", data)
            self.assertIn("forwards", data)
            self.assertIn("server_name", data)
            self.assertIn("server_host", data)

    def test_service_binder_ignores_main_wrappers(self):
        """Running the packaged script as __main__ must not recurse in /api/status."""
        def wrapper(*args, **kwargs):
            return None

        wrapper.__module__ = "__main__"
        runtime = types.SimpleNamespace(__name__="__main__", get_all_forwards_status=wrapper)
        bound = devboost.forwarding._bound_functions(runtime)
        self.assertIsNot(bound["get_all_forwards_status"], wrapper)
        self.assertEqual(bound["get_all_forwards_status"].__module__, "devboost_app.forwarding")

        sync_runtime = types.SimpleNamespace(__name__="__main__", check_rsync_prereqs=wrapper)
        sync_bound = devboost.folder_sync._bound_functions(sync_runtime)
        self.assertIsNot(sync_bound["check_rsync_prereqs"], wrapper)
        self.assertEqual(sync_bound["check_rsync_prereqs"].__module__, "devboost_app.folder_sync")

    def test_api_scan(self):
        req = urllib.request.Request(f"{self.base_url}/api/scan")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("services", data)
            self.assertIsInstance(data["services"], list)

    @patch.object(devboost.DOCKER_MONITOR, "get")
    def test_api_docker(self, mock_docker):
        mock_docker.return_value = {
            "ok": True,
            "available": True,
            "containers": [{"name": "web", "id": "abc", "stats": {"cpu_percent": "1%"}}],
            "stats": [],
            "updated_at": 123,
            "refreshing": False,
        }
        req = urllib.request.Request(f"{self.base_url}/api/docker")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["available"])
            self.assertEqual(data["containers"][0]["name"], "web")

    def test_api_clean(self):
        req = urllib.request.Request(f"{self.base_url}/api/clean", data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertIn("killed", data)

    def test_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base_url}/non-existent-endpoint")
        self.assertEqual(ctx.exception.code, 404)

    def _post_json(self, path, body):
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    @patch("dashboard.http.add_forward")
    def test_api_forward_passes_server_and_defaults_remote_port(self, add):
        status, data = self._post_json("/api/forward", {"local_port": 3000, "server_id": "box", "always": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        add.assert_called_once_with(3000, 3000, label="", always=True, server_ref="box")

    @patch("dashboard.http.remove_forward")
    @patch("dashboard.http.toggle_always")
    def test_api_remove_and_toggle_forward_routes(self, toggle, remove):
        self._post_json("/api/remove", {"local_port": 3000, "server": "box"})
        self._post_json("/api/toggle", {"local_port": 3000, "always": True, "server": "box"})
        remove.assert_called_once_with(3000, server_ref="box")
        toggle.assert_called_once_with(3000, True, server_ref="box")

    def test_api_docker_logs_requires_container(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base_url + "/api/docker/logs")
        self.assertEqual(ctx.exception.code, 400)

    @patch("dashboard.http.save_config")
    @patch("dashboard.http.load_config")
    def test_api_docker_labels_sanitizes_invalid_rows(self, load, save):
        load.return_value = {"docker_labels": []}
        _, data = self._post_json("/api/docker/labels", {"labels": [
            {"id": "x", "name": " Dev ", "match": " dev ", "color": "bad"},
            {"id": "x", "name": "duplicate", "match": "dup"},
            {"name": "", "match": "skip"},
        ]})
        self.assertEqual(len(data["labels"]), 1)
        self.assertEqual(data["labels"][0]["color"], "#8b949e")
        self.assertEqual(save.call_args.args[0]["docker_labels"], data["labels"])

    @patch("dashboard.http.save_config")
    @patch("dashboard.http.load_config", return_value={"docker_labels": [{"name": "Keep", "match": "keep"}]})
    def test_api_docker_labels_does_not_clear_existing_labels_accidentally(self, load, save):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/docker/labels", {"labels": []})
        self.assertEqual(ctx.exception.code, 400)
        save.assert_not_called()

    @patch("dashboard.http.save_usage_account", return_value={"id": "codex-work"})
    def test_api_usage_account_validation_and_save(self, save):
        _, data = self._post_json("/api/usage/accounts", {"provider": "codex", "name": "Work"})
        self.assertTrue(data["ok"])
        save.assert_called_once()
        save.side_effect = ValueError("provider must be codex")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/usage/accounts", {"provider": "invalid", "name": "x"})
        self.assertEqual(ctx.exception.code, 400)

    @patch("dashboard.http.reorder_usage_accounts", return_value=[{"id": "two"}, {"id": "one"}])
    def test_api_usage_account_reorder_delegates(self, reorder):
        _, data = self._post_json("/api/usage/accounts/reorder", {"order": ["two", "one"]})
        self.assertTrue(data["ok"])
        reorder.assert_called_once_with(["two", "one"])

    @patch("dashboard.http.save_usage_account", side_effect=ValueError("usage account not found"))
    def test_api_usage_edit_with_stale_id_returns_error(self, save):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/usage/accounts", {
                "id": "stale-id", "provider": "custom", "name": "Changed", "balance_url": "https://example.test/new",
            })
        self.assertEqual(ctx.exception.code, 400)
        save.assert_called_once()

    @patch("dashboard.http.remove_usage_account", return_value=False)
    def test_api_usage_remove_reports_missing_account(self, remove):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/usage/accounts/remove", {"id": "missing"})
        self.assertEqual(ctx.exception.code, 404)
        remove.assert_called_once_with("missing")

    @patch("dashboard.http.add_server", return_value={"id": "box", "ssh_host": "box"})
    def test_api_server_add_requires_host_and_returns_server(self, add):
        _, data = self._post_json("/api/servers", {"ssh_host": "box", "name": "Box"})
        self.assertTrue(data["ok"])
        add.assert_called_once_with("box", name="Box", ip="")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/api/servers", {})
        self.assertEqual(ctx.exception.code, 400)

    @patch("dashboard.http.get_usage_status", return_value={"accounts": [], "snapshots": {}})
    def test_api_usage_get_accepts_refresh_and_account_filter(self, get_usage):
        with urllib.request.urlopen(self.base_url + "/api/usage?refresh=true&account=work") as response:
            self.assertEqual(response.status, 200)
        get_usage.assert_called_once_with(refresh=True, account_id="work")

    @patch("dashboard.http.get_usage_status", return_value={"accounts": [], "snapshots": {}, "server_id": "box"})
    def test_api_usage_get_targets_selected_server(self, get_usage):
        with urllib.request.urlopen(self.base_url + "/api/usage?server=box&refresh=1") as response:
            self.assertEqual(response.status, 200)
        get_usage.assert_called_once_with(refresh=True, account_id=None, server_ref="box")

    @patch("dashboard.http.get_docker_logs", return_value={"ok": True, "logs": "hello"})
    def test_api_docker_logs_delegates_container_and_tail(self, get_logs):
        with urllib.request.urlopen(self.base_url + "/api/docker/logs?server=box&container=web&tail=25") as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["logs"], "hello")
        get_logs.assert_called_once_with("box", "web", "25")

    @patch("dashboard.http.set_server_pinned", return_value={"id": "box", "pinned": True})
    @patch("dashboard.http.update_server_entry", return_value={"id": "box", "name": "Box"})
    @patch("dashboard.http.reorder_servers", return_value=[])
    def test_api_server_mutations_delegate(self, reorder, update, pin):
        self._post_json("/api/servers/reorder", {"order": ["box"]})
        self._post_json("/api/servers/pin", {"id": "box", "pinned": True})
        self._post_json("/api/servers/update", {"id": "box", "name": "Box"})
        reorder.assert_called_once_with(["box"])
        pin.assert_called_once_with("box", True)
        update.assert_called_once_with("box", name="Box", ip=None)

    @patch("dashboard.http.run_sync", return_value={"ok": True, "message": "synced"})
    @patch("dashboard.http.update_sync", return_value={"id": "sync-1"})
    def test_api_sync_update_runs_by_default(self, update, run):
        _, data = self._post_json("/api/syncs/update", {"id": "sync-1", "direction": "push"})
        self.assertTrue(data["ok"])
        update.assert_called_once()
        run.assert_called_once_with("sync-1")

    @patch("dashboard.http.kill_listening_process", return_value={"ok": True, "message": "stopped"})
    def test_api_local_port_kill_requires_pid_and_returns_result(self, kill):
        _, data = self._post_json("/api/local-ports/kill", {"pid": 42})
        self.assertTrue(data["ok"])
        kill.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
