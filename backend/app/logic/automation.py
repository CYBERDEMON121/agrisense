"""Closed-loop automation: the rules that make the farm act on its own.

Two control loops run here, and both are deliberately *conservative*: every rule
has a confirmation counter, a hysteresis band and interlocks, because an
irrigation pump that chatters on a noisy soil probe or a drone that launches in
a gust costs far more than a few minutes of delay.

Irrigation loop (per zone)::

    moisture fraction < LOW for CONFIRM ticks  ->  open zone to refill to HIGH
    moisture fraction > HIGH                    ->  close zone
    rainfall in progress                         ->  hold (rain is cheaper water)

Spraying loop (per farm)::

    pest pressure > THRESHOLD for CONFIRM ticks
      AND wind <= wind_spray_max_ms
      AND solar >= daylight_min_sun
      AND no rain for rain_hold_min
      AND drone battery > 60% and payload > 0
      AND no mission already flying                ->  plan + launch mission

The drone additionally returns home on low battery (inside the drone itself, so
the safety case does not depend on this engine running).
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..bus import EventBus
from ..sim.weather import WeatherState

if TYPE_CHECKING:  # pragma: no cover
    from ..sim.engine import SimulationEngine


@dataclass(slots=True)
class Alert:
    ts: float
    level: str          # info | warning | critical
    kind: str
    message: str
    device: str | None = None
    plot: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    alert_id: int = 0
    acked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.alert_id, "ts": self.ts, "level": self.level, "kind": self.kind,
                "message": self.message, "device": self.device, "plot": self.plot,
                "acked": self.acked, "meta": self.meta}


@dataclass(slots=True)
class Decision:
    ts: float
    rule: str
    action: str          # act | hold | skip | wait
    reason: str
    target: str = "-"
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "rule": self.rule, "action": self.action, "target": self.target,
                "reason": self.reason, "data": self.data}


class AutomationEngine:
    """Evaluates every control rule once per simulation tick."""

    #: Battery/payload the drone needs before a new mission is launched.
    MIN_BATTERY_PCT = 60.0
    MIN_PAYLOAD_FRAC = 0.05
    #: Ticks between irrigation start decisions (sensor duty cycle is slower).
    IRRIGATION_CONFIRM_TICKS = 2
    #: Never open a second zone if the tank cannot supply it.
    MIN_TANK_L = 4000.0

    def __init__(self, engine: "SimulationEngine", bus: EventBus) -> None:
        self.engine = engine
        self.bus = bus
        self.settings: Settings = engine.settings
        self.enabled = True
        self.auto_spray = True
        self.auto_irrigate = True

        self._alerts: list[Alert] = []
        self._decisions: deque[Decision] = deque(maxlen=400)
        self._alert_seq = itertools.count(1)
        self._dry_ticks: dict[str, int] = {}
        self._wet_ticks: dict[str, int] = {}
        self._pest_ticks: dict[str, int] = {}
        self._spray_hold_until: float = 0.0
        self._last_mission_ts: float = 0.0
        self._zone_open_ts: dict[str, float] = {}
        self._alerts_by_key: dict[str, Alert] = {}
        self.actions_taken = 0
        self.last_action_ts = 0.0

    # ------------------------------------------------------------------ #
    # entry point
    # ------------------------------------------------------------------ #
    def evaluate(self, ts: float, dt_hours: float, weather: WeatherState) -> None:
        if not self.enabled:
            return
        self._irrigation_rule(ts, dt_hours, weather)
        self._spray_rule(ts, weather)
        self._health_rules(ts, weather)

    # ------------------------------------------------------------------ #
    # irrigation
    # ------------------------------------------------------------------ #
    def _irrigation_rule(self, ts: float, dt_hours: float, w: WeatherState) -> None:
        s = self.settings
        farm = self.engine.field
        irr = self.engine.irrigation

        if not self.auto_irrigate:
            return

        for zone_id, plot_ids in farm.zones.items():
            zone = irr.zones.get(zone_id)
            if zone is None:
                continue
            plots = [farm.by_id[p] for p in plot_ids if p in farm.by_id]
            if not plots:
                continue
            # Hysteresis on the *driest* plot: the block is watered for the
            # worst-managed spot, which is what a real zone controller does.
            driest = min(plots, key=lambda p: p.moisture_fraction)
            fraction = driest.moisture_fraction
            deficit = driest.depletion_fraction

            if zone.valve_open:
                if zone.trigger and zone.trigger != "auto":
                    # An operator is watering this block by hand. A closed loop
                    # that re-closes a valve seconds after opening it is worse
                    # than no automation, so stand down and let the cycle run to
                    # its target_mm (or to an explicit close).
                    self._log_hold("irrigate", zone_id,
                                   f"{zone_id} held open by operator "
                                   f"({zone.trigger}) - automation standing down",
                                   {"applied_mm": round(zone.applied_mm, 1),
                                    "target_mm": round(zone.target_mm, 1)})
                    continue
                # Close on the refill target, or if rain has arrived.
                if fraction >= s.soil_moisture_high:
                    irr.close_zone(zone_id, ts, trigger="moisture_recovered")
                    self._wet_ticks[zone_id] = 0
                    self._decide("irrigate", "act", f"{zone_id} reached "
                                 f"{fraction:.0%} of field capacity", zone_id,
                                 {"fraction": round(fraction, 3)})
                else:
                    self._log_hold("irrigate", zone_id,
                                   f"{zone_id} filling, {fraction:.0%} < "
                                   f"{s.soil_moisture_high:.0%}", {"fraction": round(fraction, 3)})
                continue

            # --- decide whether to start a cycle ---
            if w.rainfall > 0.2:
                self._dry_ticks[zone_id] = 0
                self._log_skip("irrigate", zone_id,
                               f"rain {w.rainfall:.1f} mm/h - rain is cheaper than pumped water")
                continue
            if fraction < s.soil_moisture_low:
                self._dry_ticks[zone_id] = self._dry_ticks.get(zone_id, 0) + 1
            else:
                self._dry_ticks[zone_id] = 0

            confirmed = self._dry_ticks.get(zone_id, 0) >= self.IRRIGATION_CONFIRM_TICKS
            if not confirmed:
                self._log_wait("irrigate", zone_id,
                               f"{zone_id} at {fraction:.0%}, "
                               f"below {s.soil_moisture_low:.0%} for "
                               f"{self._dry_ticks.get(zone_id, 0)}/"
                               f"{self.IRRIGATION_CONFIRM_TICKS} ticks",
                               {"fraction": round(fraction, 3)})
                continue
            if irr.tank_l < self.MIN_TANK_L:
                self._log_hold("irrigate", zone_id,
                               f"tank at {irr.tank_l / 1000:.0f} kL, waiting on borewell")
                continue
            if len(irr.open_zones()) >= 2:
                # Pump head drops sharply past two zones; stagger the cycles.
                self._log_hold("irrigate", zone_id,
                               "two zones already open - pump head limit")
                continue

            # Refill to the high threshold expressed as a depth: the depth that
            # moves the driest plot's root zone from wilting point back to field
            # capacity, capped at a single practical application.
            crop = driest.crop
            refill_mm = min(28.0, max(8.0,
                                      (s.soil_moisture_high - fraction)
                                      * crop.root_depth_m * 1000.0 * driest.theta_fc))
            started = irr.open_zone(zone_id, refill_mm, ts, trigger="auto")
            if started is None:
                self._log_hold("irrigate", zone_id, "valve refused - tank dry")
                continue
            self._dry_ticks[zone_id] = 0
            self._zone_open_ts[zone_id] = ts
            self.actions_taken += 1
            self.last_action_ts = ts
            self._decide("irrigate", "act",
                         f"{zone_id} opened for {refill_mm:.0f} mm "
                         f"({driest.plot_id} at {fraction:.0%})", zone_id,
                         {"mm": round(refill_mm, 1), "plot": driest.plot_id,
                          "depletion": round(deficit, 3)})

    # ------------------------------------------------------------------ #
    # spraying
    # ------------------------------------------------------------------ #
    def spray_allowed(self) -> bool:
        """Interlock consulted by the drone every step.

        Even mid-mission, an airspeed/rain/wind gate can cut the nozzle, so a
        gust that arrives after take-off stops spraying rather than being
        noticed only at the next mission planning round.
        """
        if not self.enabled or not self.auto_spray:
            return False
        w = self.engine.weather_state
        if w.rainfall > 0.1:
            return False
        if w.wind_speed > self.settings.wind_spray_max_ms:
            return False
        return True

    def _spray_rule(self, ts: float, w: WeatherState) -> None:
        s = self.settings
        farm = self.engine.field
        drone = self.engine.drone

        if not self.auto_spray:
            self._decide("spray", "skip", "automatic spraying disabled", "-")
            return
        if self._spray_hold_until > ts:
            self._decide("spray", "hold",
                         f"rain hold active for {(self._spray_hold_until - ts) / 60:.0f} min",
                         "-")
            return
        if w.rainfall > 0.1 or (w.last_rain_ts is not None and ts - w.last_rain_ts < s.rain_hold_min * 60):
            self._spray_hold_until = max(self._spray_hold_until,
                                         (w.last_rain_ts or ts) + s.rain_hold_min * 60)
            self._decide("spray", "hold",
                         f"rain within {s.rain_hold_min} min - product would wash off", "-")
            return

        # Find plots over threshold and count consecutive confirmations.
        hot = [p for p in farm.plots if p.pest_pressure > s.pest_spray_threshold]
        hot_ids = {p.plot_id for p in hot}
        for plot in hot:
            self._pest_ticks[plot.plot_id] = self._pest_ticks.get(plot.plot_id, 0) + 1
        for pid in list(self._pest_ticks):
            if pid not in hot_ids:
                del self._pest_ticks[pid]
        if not hot:
            self._decide("spray", "wait",
                         f"no plot above {s.pest_spray_threshold:.0%} pest pressure", "-",
                         {"pest_max": round(max((p.pest_pressure for p in farm.plots), default=0), 3)})
            return

        # Group by area so one mission covers a contiguous block.
        areas: dict[str, list[Any]] = {}
        for plot in hot:
            if self._pest_ticks.get(plot.plot_id, 0) < s.pest_confirm_ticks:
                continue
            areas.setdefault(plot.area_id, []).append(plot)

        if not areas:
            pending = max((self._pest_ticks.get(p.plot_id, 0) for p in hot), default=0)
            self._decide("spray", "wait",
                         f"pest pressure confirmed {pending}/{s.pest_confirm_ticks} ticks", "-")
            return

        area_id = max(areas, key=lambda a: len(areas[a]))
        plots = sorted(areas[area_id], key=lambda p: -p.pest_pressure)

        # --- interlocks ---
        if w.wind_speed > s.wind_spray_max_ms:
            self._decide("spray", "hold",
                         f"wind {w.wind_speed:.1f} m/s above limit {s.wind_spray_max_ms:.0f}",
                         area_id, {"wind": round(w.wind_speed, 2),
                                   "gust": round(w.wind_gust, 2)})
            return
        if w.solar < s.daylight_min_sun:
            self._decide("spray", "hold",
                         f"solar {w.solar:.0f} W/m2 below legal minimum "
                         f"{s.daylight_min_sun:.0f}", area_id, {"solar": round(w.solar)})
            return
        if drone.state not in ("idle",):
            self._decide("spray", "hold",
                         f"drone busy ({drone.state})"
                         + (f", sortie {drone.mission.sorties}" if drone.mission else ""),
                         area_id, {"drone_state": drone.state})
            return
        if drone.battery * 100 < self.MIN_BATTERY_PCT:
            self._decide("spray", "hold",
                         f"battery {drone.battery * 100:.0f}% below "
                         f"{self.MIN_BATTERY_PCT:.0f}%", area_id)
            return
        if drone.payload_l < drone.CAPACITY_L * self.MIN_PAYLOAD_FRAC:
            self._decide("spray", "hold",
                         f"payload {drone.payload_l:.1f} L nearly empty", area_id)
            return
        need_l = sum(p.area_ha for p in plots) * 0.35
        if need_l > drone.payload_l:
            self._decide("spray", "hold",
                         f"mission needs {need_l:.1f} L but only {drone.payload_l:.1f} L loaded",
                         area_id)
            return
        # Residual protection from a recent application means spraying again
        # wastes product and money.
        residual = [p.plot_id for p in plots if p.pest_residual_days > 0.5]
        if residual and len(residual) == len(plots):
            self._decide("spray", "hold",
                         "all plots still inside pesticide residual window", area_id)
            return
        plots = [p for p in plots if p.pest_residual_days <= 0.5] or plots

        mission = drone.plan_mission(plots, ts, trigger="auto_pest",
                                     wind_dir=w.wind_dir, wind_speed=w.wind_speed)
        if not drone.start(ts):
            self._decide("spray", "hold", "drone refused launch", area_id)
            return
        self._pest_ticks.clear()
        self._last_mission_ts = ts
        self.actions_taken += 1
        self.last_action_ts = ts
        worst = max(p.pest_pressure for p in plots)
        self._decide("spray", "act",
                     f"launched {mission.ref} over {len(plots)} plots "
                     f"({mission.target_area_ha:.1f} ha, {need_l:.1f} L, "
                     f"worst pest {worst:.0%})", area_id,
                     {"mission": mission.ref, "plots": len(plots),
                      "area_ha": round(mission.target_area_ha, 2),
                      "wind": round(w.wind_speed, 2), "deposition": round(
                          drone.deposition_efficiency(w.wind_speed), 3)})

    # ------------------------------------------------------------------ #
    # monitoring rules (alerts, no actuation)
    # ------------------------------------------------------------------ #
    def _health_rules(self, ts: float, w: WeatherState) -> None:
        farm = self.engine.field
        drone = self.engine.drone
        irr = self.engine.irrigation

        for plot in farm.plots:
            if plot.health < 0.35:
                self._raise(ts, "critical", "crop_health",
                            f"{plot.name} health {plot.health:.0%}",
                            plot=plot.plot_id,
                            meta={"health": round(plot.health, 3),
                                  "pest": round(plot.pest_pressure, 3),
                                  "moisture": round(plot.moisture_pct, 1)})
            elif plot.water_stress > 0.6:
                self._raise(ts, "warning", "water_stress",
                            f"{plot.name} under severe water stress",
                            plot=plot.plot_id,
                            meta={"water_stress": round(plot.water_stress, 3),
                                  "moisture": round(plot.moisture_pct, 1)})
            elif plot.disease_pressure > 0.5:
                self._raise(ts, "warning", "disease",
                            f"{plot.name} disease pressure {plot.disease_pressure:.0%}",
                            plot=plot.plot_id,
                            meta={"disease": round(plot.disease_pressure, 3)})

        if irr.tank_level_pct < 12.0:
            self._raise(ts, "warning", "tank_low",
                        f"water tank at {irr.tank_level_pct:.0f}%",
                        device="tank-01",
                        meta={"litres": round(irr.tank_l), "pct": round(irr.tank_level_pct, 1),
                              "source": irr.source})
        if irr.tank_l <= 1.0 and not irr.borewell_running:
            self._raise(ts, "critical", "water_supply",
                        "tank empty and borewell not running - irrigation blocked",
                        device="borewell-01")

        if drone.battery < 25 and drone.state not in ("servicing",):
            self._raise(ts, "warning", "drone_battery",
                        f"drone battery {drone.battery * 100:.0f}%", device=drone.device_id,
                        meta={"battery": round(drone.battery * 100, 1), "state": drone.state})
        if drone.last_error and drone.state not in ("idle",):
            self._raise(ts, "warning", "drone_event",
                        f"drone: {drone.last_error}", device=drone.device_id,
                        meta={"state": drone.state})

        for plot in farm.plots:
            probe = self.engine.sensors.probes.get(plot.sensor_id)
            if probe is None:
                continue
            if probe.status != "online":
                self._raise(ts, "critical", "sensor_offline",
                            f"probe {probe.device_id} is {probe.status}",
                            device=probe.device_id, plot=plot.plot_id,
                            meta={"status": probe.status})
            elif probe.battery < 15:
                self._raise(ts, "warning", "sensor_battery",
                            f"probe {probe.device_id} at {probe.battery:.0f}%",
                            device=probe.device_id, plot=plot.plot_id,
                            meta={"battery": round(probe.battery, 1)})

        for cam in self.engine.cameras.cameras.values():
            if cam.status != "online":
                self._raise(ts, "warning", "camera_offline",
                            f"{cam.name} is {cam.status}", device=cam.camera_id,
                            plot=cam.area_id, meta={"status": cam.status})
            for det in cam.last_dets:
                if det.label.endswith("_summary") and det.severity > 0.35:
                    self._raise(ts, "warning", "camera_disease",
                                f"{cam.name}: {det.label.replace('_', ' ')} "
                                f"severity {det.severity:.0%}",
                                device=cam.camera_id, plot=cam.area_id,
                                meta={"severity": round(det.severity, 3),
                                      "label": det.label})

    # ------------------------------------------------------------------ #
    # decision + alert bookkeeping
    # ------------------------------------------------------------------ #
    def _decide(self, rule: str, action: str, reason: str, target: str,
                data: dict[str, Any] | None = None) -> None:
        d = Decision(ts=self.engine.clock.now(), rule=rule, action=action, reason=reason,
                     target=target, data=data or {})
        self._decisions.append(d)
        self.bus.publish(self.bus.topic("system", "rules", "decision"), {
            "value": 1, "rule": rule, "action": action, "target": target,
            "reason": reason, "data": d.data, "ts": d.ts})

    def _log_hold(self, rule: str, target: str, reason: str,
                  data: dict[str, Any] | None = None) -> None:
        # Only log holds when the state changes, so the log stays readable.
        last = self._decisions[-1] if self._decisions else None
        if last is not None and last.rule == rule and last.action == "hold" and last.target == target:
            return
        self._decide(rule, "hold", reason, target, data)

    def _log_wait(self, rule: str, target: str, reason: str,
                  data: dict[str, Any] | None = None) -> None:
        last = self._decisions[-1] if self._decisions else None
        if last is not None and last.rule == rule and last.action == "wait" and last.target == target:
            return
        self._decide(rule, "wait", reason, target, data)

    def _log_skip(self, rule: str, target: str, reason: str,
                  data: dict[str, Any] | None = None) -> None:
        self._decide(rule, "skip", reason, target, data)

    def _raise(self, ts: float, level: str, kind: str, message: str,
               device: str | None = None, plot: str | None = None,
               meta: dict[str, Any] | None = None) -> None:
        """Raise an alert, de-duplicating on (kind, target) while it persists."""
        key = f"{kind}|{device or '-'}|{plot or '-'}"
        existing = self._alerts_by_key.get(key)
        if existing is not None:
            existing.ts = ts
            existing.message = message
            existing.level = level
            existing.meta = meta or {}
            return
        alert = Alert(ts=ts, level=level, kind=kind, message=message, device=device,
                      plot=plot, meta=meta or {}, alert_id=next(self._alert_seq))
        self._alerts.append(alert)
        self._alerts_by_key[key] = alert
        del self._alerts[:-200]
        self.bus.publish(self.bus.topic("system", "alerts", "alert"), {
            "value": 1, "level": level, "kind": kind, "message": message,
            "device": device, "plot": plot, "meta": meta or {}, "ts": ts,
            "alert_id": alert.alert_id})
        # A critical alert is worth waking a human for.
        if level == "critical":
            self.bus.publish(self.bus.topic("system", "alerts", "escalation"), {
                "value": 1, "kind": kind, "message": message, "ts": ts})

    def ack(self, alert_id: int) -> None:
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.acked = True
                self._alerts_by_key.pop(f"{alert.kind}|{alert.device or '-'}|{alert.plot or '-'}", None)

    def ack_all(self) -> int:
        count = sum(1 for a in self._alerts if not a.acked)
        for alert in self._alerts:
            alert.acked = True
        self._alerts_by_key.clear()
        return count

    def clear_resolved(self) -> int:
        """Drop alerts whose condition no longer holds."""
        resolved = 0
        for key in list(self._alerts_by_key):
            alert = self._alerts_by_key[key]
            if alert.kind == "crop_health":
                plot = self.engine.field.by_id.get(alert.plot or "")
                if plot is not None and plot.health >= 0.45:
                    self._resolve(alert)
                    resolved += 1
            elif alert.kind == "tank_low":
                if self.engine.irrigation.tank_level_pct >= 25.0:
                    self._resolve(alert)
                    resolved += 1
        return resolved

    def _resolve(self, alert: Alert) -> None:
        if alert in self._alerts:
            self._alerts.remove(alert)
        self._alerts_by_key.pop(f"{alert.kind}|{alert.device or '-'}|{alert.plot or '-'}", None)

    # ------------------------------------------------------------------ #
    # read-out
    # ------------------------------------------------------------------ #
    def active_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        order = {"critical": 0, "warning": 1, "info": 2}
        unacked = [a for a in self._alerts if not a.acked]
        unacked.sort(key=lambda a: (order.get(a.level, 3), -a.ts))
        return [a.as_dict() for a in unacked[:limit]]

    def decision_log(self, limit: int = 40) -> list[dict[str, Any]]:
        return [d.as_dict() for d in list(self._decisions)[-limit:]][::-1]

    def state(self) -> dict[str, Any]:
        unacked = [a for a in self._alerts if not a.acked]
        return {
            "enabled": self.enabled,
            "auto_spray": self.auto_spray,
            "auto_irrigate": self.auto_irrigate,
            "actions_taken": self.actions_taken,
            "last_action_ts": self.last_action_ts,
            "open_zones": [z.zone_id for z in self.engine.irrigation.open_zones()],
            "alerts_open": len(unacked),
            "critical": sum(1 for a in unacked if a.level == "critical"),
            "warning": sum(1 for a in unacked if a.level == "warning"),
            "spray_hold_s": max(0.0, round(
                self._spray_hold_until - self.engine.clock.now(), 1)),
            "pest_confirmed": {k: v for k, v in self._pest_ticks.items()},
            "dry_ticks": dict(self._dry_ticks),
            "thresholds": {
                "soil_moisture_low": self.settings.soil_moisture_low,
                "soil_moisture_high": self.settings.soil_moisture_high,
                "pest_spray_threshold": self.settings.pest_spray_threshold,
                "pest_confirm_ticks": self.settings.pest_confirm_ticks,
                "wind_spray_max_ms": self.settings.wind_spray_max_ms,
                "rain_hold_min": self.settings.rain_hold_min,
                "daylight_min_sun": self.settings.daylight_min_sun,
                "min_battery_pct": self.MIN_BATTERY_PCT,
            },
        }


