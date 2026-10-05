// Canvas line/area charts.
//
// One small renderer serves every chart on the page (weather drive, water flow,
// plot trends).  It draws no DOM legend: the caller owns the markup, so a chart
// stays a single node that can be replaced or resized cheaply.

import { cssVar, round, clamp } from './dom.js';

/** Colour tokens resolved at draw time, so a theme switch just works. */
const THEME = () => ({
  grid: cssVar('--border-soft', '#1b2530'),
  axis: cssVar('--text-faint', '#6b7d91'),
  text: cssVar('--text-dim', '#9fb0c3'),
  surface: cssVar('--bg-panel', '#151b23'),
});

export class Chart {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{padLeft?:number, padRight?:number, padTop?:number, padBottom?:number}} opts
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.pad = {
      left: opts.padLeft ?? 34,
      right: opts.padRight ?? 8,
      top: opts.padTop ?? 8,
      bottom: opts.padBottom ?? 18,
    };
    /** Optional horizontal reference line(s): {value, label, color}. */
    this.refs = [];
    /** Fixed y-range; when null the range is derived from the data. */
    this.yMin = null;
    this.yMax = null;
    this.yUnit = '';
    this.lastWidth = 0;
    this.lastHeight = 0;
  }

  /** Size the backing store to the element's CSS box at device resolution. */
  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    const cssW = Math.max(rect.width || this.canvas.clientWidth || 300, 1);
    const cssH = Number(this.canvas.getAttribute('height')) || rect.height || 120;
    const w = Math.round(cssW * dpr);
    const h = Math.round(cssH * dpr);
    if (this.canvas.width !== w) this.canvas.width = w;
    if (this.canvas.height !== h) this.canvas.height = h;
    this.lastWidth = cssW;
    this.lastHeight = cssH;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: cssW, h: cssH };
  }

  /**
   * @param {Array<{label:string,color:string,data:number[],fill?:boolean}>} series
   * @param {{title?:string, yMin?:number, yMax?:number, yUnit?:string, xLabels?:string[]}} opts
   */
  render(series, opts = {}) {
    if (opts.yMin !== undefined) this.yMin = opts.yMin;
    if (opts.yMax !== undefined) this.yMax = opts.yMax;
    if (opts.yUnit !== undefined) this.yUnit = opts.yUnit;

    const { w, h } = this.resize();
    const ctx = this.ctx;
    const theme = THEME();
    ctx.clearRect(0, 0, w, h);

    const live = (series || []).filter((s) => Array.isArray(s.data) && s.data.length);
    const plot = {
      x: this.pad.left,
      y: this.pad.top,
      w: Math.max(w - this.pad.left - this.pad.right, 10),
      h: Math.max(h - this.pad.top - this.pad.bottom, 10),
    };
    if (!live.length) {
      this.#frame(ctx, plot, theme);
      ctx.fillStyle = theme.axis;
      ctx.font = '11px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(opts.title || 'no data yet', w / 2, h / 2);
      return;
    }

    const [lo, hi] = this.#range(live);
    this.#grid(ctx, plot, theme, lo, hi);
    for (const s of live) this.#series(ctx, plot, s, lo, hi);
    for (const ref of this.refs) this.#refLine(ctx, plot, lo, hi, ref);
    this.#axis(ctx, plot, theme, lo, hi, w, h);
    if (opts.title) {
      ctx.fillStyle = theme.text;
      ctx.font = '600 10px system-ui, sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText(opts.title, plot.x, Math.max(h - 3, 12));
    }
  }

  #range(live) {
    if (this.yMin !== null && this.yMax !== null) return [this.yMin, this.yMax];
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of live) {
      for (const v of s.data) {
        const n = Number(v);
        if (!Number.isFinite(n)) continue;
        if (n < lo) lo = n;
        if (n > hi) hi = n;
      }
    }
    if (!Number.isFinite(lo)) return [0, 1];
    if (this.yMin !== null) lo = this.yMin;
    if (this.yMax !== null) hi = this.yMax;
    if (hi - lo < 1e-9) {
      // A flat series still deserves a band, otherwise it draws on the axis.
      lo -= 0.5;
      hi += 0.5;
    }
    const margin = (hi - lo) * 0.12;
    return [Math.min(lo, this.yMin ?? lo) - margin, Math.max(hi, this.yMax ?? hi) + margin];
  }

  #grid(ctx, plot, theme, lo, hi) {
    ctx.strokeStyle = theme.grid;
    ctx.lineWidth = 1;
    ctx.font = '9.5px ui-monospace, monospace';
    ctx.fillStyle = theme.axis;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 3; i += 1) {
      const t = i / 3;
      const y = Math.round(plot.y + plot.h * t) + 0.5;
      ctx.beginPath();
      ctx.moveTo(plot.x, y);
      ctx.lineTo(plot.x + plot.w, y);
      ctx.stroke();
      const value = hi - (hi - lo) * t;
      ctx.fillText(this.#fmt(value), plot.x - 5, y);
    }
  }

  #axis(ctx, plot, theme, lo, hi, w, h) {
    ctx.fillStyle = theme.axis;
    ctx.font = '9.5px ui-monospace, monospace';
    if (this.yUnit) {
      ctx.textAlign = 'left';
      ctx.fillText(this.yUnit, 2, plot.y + 5);
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(`${round(lo, 2)}`, plot.x, h - 4);
    ctx.textAlign = 'right';
    ctx.fillText(`now · ${round(hi, 2)}`, plot.x + plot.w, h - 4);
    void theme;
  }

  #fmt(value) {
    const v = Number(value) || 0;
    const span = Math.abs(v);
    if (span >= 1000) return `${round(v / 1000, 1)}k`;
    if (span >= 10) return round(v, 0).toString();
    if (span >= 1) return round(v, 1).toString();
    return round(v, 2).toString();
  }

  #series(ctx, plot, s, lo, hi) {
    const { data } = s;
    const n = data.length;
    const xAt = (i) => plot.x + (n === 1 ? plot.w / 2 : (i / (n - 1)) * plot.w);
    const yAt = (v) => plot.y + plot.h * (1 - clamp((Number(v) - lo) / (hi - lo || 1), 0, 1));

    // Area first so the line sits on top of its own fill.
    if (s.fill) {
      const grad = ctx.createLinearGradient(0, plot.y, 0, plot.y + plot.h);
      grad.addColorStop(0, withAlpha(s.color, 0.34));
      grad.addColorStop(1, withAlpha(s.color, 0.02));
      ctx.beginPath();
      ctx.moveTo(xAt(0), plot.y + plot.h);
      data.forEach((v, i) => ctx.lineTo(xAt(i), yAt(v)));
      ctx.lineTo(xAt(n - 1), plot.y + plot.h);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    }

    ctx.beginPath();
    data.forEach((v, i) => {
      const x = xAt(i);
      const y = yAt(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();

    // Mark the live end so the eye finds "now" without reading the axis.
    if (n) {
      ctx.beginPath();
      ctx.arc(xAt(n - 1), yAt(data[n - 1]), 2.2, 0, Math.PI * 2);
      ctx.fillStyle = s.color;
      ctx.fill();
    }
  }

  #refLine(ctx, plot, lo, hi, ref) {
    const value = Number(ref.value);
    if (!Number.isFinite(value) || value < lo || value > hi) return;
    const y = Math.round(plot.y + plot.h * (1 - (value - lo) / (hi - lo))) + 0.5;
    ctx.save();
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = ref.color || cssVar('--warn');
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
    ctx.stroke();
    if (ref.label) {
      ctx.setLineDash([]);
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillStyle = ref.color || cssVar('--warn');
      ctx.textAlign = 'left';
      ctx.fillText(ref.label, plot.x + 4, y - 3);
    }
    ctx.restore();
  }

  #frame(ctx, plot, theme) {
    ctx.strokeStyle = theme.grid;
    ctx.lineWidth = 1;
    ctx.strokeRect(plot.x + 0.5, plot.y + 0.5, plot.w, plot.h);
  }
}

/** Resolve a colour token or hex to rgba() so a fill can be faded. */
function withAlpha(color, alpha) {
  const c = String(color || '').trim();
  if (c.startsWith('rgb')) return c.replace(/rgba?\(([^)]+)\)/, (_, inner) => {
    const parts = inner.split(',').slice(0, 3).join(',');
    return `rgba(${parts},${alpha})`;
  });
  if (c.startsWith('#')) {
    const hex = c.slice(1);
    const full = hex.length === 3 ? hex.split('').map((ch) => ch + ch).join('') : hex;
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) || 0);
    return `rgba(${r},${g},${b},${alpha})`;
  }
  return c;
}

/** Fixed-size ring buffer for chart series. */
export class Series {
  constructor(capacity = 180) {
    this.capacity = capacity;
    this.data = [];
  }

  push(value) {
    const n = Number(value);
    this.data.push(Number.isFinite(n) ? n : 0);
    if (this.data.length > this.capacity) this.data.shift();
    return this.data;
  }

  get latest() {
    return this.data.length ? this.data[this.data.length - 1] : 0;
  }
}