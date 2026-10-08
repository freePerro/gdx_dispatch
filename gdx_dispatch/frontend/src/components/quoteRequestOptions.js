/**
 * The door questions a portal quote request asks, and the words each answer
 * is shown in — shared by the customer's form and the staff view, so both
 * sides read the same answer the same way. Values mirror the Literal types in
 * gdx_dispatch/modules/quote_requests/service.py; a value added there needs a
 * label here.
 */

const NOT_SURE = { value: 'not_sure', label: 'Not sure' };

export const DOOR_QUESTIONS = [
  {
    key: 'placement',
    label: 'Replacing a door, or a new opening?',
    options: [
      { value: 'replace', label: 'Replacing a door' },
      { value: 'new_opening', label: 'New opening' },
      NOT_SURE,
    ],
  },
  {
    key: 'material',
    label: 'Material',
    options: [
      { value: 'steel', label: 'Steel' },
      { value: 'wood', label: 'Wood' },
      { value: 'wood_composite', label: 'Wood-look composite' },
      { value: 'aluminum_glass', label: 'Aluminum and glass' },
      NOT_SURE,
    ],
  },
  {
    key: 'style',
    label: 'Style',
    options: [
      { value: 'traditional', label: 'Traditional (raised panel)' },
      { value: 'carriage_house', label: 'Carriage house' },
      { value: 'modern', label: 'Modern / flush' },
      NOT_SURE,
    ],
  },
  {
    key: 'insulation',
    label: 'Insulation',
    options: [
      { value: 'none', label: 'None' },
      { value: 'insulated', label: 'Insulated' },
      { value: 'best', label: 'Best available' },
      NOT_SURE,
    ],
  },
  {
    key: 'windows',
    label: 'Windows',
    options: [
      { value: 'none', label: 'No windows' },
      { value: 'top_row', label: 'Top row' },
      { value: 'full_view', label: 'Full view (all glass)' },
      NOT_SURE,
    ],
  },
  {
    key: 'opener',
    label: 'Need an opener too?',
    options: [
      { value: 'yes', label: 'Yes' },
      { value: 'no', label: 'No' },
      NOT_SURE,
    ],
  },
];

export const MAX_DOORS = 10;
export const MAX_PHOTOS_PER_DOOR = 4;

export function answerLabel(key, value) {
  const q = DOOR_QUESTIONS.find((x) => x.key === key);
  const opt = q?.options.find((o) => o.value === value);
  return opt ? opt.label : value || '';
}

/** "2 × 16' 0" x 7' 0"" — the size line a door is listed under. */
export function doorTitle(door) {
  // The server holds quantity to 1..10; read it as stored, never default it.
  const qty = Number(door.quantity);
  return qty === 1 ? door.size_label : `${qty} × ${door.size_label}`;
}

/** The answers worth showing: "Not sure" is left out, it says nothing. */
export function doorAnswers(door) {
  return DOOR_QUESTIONS
    .filter((q) => door[q.key] && door[q.key] !== 'not_sure')
    .map((q) => ({ key: q.key, question: q.label, answer: answerLabel(q.key, door[q.key]) }));
}

export function blankDoor() {
  return {
    quantity: 1,
    width_ft: null,
    width_in: 0,
    height_ft: null,
    height_in: 0,
    placement: 'not_sure',
    material: 'not_sure',
    style: 'not_sure',
    insulation: 'not_sure',
    windows: 'not_sure',
    opener: 'not_sure',
    color: '',
    notes: '',
  };
}
