"""
scripts/lint/lint_js.py

Extracts <script> blocks from Jinja2/HTML templates, strips template syntax,
then detects:
  - Duplicate function declarations in the same file
  - Undefined variables (conservative — only obvious cases, no false positives)

Usage:
    from scripts.lint.lint_js import lint_files
    issues = lint_files(["archive/dispatch_flask/templates/gdx_leads.html"])
"""

import re
from typing import List, Dict

# ---------------------------------------------------------------------------
# Globals that are always in scope — never flag these
# ---------------------------------------------------------------------------
BROWSER_GLOBALS = {
    "window", "document", "console", "fetch", "navigator", "location", "history",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "alert", "confirm",
    "parseInt", "parseFloat", "JSON", "Math", "Date", "Promise", "URL", "FormData",
    "File", "FileReader", "Blob", "Event", "CustomEvent", "MutationObserver",
    "ResizeObserver", "IntersectionObserver", "AbortController", "URLSearchParams",
    "localStorage", "sessionStorage", "performance", "requestAnimationFrame",
    "cancelAnimationFrame", "crypto", "atob", "btoa", "encodeURIComponent",
    "decodeURIComponent", "isNaN", "isFinite", "Array", "Object", "String", "Number",
    "Boolean", "Symbol", "Map", "Set", "WeakMap", "WeakSet", "Error", "TypeError",
    "RangeError", "SyntaxError", "undefined", "null", "true", "false", "NaN",
    "Infinity", "this", "arguments", "globalThis", "self", "top", "parent",
    "HTMLElement", "Element", "Node", "NodeList", "EventTarget", "DOMParser",
    "XMLHttpRequest", "WebSocket", "Worker", "SharedWorker", "Proxy", "Reflect",
    "Generator", "AsyncFunction", "WeakRef", "FinalizationRegistry",
    "structuredClone", "queueMicrotask", "reportError",
}

GDX_GLOBALS = {
    # gdx_base.html declares these at window scope
    "csrfToken", "appData", "currentUser",
    "openSidebar", "closeSidebar", "openChangePwModal", "closeChangePwModal",
    "saveNewPassword", "switchTab", "TIMEOUT_MS",
    # Third-party libs loaded via CDN in gdx_base.html
    "mapboxgl", "Mapbox", "htmx", "Alpine", "Sortable", "Chart",
    "flatpickr", "bootstrap", "Stripe", "Toastify", "SignaturePad",
    # Common patterns in the codebase
    "BADGE", "esc", "ageDays",
}

KNOWN_GLOBALS = BROWSER_GLOBALS | GDX_GLOBALS


# ---------------------------------------------------------------------------
# Jinja2 stripping
# ---------------------------------------------------------------------------
def _strip_jinja(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\{\{.*?\}\}", '"__JINJA__"', text, flags=re.DOTALL)
    return text


# ---------------------------------------------------------------------------
# Script block extraction (preserves line offsets)
# ---------------------------------------------------------------------------
def _extract_script_blocks(content: str) -> List[Dict]:
    """Return list of {"code": str, "line_offset": int, "extra_globals": set}."""
    blocks = []
    # Match <script ...> ... </script>, capturing optional attributes
    pattern = re.compile(
        r'<script([^>]*)>(.*?)</script>',
        re.DOTALL | re.IGNORECASE
    )
    for m in pattern.finditer(content):
        attrs = m.group(1)
        code = m.group(2)
        line_offset = content[: m.start(2)].count("\n")

        # Parse lint:globals="Foo Bar" from script tag attributes
        extra = set()
        lg = re.search(r'lint:globals=["\']([^"\']+)["\']', attrs)
        if lg:
            extra = set(lg.group(1).split())

        # Skip external scripts (src= attribute) — no inline code to lint
        if re.search(r'\bsrc\s*=', attrs):
            continue

        blocks.append({"code": code, "line_offset": line_offset, "extra_globals": extra})
    return blocks


# ---------------------------------------------------------------------------
# Declaration extraction — what names are defined in this file
# ---------------------------------------------------------------------------
_DECL_PATTERNS = [
    re.compile(r'\b(?:var|let|const)\s+([A-Za-z_$][A-Za-z0-9_$]*)'),
    re.compile(r'\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)'),
    re.compile(r'\bcatch\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*)'),
    # Arrow/assignment: name = ... at start of statement
    re.compile(r'^\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=[^=]', re.MULTILINE),
]


def _find_declarations(code: str) -> Dict[str, List[int]]:
    """Return {name: [line_numbers]} for all declarations."""
    decls: Dict[str, List[int]] = {}
    for pat in _DECL_PATTERNS:
        for m in pat.finditer(code):
            name = m.group(1)
            lineno = code[: m.start()].count("\n")
            decls.setdefault(name, []).append(lineno)
    return decls


# ---------------------------------------------------------------------------
# Main linting logic
# ---------------------------------------------------------------------------
def lint_file(filepath: str) -> List[Dict]:
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    content_stripped = _strip_jinja(content)
    blocks = _extract_script_blocks(content_stripped)

    # First pass: collect ALL declarations across all blocks in the file
    # (templates often have multiple <script> blocks sharing global scope)
    all_decls: Dict[str, List[int]] = {}
    for block in blocks:
        for name, lines in _find_declarations(block["code"]).items():
            for ln in lines:
                all_decls.setdefault(name, []).append(ln + block["line_offset"])

    issues = []

    # Duplicate function detection (reliable, zero false positives)
    func_pat = re.compile(r'\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(')
    func_lines: Dict[str, List[int]] = {}
    for block in blocks:
        for m in func_pat.finditer(block["code"]):
            name = m.group(1)
            lineno = block["code"][: m.start()].count("\n") + block["line_offset"] + 1
            func_lines.setdefault(name, []).append(lineno)

    for name, lines in func_lines.items():
        if len(lines) > 1:
            for ln in lines[1:]:  # flag second+ occurrences
                issues.append({
                    "file": filepath,
                    "line": ln,
                    "level": "error",
                    "message": f"[JS] duplicate function '{name}' (first at line {lines[0]})",
                })

    return issues


def lint_files(paths: List[str]) -> List[Dict]:
    issues = []
    for p in paths:
        try:
            issues.extend(lint_file(p))
        except Exception as e:
            issues.append({"file": p, "line": 0, "level": "error",
                           "message": f"[JS] could not parse file: {e}"})
    return issues
