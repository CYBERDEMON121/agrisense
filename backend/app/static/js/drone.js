// Drone panel: flight state, battery/payload gauges, mission plan overlay and
// the mission history table.
//
// The mission table is fed from the bus event stream rather than the database so
// it fills in the same moment the aircraft lands, with no polling and no
// secondary source of truth.

import { $, el, setText, round, hhmm, litres, clamp } from './dom.js';
import { api } from './api.js';
import { DroneMap } from './map.js';

const STATE_TEXT = {
  idle: 'on the pad', preflight: 'pre-flight checks', takeoff: 'climbing',
  transit: 'in transit', spraying: 'spraying', rtl: 'returning to base',
  landing: 'landing', servicing: 'charging & refilling', abort: 'aborting',
};

export class DronePanel {
  constructor(live) {
    this.live = live;
    this.map = new DroneMap();
    this.state = $('#droneState');
    this.stats = $('#droneStats');
    this.hint = $('#droneHint');
    this.gaugeBattery = $('#gaugeBattery');
    this.gaugePayload = $('#gaugePayload');
    this.gaugeBatteryTxt = $('#gaugeBatteryTxt');
    this.gaugePayloadTxt = $('#gaugePayloadTxt');
    this.body = $('#missionRows');
    this.missions = new Map();
    this.lastStatsSig = '';
    this.lastSig = '';

    $('#btnMission').addEventListener('click', async (ev) => {
      const button = ev.currentTarget;
      button.disabled = true;
      try {
        // No plot list: the engine picks the worst offenders, which is the
        // decision an operator actually wants when they hit this button.
        await api.planMission({ launch: true });
      } catch (err) {
        this.flash(`cannot launch: ${err.message}`);
      } finally {
        button.disabled = false;
      }
    });
    $('#btnRth').addEventListener('click', () => this.live.send({ cmd: 'rth' }));
  }

