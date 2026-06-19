/**
 * Folder-path helpers for nested document folder upload.
 * Pure functions — no Vue refs, no module-level state — so they're trivial
 * to unit-test and reusable from anywhere in the app.
 */

export function folderSegmentsFromFile(file) {
  // webkitRelativePath is "TopFolder/sub/file.ext" — strip the filename, return path segments.
  const rel = file?.webkitRelativePath || '';
  const idx = rel.lastIndexOf('/');
  if (idx < 0) return [];
  return rel.slice(0, idx).split('/').filter(Boolean);
}

/**
 * Walk path segments, creating each missing folder under its parent.
 * Returns the leaf folder_id, or null if any creation step failed.
 *
 * @param {string[]} segments      Path parts e.g. ['Top','Sub','Inner']
 * @param {object} ctx
 * @param {Map}      ctx.cache     Memoizes per-batch path → folderId|null
 * @param {Array}    ctx.folders   Live folders list (mutated to track new ids)
 * @param {Function} ctx.createFolder  async ({name, parent_id}) → {id} | throws
 * @param {Function} [ctx.onError]     Optional ({path, message}) → void
 */
export async function ensureFolderPath(segments, ctx) {
  if (!segments || !segments.length) return null;
  const { cache, folders, createFolder, onError } = ctx;
  let parentId = null;
  for (let i = 0; i < segments.length; i++) {
    const path = segments.slice(0, i + 1).join('/');
    if (cache.has(path)) {
      parentId = cache.get(path);
      if (parentId === null) return null;
      continue;
    }
    const name = segments[i];
    const existing = folders.find(
      (f) => f.name === name && (f.parent_id || null) === parentId,
    );
    if (existing) {
      cache.set(path, existing.id);
      parentId = existing.id;
      continue;
    }
    try {
      const created = await createFolder({ name, parent_id: parentId });
      const id = created?.id || null;
      if (!id) {
        onError?.({ path, message: 'Folder create returned no id' });
        cache.set(path, null);
        return null;
      }
      cache.set(path, id);
      folders.push({ id, name, parent_id: parentId, description: null });
      parentId = id;
    } catch (err) {
      onError?.({ path, message: `Folder create failed: ${err?.message || err}` });
      cache.set(path, null);
      return null;
    }
  }
  return parentId;
}
