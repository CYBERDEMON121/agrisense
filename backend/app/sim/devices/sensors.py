"""Soil probes and the weather station.

Each probe is an independent device object with its own topic, calibration
offset, battery and sampling jitter, so the dashboard can show realistic
per-device variation instead of one repeated number for every plot.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from ...bus import EventBus
from ..field import FieldModel
from ..weather import WeatherState

#: Volumetric water content -> calibrated EC (dS/m).  Deliberately non-linear.
def _ec_from_theta(theta: float, base_ec: float) -> float:
    return base_ec * (0.55 + 2.4 * theta)


@dataclass(slots=True)
class SoilProbe:
    device_id: str
    plot_id: str
    area_id: str
    name: str = ""
    vendor: str = "Aquacheck"
    model: str = "SM-220"
    fw: str = "2.4.1"
    status: str = "online"
    battery: float = 100.0
    rssi: int = -62
    depth_cm: int = 30

    # calibration / noise
    cal_offset: float = 0.0
    moisture_noise: float = 0.004
    temp_noise: float = 0.25

    # smoothing state
    moisture: float = 0.26
    temp: float = 28.0
    ec: float = 1.0
    transmit_s: float = 0.0

    def state(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "plot_id": self.plot_id, "area": self.area_id,
                "kind": "soil", "name": self.name, "status": self.status,
                "vendor": self.vendor, "model": self.model,
                "battery": round(self.battery, 1), "rssi": self.rssi, "depth_cm": self.depth_cm,
                "moisture": round(self.moisture * 100, 1), "temp": round(self.temp, 1),
                "ec": round(self.ec, 2), "fw": self.fw}


@dataclass(slots=True)
class WeatherStation:
    device_id: str = "ws-main"
    name: str = "Weather Station (roof)"
    area_id: str = "farmhouse"
    vendor: str = "Davis"
    model: str = "Vantage Pro2"
    fw: str = "3.1.0"
    status: str = "online"
    battery: float = 92.0     # solar-charged
    rssi: int = -48

    def state(self, w: WeatherState) -> dict[str, Any]:
        return {
            "device_id": self.device_id, "kind": "weather", "name": self.name,
            "area": self.area_id, "status": self.status, "battery": round(self.battery, 1),
            "rssi": self.rssi, "fw": self.fw, "temp": round(w.temp, 1),
            "temp_min": round(w.temp_min, 1), "temp_max": round(w.temp_max, 1),
            "humidity": round(w.humidity * 100, 1), "dew_point": round(w.dew_point, 1),
            "wind": round(w.wind_speed, 2), "gust": round(w.wind_gust, 2),
            "wind_dir": round(w.wind_dir, 0), "rain": round(w.rainfall, 2),
            "cloud": round(w.cloud, 2), "solar": round(w.solar, 0),
            "pressure": round(w.pressure, 1), "et0": round(w.et0, 2),
            "vpd": round(w.vapour_gap, 2),
        }


class SensorFleet:
    """Publishes soil + weather telemetry onto the bus."""

    #: Soil probes transmit at ~2.9 V battery sleep duty -> ~1 reading / 20 min.
    INTERVAL_SIM_S = 900.0

    def __init__(self, bus: EventBus, farm: FieldModel, rng: random.Random) -> None:
        self.bus = bus
        self.farm = farm
        self.rng = rng
        self.probes: dict[str, SoilProbe] = {}
        self.station = WeatherStation()
        self._since_tx = 0.0
        for plot in farm.plots:
            base_ec = rng.uniform(0.75, 1.45)
            self.probes[plot.sensor_id] = SoilProbe(
                device_id=plot.sensor_id, plot_id=plot.plot_id, area_id=plot.area_id,
                name=f"Probe {plot.plot_id}",
                cal_offset=rng.gauss(0, 0.004), battery=rng.uniform(55, 100),
                rssi=int(rng.uniform(-92, -48)), moisture=plot.theta,
            )
            self.probes[plot.sensor_id].ec = _ec_from_theta(plot.theta, base_ec)

    # ---- registration -------------------------------------------------
    def device_records(self) -> list[dict[str, Any]]:
        out = [{"device_id": p.device_id, "kind": "soil", "name": p.name, "area": p.area_id,
                "vendor": p.vendor, "model": p.model, "fw": p.fw, "status": p.status,
                "battery": round(p.battery, 1), "rssi": p.rssi,
                "meta": {"plot_id": p.plot_id, "depth_cm": p.depth_cm, "interval_s": self.INTERVAL_SIM_S}}
               for p in self.probes.values()]
        s = self.station
        out.append({"device_id": s.device_id, "kind": "weather", "name": s.name, "area": s.area_id,
                    "vendor": s.vendor, "model": s.model, "fw": s.fw, "status": s.status,
                    "battery": round(s.battery, 1), "rssi": s.rssi,
                    "meta": {"interval_s": 60.0}})
        return out

    # ---- simulation ----------------------------------------------------
    def update(self, weather: WeatherState, dt_sim_s: float) -> None:
        self._since_tx += dt_sim_s
        self._publish_weather(weather)
        if self._since_tx >= self.INTERVAL_SIM_S:
            self._since_tx = 0.0
            self._publish_soil()
        self._decay_batteries(dt_sim_s)

    def _publish_weather(self, w: WeatherState) -> None:
        if self.station.status != "online":
            return
        payload = self.station.state(w)
        self.bus.publish(self.bus.topic("weather", self.station.device_id, "telemetry"),
                         {**payload, "value": w.temp, "ts": w.ts}, qos=0)

    def _publish_soil(self) -> None:
        for probe in self.probes.values():
            # A faulted probe holds its last reading rather than reporting a
            # fresh one; "stale" is the state an operator has to notice.
            if probe.status != "online":
                continue
            plot = self.farm.by_id.get(probe.plot_id)
            if plot is None:
                continue
            # First-order lag toward the true plot value, plus noise.
            probe.moisture += (plot.theta - probe.moisture) * 0.35 + self.rng.gauss(0, probe.moisture_noise)
            probe.moisture = max(0.05, min(0.45, probe.moisture + probe.cal_offset))
            probe.temp += (plot.soil_temp - probe.temp) * 0.4 + self.rng.gauss(0, probe.temp_noise)
            base_ec = probe.ec / max(0.55 + 2.4 * max(probe.moisture, 0.02), 1e-6)
            probe.ec = _ec_from_theta(probe.moisture, base_ec) + self.rng.gauss(0, 0.01)
            self.bus.publish(
                self.bus.topic("soil", probe.device_id, "telemetry"),
                {"value": round(probe.moisture * 100, 2), "moisture": round(probe.moisture * 100, 2),
                 "temp": round(probe.temp, 2), "ec": round(probe.ec, 3),
                 "battery": round(probe.battery, 1), "rssi": probe.rssi, "plot": probe.plot_id},
            )

    def _decay_batteries(self, dt_sim_s: float) -> None:
        days = dt_sim_s / 86400.0
        for probe in self.probes.values():
            # 5-year design life -> ~0.0055 %/day.
            probe.battery = max(0.0, probe.battery - 0.0055 * days)
        st = self.station
        st.battery = min(100.0, max(0.0, st.battery - 0.001 * days + 0.02 * dt_sim_s / 3600.0))

    def fleet_health(self) -> dict[str, Any]:
        lows = [p.device_id for p in self.probes.values() if p.battery < 20]
        return {
            "count": len(self.probes) + 1,
            "online": sum(1 for p in self.probes.values() if p.status == "online") + 1,
            "avg_battery": sum(p.battery for p in self.probes.values()) / max(len(self.probes), 1),
            "low_battery": lows,
            "avg_rssi": sum(p.rssi for p in self.probes.values()) / max(len(self.probes), 1),
        }