"""plugin-host — the separate container that runs all third-party plugins.

ADR-013 Model B (VS Code Extension Host style): plugins run here, isolated from
the core app, which proxies /api/plugins/* to this process. See app.py.
"""
