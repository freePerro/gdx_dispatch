from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_JINJA_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

# ---------------------------------------------------------------------------
# Template-editor config (Settings → PDF Templates)
#
# The editor stores a per-tenant config row (pdf_templates table) that the
# renderer consumes here. Everything is optional: template_config=None must
# reproduce the pre-editor output byte-for-byte, because most tenants have
# never saved a template.
# ---------------------------------------------------------------------------

# Canonical block defaults. routers/pdf_templates.py serves these to the
# editor for unconfigured types, and _normalize_template_config falls back to
# them per-block, so editor defaults and rendered defaults can never drift.
# Signature differs by type: the estimate PDF has always printed a signature
# line; the invoice PDF never has.
LINE_ITEM_DEFAULT_SETTINGS: dict[str, Any] = {
    "show_category": False,
    "category_display": "column",  # 'column' | 'grouped'
    "show_taxable_marker": False,
}


def default_blocks(template_type: str) -> list[dict[str, Any]]:
    return [
        {"id": "logo", "type": "logo", "order": 1, "visible": True, "styles": {}, "settings": {}},
        {"id": "company_info", "type": "company_info", "order": 2, "visible": True, "styles": {}, "settings": {}},
        {"id": "customer_info", "type": "customer_info", "order": 3, "visible": True, "styles": {}, "settings": {}},
        {"id": "line_items", "type": "line_items", "order": 4, "visible": True, "styles": {},
         "settings": dict(LINE_ITEM_DEFAULT_SETTINGS)},
        {"id": "totals", "type": "totals", "order": 5, "visible": True, "styles": {}, "settings": {}},
        # Terms BEFORE notes — the legacy estimate PDF printed them in that
        # order, and no-config output must not change (audit catch 2026-07-21).
        {"id": "terms", "type": "terms", "order": 6, "visible": True, "styles": {}, "settings": {}},
        {"id": "notes", "type": "notes", "order": 7, "visible": True, "styles": {}, "settings": {}},
        {"id": "signature", "type": "signature", "order": 8,
         "visible": template_type == "estimate", "styles": {}, "settings": {}},
    ]


# Font names come from the editor's fixed dropdown; anything else (or a
# hand-edited DB value) falls back to the legacy stack. Mapping through this
# allowlist also keeps arbitrary strings out of the rendered CSS.
_FONT_STACKS = {
    "Helvetica": "Helvetica, Arial, sans-serif",
    "Arial": "Arial, Helvetica, sans-serif",
    "Times New Roman": "'Times New Roman', Times, serif",
    "Georgia": "Georgia, 'Times New Roman', serif",
    "Courier New": "'Courier New', Courier, monospace",
}
_LEGACY_FONT_STACK = "Arial, sans-serif"

# PrimeVue's ColorPicker emits hex WITHOUT the leading '#' while the paired
# InputText usually carries one — accept both. Reject anything that isn't a
# plain 3/6-digit hex so no saved value can inject CSS.
_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")

# Blocks rendered by the body loop, in legacy order. logo/company_info live
# in the fixed header band and are visibility-only. signature is also NOT in
# the loop: the legacy estimate printed it dead last, after the attachment
# sections, so the templates render it after everything (visibility+style
# still honored; ordering it mid-document isn't supported).
_BODY_BLOCK_TYPES = ("customer_info", "line_items", "totals", "terms", "notes")
_ALIGNMENTS = {"left", "center", "right"}


def _block_sort_key(block: dict[str, Any]) -> float:
    value = block.get("order")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _style_css(styles: Any) -> str:
    if not isinstance(styles, dict):
        return ""
    parts: list[str] = []
    size = styles.get("font_size")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and 6 <= size <= 48:
        parts.append(f"font-size: {int(size)}pt")
    alignment = styles.get("alignment")
    if alignment in _ALIGNMENTS:
        parts.append(f"text-align: {alignment}")
    return "; ".join(parts)


