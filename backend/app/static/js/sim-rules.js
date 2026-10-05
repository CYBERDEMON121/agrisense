// Rule tuning and manual overrides.
//
// Thresholds are sliders rather than a JSON box because the interesting question
// is never "what is the number", it is "what happens if the number were higher".
// Drag it, and the decision log two panels over tells you.
//
// Manual commands sit beside the sliders deliberately: disabling a rule and then
// driving the same device by hand is the fastest way to see whether a rule is
// helping or just churning.

import { $, el, setText, round, bindRange } from './dom.js';
import { api } from './api.js';

export class RulePanel {
  constructor() {
    this.name = 'rules';
    this.live = null;
    this.tunables = null;
    this.pending = false;
    this.toggles = {
      enabled: $('#tgAuto'),
      auto_spray: $('#tgSpray'),
      auto_irrigate: $('#tgIrr'),
    };
    for (const [key, input] of Object.entries(this.toggles)) {
      if (!input) continue;
      input.addEventListener('change', () => {
        // Flags go over REST, not the socket: they change how the engine
        // decides, so they deserve a real request/response and a real error.
        api.setAutomation({ [key]: input.checked })
          .catch((err) => {
            console.warn('automation toggle failed', err);
            input.checked = !input.checked;
          });
      });
    }
    // "Reset" means the shipped values, which the server sends alongside the
    // current ones - not "whatever the last frame happened to contain".
    $('#ruleReset').addEventListener('click', () => {
      const patch = {};
      for (const [name, spec] of Object.entries(this.tunables || {})) patch[name] = spec.default;
      this.#commit(patch, 'thresholds reset to defaults');
    });
    this.#load();
  }

  async #load() {
    try {
      const controls = await api.controls();
      this.#build(controls.tunables);
    } catch (err) {
      console.warn('thresholds unavailable', err);
    }
  }

  #build(tunables) {
    this.tunables = tunables;
    const root = $('#ruleTunables');
    root.replaceChildren();
    for (const [name, spec] of Object.entries(tunables)) {
      const value = el('output', { class: 'tunable-value' });
      const input = el('input', {
        type: 'range', min: String(spec.min), max: String(spec.max),
        // Pest threshold is a 0..1 fraction shown as a percentage; the rest of
        // the tunables are already in their display units.
        step: name === 'pest_spray_threshold' ? '0.01'
          : name === 'pest_confirm_ticks' ? '1'
            : String(round((spec.max - spec.min) / 100, 4)),
        value: String(spec.value), 'data-tunable': name,
      });
      const shown = (v) => (name === 'pest_spray_threshold' ? Math.round(v * 100) : round(v, 2));
      setText(value, `${shown(spec.value)} ${spec.unit}`);
      input.addEventListener('change', () => this.#commit(name, input, value, shown));
      root.append(el('label', { class: 'tunable' }, [
        el('span', { class: 'tunable-label' }, [
          el('b', { text: spec.label }),
          el('small', { text: name }),
        ]),
        input,
        value,
      ]));
    }
  }

  async #commit(name, input, readout, shown) {
    const raw = Number(input.value);
    const unit = this.tunables?.[name]?.unit ?? '';
    setText(readout, `${shown(raw)} ${unit}`.trim());
    return this.#push({ [name]: raw }, `${name} = ${shown(raw)} ${unit}`.trim());
  }

  /**
   * Send a patch and then adopt whatever the engine reports back.
   *
   * Rebuilding the sliders from the response is what makes the clamping and
   * cross-field validation visible: if `soil_moisture_high` is pushed below the
   * low threshold, the server refuses it and the UI has to say so.
   */
  async #push(patch, note) {
    this.pending = true;
    try {
      const res = await api.setThresholds(patch);
      this.#build(res.all);
      this.flash(note);
    } catch (err) {
      this.flash(err.message);
      this.#load();
    } finally {
      this.pending = false;
    }
  }

  flash(text) {
    setText($('#ruleFlash'), text);
  }

  update(frame) {
    const auto = frame.automation || {};
    if (!this.pending) {
      for (const [key, input] of Object.entries(this.toggles)) {
        if (input && auto[key] !== undefined) input.checked = auto[key];
      }
      // A second client (or a reset) can move a threshold; adopt it silently.
    if (this.tunables) {
      for (const input of document.querySelectorAll('#ruleTunables input[data-tunable]')) {
        const spec = this.tunables[input.dataset.tunable];
        if (spec && String(spec.value) !== input.value) {
          input.value = String(spec.value);
          const readout = input.parentElement.querySelector('.tunable-value');
          if (readout) {
            const shown = (v) => (input.dataset.tunable === 'pest_spray_threshold'
              ? Math.round(v * 100) : round(v, 2));
            setText(readout, `${shown(spec.value)} ${spec.unit}`);
          }
        }
      }
    }
    }
    const spray = auto.spray_hold_s || 0;
    const tag = $('#ruleTag');
    setText(tag, spray > 0 ? `rain hold ${Math.round(spray / 60)} min` : 'interlocks clear');
    tag.classList.toggle('warn', spray > 0);
  }
}

