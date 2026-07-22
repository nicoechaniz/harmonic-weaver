// Harmonic Weaver — live overlay.
// Renders the serpentine 4x8 harmonic pad grid over the webcam feed, mirroring
// the static overlay-preview.html but redrawing every requestAnimationFrame.

// --- layout constants ---
const COLS = 4, ROWS = 8, N = 32;
const W = 1280, H = 720;
const gap = 3;
const cellW = (W - gap * (COLS + 1)) / COLS;
const cellH = (H - gap * (ROWS + 1)) / ROWS;

// Serpentine: even cols (0,2) run bottom->top, odd cols (1,3) run top->bottom.
// row 0 = bottom of the grid model.
function padIndex(col, row) {
  if (col % 2 === 0) {
    return col * ROWS + row;                 // even: bottom->top
  } else {
    return col * ROWS + (ROWS - 1 - row);    // odd: top->bottom
  }
}

// Same 32 hues as overlay-preview.html.
const activeColors = [
  '#ff4466', '#ff6644', '#ff8844', '#ffaa44',
  '#ffcc44', '#ffee44', '#ddff44', '#bbff44',
  '#44ff88', '#44ffaa', '#44ffcc', '#44ffee',
  '#44ddff', '#44bbff', '#4499ff', '#4477ff',
  '#6644ff', '#8844ff', '#aa44ff', '#cc44ff',
  '#ee44ff', '#ff44dd', '#ff44bb', '#ff4499',
  '#ff6644', '#ff8844', '#ffaa44', '#ffcc44',
  '#ddff44', '#bbff44', '#44ff88', '#44ffaa',
];

// --- DOM ---
const cam = document.getElementById('cam');
const bg = document.getElementById('bg');
const grid = document.getElementById('grid');
const statusEl = document.getElementById('status');
const statusText = document.getElementById('statusText');

bg.width = W; bg.height = H;
grid.width = W; grid.height = H;
const bgctx = bg.getContext('2d');
const gctx = grid.getContext('2d');

// ---------------------------------------------------------------------------
// Activation state
// ---------------------------------------------------------------------------
// `activePads` is the Set the renderer reads. It is the union of three sources,
// each tracked separately so one source removing a pad never clobbers a pad
// another source still wants.
const activePads = new Set();
const wsPads = new Set();       // driven by the websocket
const randomPads = new Set();   // placeholder simulation
let hoverIdx = null;            // mouse hover (single pad)

function recomputeActive() {
  activePads.clear();
  for (const i of wsPads) activePads.add(i);
  for (const i of randomPads) activePads.add(i);
  if (hoverIdx != null) activePads.add(hoverIdx);
}

// ---------------------------------------------------------------------------
// Status pill
// ---------------------------------------------------------------------------
let cameraState = 'waiting';   // waiting | live | denied
let wsState = 'connecting';    // connecting | live | offline

function updateStatus() {
  const both = cameraState === 'live' && wsState === 'live';
  statusEl.classList.toggle('live', both);
  statusText.textContent = `cam ${cameraState} · ws ${wsState}`;
}

// ---------------------------------------------------------------------------
// Background feed
// ---------------------------------------------------------------------------
// Simulated feed used when the webcam is unavailable — same radial gradient and
// grid texture as overlay-preview.html. Drawn once; the frame loop "holds" it.
function drawFallback() {
  const bgGrad = bgctx.createRadialGradient(W / 2, H / 2, 100, W / 2, H / 2, 800);
  bgGrad.addColorStop(0, '#1a1a2e');
  bgGrad.addColorStop(0.4, '#16213e');
  bgGrad.addColorStop(1, '#0a0a15');
  bgctx.fillStyle = bgGrad;
  bgctx.fillRect(0, 0, W, H);

  bgctx.strokeStyle = 'rgba(255,255,255,0.02)';
  bgctx.lineWidth = 0.5;
  for (let x = 0; x < W; x += 40) { bgctx.beginPath(); bgctx.moveTo(x, 0); bgctx.lineTo(x, H); bgctx.stroke(); }
  for (let y = 0; y < H; y += 40) { bgctx.beginPath(); bgctx.moveTo(0, y); bgctx.lineTo(W, y); bgctx.stroke(); }
}