  flash(message) {
    this.hint.dataset.flash = '1';
    setText(this.hint, message);
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      delete this.hint.dataset.flash;
      this.hint.textContent = '';
    }, 4200);
  }

  update(frame) {
    const d = frame.drone;
    if (!d) return;
    this.map.update(frame);
    this.state.className = `pill ${d.state}`;
    setText(this.state, d.state);

    const rows = [
      ['State', STATE_TEXT[d.state] || d.state],
      ['Altitude', `${round(d.z, 1)} m`],
      ['Ground speed', `${round(d.speed, 1)} m/s`],
      ['Heading', `${round(d.heading, 0)}°`],
      ['Missions done', `${d.missions_done} · ${round(d.area_sprayed_ha, 2)} ha`],
      ['Flight time', `${round(d.flight_minutes, 1)} min`],
    ];
    const m = d.mission;
    if (m) {
      rows.push(['Mission', m.ref]);
      rows.push(['Pass lines', `${m.waypoints} wps`]);
      rows.push(['Target', `${m.n_plots} plots · ${round(m.area_ha, 2)} ha`]);
      rows.push(['Product', m.product]);
      rows.push(['Label rate', `${round(m.dose_l_ha * 1000, 0)} mL/ha`]);
      rows.push(['Sortie', `${m.sorties + 1}`]);
    }
    const sig = rows.map((r) => r[1]).join('|');
    if (sig !== this.lastStatsSig) {
      this.lastStatsSig = sig;
      this.stats.replaceChildren(...rows.flatMap(([k, v]) => [
        el('dt', { text: k }), el('dd', { text: v }),
      ]));
    }

    this.gaugeBattery.style.width = `${clamp(d.battery, 0, 100)}%`;
    this.gaugeBattery.style.background = d.battery < 22 ? 'var(--danger)'
      : d.battery < 45 ? 'var(--warn)' : 'var(--accent)';
    setText(this.gaugeBatteryTxt, `${round(d.battery, 0)}%`);
    this.gaugePayload.style.width = `${clamp(d.payload_pct, 0, 100)}%`;
    this.gaugePayload.style.background = d.payload_l < 2 ? 'var(--danger)' : 'var(--drone)';
    setText(this.gaugePayloadTxt, `${round(d.payload_l, 1)} L`);

    this.#hint(d, frame);
    this.#missions(d, frame);
  }

  /** Say what the drone is doing and, more usefully, why it is not doing more. */
  #hint(d, frame) {
    if (this.hint.dataset.flash) return;
    const auto = frame.automation || {};
    const w = frame.weather || {};
    const m = d.mission;

    // A mission that is still open outranks the drone's state: between sorties
    // the drone sits on the pad refilling, and "parked" would hide the only
    // interesting numbers on the panel.
    if (m && m.plots.length && m.status !== 'done' && m.status !== 'aborted') {
      const done = Object.values(m.coverage || {});
      const mean = done.length ? done.reduce((a, b) => a + b, 0) / done.length : 0;
      const dose = Object.values(m.dose_ml_ha || {});
      const avgDose = dose.length ? dose.reduce((a, b) => a + b, 0) / dose.length : 0;
      setText(this.hint, `${STATE_TEXT[d.state] || d.state} — ${m.ref} · `
        + `${m.sorties + 1} sortie(s) · `
        + `coverage ${round(mean * 100, 0)}% · ${round(avgDose, 0)} mL/ha flown `
        + `against a ${round(m.dose_l_ha * 1000, 0)} mL/ha label rate`);
      return;
    }
    if (d.state === 'servicing') {
      setText(this.hint, `on the pad: charging and refilling · auto `
        + `${auto.auto_spray ? 'on' : 'off'}`);
      return;
    }
    if (d.state === 'idle') {
      const reasons = [];
      if (!auto.auto_spray) reasons.push('automatic spraying disabled');
      if ((w.wind || 0) > (auto.thresholds?.wind_spray_max_ms ?? 6)) {
        reasons.push(`wind ${round(w.wind, 1)} m/s over limit`);
      }
      if ((w.rain || 0) > 0.1) reasons.push('raining');
      if ((auto.spray_hold_s || 0) > 0) {
        reasons.push(`rain hold ${Math.round(auto.spray_hold_s / 60)} min`);
      }
      setText(this.hint, reasons.length
        ? `parked — ${reasons.join(' · ')}`
        : `parked — pest pressure below the ${Math.round((auto.thresholds?.pest_spray_threshold ?? 0.55) * 100)}% threshold`);
      return;
    }
    if (d.last_error) {
      setText(this.hint, `${STATE_TEXT[d.state] || d.state} — ${d.last_error}`);
      return;
    }
    setText(this.hint, STATE_TEXT[d.state] || d.state);
  }

  /**
   * Fold mission lifecycle events from the bus into a table.  Events arrive
   * newest-first in ``recent_events``, so the first sighting of a ref wins and
   * later events update it.
   */
  #missions(d, frame) {
    const events = frame.recent_events || [];
    for (const ev of events) {
      if (!String(ev.topic || '').includes('/drone/')) continue;
      if (!String(ev.topic || '').endsWith('/mission')) continue;
      const ref = ev.ref;
      if (!ref) continue;
      const row = this.missions.get(ref) || {
        ref, trigger: ev.trigger || '—', plots: ev.plots ?? 0, area_ha: 0,
        litres: 0, sorties: 0, status: 'planned', coverage: 0, ts: ev.ts,
      };
      row.ts = ev.ts || row.ts;
      row.status = ev.status || ev.event || row.status;
      if (ev.area_ha !== undefined) row.area_ha = ev.area_ha;
      if (ev.plots !== undefined) row.plots = ev.plots;
      if (ev.trigger) row.trigger = ev.trigger;
      if (ev.sprayed_litres !== undefined) row.litres = ev.sprayed_litres;
      if (ev.sortie !== undefined) row.sorties = Math.max(row.sorties, ev.sortie || 0);
      if (ev.mean_coverage !== undefined) row.coverage = ev.mean_coverage;
      this.missions.set(ref, row);
    }
    // The in-flight mission is authoritative for its own numbers.
    const m = d.mission;
    if (m) {
      const cov = Object.values(m.coverage || {});
      this.missions.set(m.ref, {
        ref: m.ref, trigger: m.trigger, plots: m.n_plots, area_ha: m.area_ha,
        litres: m.sprayed_litres, sorties: m.sorties, status: m.status,
        coverage: cov.length ? cov.reduce((x, y) => x + y, 0) / cov.length : 0,
        ts: m.created_ts,
      });
    }

    const list = [...this.missions.values()].sort((a, b) => b.ts - a.ts).slice(0, 8);
    const sig = list.map((r) => `${r.ref}:${r.status}:${r.sorties}:${round(r.coverage, 2)}`).join('|');
    if (sig === this.lastSig) return;
    this.lastSig = sig;
    if (!list.length) {
      this.body.replaceChildren(el('tr', { class: 'empty' }, [
        el('td', { colspan: 8, text: 'No missions yet — the rules will launch one when pest pressure crosses the threshold' }),
      ]));
      return;
    }
    this.body.replaceChildren(...list.map((r) => el('tr', {}, [
      el('td', { class: 'mono', text: r.ref }),
      el('td', { class: 'mono', text: r.trigger }),
      el('td', { class: 'num', text: r.plots }),
      el('td', { class: 'num', text: `${round(r.area_ha, 2)} ha` }),
      el('td', { class: 'num', text: litres(r.litres) }),
      el('td', { class: 'num', text: `${round(r.coverage * 100, 0)}%` }),
      el('td', { class: 'num', text: r.sorties + 1 }),
      el('td', {}, [el('span', { class: `pill ${r.status}`, text: `${r.status} ${hhmm(r.ts)}` })]),
    ])));
  }
}