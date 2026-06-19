"""
scripts/lint/lint_html.py

HTML accessibility checker using stdlib html.parser.
Checks:
  - <label> must have for= attribute
  - <input>/<select>/<textarea> (non-hidden/submit/button/image/reset) must have id=
  - <img> must have alt=

Usage:
    from scripts.lint.lint_html import lint_files
    issues = lint_files(["archive/dispatch_flask/templates/gdx_leads.html"])
"""

import re
import html.parser
from typing import List, Dict


# ---------------------------------------------------------------------------
# Jinja2 stripping (must happen before feeding to HTMLParser)
# ---------------------------------------------------------------------------
def _strip_jinja(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
    # Replace block tags with whitespace to preserve line numbers
    text = re.sub(r"\{%.*?%\}", lambda m: " " * len(m.group()), text, flags=re.DOTALL)
    # Replace expressions with a safe placeholder attribute value
    text = re.sub(r"\{\{.*?\}\}", "__JINJA__", text, flags=re.DOTALL)
    return text


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
_EXEMPT_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset"}


class A11yChecker(html.parser.HTMLParser):
    def __init__(self, filepath: str):
        super().__init__()
        self.filepath = filepath
        self.issues: List[Dict] = []
        # Stack to track open label tags and whether they wrapped an input
        self._label_stack: List[Dict] = []

    def _issue(self, line: int, message: str) -> None:
        self.issues.append({
            "file": self.filepath,
            "line": line,
            "level": "warning",
            "message": message,
        })

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        line, _ = self.getpos()

        if tag == "label":
            has_for = "for" in attrs_dict
            self._label_stack.append({"line": line, "has_for": has_for, "wrapped_input": False})
            if not has_for:
                # Tentatively flag — may be cleared if label wraps an input
                pass

        elif tag in ("input", "select", "textarea"):
            # Mark any open label as having wrapped an input
            if self._label_stack:
                self._label_stack[-1]["wrapped_input"] = True

            if tag == "input":
                input_type = attrs_dict.get("type", "text").lower()
                if input_type in _EXEMPT_INPUT_TYPES:
                    return

            if "id" not in attrs_dict:
                self._issue(line, f"[HTML] <{tag}> missing id= attribute")

        elif tag == "img":
            if "alt" not in attrs_dict:
                self._issue(line, "[HTML] <img> missing alt= attribute")

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_stack:
            label = self._label_stack.pop()
            if not label["has_for"] and not label["wrapped_input"]:
                self._issue(label["line"], "[HTML] <label> missing for= attribute")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def lint_file(filepath: str) -> List[Dict]:
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    content = _strip_jinja(content)
    checker = A11yChecker(filepath)
    try:
        checker.feed(content)
    except html.parser.HTMLParseError:
        pass  # partial HTML in templates is expected
    return checker.issues


def lint_files(paths: List[str]) -> List[Dict]:
    issues = []
    for p in paths:
        try:
            issues.extend(lint_file(p))
        except Exception as e:
            issues.append({"file": p, "line": 0, "level": "warning",
                           "message": f"[HTML] could not parse file: {e}"})
    return issues
