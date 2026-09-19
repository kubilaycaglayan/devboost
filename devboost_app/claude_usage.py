"""Read Claude Code's subscription usage without starting a model turn."""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"


def detect_claude():
    """Return the Claude Code executable when it is installed on this host."""
    executable = shutil.which("claude")
    if executable:
        return executable
    for candidate in (
        "~/.local/bin/claude", "~/.claude/local/claude",
        "/usr/local/bin/claude", "/opt/homebrew/bin/claude",
        "/Applications/Claude.app/Contents/Resources/claude",
    ):
        candidate = os.path.expanduser(candidate)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _credentials_path(claude_home=None):
    home = os.path.expanduser(str(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    return os.path.join(home, ".credentials.json")


def _read_file_token(claude_home=None):
    try:
        with open(_credentials_path(claude_home), encoding="utf-8") as stream:
            payload = json.load(stream)
        oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        return token if isinstance(token, str) and token else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _read_keychain_token():
    """Read Claude Code's macOS credential item, if available.

    Claude Code stores OAuth credentials in Keychain on macOS.  The value is
    only used in this process and is never included in a result or exception.
    """
    if shutil.which("security") is None:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode:
            return None
        payload = json.loads(result.stdout)
        oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        return token if isinstance(token, str) and token else None
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError):
        return None


def read_access_token(claude_home=None):
    return _read_file_token(claude_home) or _read_keychain_token()


def read_usage(claude_home=None, timeout=15):
    """Return Claude's server-authoritative subscription usage windows."""
    if not detect_claude():
        raise ValueError("Claude Code client was not found on this host.")
    token = read_access_token(claude_home)
    if not token:
        raise ValueError("Claude Code is not signed in on this host.")
    request = urllib.request.Request(USAGE_URL, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": "claude-code/DevBoost",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("Claude Code usage query was not authorized. Sign in to Claude Code on this host.") from None
        raise ValueError(f"Claude Code usage query failed (HTTP {exc.code}).") from None
    except (OSError, TimeoutError):
        raise ValueError("Claude Code usage query failed. Check connectivity and Claude sign-in on this host.") from None
    except (UnicodeError, ValueError):
        raise ValueError("Claude Code usage query returned invalid data.") from None
    if not isinstance(payload, dict):
        raise ValueError("Claude Code usage query returned invalid data.")
    return payload


def normalize_usage(payload, now=None):
    """Map the OAuth response to DevBoost's percentage quota structure."""
    if not isinstance(payload, dict):
        raise ValueError("Claude Code returned invalid quota data.")
    quotas = []
    for key, label in (("five_hour", "Current session (5-hour window)"),
                       ("seven_day", "Current week (all models)"),
                       ("seven_day_sonnet", "Current week (Sonnet)"),
                       ("seven_day_opus", "Current week (Opus)")):
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        try:
            used = float(window.get("utilization"))
        except (TypeError, ValueError):
            continue
        if not 0 <= used <= 100:
            continue
        quotas.append({"name": label, "used": used, "limit": 100,
                       "remaining": max(0, 100 - used), "unit": "%",
                       "reset_at": window.get("resets_at")})
    if not quotas:
        raise ValueError("Claude Code returned no subscription quota data.")
    return {"source": "[HTTPS] Claude Code subscription", "stale": False,
            "quotas": quotas, "balances": [],
            "plan_type": payload.get("subscription_type")}


if __name__ == "__main__":
    import sys
    try:
        home = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
        print(json.dumps({"result": normalize_usage(read_usage(home))}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
