"""Read Codex's documented account API over stdio (also runnable over SSH).

Only the installed Codex client handles authentication. No tokens are read,
copied, or returned by DevBoost, and no conversation/model turn is started.
Protocol: https://learn.chatgpt.com/docs/app-server#auth-endpoints
"""

import json
import os
import selectors
import shutil
import signal
import subprocess
import time


def read_rate_limits(codex_home=None, timeout=15):
    executable = shutil.which("codex")
    if not executable:
        for candidate in ("~/.local/bin/codex", "/opt/homebrew/bin/codex",
                          "/usr/local/bin/codex", "/Applications/Codex.app/Contents/Resources/codex"):
            candidate = os.path.expanduser(candidate)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                executable = candidate
                break
    if not executable:
        raise ValueError("Codex CLI not found on this host. Install Codex and sign in with ChatGPT.")
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = os.path.abspath(os.path.expanduser(codex_home))
    deadline = time.monotonic() + timeout
    # Run away from the checkout so project configuration does not select a
    # different account/provider. Stderr can contain auth diagnostics: discard it.
    process = subprocess.Popen(
        [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, cwd=os.path.expanduser("~"),
        start_new_session=True,
    )
    buffer = b""
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        def request(request_id, method, params=None):
            nonlocal buffer
            send({"id": request_id, "method": method, "params": params})
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Codex live quota query timed out. Retry when the service is reachable.")
                if b"\n" not in buffer:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise ValueError("Codex app-server exited before returning quotas. Check your Codex installation and sign-in.")
                    buffer += chunk
                    if len(buffer) > 2 * 1024 * 1024:
                        raise ValueError("Codex app-server response exceeded the size limit.")
                    continue
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ValueError("Codex app-server returned invalid JSON.") from None
                if not isinstance(message, dict):
                    raise ValueError("Codex app-server returned an invalid response.")
                if message.get("method") and "id" in message:
                    # External-token/attestation requests require the owning
                    # client. Never collect credentials or leave it waiting.
                    send({"id": message["id"], "error": {
                        "code": -32601, "message": "DevBoost only reads account quotas"}})
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    code = (message.get("error") or {}).get("code")
                    if code == -32601:
                        raise ValueError("Update Codex: this version does not support the account quota API.")
                    raise ValueError("Codex could not fetch live quotas. Check connectivity and ChatGPT sign-in on this host; API-key accounts do not expose subscription quotas.")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Codex app-server returned an invalid result.")
                return result

        request(1, "initialize", {"clientInfo": {
            "name": "devboost", "title": "DevBoost Quotas", "version": "1.0"}})
        send({"method": "initialized"})
        return request(2, "account/rateLimits/read")
    except (BrokenPipeError, OSError) as exc:
        if isinstance(exc, TimeoutError):
            raise
        raise ValueError("Could not communicate with Codex app-server. Check your Codex installation.") from None
    finally:
        selector.close()
        # Reap this dedicated subprocess and its children on success AND timeout.
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.stdout.close()


if __name__ == "__main__":
    import sys
    try:
        print(json.dumps({"result": read_rate_limits(sys.argv[1] or None, float(sys.argv[2]))}))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
