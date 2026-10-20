/**
 * useServerSearchSelect — a Select whose options come from the server per
 * query, with the current value pinned in (GDXA-1965).
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { useServerSearchSelect } from "../useServerSearchSelect";

const toOption = (row) => ({ value: row.id, label: row.name || `Row ${row.id}`, searchText: row.name || "" });

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("useServerSearchSelect", () => {
  it("load() asks the server with an empty query and lists the rows", async () => {
    const search = vi.fn().mockResolvedValue([{ id: 1, name: "A" }, { id: 2, name: "B" }]);
    const picker = useServerSearchSelect({ search, toOption });
    await picker.load();
    expect(search).toHaveBeenCalledWith("");
    expect(picker.options.value.map((o) => o.value)).toEqual([1, 2]);
    expect(picker.loading.value).toBe(false);
  });

  it("typing is debounced into one server query for the last value", async () => {
    vi.useFakeTimers();
    const search = vi.fn().mockResolvedValue([{ id: 9, name: "Nine" }]);
    const picker = useServerSearchSelect({ search, toOption, delay: 250 });
    picker.onFilter({ value: "N" });
    picker.onFilter({ value: "Ni" });
    picker.onFilter({ value: " Nine " });
    vi.advanceTimersByTime(249);
    expect(search).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(search).toHaveBeenCalledTimes(1);
    expect(search).toHaveBeenCalledWith("Nine");
  });

  it("a slow older response never overwrites a newer one", async () => {
    const first = deferred();
    const second = deferred();
    const search = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const picker = useServerSearchSelect({ search, toOption });
    const a = picker.load();
    const b = picker.load();
    second.resolve([{ id: "new", name: "New" }]);
    await b;
    first.resolve([{ id: "old", name: "Old" }]);
    await a;
    expect(picker.options.value.map((o) => o.value)).toEqual(["new"]);
  });

  it("ensure() fetches a value no page holds and keeps it listed across searches", async () => {
    const search = vi.fn().mockResolvedValueOnce([{ id: 1, name: "A" }]).mockResolvedValueOnce([{ id: 2, name: "B" }]);
    const fetchOne = vi.fn().mockResolvedValue({ id: 777, name: "Old job" });
    const picker = useServerSearchSelect({ search, fetchOne, toOption });
    await picker.load();
    await picker.ensure(777);
    expect(fetchOne).toHaveBeenCalledWith(777);
    expect(picker.options.value.map((o) => o.label)).toEqual(["Old job", "A"]);
    await picker.load();
    expect(picker.options.value.map((o) => o.label)).toEqual(["Old job", "B"]);
    expect(picker.find(777)).toEqual({ id: 777, name: "Old job" });
  });

  it("ensure() pins a row already in the results without a fetch", async () => {
    const search = vi.fn().mockResolvedValueOnce([{ id: 1, name: "A" }]).mockResolvedValueOnce([]);
    const fetchOne = vi.fn();
    const picker = useServerSearchSelect({ search, fetchOne, toOption });
    await picker.load();
    await picker.ensure(1);
    await picker.load();
    expect(fetchOne).not.toHaveBeenCalled();
    expect(picker.options.value.map((o) => o.value)).toEqual([1]);
  });

  it("a value the server refuses stays selected under its id", async () => {
    const picker = useServerSearchSelect({
      search: vi.fn().mockResolvedValue([]),
      fetchOne: vi.fn().mockRejectedValue(new Error("404")),
      toOption,
    });
    await picker.ensure("gone");
    expect(picker.options.value).toEqual([{ value: "gone", label: "Row gone", searchText: "" }]);
  });

  it("a stale ensure() does not replace the newer pin", async () => {
    const slow = deferred();
    const fetchOne = vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValueOnce({ id: "b", name: "B" });
    const picker = useServerSearchSelect({ search: vi.fn().mockResolvedValue([]), fetchOne, toOption });
    const first = picker.ensure("a");
    await picker.ensure("b");
    slow.resolve({ id: "a", name: "A" });
    await first;
    expect(picker.options.value.map((o) => o.value)).toEqual(["b"]);
  });

  it("clearing the value drops the pin; toOption null skips a row", async () => {
    const picker = useServerSearchSelect({
      search: vi.fn().mockResolvedValue([{ id: 1, name: "A" }, { id: 2, name: "skip" }]),
      toOption: (row) => (row.name === "skip" ? null : toOption(row)),
    });
    await picker.load();
    await picker.ensure(1);
    await picker.ensure(null);
    expect(picker.options.value.map((o) => o.value)).toEqual([1]);
    expect(picker.find(2)).toEqual({ id: 2, name: "skip" });
  });

  it("onHide() restores the unfiltered list only after a search", async () => {
    vi.useFakeTimers();
    const search = vi.fn().mockResolvedValue([]);
    const picker = useServerSearchSelect({ search, toOption });
    await picker.load();
    picker.onHide();
    expect(search).toHaveBeenCalledTimes(1);
    picker.onFilter({ value: "x" });
    vi.advanceTimersByTime(300);
    expect(search).toHaveBeenLastCalledWith("x");
    picker.onHide();
    expect(search).toHaveBeenLastCalledWith("");
    expect(search).toHaveBeenCalledTimes(3);
  });

  it("onHide() inside the debounce cancels the pending search", async () => {
    vi.useFakeTimers();
    const search = vi.fn().mockResolvedValue([]);
    const picker = useServerSearchSelect({ search, toOption, delay: 250 });
    await picker.load();
    picker.onFilter({ value: "x" });
    vi.advanceTimersByTime(100);
    picker.onHide();
    vi.advanceTimersByTime(500);
    expect(search.mock.calls.map((c) => c[0])).toEqual(["", ""]);
  });

  it("a server match survives Select's client filter for the text that fetched it", async () => {
    vi.useFakeTimers();
    // Server matched "555-123-4567 " by stripped digits; the row's own text has neither.
    const search = vi.fn().mockResolvedValue([{ id: 5, name: "Phone match" }]);
    const picker = useServerSearchSelect({ search, toOption, delay: 250 });
    picker.onFilter({ value: "555-123-4567 " });
    await vi.advanceTimersByTimeAsync(250);
    expect(search).toHaveBeenCalledWith("555-123-4567");
    const [opt] = picker.options.value;
    expect(opt.searchText.includes("555-123-4567 ")).toBe(true);
    expect(opt.label).toBe("Phone match");
  });

  it("the pinned row is not tagged with the search text", async () => {
    vi.useFakeTimers();
    const search = vi.fn().mockResolvedValue([{ id: 2, name: "B" }]);
    const picker = useServerSearchSelect({
      search, fetchOne: vi.fn().mockResolvedValue({ id: 1, name: "Pinned" }), toOption, delay: 250,
    });
    await picker.ensure(1);
    picker.onFilter({ value: "B" });
    await vi.advanceTimersByTimeAsync(250);
    expect(picker.options.value.map((o) => o.searchText)).toEqual(["Pinned", "B B"]);
  });
});
