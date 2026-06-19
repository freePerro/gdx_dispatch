#!/usr/bin/env python3
"""Drift scanner — checks the GDX codebase for Build Rule violations.

Run after every feature, before every deploy:
    python gdx_dispatch/tools/drift_scanner.py

Exit code 0 = no drift, 1 = violations found.
"""
from __future__ import annotations
import logging

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GDX_DIR = REPO_ROOT / "gdx_dispatch"
ROUTERS_DIR = GDX_DIR / "routers"
CORE_DIR = GDX_DIR / "core"
REQUIREMENTS = GDX_DIR / "requirements.txt"
APP_PY = GDX_DIR / "app.py"

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RESET = "\033[0m"

violations: list[str] = []
warnings: list[str] = []


def scan_file(path: Path, pattern: str, msg: str, severity: str = "ERROR") -> int:
    """Scan a file for regex violations. Returns count.

    For .py files, skips lines that are comments or inside triple-quoted
    strings (docstrings) so that prose explaining a rule doesn't self-trigger.
    """
    count = 0
    is_py = path.suffix == ".py"
    try:
        content = path.read_text(errors="replace")
        in_triple = False  # True while inside a """...""" or '''...''' block
        for i, line in enumerate(content.splitlines(), 1):
            if is_py:
                # Toggle triple-quote state based on count of triple markers on this line
                triple_count = line.count('"""') + line.count("'''")
                started_in_triple = in_triple
                if triple_count % 2 == 1:
                    in_triple = not in_triple
                # Skip fully in-docstring lines, fully comment lines
                if started_in_triple or (triple_count >= 2 and not in_triple):
                    continue
                if line.lstrip().startswith("#"):
                    continue
            if re.search(pattern, line):
                entry = f"{path.relative_to(REPO_ROOT)}:{i}: {msg}"
                if severity == "ERROR":
                    violations.append(entry)
                else:
                    warnings.append(entry)
                count += 1
    except Exception:
        logging.getLogger(__name__).debug("scan_file: swallowed S110 — suppressed exception", exc_info=True)
        pass
    return count


def scan_directory(directory: Path, pattern: str, msg: str, severity: str = "ERROR", ext: str = ".py") -> int:
    """Scan all files in a directory for a pattern."""
    total = 0
    if not directory.exists():
        return 0
    for f in sorted(directory.rglob(f"*{ext}")):
        if "__pycache__" in str(f):
            continue
        total += scan_file(f, pattern, msg, severity)
    return total


def check_sql_portability() -> None:
    """Check for PostgreSQL-only SQL in GDX code."""
    print("Checking SQL portability...")
    dirs = [ROUTERS_DIR, CORE_DIR, GDX_DIR / "modules", GDX_DIR / "tasks"]
    for d in dirs:
        scan_directory(d, r"gen_random_uuid\(\)", "PostgreSQL-only: gen_random_uuid(). Use Python str(uuid4()).")
        scan_directory(d, r"(?<!')NOW\(\)(?!')", "PostgreSQL-only: NOW(). Use Python datetime.now(timezone.utc).")
        scan_directory(d, r"::(text|int|integer|boolean|varchar|numeric)", "PostgreSQL cast ::type. Use Python conversion.")
        scan_directory(d, r"COALESCE\([^,]+,\s*[01]\)\s*=\s*[01]", "Boolean/integer COALESCE mismatch. Use true/false.")