async function initCamera() {
  drawFallback();  // always show something immediately
  updateStatus();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    cameraState = 'denied';
    updateStatus();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 } });
    cam.srcObject = stream;
    cam.addEventListener('loadeddata', () => { cameraState = 'live'; updateStatus(); }, { once: true });
    try { await cam.play(); } catch (_e) { /* autoplay policy — muted+playsinline usually covers it */ }
  } catch (_err) {
    cameraState = 'denied';  // permission denied / no device — fallback already on screen
    updateStatus();
  }
}

// ---------------------------------------------------------------------------
// Grid overlay (mirrors overlay-preview.html, redrawn every frame)
// ---------------------------------------------------------------------------
function drawGrid() {
  gctx.clearRect(0, 0, W, H);

  for (let col = 0; col < COLS; col++) {
    for (let row = 0; row < ROWS; row++) {
      const idx = padIndex(col, row);
      const harmonic = idx + 1;
      // Flip columns visually so grid matches mirrored camera
      const visualCol = COLS - 1 - col;
      const x = gap + visualCol * (cellW + gap);
      const canvasRow = ROWS - 1 - row;  // row 0 = bottom in model, canvas y=0 is top
      const y = gap + canvasRow * (cellH + gap);

      const isActive = activePads.has(idx);

      // background fill
      if (isActive) {
        gctx.fillStyle = activeColors[idx] + '55';  // semi-transparent
      } else {
        gctx.fillStyle = 'rgba(10,15,25,0.4)';
      }
      gctx.fillRect(x, y, cellW, cellH);

      // border
      gctx.strokeStyle = isActive ? activeColors[idx] + 'cc' : 'rgba(100,140,200,0.35)';
      gctx.lineWidth = isActive ? 2.5 : 1.5;
      gctx.strokeRect(x, y, cellW, cellH);

      // harmonic number
      const fontSize = Math.min(cellW, cellH) * 0.38;
      gctx.font = `700 ${fontSize}px ui-monospace, SFMono, monospace`;
      gctx.textAlign = 'center';
      gctx.textBaseline = 'middle';
      gctx.fillStyle = isActive ? '#fff' : 'rgba(180,200,230,0.7)';
      gctx.fillText(`H${harmonic}`, x + cellW / 2, y + cellH / 2);
    }
  }

  // --- serpentine path arrows ---
  gctx.strokeStyle = 'rgba(255,255,255,0.15)';
  gctx.lineWidth = 2;
  gctx.setLineDash([8, 12]);

  for (let col = 0; col < COLS; col++) {
    const visualCol = COLS - 1 - col;
    const cx = gap + visualCol * (cellW + gap) + cellW / 2;
    const y0 = gap + cellH / 2;
    const y1 = H - gap - cellH / 2;

    gctx.beginPath();
    gctx.moveTo(cx, col % 2 === 0 ? y1 : y0);  // start bottom for even, top for odd
    gctx.lineTo(cx, col % 2 === 0 ? y0 : y1);  // end top for even, bottom for odd
    gctx.stroke();

    if (col < COLS - 1) {
      const nVisualCol = COLS - 1 - (col + 1);
      const nx = gap + nVisualCol * (cellW + gap) + cellW / 2;
      const connectY = col % 2 === 0 ? y0 : y1;
      gctx.beginPath();
      gctx.moveTo(cx, connectY);
      gctx.lineTo(nx, connectY);
      gctx.stroke();
    }
  }

  // --- column labels ---
  gctx.setLineDash([]);
  gctx.fillStyle = 'rgba(200,220,255,0.5)';
  gctx.font = '600 14px ui-monospace, SFMono, monospace';
  gctx.textAlign = 'center';
  for (let col = 0; col < COLS; col++) {
    const visualCol = COLS - 1 - col;
    const cx = gap + visualCol * (cellW + gap) + cellW / 2;
    const dir = col % 2 === 0 ? '↑' : '↓';
    gctx.fillText(`col ${col + 1} ${dir}`, cx, H - 8);
  }
}

