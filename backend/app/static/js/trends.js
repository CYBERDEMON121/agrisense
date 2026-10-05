// Plot trends: one canvas, one selected plot, several series.
//
// The x axis is "frames ago", not wall-clock time, because the clock speed can
// change under the operator.  Each frame is one push, and the tag shows how much
// simulated time the window actually covers so the label stays honest.

import { $, $$, el, setText, round, hms } from './dom.js';
import { api } from './api.js';
import { Chart } from './charts.js';

const VIEWS = [
  { id: 'field', label: 'Field average' },
  { id: 'plot', label: 'Selected plot' },
];

const SERIES = [
  { key: 'moisture', label: 'Moisture %', color: 'var(--water)', unit: '%' },
  { key: 'pest', label: 'Pest %', color: 'var(--pest)', unit: '%', scale: 100 },
  { key: 'health', label: 'Health %', color: 'var(--accent)', unit: '%', scale: 100 },
  { key: 'canopy', label: 'Canopy %', color: 'var(--sun)', unit: '%', scale: 100 },
  { key: 'disease', label: 'Disease %', color: 'var(--danger)', unit: '%', scale: 100 },
  { key: 'temp', label: 'Soil °C', color: 'var(--warn)', unit: '°C' },
];

const CAPACITY = 150;

export class Trends {
  constructor() {
    this.chart = new Chart($('#trendChart'), { padBottom: 24 });
    this.tag = $('#trendTag');
    this.picker = $('#chartPicker');
    this.view = 'field';
    this.plotId = null;
    this.series = new Map(SERIES.map((s) => [s.key, []]));
    this.firstTs = null;
    this.lastTs = null;
    this.lastFrame = null;
    this.enabled = new Set(['moisture', 'pest', 'health']);
    this.#buildPicker();
  }

  #buildPicker() {
    for (const view of VIEWS) {
      this.picker.append(el('button', {
        class: view.id === this.view ? 'on' : null,
        'data-view': view.id,
        text: view.label,
        onclick: () => {
          this.view = view.id;
          this.#markView();
          this.reset();
          this.draw();
        },
      }));
    }
    this.picker.append(el('span', { class: 'picker-sep' }));
    for (const s of SERIES) {
      const on = this.enabled.has(s.key);
      this.picker.append(el('button', {
        class: on ? 'on' : null,
        title: s.label,
        text: s.label,
        onclick: (ev) => {
          if (this.enabled.has(s.key)) this.enabled.delete(s.key);
          else this.enabled.add(s.key);
          ev.currentTarget.classList.toggle('on', this.enabled.has(s.key));
          this.draw();
        },
      }));
    }
  }

  #markView() {
    for (const b of $$('button[data-view]', this.picker)) {
      b.classList.toggle('on', b.dataset.view === this.view);
    }
  }

  reset() {
    for (const key of this.series.keys()) this.series.get(key).length = 0;
    this.firstTs = null;
  }

  /** The map dispatches this when an operator clicks a plot. */
  setPlot(plotId) {
    this.plotId = plotId;
    this.view = plotId ? 'plot' : 'field';
    this.#markView();
    this.reset();
    // Repaint straight away rather than waiting for the next frame: the click
    // is a deliberate switch of scope and a stale tag reads as the wrong plot.
    const label = plotId ? plotId : `${this.lastFrame?.field.plots || 0} plots`;
    setText(this.tag, plotId
      ? `${label} · no history yet`
      : `${label} · collecting…`);
    this.draw();
  }

  update(frame) {
    this.lastFrame = frame;
    if (frame.sim.paused) return;      // don't stack identical points while paused
    const plots = frame.plots || [];
    if (!plots.length) return;
    const target = this.view === 'plot' && this.plotId
      ? plots.find((p) => p.id === this.plotId)
      : null;
    if (this.view === 'plot' && !target) {
      setText(this.tag, 'no plot selected');
      return;
    }

    const mean = (key) => (target
      ? target[key]
      : plots.reduce((acc, p) => acc + p[key], 0) / plots.length);
    for (const s of SERIES) {
      const value = mean(s.key) * (s.scale || 1);
      const arr = this.series.get(s.key);
      arr.push(Number.isFinite(value) ? value : 0);
      if (arr.length > CAPACITY) arr.shift();
    }
    if (this.firstTs === null) this.firstTs = frame.sim.ts;
    this.lastTs = frame.sim.ts;
    this.draw();
  }

  draw() {
    const active = SERIES.filter((s) => this.enabled.has(s.key));
    const data = active.map((s) => ({ label: s.label, color: s.color, data: this.series.get(s.key) }));
    this.chart.render(data, {
      title: `${active.map((s) => s.label).join(' · ') || 'pick a series'} — last ${data[0]?.data.length || 0} frames`,
    });
    if (this.lastTs && this.firstTs !== null) {
      const hours = (this.lastTs - this.firstTs) / 3600;
      const where = this.view === 'plot' && this.plotId
        ? this.plotId
        : `${this.lastFrame?.field.plots || 0} plots`;
      setText(this.tag, `${where} · ${hms(this.firstTs)}→${hms(this.lastTs)} `
        + `(${round(hours, 2)} sim h)`);
    }
  }

  /** Called on the first frame so the chart has an axis before any history. */
  prime(frame) {
    if (this.series.get('moisture').length) return;
    this.update(frame);
  }

  /** Historical series for one plot from the stored telemetry table. */
  async loadHistory(plotId, hours = 6) {
    const { series } = await api.plotHistory(plotId, hours);
    this.series.get('moisture').length = 0;
    this.series.get('pest').length = 0;
    this.series.get('health').length = 0;
    this.series.get('temp').length = 0;
    for (const row of series) {
      this.series.get('moisture').push((row.moisture / 100) * 100);
      this.series.get('pest').push(row.pest * 100);
      this.series.get('health').push(row.health * 100);
      this.series.get('temp').push(row.temp);
    }
    this.firstTs = series[0]?.ts ?? null;
    this.lastTs = series[series.length - 1]?.ts ?? null;
    this.draw();
  }
}