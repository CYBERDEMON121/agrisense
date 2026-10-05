// Scenario injection: the buttons that make the automation rules misbehave.
//
// A closed loop you can never break is a demo.  Everything here is a *forcing
// function* on the world - weather, pests, soil water, device availability -
// chosen so that each one exercises a specific interlock in the rule engine.
// The point of the panel is that the button you press predicts what the rules
// will do, and you can watch them do it in the decision log beside it.

import { $, el, setText, bindRange } from './dom.js';
import { api } from './api.js';

/** Presets. `param` names map onto the weather-forcing query parameters. */
const WEATHER_PRESETS = [
  {
    id: 'rain_burst', label: 'Rain burst', tone: 'water',
    note: 'suppresses both irrigation and spraying',
    param: { rain_mm_h: 9, hours: 4 },
  },
  {
    id: 'dry_spell', label: 'Dry spell', tone: 'warn',
    note: 'clear skies, no rain - drives ET up',
    param: { rain_mm_h: 0, cloud: 5, hours: 48 },
  },
  {
    id: 'heat_wave', label: 'Heat wave', tone: 'danger',
    note: '42 °C, low humidity - heat stress',
    param: { temp_c: 42, cloud: 5, hours: 24 },
  },
  {
    id: 'gale', label: 'Gale', tone: 'danger',
    note: '11 m/s - the spray wind interlock holds',
    param: { wind_ms: 11, hours: 3 },
  },
  {
    id: 'calm_day', label: 'Calm + clear', tone: 'ok',
    note: '1 m/s and bright - the legal spray window',
    param: { wind_ms: 1, cloud: 8, hours: 6 },
  },
  {
    id: 'overcast', label: 'Overcast', tone: 'dim',
    note: 'full cloud - solar gate closes',
    param: { cloud: 96, hours: 12 },
  },
];

const FAULTS = ['offline', 'degraded', 'online'];

export class ScenarioPanel {
  constructor() {
    this.name = 'scenarios';
    this.live = null;
    this.controls = null;
    this.forcing = {};
    this.pending = false;
    this.area = '';
    this.#buildWeather();
    this.#buildField();
    this.#buildFaults();
    this.refresh().catch((err) => console.warn(err));
  }

  #buildWeather() {
    const grid = $('#scenWeather');
    for (const preset of WEATHER_PRESETS) {
      grid.append(el('button', {
        class: `scen-btn tone-${preset.tone}`,
        'data-scen': preset.id,
        title: preset.note,
        onclick: () => this.force(preset),
      }, [
        el('b', { text: preset.label }),
        el('small', { text: preset.note }),
      ]));
    }
    $('#scenRelease').addEventListener('click', () => {
      this.pending = true;
      api.releaseWeather()
        .then((res) => { this.forcing = res.forcing; this.status(res.message || 'weather released'); })
        .catch((err) => this.status(err.message))
        .finally(() => { this.pending = false; });
    });
  }

  #buildField() {
    const areaSel = $('#scenArea');
    areaSel.addEventListener('change', () => { this.area = areaSel.value; });
    bindRange($('#scenPressure'), $('#scenPressureOut'), (v) => `${v}%`);

    const bind = (id, fn) => {
      $(id).addEventListener('click', () => {
        this.pending = true;
        fn()
          .then((res) => this.status(describe(res)))
          .catch((err) => this.status(err.message))
          .finally(() => { this.pending = false; });
      });
    };
    bind('#scenPests', () => api.injectPests({
      area: this.area || undefined, pressure: Number($('#scenPressure').value) / 100,
    }));
    bind('#scenDry', () => api.stressPlots({ area: this.area || undefined, kind: 'dry' }));
    bind('#scenWet', () => api.stressPlots({ area: this.area || undefined, kind: 'wet' }));
  }

  #buildFaults() {
    this.faultDevice = $('#faultDevice');
    this.faultSel = $('#faultKind');
    $('#scenFault').addEventListener('click', () => {
      const device = this.faultDevice.value;
      const fault = this.faultSel.value;
      if (!device) return;
      this.pending = true;
      api.setFault(device, fault)
        .then((res) => this.status(`${res.device_id} -> ${res.status}`))
        .catch((err) => this.status(err.message))
        .finally(() => { this.pending = false; this.refresh().catch(() => {}); });
    });
    for (const kind of FAULTS) this.faultSel.append(el('option', { value: kind, text: kind }));
  }

  /** Buttons are disabled while a forcing call is in flight so double-clicks
   *  cannot stack two overrides with different durations. */
  async force(preset) {
    this.pending = true;
    this.#busy(true);
    try {
      const res = await api.forceWeather(preset.param);
      this.forcing = res.forcing;
      this.status(`${preset.label} forced for ${preset.param.hours} h`);
    } catch (err) {
      this.status(err.message);
    } finally {
      this.pending = false;
      this.#busy(false);
    }
  }

  #busy(busy) {
    for (const btn of document.querySelectorAll('#scenWeather .scen-btn')) btn.disabled = busy;
  }

  status(text) {
    setText($('#scenStatus'), text || 'idle');
  }

  /** Areas and device lists come from the server, never hard-coded here. */
  async refresh() {
    const controls = await api.controls();
    this.controls = controls;
    const sel = $('#scenArea');
    if (sel.options.length !== controls.areas.length + 1) {
      sel.replaceChildren(el('option', { value: '', text: 'whole farm' }));
      for (const area of controls.areas) {
        sel.append(el('option', { value: area.id, text: `${area.name} (${area.plots})` }));
      }
    }
    const wanted = ['', ...controls.sensors, ...controls.cameras].join('|');
    if (this.faultDevice.dataset.sig !== wanted) {
      this.faultDevice.dataset.sig = wanted;
      this.faultDevice.replaceChildren(
        el('option', { value: '', text: 'pick a device…' }),
        ...controls.sensors.map((id) => el('option', { value: id, text: `probe ${id}` })),
        ...controls.cameras.map((id) => el('option', { value: id, text: `camera ${id}` })),
      );
    }
    this.forcing = controls.forcing;
  }

  update(frame) {
    if (this.pending) return;
    const tag = $('#scenTag');
    const forcing = frame.sim_weather_override || {};
    const keys = Object.keys(forcing).filter((k) => k !== 'hours_left');
    tag.textContent = keys.length
      ? `forcing ${keys.map((k) => `${k} ${forcing[k]}`).join(', ')}`
      : 'model driven';
    tag.classList.toggle('on', keys.length > 0);

    const faults = (frame.devices || []).filter((d) => d.status && d.status !== 'online');
    const badge = $('#scenFaults');
    setText(badge, faults.length ? `${faults.length} device${faults.length > 1 ? 's' : ''} faulted` : 'all devices online');
    badge.classList.toggle('bad', faults.length > 0);
    if (faults.length && this.faultDevice.options.length > 1) this.refresh().catch(() => {});
  }
}

function describe(res) {
  if (res.plots) return `${res.count || res.plots} plots -> ${res.kind || 'injected'}`;
  if (res.moisture_pct !== undefined) return `${res.plots} plots in ${res.area} now ${res.moisture_pct}% moisture`;
  return JSON.stringify(res);
}