// Shared console runtime.
//
// Two pages sit on top of the same engine and the same live frame: the
// dashboard (read) and the simulator console (write).  Everything they have in
// common lives here - the panel registry, the frame dispatch, the connection
// badge, the clock-speed transport, the footer and the boot sequence - so the
// two pages differ only in which panels they register.

import { $, setText, setTimeOffset } from './dom.js';
import { api, Live, refreshOnce } from './api.js';

/** Clock speeds the slider selects, in simulated seconds per wall second. */
export const SPEEDS = [1, 5, 15, 60, 300, 900, 1800];

function logDistance(a, b) {
  return Math.abs(Math.log(Math.max(a, 1e-3)) - Math.log(Math.max(b, 1e-3)));
}

/** Nearest slider position for a speed the engine picked (e.g. from a preset). */
export function speedIndex(speed) {
  let best = 0;
  for (let i = 1; i < SPEEDS.length; i += 1) {
    if (logDistance(SPEEDS[i], speed) < logDistance(SPEEDS[best], speed)) best = i;
  }
  return best;
}

/** Header clock: the farm's local time plus the day of the season. */
export class Clock {
  constructor() {
    this.name = 'clock';
    this.time = $('#simTime');
    this.date = $('#simDate');
    this.day = $('#simDay');
    this.farm = $('#farmName');
  }

  update(frame) {
    const sim = frame.sim || {};
    setText(this.time, sim.time);
    setText(this.date, sim.date);
    setText(this.day, `day ${sim.day_of_year}`);
    if (frame.farm && frame.farm.name) setText(this.farm, frame.farm.name);
  }
}

/** Footer read-out: how hard the engine and the bus are working. */
export class StatusLine {
  constructor() {
    this.name = 'footer';
    this.stats = $('#footStats');
    this.topic = $('#footTopic');
  }

  update(frame) {
    const bus = frame.bus || {};
    const db = frame.db || {};
    const sim = frame.sim || {};
    setText(this.stats,
      `tick ${sim.ticks} @ ${sim.tick_hz} Hz`
      + ` · ${sim.sim_days} sim days · ${sim.uptime_s}s up`
      + ` · bus ${bus.published || 0} msgs${bus.retained ? `, ${bus.retained} retained` : ''}`
      + `${db.rows ? ` · ${db.rows} rows written` : ''}`);
    setText(this.topic, `agri/${(frame.farm || {}).id}/#`);
  }
}

/** Clock speed, pause, and the keyboard shortcuts that drive them. */
export class Transport {
  constructor(live) {
    this.name = 'transport';
    this.live = live;
    this.slider = $('#speed');
    this.out = $('#speedOut');
    this.pause = $('#btnPause');
    this.speed = SPEEDS[Number(this.slider.value)] ?? 60;
    this.slider.addEventListener('input', () => this.applySlider());
    this.pause.addEventListener('click', () => this.togglePause());
    this.bindKeys();
  }

  applySlider() {
    this.speed = SPEEDS[Number(this.slider.value)] ?? 60;
    setText(this.out, `${this.speed}×`);
    this.live?.send({ cmd: 'speed', value: this.speed });
  }

  togglePause() {
    this.setPause(this.pause.dataset.paused !== 'true');
    this.live?.send({ cmd: 'pause', value: this.pause.dataset.paused === 'true' });
  }

  setPause(paused) {
    this.pause.dataset.paused = String(paused);
    setText(this.pause, paused ? '▶' : '❚❚');
    this.pause.setAttribute('aria-label', paused ? 'Resume simulation' : 'Pause simulation');
  }

  setSpeed(speed) {
    this.speed = speed;
    this.slider.value = String(speedIndex(speed));
    setText(this.out, `${speed}×`);
  }

  /** Preset buttons on the simulator console drive the same control. */
  setSpeedExact(speed) {
    const idx = speedIndex(speed);
    this.slider.value = String(idx);
    this.setSpeed(SPEEDS[idx] ?? speed);
    this.live?.send({ cmd: 'speed', value: this.speed });
  }

  nudge(delta) {
    const next = Math.max(0, Math.min(Number(this.slider.max), Number(this.slider.value) + delta));
    this.slider.value = String(next);
    this.applySlider();
  }

