"""Provider usage adapters: local records, commands, HTTPS, and admin APIs."""

import datetime
import glob
import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

from remote_transport import run_ssh_command
from . import codex_usage
from .usage import parse_opencode_stats, provider_default_command, safe_number


def local_usage_path(account, provider):
    default = "~/.codex/sessions" if provider == "codex" else "~/.claude/projects"
    if provider == "codex":
        default = os.path.join(account.get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex", "sessions")
    return os.path.expanduser(str(account.get("local_path") or default))


def _window_label(key, window):
    explicit = window.get("name") or window.get("label") or window.get("window_name")
    if explicit:
        return str(explicit)
    minutes = safe_number(window.get("window_minutes"))
    if minutes is not None and minutes > 0:
        minutes = int(minutes)
        if minutes % (7 * 24 * 60) == 0:
            duration = "weekly" if minutes == 7 * 24 * 60 else f"{minutes // (7 * 24 * 60)}-week"
        elif minutes % (24 * 60) == 0:
            duration = f"{minutes // (24 * 60)}-day"
        elif minutes % 60 == 0:
            duration = f"{minutes // 60}-hour"
        else:
            duration = f"{minutes}-minute"
        return f"{key.capitalize()} ({duration} window)"
    return f"{key.capitalize()} window"


def _codex_rate_limit_result(limits, now=None):
    """Convert historical Codex limits without inventing a fresh allowance."""
    now = time.time() if now is None else now
    quotas = []
    for key in ("primary", "secondary"):
        window = limits.get(key) or {}
        used = safe_number(window.get("used_percent"))
        if used is None:
            continue
        reset_at = safe_number(window.get("resets_at"))
        expired = reset_at is not None and reset_at <= now
        quotas.append({"name": _window_label(key, window) + (" (expired; refresh needed)" if expired else ""),
                       "used": None if expired else used, "limit": None if expired else 100,
                       "remaining": None if expired else max(0, 100 - used), "unit": "%",
                       "reset_at": reset_at,
                       "window_minutes": window.get("window_minutes")})
    result = {"quotas": quotas}
    credits = limits.get("credits") or {}
    balance = safe_number(credits.get("balance"))
    if balance is not None:
        result["balances"] = [{"remaining": balance, "currency": "credits"}]
    result["plan_type"] = limits.get("plan_type")
    return result


def read_codex_api(runtime, account, server=None):
    """Use the selected host's Codex login and the official account API."""
    if server:
        # This standalone stdlib helper needs no DevBoost installation remotely.
        with open(codex_usage.__file__, encoding="utf-8") as stream:
            script = stream.read()
        command = shlex.join(["python3", "-c", script, str(account.get("codex_home") or ""),
                              str(max(1, runtime.USAGE_TIMEOUT - 4))])
        response = run_ssh_command(server["ssh_host"], command,
                                   timeout=runtime.USAGE_TIMEOUT, connect_timeout=5)
        if response.returncode:
            raise ValueError("Could not query Codex on the selected SSH host. Check SSH connectivity and that Python 3 and Codex are installed.")
        try:
            envelope = json.loads(response.stdout)
        except ValueError:
            raise ValueError("Remote Codex query returned invalid JSON.") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), dict):
            known_errors = (
                "Codex CLI not found on this host. Install Codex and sign in with ChatGPT.",
                "Codex live quota query timed out. Retry when the service is reachable.",
                "Update Codex: this version does not support the account quota API.",
                "Codex could not fetch live quotas. Check connectivity and ChatGPT sign-in on this host; API-key accounts do not expose subscription quotas.",
                "Codex app-server exited before returning quotas. Check your Codex installation and sign-in.",
            )
            if isinstance(envelope, dict) and envelope.get("error") in known_errors:
                raise ValueError(envelope["error"])
            # Do not expose arbitrary remote stdout/stderr or auth diagnostics.
            raise ValueError("Remote Codex live quotas unavailable. Check Codex installation, connectivity and ChatGPT sign-in on the selected host.")
        payload = envelope["result"]
    else:
        payload = codex_usage.read_rate_limits(account.get("codex_home"), timeout=runtime.USAGE_TIMEOUT - 2)
    return normalize_codex_api(payload)


