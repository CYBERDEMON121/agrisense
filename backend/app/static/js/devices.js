// Fleet table: every device on the bus, filterable by kind.
//
// The MQTT topic column is the point of the table.  Anything in this platform
// can be rebuilt as "subscribe to this topic", so showing the topic makes the
// data model legible instead of magical.

import { $, $$, el, round, toggleClass } from './dom.js';

/** The topic prefix a device of each kind publishes under. */
function topicFor(device) {
  const farm = 'agri/green-valley-01';
  const id = device.device_id;
  switch (device.kind) {
    case 'soil': return `${farm}/soil/${id}/telemetry`;
    case 'weather': return `${farm}/weather/${id}/telemetry`;
    case 'camera': return `${farm}/camera/${id}/detections`;
    case 'drone': return `${farm}/drone/${id}/telemetry`;
    case 'pump':
    case 'tank': return `${farm}/irrigation/${id}/telemetry`;
    case 'valve': return `${farm}/irrigation/${id}/state`;
    default: return `${farm}/irrigation/system/telemetry`;
  }
}

const KIND_LABEL = {
  soil: 'Soil probes', weather: 'Weather', camera: 'Cameras', drone: 'Drone',
  pump: 'Pumps', tank: 'Storage', valve: 'Valves', controller: 'Controller',
};

/** `Set` has no structural equality of its own, and this runs every frame. */
function sameSet(a, b) {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

/** Sort order groups a farm operator's mental model, not the alphabet. */
const KIND_RANK = {
  drone: 0, camera: 1, controller: 2, valve: 3, pump: 4, tank: 5,
  weather: 6, soil: 7,
};

export class DeviceTable {
  constructor() {
    this.root = $('#deviceRows');
    this.filterRow = $('#deviceFilter');
    this.kinds = new Set();
    this.active = 'all';
    this.lastDevices = [];
    this.lastIds = '';
    this.#filterButton('all', 'All');
  }

  #filterButton(kind, label) {
    const node = el('button', {
      'data-kind': kind,
      class: kind === 'all' ? 'on' : null,
      text: label,
      onclick: () => {
        this.active = kind;
        for (const b of $$('button', this.filterRow)) toggleClass(b, 'on', b.dataset.kind === kind);
        this.lastIds = '';      // force a re-render on the next frame
      },
    });
    this.filterRow.append(node);
    return node;
  }

  update(frame) {
    this.lastDevices = frame.devices || [];
    // The kind list is discovered from the fleet rather than hard-coded, so a
    // device type added on the server shows up here with no JS change.
    const kinds = new Set(this.lastDevices.map((d) => d.kind));
    if (!sameSet(kinds, this.kinds)) {
      this.kinds = kinds;
      const known = new Set($$('button', this.filterRow).map((b) => b.dataset.kind));
      for (const kind of kinds) {
        if (known.has(kind)) continue;
        this.#filterButton(kind, KIND_LABEL[kind] || kind);
      }
      if (this.active !== 'all' && !kinds.has(this.active)) this.active = 'all';
      for (const b of $$('button', this.filterRow)) toggleClass(b, 'on', b.dataset.kind === this.active);
    }

    const devices = this.lastDevices
      .filter((d) => this.active === 'all' || d.kind === this.active)
      .sort((a, b) => (KIND_RANK[a.kind] ?? 9) - (KIND_RANK[b.kind] ?? 9)
        || a.device_id.localeCompare(b.device_id));

    // Row identity changes only when the filtered set does, so a status change
    // never destroys a row the operator is reading.
    const ids = devices.map((d) => d.device_id).join(',');
    if (ids === this.lastIds && this.cells) {
      this.#paint(devices);
      return;
    }
    this.lastIds = ids;
    this.cells = new Map();
    const rows = devices.map((device) => {
      const status = el('span', { class: 'dot-status', 'data-s': device.status }, [device.status]);
      const batteryText = el('span', { text: '' });
      const batteryFill = el('i');
      const battery = device.battery === null || device.battery === undefined
        ? el('span', { class: 'mono', text: 'mains' })
        : el('span', { class: 'batt' }, [
          el('span', { class: 'mini' }, [batteryFill]),
          batteryText,
        ]);
      const row = el('tr', { 'data-id': device.device_id }, [
        el('td', {}, [el('b', { text: device.name }), el('div', { class: 'mono', text: device.device_id })]),
        el('td', { class: 'mono', text: KIND_LABEL[device.kind] || device.kind }),
        el('td', { class: 'mono', text: device.area || '—' }),
        el('td', { class: 'mono', text: `${device.vendor} ${device.model} · fw ${device.fw}` }),
        el('td', {}, [status]),
        el('td', {}, [battery]),
        el('td', { class: 'mono', text: device.rssi === null || device.rssi === undefined
          ? '—' : `${device.rssi} dBm` }),
        el('td', { class: 'topic', text: topicFor(device) }),
      ]);
      this.cells.set(device.device_id, { status, batteryFill, batteryText });
      return row;
    });
    this.root.replaceChildren(...rows);
    this.#paint(devices);
  }

  /** Per-tick work is three attribute writes per device, nothing structural. */
  #paint(devices) {
    for (const device of devices) {
      const cell = this.cells.get(device.device_id);
      if (!cell) continue;
      if (cell.status.textContent !== device.status) cell.status.textContent = device.status;
      if (cell.status.dataset.s !== device.status) cell.status.dataset.s = device.status;
      if (device.battery !== null && device.battery !== undefined) {
        const pct = `${round(device.battery, 0)}%`;
        if (cell.batteryText.textContent !== pct) cell.batteryText.textContent = pct;
        const w = `${Math.max(0, Math.min(100, device.battery))}%`;
        if (cell.batteryFill.style.width !== w) cell.batteryFill.style.width = w;
        const colour = device.battery < 15 ? 'var(--danger)'
          : device.battery < 35 ? 'var(--warn)' : 'var(--accent)';
        if (cell.batteryFill.style.background !== colour) cell.batteryFill.style.background = colour;
      }
    }
  }

  /** Fleet summary for the footer. */
  static summarise(devices) {
    const nominal = devices.filter((d) => d.status === 'online' || d.status === 'open'
      || d.status === 'idle').length;
    return `${devices.length} devices · ${nominal} nominal`;
  }
}