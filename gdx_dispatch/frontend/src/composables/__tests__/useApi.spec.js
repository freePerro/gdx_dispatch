/**
 * Tests for useApi.js — focus on client-error reporting payload shape.
 *
 * Regression: when an upstream response is 422, FastAPI returns `detail` as
 * a list of validation-error dicts. The client-error reporter must coerce
 * that to a string before POSTing, otherwise its own payload also fails
 * Pydantic validation (bug_reports.py expects `detail: str`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { createApiClient } from '../useApi';

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body };
}

describe('useApi client-error reporting', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    // jsdom hostname is "localhost" → no tenant subdomain
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('coerces array detail (FastAPI 422) to a string in the error-report POST', async () => {
    const validationErrors = [
      { loc: ['body', 'foo'], msg: 'field required', type: 'value_error.missing' },
      { loc: ['body', 'bar'], msg: 'not a valid int', type: 'type_error.integer' },
    ];
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: validationErrors }, { ok: false, status: 422 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));

    const api = createApiClient();
    await expect(api.get('/api/anything')).rejects.toThrow();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [reportUrl, reportInit] = fetchMock.mock.calls[1];
    expect(reportUrl).toBe('/api/feedback/client-error');
    const body = JSON.parse(reportInit.body);
    expect(typeof body.detail).toBe('string');
    expect(body.detail).toContain('field required');
    expect(body.status).toBe(422);
  });

  it('passes string detail through unchanged', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Not found' }, { ok: false, status: 404 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));

    const api = createApiClient();
    await expect(api.get('/api/missing')).rejects.toThrow('Not found');

    const [, reportInit] = fetchMock.mock.calls[1];
    const body = JSON.parse(reportInit.body);
    expect(body.detail).toBe('Not found');
  });

  it('coerces object detail to a JSON string', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: { code: 'X', hint: 'y' } }, { ok: false, status: 400 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));

    const api = createApiClient();
    await expect(api.get('/api/thing')).rejects.toThrow();

    const [, reportInit] = fetchMock.mock.calls[1];
    const body = JSON.parse(reportInit.body);
    expect(typeof body.detail).toBe('string');
    expect(body.detail).toContain('"code"');
  });

  it('attaches the parsed error body as err.body so callers can read structured fields', async () => {
    // Doug 2026-05-10: the dispatch "Cannot complete: missing parts" toast
    // needs to read `missing[]` off the 422 body. Pre-fix, useApi only
    // surfaced `detail` and dropped everything else, so the toast never
    // fired in production. Pin the contract here.
    const errorBody = {
      detail: 'completion requirements unmet',
      missing: ['parts', 'hours'],
    };
    fetchMock
      .mockResolvedValueOnce(mkResponse(errorBody, { ok: false, status: 422 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));

    const api = createApiClient();
    let captured;
    try {
      await api.post('/api/jobs/abc/complete', {});
    } catch (e) {
      captured = e;
    }

    expect(captured).toBeTruthy();
    expect(captured.status).toBe(422);
    expect(captured.body).toEqual(errorBody);
    expect(captured.body.missing).toEqual(['parts', 'hours']);
  });
});

describe('api.delete alias (contract-gap Tier 1.1)', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    fetchMock = vi.fn();
    global.fetch = fetchMock;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('transport exposes delete as an alias of del', () => {
    const api = createApiClient();
    expect(typeof api.delete).toBe('function');
    expect(api.delete).toBe(api.del);
  });

  it('delete(url) sends DELETE with no body', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ok: true }));
    const api = createApiClient();
    await api.delete('/api/campaigns/abc');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/campaigns/abc');
    expect(init.method).toBe('DELETE');
    expect(init.body).toBeUndefined();
  });

  it('delete(url, data) sends a JSON body — the push-unsubscribe shape', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ status: 'unsubscribed' }));
    const api = createApiClient();
    await api.delete('/api/push/v2/unsubscribe', { endpoint: 'https://push.example/ep' });

    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('DELETE');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(init.body)).toEqual({ endpoint: 'https://push.example/ep' });
  });
});

describe('useApi delete options-misuse guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    global.fetch = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('delete(url, { successMessage }) treats the object as options, not a body', async () => {
    const { useApi } = await import('../useApi');
    global.fetch.mockResolvedValueOnce(mkResponse({ ok: true }));
    // Outside Vue setup: toast/router are null, so no toast — but the
    // request must NOT carry the options object as a JSON body.
    const api = useApi();
    await api.delete('/api/foo/1', { successMessage: 'Deleted' });

    const [, init] = global.fetch.mock.calls[0];
    expect(init.method).toBe('DELETE');
    expect(init.body).toBeUndefined();
  });

  it('delete(url, realBody) still sends the body when keys are not option-shaped', async () => {
    const { useApi } = await import('../useApi');
    global.fetch.mockResolvedValueOnce(mkResponse({ ok: true }));
    const api = useApi();
    await api.delete('/api/push/v2/unsubscribe', { endpoint: 'x' });

    const [, init] = global.fetch.mock.calls[0];
    expect(JSON.parse(init.body)).toEqual({ endpoint: 'x' });
  });
});
