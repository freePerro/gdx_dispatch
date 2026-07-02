/**
 * Chart.js theme helpers.
 *
 * Reads the app's resolved CSS custom properties at call time so chart colors
 * follow the active theme (dark default / light via [data-theme="light"]).
 *
 * NOTE: Chart.js snapshots colors when the chart is created, so charts must be
 * re-rendered (or have their options rebuilt) on theme change to pick up the
 * new palette. Reacting to theme flips is out of scope for now.
 */

/**
 * Resolve the theme colors charts need from CSS variables, with fallbacks
 * matching the dark (default) theme.
 *
 * @returns {{ ticks: string, grid: string, text: string }}
 */
export function chartThemeColors() {
  const s = getComputedStyle(document.documentElement);
  const v = (n, fb) => (s.getPropertyValue(n) || "").trim() || fb;
  return {
    ticks: v("--text-muted", "#9eaecd"),
    grid: v("--border-subtle", "rgba(102,123,164,0.3)"),
    text: v("--text-secondary", "#c5d0e4"),
  };
}

/**
 * Apply the current theme colors onto a Chart.js options object, creating the
 * nested scales.x/y ticks/grid and plugins.legend.labels paths as needed but
 * never clobbering keys the caller already set.
 *
 * Only use this for cartesian charts (bar/line): it creates scales.x/scales.y,
 * which would render axes on a pie/doughnut chart. For those, use
 * chartThemeColors() directly (e.g. plugins.legend.labels.color).
 *
 * @param {object} [options] Chart.js options object (mutated and returned).
 * @returns {object} the same options object, themed.
 */
export function applyChartTheme(options = {}) {
  const colors = chartThemeColors();

  options.scales = options.scales || {};
  for (const axis of ["x", "y"]) {
    const scale = (options.scales[axis] = options.scales[axis] || {});
    scale.ticks = scale.ticks || {};
    scale.ticks.color = colors.ticks;
    scale.grid = scale.grid || {};
    scale.grid.color = colors.grid;
  }

  options.plugins = options.plugins || {};
  options.plugins.legend = options.plugins.legend || {};
  options.plugins.legend.labels = options.plugins.legend.labels || {};
  options.plugins.legend.labels.color = colors.text;

  return options;
}
