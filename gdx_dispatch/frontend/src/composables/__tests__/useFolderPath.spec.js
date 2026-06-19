/**
 * useFolderPath — unit tests for the bulk-upload path walker.
 * Covers: segment parsing, parent threading, reuse, cache hits, failure paths.
 */
import { describe, expect, it, vi } from 'vitest';
import { ensureFolderPath, folderSegmentsFromFile } from '../useFolderPath';


function fakeFile(relPath) {
  return { webkitRelativePath: relPath };
}


describe('folderSegmentsFromFile', () => {
  it('splits a webkitRelativePath into segments', () => {
    expect(folderSegmentsFromFile(fakeFile('Top/Sub/file.pdf'))).toEqual(['Top', 'Sub']);
  });

  it('returns empty for a flat file (no relative path)', () => {
    expect(folderSegmentsFromFile({ webkitRelativePath: '' })).toEqual([]);
    expect(folderSegmentsFromFile({})).toEqual([]);
  });

  it('handles single-level folder upload', () => {
    expect(folderSegmentsFromFile(fakeFile('Just/file.pdf'))).toEqual(['Just']);
  });

  it('strips empty segments', () => {
    expect(folderSegmentsFromFile(fakeFile('Top//Sub/file.pdf'))).toEqual(['Top', 'Sub']);
  });
});


describe('ensureFolderPath', () => {
  it('creates each segment with parent_id threaded, returns leaf id', async () => {
    let counter = 0;
    const createFolder = vi.fn(async ({ name, parent_id }) => ({
      id: `id-${++counter}-${name}`,
    }));
    const ctx = { cache: new Map(), folders: [], createFolder };

    const leaf = await ensureFolderPath(['Top', 'Sub', 'Inner'], ctx);

    expect(createFolder).toHaveBeenCalledTimes(3);
    expect(createFolder.mock.calls[0][0]).toEqual({ name: 'Top', parent_id: null });
    expect(createFolder.mock.calls[1][0]).toEqual({ name: 'Sub', parent_id: 'id-1-Top' });
    expect(createFolder.mock.calls[2][0]).toEqual({ name: 'Inner', parent_id: 'id-2-Sub' });
    expect(leaf).toBe('id-3-Inner');
  });

  it('reuses an existing folder by (name, parent_id)', async () => {
    const createFolder = vi.fn(async () => ({ id: 'never' }));
    const ctx = {
      cache: new Map(),
      folders: [
        { id: 'top-1', name: 'Top', parent_id: null },
        { id: 'sub-1', name: 'Sub', parent_id: 'top-1' },
      ],
      createFolder,
    };

    const leaf = await ensureFolderPath(['Top', 'Sub'], ctx);
    expect(createFolder).not.toHaveBeenCalled();
    expect(leaf).toBe('sub-1');
  });

  it('treats two folders with the same name but different parents as distinct', async () => {
    let counter = 0;
    const createFolder = vi.fn(async ({ name }) => ({ id: `id-${++counter}-${name}` }));
    const ctx = {
      cache: new Map(),
      folders: [
        { id: 'a', name: 'A', parent_id: null },
        // Pre-existing 'Inner' under root, not under A.
        { id: 'inner-root', name: 'Inner', parent_id: null },
      ],
      createFolder,
    };

    const leaf = await ensureFolderPath(['A', 'Inner'], ctx);
    // Should NOT reuse inner-root (its parent is null, not 'a').
    expect(createFolder).toHaveBeenCalledTimes(1);
    expect(createFolder.mock.calls[0][0]).toEqual({ name: 'Inner', parent_id: 'a' });
    expect(leaf).toBe('id-1-Inner');
  });

  it('hits the cache on a second walk through the same path', async () => {
    let counter = 0;
    const createFolder = vi.fn(async ({ name }) => ({ id: `id-${++counter}-${name}` }));
    const ctx = { cache: new Map(), folders: [], createFolder };

    const a = await ensureFolderPath(['Top', 'Sub'], ctx);
    const b = await ensureFolderPath(['Top', 'Sub'], ctx);
    expect(a).toBe(b);
    expect(createFolder).toHaveBeenCalledTimes(2); // once for Top, once for Sub
  });

  it('returns null and records an error when create throws', async () => {
    const createFolder = vi.fn(async () => {
      throw new Error('boom');
    });
    const errors = [];
    const ctx = {
      cache: new Map(),
      folders: [],
      createFolder,
      onError: (e) => errors.push(e),
    };

    const leaf = await ensureFolderPath(['Top'], ctx);
    expect(leaf).toBeNull();
    expect(errors).toHaveLength(1);
    expect(errors[0].path).toBe('Top');
    expect(errors[0].message).toContain('boom');
  });

  it('short-circuits subsequent files on the same failed branch', async () => {
    const createFolder = vi.fn(async ({ name }) => {
      if (name === 'Top') throw new Error('nope');
      return { id: 'should-not-be-reached' };
    });
    const ctx = { cache: new Map(), folders: [], createFolder, onError: () => {} };

    const a = await ensureFolderPath(['Top', 'Sub'], ctx);
    const b = await ensureFolderPath(['Top', 'Other'], ctx);
    expect(a).toBeNull();
    expect(b).toBeNull();
    // Only the first walk attempted to create Top; the second hit the cached null.
    expect(createFolder).toHaveBeenCalledTimes(1);
  });

  it('returns null for empty segments', async () => {
    const ctx = { cache: new Map(), folders: [], createFolder: vi.fn() };
    expect(await ensureFolderPath([], ctx)).toBeNull();
    expect(await ensureFolderPath(null, ctx)).toBeNull();
  });

  it('appends newly created folders to the local list so siblings see them', async () => {
    let counter = 0;
    const createFolder = vi.fn(async ({ name }) => ({ id: `id-${++counter}-${name}` }));
    const folders = [];
    const ctx = { cache: new Map(), folders, createFolder };

    await ensureFolderPath(['A', 'B'], ctx);
    // Now the second file walks ['A', 'C'] — A should already exist locally and not re-create.
    await ensureFolderPath(['A', 'C'], ctx);
    expect(folders.map((f) => f.name).sort()).toEqual(['A', 'B', 'C']);
    // Total creates: A, B, C — 3.
    expect(createFolder).toHaveBeenCalledTimes(3);
  });
});