/** Manual device control: the levers the automation engine would otherwise hold. */
export class CommandPanel {
  constructor() {
    this.name = 'commands';
    this.live = null;
    this.controls = null;
    this.zone = '';
    this.flashText('');

    $('#cmdZone').addEventListener('change', () => { this.zone = $('#cmdZone').value; });
    bindRange($('#cmdMm'), $('#cmdMmOut'), (v) => `${v} mm`);
    bindRange($('#cmdDose'), $('#cmdDoseOut'), (v) => `${round(v / 100, 2)} L/ha`);
    $('#cmdOpen').addEventListener('click', () => this.#zone('open'));
    $('#cmdClose').addEventListener('click', () => this.#zone('close'));

    const plan = () => this.#run(
      () => api.planMission({
        area: $('#cmdArea').value || undefined,
        dose_l_ha: Number($('#cmdDose').value) / 100,
        launch: $('#cmdLaunch').checked,
      }),
      'mission');
    $('#cmdPlan').addEventListener('click', plan);
    $('#cmdRth').addEventListener('click', () => this.#run(() => api.rth(), 'RTH'));
    $('#cmdAbort').addEventListener('click', () => this.#run(() => api.abortMission('operator'), 'abort'));

    this.#load();
  }

  async #load() {
    try {
      const controls = await api.controls();
      this.controls = controls;
      $('#cmdZone').replaceChildren(...controls.zones.map((z) => el('option', {
        value: z.id, text: `${z.name} (${z.plots} plots)`,
      })));
      this.zone = controls.zones[0]?.id || '';
      $('#cmdArea').replaceChildren(
        el('option', { value: '', text: 'worst offenders' }),
        ...controls.areas.map((a) => el('option', { value: a.id, text: a.name })),
      );
    } catch (err) {
      console.warn('command panel unavailable', err);
    }
  }

  async #zone(action) {
    if (!this.zone) return;
    const mm = Number($('#cmdMm').value);
    await this.#run(
      () => (action === 'open'
        ? api.openZone(this.zone, mm)
        : api.closeZone(this.zone)),
      `zone ${this.zone} ${action}`);
  }

  async #run(fn, what) {
    this.flash(`${what}…`);
    try {
      const res = await fn();
      this.flash(`${what}: ok`);
      console.info(what, res);
      return res;
    } catch (err) {
      this.flash(`${what} failed: ${err.message}`);
      return null;
    }
  }

  flashText(text) {
    this.flash(text);
  }

  flash(text) {
    setText($('#cmdFlash'), text);
  }

  update(frame) {
    const drone = frame.drone || {};
    const state = $('#cmdDroneState');
    setText(state, drone.state || 'unknown');
    state.dataset.state = drone.state || 'unknown';
    const irr = frame.irrigation || {};
    const zones = irr.zones || [];
    const open = zones.filter((z) => z.open).map((z) => z.id).join(', ');
    setText($('#cmdZoneState'), open ? `${open} open` : 'all zones closed');
  }
}