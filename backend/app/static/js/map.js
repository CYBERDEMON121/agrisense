// SVG field map: 38 plots, irrigation zones, fixed cameras, drone, landing pad.
//
// The map is the primary instrument, so it carries a lot of information at
// once; to keep it readable, layers are mutually exclusive (one colour scale)
// and everything else is neutral chrome.

import { $, $$, svg, el, setText, setAttr, clamp, round, lerpHex } from './dom.js';
import { api } from './api.js';

const NS = 'http://www.w3.org/2000/svg';

/** Colour scales per layer, as [stop, hex] pairs. */
const SCALES = {
  moisture: {
    label: 'Soil moisture', unit: '%', lo: 'dry 20%', hi: 'wet 100%',
    stops: [[0, '#7c2d12'], [0.25, '#b45309'], [0.5, '#eab308'], [0.75, '#38bdf8'], [1, '#0369a1']],
    value: (p) => p.moisture / 100,
  },
  pest: {
    label: 'Pest pressure', unit: '%', lo: '0%', hi: '100%',
    stops: [[0, '#14532d'], [0.35, '#4ade80'], [0.55, '#facc15'], [0.75, '#f97316'], [1, '#b91c1c']],
    value: (p) => p.pest,
  },
  health: {
    label: 'Crop health', unit: '%', lo: '0%', hi: '100%',
    stops: [[0, '#7f1d1d'], [0.4, '#f87171'], [0.7, '#fde047'], [1, '#22c55e']],
    value: (p) => p.health,
  },
  disease: {
    label: 'Disease', unit: '%', lo: '0%', hi: '80%',
    stops: [[0, '#1e1b4b'], [0.3, '#6366f1'], [0.6, '#c084fc'], [1, '#f472b6']],
    value: (p) => p.disease / 0.8,
  },
  crop: {
    label: 'Crop type', unit: '',
    stops: [[0, '#4a7c3a'], [0.5, '#3f8f4a'], [1, '#7fa03c']],
    value: null,
  },
};

const CROP_COLORS = {
  cotton: '#5c9c4a', tomato: '#3f8f4a', wheat: '#9cbf4a', mango: '#2f6b34',
};

const ZONE_COLORS = { z1: '#38bdf8', z2: '#a78bfa', z3: '#fbbf24' };

function scaleColor(stops, t) {
  const v = clamp(t, 0, 1);
  for (let i = 1; i < stops.length; i += 1) {
    if (v <= stops[i][0]) {
      const [t0, c0] = stops[i - 1];
      const [t1, c1] = stops[i];
      return lerpHex(c0, c1, t1 === t0 ? 0 : (v - t0) / (t1 - t0));
    }
  }
  return stops[stops.length - 1][1];
}

export class FieldMap {
  constructor() {
    this.svg = $('#fieldMap');
    this.legend = $('#legend');
    this.tip = $('#mapTip');
    this.layer = 'moisture';
    this.plots = new Map();
    this.cameras = [];
    this.bounds = { x: 0, y: 0, w: 580, h: 430 };
    this.areas = [];

    this.buildSwitch();
    this.buildLegend();
  }

  buildSwitch() {
    for (const button of $$('.layer-switch button')) {
      button.addEventListener('click', () => {
        this.layer = button.dataset.layer;
        for (const other of $$('.layer-switch button')) {
          other.classList.toggle('on', other === button);
        }
        this.buildLegend();
        if (this.lastFrame) this.renderPlots(this.lastFrame);
      });
    }
  }

  buildLegend() {
    const scale = SCALES[this.layer];
    if (!scale.value) {
      this.legend.replaceChildren(
        el('span', { text: 'Crop' }),
        ...Object.entries(CROP_COLORS).map(([key, color]) => el('span', {
          style: { display: 'inline-flex', alignItems: 'center', gap: '3px' },
        }, [
          el('span', { style: { width: '9px', height: '9px', borderRadius: '2px', background: color } }),
          el('span', { text: key }),
        ])),
      );
      return;
    }
    const ramp = el('span', {
      class: 'ramp',
      style: { background: `linear-gradient(90deg, ${scale.stops.map(([t, c]) => `${c} ${t * 100}%`).join(',')})` },
    });
    this.legend.replaceChildren(
      el('span', { text: `${scale.label} ${scale.lo} → ${scale.hi}` }), ramp,
    );
  }

