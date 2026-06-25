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

/** Pure: a single printable character with no modifier → goes via insertText. */
export function isPrintableKey(e) {
  return !!e.key && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
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
  let onCapture = null;           // resolver for a pending capturePage()

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
      else if (msg.type === 'capture' && onCapture) { onCapture({ url: msg.url, text: msg.text, image: msg.image }); onCapture = null; }
    };
  }

  let pressed = false;  // is the left button currently held? (for correct drag vs hover)

  function mouse(domType, e, el) {
    const map = { mousedown: 'mousePressed', mouseup: 'mouseReleased', mousemove: 'mouseMoved' };
    const t = map[domType];
    if (!t || !el) return;
    // The screen <img> uses @mousedown.prevent (to suppress drag/selection), which
    // also suppresses the focus a click normally gives — so keydown wouldn't fire
    // on it and typing wouldn't work. Focus it explicitly on press.
    if (domType === 'mousedown' && typeof el.focus === 'function') el.focus();
    if (domType === 'mousedown') pressed = true;
    if (domType === 'mouseup') pressed = false;
    const { x, y } = mapCoords(el.getBoundingClientRect(), e.clientX, e.clientY);
    // Only report the button as held when it actually is — sending buttons:1 on
    // every move made Chromium treat all movement as a drag, which broke clicks,
    // hovers, and dropdowns. mousePressed/Released are clicks; moves carry the
    // live button state so a real drag still works.
    const payload =
      domType === 'mousemove'
        ? { type: t, x, y, button: pressed ? 'left' : 'none', buttons: pressed ? 1 : 0 }
        : { type: t, x, y, button: 'left', clickCount: 1, buttons: domType === 'mousedown' ? 1 : 0 };
    send({ type: 'mouse', payload });
  }

  function wheel(e, el) {
    if (!el) return;
    const { x, y } = mapCoords(el.getBoundingClientRect(), e.clientX, e.clientY);
    // Reuse the mouse path: CDP dispatchMouseEvent type "mouseWheel" scrolls.
    send({ type: 'mouse', payload: { type: 'mouseWheel', x, y, deltaX: e.deltaX, deltaY: e.deltaY } });
  }

  function key(domType, e) {
    // Printable chars (no modifier) go via insertText so punctuation isn't
    // mis-mapped to a virtual key (the "." → VK_DELETE bug). Control keys
    // (Enter, Backspace, Tab, arrows, …) still need real key events.
    if (isPrintableKey(e)) {
      if (domType === 'keydown') send({ type: 'text', text: e.key });
      return;
    }
    send({ type: 'key', payload: keyPayload(domType, e) });
  }

  // Paste into the remote browser: the local clipboard isn't shared, so read the
  // pasted text and insert it server-side via the same `text` path.
  function paste(e) {
    const text = e.clipboardData && e.clipboardData.getData('text');
    if (text) send({ type: 'text', text });
  }

  function saveSession() {
    return new Promise((resolve) => { onSession = resolve; send({ type: 'save_session' }); });
  }

  // Grab the live page's visible text + URL (the page the operator is on, already
  // logged in) so the host can POST it to a plugin's capture endpoint.
  function capturePage() {
    return new Promise((resolve) => { onCapture = resolve; send({ type: 'capture' }); });
  }

  function disconnect() {
    send({ type: 'close' });
    if (sock) { try { sock.close(); } catch { /* ignore */ } }
    sock = null;
    connected.value = false;
  }

  return { frameSrc, connected, error, connect, mouse, wheel, key, paste, saveSession, capturePage, disconnect };
}
