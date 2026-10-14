"""Guard: `.env.template` names the variables the code reads, and only those.

The template is a hand-kept list, and hand-kept lists drift from the tree
(GDXA-573): it shipped variables no running container could act on
(CELERY_BROKER_URL and CELERY_RESULT_BACKEND, which Celery would honour but no
compose file forwards; STRIPE_CONNECT_WEBHOOK_SECRET, which no code reads) and
omitted ones live code did read (VAPID_*, CELL_GATEWAY_WEBHOOK_SECRET, ...).
Both directions are checked here:

1. Every name the backend reads from the environment is in the template, or in
   ``NOT_IN_TEMPLATE`` with the reason it is kept out.
2. Every name in the template is read somewhere: a quoted string literal in a
   non-test ``.py`` file under ``gdx_dispatch/``, a ``${NAME}`` or ``NAME:`` in
   ``gdx_dispatch/docker/``, or an entry in ``READ_ONLY_BY_TESTS`` or
   ``READ_OUTSIDE_THE_TREE``. A mention in a comment does not count, and
   neither does a test: STRIPE_CONNECT_WEBHOOK_SECRET was read by one e2e test
   and by no code an operator runs.

Direction 1 is an AST scan, so it sees only reads it can resolve:
``os.getenv``/``os.environ.get``/``os.environ[...]`` with a literal, a
module-level string constant, or a ``secret_env=`` keyword. A name built at
runtime (``for var in (...): os.getenv(var)``) is invisible to it; that gap is
why direction 2 accepts any quoted literal rather than a resolved read.

Neither direction sees a variable only a dependency reads (Celery, the
OpenTelemetry SDK, boto), nor whether a compose file forwards a variable at
all. A library-read name the template should keep goes in
``READ_OUTSIDE_THE_TREE``; direction 2 will otherwise call it dead.

``gdx_dispatch/tools/drift_scanner.py`` (``check_env_template_drift``) is an
older, warning-only regex form of direction 1 over three directories; this
file is the gate.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "gdx_dispatch"
TEMPLATE = REPO / ".env.template"

_READ_CALLS = frozenset(
    {"os.getenv", "getenv", "os.environ.get", "environ.get", "os.environ.setdefault"}
)
_ENVIRON = frozenset({"os.environ", "environ"})
_SKIP_PARTS = frozenset({"tests", "frontend", "node_modules", "__pycache__"})

# Read by the backend, deliberately absent from the operator's template. Every
# entry must still be read (see test_the_allowlists_are_not_stale).
_RUNTIME = "set by the runtime, the image or compose wiring, not by the operator"
_TOOLING = "read by a developer/CI tool under gdx_dispatch/tools/, not by the app"
_MINTING = "self-host secret minting; docker-compose.customer.yml sets it"
# A real operator knob with a working default, never written into the template.
# Each one is a candidate for a documented template entry, not a decision that
# it should stay out.
_UNDOCUMENTED = "operator knob with a default; not yet documented in the template"

NOT_IN_TEMPLATE: dict[str, str] = {
    "PYTEST_CURRENT_TEST": _RUNTIME,
    "PYTEST_VERSION": _RUNTIME,
    "ALEMBIC_DATABASE_URL": _RUNTIME,
    "GIT_SHA": _RUNTIME,
    "APP_GIT_SHA": _RUNTIME,
    "TENANT_ID": _RUNTIME,
    "PLUGIN_HOST_URL": _RUNTIME,
    "PLUGIN_INSTALL_DIR": _RUNTIME,
    "PLUGIN_PIP_TIMEOUT": _RUNTIME,
    "PLUGIN_BROWSER_STATE_DIR": _RUNTIME,
    "GDX_REPO_ROOT": _TOOLING,
    "GDX_SNAPSHOT_DIR": _TOOLING,
    "GDX_SKIP_BOOTSTRAP": _TOOLING,
    "MATRIX_BASELINE_DIR": _TOOLING,
    "GITGUARDIAN_API_KEY": _TOOLING,
    "PAVE_DIRECT_TENANT_HOST": _TOOLING,
    "SCHEMA_DRIFT_KUMA_URL": _TOOLING,
    "SCHEMA_DRIFT_SKIP_TENANTS": _TOOLING,
    "GDX_SECRETS_DIR": _MINTING,
    "GDX_SECRETS_OWNER_UID": _MINTING,
    "GDX_SECRETS_OWNER_GID": _MINTING,
    "GDX_N8N_OWNER_UID": _MINTING,
    "GDX_N8N_SECRETS_DIR": _MINTING,
    "ACTIVE_SESSION_COUNT": _UNDOCUMENTED,
    "LAST_BACKUP_TS": _UNDOCUMENTED,
    "MFA_REQUIRED": _UNDOCUMENTED,
    "AI_PROVIDER": _UNDOCUMENTED,
    "AI_CLOUD_API_KEY": _UNDOCUMENTED,
    "AI_CLOUD_ENDPOINT": _UNDOCUMENTED,
    "AI_CLOUD_MODEL": _UNDOCUMENTED,
    "AI_CLOUD_MODEL_FAST": _UNDOCUMENTED,
    "AI_LOCAL_ENDPOINT": _UNDOCUMENTED,
    "AI_LOCAL_MODEL": _UNDOCUMENTED,
    "AI_RATE_LIMIT_PER_MIN": _UNDOCUMENTED,
    "AUTH_DB_VERIFY_ENABLED": _UNDOCUMENTED,
    "GDX_BUSINESS_TZ": _UNDOCUMENTED,
    "GDX_INVOICE_INVARIANT": _UNDOCUMENTED,
    "GDX_LOG_REDACT_SALT": _UNDOCUMENTED,
    "GDX_PLUGIN_CATALOG_URL": _UNDOCUMENTED,
    "GDX_SECURITY_EMAIL": _UNDOCUMENTED,
    "GDX_SUPPORT_EMAIL": _UNDOCUMENTED,
    "GDX_SESSION_RECORDING": _UNDOCUMENTED,
    "GDX_SESSION_RECORDING_DIR": _UNDOCUMENTED,
    "GDX_WEBHOOK_PRIVATE_ALLOW": _UNDOCUMENTED,
    "OTEL_ENABLED": _UNDOCUMENTED,
    "OTEL_EXPORTER": _UNDOCUMENTED,
    "OTEL_EXPORTER_OTLP_ENDPOINT": _UNDOCUMENTED,
    "OUTLOOK_BOOTSTRAP_TENANT_SLUG": _UNDOCUMENTED,
    "OUTLOOK_FALLBACK_STALE_MINUTES": _UNDOCUMENTED,
    "OUTLOOK_SYNC_FOLDER_LAG_HOURS": _UNDOCUMENTED,
    "OUTLOOK_SYNC_STALL_HOURS": _UNDOCUMENTED,
    "PLANNER_DIGEST_EMAIL": _UNDOCUMENTED,
    "PLANNER_DIGEST_HOUR": _UNDOCUMENTED,
    "QB_DELETE_SYNC_ENABLED": _UNDOCUMENTED,
    "REFRESH_TTL_SECONDS": _UNDOCUMENTED,
    "SEARCH_HASH_SALT": _UNDOCUMENTED,
}

# In the template's dev/test section, read only by the e2e and load tests.
READ_ONLY_BY_TESTS = frozenset(
    {"GDX_E2E_EMAIL", "GDX_E2E_PASSWORD", "GDX_LOAD_USERS", "GDX_LOAD_DURATION"}
)

# In the template, read by nothing in this repo's own code: a host-side tool
# (the template's comment names it) or a library dependency reads them.
READ_OUTSIDE_THE_TREE: dict[str, str] = {
    "CEREBRAS_API_KEY": "host LLM-helper MCP server, outside the repo",
    "GROQ_API_KEY": "host LLM-helper MCP server, outside the repo",
    "GOOGLE_API_KEY": "host LLM-helper MCP server, outside the repo",
}


def template_names(text: str) -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


def _module_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    return consts


def _resolve(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def env_reads(source: str) -> set[str]:
    """Names a module reads from the environment, as far as the AST can tell."""
    tree = ast.parse(source)
    consts = _module_constants(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call):
            if ast.unparse(node.func) in _READ_CALLS and node.args:
                name = _resolve(node.args[0], consts)
            for kw in node.keywords:
                if kw.arg == "secret_env":
                    found.add(_resolve(kw.value, consts) or "")
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in _ENVIRON:
            name = _resolve(node.slice, consts)
        if name:
            found.add(name)
    found.discard("")
    return found


def _backend_sources() -> list[Path]:
    return [
        p for p in PKG.rglob("*.py")
        if not _SKIP_PARTS.intersection(p.relative_to(PKG).parts)
    ]


def backend_reads() -> dict[str, list[str]]:
    reads: dict[str, list[str]] = {}
    for path in _backend_sources():
        for name in env_reads(path.read_text(encoding="utf-8")):
            reads.setdefault(name, []).append(str(path.relative_to(REPO)))
    return reads


def _quoted_in(names: set[str], paths: list[Path]) -> set[str]:
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in paths)
    return {n for n in names if re.search(rf"[\"']{n}[\"']", text)}


def _referenced(names: set[str]) -> set[str]:
    """The subset of ``names`` that non-test code or the compose files use."""
    docker_text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in (PKG / "docker").rglob("*")
        if p.is_file() and (p.suffix in {".yml", ".sh"} or p.name.startswith("Dockerfile"))
    )
    used = _quoted_in(names, _backend_sources())
    for name in names:
        if re.search(rf"\$\{{?{name}\b|^\s*{name}:", docker_text, re.M):
            used.add(name)
    return used


def test_every_env_read_is_in_the_template_or_allowlisted():
    """The backend reads a variable the template never mentions.

    Add it to ``.env.template`` with a comment and a blank or safe default.
    If an operator should never set it, add it to ``NOT_IN_TEMPLATE`` with
    the reason.
    """
    reads = backend_reads()
    template = template_names(TEMPLATE.read_text(encoding="utf-8"))
    missing = sorted(set(reads) - template - set(NOT_IN_TEMPLATE))
    assert not missing, "\n".join(f"{n}  ({', '.join(sorted(reads[n])[:2])})" for n in missing)


def test_every_template_variable_is_read():
    """The template lists a variable nothing in gdx_dispatch/ uses.

    An operator fills it in and nothing happens. Delete it from the template,
    or, if something outside this repo reads it, record it in
    ``READ_OUTSIDE_THE_TREE``.
    """
    template = template_names(TEMPLATE.read_text(encoding="utf-8"))
    dead = sorted(
        template - _referenced(template) - READ_ONLY_BY_TESTS - set(READ_OUTSIDE_THE_TREE)
    )
    assert not dead, dead


def test_the_allowlists_are_not_stale():
    """An allowlist entry no longer needs its exemption.

    A name in ``NOT_IN_TEMPLATE`` is no longer read, or has since been added
    to the template; a name in ``READ_ONLY_BY_TESTS`` has left the template,
    is read by non-test code now, or no test reads it any more; a name in
    ``READ_OUTSIDE_THE_TREE`` has left the template. Delete the entry.
    """
    reads = backend_reads()
    template = template_names(TEMPLATE.read_text(encoding="utf-8"))
    tests = [p for p in (PKG / "tests").rglob("*.py") if "__pycache__" not in p.parts]
    read_by_tests = _quoted_in(set(READ_ONLY_BY_TESTS), tests)
    stale = sorted(n for n in NOT_IN_TEMPLATE if n not in reads or n in template)
    stale += sorted(
        n for n in READ_ONLY_BY_TESTS
        if n not in template or n in _referenced({n}) or n not in read_by_tests
    )
    stale += sorted(n for n in READ_OUTSIDE_THE_TREE if n not in template)
    assert not stale, stale


def test_the_scan_sees_each_read_shape_and_ignores_comments():
    """Proves the AST scan can go red: each supported read shape is seen,
    and a name that appears only in a comment or a docstring is not."""
    source = '''
"""Mentions DOC_ONLY in a docstring."""
import os
from os import environ, getenv
SECRET_ENV = "VIA_CONSTANT"
# COMMENT_ONLY is not read
a = os.getenv("VIA_GETENV")
b = os.environ.get("VIA_ENVIRON_GET", "")
c = os.environ["VIA_SUBSCRIPT"]
d = getenv("VIA_BARE_GETENV")
e = os.getenv(SECRET_ENV)
verify_shared_secret(request, header="X", secret_env="VIA_KWARG")
'''
    assert env_reads(source) == {
        "VIA_GETENV",
        "VIA_ENVIRON_GET",
        "VIA_SUBSCRIPT",
        "VIA_BARE_GETENV",
        "VIA_CONSTANT",
        "VIA_KWARG",
    }
    assert template_names("A=1\n# B=2\nC_D=\n") == {"A", "C_D"}