def read_agy_usage(runtime, account, server=None):
    """Read Agy's built-in /usage command without starting a model turn."""
    timeout = max(1, runtime.USAGE_TIMEOUT - 4)
    argv = ["agy", "--print", "/usage", "--output-format", "json",
            "--print-timeout", f"{timeout}s"]
    if not server and not shutil.which("agy"):
        # Retain the helper for installations predating Agy's native command.
        argv = provider_default_command("agy") or []
        if not argv:
            raise ValueError("Agy CLI not found. Install Agy and sign in, or configure a JSON usage command.")
    if server:
        # SSH's non-interactive PATH commonly omits ~/.local/bin, where Agy's
        # installer places its launcher.  A shell also keeps this one command
        # intact instead of accidentally making `env` execute its first word.
        remote_script = 'export PATH="$HOME/.local/bin:$PATH"; exec ' + shlex.join(argv)
        result = run_ssh_command(server["ssh_host"], shlex.join(["sh", "-lc", remote_script]),
                                 timeout=runtime.USAGE_TIMEOUT, connect_timeout=5)
    else:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=runtime.USAGE_TIMEOUT,
                                check=False)
    if result.returncode:
        raise ValueError("Agy live quota query failed. Check that Agy is installed and signed in.")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise ValueError("Agy live quota query returned invalid JSON.") from None
    from .usage import normalize_usage_payload
    return normalize_usage_payload(payload)


def normalize_codex_api(payload):
    """Prefer the multi-bucket API view, retaining exact server percentages."""
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = payload.get("rateLimits")
        buckets = {"codex": legacy} if isinstance(legacy, dict) else {}
    result = {"source": "Codex live API", "stale": False, "quotas": [], "balances": []}
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        label = str(bucket.get("limitName") or bucket_id)
        for key in ("primary", "secondary"):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            used = safe_number(window.get("usedPercent"))
            if used is None or not 0 <= used <= 100:
                continue
            minutes = window.get("windowDurationMins")
            result["quotas"].append({
                "name": f"{label} · {_window_label(key, {'window_minutes': minutes})}",
                "used": used, "limit": 100, "remaining": 100 - used, "unit": "%",
                "reset_at": safe_number(window.get("resetsAt")),
            })
        if bucket_id == "codex" or len(buckets) == 1:
            result["plan_type"] = bucket.get("planType")
            credits = bucket.get("credits") or {}
            balance = safe_number(credits.get("balance"))
            if balance is not None:
                result["balances"] = [{"remaining": balance, "currency": "credits"}]
            result["credits_unlimited"] = credits.get("unlimited") is True
    resets = payload.get("rateLimitResetCredits")
    if isinstance(resets, dict):
        count = safe_number(resets.get("availableCount"))
        if count is not None and count >= 0:
            result["available_resets"] = int(count)
    if not result["quotas"] and not result["balances"] and not result.get("credits_unlimited"):
        raise ValueError("Codex returned no subscription quota data for this account.")
    return result

def read_local_transcript_usage(runtime, account):
    """Read local, non-secret usage records emitted by Codex or Claude Code."""
    provider = account.get("provider")
    root = local_usage_path(account, provider)
    paths = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    if not paths:
        raise ValueError(f"no {provider} usage records found at {root}")
    paths = sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)[:500]
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    latest_limits, latest_limit_mtime = None, -1
    latest_observed_at = None
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                last_usage, last_limits, last_observed_at = None, None, None
                for line in stream:
                    try:
                        obj = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    payload = obj.get("payload", obj) if isinstance(obj, dict) else {}
                    if provider == "codex":
                        usage = payload.get("thread_token_usage") or payload.get("usage")
                        limits = payload.get("rate_limits")
                    else:
                        message = payload.get("message", payload) if isinstance(payload, dict) else {}
                        usage = message.get("usage") if isinstance(message, dict) else None
                        limits = None
                    if isinstance(usage, dict):
                        last_usage = usage
                    if isinstance(limits, dict):
                        last_limits = limits
                        last_observed_at = obj.get("timestamp")
                if provider == "codex" and isinstance(last_usage, dict):
                    for key, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                                        ("cached_input_tokens", "cache_read"), ("cache_write_input_tokens", "cache_write")):
                        totals[target] += safe_number(last_usage.get(key)) or 0
                elif provider == "claude" and isinstance(last_usage, dict):
                    for key, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                                        ("cache_read_input_tokens", "cache_read"), ("cache_creation_input_tokens", "cache_write")):
                        totals[target] += safe_number(last_usage.get(key)) or 0
                mtime = os.path.getmtime(path)
                if last_limits and mtime > latest_limit_mtime:
                    latest_limits, latest_limit_mtime = last_limits, mtime
                    latest_observed_at = last_observed_at or datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat()
        except (OSError, UnicodeError):
            continue
    result = {"source": f"{provider} local records", "quotas": [{"name": "tokens observed",
              "used": sum(totals.values()), "unit": "tokens"}]}
    if provider == "codex" and latest_limits:
        result.update(_codex_rate_limit_result(latest_limits))
    if provider == "codex":
        result.update(stale=True, observed_at=latest_observed_at,
                      message="Historical Codex records; current allowance is unverified.")
    return result


