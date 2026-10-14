import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  cleanServerUrl,
  getServerUrl,
  setServerUrl,
  resolveApiUrl,
  testServerConnection,
  installFetchInterceptor,
  SERVER_STORAGE_KEY,
} from '../serverConfig';

describe('serverConfig', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  describe('cleanServerUrl', () => {
    it('returns empty string for null or empty input', () => {
      expect(cleanServerUrl('')).toBe('');
      expect(cleanServerUrl(null)).toBe('');
      expect(cleanServerUrl('   ')).toBe('');
    });

    it('strips trailing slashes', () => {
      expect(cleanServerUrl('https://example.com/')).toBe('https://example.com');
      expect(cleanServerUrl('https://example.com///')).toBe('https://example.com');
    });

    it('defaults protocol to https if missing', () => {
      expect(cleanServerUrl('dispatch.example.com')).toBe('https://dispatch.example.com');
    });

    it('preserves explicit http protocol for local dev', () => {
      expect(cleanServerUrl('http://10.0.2.2:8001/')).toBe('http://10.0.2.2:8001');
    });
  });

  describe('setServerUrl & getServerUrl', () => {
    it('saves cleaned URL to localStorage and retrieves it', async () => {
      await setServerUrl('https://demo.gdxdispatch.com/');
      expect(getServerUrl()).toBe('https://demo.gdxdispatch.com');
      expect(localStorage.getItem(SERVER_STORAGE_KEY)).toBe('https://demo.gdxdispatch.com');
    });

    it('clears stored URL when set to empty', async () => {
      await setServerUrl('https://demo.gdxdispatch.com');
      expect(getServerUrl()).toBe('https://demo.gdxdispatch.com');

      await setServerUrl('');
      expect(getServerUrl()).toBe('');
      expect(localStorage.getItem(SERVER_STORAGE_KEY)).toBeNull();
    });
  });

  describe('resolveApiUrl', () => {
    it('returns relative path untouched when no server URL configured', async () => {
      await setServerUrl('');
      expect(resolveApiUrl('/api/jobs')).toBe('/api/jobs');
    });

    it('prepends server URL when configured', async () => {
      await setServerUrl('https://dispatch.example.com');
      expect(resolveApiUrl('/api/jobs')).toBe('https://dispatch.example.com/api/jobs');
      expect(resolveApiUrl('auth/login')).toBe('https://dispatch.example.com/auth/login');
    });

    it('leaves absolute and special protocol URLs untouched', async () => {
      await setServerUrl('https://dispatch.example.com');
      expect(resolveApiUrl('https://other.com/api')).toBe('https://other.com/api');
      expect(resolveApiUrl('http://10.0.2.2:8001/status')).toBe('http://10.0.2.2:8001/status');
      expect(resolveApiUrl('data:image/png;base64,...')).toBe('data:image/png;base64,...');
      expect(resolveApiUrl('blob:http://localhost/...')).toBe('blob:http://localhost/...');
    });
  });

  describe('testServerConnection', () => {
    it('returns error when target URL is empty', async () => {
      const res = await testServerConnection('');
      expect(res.ok).toBe(false);
      expect(res.message).toContain('Please enter a server URL');
    });

    it('reports success when /health returns 200', async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: 'ok', db: 'ok' }),
      });
      vi.stubGlobal('fetch', mockFetch);

      const res = await testServerConnection('http://localhost:8001');
      expect(res.ok).toBe(true);
      expect(res.status).toBe('ok');
      expect(mockFetch).toHaveBeenCalledWith('http://localhost:8001/health', expect.any(Object));
    });

    it('reports failure when /health fails', async () => {
      const mockFetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
      });
      vi.stubGlobal('fetch', mockFetch);

      const res = await testServerConnection('http://localhost:8001');
      expect(res.ok).toBe(false);
      expect(res.message).toContain('HTTP 503');
    });
  });
});
