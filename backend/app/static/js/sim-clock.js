// Clock control: speed presets and manual stepping.
//
// The dashboard's slider chooses between seven speeds.  A simulator needs one
// more thing: the ability to move the world forward *without* the wall clock,
// so a single rule transition can be caught in the act.  Stepping is the reason
// this page exists rather than a settings dialog.

import { $, el, setText } from './dom.js';
import { api } from './api.js';
import { SPEEDS } from './core.js';

/** Step sizes in simulated seconds; the label is what the operator reads. */
const STEPS = [
  { id: 'step1m', seconds: 60, label: '+1 min' },
  { id: 'step10m', seconds: 600, label: '+10 min' },
  { id: 'step1h', seconds: 3600, label: '+1 h' },
  { id: 'step6h', seconds: 21600, label: '+6 h' },
];

export class ClockPanel {
  constructor() {
    this.name = 'clock';
    this.live = null;
    this.transport = null;
    this.stats = $('#clockStats');
    this.tag = $('#clockTag');
    this.busy = false;

    for (const step of STEPS) {
      const button = $(`#${step.id}`);
      if (button) button.addEventListener('click', () => this.step(step));
    }

    // Presets make the slider's seven detents discoverable, and let the operator
    // jump straight to "a full day in a minute".
    const presets = $('#speedPresets');
    presets.replaceChildren(...SPEEDS.map((speed) => el('button', {
      class: 'preset', 'data-speed': String(speed), text: `${speed}×`,
      onclick: () => this.setSpeed(speed),
    })));
  }

  /** `Transport` is injected after construction, so look it up lazily. */
  #transport() {
    return this.transport || (this.transport =
      (window.agrisense?.console?.panels || []).find((p) => p.name === 'transport') || null);
  }

  setSpeed(speed) {
    const transport = this.#transport();
    if (transport?.setSpeedExact) transport.setSpeedExact(speed);
    else this.live?.send({ cmd: 'speed', value: speed });
  }

  async step(step) {
    if (this.busy) return;
    this.busy = true;
    // Stepping is a POST that runs the engine forward synchronously; a second
    // click while it is in flight would queue a second advance.
    const buttons = this.#buttons();
    for (const btn of buttons) btn.disabled = true;
    try {
      const res = await api.step(step.seconds);
      setText(this.tag, `stepped ${Math.round(res.sim_seconds / 60)} min in ${res.slices} slice(s)`);
    } catch (err) {
      setText(this.tag, `step failed: ${err.message}`);
    } finally {
      this.busy = false;
      for (const btn of this.#buttons()) btn.disabled = false;
    }
  }

  #buttons() {
    return document.querySelectorAll('button[id^="step"]');
  }

  update(frame) {
    const sim = frame.sim || {};
    const speed = sim.speed ?? 0;
    for (const btn of document.querySelectorAll('#speedPresets .preset')) {
      btn.classList.toggle('on', Number(btn.dataset.speed) === speed);
    }
    setText(this.tag, sim.paused
      ? `paused · ${sim.sim_days} sim days done`
      : `${sim.sim_days} sim days done`);
    this.tag.classList.toggle('on', !!sim.paused);

    const rows = [
      ['speed', `${speed}× (${sim.speed === 1 ? 'real time' : `${Math.round(60 / Math.max(speed, 1))}× real`})`],
      ['simulated time', `${sim.time} · day ${sim.day_of_year}`],
      ['elapsed', `${sim.sim_hours} h simulated over ${sim.uptime_s}s wall`],
      ['ticks', `${sim.ticks} at ${sim.tick_hz} Hz`],
      ['paused', sim.paused ? 'yes — stepping by hand' : 'no — running'],
    ];
    this.stats.replaceChildren(...rows.flatMap(([k, v]) => [
      el('dt', { text: k }),
      el('dd', { text: v }),
    ]));
  }
}