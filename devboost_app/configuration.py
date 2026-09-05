"""Configuration persistence and migration with explicit runtime dependencies."""

import json
import os
import tempfile


def _write_config_atomic(runtime, cfg):
    directory = os.path.dirname(os.path.abspath(runtime.CONFIG_FILE))
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, runtime.CONFIG_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _migrate_legacy_config(runtime):
    if os.path.exists(runtime.CONFIG_FILE):
        return None
    try:
        if os.path.abspath(os.path.dirname(os.path.abspath(runtime.CONFIG_FILE))) != os.path.abspath(runtime.APP_DIR):
            return None
    except OSError:
        return None
    for legacy_path in runtime.LEGACY_CONFIG_FILES:
        try:
            if not legacy_path or os.path.abspath(legacy_path) == os.path.abspath(runtime.CONFIG_FILE):
                continue
        except OSError:
            continue
        if os.path.exists(legacy_path):
            try:
                with open(legacy_path, "r", encoding="utf-8") as handle:
                    legacy_cfg = json.load(handle)
                runtime.ensure_dirs()
                _write_config_atomic(runtime, legacy_cfg)
                return legacy_path
            except Exception:
                continue
    return None


def _default_docker_labels():
    return [
        {"id": "dev", "name": "Dev", "match": "dev", "color": "#58a6ff", "enabled": True},
        {"id": "production", "name": "production", "match": "production", "color": "#f85149", "enabled": True},
    ]


def load_config(runtime):
    runtime.ensure_dirs()
    _migrate_legacy_config(runtime)
    if os.path.exists(runtime.CONFIG_FILE):
        try:
            with open(runtime.CONFIG_FILE, "r", encoding="utf-8") as handle:
                cfg = json.load(handle)
            cfg.setdefault("labels", {})
            cfg.setdefault("rules", {})
            cfg.setdefault("docker_labels", _default_docker_labels())
            cfg.setdefault("history", [])
            cfg.setdefault("usage_accounts", [])
            cfg.setdefault("usage_snapshots", {})
            cfg, changed = runtime.ensure_servers_migrated(cfg)
            cfg, sync_changed = runtime.ensure_syncs_migrated(cfg)
            if changed or sync_changed:
                try:
                    _write_config_atomic(runtime, cfg)
                except Exception:
                    pass
            return cfg
        except Exception:
            pass
    return {
        "labels": {}, "rules": {}, "docker_labels": _default_docker_labels(),
        "history": [], "server_labels": {}, "usage_accounts": [],
        "usage_snapshots": {}, "servers": [runtime.default_server_from_env(order=0)],
        "syncs": [], "folder_history": [],
    }


def save_config(runtime, cfg):
    runtime.ensure_dirs()
    cfg, _ = runtime.ensure_servers_migrated(cfg)
    cfg, _ = runtime.ensure_syncs_migrated(cfg)
    _write_config_atomic(runtime, cfg)
