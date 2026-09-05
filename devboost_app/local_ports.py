"""Local listening-port inspection and safe process termination."""

import os
import signal
import time


def get_listening_ports(runtime):
    """Return local listeners with DevBoost tunnel attribution."""
    try:
        listening = runtime.get_listening_ports()
    except Exception:
        return []
    tunnel_pids = set()
    try:
        for forward in runtime.get_ssh_forwards():
            if forward.get("is_listening") and forward.get("pid"):
                tunnel_pids.add(forward.get("pid"))
    except Exception:
        pass
    rows = []
    for port in sorted(listening):
        info = listening.get(port) or {}
        if not isinstance(info, dict):
            continue
        rows.append({
            "port": port,
            "pid": info.get("pid"),
            "cmd": info.get("cmd", ""),
            "node": info.get("node", ""),
            "devboost": info.get("pid") in tunnel_pids,
        })
    return rows


def _pid_is_dead(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except (ChildProcessError, OSError):
        pass
    return False


def kill_listening_process(runtime, pid):
    """Stop a process holding a listener, refusing stale or unsafe targets."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid pid: {pid!r}"}
    if pid <= 1:
        return {"ok": False, "message": "Refusing to kill pid 1"}
    if pid == os.getpid():
        return {"ok": False, "message": "Refusing to kill the dashboard itself"}
    try:
        holders = {info.get("pid") for info in runtime.get_listening_ports().values()
                   if isinstance(info, dict)}
    except Exception:
        holders = set()
    if pid not in holders:
        return {"ok": False, "message": f"PID {pid} no longer holds a listening port (refresh and retry)"}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {"ok": False, "message": f"Cannot signal PID {pid}: {exc}"}
    time.sleep(0.5)
    if _pid_is_dead(pid):
        return {"ok": True, "message": f"Stopped PID {pid} (SIGTERM)"}
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return {"ok": False, "message": f"PID {pid} ignored SIGTERM and cannot be force-killed: {exc}"}
    time.sleep(0.3)
    if _pid_is_dead(pid):
        return {"ok": True, "message": f"Killed PID {pid} (SIGKILL)"}
    return {"ok": False, "message": f"PID {pid} is still alive (zombie or kernel process?)"}
