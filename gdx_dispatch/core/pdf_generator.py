from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_JINJA_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def _default_branding(tenant_branding: dict[str, Any] | None) -> dict[str, Any]:
    branding = dict(tenant_branding or {})
    return {
        "company_name": branding.get("company_name") or "",
        "logo": branding.get("logo") or "",
        "primary_color": branding.get("primary_color") or "#0f172a",
        "secondary_color": branding.get("secondary_color") or "#2563eb",
        "address": branding.get("address") or "",
    }


def _render_template(template_name: str, document_data: dict[str, Any], tenant_branding: dict[str, Any]) -> str:
    template = _JINJA_ENV.get_template(template_name)
    return template.render(data=document_data, branding=_default_branding(tenant_branding))


def generate_estimate_pdf(estimate_data: dict[str, Any], tenant_branding: dict[str, Any]) -> bytes:
    html = _render_template("estimate_pdf.html", estimate_data, tenant_branding)
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()


def generate_invoice_pdf(invoice_data: dict[str, Any], tenant_branding: dict[str, Any]) -> bytes:
    html = _render_template("invoice_pdf.html", invoice_data, tenant_branding)
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()

