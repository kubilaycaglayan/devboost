"""Provider usage adapters: local records, commands, HTTPS, and admin APIs."""

import datetime
import glob
import json
import os
import shlex
import subprocess
import time
import urllib.parse
import urllib.request

from .usage import parse_opencode_stats, provider_default_command, safe_number


def local_usage_path(account, provider):
    default = "~/.codex/sessions" if provider == "codex" else "~/.claude/projects"
    return os.path.expanduser(str(account.get("local_path") or default))

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
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                last_usage, last_limits = None, None
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
        except (OSError, UnicodeError):
            continue
    result = {"source": f"{provider} local records", "quotas": [{"name": "tokens observed",
              "used": sum(totals.values()), "unit": "tokens"}]}
    if provider == "codex" and latest_limits:
        result["quotas"] = []
        for key, label in (("primary", "primary window"), ("secondary", "secondary window")):
            window = latest_limits.get(key) or {}
            used = safe_number(window.get("used_percent"))
            if used is not None:
                result["quotas"].append({"name": label, "used": used, "limit": 100,
                                         "remaining": max(0, 100 - used), "unit": "%",
                                         "reset_at": window.get("resets_at"),
                                         "window_minutes": window.get("window_minutes")})
        credits = latest_limits.get("credits") or {}
        balance = safe_number(credits.get("balance"))
        if balance is not None:
            result["balances"] = [{"remaining": balance, "currency": "credits"}]
        result["plan_type"] = latest_limits.get("plan_type")
    return result

def read_usage_command(runtime, account):
    command = account.get("usage_command") or account.get("command") or provider_default_command(account.get("provider"))
    if not command:
        raise ValueError(f"{account.get('provider', 'provider')} has no safe machine-readable usage adapter; configure a JSON command or endpoint")
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv or len(argv) > 32:
        raise ValueError("usage command is invalid")
    env = os.environ.copy()
    for key, source in (account.get("env") or {}).items():
        env[str(key)] = os.environ.get(str(source), "")
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