def check_silent_exceptions() -> None:
    """Check for except blocks without logging (AST-based to avoid false positives).

    Parses each Python file with ast, finds ExceptHandler nodes, checks if
    the handler body contains any call to log/logger/logging methods
    (exception, error, warning, info, debug) or a raise statement.
    Only flags handlers that silently swallow errors.
    """
    import ast

    print("Checking error handling (AST)...")
    LOG_METHODS = {"exception", "error", "warning", "warn", "info", "debug", "critical", "fatal"}

    def _has_logging_or_raise(body: list[ast.stmt]) -> bool:
        """Return True if the except body logs or re-raises."""
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call):
                func = node.func
                # log.exception(...), logger.error(...), logging.warning(...)
                if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
                    return True
        return False

    def _captures_bound_name(handler: ast.ExceptHandler) -> bool:
        """Return True when `except X as <name>:` references <name> in the body.

        Catches the capture-and-reuse pattern (2026-04-17 enhancement after
        a false-positive sat in the bug queue):

            primary_error: JWTValidationError | None = None
            try:
                principal = validate_principal(...)
            except JWTValidationError as exc:
                # Fall through to legacy decoder
                primary_error = exc       # <-- captured, not dropped
            ...
            if primary_error is not None:
                log.warning("...", primary_error)  # used at tail

        That is *not* silent-swallow — the error is carried forward for
        diagnostics. Touching `exc` anywhere inside the body (assignment,
        function arg, container append, etc.) is sufficient proof of
        intent; the existing _has_logging_or_raise covers the direct
        log/raise shape separately.

        Returns False for bare `except:` (handler.name is None) so those
        still get flagged as silent when the body has no log/raise.
        """
        if handler.name is None:
            return False
        for stmt in handler.body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and node.id == handler.name:
                    return True
        return False

    for d in [ROUTERS_DIR, CORE_DIR, GDX_DIR / "modules"]:
        if not d.exists():
            continue
        for py_file in sorted(d.rglob("*.py")):
            try:
                tree = ast.parse(py_file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if _has_logging_or_raise(node.body) or _captures_bound_name(node):
                    continue
                rel = py_file.relative_to(REPO_ROOT)
                # CLAUDE.md Build Rule "Error Handling — No Silent Failures"
                # says NEVER. These are violations, not warnings. Classifier
                # flipped 2026-04-16 an earlier session after D21 discovery.
                # Capture-and-reuse whitelist added 2026-04-17 after the
                # fallback-auth pattern kept tripping this detector.
                violations.append(
                    f"{rel}:{node.lineno}: except block swallows error silently (no log or raise)"
                )


def check_requirements_sync() -> None:
    """Check that common packages are in requirements.txt."""
    print("Checking requirements sync...")
    if not REQUIREMENTS.exists():
        violations.append("gdx_dispatch/requirements.txt missing!")
        return

    req_text = REQUIREMENTS.read_text()
    needed = {
        "werkzeug": "werkzeug needed for Flask password hash compat",
        "python-multipart": "python-multipart needed for UploadFile/Form endpoints",
        "prometheus_client": "prometheus_client needed for /metrics endpoint",
    }
    for pkg, reason in needed.items():
        if pkg not in req_text:
            violations.append(f"gdx_dispatch/requirements.txt: missing {pkg} — {reason}")


def check_router_registration() -> None:
    """Check that all routers in gdx_dispatch/routers/ are registered in app.py."""
    print("Checking router registration...")
    if not APP_PY.exists():
        return
    app_text = APP_PY.read_text()

    for f in sorted(ROUTERS_DIR.glob("*.py")):
        if f.name.startswith("_") or f.name == "__init__.py":
            continue
        module_name = f.stem
        if module_name not in app_text:
            warnings.append(f"gdx_dispatch/routers/{f.name}: not found in app.py — may not be registered")


def check_import_logging() -> None:
    """Check that app.py import blocks log failures or raise.

    2026-04-17 D48-prereq fix: widen lookahead window from 3→10 lines
    (logging often follows a multi-line comment block, especially on
    health-probe patterns) and respect ``
    markers the same way silent_failure_scanner does. The prior version
    flagged app.py:1186 as a false positive — log call was on line
    1190, four past a 3-line comment, outside the old window.

    The substring check was also too loose — "error" appeared in
    many unrelated strings like `validation_error`. Using a regex
    anchored on method calls (`.exception(`, `.warn(`, etc.) or bare
    `raise` is a stronger signal.
    """
    print("Checking import error logging...")
    if not APP_PY.exists():
        return
    lines = APP_PY.read_text().splitlines()
    log_pattern = re.compile(
        r"\.(?:exception|error|warning|warn|info|debug|critical|fatal)\s*\(|\braise\b",
        re.IGNORECASE,
    )
    noqa_pattern = re.compile(r"#\s*noqa:\s*silent-failure", re.IGNORECASE)
    for i, line in enumerate(lines):
        if "except Exception" not in line and "except ImportError" not in line:
            continue
        # Respect noqa markers on the except line or adjacent lines
        # (mirrors silent_failure_scanner's ±1 tolerance).
        if noqa_pattern.search(line):
            continue
        if i > 0 and noqa_pattern.search(lines[i - 1]):
            continue
        if i + 1 < len(lines) and noqa_pattern.search(lines[i + 1]):
            continue
        # 10-line lookahead window catches logging that follows a
        # multi-line comment block.
        lookahead = lines[i + 1:i + 11]
        if any(log_pattern.search(ln) for ln in lookahead):
            continue
        violations.append(
            f"gdx_dispatch/app.py:{i + 1}: except block without logging or raise"
        )


def check_nullable_company_id() -> None:
    """Check for nullable=True on company_id columns."""
    print("Checking company_id nullable...")
    models = REPO_ROOT / "archive/dispatch_flask" / "db" / "models.py"
    if models.exists():
        scan_file(models, r"company_id.*nullable\s*=\s*True", "company_id nullable=True — must be nullable=False")
    gdx_models = GDX_DIR / "models"
    if gdx_models.exists():
        scan_directory(gdx_models, r"company_id.*nullable\s*=\s*True", "company_id nullable=True — must be nullable=False")


def check_openapi_coverage() -> None:
    """Check that new routers appear in OpenAPI spec (requires running server)."""
    print("Checking OpenAPI coverage (skipped — requires running server)...")


def check_env_template_drift() -> None:
    """Check that .env.template has all env vars used in code."""
    print("Checking .env.template drift...")
    template = REPO_ROOT / ".env.template"
    if not template.exists():
        warnings.append(".env.template missing — cannot check env var coverage")
        return

    template_text = template.read_text()

    # Find env vars used in GDX code
    env_pattern = re.compile(r'os\.(?:environ|getenv)\s*[\.\[]\s*[\'"]([A-Z_]+)')
    used_vars: set[str] = set()
    for d in [ROUTERS_DIR, CORE_DIR, GDX_DIR / "tasks"]:
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                for m in env_pattern.finditer(f.read_text(errors="replace")):
                    used_vars.add(m.group(1))
            except Exception:
                logging.getLogger(__name__).debug("check_env_template_drift: swallowed S110 — suppressed exception", exc_info=True)
                pass

    # Ignore common/standard vars
    ignore = {"PATH", "HOME", "USER", "PYTHONPATH", "GDX_ENV", "SENTRY_DSN", "REDIS_URL",
              "CONTROL_DATABASE_URL", "DATABASE_URL", "SECRET_KEY", "JWT_SECRET_KEY"}

    missing = used_vars - ignore
    for var in sorted(missing):
        if var not in template_text:
            warnings.append(f".env.template: missing {var} (used in code but not documented)")


def check_audit_logging_coverage() -> None:
    """Check that routers with mutations have audit logging."""
    print("Checking audit logging coverage...")
    mutation_keywords = re.compile(r"INSERT INTO|UPDATE .+ SET|DELETE FROM", re.IGNORECASE)
    audit_pattern = re.compile(r"log_audit_event|log_audit_event_sync")

    for f in sorted(ROUTERS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            content = f.read_text(errors="replace")
            has_mutations = bool(mutation_keywords.search(content))
            has_audit = bool(audit_pattern.search(content))
            if has_mutations and not has_audit:
                warnings.append(f"gdx_dispatch/routers/{f.name}: has INSERT/UPDATE/DELETE but no log_audit_event() calls")
        except Exception:
            logging.getLogger(__name__).debug("check_audit_logging_coverage: swallowed S110 — suppressed exception", exc_info=True)
            pass


def check_test_fixture_patterns() -> None:
    """Check that test files with TestClient have proper tenant middleware."""
    print("Checking test fixture patterns...")
    tests_dir = GDX_DIR / "tests"
    if not tests_dir.exists():
        return

    for f in sorted(tests_dir.glob("*.py")):
        if f.name.startswith("_") or f.name == "conftest.py":
            continue
        try:
            content = f.read_text(errors="replace")
            has_testclient = "TestClient" in content or "AsyncClient" in content
            has_tenant_middleware = "request.state.tenant" in content or "tenant_module_grants" in content
            if has_testclient and not has_tenant_middleware:
                warnings.append(f"gdx_dispatch/tests/{f.name}: uses TestClient but missing tenant middleware/module grants")
        except Exception:
            logging.getLogger(__name__).debug("check_test_fixture_patterns: swallowed S110 — suppressed exception", exc_info=True)
            pass


def check_container_dep_sync() -> None:
    """Check that requirements.txt has all imports used in GDX code."""
    print("Checking dependency coverage...")
    if not REQUIREMENTS.exists():
        return

    req_text = REQUIREMENTS.read_text().lower()
    # Map import names to pip package names (when they differ)
    import_to_pip = {
        "jwt": "python-jose",
        "jose": "python-jose",
        "PIL": "pillow",
        "yaml": "pyyaml",
        "multipart": "python-multipart",
        "weasyprint": "weasyprint",
        "celery": "celery",
        "boto3": "boto3",
        "google": "google-api-python-client",
        "googlemaps": "googlemaps",
        "sentry_sdk": "sentry-sdk",
        "pythonjsonlogger": "python-json-logger",
    }

    third_party_imports: set[str] = set()
    stdlib = {"os", "sys", "re", "json", "time", "datetime", "uuid", "logging", "typing",
              "pathlib", "hashlib", "secrets", "base64", "binascii", "csv", "io", "copy",
              "contextlib", "collections", "functools", "threading", "asyncio", "dataclasses",
              "decimal", "email", "imaplib", "enum", "abc", "unittest", "importlib", "math",
              "traceback", "inspect", "textwrap", "itertools", "operator", "string", "http",
              "__future__", "argparse", "contextvars", "gzip", "hmac", "random", "smtplib",
              "socket", "statistics", "subprocess", "urllib", "zipfile", "zoneinfo",
              "shutil", "tempfile", "struct", "signal", "glob", "fnmatch", "pprint",
              # Packages that come with FastAPI/SQLAlchemy (transitive deps)
              "starlette", "jinja2", "kombu", "codebase"}

    for d in [ROUTERS_DIR, CORE_DIR, GDX_DIR / "tasks", GDX_DIR / "modules"]:
        if not d.exists():
            continue
        for f in d.rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            try:
                for line in f.read_text(errors="replace").splitlines():
                    m = re.match(r"^(?:from|import)\s+(\w+)", line)
                    if m:
                        pkg = m.group(1)
                        if pkg not in stdlib and not pkg.startswith("gdx_dispatch") and not pkg.startswith("archive/dispatch_flask"):
                            third_party_imports.add(pkg)
            except Exception:
                logging.getLogger(__name__).debug("check_container_dep_sync: swallowed S110 — suppressed exception", exc_info=True)
                pass

    for imp in sorted(third_party_imports):
        pip_name = import_to_pip.get(imp, imp).lower()
        if pip_name not in req_text and imp.lower() not in req_text:
            warnings.append(f"gdx_dispatch/requirements.txt: import '{imp}' (pip: {pip_name}) used in code but not in requirements")


def check_pii_in_logs() -> None:
    """Platform SS-2 P7 / SS-3 P11 sensitivity classification.

    PII-class values (email / JWT / bearer / JSONB payloads carrying secrets)
    must never hit logs unredacted. Policy: any logger.* call whose f-string
    or .format() args contain a PII-shaped expression must route through
    gdx_dispatch.core.log_redact before formatting.

    Heuristic: flag `logger.*(...)` or `logging.*(...)` calls whose argument
    string contains a whitelisted-offender token — `.email`, `.metadata`,
    `.provider_email`, `.token_hash`, `.provider_metadata`, `.config` (JSONB),
    `.dimensions` (meter_events), `Authorization`, `Bearer ` — UNLESS the
    same line or the preceding line imports or calls `redact_` helpers.

    This is deliberately over-broad: a false-positive forces the author to
    decide whether the log line is safe; a false-negative ships PII to logs.
    Over-broad is the safer failure mode.
    """
    print("Checking PII in log calls (SS-2 P7 / SS-3 P11)...")
    offender_tokens = (
        r"\.(?:email|provider_email|metadata|provider_metadata|"
        r"token_hash|config|dimensions|contact_email|refresh_token|access_token)\b"
        r"|\bAuthorization\b|\bBearer\s+"
    )
    log_call = r"(?:logger|logging|log)\.(?:debug|info|warning|error|exception|critical)\s*\("
    # Combine: a log call line that also has an offender token and does NOT
    # mention redact_ on the same line.
    # Multi-line AST would be more accurate; this line-level check catches the
    # common f-string and .format patterns.
    pattern = rf"^(?!.*\bredact_).*{log_call}.*(?:{offender_tokens})"

    for d in [ROUTERS_DIR, CORE_DIR, GDX_DIR / "tasks", GDX_DIR / "modules"]:
        scan_directory(
            d, pattern,
            "PII-shaped field in log call without redact_ helper — use gdx_dispatch.core.log_redact.redact_email() / redact_jsonb().",
            severity="ERROR",
        )


def check_platform_schema_tests_exist() -> None:
    """Platform SS-3 P13: every declared resource_type has an integrity test.

    When `gdx_dispatch/models/platform.py` (created in SS-2/3 execution) declares a
    resource_descriptors.type_id, `gdx_dispatch/tests/test_shared_resource_integrity.py`
    must contain a matching test case. Until platform.py exists this check
    is a no-op; once it exists it enforces the P13 acceptance criterion.
    """
    print("Checking platform resource_type test coverage (SS-3 P13)...")
    platform_models = CORE_DIR.parent / "models" / "platform.py"
    integrity_test = CORE_DIR.parent / "tests" / "test_shared_resource_integrity.py"
    if not platform_models.exists():
        return  # SS-2/3 not executed yet; check is a no-op
    try:
        src = platform_models.read_text(errors="replace")
    except OSError:
        return
    # Extract declared resource types — conservative regex matches string
    # literals passed to `resource_type=` or in a ResourceDescriptor call.
    declared = set(re.findall(r"resource_type\s*=\s*[\"']([a-z][\w\-]*)[\"']", src))
    if not declared:
        return
    if not integrity_test.exists():
        violations.append(
            f"gdx_dispatch/tests/test_shared_resource_integrity.py: MISSING "
            f"(required by SS-3 P13; {len(declared)} resource_types declared)"
        )
        return
    test_src = integrity_test.read_text(errors="replace")
    missing = [t for t in declared if t not in test_src]
    for t in sorted(missing):
        violations.append(
            f"gdx_dispatch/tests/test_shared_resource_integrity.py: no test case references "
            f"resource_type='{t}' (SS-3 P13 requires one per declared type)"
        )


def main() -> int:
    print(f"\n{'=' * 60}")
    print("GDX Build Rules Drift Scanner")
    print(f"{'=' * 60}\n")

    check_sql_portability()
    check_silent_exceptions()
    check_requirements_sync()
    check_router_registration()
    check_import_logging()
    check_nullable_company_id()
    check_env_template_drift()
    check_audit_logging_coverage()
    check_test_fixture_patterns()
    check_container_dep_sync()
    check_pii_in_logs()
    check_platform_schema_tests_exist()

    print()
    if warnings:
        print(f"{YELLOW}WARNINGS ({len(warnings)}):{RESET}")
        for w in warnings[:20]:
            print(f"  {YELLOW}! {w}{RESET}")
        if len(warnings) > 20:
            print(f"  ... and {len(warnings) - 20} more")

    if violations:
        print(f"\n{RED}VIOLATIONS ({len(violations)}):{RESET}")
        for v in violations:
            print(f"  {RED}X {v}{RESET}")
        print(f"\n{RED}DRIFT DETECTED — fix violations before deploying.{RESET}")
        return 1

    print(f"\n{GREEN}NO DRIFT — all Build Rules pass.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
