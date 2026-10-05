// Line categories on the customer's web pages (approval page, portal) follow
// the estimate PDF's template setting, sent by the API as `line_category`:
// 'off' | 'column' | 'grouped'. These helpers mirror the PDF's own grouping
// (core/pdf_generator.py _group_lines) so the page and the PDF show the same
// document.

/** The mode the page should render; anything unknown is 'off'. */
export function lineCategoryMode(value) {
  return value === "column" || value === "grouped" ? value : "off";
}

/**
 * Bucket lines by category in first-appearance order — NOT alphabetical, so
 * the operator's line order survives, exactly as the PDF does it. Lines with
 * no category form the '' group, which renders without a heading.
 * @returns {{category: string, lines: object[]}[]}
 */
export function groupLinesByCategory(lines) {
  const groups = [];
  const index = new Map();
  for (const line of lines || []) {
    const category = String(line?.category ?? "").trim();
    if (!index.has(category)) {
      index.set(category, groups.length);
      groups.push({ category, lines: [] });
    }
    groups[index.get(category)].lines.push(line);
  }
  return groups;
}

/**
 * The same grouping flattened for a PrimeVue DataTable in subheader mode,
 * which starts a new group whenever `groupRowsBy` changes between ADJACENT
 * rows — so the rows must already be bucketed. Each row gains `_category`.
 */
export function rowsGroupedByCategory(lines) {
  return groupLinesByCategory(lines).flatMap((g) =>
    g.lines.map((line) => ({ ...line, _category: g.category })),
  );
}
