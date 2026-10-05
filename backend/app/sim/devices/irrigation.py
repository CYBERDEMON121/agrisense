"""Irrigation system: pump, storage tank, borewell and zone solenoids.

Hydraulics are modelled rather than faked:

* the pump has a rated flow and head, and delivered flow falls off as the
  number of simultaneously open zones rises (a real curve: shared pump, real
  pipe losses);
* delivered volume is debited from the tank, and if the tank empties the pump
  switches to the borewell at its lower yield;
* water that reaches a plot is credited through the field model, discounted by
  the zone's distribution uniformity, so poor coverage shows up as uneven soil
  moisture instead of being invisible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable

from ...bus import EventBus
from ..field import FieldModel, Plot


@dataclass(slots=True)
class Zone:
    zone_id: str
    name: str
    plots: list[str]
    valve_id: str
    valve_open: bool = False
    target_mm: float = 0.0
    applied_mm: float = 0.0
    started_ts: float | None = None
    uniformity: float = 0.92     # distribution uniformity of the emitter layout
    valve_state: float = 0.0    # 0..1, ramps with solenoid travel time
    valve_pct: float = 0.0
    #: Who opened this cycle: "auto" (the closed loop) or a manual trigger.
    #: The automation stands down for manual cycles - see AutomationEngine.
    trigger: str = ""

    def state(self, ts: float | None = None) -> dict[str, Any]:
        elapsed = 0.0
        if self.started_ts is not None and ts is not None:
            elapsed = max(0.0, (ts - self.started_ts) / 60.0)
        return {"id": self.zone_id, "name": self.name, "plots": self.plots,
                "valve": self.valve_id, "open": self.valve_open,
                "valve_pct": round(self.valve_pct, 1),
                "target_mm": round(self.target_mm, 1), "applied_mm": round(self.applied_mm, 1),
                "uniformity": round(self.uniformity, 2),
                "trigger": self.trigger,
                "minutes": round(elapsed, 1)}


class IrrigationSystem:
    """Pump + tank + borewell feeding the field's irrigation zones."""

    #: Pump nameplate: 90 m3/h at rated head.
    PUMP_RATED_LPM = 1500.0
    #: Head loss: each extra open zone costs ~12% of rated flow.
    FLOW_LOSS_PER_ZONE = 0.12
    #: Borewell recharge rate when the tank runs dry (litre/min) - roughly a
    #: 15 HP submersible set.
    BOREWELL_LPM = 900.0
    #: Per-zone flow cap (each zone is metered separately).
    ZONE_MAX_LPM = 620.0
    #: Solenoid travel time (seconds of real time).
    VALVE_TRAVEL_S = 1.5
    #: Application efficiency: fraction of pumped water that reaches the root zone.
    APPLICATION_EFFICIENCY = 0.88

    def __init__(self, bus: EventBus, farm: FieldModel, rng: random.Random,
                 start_ts: float, tank_litres: float = 600_000.0) -> None:
        self.bus = bus
        self.farm = farm
        self.rng = rng
        self.tank_l: float = tank_litres
        self.tank_capacity_l: float = 800_000.0
        self.tank_level_pct: float = 100.0 * tank_litres / self.tank_capacity_l
        self.pump_on = False
        self.source = "tank"          # tank | borewell
        self.pressure_bar: float = 0.0
        self.flow_lpm: float = 0.0
        self.total_flow_lpm: float = 0.0
        self.cumulative_litres: float = 0.0
        self.daily_litres: float = 0.0
        self.today_key: str = ""
        self.borewell_running = False
        self.energy_kwh: float = 0.0
        self.zones: dict[str, Zone] = {}
        self._valve_accum: dict[str, float] = {}
        for zone_id, plot_ids in farm.zones.items():
            self.zones[zone_id] = Zone(
                zone_id=zone_id,
                name=f"Zone {zone_id[-1].upper()}",
                plots=list(plot_ids),
                valve_id=f"sv-{zone_id}",
                uniformity=rng.uniform(0.82, 0.95),
            )
            self._valve_accum[zone_id] = 0.0

    # ---- device records -------------------------------------------------
    def device_records(self) -> list[dict[str, Any]]:
        recs = [
            {"device_id": "pump-01", "kind": "pump", "name": "Main irrigation pump",
             "area": "pumphouse", "vendor": "Grundfos", "model": "NB 65-200",
             "fw": "1.8.2", "status": "online", "battery": None, "rssi": -55,
             "meta": {"rated_lpm": self.PUMP_RATED_LPM, "rated_head_m": 42.0}},
            {"device_id": "tank-01", "kind": "tank", "name": "Farm water tank",
             "area": "pumphouse", "vendor": "Local", "model": "200 m3 GRP",
             "fw": "1.0.0", "status": "online", "battery": None, "rssi": -55,
             "meta": {"capacity_l": self.tank_capacity_l}},
            {"device_id": "borewell-01", "kind": "pump", "name": "Borewell submersible",
             "area": "wellfield", "vendor": "Kirloskar", "model": "4HS 12-30",
             "fw": "1.1.0", "status": "online", "battery": None, "rssi": -62,
             "meta": {"yield_lpm": self.BOREWELL_LPM}},
        ]
        for z in self.zones.values():
            recs.append({"device_id": z.valve_id, "kind": "valve", "name": f"{z.name} solenoid",
                         "area": "pumphouse", "vendor": "Hunter", "model": "DV-24",
                         "fw": "2.0.1", "status": "online", "battery": None, "rssi": -58,
                         "meta": {"zone": z.zone_id, "plots": len(z.plots)}})
        return recs

    # ---- commands (called by the automation engine) ----------------------
    def open_zone(self, zone_id: str, target_mm: float, ts: float, trigger: str = "manual") -> Zone | None:
        z = self.zones.get(zone_id)
        if z is None or z.valve_open:
            return z
        if self.tank_l < 500 and not self.borewell_running:
            # Nothing to deliver yet; the borewell rule will fill the tank.
            return None
        z.valve_open = True
        z.target_mm = target_mm
        z.applied_mm = 0.0
        z.started_ts = ts
        z.valve_state = 0.0
        z.trigger = trigger
        self._publish(z, "valve_open", ts, trigger=trigger)
        self.bus.publish(self.bus.topic("irrigation", z.valve_id, "state"),
                         {"value": 1.0, "state": "open", "zone": zone_id, "target_mm": target_mm,
                          "plots": len(z.plots), "ts": ts})
        return z

    def close_zone(self, zone_id: str, ts: float, trigger: str = "manual") -> Zone | None:
        z = self.zones.get(zone_id)
        if z is None or not z.valve_open:
            return z
        z.valve_open = False
        z.trigger = ""
        self._publish(z, "valve_close", ts, trigger=trigger,
                      applied=round(z.applied_mm, 1), minutes=round(self._duration_min(z), 1))
        self.bus.publish(self.bus.topic("irrigation", z.valve_id, "state"),
                         {"value": 0.0, "state": "closed", "zone": zone_id,
                          "applied_mm": round(z.applied_mm, 1), "ts": ts})
        return z

    def _publish(self, z: Zone, action: str, ts: float, trigger: str, **extra: Any) -> None:
        payload = {"zone": zone_id_str(z), "action": action, "trigger": trigger,
                   "flow_lpm": round(self.total_flow_lpm, 1), "ts": ts, **extra}
        self.bus.publish(self.bus.topic("irrigation", "system", "event"), payload)
        self.bus.publish(self.bus.topic("irrigation", "system", "telemetry"),
                         {"value": self.total_flow_lpm, "tank_pct": round(self.tank_level_pct, 1),
                          "pressure": round(self.pressure_bar, 2), "ts": ts})

    # ---- simulation ------------------------------------------------------
    def step(self, ts: float, dt_sim_s: float, dt_real_s: float) -> None:
        self._roll_day(ts)
        # Solenoid travel: valves ramp open/closed over a couple of seconds.
        open_count = 0
        for zid, z in self.zones.items():
            target = 1.0 if z.valve_open else 0.0
            rate = dt_real_s / self.VALVE_TRAVEL_S
            if z.valve_state < target:
                z.valve_state = min(target, z.valve_state + rate)
            elif z.valve_state > target:
                z.valve_state = max(target, z.valve_state - rate)
            z.valve_pct = z.valve_state * 100.0
            if z.valve_pct > 5:
                open_count += 1

        # Pump output falls as more zones share the same pump.
        demand = sum(1 for z in self.zones.values() if z.valve_pct > 5)
        capacity = self.PUMP_RATED_LPM * (1.0 - self.FLOW_LOSS_PER_ZONE * max(0, demand - 1))
        capacity = max(capacity, 0.0)

        tank_ok = self.tank_l > 200.0
        source = "tank"
        if not tank_ok:
            source = "borewell"
        self.source = source
        available = capacity if tank_ok else min(capacity, self.BOREWELL_LPM)
        self.borewell_running = not tank_ok and demand > 0

        # Split available flow across open zones, capped per zone.
        active = [z for z in self.zones.values() if z.valve_pct > 5]
        self.flow_lpm = 0.0
        if active:
            share = min(self.ZONE_MAX_LPM, available / max(len(active), 1))
            share *= sum(z.valve_pct for z in active) / (100.0 * len(active))
            self.flow_lpm = max(0.0, share * len(active))
        self.total_flow_lpm = self.flow_lpm
        self.pump_on = self.flow_lpm > 1.0
        # Pressure drops with demand; a dry tank starves the system.
        self.pressure_bar = round(
            min(6.0, (self.flow_lpm / self.PUMP_RATED_LPM) * 5.2 * (1.0 if tank_ok else 0.55)), 2)

        # ---- deliver water ----
        delivered_l = self.flow_lpm * (dt_sim_s / 60.0)
        if tank_ok:
            self.tank_l = max(0.0, self.tank_l - delivered_l)
        else:
            # Borewell output is capped at its yield; the rest is a shortfall.
            delivered_l = min(delivered_l, self.BOREWELL_LPM * (dt_sim_s / 60.0))
        if delivered_l > 0 and active:
            self._distribute(active, delivered_l, dt_sim_s, ts)

        self.tank_level_pct = 100.0 * self.tank_l / self.tank_capacity_l
        self.cumulative_litres += delivered_l
        self.daily_litres += delivered_l
        # 7.5 kW pump at nominal duty -> energy from delivered volume.
        self.energy_kwh += delivered_l / 1000.0 * 0.75

        # Borewell refills the tank whenever it is not drawing.
        if not self.borewell_running and self.tank_l < self.tank_capacity_l:
            recharge = self.BOREWELL_LPM * (dt_sim_s / 60.0) * (0.9 if self.pump_on else 1.0)
            self.tank_l = min(self.tank_capacity_l, self.tank_l + recharge)

    def _distribute(self, active: list[Zone], delivered_l: float, dt_sim_s: float, ts: float) -> None:
        """Split delivered litres across open zones and into their plots."""
        for z in active:
            z_l = delivered_l * (z.valve_pct / 100.0) / max(len(active), 1)
            area_m2 = sum(self.farm.by_id[p].area_m2 for p in z.plots if p in self.farm.by_id)
            if area_m2 <= 0:
                continue
            # 1 mm of depth over 1 m2 is exactly 1 litre, so litres / m2 == mm.
            mm = z_l / area_m2
            effective = mm * self.APPLICATION_EFFICIENCY * z.uniformity
            z.applied_mm += mm * self.APPLICATION_EFFICIENCY
            for plot_id in z.plots:
                plot = self.farm.by_id.get(plot_id)
                if plot is None:
                    continue
                # Per-plot emitters are not perfectly uniform.
                jitter = 1.0 + self.rng.gauss(0, 0.06)
                self.farm.queue_irrigation(plot_id, max(0.0, effective * jitter))
                plot.irrigated_ts = ts
            if z.applied_mm >= z.target_mm > 0:
                self.close_zone(z.zone_id, ts, trigger="target_reached")
                self.bus.publish(self.bus.topic("irrigation", "system", "event"), {
                    "zone": z.zone_id, "action": "cycle_complete",
                    "applied_mm": round(z.applied_mm, 1), "target_mm": round(z.target_mm, 1),
                    "minutes": round(self._duration_min(z), 1), "ts": ts,
                })

    def _duration_min(self, z: Zone) -> float:
        area_ha = sum(self.farm.by_id[p].area_ha for p in z.plots if p in self.farm.by_id)
        if self.flow_lpm <= 0 or area_ha <= 0:
            return 0.0
        litres_needed = area_ha * 10_000.0 * 25.0     # a nominal 25 mm application
        return litres_needed / self.flow_lpm

    def _roll_day(self, ts: float) -> None:
        from ...clock import to_local
        key = to_local(ts).strftime("%Y-%m-%d")
        if key != self.today_key:
            self.today_key = key
            self.daily_litres = 0.0

    # ---- state ------------------------------------------------------------
    def state(self, ts: float | None = None) -> dict[str, Any]:
        return {
            "pump": {"on": self.pump_on, "flow_lpm": round(self.flow_lpm, 1),
                     "pressure_bar": self.pressure_bar, "source": self.source},
            "tank": {"litres": round(self.tank_l, 1), "capacity_l": self.tank_capacity_l,
                     "level_pct": round(self.tank_level_pct, 1)},
            "borewell": {"running": self.borewell_running, "yield_lpm": self.BOREWELL_LPM},
            "daily_litres": round(self.daily_litres, 1),
            "cumulative_litres": round(self.cumulative_litres, 1),
            "energy_kwh": round(self.energy_kwh, 2),
            "zones": [z.state(ts) for z in self.zones.values()],
        }

    def open_zones(self) -> list[Zone]:
        return [z for z in self.zones.values() if z.valve_open]


def zone_id_str(z: Zone) -> str:
    return z.zone_id