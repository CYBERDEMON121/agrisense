// Automation decision log.
//
// This is the panel that answers "why did the farm do that?".  Holds and waits
// are shown next to the actions, because a rule that silently declines to act
// is indistinguishable from a broken rule.

import { $, el, setText, hhmm } from './dom.js';
import { api } from './api.js';

const ACTION_TEXT = {
  act: 'acted', hold: 'held', wait: 'waited', skip: 'skipped',
};

/**
 * @param {{toggles?: boolean, tag?: boolean}} opts
 *   `toggles` binds the three automation switches - only the simulator console
 *   has them, since the dashboard is read-only.  `tag` adds an interlock badge,
 *   which the console already shows on its threshold panel.
 */
export class DecisionLog {
  constructor({ toggles = true, tag = true } = {}) {
    this.root = $('#decisions');
    this.tag = null;
    // The interlock state has no dedicated node in the markup, and it is the one
    // thing an operator most needs at a glance, so the panel grows its own tag.
    if (tag) {
      this.tag = el('span', { class: 'tag', id: 'decisionTag' });
      this.root?.closest('.panel')?.querySelector('.panel-head')?.append(this.tag);
    }
    this.toggles = toggles
      ? { enabled: $('#tgAuto'), auto_spray: $('#tgSpray'), auto_irrigate: $('#tgIrr') }
      : {};
    this.empty = true;
    this.lastSig = '';
    this.pending = false;

    for (const [key, input] of Object.entries(this.toggles)) {
      if (!input) continue;
      input.addEventListener('change', async () => {
        this.pending = true;
        try {
          // The flags go over REST, not the socket: they change how the engine
          // decides, so they deserve a real request/response.
          await api.setAutomation({ [key]: input.checked });
        } catch (err) {
          console.warn('automation toggle failed', err);
          input.checked = !input.checked;
        } finally {
          this.pending = false;
        }
      });
    }
  }

  update(frame) {
    const auto = frame.automation || {};
    if (!this.pending) {
      for (const [key, input] of Object.entries(this.toggles)) {
        if (input && input.checked !== auto[key]) input.checked = Boolean(auto[key]);
      }
    }

    const decisions = frame.decisions || [];
    if (!decisions.length && this.empty) return;
    if (this.empty && decisions.length) {
      this.root.replaceChildren();
      this.empty = false;
    }
    // Rebuilding only when the visible content changed: the log is the one panel
    // where churn would be genuinely distracting.
    const sig = decisions.slice(0, 24).map((d) => `${d.ts}|${d.rule}|${d.action}|${d.reason}`).join('\n');
    if (sig === this.lastSig) return;
    this.lastSig = sig;

    this.root.replaceChildren(...decisions.slice(0, 40).map((d) => el('li', {
      'data-action': d.action,
      title: `${d.rule} → ${d.target}`,
    }, [
      el('span', { class: 't', text: hhmm(d.ts) }),
      el('span', { class: 'a', text: ACTION_TEXT[d.action] || d.action }),
      el('span', { class: 'why', text: `${d.rule}: ${d.reason}` }),
    ])));

    const hold = auto.spray_hold_s || 0;
    if (this.tag) {
      setText(this.tag, hold > 0 ? `rain hold · ${Math.round(hold / 60)} min left` : `${auto.actions_taken || 0} actions`);
    }
  }
}