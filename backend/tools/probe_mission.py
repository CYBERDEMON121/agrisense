#!/usr/bin/env python3
"""Fly one mission headless and print where the spray accounting goes wrong.

Run with no arguments for defaults.  This exists because a green dashboard
means nothing if the numbers behind it are wrong: the operator reads coverage
and dose, and those have to mean what a real agronomist would expect.
"""

from __future__ import annotations

import math
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings                      # noqa: E402
from app.sim.engine import SimulationEngine          # noqa: E402
from app.sim.devices import drone as drone_mod       # noqa: E402

AREA = sys.argv[1] if len(sys.argv) > 1 else "north"
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 240.0


def main() -> int:
    engine = SimulationEngine(Settings(default_speed=SPEED, write_telemetry=False))
    for _ in range(60):                 # let the weather/field settle
        engine.tick()
    targets = engine.field.plots_in_area(AREA)
    w = engine.weather_state
    mission = engine.drone.plan_mission(targets, engine.clock.now(), trigger="probe",
                                        wind_dir=w.wind_dir, wind_speed=w.wind_speed,
                                        dose_l_ha=0.35)
    engine.drone.start(engine.clock.now())

    print(f"{AREA}: {len(targets)} plots, {mission.target_area_ha:.2f} ha, "
          f"{mission.notes['pass_lines']} pass lines, {len(mission.waypoints)} waypoints")
    print(f"wind {w.wind_speed:.1f} m/s @ {w.wind_dir:.0f} deg -> effective swath "
          f"{engine.drone.effective_swath(mission.swath_m, w.wind_speed):.2f} m, "
          f"deposition {engine.drone.deposition_efficiency(w.wind_speed):.2f}")
    need_l = mission.target_area_ha * mission.dose_l_ha
    print(f"label volume needed: {need_l:.2f} L, tank {engine.drone.CAPACITY_L} L\n")

    # Instrument the deposit so we can see who is being sprayed twice.
    per_pass: dict[str, list[float]] = {}
    turning_spray_s = 0.0
    swept_total = 0.0
    sprayed_total = 0.0
    unassigned_total = 0.0
    original = drone_mod.SprayDrone._deposit

    def traced(self, m, sprayed_l, moved_m, wind_speed, ts):
        before = dict(m.coverage)
        original(self, m, sprayed_l, moved_m, wind_speed, ts)
        for pid, after in m.coverage.items():
            per_pass.setdefault(pid, []).append(after - before.get(pid, 0.0))
        nonlocal swept_total, sprayed_total, unassigned_total
        eff = self.deposition_efficiency(wind_speed)
        swath = self.effective_swath(m.swath_m, wind_speed)
        swept_total += moved_m * swath
        sprayed_total += sprayed_l
        unassigned_total += abs(sprayed_l * eff
                                - sum(m.dose_ml_ha.values()) / 1.0e7 * sum(
                                    p.area_m2 for p in self.farm.plots
                                    if p.plot_id in m.dose_ml_ha))
        if abs(((self.heading - math.degrees(math.atan2(0, 1))) % 360)) > 180:
            pass

    drone_mod.SprayDrone._deposit = traced

    ticks = 0
    turning_ticks = 0
    spray_ticks = 0
    while ticks < 400_000:
        engine.tick()
        ticks += 1
        d = engine.drone
        if d.state == "spraying":
            spray_ticks += 1
        if d.mission and d.mission.status in ("done", "partial", "aborted"):
            break
        if d.state == "idle" and d.mission and d.mission.status not in ("done", "partial"):
            break
    drone_mod.SprayDrone._deposit = original

    m = engine.drone.mission or mission
    cov = m.coverage
    dose = m.dose_ml_ha
    print(f"ticks {ticks} ({spray_ticks} spraying), state {engine.drone.state}, "
          f"mission {m.status}, sorties {m.sorties + 1}")
    print(f"waypoints {m.wp_idx}/{len(m.waypoints)}  distance {m.distance_m:.0f} m")
    print(f"sprayed {m.sprayed_litres:.3f} L (need {need_l:.2f} L, "
          f"ratio {m.sprayed_litres / need_l:.2f})")
    print(f"swath area actually swept {swept_total:.0f} m2 = "
          f"{swept_total / 10_000:.2f} ha ({swept_total / 10_000 / mission.target_area_ha:.2f} x plot area)")
    if cov:
        vals = sorted(cov.values())
        print(f"coverage  min {vals[0]:.2f} median {vals[len(vals) // 2]:.2f} max {vals[-1]:.2f}")
    if dose:
        dvals = sorted(dose.values())
        print(f"dose mL/ha min {dvals[0]:.0f} median {dvals[len(dvals) // 2]:.0f} max {dvals[-1]:.0f} "
              f"(label {mission.dose_l_ha * 1000:.0f})")

    # Which plot got sprayed most, and how many separate passes hit it?
    if per_pass:
        worst = max(per_pass, key=lambda p: sum(per_pass[p]))
        chunks = per_pass[worst]
        print(f"\nworst plot {worst}: {len(chunks)} deposit steps, "
              f"total {sum(chunks):.2f}, largest single step {max(chunks):.2f}, "
              f"mean {sum(chunks) / len(chunks):.3f}")
        big = [c for c in chunks if c > 0.05]
        print(f"  steps adding >5% coverage: {len(big)} "
              f"(sum {sum(big):.2f}) - more than a few means double-application")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())