// ---------------------------------------------------------------------------
// Hand position dots — HarMoCAP detected wrist positions
// ---------------------------------------------------------------------------
function drawHandDot(sourceId, color) {
  const xKey = sourceId.replace(/_[xy]$/, '_x');
  const yKey = sourceId.replace(/_[xy]$/, '_y');
  const x = handPos[xKey], y = handPos[yKey];
  if (x == null || y == null) return;
  // Grid matches HarMoCAP coordinates (both unflipped); camera is only
  // flipped for visual comfort — dots stay at HarMoCAP positions.
  const px = x * W;
  // HarMoCAP Y=0 is top of image; canvas Y=0 is top → direct match
  const py = y * H;
  const r = 14;
  gctx.beginPath();
  gctx.arc(px, py, r, 0, Math.PI * 2);
  gctx.fillStyle = color + '66';
  gctx.fill();
  gctx.strokeStyle = color;
  gctx.lineWidth = 2.5;
  gctx.stroke();
  // crosshair
  gctx.beginPath();
  gctx.moveTo(px - r - 4, py); gctx.lineTo(px + r + 4, py);
  gctx.moveTo(px, py - r - 4); gctx.lineTo(px, py + r + 4);
  gctx.strokeStyle = '#fff';
  gctx.lineWidth = 1;
  gctx.stroke();
}

function drawHandDots() {
  drawHandDot('hand_r_x', '#ff4466');  // red for right hand
  drawHandDot('hand_l_x', '#44aaff');  // blue for left hand
}

// ---------------------------------------------------------------------------
// Frame loop
// ---------------------------------------------------------------------------
function frame() {
  if (cameraState === 'live' && cam.readyState >= 2) {
    // Mirror the camera feed horizontally (dancer's perspective)
    bgctx.save();
    bgctx.translate(W, 0);
    bgctx.scale(-1, 1);
    try { bgctx.drawImage(cam, 0, 0, W, H); } catch (_e) { /* frame not ready */ }
    bgctx.restore();
  }
  // else: bg holds the fallback drawn once at startup
  drawGrid();
  drawHandDots();
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------------------
// Mouse hover -> pad
// ---------------------------------------------------------------------------
function padAt(px, py) {
  for (let col = 0; col < COLS; col++) {
    const visualCol = COLS - 1 - col;
    const x = gap + visualCol * (cellW + gap);
    if (px < x || px > x + cellW) continue;
    for (let row = 0; row < ROWS; row++) {
      const canvasRow = ROWS - 1 - row;
      const y = gap + canvasRow * (cellH + gap);
      if (py >= y && py <= y + cellH) return padIndex(col, row);
    }
  }
  return null;  // in a gap / outside a cell
}

function setHover(idx) {
  if (idx === hoverIdx) return;
  hoverIdx = idx;
  recomputeActive();
}

grid.addEventListener('mousemove', (e) => {
  const r = grid.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const px = (e.clientX - r.left) / r.width * W;
  const py = (e.clientY - r.top) / r.height * H;
  setHover(padAt(px, py));
});
grid.addEventListener('mouseleave', () => setHover(null));

// ---------------------------------------------------------------------------
// WebSocket — Stage Contract protocol.
// Handshake: server.hello → client.hello → server.hello(ready) →
// state.subscribe(sources) → state.snapshot + state.event stream.
// Pad highlights come from derived sources hand_r_pad.pad / hand_l_pad.pad.
// ---------------------------------------------------------------------------
const PROTOCOL_VERSION = '0.1-draft';
const STAGE_CONTRACT_ID = 'cc2f83205e0dccf6d0b5d488883d73ad';
const PAD_SOURCE_CHANNELS = {
  hand_r_pad: 'pad',
  hand_l_pad: 'pad',
};
const POS_SOURCE_CHANNELS = {
  hand_r_x: 'x',
  hand_r_y: 'y',
  hand_l_x: 'x',
  hand_l_y: 'y',
};

let ws = null;
let reconnectTimer = null;
let lastPadMsgAt = 0;  // when we last got real pad data (gates the simulation)
let requestSeq = 0;
let stageGated = false;
// Latest pad index per hand source (null = invalid / unknown).
const handPads = { hand_r_pad: null, hand_l_pad: null };
// Latest raw hand positions (0-1 in camera frame) — for drawing dots.
const handPos = { hand_r_x: null, hand_r_y: null, hand_l_x: null, hand_l_y: null };

function clientId() {
  try {
    const key = 'harmonic-weaver-overlay-client-id';
    let value = localStorage.getItem(key);
    if (!value) {
      value = `overlay-${crypto.randomUUID()}`;
      localStorage.setItem(key, value);
    }
    return value;
  } catch (_e) {
    return `overlay-${Math.random().toString(16).slice(2)}`;
  }
}

function nextRequestId(prefix) {
  requestSeq += 1;
  return `${prefix}-${requestSeq}`;
}

function sendClient(type, payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    type,
    protocol_version: PROTOCOL_VERSION,
    request_id: nextRequestId(type.replace('.', '-')),
    payload,
  }));
}

