#!/usr/bin/env python3
"""Send a redacted DevBoost status snapshot to a phone through ntfy.

Configuration is supplied through the environment (or the user's DevBoost
.env, when running through the installed launcher):

  NTFY_TOPIC                 Required topic name; never store it in the repo.
  NTFY_SERVER                Optional server URL, default: https://ntfy.sh
  NTFY_TITLE                 Optional notification title.
  NTFY_PRIORITY              Optional ntfy priority.
  NTFY_TAGS                  Optional comma-separated ntfy tags.
  DEVBOOST_DASHBOARD_URL     Optional local URL, default: http://127.0.0.1:3080

The notification is intentionally a summary rather than a dump of the API:
hostnames, paths, account names, and credentials are not sent to the phone.
"""

import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_DASHBOARD_URL = "http://127.0.0.1:3080"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


class StateSendError(RuntimeError):
    """Raised when the dashboard snapshot or notification cannot be sent."""


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _request_json(opener, url):
    try:
        with opener.open(url, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        raise StateSendError(f"Could not read DevBoost state from {url}: {exc}") from exc


def fetch_state(base_url):
    """Fetch dashboard endpoints using the dashboard's session cookie."""
    base_url = base_url.rstrip("/")
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        with opener.open(base_url + "/", timeout=15):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise StateSendError(f"Could not connect to DevBoost at {base_url}: {exc}") from exc

    endpoints = ("/api/status", "/api/servers", "/api/syncs", "/api/docker", "/api/usage")
    return {endpoint[5:]: _request_json(opener, base_url + endpoint) for endpoint in endpoints}


def _redacted_summary(state):
    """Return a phone-friendly summary without user-identifying fields."""
    status = state.get("status", {})
    forwards = status.get("forwards", [])
    active_forwards = sum(1 for forward in forwards if forward.get("active"))
    configured_servers = len(state.get("servers", {}).get("servers", []))
    server_state = "online" if status.get("server_reachable") is True else "offline"

    sync_payload = state.get("syncs", {})
    syncs = sync_payload.get("syncs", [])
    sync_ok = sum(1 for sync in syncs if sync.get("last_status") == "ok")

    docker = state.get("docker", {})
    containers = docker.get("containers", []) if docker.get("available") is True else []

    usage = state.get("usage", {})
    usage_accounts = usage.get("accounts", [])
    usage_snapshots = usage.get("snapshots", {})
    usage_ok = sum(
        1 for account in usage_accounts
        if usage_snapshots.get(account.get("id"), {}).get("ok")
    )

    lines = [
        "DevBoost latest state",
        f"Selected server: {server_state} ({configured_servers} configured)",
        f"Forwards: {active_forwards}/{len(forwards)} active",
        f"Folder syncs: {sync_ok}/{len(syncs)} healthy",
        f"Docker: {len(containers)} running" if docker.get("available") is True else "Docker: unavailable",
        f"AI usage: {usage_ok}/{len(usage_accounts)} available",
    ]
    return "\n".join(lines)


def send_ntfy(message, server, topic, title, priority="", tags=""):
    """Publish a text notification to ntfy."""
    url = server.rstrip("/") + "/" + urllib.parse.quote(topic, safe="")
    headers = {"Content-Type": "text/plain; charset=utf-8", "Title": title}
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags
    request = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status < 200 or response.status >= 300:
                raise StateSendError(f"ntfy returned HTTP {response.status}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        raise StateSendError(f"Could not send notification to ntfy: {exc}") from exc


def main():
    topic = _env("NTFY_TOPIC")
    if not topic:
        print("Set NTFY_TOPIC in your environment before running this script.", file=sys.stderr)
        return 2
    try:
        state = fetch_state(_env("DEVBOOST_DASHBOARD_URL", DEFAULT_DASHBOARD_URL))
        message = _redacted_summary(state)
        send_ntfy(
            message,
            _env("NTFY_SERVER", DEFAULT_NTFY_SERVER),
            topic,
            _env("NTFY_TITLE", "DevBoost"),
            _env("NTFY_PRIORITY"),
            _env("NTFY_TAGS"),
        )
    except StateSendError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Sent the latest redacted DevBoost state to your phone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
