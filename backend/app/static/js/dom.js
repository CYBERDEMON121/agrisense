// Tiny DOM + formatting helpers shared by every dashboard module.
// No framework: the live frame arrives twice a second and the whole point is
// to keep the update cost proportional to what actually changed.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Create an element with attributes and children in one call. */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** SVG element factory (needs the SVG namespace). */
export function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'text') { node.textContent = value; continue; }
    if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
      continue;
    }
    node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

/** Replace a node's children only when the markup actually differs. */
export function setHTML(node, html) {
  if (!node) return;
  if (node.__agrisenseHtml !== html) {
    node.innerHTML = html;
    node.__agrisenseHtml = html;
  }
}

/** Set textContent only when it changed (avoids needless layout). */
export function setText(node, value) {
  if (!node) return;
  const str = value === null || value === undefined ? '—' : String(value);
  if (node.textContent !== str) node.textContent = str;
}

/** Set an attribute only when it changed. */
export function setAttr(node, name, value) {
  if (!node) return;
  const str = String(value);
  if (node.getAttribute(name) !== str) node.setAttribute(name, str);
}

/** Toggle a class without thrashing the class list. */
export function toggleClass(node, name, on) {
  if (!node) return;
  node.classList.toggle(name, Boolean(on));
}

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const round = (v, d = 1) => {
  const f = 10 ** d;
  return Math.round((Number(v) || 0) * f) / f;
};
export const pct = (v, d = 0) => `${round((Number(v) || 0) * 100, d)}`;

/**
 * Pair a range input with a sibling <output> and keep it in sync.
 *
 * Sliders are unreadable without their number: the thumb position alone does not
 * tell you whether 0.31 is above or below the threshold you are trying to cross.
 * Returns the paint function so a caller can refresh it after a server-side
 * clamp moves the input.
 */
export function bindRange(input, output, format) {
  if (!input || !output) return () => {};
  const paint = () => setText(output, format(Number(input.value)));
  input.addEventListener('input', paint);
  paint();
  return paint;
}

// Timestamps are rendered in the *farm's* timezone, not the viewer's.  A
// control room in another timezone still needs to read the same wall clock as
// the sensor that produced the reading, so the offset comes from the server.
let tzOffsetH = 5.5;

export function setTimeOffset(hours) {
  const h = Number(hours);
  if (Number.isFinite(h) && h >= -12 && h <= 14) tzOffsetH = h;
}

function atFarmTime(epoch) {
  return new Date((epoch + tzOffsetH * 3600) * 1000);
}

/** Epoch -> farm-local "HH:MM:SS". */
export function hms(epoch) {
  if (!epoch) return '--:--:--';
  const d = atFarmTime(epoch);
  return `${String(d.getUTCHours()).padStart(2, '0')}:`
    + `${String(d.getUTCMinutes()).padStart(2, '0')}:`
    + `${String(d.getUTCSeconds()).padStart(2, '0')}`;
}

/** Epoch -> farm-local "HH:MM". */
export function hhmm(epoch) {
  if (!epoch) return '--:--';
  const d = atFarmTime(epoch);
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}

/** Interpolate a colour ramp: stops are [[t, 'rgb'], ...] sorted by t. */
export function rampColor(stops, t) {
  const v = clamp(t, 0, 1);
  for (let i = 1; i < stops.length; i += 1) {
    if (v <= stops[i][0]) {
      const [t0, c0] = stops[i - 1];
      const [t1, c1] = stops[i];
      const f = t1 === t0 ? 0 : (v - t0) / (t1 - t0);
      return mixColor(c0, c1, f);
    }
  }
  return stops[stops.length - 1][1];
}

function parseRgb(value) {
  const nums = value.match(/[\d.]+/g);
  return nums ? nums.slice(0, 3).map(Number) : [0, 0, 0];
}

export function mixColor(a, b, f) {
  const ca = parseRgb(a);
  const cb = parseRgb(b);
  const out = ca.map((v, i) => Math.round(v + (cb[i] - v) * clamp(f, 0, 1)));
  return `rgb(${out[0]},${out[1]},${out[2]})`;
}

export function cssVar(name, fallback = '#888') {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** Linear interpolation between two hex colours, used for the legend ramp. */
export function lerpHex(a, b, f) {
  const pa = parseHex(a);
  const pb = parseHex(b);
  const out = pa.map((v, i) => Math.round(v + (pb[i] - v) * clamp(f, 0, 1)));
  return `#${out.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
}

function parseHex(value) {
  const hex = value.replace('#', '');
  const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex;
  return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) || 0);
}

/** Format a litre count compactly (812 kL, 1.4 ML). */
export function litres(value) {
  const v = Number(value) || 0;
  if (Math.abs(v) >= 1e6) return `${round(v / 1e6, 2)} ML`;
  if (Math.abs(v) >= 1e3) return `${round(v / 1e3, 1)} kL`;
  return `${round(v, 0)} L`;
}

export function compact(value) {
  const v = Number(value) || 0;
  if (Math.abs(v) >= 1e6) return `${round(v / 1e6, 1)}M`;
  if (Math.abs(v) >= 1e3) return `${round(v / 1e3, 1)}k`;
  return round(v, 0).toString();
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}
