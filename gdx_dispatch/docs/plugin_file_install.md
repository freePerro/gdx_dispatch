# Installing a private plugin by file upload

Extends ADR-013 step 5 (in-app install). The registry flow installs plugins by
**name from a pip index** (PyPI). A **private/internal plugin** (not published
anywhere) is installed by **uploading its built artifact** instead.

## Flow

1. Build the plugin into a wheel or sdist:
   ```bash
   cd plugins/gdx-plugin-foo && python -m build   # → dist/gdx_plugin_foo-0.1.0-...whl
   ```
2. **Owner** → Plugins admin → **Upload plugin file** → pick the `.whl`/`.tar.gz`.
3. **Restart plugin-host.** On boot, `reconcile()` installs every uploaded
   artifact (`pip install --target /plugins <file>`) alongside the index packages,
   then discovery mounts it.

## Storage & trust

- The artifact is stored in the DB (`plugin_artifact`: filename, sha256, bytes,
  uploaded_by) — so the core app (which receives the upload) and plugin-host
  (which installs it) share state without a shared volume, mirroring the
  `plugin_registry` pattern. plugin-host writes it to `/plugins/_artifacts/<name>`
  and pip-installs from there.
- **Owner-only + audited.** An uploaded plugin runs with backend access in
  plugin-host — the same trust tier as adding a pip dependency. Only upload
  artifacts you built or vetted.
- **Installation executes code.** A `.tar.gz` sdist runs its `setup.py` during
  `pip install` (at plugin-host reconcile), and any plugin's code runs once
  mounted. The ADR-014 **`browser` consent gate is use-time, not install-time** —
  it does NOT vet installation. Treat uploading like adding a dependency.
- Filenames are reduced to a safe basename and must be `.whl`/`.tar.gz` (no path
  traversal); uploads are capped at 50 MB; the stored SHA-256 is re-verified
  before install (a tampered/corrupted row is refused).

## Endpoints (owner-only)

- `POST /api/admin/plugins/upload` — multipart `file=` → stores the artifact.
- `GET /api/admin/plugins/artifacts` — list (metadata only, never the bytes).
- `DELETE /api/admin/plugins/artifacts/{filename}` — remove (installed copy stays
  until the next plugin-host restart).

## Note on the CHI plugin

This is how the gitignored CHI pricing plugin (browser stream, Phases 1/3) gets
onto prod: build its wheel, upload it here, restart plugin-host. Its `browser`
permission still requires owner consent before the stream can be used (ADR-014).
