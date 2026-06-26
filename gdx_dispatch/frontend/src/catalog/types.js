/**
 * Typed-catalog registry — Class Table Inheritance frontend mapping.
 *
 * One entry per backend product_class. Drives the New Catalog dialog's Type
 * select, the Item form fields, and the DataTable columns. Adding a new
 * product class is a one-entry change; no CatalogView.vue edits required.
 *
 * See ai-queue/plans/sprint_typed_product_catalogs.md for the architecture
 * rationale (typed tables, not JSONB).
 */

const SPINE_COLUMNS = [
  { field: 'sku', header: 'SKU', sortable: true, style: 'width:140px' },
  { field: 'name', header: 'Name', sortable: true },
];

const SPINE_PRICE_COLUMNS = [
  { field: 'cost', header: 'Cost', sortable: true, style: 'width:100px', format: 'currency' },
  { field: 'price', header: 'Retail', sortable: true, style: 'width:100px', format: 'currency' },
];

const SPINE_FIELDS = [
  { name: 'sku', label: 'SKU', type: 'text', section: 'identity' },
  { name: 'name', label: 'Name', type: 'text', required: true, section: 'identity' },
  { name: 'description', label: 'Description', type: 'textarea', section: 'identity', fullWidth: true },
  { name: 'cost', label: 'Cost', type: 'currency', section: 'pricing' },
  { name: 'price', label: 'Retail Price', type: 'currency', section: 'pricing' },
];

export const PRODUCT_CLASSES = {
  parts: {
    key: 'parts',
    label: 'Parts',
    description: 'Hardware, accessories, miscellaneous SKUs.',
    formFields: [
      ...SPINE_FIELDS,
      { name: 'category', label: 'Category', type: 'text', section: 'identity' },
      { name: 'qb_item_id', label: 'QB Item ID', type: 'text', section: 'integration' },
    ],
    tableColumns: [
      ...SPINE_COLUMNS,
      { field: 'description', header: 'Description' },
      ...SPINE_PRICE_COLUMNS,
      { field: 'category', header: 'Category', style: 'width:120px' },
    ],
    defaultSpec: null,
  },

  door: {
    key: 'door',
    label: 'Doors',
    description: 'Garage doors with full install attributes (size, R-value, panel style, finish).',
    formFields: [
      ...SPINE_FIELDS,
      { name: 'spec.manufacturer', label: 'Manufacturer', type: 'text', section: 'spec' },
      { name: 'spec.model_number', label: 'Model #', type: 'text', section: 'spec' },
      { name: 'spec.door_type', label: 'Door Type', type: 'text', section: 'spec' },
      { name: 'spec.width', label: 'Width (in)', type: 'number', section: 'dimensions' },
      { name: 'spec.height', label: 'Height (in)', type: 'number', section: 'dimensions' },
      { name: 'spec.color', label: 'Color', type: 'text', section: 'appearance' },
      { name: 'spec.panel_style', label: 'Panel Style', type: 'text', section: 'appearance' },
      { name: 'spec.finish_type', label: 'Finish', type: 'text', section: 'appearance' },
      { name: 'spec.insulation_type', label: 'Insulation Type', type: 'text', section: 'construction' },
      { name: 'spec.r_value', label: 'R-Value', type: 'number', section: 'construction' },
      { name: 'spec.section_construction', label: 'Section Construction', type: 'text', section: 'construction' },
      { name: 'spec.section_thickness_in', label: 'Section Thickness (in)', type: 'number', section: 'construction' },
      { name: 'spec.section_sides', label: 'Section Sides', type: 'number', section: 'construction' },
      { name: 'spec.section_material', label: 'Section Material', type: 'text', section: 'construction' },
      { name: 'spec.window_option', label: 'Windows? (Y/N)', type: 'text', section: 'windows' },
      { name: 'spec.window_rows', label: 'Window Rows', type: 'number', section: 'windows' },
      { name: 'spec.window_type', label: 'Window Type', type: 'text', section: 'windows' },
      { name: 'spec.high_lift', label: 'High Lift? (Y/N)', type: 'text', section: 'lift' },
      { name: 'spec.high_lift_in', label: 'High Lift (in)', type: 'number', section: 'lift' },
      { name: 'spec.sales_talking_point', label: 'Sales Talking Point', type: 'textarea', section: 'spec', fullWidth: true },
    ],
    tableColumns: [
      ...SPINE_COLUMNS,
      { field: 'spec.manufacturer', header: 'Mfr', style: 'width:100px' },
      { field: 'spec.width', header: 'W', style: 'width:60px' },
      { field: 'spec.height', header: 'H', style: 'width:60px' },
      { field: 'spec.color', header: 'Color', style: 'width:90px' },
      { field: 'spec.r_value', header: 'R', style: 'width:60px' },
      ...SPINE_PRICE_COLUMNS,
    ],
    defaultSpec: {},
  },
};

