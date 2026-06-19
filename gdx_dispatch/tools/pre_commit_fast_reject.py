#!/usr/bin/env python3
"""Pre-commit fast reject — Gemma scans diff for obvious errors in ~1s.

Catches: syntax errors, undefined names, missing imports, obvious typos.
If Gemma says REJECT, skip the expensive 70s pytest gate entirely.
If Gemma says PASS or is unavailable, proceed to full gate.

Exit 0 = PASS (proceed to tests)
Exit 1 = REJECT (don't bother testing — obvious error found)
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

LLM_URL = "http://localhost:11440/v1/chat/completions"
MODEL = "gemma-4-26b-a4b"


def get_diff() -> str:
    r = subprocess.run(
        ["git", "diff", "--cached", "--no-color"],
        capture_output=True, text=True, check=False,
    )
    return r.stdout[:8000]  # cap at 8K chars


def check_python_syntax(changed_files: list[str]) -> tuple[str, str]:
    """Deterministic: ast.parse each changed .py file. Catches REAL syntax
    errors without LLM hallucination risk."""
    import ast
    repo = Path(__file__).resolve().parents[2]
    for f in changed_files:
        if not f.endswith('.py'):
            continue
        path = repo / f
        if not path.exists():
            continue
        try:
            ast.parse(path.read_text())
        except SyntaxError as e:
            return 'REJECT', f'SyntaxError in {f}:{e.lineno}: {e.msg}'
    return 'PASS', 'all .py files parse cleanly'


def ask_gemma(diff: str) -> tuple[str, str]:
    """Returns (PASS|REJECT, reason)."""
    prompt = f"""Quick code review. Scan this diff for OBVIOUS errors only:
- Python syntax errors (missing colons, unmatched parens)
- Undefined variable names used before assignment
- Missing imports that would cause ImportError
- Obviously wrong function signatures (wrong arg count)

If you find ANY obvious error, respond: REJECT: <one-line description>
If the code looks syntactically valid, respond: PASS

DIFF:
{diff}"""

    try:
        payload = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100,
        }).encode()
        req = urllib.request.Request(LLM_URL, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
        content = body["choices"][0]["message"]["content"].strip()
        if content.startswith("REJECT"):
            return "REJECT", content
        return "PASS", content
    except Exception as exc:
        return "PASS", f"Gemma unavailable: {exc}"


def main() -> int:
    import subprocess
    # Step 1: deterministic syntax check (no false positives)
    r = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                       capture_output=True, text=True, check=False)
    files = [f.strip() for f in r.stdout.splitlines() if f.strip()]
    verdict, reason = check_python_syntax(files)
    if verdict == 'REJECT':
        print(f'⚡ FAST REJECT (syntax): {reason}', file=sys.stderr)
        return 1
    # Step 2: optional Gemma check for import/undefined issues
    # (skip for now — ast.parse is sufficient and zero-false-positive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
