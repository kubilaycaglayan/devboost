"""Small, reusable SSH transport for remote DevBoost capabilities.

The application deliberately delegates authentication and host resolution to
the user's SSH configuration.  This module only owns the common subprocess
shape and timeout policy so feature modules do not each reimplement it.
"""

import subprocess


def run_ssh_command(host, remote_cmd, timeout=30, connect_timeout=5):
    """Run a non-interactive command through the configured SSH host alias."""
    return subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(connect_timeout)}",
            host,
            remote_cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