def _normalize_template_config(template_config: dict[str, Any] | None, template_type: str) -> dict[str, Any]:
    cfg = template_config if isinstance(template_config, dict) else {}
    raw_blocks = cfg.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raw_blocks = default_blocks(template_type)

    by_type: dict[str, dict[str, Any]] = {}
    saved_order: list[str] = []
    sortable = [b for b in raw_blocks if isinstance(b, dict) and isinstance(b.get("type"), str)]
    for block in sorted(sortable, key=_block_sort_key):
        btype = block["type"]
        if btype in by_type:
            continue
        by_type[btype] = {
            "visible": bool(block.get("visible", True)),
            "style": _style_css(block.get("styles")),
            "settings": block.get("settings") if isinstance(block.get("settings"), dict) else {},
        }
        if btype in _BODY_BLOCK_TYPES:
            saved_order.append(btype)
    # A saved config missing a known block (older editor version) inherits
    # that block's default, appended after the blocks the user DID order.
    for block in default_blocks(template_type):
        by_type.setdefault(block["type"], {
            "visible": block["visible"], "style": "", "settings": dict(block["settings"]),
        })
    for btype in _BODY_BLOCK_TYPES:
        if btype not in saved_order:
            saved_order.append(btype)

    li_settings = by_type["line_items"]["settings"]
    category_display = li_settings.get("category_display")
    line_items = {
        "show_category": bool(li_settings.get("show_category")),
        "category_display": category_display if category_display in ("column", "grouped") else "column",
        "show_taxable_marker": bool(li_settings.get("show_taxable_marker")),
    }

    accent = None
    match = _HEX_COLOR_RE.match(str(cfg.get("brand_color") or "").strip())
    if match:
        accent = f"#{match.group(1)}"

    return {
        # None → templates fall back to branding.primary_color
        "accent": accent,
        "font_stack": _FONT_STACKS.get(cfg.get("font_family") or "", _LEGACY_FONT_STACK),
        "header_content": str(cfg.get("header_content") or ""),
        "footer_content": str(cfg.get("footer_content") or ""),
        "show_logo": by_type["logo"]["visible"],
        "show_company_info": by_type["company_info"]["visible"],
        "body_blocks": [
            {"type": btype, "style": by_type[btype]["style"]}
            for btype in saved_order if by_type[btype]["visible"]
        ],
        "signature": {
            "visible": by_type["signature"]["visible"],
            "style": by_type["signature"]["style"],
        },
        "line_items": line_items,
    }


def _group_lines(lines: Any) -> list[dict[str, Any]]:
    """Bucket lines by category, preserving first-appearance order (Jinja's
    groupby sorts alphabetically, which would shuffle the operator's line
    order). Uncategorized lines keep category '' — the template skips the
    heading row for that group."""
    groups: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for line in lines or []:
        category = str(line.get("category") or "").strip()
        if category not in index:
            index[category] = len(groups)
            groups.append({"category": category, "lines": []})
        groups[index[category]]["lines"].append(line)
    return groups


def _default_branding(tenant_branding: dict[str, Any] | None) -> dict[str, Any]:
    branding = dict(tenant_branding or {})
    return {
        "company_name": branding.get("company_name") or "",
        "logo": branding.get("logo") or "",
        "primary_color": branding.get("primary_color") or "#0f172a",
        "secondary_color": branding.get("secondary_color") or "#2563eb",
        "address": branding.get("address") or "",
    }


def _render_template(
    template_name: str,
    document_data: dict[str, Any],
    tenant_branding: dict[str, Any],
    template_config: dict[str, Any] | None = None,
    template_type: str = "estimate",
) -> str:
    tpl = _normalize_template_config(template_config, template_type)
    data = dict(document_data or {})
    if tpl["line_items"]["show_category"] and tpl["line_items"]["category_display"] == "grouped":
        data["line_groups"] = _group_lines(data.get("lines"))
    template = _JINJA_ENV.get_template(template_name)
    return template.render(data=data, branding=_default_branding(tenant_branding), tpl=tpl)


def generate_estimate_pdf(
    estimate_data: dict[str, Any],
    tenant_branding: dict[str, Any],
    template_config: dict[str, Any] | None = None,
) -> bytes:
    html = _render_template("estimate_pdf.html", estimate_data, tenant_branding, template_config, "estimate")
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()


def generate_invoice_pdf(
    invoice_data: dict[str, Any],
    tenant_branding: dict[str, Any],
    template_config: dict[str, Any] | None = None,
) -> bytes:
    html = _render_template("invoice_pdf.html", invoice_data, tenant_branding, template_config, "invoice")
    return HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf()
