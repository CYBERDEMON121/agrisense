"""Simulation clock helpers.

The simulator runs on a *simulated* wall clock so a full growing season or a
multi-day irrigation cycle can be observed in minutes.  ``SimClock`` is the only
place that knows about time scaling; everything else asks it for ``now()``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

MIN_PER_HOUR = 60.0
HOURS_PER_DAY = 24.0
SECONDS_PER_DAY = 86400.0
#: Fixed offset (not DST) - keeps the model deterministic across runs.
TZ = timezone(timedelta(hours=5.5), name="IST")


def to_local(epoch: float) -> datetime:
    """Convert a unix timestamp into the farm's local time."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(TZ)


def day_of_year(epoch: float) -> int:
    return to_local(epoch).timetuple().tm_yday


def hour_of_day(epoch: float) -> float:
    local = to_local(epoch)
    return local.hour + local.minute / 60.0 + local.second / 3600.0


def daylight_fraction(epoch: float, latitude: float, day: int) -> float:
    """Astronomical sunrise/sunset, used for solar radiation and drone legality."""
    decl = declination(day, latitude)
    lat = math.radians(latitude)
    cos_h = -math.tan(lat) * math.tan(decl)
    if cos_h <= -1:
        return 1.0, 12.0
    if cos_h >= 1:
        return 0.0, 12.0
    half_day = math.degrees(math.acos(cos_h)) / 15.0
    return 2 * half_day, 12.0 - half_day


def declination(day: int, latitude: float) -> float:
    """Cooper's equation for solar declination."""
    return math.radians(23.45) * math.sin(math.radians(360 * (284 + day) / 365.0))


def extraterrestrial_radiation(latitude: float, day: int) -> float:
    """Hargreaves' Ra in mm/day equivalent (MJ m-2 day-1 / 0.408)."""
    lat = math.radians(latitude)
    decl = declination(day, latitude)
    dr = 1 + 0.033 * math.cos(math.radians(360 * day / 365.0))
    cos_w = -math.tan(lat) * math.tan(decl)
    cos_w = max(cos_w, -1.0)
    ws = math.acos(cos_w)
    return (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.sin(ws)
    )


def et0_hargreaves(tmin: float, tmax: float, tmean: float, ra: float) -> float:
    """Reference evapotranspiration (mm/day) - FAO-56 simplified form."""
    return max(0.0, 0.0023 * (tmean + 17.8) * math.sqrt(max(tmax - tmin, 0.0)) * ra)


@dataclass(slots=True)
class SimClock:
    """Maps wall-clock seconds onto simulated seconds via a speed multiplier."""

    start_epoch: float
    speed: float = 1.0
    paused: bool = False
    _wall_ref: float = field(default_factory=time.monotonic)
    _sim_ref: float = 0.0

    def now(self) -> float:
        if self.paused:
            return self._sim_ref
        elapsed = time.monotonic() - self._wall_ref
        self._sim_ref = self.start_epoch + elapsed * self.speed
        return self._sim_ref

    def set_speed(self, speed: float) -> None:
        self.now()
        self._wall_ref = time.monotonic()
        self.speed = max(0.0, min(speed, 3600.0))

    def set_paused(self, paused: bool) -> None:
        self.now()
        self._wall_ref = time.monotonic()
        self.paused = paused

    def reset(self, start_epoch: float | None = None) -> None:
        self.now()
        self._wall_ref = time.monotonic()
        if start_epoch is not None:
            self.start_epoch = start_epoch
        self._sim_ref = self.now()

    def advance(self, sim_seconds: float) -> float:
        """Push the simulated clock forward without waiting on the wall clock.

        The live server derives time from ``time.monotonic()``, which makes any
        test that wants to fly a whole mission in milliseconds impossible.  This
        advances ``_sim_ref`` and re-anchors the wall reference so the next
        ``now()`` continues from exactly where this left off.
        """
        self.now()
        self._sim_ref += max(0.0, sim_seconds)
        self._wall_ref = time.monotonic() - (self._sim_ref - self.start_epoch) / max(self.speed, 1e-9)
        return self._sim_ref

    @property
    def sim_seconds_per_wall_second(self) -> float:
        return 0.0 if self.paused else self.speed

    def local(self) -> datetime:
        return to_local(self.now())