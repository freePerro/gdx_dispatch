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

export const PRODUCT_CLASS_OPTIONS = Object.values(PRODUCT_CLASSES).map((c) => ({
  label: c.label,
  value: c.key,
  description: c.description,
}));

export function getProductClass(key) {
  return PRODUCT_CLASSES[key] || PRODUCT_CLASSES.parts;
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

export function readField(obj, path) {
  if (!obj || !path) return undefined;
  if (!path.includes('.')) return obj[path];
  return path.split('.').reduce((o, key) => (o == null ? o : o[key]), obj);
}

export function writeField(obj, path, value) {
  if (!path) return;
  if (!path.includes('.')) {
    obj[path] = value;
    return;
  }
  const keys = path.split('.');
  let cursor = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    if (cursor[k] == null || typeof cursor[k] !== 'object') cursor[k] = {};
    cursor = cursor[k];
  }
  cursor[keys[keys.length - 1]] = value;
}
