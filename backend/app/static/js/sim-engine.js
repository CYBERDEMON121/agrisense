// Engine internals and the raw event stream.
//
// The simulator console is where you go when you want to know what the platform
// is actually doing rather than what the farm looks like: how many messages the
// bus carried, how fast the engine is ticking, what the topic grammar is, and
// the live tail of every publish.  None of that belongs on the dashboard.

import { $, el, setText, round } from './dom.js';
import { api } from './api.js';

const READOUTS = [
  { key: 'tick_hz', label: 'Tick', unit: 'Hz' },
  { key: 'tps', label: 'Bus msgs/s', unit: '' },
  { key: 'sim_days', label: 'Sim days', unit: '' },
  { key: 'retained', label: 'Retained', unit: '' },
  { key: 'subs', label: 'Subscriptions', unit: '' },
  { key: 'db_rows', label: 'DB rows', unit: '' },
  { key: 'db_queue', label: 'DB queue', unit: '' },
  { key: 'sensors', label: 'Probes', unit: '' },
  { key: 'cameras', label: 'Cameras', unit: '' },
];

export class EnginePanel {
  constructor() {
    this.name = 'engine';
    this.live = null;
    this.rows = null;
    this.sig = '';
    this.cells = new Map();
    const root = $('#engineStats');
    for (const item of READOUTS) {
      const value = el('b', {}, [document.createTextNode('—')]);
      root.append(el('div', { class: 'readout' }, [el('label', { text: item.label }), value]));
      this.cells.set(item.key, { value, item });
    }
    $('#engineRefresh').addEventListener('click', () => this.refresh().catch((e) => console.warn(e)));
  }

  async refresh() {
    const [config, controls] = await Promise.all([api.config(), api.controls()]);
    this.rows = [
      ['transport', config.transport?.mode || 'simulated'],
      ['broker', config.transport?.broker || 'in-process bus'],
      ['telemetry rows', config.settings?.write_telemetry ? 'persisted (sqlite)' : 'not persisted'],
      ['plots / areas / zones',
        `${controls.areas.reduce((n, a) => n + a.plots, 0)} / ${controls.areas.length} / ${controls.zones.length}`],
      ['seed', String(config.settings?.seed ?? '-')],
      ['tick rate', `${config.settings?.tick_hz ?? '-'} Hz`],
      ['retention', `${config.settings?.telemetry_retention_h ?? '-'} h`],
    ];
    const body = $('#engineRows');
    body.replaceChildren(...this.rows.map(([k, v]) => el('tr', {}, [
      el('th', { text: k }),
      el('td', { text: v }),
    ])));

    const topics = $('#engineTopics');
    if (!topics.options.length) {
      for (const [filter, note] of Object.entries(config.topics || {})) {
        topics.append(el('option', { value: filter, text: `${filter} — ${note}` }));
      }
    }
  }

  update(frame) {
    const sim = frame.sim || {};
    const bus = frame.bus || {};
    const db = frame.db || {};
    const sensors = frame.sensors || {};
    const values = {
      tick_hz: round(sim.tick_hz, 2),
      tps: round((bus.published || 0) / Math.max(sim.uptime_s || 1, 1), 2),
      sim_days: round(sim.sim_days, 2),
      retained: bus.retained || 0,
      subs: bus.subscriptions || 0,
      db_rows: db.rows || 0,
      db_queue: db.pending ?? db.queued ?? 0,
      sensors: `${sensors.online ?? 0}/${sensors.count ?? 0}`,
      cameras: (frame.cameras || []).length,
    };
    for (const [key, cell] of this.cells) {
      setText(cell.value.firstChild, String(values[key] ?? '—'));
      if (cell.value.children.length === 0 && cell.item.unit) {
        cell.value.append(document.createTextNode(' '), el('small', { text: cell.item.unit }));
      }
    }

    // The fault list only changes when the operator changes it, so rebuild it on
    // a signature rather than every frame.
    const faults = (frame.devices || []).filter((d) => d.status && d.status !== 'online');
    const sig = faults.map((d) => d.device_id).join('|');
    if (sig !== this.sig) {
      this.sig = sig;
      const body = $('#engineFaults');
      body.replaceChildren(...(faults.length
        ? faults.map((d) => el('tr', {}, [
          el('th', { text: d.device_id }),
          el('td', { text: `${d.kind} · ${d.status}` }),
        ]))
        : [el('tr', {}, [el('td', { colspan: '2', class: 'empty', text: 'no faults injected' })])]));
    }
  }
}

/**
 * Live tail of the event bus.
 *
 * Fed from the frame's `recent_events` rather than a second socket: the payload
 * is already there, and a second connection would just be another thing that can
 * silently stop.
 */
export class EventStream {
  constructor() {
    this.name = 'events';
    this.live = null;
    this.root = $('#eventRows');
    this.count = 0;
    this.filter = '';
    this.#buildFilter();
  }

  #buildFilter() {
    const input = $('#eventFilter');
    input.addEventListener('input', () => {
      this.filter = input.value.trim().toLowerCase();
      this.root.replaceChildren();
      this.count = 0;
    });
  }

  update(frame) {
    const events = frame.recent_events || [];
    if (!events.length) return;
    const add = [];
    for (const ev of events) {
      const topic = String(ev.topic || '');
      if (this.filter && !topic.toLowerCase().includes(this.filter)
        && !String(ev.event || '').toLowerCase().includes(this.filter)) continue;
      add.push(el('tr', {}, [
        el('td', { class: 'mono', text: hhmm(ev.ts) }),
        el('td', { class: 'mono topic', text: shortTopic(topic) }),
        el('td', { text: summarise(ev) }),
      ]));
      this.count += 1;
      if (this.count >= 40) break;
    }
    if (!add.length) return;
    this.root.prepend(...add);
    while (this.root.children.length > 60) this.root.lastElementChild.remove();
  }
}

function shortTopic(topic) {
  const parts = String(topic).split('/');
  return parts.length > 4 ? parts.slice(-3).join('/') : topic;
}

const EVENT_TEXT = {
  engine_started: 'engine started',
  speed: 'clock speed',
  pause: 'pause',
  thresholds: 'thresholds changed',
  step: 'manual step',
  weather_override: 'weather override',
  valve_open: 'valve open',
  valve_close: 'valve close',
  complete: 'mission complete',
  abort: 'mission aborted',
  alert: 'alert raised',
};

function summarise(ev) {
  if (ev.event && EVENT_TEXT[ev.event]) {
    const bits = [EVENT_TEXT[ev.event]];
    if (ev.event === 'speed') bits.push(`${ev.speed}x`);
    if (ev.event === 'pause') bits.push(ev.paused ? 'paused' : 'running');
    if (ev.event === 'step') bits.push(`${Math.round(ev.sim_seconds / 60)} min`);
    if (ev.event === 'valve_open' || ev.event === 'valve_close') {
      bits.push(`${ev.zone} ${ev.applied_mm ? `${ev.applied_mm} mm` : ''}`.trim());
    }
    if (ev.event === 'alert') bits.push(`${ev.level}: ${ev.message}`);
    if (ev.event === 'weather_override') bits.push(ev.scenario || '');
    if (ev.event === 'thresholds') bits.push(JSON.stringify(ev.thresholds));
    return bits.filter(Boolean).join(' · ');
  }
  if (ev.action) return `${ev.action} ${ev.reason || ev.zone || ''}`.trim();
  if (ev.event) return String(ev.event);
  if (ev.severity !== undefined) return `detection severity ${round(ev.severity, 2)}`;
  return JSON.stringify(ev).slice(0, 80);
}

function hhmm(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}