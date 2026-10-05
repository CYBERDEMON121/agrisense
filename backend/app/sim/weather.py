"""Synthetic weather: monsoon-aware synoptic state plus a diurnal cycle.

The model is a two-layer generator:

* a *synoptic* layer - a random walk on daily mean temperature, a Markov chain on
  cloud cover / rain state, and an Ornstein-Uhlenbeck process on wind speed that
  gives realistic gusting.
* a *diurnal* layer - temperature follows a cosine peaking at 14:00, humidity is
  inversely tied to temperature with a lag, and solar radiation comes from FAO-56
  clear-sky equations scaled by sunshine duration.

That is enough structure for the automation engine to face genuine trade-offs:
strong sun and low wind (good for spraying) usually arrive together, and rain
suppresses both spraying and irrigation demand at once.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from ..clock import (HOURS_PER_DAY, day_of_year, declination, et0_hargreaves,
                     extraterrestrial_radiation, hour_of_day)


def _ou_step(value: float, mean: float, theta: float, sigma: float, dt: float, rng: random.Random) -> float:
    """Ornstein-Uhlenbeck step, integrated exactly over dt."""
    variance_factor = math.sqrt(max(1.0 - math.exp(-2 * theta * dt), 1e-9))
    return mean + (value - mean) * math.exp(-theta * dt) + sigma * variance_factor * rng.gauss(0, 1)


@dataclass(slots=True)
class WeatherState:
    ts: float
    temp: float = 30.0
    temp_min: float = 24.0
    temp_max: float = 36.0
    humidity: float = 0.55
    dew_point: float = 20.0
    wind_speed: float = 2.0
    wind_gust: float = 2.5
    wind_dir: float = 90.0
    cloud: float = 0.35
    rainfall: float = 0.0        # mm in the current hour
    rain_total: float = 0.0      # mm cumulative in the last hour
    solar: float = 400.0         # W/m2
    et0: float = 5.0             # mm/day reference ET
    pressure: float = 1010.0
    vapour_gap: float = 1.5      # kPa - drives irrigation need
    last_rain_ts: float | None = None

    @property
    def is_raining(self) -> bool:
        return self.rainfall > 0.05

    @property
    def temperature_mean(self) -> float:
        return self.temp


class WeatherModel:
    """Generates a physically-plausible weather series for any latitude."""

    #: Fraction of the year that is monsoon-ish, centred on day 200 (mid-July).
    MONSOON_CENTRE = 200.0
    MONSOON_WIDTH = 55.0

    def __init__(self, latitude: float, rng: random.Random, start_ts: float) -> None:
        self.lat = latitude
        self.rng = rng
        self.day = day_of_year(start_ts)
        self.tmean_day = self._climatology_mean(self.day)
        self.tamp_day = 8.5
        self.cloud = 0.4
        self.wind = 2.4
        self.wind_dir = rng.uniform(30, 150)
        self.pressure = 1010.0
        self.rain_mm_hour = 0.0
        self._daily_rain_mm = 0.0
        self._rain_remaining = 0.0
        self.last_rain_ts: float | None = None
        #: Operator override applied on top of the generated weather, for a
        #: bounded number of simulated hours.  Empty when nothing is forced.
        self.override: dict[str, float] = {}
        self.override_until_ts: float | None = None
        self.state = WeatherState(ts=start_ts)
        self._roll_day(start_ts)

    # ---- climatology --------------------------------------------------
    def _climatology_mean(self, day: int) -> float:
        """Smooth seasonal temperature curve for the latitude."""
        seasonal = 3.5 * math.cos(2 * math.pi * (day - 20) / HOURS_PER_DAY * 15)  # mid-Jan warm
        latitude_penalty = max(0.0, (abs(self.lat) - 12.0) * 0.25)
        return 28.0 + seasonal - latitude_penalty

    def _monsoon_weight(self) -> float:
        delta = (self.day - self.MONSOON_CENTRE) / self.MONSOON_WIDTH
        return math.exp(-delta * delta)

    def _roll_day(self, ts: float) -> None:
        """Advance synoptic state for a new day."""
        self.day = day_of_year(ts)
        target = self._climatology_mean(self.day)
        # Persistent but wandering daily mean.
        self.tmean_day += 0.45 * (target - self.tmean_day) + self.rng.gauss(0, 0.9)
        self.tmean_day = max(14.0, min(41.0, self.tmean_day))
        self.tamp_day = max(4.0, min(13.0, self.tamp_day + self.rng.gauss(0, 0.7)))

        monsoon = self._monsoon_weight()
        cloudy_target = 0.25 + 0.55 * monsoon + self.rng.gauss(0, 0.12)
        self.cloud = max(0.02, min(0.98, 0.6 * self.cloud + 0.4 * cloudy_target))

        # Rain occurrence: daily wet-day probability from cloud cover + monsoon.
        wet_day_prob = min(0.9, 0.08 + 0.85 * monsoon * max(0.0, self.cloud - 0.4))
        if self.rng.random() < wet_day_prob:
            depth = min(120.0, self.rng.lognormvariate(1.6, 0.9))
        else:
            depth = 0.0
        self._daily_rain_mm = depth
        self._rain_remaining = depth

    # ---- continuous update --------------------------------------------
    def update(self, ts: float, dt_hours: float) -> WeatherState:
        if day_of_year(ts) != day_of_year(ts - dt_hours * 3600):
            self._roll_day(ts)

        hod = hour_of_day(ts)
        n, sunrise, sunset = self._daylight(ts)
        is_day = sunrise <= hod <= sunset and n > 0

        # -- temperature: cosine around 14:00, warmed by residual cloud
        phase = 2 * math.pi * (hod - 14.0) / 24.0
        diurnal = (self.tamp_day / 2) * math.cos(phase)
        solar_gain = 2.6 * (1 - 0.45 * self.cloud) if is_day else 0.0
        temp = self.tmean_day + diurnal + solar_gain
        temp += _ou_step(0.0, 0.0, 0.6, 0.25, dt_hours, self.rng)  # sensor-scale noise

        # -- humidity: inverse of temperature, floored by monsoon humidity
        monsoon = self._monsoon_weight()
        rh = 0.72 - 0.42 * (temp - self.tmean_day) / 6.0
        rh = 0.78 * rh + 0.22 * (0.55 + 0.40 * monsoon)
        rh += _ou_step(0.0, 0.0, 1.2, 0.02, dt_hours, self.rng)
        rh = max(0.15, min(1.0, rh))

        # -- wind: OU with diurnal amplification (afternoon mixing)
        wind_target = (1.8 + 3.4 * monsoon + 1.6 * max(0.0, (temp - self.tmean_day) / 4.0)
                       + 1.2 * self.cloud)
        self.wind = _ou_step(self.wind, wind_target, 0.35, 1.1, dt_hours, self.rng)
        self.wind = max(0.2, min(14.0, self.wind))
        gust = self.wind * (1.25 + 0.35 * self.rng.random()) + 0.4
        self.wind_dir = (self.wind_dir + self.rng.gauss(0, 6.0)) % 360.0

        # -- cloud drifts within the day
        self.cloud = max(0.02, min(0.99, self.cloud + self.rng.gauss(0, 0.02 * dt_hours)))

        # -- rain: deliver the daily total over the wettest hours
        self.rain_mm_hour = 0.0
        if self._rain_remaining > 0.01:
            wet_window = 1.0 if (2 <= hod <= 20 or monsoon > 0.4) else 0.35
            share = dt_hours * (0.5 + 1.5 * self.cloud) * wet_window
            self.rain_mm_hour = min(self._rain_remaining, share * self.rng.random() * 2.4)
            self._rain_remaining = max(0.0, self._rain_remaining - self.rain_mm_hour)
            if self.rain_mm_hour > 0.05:
                self.last_rain_ts = ts

        # -- radiation & ET0
        # Hargreaves/FAO-56: Rs is a DAILY total in MJ/m2/day, then converted to
        # an instantaneous W/m2 irradiance by spreading it over daylight hours
        # with a half-sine shape, because devices report irradiance in W/m2.
        sunshine = n * (1.0 - 0.78 * self.cloud)
        ra = extraterrestrial_radiation(self.lat, self.day)
        if n > 0:
            rs_daily = (0.25 + 0.75 * math.sqrt(max(sunshine, 0.0) / n) * (1 - 0.7 * self.cloud)) * ra
        else:
            rs_daily = 0.0
        rs_daily = max(0.0, min(ra * 1.05, rs_daily))
        mean_irradiance = rs_daily * 1e6 / max(n * 3600.0, 1.0)
        if n > 0 and sunset > sunrise:
            frac = (hod - sunrise) / (sunset - sunrise)
            shape = math.sin(math.pi * frac) if 0.0 <= frac <= 1.0 else 0.0
        else:
            shape = 0.0
        rs_irradiance = max(0.0, mean_irradiance * shape)
        self.pressure = 1010.0 + 12.0 * math.sin(2 * math.pi * (self.day - 100) / 90.0) - 6.0 * self.cloud

        tmax, tmin = temp + self.tamp_day / 2, temp - self.tamp_day / 2
        et0 = et0_hargreaves(tmin, tmax, temp, ra) * (1.0 - 0.25 * self.cloud)
        et0 = max(0.0, min(12.0, et0))

        dew = temp - (100.0 - rh * 100.0) / 5.0
        vpd = 0.6108 * math.exp(17.27 * temp / (temp + 237.3)) * (1.0 - rh)
        self.rs_daily = rs_daily
        self.daylight_hours = n

        self.state = WeatherState(
            ts=ts, temp=temp, temp_min=tmin, temp_max=tmax, humidity=rh, dew_point=dew,
            wind_speed=self.wind, wind_gust=gust, wind_dir=self.wind_dir, cloud=self.cloud,
            rainfall=self.rain_mm_hour, solar=rs_irradiance, et0=et0, pressure=self.pressure,
            vapour_gap=max(0.0, vpd), last_rain_ts=self.last_rain_ts,
        )
        self._apply_override(ts)
        return self.state

    # ---- operator forcing ----------------------------------------------
    #: Fields an operator may pin.  Values replace the generated series rather
    #: than biasing it: pressing "rain burst" should mean 8 mm/h *now*, not
    #: "a bit wetter than the model felt like".
    OVERRIDES = ("rain_mm_h", "wind_ms", "temp_c", "cloud")

    def force(self, values: dict[str, float], hours: float, ts: float) -> dict[str, float]:
        """Pin weather fields for ``hours`` simulated hours from ``ts``."""
        pinned = {k: float(v) for k, v in values.items()
                  if k in self.OVERRIDES and v is not None}
        self.override = pinned
        self.override_until_ts = ts + max(0.0, float(hours)) * HOURS_PER_DAY * 3600.0
        return dict(self.override)

    def release(self) -> None:
        """Hand control back to the generator."""
        self.override = {}
        self.override_until_ts = None

    def forcing(self, ts: float | None = None) -> dict[str, Any]:
        """Describe the active override, for the API and the simulator console."""
        if not self.override:
            return {}
        remaining = None
        if self.override_until_ts is not None and ts is not None:
            remaining = round(max(0.0, (self.override_until_ts - ts) / 3600.0), 2)
        return {**self.override, "hours_left": remaining}

    def _apply_override(self, ts: float) -> None:
        if not self.override:
            return
        if self.override_until_ts is not None and ts >= self.override_until_ts:
            self.release()
            return
        s = self.state
        if "cloud" in self.override:
            s.cloud = max(0.0, min(1.0, self.override["cloud"]))
            # Cloud and radiation are physically linked; forcing one without the
            # other leaves the model contradicting itself (full overcast at
            # 900 W/m2), so the derived quantities follow.
            s.solar *= max(0.0, 1.0 - 0.8 * s.cloud)
            s.et0 *= max(0.0, 1.0 - 0.6 * s.cloud)
        if "temp_c" in self.override:
            shift = self.override["temp_c"] - s.temp
            s.temp += shift
            s.temp_min += shift
            s.temp_max += shift
        if "wind_ms" in self.override:
            s.wind_speed = max(0.0, self.override["wind_ms"])
            s.wind_gust = s.wind_speed * 1.35 + 0.3
        if "rain_mm_h" in self.override:
            s.rainfall = max(0.0, self.override["rain_mm_h"])
            if s.rainfall > 0.05:
                self.last_rain_ts = ts
                s.last_rain_ts = ts

    def _daylight(self, ts: float) -> tuple[float, float, float]:
        lat = math.radians(self.lat)
        decl = declination(self.day, self.lat)
        cos_h = -math.tan(lat) * math.tan(decl)
        if cos_h <= -1:
            return 0.0, 12.0, 12.0
        if cos_h >= 1:
            return 0.0, 12.0, 12.0
        h = math.degrees(math.acos(cos_h))
        return (2 * h / 15.0, 12.0 - h / 15.0, 12.0 + h / 15.0)

    @property
    def sunrise(self) -> float:
        return self._daylight(self.state.ts)[1]

    @property
    def sunset(self) -> float:
        return self._daylight(self.state.ts)[2]