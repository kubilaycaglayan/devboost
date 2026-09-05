"""Dashboard presentation assets and HTTP helpers."""

from pathlib import Path


_DASHBOARD_DIR = Path(__file__).resolve().parent


def _read_asset(name):
    return (_DASHBOARD_DIR / name).read_text(encoding="utf-8")


def load_dashboard_html():
    """Build the dashboard document from its maintainable HTML/CSS/JS assets."""
    return _read_asset("index.html")


def read_asset(name):
    """Return a dashboard asset, raising FileNotFoundError for unknown names."""
    allowed = {"styles.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
    if name not in allowed:
        raise FileNotFoundError(name)
    return _read_asset(name), allowed[name]


__all__ = ["load_dashboard_html", "read_asset"]