  /** (Re)build static geometry: zones, plots, cameras, pad. */
  build(frame) {
    this.lastFrame = frame;
    this.bounds = frame.bounds || this.bounds;
    const b = this.bounds;
    // SVG y grows downward; the farm frame has y growing north, so flip it.
    this.flipY = (y) => b.y + b.h - y;
    this.svg.replaceChildren();

    const defs = svg('defs', {}, [
      svg('pattern', { id: 'hatch', width: 6, height: 6, patternTransform: 'rotate(45)', patternUnits: 'userSpaceOnUse' }, [
        svg('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: 'currentColor', 'stroke-width': 2, opacity: 0.35 }),
      ]),
    ]);
    this.svg.append(defs);

    this.groupPlots = svg('g', {});
    this.groupZones = svg('g', {});
    this.groupCams = svg('g', {});
    this.groupDrone = svg('g', {});
    this.groupLabels = svg('g', {});
    this.svg.append(this.groupZones, this.groupPlots, this.groupCams, this.groupDrone, this.groupLabels);

    // ---- irrigation zone outlines + labels ----
    // Drawn from the server's per-zone boxes rather than per area: zone 1 is
    // valved as a single hydraulic unit spanning two areas, and one box per
    // zone is what actually tells an operator which plots share a valve.
    const boxes = frame.fieldMap?.area_boxes?.length
      ? frame.fieldMap.area_boxes
      : (frame.areas || []).map((a) => ({ id: a.zone, label: `${a.name} · ${a.zone}`, x: a.x, y: a.y, w: a.w, h: a.h }));
    for (const box of boxes) {
      const color = ZONE_COLORS[box.id] || '#888';
      this.groupZones.append(svg('rect', {
        class: 'zone-outline',
        x: box.x - 3, y: this.flipY(box.y + box.h) - 3,
        width: box.w + 6, height: box.h + 6,
        rx: 4, stroke: color,
      }));
      this.groupZones.append(svg('text', {
        class: 'zone-tag',
        x: box.x + 1, y: this.flipY(box.y + box.h) + 10,
      }, [document.createTextNode(`${box.label || box.id} · ${box.plots ?? '?'} plots`)]));
    }

    // ---- plots ----
    this.plots.clear();
    for (const plot of frame.plots || []) {
      const rect = svg('rect', {
        class: 'plot', 'data-id': plot.id,
        x: plot.x, y: this.flipY(plot.y + plot.h),
        width: plot.w, height: plot.h, rx: 1.5,
      });
      const title = svg('title');
      rect.append(title);
      rect.addEventListener('mousemove', (ev) => this.showTip(ev, plot.id));
      rect.addEventListener('mouseleave', () => this.hideTip());
      rect.addEventListener('click', () => this.selectPlot(plot.id));
      this.groupPlots.append(rect);
      this.plots.set(plot.id, { rect, title, plot });
    }

    // ---- landing pad ----
    const pad = frame.drone?.pad || [270, 430];
    this.groupLabels.append(svg('circle', {
      class: 'pad-mark', cx: pad[0], cy: this.flipY(pad[1]), r: 9,
    }));
    this.groupLabels.append(svg('text', {
      class: 'zone-tag', x: pad[0] + 12, y: this.flipY(pad[1]) + 3,
    }, [document.createTextNode('pad')]));

    // ---- north arrow + scale bar ----
    this.groupLabels.append(svg('text', {
      class: 'zone-tag', x: b.x + 6, y: 22, 'font-size': 11,
    }, [document.createTextNode('N ↑')]));
    const scaleLen = 100;
    this.groupLabels.append(svg('line', {
      x1: b.x + 6, y1: b.y + b.h - 12, x2: b.x + 6 + scaleLen, y2: b.y + b.h - 12,
      stroke: 'currentColor', 'stroke-width': 1.5, opacity: 0.6,
    }));
    this.groupLabels.append(svg('text', {
      class: 'zone-tag', x: b.x + 6, y: b.y + b.h - 18,
    }, [document.createTextNode('100 m')]));

    this.cameras = frame.cameras || [];
    this.renderCams(frame);
    this.renderPlots(frame);
    this.renderDrone(frame);
  }

  renderPlots(frame) {
    const scale = SCALES[this.layer];
    for (const [id, entry] of this.plots) {
      const plot = frame.plots.find((p) => p.id === id);
      if (!plot) continue;
      entry.plot = plot;
      let fill;
      if (!scale.value) fill = CROP_COLORS[plot.crop] || '#4a7c3a';
      else fill = scaleColor(scale.stops, scale.value(plot));
      entry.rect.setAttribute('fill', fill);
      // Outline any plot the automation currently wants to act on.
      const hot = plot.pest > 0.55 || plot.moisture < 28 || plot.health < 0.35;
      entry.rect.setAttribute('stroke', hot ? '#fff' : 'rgba(0,0,0,.35)');
      entry.rect.setAttribute('stroke-width', hot ? 1.4 : 0.5);
      entry.title.textContent = this.tipText(plot);
    }
  }

  tipText(plot) {
    return [
      `${plot.name} · ${plot.crop_label}`,
      `moisture ${plot.moisture}%  pest ${Math.round(plot.pest * 100)}%`,
      `health ${Math.round(plot.health * 100)}%  canopy ${Math.round(plot.canopy * 100)}%`,
      `${plot.stage_name} · ${plot.spray_count} sprays`,
    ].join('\n');
  }

  renderCams(frame) {
    this.groupCams.replaceChildren();
    const byId = new Map((frame.cameras || []).map((c) => [c.id, c]));
    for (const camId of ['cam-north', 'cam-south', 'cam-east', 'cam-orchard', 'cam-pump', 'cam-gate']) {
      const cam = byId.get(camId);
      if (!cam) continue;
      const cx = cam.x;
      const cy = this.flipY(cam.y);
      const rad = ((cam.fov || 70) * Math.PI) / 180;
      // View direction convention: (sin yaw, cos yaw), y flipped for SVG.
      const yaw = (cam.yaw * Math.PI) / 180;
      const dirX = Math.sin(yaw);
      const dirY = -Math.cos(yaw);
      const range = 150;
      const x1 = cx + (dirX * Math.cos(rad) - dirY * Math.sin(rad)) * range;
      const y1 = cy + (dirX * Math.sin(rad) + dirY * Math.cos(rad)) * range;
      const x2 = cx + (dirX * Math.cos(rad) + dirY * Math.sin(rad)) * range;
      const y2 = cy + (-dirX * Math.sin(rad) + dirY * Math.cos(rad)) * range;
      const alerting = (cam.detections || 0) > 1 && (cam.severity || 0) > 0.2;
      const group = svg('g', {
        class: `cam-mark${alerting ? ' alerting' : ''}`,
        onclick: () => this.focusCamera(cam.id),
      }, [
        svg('title', {}, [document.createTextNode(`${cam.name} — click to open the feed`)]),
        svg('path', { class: 'cam-fov', d: `M ${cx} ${cy} L ${x1} ${y1} A ${range} ${range} 0 0 0 ${x2} ${y2} Z` }),
        svg('rect', { class: 'cam-body', x: cx - 3.5, y: cy - 2.5, width: 7, height: 5, rx: 1, transform: `rotate(${cam.yaw} ${cx} ${cy})` }),
      ]);
      this.groupCams.append(group);
    }
  }

  renderDrone(frame) {
    const d = frame.drone;
    if (!d) return;
    this.groupDrone.replaceChildren();
    const cx = d.x;
    const cy = this.flipY(d.y);
    const airborne = !['idle', 'servicing', 'landing'].includes(d.state);

    // Flight trail.
    if (d.trail?.length > 1) {
      const pts = d.trail.map(([x, y]) => `${round(x, 1)},${round(this.flipY(y), 1)}`).join(' ');
      this.groupDrone.append(svg('polyline', { class: 'drone-trail', points: pts }));
    }
    if (airborne) {
      // Downward camera cone from the nose.
      const yaw = (d.heading * Math.PI) / 180;
      const dirX = Math.sin(yaw);
      const dirY = -Math.cos(yaw);
      const range = 46;
      const half = 0.62;
      const a1 = yaw - Math.PI / 2 + half;
      const a2 = yaw - Math.PI / 2 - half;
      const p1 = `${cx + Math.cos(a1) * range * -dirY + Math.sin(a1) * range * dirX},${cy + Math.cos(a1) * range * -dirX + Math.sin(a1) * range * -dirY}`;
      const p2 = `${cx + Math.cos(a2) * range * -dirY + Math.sin(a2) * range * dirX},${cy + Math.cos(a2) * range * -dirX + Math.sin(a2) * range * -dirY}`;
      this.groupDrone.append(svg('polygon', {
        class: 'drone-fov', points: `${cx},${cy} ${p1} ${p2}`,
      }));
      this.groupDrone.append(svg('circle', { class: 'drone-halo', cx, cy, r: 11 }));
    }
    this.groupDrone.append(svg('g', {
      transform: `translate(${cx} ${cy}) rotate(${d.heading})`,
    }, [
      svg('path', {
        class: 'drone-air',
        d: 'M 0 -6 L 5 5 L 0 2.5 L -5 5 Z',
      }),
    ]));
    this.groupDrone.append(svg('text', {
      class: 'zone-tag', x: cx + 9, y: cy - 7,
    }, [document.createTextNode(d.state)]));
  }

  update(frame) {
    // Geometry is rebuilt whenever the plot or zone set changes, not just on
    // the first frame: the dashboard can receive a frame before the fleet has
    // registered, and a one-shot build would leave the map permanently empty.
    const plots = frame.plots || [];
    const key = [
      frame.bounds ? `${frame.bounds.x},${frame.bounds.y},${frame.bounds.w},${frame.bounds.h}` : '',
      (frame.areas || []).map((a) => a.id).join(),
      (frame.fieldMap?.area_boxes || []).length,
      plots.length,
      plots.length ? `${plots[0].id}..${plots[plots.length - 1].id}` : '',
    ].join('|');
    if (!this.groupPlots || key !== this.buildKey) {
      this.build(frame);
      this.buildKey = key;
    }
    this.lastFrame = frame;
    this.renderPlots(frame);
    this.renderCams(frame);
    this.renderDrone(frame);
  }

  selectPlot(id) {
    for (const [pid, entry] of this.plots) entry.rect.classList.toggle('sel', pid === id);
    document.dispatchEvent(new CustomEvent('agrisense:plot', { detail: id }));
  }

  showTip(event, id) {
    const entry = this.plots.get(id);
    if (!entry) return;
    const plot = entry.plot;
    const wrap = this.svg.parentElement.getBoundingClientRect();
    this.tip.hidden = false;
    this.tip.style.left = `${event.clientX - wrap.left}px`;
    this.tip.style.top = `${event.clientY - wrap.top}px`;
    this.tip.replaceChildren(
      el('h4', { text: `${plot.name}` }),
      el('div', { text: `${plot.crop_label} · ${plot.stage_name} · ${plot.area_ha} ha` }),
      el('dl', {}, [
        el('dt', { text: 'Moisture' }), el('dd', { text: `${plot.moisture}%` }),
        el('dt', { text: 'Soil temp' }), el('dd', { text: `${plot.temp} °C` }),
        el('dt', { text: 'EC' }), el('dd', { text: `${plot.ec} dS/m` }),
        el('dt', { text: 'Health' }), el('dd', { text: `${Math.round(plot.health * 100)}%` }),
        el('dt', { text: 'Pest' }), el('dd', { text: `${Math.round(plot.pest * 100)}%` }),
        el('dt', { text: 'Disease' }), el('dd', { text: `${Math.round(plot.disease * 100)}%` }),
        el('dt', { text: 'Sensor' }), el('dd', { text: plot.sensor }),
      ]),
    );
  }

  hideTip() {
    this.tip.hidden = true;
  }

  async focusCamera(id) {
    document.dispatchEvent(new CustomEvent('agrisense:camera', { detail: id }));
  }
}

// --------------------------------------------------------------------------
// Standalone drone map (focused view with the mission plan overlaid)
// --------------------------------------------------------------------------
export class DroneMap {
  constructor() {
    this.svg = $('#droneMap');
    this.bounds = { x: 0, y: 0, w: 580, h: 430 };
  }