function padIndexFromEnvelope(envelope) {
  if (!envelope || typeof envelope !== 'object') return null;
  if (envelope.state && envelope.state !== 'observed' && envelope.state !== 'held') return null;
  const n = Number(envelope.value);
  if (!Number.isFinite(n)) return null;
  const idx = Math.round(n);
  if (!Number.isInteger(idx) || idx < 0 || idx >= N) return null;
  return idx;
}

function recomputeWsPadsFromHands() {
  wsPads.clear();
  for (const idx of Object.values(handPads)) {
    if (idx != null) wsPads.add(idx);
  }
  if (wsPads.size) {
    lastPadMsgAt = Date.now();
    randomPads.clear();
  }
  recomputeActive();
}

function applyPadChannel(sourceId, channel, envelope) {
  // Track raw hand positions for dot rendering
  if (sourceId in handPos && channel === PAD_SOURCE_CHANNELS[sourceId]) {
    if (envelope && typeof envelope.value === 'number') {
      handPos[sourceId] = envelope.value;
    }
  }
  // Track pad indices for grid highlighting
  if (!(sourceId in handPads)) return;
  if (channel !== PAD_SOURCE_CHANNELS[sourceId]) return;
  handPads[sourceId] = padIndexFromEnvelope(envelope);
  recomputeWsPadsFromHands();
}

function applySourcesSnapshot(sources) {
  if (!Array.isArray(sources)) return;
  let touched = false;
  for (const source of sources) {
    if (!source || typeof source !== 'object') continue;
    const sourceId = source.source_id;
    // Position sources
    if (sourceId in POS_SOURCE_CHANNELS) {
      const ch = POS_SOURCE_CHANNELS[sourceId];
      const env = source.channels?.[ch];
      if (env && typeof env.value === 'number') handPos[sourceId] = env.value;
    }
    // Pad sources
    if (!(sourceId in handPads)) continue;
    const channel = PAD_SOURCE_CHANNELS[sourceId];
    const envelope = source.channels?.[channel];
    handPads[sourceId] = padIndexFromEnvelope(envelope);
    touched = true;
  }
  if (touched) recomputeWsPadsFromHands();
}

function applySourceChannelsUpdated(payload) {
  if (!payload || typeof payload !== 'object') return;
  const entity = payload.entity || payload;
  const sourceId = entity.source_id || payload.entity_id;
  // Handle position sources
  if (sourceId in POS_SOURCE_CHANNELS) {
    const channels = entity.channels;
    if (channels && typeof channels === 'object') {
      const ch = POS_SOURCE_CHANNELS[sourceId];
      if (ch in channels) {
        const env = channels[ch];
        if (env && typeof env.value === 'number') handPos[sourceId] = env.value;
      }
    }
  }
  // Handle pad sources
  if (!(sourceId in handPads)) return;
  const channels = entity.channels;
  if (!channels || typeof channels !== 'object') return;
  const channel = PAD_SOURCE_CHANNELS[sourceId];
  if (!(channel in channels)) return;
  applyPadChannel(sourceId, channel, channels[channel]);
}

// Legacy defensive parsers (route_state / pad_activation) kept as no-ops if a
// future feed still uses them — they merge into wsPads without clobbering hands.
function toPadList(value) {
  const out = [];
  if (Array.isArray(value)) {
    if (value.length && typeof value[0] === 'boolean') {
      value.forEach((on, i) => { if (on) out.push(i); });
    } else {
      for (const v of value) {
        const n = Number(v);
        if (Number.isInteger(n) && n >= 0 && n < N) out.push(n);
      }
    }
  } else if (value && typeof value === 'object') {
    for (const [k, on] of Object.entries(value)) {
      const n = Number(k);
      if (on && Number.isInteger(n) && n >= 0 && n < N) out.push(n);
    }
  }
  return out;
}

