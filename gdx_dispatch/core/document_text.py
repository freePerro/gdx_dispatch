"""Text extraction for documents.summarize AI tool.

Pure-Python: pypdf for PDF, python-docx for DOCX, plain decode for text.
Returns ("", reason) for unsupported types or failures so the caller
can surface a graceful "no extractable text" instead of crashing.
"""
from __future__ import annotations

from pathlib import Path

# Soft cap on returned text — keeps token usage bounded when summarize
# feeds the result to Anthropic. ~25k chars ≈ 6k tokens; well under
# Haiku's window even with the user's question + system prompt.
MAX_CHARS = 25_000

_TEXT_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/json",
    "application/xml",
    "text/xml",
}


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CHARS:
        return text, False
    return text[:MAX_CHARS], True


def extract_text(path: Path | str, content_type: str | None) -> tuple[str, bool, str | None]:
    """Return (text, truncated, error). Empty text + error for unsupported types.

    `truncated` is True if MAX_CHARS clipped the output.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return "", False, "file not found"

    ct = (content_type or "").lower()
    suffix = p.suffix.lower()

    try:
        if ct == "application/pdf" or suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(p))
            chunks = []
            for page in reader.pages:
                chunks.append(page.extract_text() or "")
            text, truncated = _truncate("\n".join(chunks).strip())
            return text, truncated, None

        if (
            ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or suffix == ".docx"
        ):
            import docx

            doc = docx.Document(str(p))
            chunks = [para.text for para in doc.paragraphs]
            text, truncated = _truncate("\n".join(chunks).strip())
            return text, truncated, None

        if ct in _TEXT_CONTENT_TYPES or suffix in {".txt", ".md", ".csv", ".json", ".xml", ".log"}:
            raw = p.read_bytes()
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                decoded = raw.decode("latin-1", errors="replace")
            text, truncated = _truncate(decoded.strip())
            return text, truncated, None

        return "", False, f"unsupported content_type {ct or 'unknown'} ({suffix or 'no extension'})"
    except Exception as exc:  # noqa: BLE001 — surface the reason to the AI
        return "", False, f"extraction failed: {exc.__class__.__name__}: {exc}"
