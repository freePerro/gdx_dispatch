# tests/serial/ — what "serial" actually means here

Historical name. **No mechanism makes this directory serial**: there is no
marker, no plugin, and no runner logic that treats it specially. pytest-split
assigns these files to shards like any others.

What actually protects them:

- Each CI shard is one independent pytest process — cross-shard interference
  is impossible by construction.
- The autouse `_reset_module_state` fixture in `tests/conftest.py` resets the
  known module-level stores between every test.
- Until 2026-08-04, CI also ran `--forked` (per-test subprocesses), which
  incidentally isolated the one genuine global mutation here
  (`test_module_system.py` calling `engine_registry.dispose_all()`). CI is now
  **unforked**; that this directory stays green in-process was verified by a
  full serial single-process run of the whole suite (2026-08-04, exit 0) and
  17 unforked shard runs.

If you add a test that mutates process-global state and cannot restore it,
either restore it in a fixture (preferred), or mark that one test
`@pytest.mark.forked` (pytest-forked is installed for exactly this), or fix
`_reset_module_state` to cover the new store. Do not rely on this directory's
name for isolation — it provides none.
