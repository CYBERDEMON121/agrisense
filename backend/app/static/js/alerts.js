// Alerts panel.
//
// Alerts are de-duplicated server-side by (kind, target), so this list stays
// short enough to read: one line per *condition*, refreshed in place while it
// persists, rather than one line per tick that triggered it.

import { $, el, setText, hhmm } from './dom.js';
import { api } from './api.js';

export class AlertsPanel {
  constructor() {
    this.root = $('#alerts');
    this.ackAll = $('#btnAckAll');
    this.items = new Map();
    this.empty = true;
    this.ackAll.addEventListener('click', async () => {
      this.ackAll.disabled = true;
      try {
        await api.ackAll();
        this.items.clear();
        this.root.replaceChildren(el('li', { class: 'empty', text: 'all acknowledged' }));
        this.empty = true;
      } catch (err) {
        console.warn('ack all failed', err);
      } finally {
        this.ackAll.disabled = false;
      }
    });
  }

  update(frame) {
    const alerts = frame.alerts || [];
    if (this.empty && alerts.length) {
      this.root.replaceChildren();
      this.items.clear();
      this.empty = false;
    }
    if (!alerts.length && this.empty) return;

    const seen = new Set();
    for (const alert of alerts) {
      seen.add(alert.id);
      let item = this.items.get(alert.id);
      if (!item) {
        const sev = el('span', { class: 'sev' });
        const title = el('b');
        const meta = el('span');
        const button = el('button', {
          text: 'Ack',
          onclick: () => api.ackAlert(alert.id).catch((err) => console.warn(err)),
        });
        const node = el('li', {}, [sev, el('div', { class: 'msg' }, [title, meta]), button]);
        this.root.append(node);
        item = { node, sev, title, meta, sig: '' };
        this.items.set(alert.id, item);
      }
      // Only touch the DOM when the text changed: alerts update every tick.
      const sig = `${alert.level}|${alert.kind}`;
      if (sig !== item.sig) {
        item.sig = sig;
        item.node.dataset.level = alert.level;
        setText(item.title, alert.message);
      }
      setText(item.meta, `${hhmm(alert.ts)} · ${alert.kind} · ${alert.device || alert.plot || 'system'}`);
    }

    for (const [id, item] of this.items) {
      if (seen.has(id)) continue;
      item.node.remove();
      this.items.delete(id);
    }
    if (!this.items.size) {
      this.root.replaceChildren(el('li', { class: 'empty', text: 'no open alerts' }));
      this.empty = true;
    }
    setText(this.ackAll, `Acknowledge all${this.items.size ? ` (${this.items.size})` : ''}`);
  }
}