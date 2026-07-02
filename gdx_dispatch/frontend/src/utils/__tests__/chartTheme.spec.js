import { afterEach, describe, expect, it } from "vitest";
import { applyChartTheme, chartThemeColors } from "../chartTheme";

const root = document.documentElement;

afterEach(() => {
  root.style.removeProperty("--text-muted");
  root.style.removeProperty("--border-subtle");
  root.style.removeProperty("--text-secondary");
});

describe("chartThemeColors", () => {
  it("reads resolved CSS variables from the document root", () => {
    root.style.setProperty("--text-muted", " #6b7a8d ");
    root.style.setProperty("--border-subtle", "rgba(0, 0, 0, 0.08)");
    root.style.setProperty("--text-secondary", "#3d4558");

    expect(chartThemeColors()).toEqual({
      ticks: "#6b7a8d",
      grid: "rgba(0, 0, 0, 0.08)",
      text: "#3d4558",
    });
  });

  it("falls back to dark-theme defaults when variables are unset", () => {
    expect(chartThemeColors()).toEqual({
      ticks: "#9eaecd",
      grid: "rgba(102,123,164,0.3)",
      text: "#c5d0e4",
    });
  });
});

describe("applyChartTheme", () => {
  it("deep-sets scale and legend colors on an existing options object without clobbering other keys", () => {
    root.style.setProperty("--text-muted", "#111111");
    root.style.setProperty("--border-subtle", "#222222");
    root.style.setProperty("--text-secondary", "#333333");

    const callback = (v) => "$" + v;
    const options = {
      responsive: true,
      plugins: { legend: { display: false, position: "bottom" }, title: { display: false } },
      scales: {
        x: { stacked: true },
        y: { ticks: { callback }, beginAtZero: true },
      },
    };

    const result = applyChartTheme(options);

    expect(result).toBe(options); // mutates + returns the same object

    // Theme colors set
    expect(options.scales.x.ticks.color).toBe("#111111");
    expect(options.scales.x.grid.color).toBe("#222222");
    expect(options.scales.y.ticks.color).toBe("#111111");
    expect(options.scales.y.grid.color).toBe("#222222");
    expect(options.plugins.legend.labels.color).toBe("#333333");

    // Existing keys untouched
    expect(options.responsive).toBe(true);
    expect(options.plugins.title).toEqual({ display: false });
    expect(options.plugins.legend.display).toBe(false);
    expect(options.plugins.legend.position).toBe("bottom");
    expect(options.scales.x.stacked).toBe(true);
    expect(options.scales.y.beginAtZero).toBe(true);
    expect(options.scales.y.ticks.callback).toBe(callback);
  });

  it("creates the nested paths on an empty options object", () => {
    const options = applyChartTheme({});
    expect(options.scales.x.ticks.color).toBe("#9eaecd");
    expect(options.scales.y.grid.color).toBe("rgba(102,123,164,0.3)");
    expect(options.plugins.legend.labels.color).toBe("#c5d0e4");
  });
});
