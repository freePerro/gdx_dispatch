import { ref } from 'vue';

/**
 * useBrowserStream — drives the Phase 2 plugin browser stream (ADR-014).
 *
 * Opens a WebSocket to the core proxy (/api/plugins/_browser/ws), shows the
 * JPEG frames it pushes, and forwards mouse/keyboard back as CDP input events.
 * The remote site never executes here — we only render pixels and send input
 * coordinates. Logic lives here (no DOM) so it unit-tests; BrowserStream.vue is
 * the thin template.
 *
 * Remote viewport is fixed REMOTE_W x REMOTE_H; the displayed <img> can be any
 * size, so input coords are scaled from the element rect into viewport space.
 */
export const REMOTE_W = 1280;
export const REMOTE_H = 800;

// Non-printable keys CDP needs a virtual-key-code for. Printable chars go via
// `text` on keyDown, which is enough to fill a login form.
const VKEY = {
  Backspace: 8, Tab: 9, Enter: 13, Shift: 16, Control: 17, Alt: 18,
  Escape: 27, ' ': 32, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39,
  ArrowDown: 40, Delete: 46,
};

/** Pure: map a client (mouse) point to remote viewport coords. Unit-tested. */
export function mapCoords(rect, clientX, clientY) {
  const w = rect.width || REMOTE_W;
  const h = rect.height || REMOTE_H;
  return {
    x: Math.round(((clientX - rect.left) / w) * REMOTE_W),
    y: Math.round(((clientY - rect.top) / h) * REMOTE_H),
  };
}

/** Pure: build the CDP key event payload for a DOM KeyboardEvent. Unit-tested. */
export function keyPayload(domType, e) {
  const printable = e.key && e.key.length === 1;
  const payload = {
    type: domType === 'keydown' ? 'keyDown' : 'keyUp',
    key: e.key,
    code: e.code,
    windowsVirtualKeyCode: VKEY[e.key] ?? (printable ? e.key.toUpperCase().charCodeAt(0) : 0),
  };
  if (domType === 'keydown' && printable) payload.text = e.key;
  return payload;
}

export function wsTicketUrl(ticket) {
  const scheme = (typeof location !== 'undefined' && location.protocol === 'https:') ? 'wss' : 'ws';
  const host = typeof location !== 'undefined' ? location.host : 'localhost';
  const q = new URLSearchParams({ ticket });
  return `${scheme}://${host}/api/plugins/_browser/ws?${q.toString()}`;
}

export function useBrowserStream() {
  const frameSrc = ref(null);     // data: URL of the latest JPEG frame
  const connected = ref(false);
  const error = ref(null);
  let sock = null;
  let onSession = null;           // resolver for a pending saveSession()

  function send(obj) {
    if (sock && sock.readyState === 1) sock.send(JSON.stringify(obj));
  }

  // Two-step auth (ADR-014): POST for a short-lived ticket over HTTP (full auth
  // gate stack runs there), then open the socket with only the ticket. `api.post`
  // is e.g. useApiWithToast().
  async function connect({ key, url, api }) {
    error.value = null;
    let ticket;
    try {
      ({ ticket } = await api.post('/api/plugins/_browser/ticket', { key, url }));
    } catch (e) {
      error.value = e?.message || 'not authorized for browser stream';
      return;
    }
    sock = new WebSocket(wsTicketUrl(ticket));
    sock.onopen = () => { connected.value = true; };
    sock.onclose = () => { connected.value = false; };
    sock.onerror = () => { error.value = 'stream connection failed'; };
    sock.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'frame') frameSrc.value = `data:image/jpeg;base64,${msg.data}`;
      else if (msg.type === 'session' && onSession) { onSession(msg.state); onSession = null; }
    };
  }

  function mouse(domType, e, el) {
    const map = { mousedown: 'mousePressed', mouseup: 'mouseReleased', mousemove: 'mouseMoved' };
    const t = map[domType];
    if (!t || !el) return;
    // The screen <img> uses @mousedown.prevent (to suppress drag/selection), which
    // also suppresses the focus a click normally gives — so keydown would never
    // fire on it and typing wouldn't work. Focus it explicitly on press so the
    // keyboard handlers receive events.
    if (domType === 'mousedown' && typeof el.focus === 'function') el.focus();
    const { x, y } = mapCoords(el.getBoundingClientRect(), e.clientX, e.clientY);
    send({ type: 'mouse', payload: { type: t, x, y, button: 'left', clickCount: 1, buttons: 1 } });
  }

  function key(domType, e) {
    send({ type: 'key', payload: keyPayload(domType, e) });
  }

  function saveSession() {
    return new Promise((resolve) => { onSession = resolve; send({ type: 'save_session' }); });
  }

  function disconnect() {
    send({ type: 'close' });
    if (sock) { try { sock.close(); } catch { /* ignore */ } }
    sock = null;
    connected.value = false;
  }

  return { frameSrc, connected, error, connect, mouse, key, saveSession, disconnect };
}