// ADR-015 — the "Custom…" choice in the New Catalog dialog. Unlike the built-in
// classes, a custom catalog carries its own field_schema (defined by the user at
// create time), so there is no static registry entry for it.
export const CUSTOM_CLASS_OPTION = {
  label: 'Custom…',
  value: 'custom',
  description: 'Define your own fields — for any industry (HVAC, electrical, …).',
};

export const PRODUCT_CLASS_OPTIONS = [
  ...Object.values(PRODUCT_CLASSES).map((c) => ({
    label: c.label,
    value: c.key,
    description: c.description,
  })),
  CUSTOM_CLASS_OPTION,
];

// ADR-015 — custom field type → form input + table format. Keep keys in sync
// with the backend CUSTOM_FIELD_TYPES set in routers/catalog.py.
export const FIELD_INPUTS = {
  text: { input: 'text' },
  textarea: { input: 'textarea', fullWidth: true },
  number: { input: 'number' },
  currency: { input: 'currency', format: 'currency' },
  checkbox: { input: 'checkbox' },
  select: { input: 'select' },
  date: { input: 'date' },
};

export function getProductClass(key) {
  return PRODUCT_CLASSES[key] || PRODUCT_CLASSES.parts;
}

// ADR-015 — build a registry-shaped object from a custom catalog's field_schema
// so the same form/table machinery renders it. Built-in classes fall through to
// the static registry above.
export function buildCustomClass(catalog) {
  const schema = (catalog && catalog.field_schema) || [];
  const customFields = schema.map((f) => ({
    name: `attributes.${f.name}`,
    label: f.label || f.name,
    type: (FIELD_INPUTS[f.type] || FIELD_INPUTS.text).input,
    section: 'attributes',
    required: !!f.required,
    options: f.options || undefined,
    fullWidth: (FIELD_INPUTS[f.type] || {}).fullWidth || false,
  }));
  const customColumns = schema.slice(0, 5).map((f) => ({
    field: `attributes.${f.name}`,
    header: f.label || f.name,
    format: (FIELD_INPUTS[f.type] || {}).format,
  }));
  return {
    key: 'custom',
    label: (catalog && catalog.name) || 'Custom',
    singular: 'Item',
    description: CUSTOM_CLASS_OPTION.description,
    formFields: [...SPINE_FIELDS, ...customFields],
    tableColumns: [...SPINE_COLUMNS, ...customColumns, ...SPINE_PRICE_COLUMNS],
    defaultSpec: null,
    isCustom: true,
  };
}

// ADR-015 — resolve the rendering definition for a catalog: custom catalogs are
// data-driven from their field_schema; everything else uses the static registry.
export function getCatalogClass(catalog) {
  if (catalog && catalog.product_class === 'custom') return buildCustomClass(catalog);
  return getProductClass(catalog && catalog.product_class);
}

export function emptyItemForClass(key) {
  const klass = getProductClass(key);
  const item = {
    sku: '',
    name: '',
    description: '',
    cost: 0,
    price: 0,
    category: '',
    qb_item_id: '',
  };
  if (klass.defaultSpec !== null) {
    item.spec = { ...klass.defaultSpec };
  }
  return item;
}

// ADR-015 — empty item for a specific catalog (custom catalogs need an
// `attributes` bag for their user-defined fields).
export function emptyItemForCatalog(catalog) {
  const item = emptyItemForClass(catalog && catalog.product_class);
  if (catalog && catalog.product_class === 'custom') item.attributes = {};
  return item;
}

// ADR-015 Slice 3 — a Catalog Pack type is a template. Creating a catalog from
// it produces a self-contained custom catalog: the pack's field_schema + pricing
// are copied onto the new catalog, so it needs no pack code at run time.
export function createPayloadFromPackType(name, source, packType) {
  return {
    name,
    source: source || 'manual',
    product_class: 'custom',
    field_schema: packType.field_schema || [],
    pricing_strategy: packType.pricing_strategy || 'manual',
    pricing_config: packType.pricing_config || {},
  };
}

export function readField(obj, path) {
  if (!obj || !path) return undefined;
  if (!path.includes('.')) return obj[path];
  return path.split('.').reduce((o, key) => (o == null ? o : o[key]), obj);
}

// Reject keys that would let a crafted path walk into the prototype chain.
const UNSAFE_KEY = /^(__proto__|prototype|constructor)$/;

export function writeField(obj, path, value) {
  if (!path) return;
  if (!path.includes('.')) {
    if (UNSAFE_KEY.test(path)) return;
    obj[path] = value;
    return;
  }
  const keys = path.split('.');
  if (keys.some((k) => UNSAFE_KEY.test(k))) return;
  let cursor = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    if (cursor[k] == null || typeof cursor[k] !== 'object') cursor[k] = {};
    cursor = cursor[k];
  }
  cursor[keys[keys.length - 1]] = value;
}