def _remote_path_expression(root):
    """Return a safely quoted remote path while preserving a leading ~."""
    root = str(root)
    if root == "~":
        return '"$HOME"'
    if root.startswith("~/"):
        return '"$HOME"' + shlex.quote(root[1:])
    return shlex.quote(root)


def read_remote_transcript_usage(runtime, account, ssh_host):
    """Read Codex/Claude JSONL records from a selected SSH server."""
    provider = account.get("provider")
    root = str(account.get("local_path") or ("~/.codex/sessions" if provider == "codex" else "~/.claude/projects"))
    if provider == "codex" and account.get("codex_home") and not account.get("local_path"):
        root = str(account["codex_home"]).rstrip("/") + "/sessions"
    root_expr = _remote_path_expression(root)
    if provider == "codex" and not account.get("local_path") and not account.get("codex_home"):
        root_expr = '"${CODEX_HOME:-$HOME/.codex}/sessions"'
    # Keep parsing local, but stream only the JSONL records over SSH. The
    # marker lets us safely separate files without depending on Python or
    # GNU-only utilities on the remote host.
    remote_cmd = (
        f"find {root_expr} -type f -name '*.jsonl' -print 2>/dev/null | head -500 | "
        "while IFS= read -r file; do "
        "printf '\\nDEVBOOST_USAGE_FILE:%s\\n' \"$file\"; cat -- \"$file\" 2>/dev/null; "
        "done"
    )
    result = run_ssh_command(ssh_host, remote_cmd, timeout=runtime.USAGE_TIMEOUT, connect_timeout=5)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "remote usage query failed").strip()[:400])
    if "DEVBOOST_USAGE_FILE:" not in result.stdout:
        raise ValueError(f"no {provider} usage records found at {root} on {ssh_host}")

    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    latest_limits = None
    current_usage = current_limits = None
    found = False
    for line in result.stdout.splitlines():
        if line.startswith("DEVBOOST_USAGE_FILE:"):
            if current_usage:
                _add_transcript_usage(totals, provider, current_usage)
            if current_limits:
                latest_limits = current_limits
            current_usage, current_limits = None, None
            found = True
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        payload = obj.get("payload", obj) if isinstance(obj, dict) else {}
        if provider == "codex":
            usage = payload.get("thread_token_usage") or payload.get("usage")
            limits = payload.get("rate_limits")
        else:
            message = payload.get("message", payload) if isinstance(payload, dict) else {}
            usage = message.get("usage") if isinstance(message, dict) else None
            limits = None
        if isinstance(usage, dict):
            current_usage = usage
        if isinstance(limits, dict):
            current_limits = limits
    if current_usage:
        _add_transcript_usage(totals, provider, current_usage)
    if current_limits:
        latest_limits = current_limits
    if not found:
        raise ValueError(f"no {provider} usage records found at {root} on {ssh_host}")
    result = {"source": f"{provider} remote records ({ssh_host})",
              "quotas": [{"name": "tokens observed", "used": sum(totals.values()), "unit": "tokens"}]}
    if provider == "codex" and latest_limits:
        result.update(_codex_rate_limit_result(latest_limits))
    if provider == "codex":
        result.update(stale=True, message="Historical Codex records; current allowance is unverified.")
    return result


def _add_transcript_usage(totals, provider, usage):
    keys = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
            ("cached_input_tokens", "cache_read"), ("cache_write_input_tokens", "cache_write"))
    if provider == "claude":
        keys = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                ("cache_read_input_tokens", "cache_read"),
                ("cache_creation_input_tokens", "cache_write"))
    for key, target in keys:
        totals[target] += safe_number(usage.get(key)) or 0

