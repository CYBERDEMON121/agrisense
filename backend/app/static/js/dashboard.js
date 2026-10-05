// Dashboard page: everything needed to *watch* the farm.
//
// This page owns no controls that change the simulation - those live on the
// simulator console.  The exceptions are the actions an operator needs in the
// moment (return the drone, plan a spray mission by hand), because those are
// safety-relevant and should not be two clicks away.
//
// The runtime - live socket, panel registry, clock, transport, footer - comes
// from core.js, which the simulator console shares.

import { $ } from './dom.js';
import { api } from './api.js';
import { Chart } from './charts.js';
import { FieldMap } from './map.js';
import { Kpis, WeatherPanel, IrrigationPanel } from './panels.js';
import { CameraWall } from './cameras.js';
import { DeviceTable } from './devices.js';
import { AlertsPanel } from './alerts.js';
import { DecisionLog } from './decisions.js';
import { DronePanel } from './drone.js';
import { Trends } from './trends.js';
import { Clock, StatusLine, Transport, Console, bootPage } from './core.js';

export const app = new Console('dashboard');

export const { onFrame } = app;

bootPage('dashboard', () => {
  const clock = new Clock();
  const transport = new Transport();
  const kpis = new Kpis();
  const weather = new WeatherPanel(new Chart($('#weatherChart')));
  const irrigation = new IrrigationPanel();
  irrigation.attachChart(new Chart($('#waterChart')));
  const map = new FieldMap();
  const cameras = new CameraWall();
  const devices = new DeviceTable();
  const alerts = new AlertsPanel();
  const decisions = new DecisionLog();
  const drone = new DronePanel();
  const trends = new Trends();

  // A clicked plot drives the trend panel and logs the crop's agronomy.
  document.addEventListener('agrisense:plot', (ev) => {
    trends.setPlot(ev.detail);
    api.plot(ev.detail).then(({ plot, sensor, crop }) => {
      console.info(`${plot.name} - ${crop.label}, ${crop.stage}, Kc ${crop.kc}, `
        + `root zone holds ${crop.taw_mm} mm, ${crop.depletion_mm} mm depleted`
        + (sensor ? `, probe ${sensor.moisture}%` : ''));
    }).catch(() => {});
  });

  app.use(clock, transport, kpis, weather, irrigation, map, cameras, devices,
    alerts, decisions, drone, trends, new StatusLine());

  app.start();
  return app;
});

export default app;