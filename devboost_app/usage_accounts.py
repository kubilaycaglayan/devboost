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


_FUNCTIONS = ["get_usage_accounts","refresh_usage_account","get_usage_status","save_usage_account","remove_usage_account"]


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
             "organization": a.get("organization"), "project": a.get("project"),
             "api_mode": bool(a.get("api_mode", False)), "api_days": a.get("api_days", 1)}
            for a in cfg.get("usage_accounts", []) if isinstance(a, dict)]

def refresh_usage_account(account):
    started = time.time()
    try:
        explicit_url = account.get("balance_url") or account.get("usage_url")
        explicit_command = account.get("usage_command") or account.get("command")
        has_command = explicit_command or _provider_default_command(account.get("provider"))
        # An account with neither source still goes through the command
        # adapter so the user receives a provider-specific actionable error.
        if account.get("api_mode"):
            payload = _read_provider_api(account)
        elif explicit_url and not has_command:
            payload = _read_usage_http(account)
        elif explicit_command or _provider_default_command(account.get("provider")):
            payload = _read_usage_command(account)
        elif account.get("provider") in ("codex", "claude"):
            payload = _read_local_transcript_usage(account)
        else:
            payload = _read_usage_command(account)
        result = {"ok": True, **_normalize_usage_payload(payload)}
    except Exception as exc:
        result = {"ok": False, "quotas": [], "balances": [], "message": str(exc)[:400]}
    result["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result["duration_ms"] = int((time.time() - started) * 1000)
    return result

def get_usage_status(refresh=False, account_id=None):
    cfg = load_config()
    accounts = [a for a in cfg.get("usage_accounts", []) if isinstance(a, dict) and a.get("enabled", True)]
    if account_id:
        accounts = [a for a in accounts if a.get("id") == account_id]
    snapshots = cfg.setdefault("usage_snapshots", {})
    due = [a for a in accounts if refresh or a.get("id") not in snapshots]
    changed = bool(due)
    if due:
        # A broken/slow provider must not serialize all other accounts.
        with ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
            results = pool.map(refresh_usage_account, due)
            for account, result in zip(due, results):
                snapshots[account.get("id")] = result
    if changed:
        save_config(cfg)
    return {"accounts": get_usage_accounts(cfg), "snapshots": {a.get("id"): snapshots.get(a.get("id"), {}) for a in accounts}}

def save_usage_account(data):
    provider, name = str(data.get("provider") or "").strip().lower(), str(data.get("name") or "").strip()
    if provider not in USAGE_PROVIDERS and provider != "custom":
        raise ValueError("provider must be codex, agy, claude, opencode, or custom")
    if not name:
        raise ValueError("account name is required")
    cfg, accounts = load_config(), None
    accounts = cfg.setdefault("usage_accounts", [])
    aid = str(data.get("id") or _usage_account_id(provider, name, [a.get("id") for a in accounts]))
    allowed = ("id", "provider", "name", "enabled", "usage_command", "balance_url", "token_env", "env", "local_path", "auth_header", "organization", "project", "api_mode", "api_days")
    existing = next((a for a in accounts if a.get("id") == aid), None)
    account = dict(existing) if isinstance(existing, dict) else {}
    account.update({key: data[key] for key in allowed if key in data})
    # The canonical fields supersede legacy aliases when an account is edited.
    if "usage_command" in data:
        account.pop("command", None)
    if "balance_url" in data:
        account.pop("usage_url", None)
    account.update({"id": aid, "provider": provider, "name": name, "enabled": bool(data.get("enabled", True))})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", aid):
        raise ValueError("account id may contain only letters, numbers, dot, underscore, and hyphen")
    accounts[:] = [a for a in accounts if a.get("id") != aid]
    accounts.append(account)
    cfg.setdefault("usage_snapshots", {}).pop(aid, None)
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

