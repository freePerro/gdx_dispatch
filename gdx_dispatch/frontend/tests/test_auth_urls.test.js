import { describe, expect, it } from 'vitest';
import {
  getLoginRedirectLocation,
  getLoginRedirectUrl,
  getPostLoginRedirect,
} from '../src/lib/auth-urls';

describe('auth-urls — SS-13 Slice F', () => {
  describe('getLoginRedirectUrl', () => {
    it('builds /login?redirect=<encoded path> for a plain path', () => {
      expect(getLoginRedirectUrl('/login-picker')).toBe('/login?redirect=%2Flogin-picker');
    });

    it('URL-encodes slashes, query strings, and special chars', () => {
      expect(getLoginRedirectUrl('/t/acme/jobs?status=open')).toBe(
        '/login?redirect=%2Ft%2Facme%2Fjobs%3Fstatus%3Dopen',
      );
    });

    it('returns bare /login when returnTo is empty string', () => {
      expect(getLoginRedirectUrl('')).toBe('/login');
    });

    it('returns bare /login when returnTo is null or undefined', () => {
      expect(getLoginRedirectUrl(null)).toBe('/login');
      expect(getLoginRedirectUrl(undefined)).toBe('/login');
    });
  });

  describe('getLoginRedirectLocation', () => {
    it('builds a vue-router location object for a standard path string', () => {
      expect(getLoginRedirectLocation('/dashboard')).toEqual({
        path: '/login',
        query: { redirect: '/dashboard' },
      });
    });

    it('passes an empty-string returnTo through as-is (no falsy-guard — vue-router owns empty-query handling)', () => {
      expect(getLoginRedirectLocation('')).toEqual({
        path: '/login',
        query: { redirect: '' },
      });
    });

    it('passes null and undefined through as query.redirect (documents pass-through contract)', () => {
      expect(getLoginRedirectLocation(null)).toEqual({
        path: '/login',
        query: { redirect: null },
      });
      expect(getLoginRedirectLocation(undefined)).toEqual({
        path: '/login',
        query: { redirect: undefined },
      });
    });

    it('preserves a path that already contains query parameters (vue-router encodes on serialization)', () => {
      expect(getLoginRedirectLocation('/settings?user=123')).toEqual({
        path: '/login',
        query: { redirect: '/settings?user=123' },
      });
    });
  });

  describe('getPostLoginRedirect', () => {
    it('returns route.query.redirect when present', () => {
      expect(getPostLoginRedirect({ query: { redirect: '/jobs' } })).toBe('/jobs');
    });

    it('returns /dashboard when redirect query is missing', () => {
      expect(getPostLoginRedirect({ query: {} })).toBe('/dashboard');
    });

    it('returns /dashboard when redirect query is empty string', () => {
      expect(getPostLoginRedirect({ query: { redirect: '' } })).toBe('/dashboard');
    });

    it('returns /dashboard when route has no query object', () => {
      expect(getPostLoginRedirect({})).toBe('/dashboard');
    });

    it('returns /dashboard when route is null or undefined', () => {
      expect(getPostLoginRedirect(null)).toBe('/dashboard');
      expect(getPostLoginRedirect(undefined)).toBe('/dashboard');
    });

    it('honors explicit fallback override', () => {
      expect(getPostLoginRedirect({ query: {} }, '/login-picker')).toBe('/login-picker');
    });

    it('prefers redirect over fallback when both are set', () => {
      expect(getPostLoginRedirect({ query: { redirect: '/jobs' } }, '/dashboard')).toBe('/jobs');
    });
  });
});
