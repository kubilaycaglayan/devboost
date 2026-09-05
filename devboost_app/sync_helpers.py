"""Deterministic helpers for folder sync and SSH host discovery."""

import glob
import os
import posixpath
import re

SYNC_DEFAULT_DIRECTION = "two-way"


def normalize_sync_path(p, is_remote=False):
    """Normalizes a sync path: expands ~ (local only), strips whitespace."""
    p = (p or "").strip()
    if not p:
        return ""
    if not is_remote:
        # Expand ~ and make absolute; keep as-entered for display otherwise.
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            # Resolve relative to $HOME (dashboard browsers always send absolute,
            # CLI users may send ~/x which is already expanded above).
            p = os.path.join(os.path.expanduser("~"), p)
        p = os.path.normpath(p)
    else:
        # Remote (POSIX): expand leading ~/ via $HOME is resolved server-side;
        # keep "~" as-is for display, normalize absolute paths with posixpath.
        if p.startswith("~"):
            return p  # e.g. ~, ~/projects — resolved by ssh/rsync remotely
        if not p.startswith("/"):
            p = "/" + p
        p = posixpath.normpath(p)
    return p

def validate_sync_paths(local_path, remote_path):
    """Validates a sync pair. Returns (local, remote) normalized or raises ValueError."""
    local = normalize_sync_path(local_path, is_remote=False)
    remote = normalize_sync_path(remote_path, is_remote=True)
    if not local or not remote:
        raise ValueError("Both local and remote folders are required")
    if local == "/":
        raise ValueError("Refusing to sync the filesystem root (/) — pick a subfolder")
    if remote in ("/",):
        raise ValueError("Refusing to sync the remote filesystem root (/) — pick a subfolder")
    if ":" in remote:
        raise ValueError("Remote path must not contain ':' (rsync host:path separator)")
    return local, remote

def explain_rsync_output(text, ssh_host=""):
    """Appends ONE targeted hint to raw rsync stderr based on known patterns.

    Hints are exclusive and ordered by conclusiveness: a local TCC denial
    explains the whole failure (including the follow-on "connection closed" /
    code-12 lines), so no other guess is appended in that case.
    """
    t = text or ""
    host = f" '{ssh_host}'" if ssh_host else ""
    if "Operation not permitted" in t:
        return t + " — macOS blocked local file access (grant Full Disk Access, see dashboard help)"
    if "command not found" in t and "rsync" in t:
        return t + f" — rsync is missing on the remote side (install with `sudo apt install rsync` on{host})"
    if "Permission denied (publickey" in t:
        return t + f" — SSH key rejected by{host} (check `ssh{host}` works without a password prompt)"
    if "No such file or directory" in t:
        return t + " — a synced path (or its parent) does not exist on that side"
    if "connection unexpectedly closed" in t:
        return (t + f" — the remote rsync never started on{host}: "
                "either rsync is not installed there, or the remote shell prints "
                "startup text (motd/echo in ~/.bashrc) that corrupts the protocol stream")
    return t

def build_rsync_commands(sync, ssh_host):
    """Builds the ordered rsync command list for a sync entry (no shell)."""
    direction = sync.get("direction", SYNC_DEFAULT_DIRECTION)
    mirror = bool(sync.get("mirror", False)) and direction in ("push", "pull")
    local = sync.get("local_path", "")
    remote = sync.get("remote_path", "")
    ssh_opts = "ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
    local_src = local.rstrip("/") + "/"
    remote_spec = f"{ssh_host}:{remote.rstrip('/')}/"
    remote_src = f"{ssh_host}:{remote.rstrip('/')}/"
    cmds = []
    base = ["rsync", "-az", "--update", "-e", ssh_opts]
    if direction == "push":
        cmd = base + (["--delete"] if mirror else []) + [local_src, remote_spec]
        cmds.append(cmd)
    elif direction == "pull":
        cmd = base + (["--delete"] if mirror else []) + [remote_src, local_src]
        cmds.append(cmd)
    else:  # two-way: push then pull, newer wins, never delete
        cmds.append(base + [local_src, remote_spec])
        cmds.append(base + [remote_src, local_src])
    return cmds

def parse_ssh_config_file(path):
    """Parses an OpenSSH config file into {alias: {hostname, user, port}}.

    Handles `Host` blocks with multiple patterns and `Include` directives.
    Wildcard patterns (* ? !) are skipped — they are not connectable tabs.
    """
    results = {}
    order = []

    def _parse_file(file_path, depth=0):
        if depth > 5:
            return
        try:
            with open(os.path.expanduser(file_path), "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return
        base_dir = os.path.dirname(os.path.expanduser(file_path))
        current_aliases = []
        current_opts = {}
        pending_aliases = None

        def _flush():
            if pending_aliases is None:
                return
            for alias in pending_aliases:
                if alias not in results:
                    results[alias] = {}
                    order.append(alias)
                for k, v in current_opts.items():
                    results[alias].setdefault(k, v)

        for raw in lines:
            # Strip comments (naive: cut at first #)
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" in line:
                # Support `Key=Value` style (e.g. HostName=example.com)
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
            else:
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                key, value = parts[0].strip(), parts[1].strip()
            key_low = key.lower()
            if key_low == "host":
                _flush()
                patterns = value.split()
                # A `Host` line opens a new block; concrete aliases become pending
                pending_aliases = [p for p in patterns if p and "*" not in p and "?" not in p and "!" not in p]
                current_aliases = pending_aliases
                current_opts = {}
                if not pending_aliases:
                    pending_aliases = []  # wildcard-only block: parse but don't record
            elif key_low == "include":
                _flush()
                pending_aliases = None
                current_opts = {}
                for pat in value.split():
                    # Strip optional quotes (Include "~/.ssh/config.d/*") and
                    # expand ~ BEFORE the isabs check — os.path.isabs("~/.ssh/..")
                    # is False, so checking first would wrongly join it onto base_dir
                    # and break forms like `Include ~/.ssh/config.d/xyz`.
                    pat = pat.strip().strip("'\"")
                    if not pat:
                        continue
                    pat = os.path.expanduser(pat)
                    full = pat if os.path.isabs(pat) else os.path.join(base_dir, pat)
                    for expanded in sorted(glob.glob(full)):
                        _parse_file(expanded, depth + 1)
            elif key_low in ("hostname", "user", "port"):
                if pending_aliases:
                    current_opts[key_low] = value
            # Other keys ignored for tab suggestions
        _flush()

    _parse_file(path)
    hosts = []
    for alias in order:
        opts = results.get(alias, {})
        try:
            port = int(str(opts.get("port", 22)))
        except ValueError:
            port = 22
        hosts.append({
            "ssh_host": alias,
            "hostname": opts.get("hostname", ""),
            "user": opts.get("user", ""),
            "port": port,
        })
    return hosts