def read_usage_command(runtime, account, server=None):
    command = account.get("usage_command") or account.get("command") or provider_default_command(account.get("provider"))
    if server and not command:
        command = {"opencode": ["opencode", "stats"], "agy": ["agy-quota", "--json"]}.get(account.get("provider"))
    if not command:
        raise ValueError(f"{account.get('provider', 'provider')} has no safe machine-readable usage adapter; configure a JSON command or endpoint")
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv or len(argv) > 32:
        raise ValueError("usage command is invalid")
    env = os.environ.copy()
    for key, source in (account.get("env") or {}).items():
        env[str(key)] = os.environ.get(str(source), "")
    if server:
        assignments = []
        for key, source in (account.get("env") or {}).items():
            value = os.environ.get(str(source), "")
            assignments.append(f"{shlex.quote(str(key))}={shlex.quote(value)}")
        # `env` requires an assignment or option before the command.  Prefixing
        # a command with a bare `env` makes some remote shells report the
        # misleading "env: <command>: No such file" error.
        remote_command = " ".join((["env"] + assignments if assignments else []) + [shlex.join(argv)])
        if account.get("provider") == "opencode":
            # OpenCode's installer uses ~/.opencode/bin and adds it from the
            # interactive shell profile. SSH runs a non-interactive shell, so
            # the default `opencode stats` command otherwise becomes
            # "opencode: command not found" even when OpenCode is installed.
            remote_script = (
                'export PATH="$HOME/.opencode/bin:$HOME/.local/bin:'
                '/opt/homebrew/bin:/usr/local/bin:$PATH"; exec ' + remote_command
            )
            remote_command = shlex.join(["sh", "-lc", remote_script])
        result = run_ssh_command(server["ssh_host"], remote_command,
                                 timeout=runtime.USAGE_TIMEOUT, connect_timeout=5)
    else:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=runtime.USAGE_TIMEOUT, env=env, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "command failed").strip()[:400])
    output = result.stdout if result.stdout.strip() else result.stderr
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        if account.get("provider") == "opencode":
            return parse_opencode_stats(output)
        raise ValueError("usage command must print JSON") from exc

def read_usage_http(runtime, account):
    url = str(account.get("balance_url") or account.get("usage_url") or "").strip()
    if not url or not (url.startswith("https://") or url.startswith("http://localhost") or url.startswith("http://127.0.0.1")):
        raise ValueError("balance/usage URL must use HTTPS (or localhost)")
    headers = {"Accept": "application/json"}
    token_env = account.get("token_env") or account.get("api_key_env")
    token = os.environ.get(str(token_env), "") if token_env else ""
    if token:
        auth_header = account.get("auth_header") or ("x-api-key" if account.get("provider") == "claude" else "Authorization")
        headers[str(auth_header)] = token if auth_header.lower() == "x-api-key" else "Bearer " + token
    if account.get("provider") == "claude":
        headers.setdefault("anthropic-version", "2023-06-01")
    if account.get("organization"):
        headers["OpenAI-Organization"] = str(account["organization"])
    if account.get("project"):
        headers["OpenAI-Project"] = str(account["project"])
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=runtime.USAGE_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
        response_headers = getattr(response, "headers", {})
    header_quotas = []
    # OpenAI-compatible and Anthropic-compatible APIs expose remaining limits
    # in response headers even when the JSON body is a usage report.
    for prefix, label in (("x-ratelimit", "rate limit"), ("anthropic-ratelimit-unified", "unified rate limit")):
        for key, value in response_headers.items() if hasattr(response_headers, "items") else ():
            lowered = key.lower()
            if not lowered.startswith(prefix) or "remaining" not in lowered:
                continue
            remaining = safe_number(value)
            if remaining is None:
                continue
            suffix = lowered.rsplit("-", 1)[-1]
            limit = safe_number(response_headers.get(key.replace("remaining", "limit"))) if hasattr(response_headers, "get") else None
            reset = response_headers.get(key.replace("remaining", "reset")) if hasattr(response_headers, "get") else None
            header_quotas.append({"name": f"{label} {suffix}", "remaining": remaining,
                                  "limit": limit, "unit": "requests", "reset_at": reset})
    if header_quotas:
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["quotas"] = list(payload.get("quotas") or []) + header_quotas
        else:
            payload = {"quotas": header_quotas, "response": payload}
    return payload

def read_provider_api(runtime, account):
    """Read documented organization usage reports for API-backed accounts."""
    provider = account.get("provider")
    days = max(1, min(30, int(account.get("api_days", 1))))
    now = int(time.time())
    if provider == "codex":
        query = urllib.parse.urlencode({"start_time": now - days * 86400, "limit": 1})
        url = "https://api.openai.com/v1/organization/usage/completions?" + query
    elif provider == "claude":
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=days)
        query = urllib.parse.urlencode({"starting_at": start.isoformat().replace("+00:00", "Z"),
                                        "ending_at": end.isoformat().replace("+00:00", "Z"), "limit": 100})
        url = "https://api.anthropic.com/v1/organizations/usage_report/messages?" + query
    else:
        raise ValueError("provider API mode is supported for codex and claude only")
    api_account = dict(account, balance_url=url)
    return read_usage_http(runtime, api_account)
