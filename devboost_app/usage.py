"""Pure helpers for normalizing provider usage data."""

import re
import shutil


def provider_default_command(provider):
    """Return a safe, read-only local adapter when the installed CLI supports one."""
    if provider == "opencode" and shutil.which("opencode"):
        return ["opencode", "stats"]
    # agy-quota is the JSON quota helper used with the agy/Antigravity CLI.
    if provider == "agy" and shutil.which("agy-quota"):
        return ["agy-quota", "--json"]
    return None

def usage_account_id(provider, name, existing=None):
    base = re.sub(r"[^a-z0-9]+", "-", f"{provider}-{name}".lower()).strip("-") or provider
    candidate, index = base, 2
    existing = set(existing or [])
    while candidate in existing:
        candidate, index = f"{base}-{index}", index + 1
    return candidate

def safe_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def first(data, *keys):
    if isinstance(data, dict):
        for key in keys:
            if data.get(key) is not None:
                return data[key]
    return None

def normalize_usage_payload(payload):
    """Normalize common CLI/API response shapes for the dashboard and menu bar."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        # OpenAI and Anthropic admin reports return paginated time buckets.
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
        for bucket in payload["data"]:
            for row in (bucket.get("results", []) if isinstance(bucket, dict) else []):
                if not isinstance(row, dict):
                    continue
                for keys, target in ((("input_tokens", "uncached_input_tokens"), "input_tokens"),
                                     (("output_tokens",), "output_tokens"),
                                     (("input_cached_tokens", "cache_read_input_tokens"), "cache_read"),
                                     (("cache_creation_input_tokens",), "cache_write")):
                    for key in keys:
                        value = _safe_number(row.get(key))
                        if value is not None:
                            totals[target] += value
                            break
        return {"quotas": [{"name": "API tokens used", "used": sum(totals.values()), "unit": "tokens"}],
                "balances": [], "source": "provider admin usage API"}
    if isinstance(payload, dict) and any(key in payload for key in
                                        ("remaining_fraction", "remaining_percent", "models", "providers")):
        # agy-quota reports model and provider pools as percentages rather than
        # the generic used/limit/remaining shape.
        def remaining_percent(raw):
            percent = _safe_number(_first(raw, "remaining_percent", "pool_remaining_percent"))
            if percent is not None:
                return percent
            fraction = _safe_number(_first(raw, "remaining_fraction", "pool_remaining_fraction"))
            return fraction * 100 if fraction is not None else None

        quotas = []
        for model in payload.get("models", []):
            if not isinstance(model, dict):
                continue
            remaining = remaining_percent(model)
            if remaining is None:
                continue
            name = _first(model, "model", "model_id", "name", "token_type") or "model quota"
            quotas.append({"name": str(name), "used": 100 - remaining, "limit": 100,
                           "remaining": remaining, "unit": "%",
                           "reset_at": _first(model, "reset_time", "reset_at", "resets_at")})
        for provider in payload.get("providers", []):
            if not isinstance(provider, dict):
                continue
            remaining = remaining_percent(provider)
            if remaining is None:
                continue
            name = _first(provider, "provider", "name", "id") or "provider pool"
            quotas.append({"name": f"{name} pool", "used": 100 - remaining, "limit": 100,
                           "remaining": remaining, "unit": "%",
                           "reset_at": _first(provider, "reset_time", "reset_at", "resets_at")})
        if not quotas:
            remaining = remaining_percent(payload)
            if remaining is not None:
                quotas.append({"name": "Agy quota", "used": 100 - remaining, "limit": 100,
                               "remaining": remaining, "unit": "%",
                               "reset_at": _first(payload, "reset_time", "reset_at", "resets_at")})
        result = {"quotas": quotas, "balances": [], "source": str(payload.get("source") or "agy-quota")}
        return result
    if isinstance(payload, dict):
        quotas, balances = _first(payload, "quotas", "limits", "usage", "rate_limits"), _first(payload, "balances", "balance", "credits", "credits_remaining", "available_credits", "remaining_credits")
        if balances is None:
            granted = _safe_number(_first(payload, "total_granted", "credits_granted"))
            spent = _safe_number(_first(payload, "total_used", "credits_used", "amount_used"))
            if granted is not None and spent is not None:
                balances = [{"remaining": granted - spent, "spent": spent}]
    else:
        quotas, balances = payload, None
    if isinstance(quotas, dict):
        quotas = [dict({"name": k}, **(v if isinstance(v, dict) else {"remaining": v})) for k, v in quotas.items()]
    if not isinstance(quotas, list):
        quotas = []
    if isinstance(balances, (int, float, str)):
        balances = [{"remaining": balances}]
    elif isinstance(balances, dict):
        balances = [balances]
    if not isinstance(balances, list):
        balances = []
    normalized = []
    for raw in quotas:
        if not isinstance(raw, dict):
            continue
        used, limit, remaining = (_safe_number(_first(raw, "used", "consumed", "usage")),
                                  _safe_number(_first(raw, "limit", "max", "total")),
                                  _safe_number(_first(raw, "remaining", "left", "available")))
        if remaining is None and limit is not None and used is not None:
            remaining = limit - used
        normalized.append({"name": str(_first(raw, "name", "window", "period") or "quota"), "used": used,
                           "limit": limit, "remaining": remaining,
                           "unit": str(_first(raw, "unit", "units") or "requests"),
                           "reset_at": _first(raw, "reset_at", "resets_at", "reset")})
    clean_balances = []
    for raw in balances:
        if not isinstance(raw, dict):
            raw = {"remaining": raw}
        clean_balances.append({"remaining": _safe_number(_first(raw, "remaining", "left", "balance", "available", "available_credits", "remaining_credits")),
                               "currency": str(_first(raw, "currency", "unit") or "USD"),
                               "spent": _safe_number(_first(raw, "spent", "used"))})
    result = {"quotas": normalized, "balances": clean_balances}
    if isinstance(payload, dict) and payload.get("source"):
        result["source"] = str(payload["source"])
    return result

def parse_opencode_stats(text):
    """Parse the table emitted by current `opencode stats` versions."""
    def amount(label):
        match = re.search(rf"(?:^|│)\s*{re.escape(label)}\s+([0-9]+(?:\.[0-9]+)?\s*[KMG]?)", text, re.I | re.M)
        if not match:
            return None
        raw = match.group(1).replace(" ", "").upper()
        multiplier = {"K": 1000, "M": 1000000, "G": 1000000000}.get(raw[-1], 1)
        return float(raw[:-1]) * multiplier if raw[-1:] in "KMG" else float(raw)
    cost_match = re.search(r"(?:^|│)\s*Total Cost\s+\$?([0-9]+(?:\.[0-9]+)?)", text, re.I | re.M)
    cost = float(cost_match.group(1)) if cost_match else None
    input_tokens, output_tokens = amount("Input"), amount("Output")
    quotas = []
    if input_tokens is not None or output_tokens is not None:
        quotas.append({"name": "tokens used", "used": (input_tokens or 0) + (output_tokens or 0), "unit": "tokens"})
    result = {"quotas": quotas, "source": "opencode stats"}
    if cost is not None:
        result["balances"] = [{"spent": cost, "currency": "USD"}]
    return result


# Internal aliases preserve the extracted normalizer's readable helper calls.
_safe_number = safe_number
_first = first
