import { useAuthStore } from '../stores/auth';

function resolveTenantId() {
  const stored = sessionStorage.getItem('gdx_tenant_slug');
  if (stored) return stored;
  const parts = window.location.hostname.split('.');
  const sub = parts.length >= 3 ? parts[0] : null;
  return sub && sub !== 'www' ? sub : null;
}

function buildHeaders(auth) {
  const headers = {};
  const tenantId = resolveTenantId();
  if (tenantId) headers['x-tenant-id'] = tenantId;
  if (auth.accessToken) headers.Authorization = `Bearer ${auth.accessToken}`;
  return headers;
}

async function fetchAuthedBlob(url) {
  const auth = useAuthStore();
  let res = await fetch(url, { headers: buildHeaders(auth), credentials: 'include' });
  if (res.status === 401) {
    await auth.refreshAccessToken();
    res = await fetch(url, { headers: buildHeaders(auth), credentials: 'include' });
  }
  if (!res.ok) {
    const err = new Error(`Failed to load file (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.blob();
}

export async function createAuthedBlobUrl(url) {
  const blob = await fetchAuthedBlob(url);
  return URL.createObjectURL(blob);
}

export async function openAuthedFile(url) {
  const blobUrl = await createAuthedBlobUrl(url);
  const w = window.open(blobUrl, '_blank');
  setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
  if (!w) window.location.href = blobUrl;
}