  update(frame) {
    const b = frame.bounds || this.bounds;
    const flipY = (y) => b.y + b.h - y;
    const map = frame.fieldMap;
    const children = [];

    // Field outline for context.
    for (const area of frame.areas || []) {
      children.push(svg('rect', {
        x: area.x, y: flipY(area.y + area.h), width: area.w, height: area.h,
        rx: 3, fill: 'none', stroke: area.color || '#3d4b3f', 'stroke-width': 1.2,
        opacity: 0.75,
      }));
      children.push(svg('text', {
        class: 'zone-tag', x: area.x + 4, y: flipY(area.y + area.h) + 12,
      }, [document.createTextNode(area.name)]));
    }

    // Planned coverage: pass lines already flown solid, remaining dashed.
    const mission = frame.drone.mission;
    if (mission?.waypoint_paths?.length) {
      for (const seg of mission.waypoint_paths) {
        const flown = seg.idx <= (mission.wp_idx || 0);
        children.push(svg('line', {
          class: 'drone-line',
          x1: seg.x1, y1: flipY(seg.y1), x2: seg.x2, y2: flipY(seg.y2),
          stroke: flown ? 'var(--sun)' : 'var(--text-faint)',
          opacity: flown ? 0.95 : 0.4,
        }));
      }
    }

    const d = frame.drone;
    if (d?.trail?.length > 1) {
      children.push(svg('polyline', {
        class: 'drone-trail',
        points: d.trail.map(([x, y]) => `${round(x, 1)},${round(flipY(y), 1)}`).join(' '),
      }));
    }
    const airborne = d && !['idle', 'servicing', 'landing'].includes(d.state);
    if (d) {
      if (airborne) {
        const yaw = (d.heading * Math.PI) / 180;
        const dirX = Math.sin(yaw);
        const dirY = -Math.cos(yaw);
        const range = 48;
        const p = (angle) => [
          d.x + Math.cos(angle) * range * -dirY + Math.sin(angle) * range * dirX,
          flipY(d.y) - Math.cos(angle) * range * dirX + Math.sin(angle) * range * -dirY,
        ];
        const [x1, y1] = p(yaw - Math.PI / 2 + 0.6);
        const [x2, y2] = p(yaw - Math.PI / 2 - 0.6);
        children.push(svg('polygon', {
          class: 'drone-fov', points: `${d.x},${flipY(d.y)} ${round(x1, 1)},${round(y1, 1)} ${round(x2, 1)},${round(y2, 1)}`,
        }));
        children.push(svg('circle', { class: 'drone-halo', cx: d.x, cy: flipY(d.y), r: 12 }));
      }
      children.push(svg('g', { transform: `translate(${d.x} ${flipY(d.y)}) rotate(${d.heading})` }, [
        svg('path', { class: 'drone-air', d: 'M 0 -8 L 6.5 6.5 L 0 3.2 L -6.5 6.5 Z' }),
      ]));
      children.push(svg('text', {
        class: 'zone-tag', x: d.x + 11, y: flipY(d.y) - 8,
      }, [document.createTextNode(`${d.state} · ${Math.round(d.alt_m || d.z || 0)} m`)]));
    }
    const pad = d?.pad || [270, 430];
    children.push(svg('circle', { class: 'pad-mark', cx: pad[0], cy: flipY(pad[1]), r: 10 }));

    this.svg.replaceChildren(...children);
  }
}
