"""Usage-account persistence and snapshot orchestration."""

import datetime
import re
import time
import types
from concurrent.futures import ThreadPoolExecutor


class _RuntimeGlobals(dict):
    def __init__(self, runtime, base):
        super().__init__(base)
        self._runtime = runtime

    def __missing__(self, key):
        try:
            return getattr(self._runtime, key)
        except AttributeError as exc:
            raise KeyError(key) from exc


_FUNCTIONS = ["get_usage_accounts","refresh_usage_account","get_usage_status","save_usage_account","remove_usage_account","reorder_usage_accounts"]


def get_usage_accounts(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return [{"id": a.get("id"), "provider": a.get("provider"), "name": a.get("name"),
             "enabled": bool(a.get("enabled", True)),
             "has_command": bool(a.get("usage_command") or a.get("command") or _provider_default_command(a.get("provider")) or a.get("provider") in ("codex", "claude")),
             "has_balance_url": bool(a.get("balance_url") or a.get("usage_url")),
             # These are configuration metadata, not credentials, and are
             # returned so an existing account can be edited in the dashboard.
             "usage_command": a.get("usage_command") or a.get("command"),
             "balance_url": a.get("balance_url") or a.get("usage_url"),
             "token_env": a.get("token_env") or a.get("api_key_env"),
             "local_path": a.get("local_path"),
             "codex_home": a.get("codex_home"),
             "organization": a.get("organization"), "project": a.get("project"),
             "api_mode": bool(a.get("api_mode", False)), "api_days": a.get("api_days", 1)}
            for a in cfg.get("usage_accounts", []) if isinstance(a, dict)]

def refresh_usage_account(account, server=None):
    started = time.time()
    try:
        explicit_url = account.get("balance_url") or account.get("usage_url")
        explicit_command = account.get("usage_command") or account.get("command")
        remote_default = server and account.get("provider") in ("agy", "opencode")
        has_command = explicit_command or _provider_default_command(account.get("provider")) or remote_default
        # An account with neither source still goes through the command
        # adapter so the user receives a provider-specific actionable error.
        if account.get("api_mode"):
            payload = _read_provider_api(account)
        elif account.get("provider") == "codex" and not explicit_url and not explicit_command and not account.get("local_path"):
            try:
                payload = _read_codex_upstream(account)
            except Exception as https_error:
                try:
                    if server and not account.get("local_path"):
                        payload = _read_codex_api(account, server=server)
                    else:
                        payload = (_read_remote_transcript_usage(account, server["ssh_host"]) if server
                                   else _read_local_transcript_usage(account))
                except Exception as local_error:
                    # A configured local path should retain the actionable
                    # existing error when neither source can answer.
                    if account.get("local_path"):
                        raise local_error
                    try:
                        payload = (_read_remote_transcript_usage(account, server["ssh_host"]) if server
                                   else _read_local_transcript_usage(account))
                    except Exception:
                        raise https_error
                payload.update(stale=True, message=f"{https_error} Showing local usage.")
        elif explicit_url and not has_command:
            payload = _read_usage_http(account)
        elif explicit_command:
            payload = _read_usage_command(account, server=server)
        elif account.get("provider") == "agy":
            payload = _read_agy_usage(account, server=server)
        elif _provider_default_command(account.get("provider")):
            payload = _read_usage_command(account, server=server)
        elif account.get("provider") == "claude":
            if server:
                payload = _read_remote_transcript_usage(account, server["ssh_host"])
            else:
                payload = _read_local_transcript_usage(account)
        elif account.get("provider") == "codex":
            if server:
                payload = _read_remote_transcript_usage(account, server["ssh_host"])
            else:
                payload = _read_local_transcript_usage(account)
        else:
            payload = _read_usage_command(account, server=server)
        result = {"ok": True, **_normalize_usage_payload(payload)}
        if account.get("provider") == "codex":
            for key in ("stale", "message", "observed_at", "plan_type", "credits_unlimited", "available_resets"):
                if key in payload:
                    result[key] = payload[key]
    except Exception as exc:
        result = {"ok": False, "quotas": [], "balances": [], "message": str(exc)[:400]}
    result["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if result.get("ok") and not result.get("stale"):
        # Keep this distinct from updated_at: a failed refresh is not a valid
        # usage query and must not make the data look freshly verified.
        result["last_valid_query_at"] = result["updated_at"]
    result["duration_ms"] = int((time.time() - started) * 1000)
    return result

def get_usage_status(refresh=False, account_id=None, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref) if server_ref else None
    server_id = server.get("id") if server else "local"
    accounts = [a for a in cfg.get("usage_accounts", []) if isinstance(a, dict) and a.get("enabled", True)]
    if account_id:
        accounts = [a for a in accounts if a.get("id") == account_id]
    snapshots = cfg.setdefault("usage_snapshots", {})
    def snapshot_key(account):
        return account.get("id") if not server else f"{server_id}:{account.get('id')}"
    def live_quota_due(account):
        if account.get("provider") not in ("codex", "agy"):
            return False
        try:
            checked = datetime.datetime.fromisoformat(snapshots.get(snapshot_key(account), {}).get("updated_at", ""))
            return time.time() - checked.timestamp() >= 60
        except (ValueError, TypeError):
            return True
    due = [a for a in accounts if refresh or snapshot_key(a) not in snapshots or live_quota_due(a)]
    changed = bool(due)
    if due:
        # A broken/slow provider must not serialize all other accounts.
        with ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
            results = pool.map(lambda account: refresh_usage_account(account, server=server), due)
            for account, result in zip(due, results):
                key = snapshot_key(account)
                previous = snapshots.get(key, {})
                if account.get("provider") == "agy" and (not result.get("ok") or result.get("stale")):
                    if previous.get("source") == "Agy live usage":
                        result.update({field: previous[field] for field in ("quotas", "balances", "source") if field in previous})
                        result["stale"] = True
                        result["message"] = result.get("message", "Live query failed.") + " Showing last successful Agy values."
                    if previous.get("last_valid_query_at"):
                        result["last_valid_query_at"] = previous["last_valid_query_at"]
                if not result.get("ok") and previous.get("last_valid_query_at"):
                    result["last_valid_query_at"] = previous["last_valid_query_at"]
                snapshots[key] = result
    if changed:
        save_config(cfg)
    account_rows = get_usage_accounts(cfg)
    enabled_ids = {a.get("id") for a in accounts}
    account_rows = [a for a in account_rows if a.get("id") in enabled_ids]
    return {"accounts": account_rows, "server_id": server_id,
            "server_host": server.get("ssh_host") if server else None,
            "snapshots": {a.get("id"): snapshots.get(snapshot_key(a), {}) for a in accounts}}

def save_usage_account(data):
    provider, name = str(data.get("provider") or "").strip().lower(), str(data.get("name") or "").strip()
    if provider not in USAGE_PROVIDERS and provider != "custom":
        raise ValueError("provider must be codex, agy, claude, opencode, or custom")
    if not name:
        raise ValueError("account name is required")
    cfg, accounts = load_config(), None
    accounts = cfg.setdefault("usage_accounts", [])
    requested_id = str(data.get("id") or "").strip()
    existing = next((a for a in accounts if a.get("id") == requested_id), None) if requested_id else None
    if requested_id and existing is None:
        raise ValueError("usage account not found")
    aid = requested_id or _usage_account_id(provider, name, [a.get("id") for a in accounts])
    allowed = ("id", "provider", "name", "enabled", "usage_command", "balance_url", "token_env", "env", "local_path", "codex_home", "auth_header", "organization", "project", "api_mode", "api_days")
    account = dict(existing) if isinstance(existing, dict) else {}
    account.update({key: data[key] for key in allowed if key in data})
    # The canonical fields supersede legacy aliases when an account is edited.
    if "usage_command" in data:
        account.pop("command", None)
    if "balance_url" in data:
        account.pop("usage_url", None)
    enabled = data["enabled"] if "enabled" in data else account.get("enabled", True)
    account.update({"id": aid, "provider": provider, "name": name, "enabled": bool(enabled)})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", aid):
        raise ValueError("account id may contain only letters, numbers, dot, underscore, and hyphen")
    if existing is None:
        accounts.append(account)
    else:
        # Editing an account should not unexpectedly change the user's row
        # order on the quotas page.
        existing_index = next(index for index, item in enumerate(accounts) if item.get("id") == aid)
        accounts[existing_index] = account
    cfg.setdefault("usage_snapshots", {}).pop(aid, None)
    if provider == "codex" or (existing and existing.get("provider") == "codex"):
        # A profile/source edit must not reuse another profile's remote values.
        for key in list(cfg["usage_snapshots"]):
            if key.endswith(":" + aid):
                cfg["usage_snapshots"].pop(key, None)
    save_config(cfg)
    return next(a for a in get_usage_accounts(cfg) if a["id"] == aid)

def remove_usage_account(account_id):
    cfg = load_config()
    before = len(cfg.get("usage_accounts", []))
    cfg["usage_accounts"] = [a for a in cfg.get("usage_accounts", []) if a.get("id") != account_id]
    cfg.get("usage_snapshots", {}).pop(account_id, None)
    if len(cfg["usage_accounts"]) == before:
        return False
    save_config(cfg)
    return True


def reorder_usage_accounts(order_ids):
    """Persist the visible quotas-row order while retaining unknown accounts."""
    cfg = load_config()
    accounts = [account for account in cfg.get("usage_accounts", []) if isinstance(account, dict)]
    by_id = {str(account.get("id")): account for account in accounts if account.get("id") is not None}
    requested = []
    seen = set()
    for account_id in order_ids if isinstance(order_ids, list) else []:
        account_id = str(account_id)
        if account_id in by_id and account_id not in seen:
            requested.append(account_id)
            seen.add(account_id)
    remaining = [account for account in accounts if str(account.get("id")) not in seen]
    cfg["usage_accounts"] = [by_id[account_id] for account_id in requested] + remaining
    save_config(cfg)
    return get_usage_accounts(cfg)


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
