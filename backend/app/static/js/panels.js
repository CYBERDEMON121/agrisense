// KPI header, weather readouts and the per-zone irrigation panel.

import { $, setText, setAttr, clamp, round, litres, el } from './dom.js';
import { api } from './api.js';

// --------------------------------------------------------------------------
// KPI strip
// --------------------------------------------------------------------------
export class Kpis {
  constructor() {
    this.nodes = {
      moisture: $('#kMoisture'), moistureSub: $('#kMoistureSub'), barMoisture: $('#barMoisture'),
      pest: $('#kPest'), pestSub: $('#kPestSub'), barPest: $('#barPest'),
      health: $('#kHealth'), healthSub: $('#kHealthSub'), barHealth: $('#barHealth'),
      tank: $('#kTank'), tankSub: $('#kTankSub'), barTank: $('#barTank'),
      drone: $('#kDrone'), droneSub: $('#kDroneSub'), barBattery: $('#barBattery'),
      alerts: $('#kAlerts'), alertsSub: $('#kAlertsSub'), barAlerts: $('#barAlerts'),
    };
    this.nodes.drone.classList.add('textual');
  }

  update(frame) {
    // Panels render on every frame, including a partial one during a socket
    // reconnect, so every level below the frame is defaulted rather than
    // destructured straight through.
    const { sim = {}, field = {}, irrigation = {}, drone = {}, automation = {} } = frame;

    setText(this.nodes.moisture, round(field.moisture_avg, 0));
    setText(this.nodes.moistureSub,
      `min ${round(field.moisture_min, 0)} · max ${round(field.moisture_max, 0)}%`);
    setAttr(this.nodes.barMoisture, 'style', `width:${clamp(field.moisture_avg, 0, 100)}%`);
    tint(this.nodes.barMoisture, field.moisture_avg, 40, 60);

    setText(this.nodes.pest, round(field.pest_max * 100, 0));
    setText(this.nodes.pestSub,
      `avg ${round(field.pest_avg * 100, 0)}% · threshold 55%`);
    setAttr(this.nodes.barPest, 'style', `width:${clamp(field.pest_max * 100, 0, 100)}%`);
    tint(this.nodes.barPest, field.pest_max, 0.35, 0.55);

    setText(this.nodes.health, round(field.health_avg * 100, 0));
    setText(this.nodes.healthSub,
      `canopy ${round(field.canopy_avg * 100, 0)}% · loss ${round(field.yield_loss_avg * 100, 0)}%`);
    setAttr(this.nodes.barHealth, 'style', `width:${clamp(field.health_avg * 100, 0, 100)}%`);
    tint(this.nodes.barHealth, field.health_avg, 0.4, 0.7);

    const tank = irrigation.tank || {};
    const pump = irrigation.pump || {};
    setText(this.nodes.tank, round(tank.level_pct, 0));
    setText(this.nodes.tankSub,
      `${litres(irrigation.daily_litres)} today · ${pump.source || '—'}`);
    setAttr(this.nodes.barTank, 'style', `width:${clamp(tank.level_pct, 0, 100)}%`);
    tint(this.nodes.barTank, tank.level_pct / 100, 0.15, 0.35);

    setText(this.nodes.drone, drone.state);
    setText(this.nodes.droneSub,
      `${round(drone.battery, 0)}% · ${round(drone.payload_l, 1)}/${round(drone.capacity_l, 0)} L`);
    setAttr(this.nodes.barBattery, 'style', `width:${clamp(drone.battery, 0, 100)}%`);
    tint(this.nodes.barBattery, drone.battery / 100, 0.25, 0.5);

    setText(this.nodes.alerts, automation.alerts_open);
    setText(this.nodes.alertsSub, automation.alerts_open === 0
      ? 'all clear'
      : `${automation.critical} critical · ${automation.warning} warning`);
    setAttr(this.nodes.barAlerts, 'style',
      `width:${clamp(automation.alerts_open * 12, 3, 100)}%`);
    this.nodes.barAlerts.style.background = automation.critical > 0
      ? 'var(--danger)'
      : automation.warning > 0 ? 'var(--warn)' : 'var(--accent)';
  }
}

