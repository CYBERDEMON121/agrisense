// Camera wall: one MJPEG pane per device plus what the detector currently sees.
//
// Streams are only attached when a pane scrolls into view.  Seven cameras at
// 2 fps is 14 JPEG decodes a second; opening them all the moment the page loads
// is the fastest way to make the whole dashboard feel slow.

import { $, el, setText, setAttr, round, toggleClass } from './dom.js';
import { api } from './api.js';

/** Detection severity -> chip class. Labels ending in _summary are field-wide. */
function chipClass(label, severity) {
  if (label === 'ndvi_proxy') return severity > 0.02 ? 'ok' : '';
  if (label.endsWith('_summary')) return severity > 0.35 ? 'bad' : 'warn';
  if (severity > 0.35) return 'bad';
  if (severity > 0.12) return 'warn';
  return 'ok';
}

function prettyLabel(label) {
  return String(label).replace(/_summary$/, '').replace(/_/g, ' ');
}

export class CameraWall {
  constructor() {
    this.root = $('#camGrid');
    this.tag = $('#camTag');
    this.panes = new Map();
    this.visible = new Set();
    this.maxStreams = 4;
    this.streamed = new Set();
    this.observer = new IntersectionObserver(
      (entries) => this.#onIntersect(entries),
      { rootMargin: '160px' },
    );
    document.addEventListener('agrisense:camera', (ev) => this.focus(ev.detail));
  }

  #onIntersect(entries) {
    for (const entry of entries) {
      const id = entry.target.dataset.cam;
      if (!id) continue;
      if (entry.isIntersecting) this.visible.add(id);
      else this.visible.delete(id);
    }
    this.#rebalance();
  }

  /**
   * Keep at most `maxStreams` live feeds.  Pane order is the camera's own
   * priority: fields first, the drone feed last, because it only exists while
   * airborne.
   */
  #rebalance() {
    const wanted = Array.from(this.panes.values())
      .filter((p) => this.visible.has(p.id))
      .sort((a, b) => a.rank - b.rank);
    for (let i = 0; i < wanted.length; i += 1) {
      const pane = wanted[i];
      const should = i < this.maxStreams;
      if (should && !this.streamed.has(pane.id)) this.#attach(pane);
      else if (!should && this.streamed.has(pane.id)) this.#detach(pane);
    }
  }

  #attach(pane) {
    if (this.streamed.has(pane.id)) return;
    this.streamed.add(pane.id);
    // Cache-buster in the URL: the src changes when the user reopens a pane and
    // some browsers will otherwise show the last frame of the old request.
    pane.img.src = api.streamUrl(pane.id);
    toggleClass(pane.node, 'live', true);
  }

  #detach(pane) {
    if (!this.streamed.has(pane.id)) return;
    this.streamed.delete(pane.id);
    // An <img> feeding from a multipart stream is only really stopped by
    // dropping the src, which is exactly what this does.
    pane.img.removeAttribute('src');
    pane.placeholder.hidden = false;
    toggleClass(pane.node, 'live', false);
  }

  focus(id) {
    const pane = this.panes.get(id);
    if (!pane) return;
    pane.node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    pane.node.animate?.(
      [{ outline: '0px solid transparent' }, { outline: `2px solid var(--accent)` }, { outline: '0px solid transparent' }],
      { duration: 1400, easing: 'ease-out' },
    );
    // Make sure the requested camera is the one that gets the feed.
    this.visible.add(id);
    this.visible.delete([...this.streamed].find((k) => k !== id) || '');
    this.#rebalance();
  }

  update(frame) {
    const cams = frame.cameras || [];
    let alerting = 0;
    for (const cam of cams) {
      const pane = this.#ensure(cam);
      if (cam.severity > 0.12) alerting += 1;
      setText(pane.name, cam.name);
      setText(pane.meta, `${round(cam.fps, 1)} fps · ${compact(cam.frames)} frames`);
      setAttr(pane.node, 'data-kind', cam.kind);
      toggleClass(pane.node, 'alert', cam.severity > 0.2);
      this.#chips(pane, cam);
    }
    // A pane whose camera went away (the drone cam lands) closes its stream.
    const live = new Set(cams.map((c) => c.id));
    for (const [id, pane] of this.panes) {
      if (live.has(id)) continue;
      this.#detach(pane);
      pane.node.remove();
      this.panes.delete(id);
      this.visible.delete(id);
    }
    setText(this.tag, `${cams.length} online${alerting ? ` · ${alerting} alerting` : ''}`);
  }

  #ensure(cam) {
    let pane = this.panes.get(cam.id);
    if (!pane) {
      const name = el('span', { class: 'name' });
      const meta = el('span', { class: 'meta' });
      const img = el('img', { alt: `${cam.name} live feed`, loading: 'lazy' });
      const placeholder = el('div', {
        class: 'cam-placeholder',
        style: {
          position: 'absolute', inset: '0', display: 'grid', placeItems: 'center',
          color: 'var(--text-faint)', fontSize: '11px', fontFamily: 'var(--mono)',
        },
      }, ['stream idle']);
      const chips = el('div', { class: 'cam-dets' });
      const node = el('div', { class: 'cam', 'data-cam': cam.id }, [
        el('div', { class: 'cam-head' }, [name, meta]),
        el('div', { class: 'cam-body-wrap' }, [img, placeholder]),
        chips,
      ]);
      this.root.append(node);
      // Field views first, extras and the nose cam last.
      const rank = cam.kind === 'drone' ? 90
        : ['north', 'south', 'east', 'orchard'].includes(cam.area) ? 10 : 50;
      pane = { id: cam.id, node, name, meta, img, placeholder, chips, rank, sig: '' };
      this.panes.set(cam.id, pane);
      this.observer.observe(node);
    }
    return pane;
  }

  /** Detection chips, rebuilt only when the set of labels actually changes. */
  #chips(pane, cam) {
    const dets = cam.dets || [];
    const sig = dets.map((d) => `${d.label}:${d.severity}`).join('|');
    if (sig === pane.sig) return;
    pane.sig = sig;
    if (!dets.length) {
      pane.chips.replaceChildren(el('span', { class: 'det', text: 'no detections' }));
      return;
    }
    pane.chips.replaceChildren(...dets.map((d) => el('span', {
      class: `det ${chipClass(d.label, d.severity)}`,
      title: `${prettyLabel(d.label)} · confidence ${round(d.confidence * 100, 0)}%`,
      text: d.label.endsWith('_summary') || d.label === 'ndvi_proxy'
        ? `${prettyLabel(d.label)} ${round(d.severity * 100, 0)}%`
        : prettyLabel(d.label),
    })));
  }
}

function compact(value) {
  const v = Number(value) || 0;
  if (v >= 1e6) return `${round(v / 1e6, 1)}M`;
  if (v >= 1e3) return `${round(v / 1e3, 1)}k`;
  return round(v, 0).toString();
}