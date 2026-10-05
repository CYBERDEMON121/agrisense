"""The simulation engine: the single loop that drives every device.

One tick, in order:

    clock -> weather -> field (soil/phenology/pest) -> sensors
          -> cameras (round-robin) -> irrigation -> drone
          -> automation rules -> bus publish -> database

Devices never call each other.  They are wired only through the field model
(irrigation credits water, spraying knocks down pests) and the event bus, which
is what makes the topology identical to a real deployment where every device
talks over MQTT and nothing is called directly.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from ..bus import EventBus, MqttTransport
from ..clock import SimClock, hour_of_day, to_local
from ..config import Settings
from ..db import Database
from ..logic.automation import AutomationEngine
from .devices.camera import CameraArray
from .devices.drone import SprayDrone
from .devices.irrigation import IrrigationSystem
from .devices.sensors import SensorFleet
from .field import FieldModel, Plot
from .weather import WeatherModel, WeatherState


class SimulationEngine:
    """Owns every device and advances them all on the simulated clock."""

    #: Finest sub-step handed to integrating devices (drone autopilot), and the
    #: ceiling on how many sub-steps one tick may be broken into.  The ceiling
    #: bounds worst-case CPU at very high clock speeds; beyond it the drone
    #: simply flies with a coarser integration, which degrades accuracy but
    #: never stalls the loop.
    DRONE_MAX_STEP_S = 1.0
    DRONE_MAX_SUBSTEPS = 240

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        #: Pristine copy of the thresholds as shipped, captured before any
        #: runtime tuning.  "Reset to defaults" needs a reference to compare
        #: against, and re-instantiating Settings would pick up the environment
        #: a second time.
        self._default_settings = replace(settings)
        self.rng = random.Random(settings.seed)

        self.clock = SimClock(start_epoch=settings.start_epoch, speed=settings.default_speed)
        self.bus = EventBus(settings.farm_id)
        self.transport = MqttTransport(self.bus, settings.mqtt_broker, settings.mqtt_port)
        self.db = Database(settings.db_path) if settings.write_telemetry else None

        start = settings.start_epoch
        self.weather = WeatherModel(settings.latitude, self.rng, start)
        self.field = FieldModel(self.rng, start, settings.latitude)
        self.sensors = SensorFleet(self.bus, self.field, self.rng)
        self.cameras = CameraArray(self.field, self.rng, settings.camera_width,
                                   settings.camera_height)
        self.irrigation = IrrigationSystem(self.bus, self.field, self.rng, start)
        self.drone = SprayDrone(self.bus, self.field, self.rng)

        self.automation = AutomationEngine(self, self.bus)

        self.weather_state: WeatherState = self.weather.state
        self.tick_count = 0
        self.sim_hours_done = 0.0
        self.last_tick_ts = start
        self.last_tick_wall = time.monotonic()
        self.tick_hz_avg = 0.0
        self.started_wall = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused = False
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._plot_rows: dict[str, float] = {}
        self._camera_cursor = 0
        self._mission_rows: list[dict[str, Any]] = []
        self._last_prune_ts = start

        self._register_devices()
        self._subscribe_persistence()

    # ------------------------------------------------------------------ #
    # wiring
    # ------------------------------------------------------------------ #
    def _register_devices(self) -> None:
        if self.db is None:
            return
        for rec in (self.sensors.device_records() + self.cameras.device_records()
                    + self.irrigation.device_records() + [self.drone.device_record()]):
            self.db.upsert_device(first_seen=self.settings.start_epoch, **rec)

    def _subscribe_persistence(self) -> None:
        """Mirror the interesting parts of the bus into SQLite."""
        if self.db is None:
            return
        db = self.db

        def on_soil(topic: str, payload: dict[str, Any], msg: Any) -> None:
            device = payload.get("plot") or topic.split("/")[3]
            db.telemetry(payload.get("ts", msg.ts), device, topic, payload)

        def on_weather(topic: str, payload: dict[str, Any], msg: Any) -> None:
            db.telemetry(payload.get("ts", msg.ts), "ws-main", topic, payload)

        def on_event(topic: str, payload: dict[str, Any], msg: Any) -> None:
            db.submit("INSERT INTO bus_log(ts, topic, payload) VALUES (?,?,?)",
                      (payload.get("ts", msg.ts), topic, _j(payload)))

        def on_irrigation(topic: str, payload: dict[str, Any], msg: Any) -> None:
            db.submit(
                "INSERT INTO irrigation_events(ts, zone, action, flow_lpm, duration_s, trigger, reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (payload.get("ts", msg.ts), payload.get("zone", "-"), payload.get("action", "-"),
                 payload.get("flow_lpm"), payload.get("minutes"), payload.get("trigger"),
                 payload.get("reason")))

        def on_mission(topic: str, payload: dict[str, Any], msg: Any) -> None:
            self._mission_rows.append({"ts": payload.get("ts", msg.ts), **payload})
            del self._mission_rows[:-200]
            if payload.get("event") in ("complete", "abort"):
                mission = self._pending_mission
                if mission is None:
                    return
                db.submit(
                    "INSERT INTO missions(ref, ts_start, ts_end, kind, status, trigger, area_ha,"
                    " battery_used, payload_l, waypoints, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(ref) DO UPDATE SET ts_end=excluded.ts_end, status=excluded.status",
                    (mission.ref, mission.started_ts or mission.created_ts, mission.ended_ts,
                     mission.kind, mission.status, mission.trigger, mission.target_area_ha,
                     round((mission.battery_start - self.drone.battery) * 100, 1)
                     if mission.status in ("done", "partial") else None,
                     mission.sprayed_litres,
                     _j([w.as_dict() for w in mission.waypoints]),
                     _j({**mission.notes, "coverage": mission.coverage,
                         "dose_ml_ha": mission.dose_ml_ha, "sorties": mission.sorties})))

        def on_application(topic: str, payload: dict[str, Any], msg: Any) -> None:
            db.submit(
                "INSERT INTO spray_events(ts, mission_ref, plot_id, dose_ml_ha, coverage,"
                " pest_before, pest_after) VALUES (?,?,?,?,?,?,?)",
                (payload.get("ts", msg.ts), payload.get("mission"), payload.get("plot"),
                 payload.get("dose_ml_ha"), payload.get("coverage"),
                 payload.get("pest_before"), payload.get("pest_after")))

        def on_detection(topic: str, payload: dict[str, Any], msg: Any) -> None:
            for det in payload.get("detections", []):
                db.submit(
                    "INSERT INTO camera_detections(ts, camera_id, plot_id, label, confidence,"
                    " bbox, severity) VALUES (?,?,?,?,?,?,?)",
                    (payload.get("ts", msg.ts), payload.get("camera"), payload.get("plot"),
                     det.get("label"), det.get("confidence"), _j(det.get("bbox", [])),
                     det.get("severity")))

        def on_alert(topic: str, payload: dict[str, Any], msg: Any) -> None:
            db.submit(
                "INSERT INTO alerts(ts, level, kind, message, device_id, plot_id, meta)"
                " VALUES (?,?,?,?,?,?,?)",
                (payload.get("ts", msg.ts), payload.get("level", "info"), payload.get("kind", "-"),
                 payload.get("message", ""), payload.get("device"), payload.get("plot"),
                 _j(payload.get("meta", {}))))

        b = self.bus
        b.subscribe(b.topic("soil", "+", "telemetry"), on_soil)
        b.subscribe(b.topic("weather", "+", "telemetry"), on_weather)
        b.subscribe(b.topic("irrigation", "+", "#"), on_irrigation)
        b.subscribe(b.topic("drone", "+", "mission"), on_mission)
        b.subscribe(b.topic("drone", "+", "application"), on_application)
        b.subscribe(b.topic("camera", "+", "detections"), on_detection)
        b.subscribe(b.topic("system", "+", "alert"), on_alert)
        b.subscribe(b.topic("system", "+", "decision"), on_event)

    _pending_mission: Any = None

    def on_broadcast(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a hook for the WebSocket feed (called from the API layer)."""
        self._listeners.append(fn)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.db is not None:
            self.db.start()
        self._thread = threading.Thread(target=self._run, name="sim-engine", daemon=True)
        self._thread.start()
        self._publish_system("engine_started", {"speed": self.clock.speed})

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.db is not None:
            self.db.flush()
            self.db.stop()

    def _run(self) -> None:
        period = 1.0 / max(self.settings.tick_hz, 0.1)
        next_at = time.monotonic()
        while not self._stop.is_set():
            self.tick()
            next_at += period
            sleep = next_at - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # Never spiral: if we fell behind, resynchronise.
                next_at = time.monotonic()

    # ------------------------------------------------------------------ #
    # the tick
    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        """Advance every device by one step of simulated time."""
        if self._paused:
            self.last_tick_wall = time.monotonic()
            return

        wall_now = time.monotonic()
        dt_wall = max(1e-4, wall_now - self.last_tick_wall)
        self.last_tick_wall = wall_now

        ts_now = self.clock.now()
        dt_sim = max(0.0, ts_now - self.last_tick_ts)
        self.last_tick_ts = ts_now
        # A very large step would skip the whole diurnal cycle, so cap it at
        # ~10 simulated minutes and let the next tick catch up.
        dt_sim = min(dt_sim, 600.0)
        self._tick_to(ts_now, dt_sim, dt_wall)

    def _tick_to(self, ts_now: float, dt_sim: float, dt_wall: float) -> None:
        """The body of a tick, with both step sizes handed in explicitly.

        Live ticks measure the wall gap themselves; the simulator console's step
        button synthesises both sizes, because a paused clock never advances on
        its own.
        """
        dt_hours = dt_sim / 3600.0
        self.sim_hours_done += dt_hours
        self.tick_count += 1
        self.tick_hz_avg = 0.9 * self.tick_hz_avg + 0.1 / dt_wall

        # 1) weather drives everything downstream
        w = self.weather.update(ts_now, dt_hours)
        self.weather_state = w

        # 2) agronomy: soil water balance, phenology, pest dynamics
        self.field.step(w, dt_hours)

        # 3) soil probes + weather station publish onto the bus
        self.sensors.update(w, dt_sim)

        # 4) cameras: round-robin so N cameras do not cost N frames per tick
        self._tick_cameras(ts_now, w, dt_wall)

        # 5) irrigation hydraulics
        self.irrigation.step(ts_now, dt_sim, dt_wall)

        # 6) drone autopilot - only spray while the rules say it is legal
        self._tick_drone(ts_now, dt_sim, w)

        # 7) automation: decisions + alerts
        self.automation.evaluate(ts_now, dt_hours, w)

        # 8) persistence of the crop state
        self._tick_persistence(ts_now)

    def _tick_cameras(self, ts: float, w: WeatherState, dt_wall: float) -> None:
        # A faulted camera stops publishing entirely, exactly as a real one that
        # has lost power would: the fleet table goes quiet and the detector is
        # fed nothing new.
        cams = [c for c in self.cameras.cameras.values() if c.status == "online"]
        if not cams:
            return
        budget = max(1, int(round(self.settings.camera_hz * dt_wall)))
        fixed = [c for c in cams if c.kind == "fixed"]
        drones = [c for c in cams if c.kind == "drone"]
        pool = fixed + drones
        for _ in range(min(budget, len(pool))):
            cam = pool[self._camera_cursor % len(pool)]
            self._camera_cursor += 1
            area_plots = (self.field.plots_in_area(cam.area_id)
                          if cam.area_id and cam.area_id in self.field.areas else [])
            frame = self.cameras.render(cam, w, area_plots, dt_wall, ts)
            dets = self.cameras.detect(cam, frame)
            worst = max((d.severity for d in dets), default=0.0)
            self.bus.publish(self.bus.topic("camera", cam.camera_id, "detections"), {
                "value": round(worst, 3), "camera": cam.camera_id, "plot": cam.area_id,
                "detections": [d.as_dict() for d in dets], "ts": ts})

    def _tick_drone(self, ts: float, dt_sim: float, w: WeatherState) -> None:
        allow = self.automation.spray_allowed()
        # A drone camera exists only while the airframe is airborne.
        airborne = self.drone.state not in ("idle", "servicing", "preflight", "takeoff", "landing")
        cam_id = f"cam-{self.drone.device_id}"
        if airborne and cam_id not in self.cameras.cameras:
            self.drone_cam = self.cameras.attach_drone_camera(cam_id)
        elif not airborne and cam_id in self.cameras.cameras:
            self.cameras.detach(cam_id)

        # The autopilot integrates position and battery, so it needs a fine
        # step regardless of how fast the clock is running.  At 3600x a single
        # tick would otherwise be 30 simulated minutes - the whole battery - and
        # the aircraft would "teleport" from the pad to the far corner.
        n = max(1, min(self.DRONE_MAX_SUBSTEPS,
                       int(math.ceil(dt_sim / self.DRONE_MAX_STEP_S))))
        sub = dt_sim / n
        t = ts - dt_sim
        for _ in range(n):
            t += sub
            self.drone.step(t, sub, w.wind_speed, w.wind_dir, allow_spray=allow)
        self._pending_mission = self.drone.mission

    def _tick_persistence(self, ts: float) -> None:
        if self.db is None:
            return
        self._plot_rows = {p.plot_id: ts for p in self.field.plots}
        for plot in self.field.plots:
            self.db.plot_reading({
                "ts": ts, "plot_id": plot.plot_id, "moisture": round(plot.moisture_pct, 2),
                "temp": round(plot.soil_temp, 2), "ec": round(plot.ec, 3),
                "health": round(plot.health, 4), "pest": round(plot.pest_pressure, 4),
                "stage": plot.stage_index})
        # Prune once an hour of simulated time, not once per tick.
        if ts - self._last_prune_ts > 3600.0:
            self._last_prune_ts = ts
            self.db.prune(ts - self.settings.telemetry_retention_h * 3600.0)

    # ------------------------------------------------------------------ #
    # control surface
    # ------------------------------------------------------------------ #
    def _publish_system(self, event: str, payload: dict[str, Any]) -> None:
        self.bus.publish(self.bus.topic("system", "engine", "event"),
                         {"event": event, **payload})

    def set_speed(self, speed: float) -> float:
        self.clock.set_speed(speed)
        self._publish_system("speed", {"speed": self.clock.speed})
        return self.clock.speed

    def set_paused(self, paused: bool) -> bool:
        self._paused = paused
        self.clock.set_paused(paused)
        self._publish_system("pause", {"paused": paused})
        return paused

    @property
    def paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------ #
    # simulator console: what an operator can push into the world
    # ------------------------------------------------------------------ #
    #: Every threshold the automation rules read, with the bounds the API will
    #: accept.  The simulator console renders its sliders straight from this, so
    #: the UI can never offer a knob the engine does not actually consult.
    TUNABLES: dict[str, tuple[str, float, float, str]] = {
        "soil_moisture_low": ("Irrigation start", 0.05, 0.60, "%"),
        "soil_moisture_high": ("Irrigation stop", 0.20, 0.95, "%"),
        "pest_spray_threshold": ("Spray trigger", 0.10, 0.95, "%"),
        "pest_confirm_ticks": ("Confirm ticks", 1.0, 20.0, "ticks"),
        "wind_spray_max_ms": ("Wind limit", 0.5, 14.0, "m/s"),
        "rain_hold_min": ("Rain hold", 0.0, 240.0, "min"),
        "daylight_min_sun": ("Min sunlight", 0.0, 600.0, "W/m²"),
    }

    def apply_thresholds(self, patch: dict[str, float]) -> dict[str, float]:
        """Retune the rule thresholds while the simulation is running."""
        applied: dict[str, float] = {}
        for name, raw in patch.items():
            spec = self.TUNABLES.get(name)
            if spec is None:
                raise KeyError(name)
            _, lo, hi, _unit = spec
            value = float(raw)
            if name == "pest_confirm_ticks":
                value = int(round(value))
            else:
                value = max(lo, min(hi, value))
            if name == "soil_moisture_high" and value <= self.settings.soil_moisture_low:
                raise ValueError("soil_moisture_high must exceed soil_moisture_low")
            setattr(self.settings, name, value)
            applied[name] = value
        if applied:
            self._publish_system("thresholds", {"thresholds": applied})
        return applied

    def step_manual(self, sim_seconds: float, max_seconds: float = 21600.0) -> dict[str, Any]:
        """Advance the world by hand, in fixed slices, ignoring the wall clock.

        This is the "step" button on the simulator console.  It is the only way
        to inspect a single rule transition: pause, step one tick, look at what
        the decision log said, step again.
        """
        total = max(1.0, min(float(sim_seconds), float(max_seconds)))
        slices = max(1, min(400, int(math.ceil(total / 600.0))))
        sub = total / slices
        ts = self.clock.now()
        for _ in range(slices):
            ts += sub
            self.clock.advance(sub)
            self._tick_to(ts, sub, sub)
        # The stepping thread has not moved, so restore the wall reference or the
        # next live tick would see a multi-hour "dt_wall" and blow up the rates.
        self.last_tick_wall = time.monotonic()
        self._publish_system("step", {"sim_seconds": total, "ts": ts})
        return {"sim_seconds": total, "slices": slices, "ts": ts,
                "ticks": self.tick_count, "sim_hours": round(self.sim_hours_done, 2)}

    def override_weather(self, values: dict[str, float], hours: float) -> dict[str, Any]:
        """Pin weather fields for a bounded number of simulated hours."""
        ts = self.clock.now()
        pinned = self.weather.force(values, hours, ts)
        self._publish_system("weather_override",
                             {"forcing": self.weather.forcing(ts), "scenario": "custom"})
        if not pinned:
            return {"forcing": {}, "message": "no known weather field pinned"}
        return {"forcing": self.weather.forcing(ts)}

    def release_weather(self) -> dict[str, Any]:
        self.weather.release()
        self._publish_system("weather_override", {"forcing": {}, "scenario": "released"})
        return {"forcing": {}}

    def inject_pests(self, area: str | None, pressure: float,
                     plot_ids: list[str] | None = None) -> dict[str, Any]:
        """Raise pest pressure so the spray rule can be watched end to end."""
        if plot_ids:
            targets = [self.field.by_id[p] for p in plot_ids if p in self.field.by_id]
        elif area:
            targets = self.field.plots_in_area(area)
        else:
            targets = self.field.plots
        if not targets:
            raise KeyError(area or ",".join(plot_ids or []))
        level = max(0.0, min(0.99, float(pressure)))
        for plot in targets:
            plot.pest_pressure = max(plot.pest_pressure, level)
            plot.pest_residual_days = 0.0
        self.automation._pest_ticks.clear()
        return {"plots": [p.plot_id for p in targets], "count": len(targets),
                "pressure": round(level, 3),
                "areas": sorted({p.area_id for p in targets})}

    def stress_plots(self, area: str | None, kind: str = "dry") -> dict[str, Any]:
        """Push plots into water stress (dry) or waterlogging (wet)."""
        targets = self.field.plots_in_area(area) if area else self.field.plots
        if not targets:
            raise KeyError(area or "-")
        changed = 0
        for plot in targets:
            if kind == "dry":
                plot.theta = max(plot.theta_wp + 0.005,
                                 plot.theta_wp + (plot.theta_fc - plot.theta_wp) * 0.12)
            elif kind == "wet":
                plot.theta = min(plot.theta_fc * 0.99,
                                 plot.theta_wp + (plot.theta_fc - plot.theta_wp) * 0.96)
            else:
                raise KeyError(kind)
            changed += 1
        return {"area": area or "all", "kind": kind, "plots": changed,
                "moisture_pct": round(sum(p.moisture_pct for p in targets) / len(targets), 1)}

    def set_device_fault(self, device_id: str, fault: str) -> dict[str, Any]:
        """Take a device offline (or bring it back).

        Faults are what make the fleet table worth reading: a probe that stops
        publishing looks identical to a plot that is simply quiet until you
        compare the two.
        """
        fault = fault or "online"
        probe = self.sensors.probes.get(device_id)
        if probe is not None:
            probe.status = fault if fault != "online" else "online"
            if probe.status != "online":
                probe.battery = min(probe.battery, 4.0)
            return {"device_id": device_id, "status": probe.status}
        cam = self.cameras.cameras.get(device_id)
        if cam is not None:
            cam.status = "online" if fault == "online" else fault
            if cam.status != "online":
                cam.fps = 0.0
                cam.last_frame = None
            return {"device_id": device_id, "status": cam.status}
        if device_id == self.drone.device_id:
            if fault != "online":
                self.drone.abort(self.clock.now(), reason=f"fault:{fault}")
            return {"device_id": device_id, "status": "online",
                    "state": self.drone.state}
        if device_id == self.sensors.station.device_id:
            self.sensors.station.status = "online" if fault == "online" else fault
            return {"device_id": device_id, "status": self.sensors.station.status}
        raise KeyError(device_id)

    def fault_devices(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for probe in self.sensors.probes.values():
            if probe.status != "online":
                out.append({"device_id": probe.device_id, "kind": "soil", "status": probe.status})
        for cam in self.cameras.cameras.values():
            if cam.status != "online":
                out.append({"device_id": cam.camera_id, "kind": "camera", "status": cam.status})
        if self.sensors.station.status != "online":
            out.append({"device_id": self.sensors.station.device_id, "kind": "weather",
                        "status": self.sensors.station.status})
        return out

    def controls(self) -> dict[str, Any]:
        """Everything the simulator console needs that is not in the frame."""
        ts = self.clock.now()
        return {
            "tunables": {
                name: {"label": label, "min": lo, "max": hi, "unit": unit,
                       "value": getattr(self.settings, name),
                       "default": getattr(self._default_settings, name)}
                for name, (label, lo, hi, unit) in self.TUNABLES.items()
            },
            "forcing": self.weather.forcing(ts),
            "faults": self.fault_devices(),
            "areas": [{"id": a.area_id, "name": a.name, "plots": len(self.field.zones.get(a.zone, [])),
                       "crop": a.crop_key} for a in self.field.areas.values()],
            "zones": [{"id": z.zone_id, "name": z.name, "plots": len(z.plots),
                       "valve": z.valve_id, "open": z.valve_open} for z in self.irrigation.zones.values()],
            "sensors": sorted(self.sensors.probes),
            "cameras": sorted(self.cameras.cameras),
        }

    # ------------------------------------------------------------------ #
    # aggregate state for the API
    # ------------------------------------------------------------------ #
    def snapshot(self, include_plots: bool = True, include_history: bool = True) -> dict[str, Any]:
        ts = self.clock.now()
        w = self.weather_state
        local = to_local(ts)
        out: dict[str, Any] = {
            "farm": {"id": self.settings.farm_id, "name": self.settings.farm_name,
                     "lat": self.settings.latitude, "lon": self.settings.longitude,
                     "tz": "IST", "tz_offset_h": self.settings.timezone_offset_h},
            "sim": {
                "ts": ts, "iso": local.isoformat(), "date": local.strftime("%Y-%m-%d"),
                "time": local.strftime("%H:%M:%S"), "hour": hour_of_day(ts),
                "day_of_year": local.timetuple().tm_yday,
                "speed": self.clock.speed, "paused": self._paused,
                "ticks": self.tick_count, "sim_hours": round(self.sim_hours_done, 2),
                "sim_days": round(self.sim_hours_done / 24.0, 2),
                "tick_hz": round(self.tick_hz_avg, 2),
                "uptime_s": round(time.monotonic() - self.started_wall, 1),
                "start_epoch": self.settings.start_epoch,
            },
            "weather": self.weather_payload(w),
            "sim_weather_override": self.weather.forcing(ts),
            "field": self.field.stats(),
            "areas": [a.state() for a in self.field.areas.values()],
            "bounds": self.farm_bounds(),
            "fieldMap": self.field_map(),
            "devices": self.device_states(),
            "sensors": self.sensors.fleet_health(),
            "cameras": self.cameras.states(),
            "irrigation": self.irrigation.state(ts),
            "drone": self.drone.snapshot(),
            "automation": self.automation.state(),
            "bus": self.bus.stats(),
            "db": self.db.stats() if self.db else {},
            "transport": self.transport.info(),
        }
        if include_plots:
            out["plots"] = self.field.states()
        if include_history:
            out["recent_events"] = self.recent_events(40)
            out["alerts"] = self.automation.active_alerts(30)
            out["decisions"] = self.automation.decision_log(30)
        return out

    def weather_payload(self, w: WeatherState) -> dict[str, Any]:
        return {
            "ts": w.ts, "temp": round(w.temp, 1), "temp_min": round(w.temp_min, 1),
            "temp_max": round(w.temp_max, 1), "humidity": round(w.humidity * 100, 1),
            "dew_point": round(w.dew_point, 1), "wind": round(w.wind_speed, 2),
            "gust": round(w.wind_gust, 2), "wind_dir": round(w.wind_dir, 0),
            "cloud": round(w.cloud * 100, 1), "rain": round(w.rainfall, 2),
            "solar": round(w.solar, 0), "et0": round(w.et0, 2),
            "pressure": round(w.pressure, 1), "vpd": round(w.vapour_gap, 2),
            "sunrise": round(self.weather.sunrise, 2), "sunset": round(self.weather.sunset, 2),
            "daylight_h": round(self.weather.daylight_hours, 2),
            "is_raining": w.is_raining,
        }

    def farm_bounds(self) -> dict[str, float]:
        """Farm-frame extents (metres, origin at the SW corner) for the map view.

        The drone's pad is included even though it sits outside the planted area,
        because the map has to show where the aircraft is based.
        """
        if not self.field.plots:
            return {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
        points = [(p.x, p.y) for p in self.field.plots]
        points += [(p.x + p.width, p.y + p.height) for p in self.field.plots]
        points.append(self.drone.pad)
        x0 = min(px for px, _ in points)
        y0 = min(py for _, py in points)
        x1 = max(px for px, _ in points)
        y1 = max(py for _, py in points)
        return {"x": round(x0 - 10, 1), "y": round(y0 - 10, 1),
                "w": round(x1 - x0 + 20, 1), "h": round(y1 - y0 + 20, 1)}

    def field_map(self) -> dict[str, Any]:
        """Static geometry the dashboard needs to draw the farm.

        Areas are grouped into one bounding box per irrigation zone: that is the
        unit the pump and the drone actually work in, so it is the unit worth
        outlining.
        """
        boxes: list[dict[str, Any]] = []
        for zone_id in sorted(self.field.zones):
            plots = [self.field.by_id[p] for p in self.field.zones[zone_id]
                     if p in self.field.by_id]
            if not plots:
                continue
            x0 = min(p.x for p in plots)
            y0 = min(p.y for p in plots)
            x1 = max(p.x + p.width for p in plots)
            y1 = max(p.y + p.height for p in plots)
            boxes.append({
                "id": zone_id, "label": f"Zone {zone_id[-1].upper()}",
                "x": round(x0 - 3, 1), "y": round(y0 - 3, 1),
                "w": round(x1 - x0 + 6, 1), "h": round(y1 - y0 + 6, 1),
                "plots": len(plots),
                "area_ha": round(sum(p.area_ha for p in plots), 2),
                "crops": sorted({p.crop_key for p in plots}),
            })
        bounds = self.farm_bounds()
        return {
            "bounds": bounds,
            "area_boxes": boxes,
            "pad": [self.drone.pad[0], self.drone.pad[1]],
            "view_box": f"0 0 {bounds['w']} {bounds['h']}",
        }

    def device_states(self) -> list[dict[str, Any]]:
        out = [p.state() for p in self.sensors.probes.values()]
        out.append(self.sensors.station.state(self.weather_state))
        # Camera.state() is the *view* shape (id/yaw/fov/...); the fleet table
        # wants the device-record shape, so add the identity fields here rather
        # than widening Camera.state() with two names for one thing.
        out.extend({"device_id": c.state()["id"], **c.device_record()}
                   for c in self.cameras.cameras.values())
        out.append(self.drone.device_record())
        out.append({"device_id": "irrigation-controller", "kind": "controller",
                    "name": "Irrigation controller", "area": "pumphouse",
                    "vendor": "Local", "model": "agrios-8", "fw": "2.1.0",
                    "status": "online", "battery": None, "rssi": -50,
                    "meta": {"zones": len(self.irrigation.zones)}})
        for z in self.irrigation.zones.values():
            out.append({"device_id": z.valve_id, "kind": "valve", "name": f"{z.name} solenoid",
                        "area": "pumphouse", "vendor": "Hunter", "model": "DV-24",
                        "fw": "2.0.1", "status": "online", "battery": None, "rssi": -58,
                        "meta": {"zone": z.zone_id, "state": "open" if z.valve_open else "idle",
                                 "pct": round(z.valve_pct, 1)}})
        return out

    def recent_events(self, limit: int = 40) -> list[dict[str, Any]]:
        """Interesting bus traffic only - skip the high-rate telemetry streams."""
        skip = ("/telemetry", "/detections")
        out: list[dict[str, Any]] = []
        for msg in reversed(self.bus.history()):
            if any(msg.topic.endswith(s) for s in skip):
                continue
            out.append({"topic": msg.topic, "ts": msg.payload.get("ts", msg.ts),
                        **msg.payload})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ #
    # history queries
    # ------------------------------------------------------------------ #
    def plot_history(self, plot_id: str, hours: float = 24.0) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        since = self.clock.now() - hours * 3600.0
        return self.db.query(
            "SELECT ts, moisture, temp, ec, health, pest, stage FROM plot_readings"
            " WHERE plot_id = ? AND ts >= ? ORDER BY ts", (plot_id, since))

    def device_history(self, device_id: str, hours: float = 6.0, limit: int = 600) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        since = self.clock.now() - hours * 3600.0
        return self.db.query(
            "SELECT ts, topic, value, payload FROM telemetry WHERE device_id = ? AND ts >= ?"
            " ORDER BY ts DESC LIMIT ?", (device_id, since, limit))

    def alerts(self, limit: int = 100, unacked_only: bool = False) -> list[dict[str, Any]]:
        if self.db is None:
            return self.automation.active_alerts(limit)
        sql = "SELECT * FROM alerts"
        if unacked_only:
            sql += " WHERE acked = 0"
        sql += " ORDER BY ts DESC LIMIT ?"
        rows = self.db.query(sql, (limit,))
        for row in rows:
            row["meta"] = _loads(row.get("meta"))
        return rows

    def missions(self, limit: int = 40) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        rows = self.db.query("SELECT * FROM missions ORDER BY ts_start DESC LIMIT ?", (limit,))
        for row in rows:
            row["notes"] = _loads(row.get("notes"))
            row["waypoints"] = _loads(row.get("waypoints"))
        return rows

    def detections(self, limit: int = 100, label: str | None = None) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        if label:
            return self.db.query(
                "SELECT * FROM camera_detections WHERE label = ? ORDER BY ts DESC LIMIT ?",
                (label, limit))
        return self.db.query("SELECT * FROM camera_detections ORDER BY ts DESC LIMIT ?", (limit,))

    def ack_alert(self, alert_id: int) -> bool:
        self.automation.ack(alert_id)
        if self.db is None:
            return True
        self.db.submit("UPDATE alerts SET acked = 1 WHERE id = ?", (alert_id,))
        return True

    def ack_all_alerts(self) -> int:
        self.automation.ack_all()
        if self.db is None:
            return 0
        return int(self.db.scalar("SELECT COUNT(*) FROM alerts WHERE acked = 0"))


def _j(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), default=str)


def _loads(raw: Any) -> Any:
    import json
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}