/** Colour a bar green below `hi`, amber below `lo` (checked low first). */
function tint(bar, value, lo, hi) {
  const v = Number(value) || 0;
  bar.style.background = v < lo ? 'var(--danger)' : v < hi ? 'var(--warn)' : 'var(--accent)';
}

// --------------------------------------------------------------------------
// Weather panel
// --------------------------------------------------------------------------
function clockLabel(value) {
  const h = Math.floor(value);
  const m = Math.floor((value - h) * 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function compass(deg) {
  const points = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  return points[Math.round(((Number(deg) || 0) % 360) / 45) % 8];
}

const READOUTS = [
  { key: 'temp', label: 'Air', unit: '°C' },
  { key: 'humidity', label: 'RH', unit: '%' },
  { key: 'wind', label: 'Wind', unit: 'm/s', fmt: (w) => round(w.wind, 1) },
  { key: 'gust', label: 'Gust', unit: 'm/s' },
  { key: 'wind_dir', label: 'Dir', unit: '', fmt: (w) => `${compass(w.wind_dir)} ${round(w.wind_dir, 0)}°` },
  { key: 'rain', label: 'Rain', unit: 'mm' },
  { key: 'solar', label: 'Sun', unit: 'W/m²', fmt: (w) => round(w.solar, 0) },
  { key: 'et0', label: 'ET₀', unit: 'mm/d' },
  { key: 'pressure', label: 'Baro', unit: 'hPa' },
  { key: 'vpd', label: 'VPD', unit: 'kPa' },
  { key: 'cloud', label: 'Cloud', unit: '%' },
  { key: 'dew_point', label: 'Dew', unit: '°C' },
];

export class WeatherPanel {
  constructor(chart) {
    this.root = $('#weatherReadouts');
    this.hint = $('#weatherHint');
    this.cells = new Map();
    for (const item of READOUTS) {
      const value = el('b', {}, [document.createTextNode('—')]);
      const node = el('div', { class: 'readout' }, [el('label', { text: item.label }), value]);
      this.root.append(node);
      this.cells.set(item.key, { value, item });
    }
    this.history = { et0: [], rain: [], solar: [], temp: [], wind: [] };
    this.chart = chart;
  }

  update(frame) {
    const w = frame.weather;
    for (const [key, cell] of this.cells) {
      const text = cell.item.fmt ? cell.item.fmt(w) : round(w[key], 1);
      // The numeric part is its own text node so only it gets replaced.
      setText(cell.value.firstChild, text);
      if (cell.value.children.length === 0 && cell.item.unit) {
        cell.value.append(document.createTextNode(' '), el('small', { text: cell.item.unit }));
      }
    }

    // Call out only what changes what the automation rules are allowed to do.
    const notes = [];
    if (w.wind > 6) notes.push(`wind ${round(w.wind, 1)} m/s over the 6 m/s spray limit — missions held`);
    else if (w.wind > 4) notes.push(`wind ${round(w.wind, 1)} m/s will cost spray accuracy`);
    if (w.rain > 0.1) notes.push(`raining ${round(w.rain, 1)} mm/h — irrigation and spraying both held`);
    if (w.solar < 150) notes.push('below the 150 W/m² minimum for legal drone ops');
    setText(this.hint, notes.length
      ? notes.join(' · ')
      : `sunrise ${clockLabel(w.sunrise)} · sunset ${clockLabel(w.sunset)}`
        + ` · ${round(w.daylight_h, 1)} h daylight`);

    const h = this.history;
    push(h.et0, w.et0, 90);
    push(h.rain, w.rain, 90);
    push(h.solar, w.solar / 1000, 90);
    push(h.temp, w.temp, 90);
    push(h.wind, w.wind, 90);
    this.chart?.render([
      { label: 'ET₀ mm/d', color: 'var(--water)', data: h.et0, fill: true },
      { label: 'Air °C', color: 'var(--sun)', data: h.temp },
      { label: 'Wind m/s', color: 'var(--warn)', data: h.wind },
      { label: 'Solar kW/m²', color: 'var(--text-faint)', data: h.solar },
    ]);
  }
}

function push(arr, value, cap) {
  arr.push(Number(value) || 0);
  if (arr.length > cap) arr.shift();
}

// --------------------------------------------------------------------------
// Irrigation panel
// --------------------------------------------------------------------------
export class IrrigationPanel {
  constructor(live) {
    this.live = live;
    this.root = $('#zoneList');
    this.stats = $('#waterStats');
    this.pumpTag = $('#pumpTag');
    this.zones = new Map();
    this.flowHistory = [];
    this.lastStatsKey = '';
  }

  toggle(zoneId, open, mm = 20) {
    this.live.send({ cmd: 'zone', zone: zoneId, action: open ? 'open' : 'close', mm });
  }

  update(frame) {
    const irr = frame.irrigation || {};
    const p = irr.pump || {};
    const tank = irr.tank || {};
    const well = irr.borewell || {};
    setText(this.pumpTag, p.on
      ? `pump on · ${round(p.flow_lpm, 0)} L/min · ${p.source || '—'}`
      : `pump off · ${p.source || '—'}`);

    for (const zone of irr.zones || []) {
      let entry = this.zones.get(zone.id);
      if (!entry) {
        const fill = el('i');
        const button = el('button', {
          onclick: () => this.toggle(zone.id, !zone.open, 20),
        });
        const label = el('span');
        const applied = el('span');
        const node = el('div', { class: 'zone', 'data-zone': zone.id }, [
          el('div', { class: 'zone-top' }, [
            el('span', { class: 'zone-name', text: zone.name }),
            el('span', { class: 'zone-plots', text: `${zone.plots.length} plots` }),
            el('span', { class: 'zone-ctl' }, [button]),
          ]),
          el('div', { class: 'zone-bar' }, [el('div', { class: 'bar' }, [fill]), label, applied]),
        ]);
        this.root.append(node);
        entry = { node, fill, button, label, applied };
        this.zones.set(zone.id, entry);
      }
      const open = zone.open;
      setAttr(entry.node, 'data-open', open ? 'true' : 'false');
      setText(entry.button, open ? 'Close' : 'Open');
      setText(entry.label, `DU ${Math.round(zone.uniformity * 100)}%`);
      setText(entry.applied, open
        ? `${round(zone.applied_mm, 1)} / ${round(zone.target_mm, 0)} mm`
        : 'idle');
      const progress = zone.target_mm > 0
        ? clamp(zone.applied_mm / zone.target_mm, 0, 1) * 100 : 0;
      entry.fill.style.width = `${open ? progress : 0}%`;
      entry.fill.style.background = open ? 'var(--water)' : 'var(--text-faint)';
    }

    // Rebuild the definition list only when a value actually changed.
    const rows = [
      ['Flow', `${round(p.flow_lpm, 0)} L/min`],
      ['Pressure', `${round(p.pressure_bar, 2)} bar`],
      ['Source', p.source || '—'],
      ['Tank', `${litres(tank.litres)} / ${litres(tank.capacity_l)}`],
      ['Borewell', well.running
        ? 'running' : `standby · ${round(well.yield_lpm, 0)} L/min`],
      ['Pumped today', litres(irr.daily_litres)],
      ['Pumped total', litres(irr.cumulative_litres)],
      ['Energy', `${round(irr.energy_kwh, 1)} kWh`],
    ];
    const key = rows.map((r) => r[1]).join('|');
    if (key !== this.lastStatsKey) {
      this.lastStatsKey = key;
      this.stats.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
    }

    push(this.flowHistory, p.flow_lpm, 90);
    this.waterChart?.render([
      { label: 'Flow L/min', color: 'var(--water)', data: this.flowHistory, fill: true },
    ]);
  }

  attachChart(chart) {
    this.waterChart = chart;
  }
}