function applyRouteState(payload) {
  if (!payload || typeof payload !== 'object') return;
  const list = toPadList(payload.active_pads ?? payload.pads ?? payload.active ?? payload.routes);
  if (!list.length) return;
  wsPads.clear();
  for (const i of list) wsPads.add(i);
  lastPadMsgAt = Date.now();
  randomPads.clear();
  recomputeActive();
}

function applyPadActivation(payload) {
  if (!payload || typeof payload !== 'object') return;
  const raw = payload.index ?? payload.pad ?? payload.idx ?? payload.harmonic;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 0 || n >= N) return;
  const on = payload.active ?? payload.on ?? payload.state ?? payload.value ?? true;
  if (on) wsPads.add(n); else wsPads.delete(n);
  lastPadMsgAt = Date.now();
  randomPads.clear();
  recomputeActive();
}

function handleWSMessage(data) {
  let msg;
  try { msg = JSON.parse(data); } catch (_e) { return; }
  if (!msg || typeof msg !== 'object') return;
  const type = msg.type || '';
  const payload = (msg.payload && typeof msg.payload === 'object') ? msg.payload : {};

  if (type === 'server.hello') {
    if (payload.gate_state === 'awaiting_client') {
      sendClient('client.hello', {
        client_id: clientId(),
        expected_contract_id: STAGE_CONTRACT_ID,
        supported_protocol_versions: [PROTOCOL_VERSION],
      });
    } else if (payload.gate_state === 'ready') {
      stageGated = true;
      wsState = 'live';
      updateStatus();
      sendClient('state.subscribe', { topics: ['sources'] });
    } else if (payload.gate_state === 'incompatible') {
      wsState = 'offline';
      updateStatus();
    }
    return;
  }

  if (type === 'state.snapshot') {
    applySourcesSnapshot(payload.sources);
    return;
  }

  if (type === 'state.event') {
    if (payload.topic === 'sources' && payload.action === 'source.channels_updated') {
      applySourceChannelsUpdated(payload);
    }
    return;
  }

  if (type === 'registry.source' && payload.action === 'derived_ready') {
    // Derived sources just appeared — re-subscribe for a fresh snapshot.
    if (stageGated) sendClient('state.subscribe', { topics: ['sources'] });
    return;
  }

  // Back-compat: ignore silently if never emitted.
  if (type === 'route_state') applyRouteState(payload);
  else if (type === 'pad_activation') applyPadActivation(payload);
}

function connectWS() {
  clearTimeout(reconnectTimer);
  stageGated = false;
  requestSeq = 0;
  handPads.hand_r_pad = null;
  handPads.hand_l_pad = null;
  for (const k of Object.keys(handPos)) handPos[k] = null;
  wsState = 'connecting';
  updateStatus();
  let socket;
  try {
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${scheme}//${location.host}/ws`);
  } catch (_e) {
    wsState = 'offline';
    updateStatus();
    reconnectTimer = setTimeout(connectWS, 3000);
    return;
  }
  ws = socket;
  socket.addEventListener('message', (ev) => handleWSMessage(ev.data));
  socket.addEventListener('close', () => {
    stageGated = false;
    wsState = 'offline';
    updateStatus();
    reconnectTimer = setTimeout(connectWS, 3000);
  });
  socket.addEventListener('error', () => { /* close handler drives reconnect */ });
}

// ---------------------------------------------------------------------------
// Placeholder simulation — keeps the grid alive until the WS feed exists.
// Every 6s toggle 2-3 pads, but stand down while real pad data is arriving.
// ---------------------------------------------------------------------------
function startSimulation() {
  setInterval(() => {
    if (Date.now() - lastPadMsgAt < 12000) return;  // real feed is driving; stay quiet
    const count = 2 + Math.floor(Math.random() * 2);  // 2 or 3
    for (let k = 0; k < count; k++) {
      const idx = Math.floor(Math.random() * N);
      if (randomPads.has(idx)) randomPads.delete(idx); else randomPads.add(idx);
    }
    recomputeActive();
  }, 6000);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
updateStatus();
initCamera();
connectWS();
startSimulation();
requestAnimationFrame(frame);
