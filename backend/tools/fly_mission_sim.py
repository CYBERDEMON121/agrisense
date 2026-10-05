#!/usr/bin/env python3
"""Fly a mission deterministically and report the spray accounting.

The live server derives simulated time from the wall clock, so a test cannot fly
a whole mission in milliseconds.  This drives ``SimClock.advance`` directly and
asserts the invariants an agronomist would check:

* spray volume reconciles against the planned air volume (label rate inflated by
  the drift the airframe cannot avoid) - no double-application, no lost volume,
* coverage lands near 1.0 rather than exceeding it,
* delivered dose brackets the label rate,
* the mission terminates instead of looping sorties forever.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings                              # noqa: E402
from app.sim.engine import SimulationEngine                  # noqa: E402

#: Simulated seconds per tick.  The drone flies at 5 m/s over a ~300 m pass, so
#: a 10 s step resolves a pass line into a few dozen deposits.
DT = 10.0
MAX_TICKS = 40_000


def run(area: str, dose: float, wind: float | None, max_ticks: int) -> tuple[dict, dict, int, int]:
    settings = Settings(write_telemetry=False, default_speed=1.0)
    engine = SimulationEngine(settings)

    # Warm up to a settled weather/field state before planning.
    for _ in range(30):
        engine.clock.advance(DT)
        engine.tick()

    if wind is not None:
        engine.weather.wind = wind

    targets = engine.field.plots_in_area(area)
    if not targets:
        raise SystemExit(f"no plots in area {area!r}")
    w = engine.weather_state
    mission = engine.drone.plan_mission(
        targets, engine.clock.now(), trigger="test",
        wind_dir=w.wind_dir, wind_speed=w.wind_speed, dose_l_ha=dose)
    if not engine.drone.start(engine.clock.now()):
        raise SystemExit(f"drone refused to launch from {engine.drone.state}")

    sorties = 0
    ticks = 0
    for ticks in range(1, max_ticks + 1):
        engine.clock.advance(DT)
        engine.tick()
        m = engine.drone.mission
        if engine.drone.sorties > sorties:
            sorties = engine.drone.sorties
        if engine.drone.state == "idle" and (m is None or m.status in ("done", "partial", "aborted")):
            break
        if m is not None and m.status in ("done", "partial", "aborted") and engine.drone.state == "idle":
            break
    return mission.as_dict(), engine.drone.snapshot(), ticks, sorties


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default="north")
    ap.add_argument("--dose", type=float, default=0.35, help="L/ha")
    ap.add_argument("--wind", type=float, default=None, help="force wind speed m/s")
    ap.add_argument("--max-ticks", type=int, default=MAX_TICKS)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    mission, drone, ticks, sorties = run(args.area, args.dose, args.wind, args.max_ticks)

    cov = mission["coverage"] or {}
    dose = mission["dose_ml_ha"] or {}
    label = args.dose * 1000.0
    cov_vals = sorted(cov.values())
    dose_vals = sorted(dose.values())
    mean_cov = sum(cov_vals) / len(cov_vals) if cov_vals else 0.0
    mean_dose = sum(dose_vals) / len(dose_vals) if dose_vals else 0.0
    # The tank has to carry the label volume *plus* the drift the airframe cannot
    # avoid, so reconciling tank volume against the label volume alone would fail
    # every windy flight.  `expected_air_l` is what the plan says it should cost.
    need = mission["notes"].get("expected_air_l") or (mission["area_ha"] * args.dose)
    volume_ratio = mission["sprayed_litres"] / need if need else 0.0

    if not args.quiet:
        print(f"area={args.area} plots={mission['n_plots']} ha={mission['area_ha']} "
              f"status={mission['status']} sorties={sorties + 1} ticks={ticks}")
        print(f"  spray   {mission['sprayed_litres']:.2f} L of {need:.2f} L planned air "
              f"({volume_ratio:.2f}x, label {mission['area_ha'] * args.dose:.2f} L "
              f"+ drift at {mission['notes'].get('deposition', 0):.0%} deposition)")
        if cov_vals:
            print(f"  cover   min {cov_vals[0]:.2f} mean {mean_cov:.2f} max {cov_vals[-1]:.2f}")
        if dose_vals:
            print(f"  dose    min {dose_vals[0]:.0f} mean {mean_dose:.0f} max {dose_vals[-1]:.0f} "
                  f"mL/ha (label {label:.0f})")

    problems: list[str] = []
    if mission["status"] not in ("done", "partial"):
        problems.append(f"mission ended {mission['status']!r} instead of done/partial")
    if not cov:
        problems.append("no coverage recorded")
    if cov_vals and max(cov_vals) > 1.02:
        problems.append(f"coverage {max(cov_vals):.2f} exceeds 1.0 - plots sprayed twice")
    if cov_vals and min(cov_vals) < 0.90:
        worst = min(cov, key=lambda k: cov[k])
        problems.append(f"plot {worst} only {cov[worst]:.2f} covered")
    if not (0.9 <= volume_ratio <= 1.25):
        problems.append(f"sprayed {volume_ratio:.2f}x the planned air volume")
    if not dose:
        problems.append("no dose recorded")
    elif not (0.7 * label <= mean_dose <= 1.3 * label):
        problems.append(f"mean dose {mean_dose:.0f} mL/ha is far off the {label:.0f} label rate")
    if drone["state"] not in ("idle", "servicing"):
        problems.append(f"drone stuck in {drone['state']!r}")

    if problems:
        for p in problems:
            print(f"  FAIL {p}")
        return 1
    print("  ok  coverage, dose, volume and termination all within tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())