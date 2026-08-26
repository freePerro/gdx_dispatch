# PDF Line-Item Column Toggles — Plan

**Date:** 2026-08-18
**Status:** PLANNED (not started)
**Ask (Doug):** PDF line items currently show Item / Qty / Price / Total. Add a
Category column, and in the PDF Template Editor make **every** column
individually toggleable on/off.

---

## 1. What already exists (do not rebuild)

The category feature is ~half shipped already, defaulted OFF, which is why the
PDFs don't show it today:

| Piece | State | Where |
|---|---|---|
| `show_category` toggle + `category_display` (column / grouped) | ✅ shipped | `gdx_dispatch/core/pdf_generator.py` (`LINE_ITEM_DEFAULT_SETTINGS`), editor UI in `frontend/src/views/PdfTemplateEditorView.vue:66-74` |
| Category rendering (column mode + grouped-heading mode) | ✅ shipped | `gdx_dispatch/templates/_pdf_line_items.html` |
| `category` on line data for both doc types | ✅ shipped | `gdx_dispatch/routers/pdf.py:195` (estimate), `:372` (invoice) |
| Editor live preview of category column/groups | ✅ shipped | `PdfTemplateEditorView.vue:108-137` |

So "add category" is literally: open Settings → PDF Templates → click the Line
Items block → flip **Show Category** on, save. No code needed for that half.

**The actual gap:** the other columns have no toggles.
- **Description (Item)** — always rendered, no toggle.
- **Qty** — always rendered, no toggle.
- **Unit Price / Line Total** — rendered unless the document-level
  `hide_line_prices` flag is set (owned by estimates feature settings /
  invoice snapshot, *not* the template editor).

## 2. Design

### New settings keys on the `line_items` block

```
show_description: true   # the Item column
show_qty:         true
show_unit_price:  true   # "Price"
show_line_total:  true   # "Total"
```

All four default **True**; `show_category` keeps its default **False**. This
preserves the hard contract stated in `pdf_generator.py`: with
`template_config=None` (most tenants) the output must stay byte-for-byte
identical to the legacy PDF.

Fixed column order (no reordering ask): Category · Description · Qty · Unit
Price · Line Total.

### Rules / edge cases

1. **Missing-key trap (the one real trap).** Doug's tenant has a *saved*
   template row whose `line_items.settings` lacks the new keys. Normalization
   must read them as `bool(settings.get("show_qty", True))` — default-True on
   missing. A plain `.get(key)` would silently blank all four columns for
   every tenant that ever saved a template.
2. **`hide_line_prices` still wins.** It is a per-document privacy flag, not
   layout. Effective visibility for the two price columns is
   `editor_toggle AND NOT data.hide_line_prices`. Tax stays separately stated
   in totals regardless (existing invoice-disclosure comment in
   `invoice_pdf.html` still holds).
3. **All-columns-off guard.** If normalization ends with zero visible columns
   (all four off and category off — or category in *grouped* mode, which
   contributes no column), fall back to the default column set. A zero-column
   table is never a valid render; the block-level `visible` toggle is the
   sanctioned way to hide line items entirely.
4. **Grouped-heading colspan.** `n_cols` in `_pdf_line_items.html` is
   currently `2 + cat + prices`; it becomes the count of actually-visible
   columns. Same fix in the editor preview, which hard-codes `colspan="4"`.

## 3. Changes by file

### Backend

- **`gdx_dispatch/core/pdf_generator.py`**
  - Extend `LINE_ITEM_DEFAULT_SETTINGS` with the four new keys (True).
  - In `_normalize_template_config`, normalize them with default-True
    semantics (rule 1) and apply the all-off fallback (rule 3).
- **`gdx_dispatch/templates/_pdf_line_items.html`**
  - Gate each `<th>`/`<td>` (Description, Qty, Unit Price, Line Total) on its
    flag; price columns additionally on `not data.hide_line_prices` as today.
  - Compute `n_cols` from the visible set; pass the flags into `line_row`.
- **No router change:** `BlockConfig.settings` is `dict[str, Any]` — the save
  endpoint already round-trips arbitrary settings. **No migration:** settings
  live in the `pdf_templates.blocks` JSON blob. No OpenAPI change.

### Frontend

- **`frontend/src/views/PdfTemplateEditorView.vue`**
  - Add four ToggleSwitches to the Line Items block-settings panel
    (`data-testid`: `li-show-description`, `li-show-qty`, `li-show-price`,
    `li-show-total`), alongside the existing Show Category controls.
  - Extend `LINE_ITEM_DEFAULTS` (the existing
    `{...LINE_ITEM_DEFAULTS, ...saved}` merge already handles old configs).
  - Preview table: gate Item/Qty/Price/Total headers + cells on the settings;
    compute the grouped-heading colspan instead of the hard-coded 4.
  - Keep the existing hint that price visibility can also be forced off by
    the estimate/invoice "hide line prices" setting; reword to "these
    toggles hide the columns for all documents; hide-line-prices hides
    prices per document".

### Tests

- **`gdx_dispatch/tests/test_pdf_template_render.py`** (follow existing
  patterns, e.g. `test_category_column`, `test_hide_line_prices_still_gates_price_columns`):
  1. No-config render still contains all four legacy columns (extends the
     existing byte-compat tests).
  2. Saved config **without** the new keys (old editor version) renders all
     four columns — the missing-key trap test.
  3. Each toggle independently removes exactly its column (th + td).
  4. `hide_line_prices=True` + `show_unit_price=True` still hides prices.
  5. Grouped mode: heading colspan equals visible column count when qty is
     toggled off.
  6. All toggles off → default columns render (fallback guard).
- **`frontend/src/views/__tests__/PdfTemplateEditorView.spec.js`**: toggles
  render for the line_items block; flipping `show_qty` removes the Qty header
  from the preview; grouped preview colspan matches.

## 4. Verification

1. Backend: targeted pytest (`test_pdf_template_render.py`,
   `serial/test_pdf_templates.py`) via the docker-app harness, then the full
   local matrix (incl. LINT ratchet) before any merge.
2. Frontend: `vitest` for the editor spec.
3. Browser walk (`/verifyplaywright`, headed, real account): open the editor,
   flip toggles, confirm live preview; save; download a real estimate PDF and
   a real invoice PDF and confirm the column set matches; confirm an
   *unsaved* tenant type (e.g. work_order defaults) is unaffected.
4. Manifest before commit.

## 5. After it ships (Doug's config, not code)

Doug's stated target — Category, Qty, Price, Total — is reached in the editor:
Show Category ON (column mode), and optionally Show Item OFF. **Flag:** turning
off Description means customer-facing docs list quantities and prices with no
statement of *what* the line is beyond its category ("Door — 1 — $1,830").
Recommend walking one real estimate with Description off before adopting it;
the toggle makes both looks a save away either way.

## 6. Size

Small. Two backend files + one template, one Vue view, tests. No schema, no
API surface, no migration. One PR.
