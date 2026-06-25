import { describe, it, expect } from 'vitest';
import { mapCoords, keyPayload, wsTicketUrl, isPrintableKey, REMOTE_W, REMOTE_H } from '../useBrowserStream';

describe('isPrintableKey (text vs control routing)', () => {
  it('treats single chars (incl. punctuation) with no modifier as printable', () => {
    expect(isPrintableKey({ key: '.' })).toBe(true);
    expect(isPrintableKey({ key: 'a' })).toBe(true);
    expect(isPrintableKey({ key: '@' })).toBe(true);
  });
  it('treats control keys as not printable', () => {
    expect(isPrintableKey({ key: 'Enter' })).toBe(false);
    expect(isPrintableKey({ key: 'Backspace' })).toBe(false);
    expect(isPrintableKey({ key: 'Tab' })).toBe(false);
  });
  it('treats modified keys (Ctrl/Meta/Alt) as not printable', () => {
    expect(isPrintableKey({ key: 'v', ctrlKey: true })).toBe(false);
    expect(isPrintableKey({ key: 'c', metaKey: true })).toBe(false);
  });
});

describe('useBrowserStream pure logic', () => {
  it('mapCoords scales a displayed point into remote viewport space', () => {
    // element 640x400 on screen, remote is 1280x800 → 2x scale
    const rect = { left: 0, top: 0, width: 640, height: 400 };
    expect(mapCoords(rect, 0, 0)).toEqual({ x: 0, y: 0 });
    expect(mapCoords(rect, 640, 400)).toEqual({ x: REMOTE_W, y: REMOTE_H });
    expect(mapCoords(rect, 320, 200)).toEqual({ x: 640, y: 400 });
  });

  it('mapCoords accounts for element offset', () => {
    const rect = { left: 100, top: 50, width: 1280, height: 800 };
    expect(mapCoords(rect, 100, 50)).toEqual({ x: 0, y: 0 });
    expect(mapCoords(rect, 740, 450)).toEqual({ x: 640, y: 400 });
  });

  it('keyPayload sends text for printable keydown', () => {
    const p = keyPayload('keydown', { key: 'a', code: 'KeyA' });
    expect(p.type).toBe('keyDown');
    expect(p.text).toBe('a');
    expect(p.windowsVirtualKeyCode).toBe('A'.charCodeAt(0));
  });

  it('keyPayload maps Enter without text', () => {
    const p = keyPayload('keydown', { key: 'Enter', code: 'Enter' });
    expect(p.type).toBe('keyDown');
    expect(p.windowsVirtualKeyCode).toBe(13);
    expect(p.text).toBeUndefined();
  });

  it('keyPayload keyup has no text', () => {
    const p = keyPayload('keyup', { key: 'a', code: 'KeyA' });
    expect(p.type).toBe('keyUp');
    expect(p.text).toBeUndefined();
  });

  it('wsTicketUrl carries only the ticket (no creds in the socket URL)', () => {
    const u = wsTicketUrl('tkt-abc.def.ghi');
    expect(u).toContain('/api/plugins/_browser/ws?');
    expect(u).toContain('ticket=tkt-abc.def.ghi');
    expect(u).toMatch(/^wss?:\/\//);
  });
});
