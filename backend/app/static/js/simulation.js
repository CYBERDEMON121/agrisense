// Simulation page: every control that changes the world.
//
// The dashboard answers "what is the farm doing?".  This page answers "make it
// do that instead".  It owns four groups of control:
//
//   clock       speed presets and manual stepping (no wall clock involved)
//   thresholds  the live rule parameters, so interlocks can be moved
//   scenarios   forcing functions on weather, pests, soil water, availability
//   commands    direct device control, and the raw engine internals
//
// The runtime is shared with the dashboard via core.js; only the panels differ.

import { Clock, StatusLine, Transport, Console, bootPage } from './core.js';
import { AlertsPanel } from './alerts.js';
import { DecisionLog } from './decisions.js';
import { ClockPanel } from './sim-clock.js';
import { RulePanel, CommandPanel } from './sim-rules.js';
import { ScenarioPanel } from './sim-scenarios.js';
import { EnginePanel, EventStream } from './sim-engine.js';

export const app = new Console('simulation');

export const { onFrame } = app;

bootPage('simulation', () => {
  const clock = new Clock();
  const transport = new Transport();
  const clockPanel = new ClockPanel();
  const rules = new RulePanel();
  const scenarios = new ScenarioPanel();
  const commands = new CommandPanel();
  const engine = new EnginePanel();
  const events = new EventStream();
  const alerts = new AlertsPanel();
  // The switches live on the threshold panel above, so this log is read-only.
  const decisions = new DecisionLog({ toggles: false, tag: false });

  app.use(clock, transport, clockPanel, rules, scenarios, commands, engine,
    events, alerts, decisions, new StatusLine());

  app.start();
  engine.refresh().catch((err) => console.warn('engine refresh failed', err));
  return app;
});

export default app;