  bindKeys() {
    window.addEventListener('keydown', (ev) => {
      if (ev.target instanceof HTMLElement && ev.target.matches('input, textarea, select')) return;
      switch (ev.key) {
        case ' ':
          ev.preventDefault();
          this.togglePause();
          break;
        case '+': case '=':
          this.nudge(1);
          break;
        case '-': case '_':
          this.nudge(-1);
          break;
        case 'r':
          if (ev.shiftKey) this.live?.send({ cmd: 'rth' });   // shift+R: return home
          break;
        default:
          break;
      }
    });
  }

  /** Mirror server truth without echoing it back down the socket. */
  update(frame) {
    const sim = frame.sim || {};
    if (sim.speed !== undefined && sim.speed !== this.speed) this.setSpeed(sim.speed);
    if ((this.pause.dataset.paused === 'true') !== sim.paused) this.setPause(sim.paused);
  }
}

/**
 * Owns the live connection and the panel list.
 *
 * Panels are dumb: each one only needs `update(frame)`.  The registry is what
 * makes a broken panel survivable - a throw inside one is logged and the other
 * eleven keep painting.
 */
export class Console {
  /** @param {string} name - page id, used in log messages. */
  constructor(name) {
    this.name = name;
    this.panels = [];
    this.live = null;
    this.firstFrame = true;
    this.onFrame = (frame) => this.#frame(frame);
    this.onStatus = (state) => this.#status(state);
  }

  /** Register panels. `undefined` entries are ignored so optional panels can
   *  be passed inline: `use(new A(), maybeB)`. */
  use(...panels) {
    for (const panel of panels.flat()) {
      if (!panel) continue;
      panel.name = panel.name || panel.constructor.name;
      this.panels.push(panel);
    }
    return this;
  }

  /** Panels that ask to be redrawn on resize / theme change. */
  get painters() {
    return this.panels.filter((p) => typeof p.draw === 'function');
  }

  #frame(frame) {
    if (!frame || !frame.sim) return;
    setTimeOffset(frame.farm.tz_offset_h);
    for (const panel of this.panels) {
      try {
        panel.update(frame);
      } catch (err) {
        // One broken panel must not take the whole console down with it.
        console.error(`${panel.name || 'panel'} failed`, err);
      }
    }
    if (this.firstFrame) {
      this.firstFrame = false;
      document.dispatchEvent(new CustomEvent('agrisense:ready', { detail: frame }));
    }
  }

  #status(state) {
    const conn = $('#conn');
    if (!conn) return;
    conn.dataset.state = state;
    setText($('#connText'),
      state === 'live' ? 'live' : state === 'lost' ? 'reconnecting' : 'connecting');
    if (state !== 'lost') return;
    // The socket is the fast path, not the only path: keep the console alive off
    // the REST snapshot until the socket comes back.
    refreshOnce().then((frame) => {
      if (frame && conn.dataset.state === 'lost') this.onFrame(frame);
    });
  }

  /** Connect, paint once from REST, and wire the shared chrome. */
  start({ panels: extra } = {}) {
    this.live = new Live({ onFrame: this.onFrame, onStatus: this.onStatus });
    this.use(this.live, ...(extra || []));
    // Panels that send operator commands are built before the socket exists, so
    // hand them the live reference here rather than making every page juggle it.
    for (const panel of this.panels) {
      if (panel !== this.live && 'live' in panel) panel.live = this.live;
    }
    this.live.connect();

    refreshOnce().then((frame) => {
      if (frame) this.onFrame(frame);
    });

    // Charts and SVGs are sized from their element box; a window resize needs a
    // redraw.  Debounced so dragging a window edge does not thrash the canvases.
    let resizeTimer = 0;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        for (const panel of this.painters) {
          try {
            panel.draw();
          } catch (err) {
            console.error(`${panel.name} redraw failed`, err);
          }
        }
      }, 120);
    });

    window.agrisense = { api, live: this.live, console: this, panels: this.panels };
    return this.live;
  }
}

/** Boot a page, turning any startup failure into a visible connection badge. */
export function bootPage(name, build) {
  try {
    return build();
  } catch (err) {
    console.error(`${name} failed to start`, err);
    const conn = $('#conn');
    if (conn) conn.dataset.state = 'lost';
    setText($('#connText'), 'startup failed');
    throw err;
  }
}