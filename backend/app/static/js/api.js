// REST client + the single multiplexed WebSocket the dashboard runs on.

const BASE = '/api';

/** Fetch JSON and raise a useful error on failure. */
async function call(path, options) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch { /* non-JSON error body */ }
    throw new Error(`${res.status} ${detail}`);
  }
  return res.status === 204 ? null : res.json();
}

const post = (path) => call(path, { method: 'POST' });
const query = (params) => new URLSearchParams(
  Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
).toString();

export const api = {
  state: (opts = {}) => call(`/state?${query(opts)}`),
  summary: () => call('/summary'),
  plots: (area) => call(`/plots${area ? `?area=${encodeURIComponent(area)}` : ''}`),
  plot: (id) => call(`/plots/${encodeURIComponent(id)}`),
  plotHistory: (id, hours = 24) => call(`/plots/${encodeURIComponent(id)}/history?hours=${hours}`),
  areas: () => call('/areas'),
  devices: (kind) => call(`/devices${kind ? `?kind=${kind}` : ''}`),
  deviceHistory: (id, hours = 6) => call(`/devices/${encodeURIComponent(id)}/history?hours=${hours}`),
  weather: () => call('/weather'),
  irrigation: () => call('/irrigation'),
  drone: () => call('/drone'),
  missions: (limit = 40) => call(`/missions?limit=${limit}`),
  cameras: () => call('/cameras'),
  detections: (id) => call(`/cameras/${encodeURIComponent(id)}/detections?limit=40`),
  alerts: (limit = 100) => call(`/alerts?limit=${limit}`),
  events: (limit = 60) => call(`/events?limit=${limit}`),
  decisions: (limit = 60) => call(`/decisions?limit=${limit}`),
  config: () => call('/config'),
  health: () => call('/health'),

  setSpeed: (speed) => post(`/sim/speed?speed=${speed}`),
  setPaused: (paused) => post(`/sim/pause?paused=${paused}`),
  setAutomation: (flags) => post(`/automation?${query(flags)}`),

  // --- simulator console ---
  controls: () => call('/sim/controls'),
  step: (seconds) => post(`/sim/step?seconds=${seconds}`),
  setThresholds: (patch) => call('/sim/thresholds', {
    method: 'POST', body: JSON.stringify(patch),
  }),
  forceWeather: (opts) => post(`/sim/weather?${query(opts)}`),
  releaseWeather: () => post('/sim/weather/release'),
  injectPests: (opts) => post(`/sim/pests?${query(opts)}`),
  stressPlots: (opts) => post(`/sim/stress?${query(opts)}`),
  setFault: (deviceId, fault) => post(`/sim/fault/${encodeURIComponent(deviceId)}?fault=${fault}`),

  openZone: (zone, mm = 20) => post(`/irrigation/${encodeURIComponent(zone)}/open?target_mm=${mm}`),
  closeZone: (zone) => post(`/irrigation/${encodeURIComponent(zone)}/close`),
  planMission: (opts = {}) => post(`/drone/mission?${query(opts)}`),
  rth: () => post('/drone/rth'),
  abortMission: (reason = 'manual') => post(`/drone/abort?reason=${encodeURIComponent(reason)}`),
  ackAlert: (id) => post(`/alerts/${id}/ack`),
  ackAll: () => post('/alerts/ack-all'),
  streamUrl: (id) => `${BASE}/cameras/${encodeURIComponent(id)}/stream.mjpg?fps=2&t=${Date.now()}`,
};

/**
 * Live connection. One socket carries the state stream *and* the operator's
 * commands, so speed/zone/RTH controls need no extra round trip.
 */
export class Live {
  constructor({ onFrame, onStatus }) {
    this.onFrame = onFrame;
    this.onStatus = onStatus;
    this.socket = null;
    this.queue = [];
    this.retry = 0;
    this.stopped = false;
  }

  connect() {
    if (this.stopped) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.socket = new WebSocket(`${proto}://${location.host}${BASE}/ws`);

    this.socket.addEventListener('open', () => {
      this.retry = 0;
      this.onStatus?.('live');
      for (const msg of this.queue.splice(0)) this.socket.send(msg);
    });

    this.socket.addEventListener('message', (event) => {
      try {
        const frame = JSON.parse(event.data);
        if (frame.type === 'state') this.onFrame(frame);
      } catch (err) {
        console.warn('bad frame', err);
      }
    });

    this.socket.addEventListener('close', () => {
      this.onStatus?.('lost');
      if (this.stopped) return;
      // Exponential backoff, capped, so a server restart reconnects quietly.
      this.retry = Math.min(this.retry + 1, 6);
      setTimeout(() => this.connect(), 400 * 2 ** (this.retry - 1));
    });

    this.socket.addEventListener('error', () => this.socket?.close());
  }

  send(command) {
    const msg = JSON.stringify(command);
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(msg);
    else this.queue.push(msg);
  }

  close() {
    this.stopped = true;
    this.socket?.close();
  }
}

/** One-shot pull, used when the socket is down so the UI never goes blank. */
export async function refreshOnce() {
  try {
    return await api.state();
  } catch (err) {
    console.warn('fallback poll failed', err);
    return null;
  }
}
