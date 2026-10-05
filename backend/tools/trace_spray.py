#!/usr/bin/env python3
"""Attribute every m2 of swept rectangle to plots, to find the double-count.

Run ``tools/fly_mission_sim.py`` first: if coverage exceeds 1.0 the drone is
treating ground it has already treated.  This dumps the per-step detail for one
plot so the arithmetic can be checked by hand.
"""

from __future__ import annotations

import argparse
import collections
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings                       # noqa: E402
from app.sim.engine import SimulationEngine           # noqa: E402
from app.sim.devices.drone import SprayDrone         # noqa: E402

DT = 10.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", default="north")
    ap.add_argument("--plot", default=None, help="plot id to trace (default: worst)")
    ap.add_argument("--dose", type=float, default=0.35)
    args = ap.parse_args()

    engine = SimulationEngine(Settings(write_telemetry=False, default_speed=1.0))
    for _ in range(30):
        engine.clock.advance(DT)
        engine.tick()

    targets = engine.field.plots_in_area(args.area)
    w = engine.weather_state
    mission = engine.drone.plan_mission(targets, engine.clock.now(), trigger="trace",
                                        wind_dir=w.wind_dir, wind_speed=w.wind_speed,
                                        dose_l_ha=args.dose)
    swath = engine.drone.effective_swath(mission.swath_m, w.wind_speed)
    print(f"effective swath {swath:.2f} m, pass lines {mission.notes['pass_lines']}, "
          f"bbox {mission.notes['bbox']}, wind_dir {w.wind_dir:.0f} "
          f"wind {w.wind_speed:.1f} m/s")
    n_lines = mission.notes["pass_lines"]
    if n_lines > 1:
        x0, y0, x1, y1 = mission.notes["bbox"]
        # pass lines run perpendicular to the wind; spacing across them is what
        # the planner computed as (extent / n_lines)
        across = max(x1 - x0, y1 - y0)
        print(f"  nominal spacing {across / n_lines:.2f} m across {n_lines} lines "
              f"(field extent {across:.0f} m)")

    plot_id = args.plot
    trace: list[tuple] = []
    swept_total = 0.0
    per_plot_area = collections.defaultdict(float)
    original = SprayDrone._deposit

    class _WpProbe:
        """Expose the waypoint the autopilot is flying, for line attribution."""

    def traced(self, m, sprayed_l, fx, fy, tx, ty, wind_speed, ts):
        self._last_wp_idx = m.waypoints[m.wp_idx].idx if m.wp_idx < len(m.waypoints) else -1
        nonlocal swept_total
        eff_swath = self.effective_swath(m.swath_m, wind_speed)
        seg = math.hypot(tx - fx, ty - fy)
        swept_total += seg * eff_swath
        before = dict(m.coverage)
        original(self, m, sprayed_l, fx, fy, tx, ty, wind_speed, ts)
        for pid, plot in ((p.plot_id, p) for p in self.farm.plots):
            delta = m.coverage.get(pid, 0.0) - before.get(pid, 0.0)
            if delta > 0:
                per_plot_area[pid] += delta * plot.area_m2
                if pid == plot_id:
                    trace.append((m.wp_idx, seg, eff_swath,
                                  delta * plot.area_m2, plot.area_m2))

    SprayDrone._deposit = traced
    engine.drone.start(engine.clock.now())
    ticks = 0
    for ticks in range(1, 20_000):
        engine.clock.advance(DT)
        engine.tick()
        m = engine.drone.mission
        if engine.drone.state == "idle" and m and m.status in ("done", "partial"):
            break
    SprayDrone._deposit = original

    # The drone clears ``self.mission`` once it lands, so keep our reference.
    m = mission
    total_plot_area = sum(p.area_m2 for p in targets)
    print(f"\nmission {m.status} after {ticks} ticks ({ticks * DT / 3600:.2f} sim h), "
          f"{m.sorties + 1} sorties, {m.wp_idx}/{len(m.waypoints)} waypoints")
    print(f"swath area swept {swept_total:.0f} m2 vs target area {total_plot_area:.0f} m2 "
          f"-> {swept_total / total_plot_area:.2f}x the field")
    cov = m.coverage
    if cov:
        worst = min(cov, key=lambda k: cov[k])
        best = max(cov, key=lambda k: cov[k])
        print(f"coverage worst {worst} {cov[worst]:.3f}  best {best} {cov[best]:.3f}")

    # Which pass lines actually laid chemistry on each plot, and how many times.
    lines_touching: dict[str, set[int]] = collections.defaultdict(set)
    area_by_line: dict[str, dict[int, float]] = collections.defaultdict(dict)
    SprayDrone._deposit = traced2
    engine2 = SimulationEngine(Settings(write_telemetry=False, default_speed=1.0))
    for _ in range(30):
        engine2.clock.advance(DT)
        engine2.tick()
    w2 = engine2.weather_state
    m2 = engine2.drone.plan_mission(targets, engine2.clock.now(), trigger="trace2",
                                     wind_dir=w2.wind_dir, wind_speed=w2.wind_speed,
                                     dose_l_ha=args.dose)
    engine2.drone.start(engine2.clock.now())

    def traced2(self, mm, sprayed_l, fx, fy, tx, ty, wind_speed, ts):
        before = dict(mm.coverage)
        line = mm.line_of_wp.get(self._last_wp_idx, None)
        original2(self, mm, sprayed_l, fx, fy, tx, ty, wind_speed, ts)
        for pid, plot in ((p.plot_id, p) for p in self.farm.plots):
            delta = mm.coverage.get(pid, 0.0) - before.get(pid, 0.0)
            if delta > 0 and line is not None:
                lines_touching[pid].add(line)
                area_by_line[pid][line] = area_by_line[pid].get(line, 0.0) + delta * plot.area_m2

    original2 = original
    for _ in range(20_000):
        engine2.clock.advance(DT)
        engine2.tick()
        if engine2.drone.state == "idle" and m2.status in ("done", "partial"):
            break
    SprayDrone._deposit = original2

    sample = targets[0].plot_id
    p = engine.field.by_id[sample]
    need_lines = math.ceil((abs(p.width * math.cos(math.radians(w2.wind_dir + 90)))
                            + abs(p.height * math.sin(math.radians(w2.wind_dir + 90))))
                           / m2.notes["swath_m"])
    print(f"\n{sample}: {p.width:.0f}x{p.height:.0f} m, {p.area_m2:.0f} m2")
    print(f"  pass lines that touched it: {len(lines_touching[sample])} "
          f"(geometry needs ~{need_lines})")
    print(f"  planned for it: {len(m2.covered_lines.get(sample, set()))} lines")
    tot = sum(area_by_line[sample].values())
    print(f"  swept area attributed: {tot:.0f} m2 = {tot / p.area_m2:.2f}x the plot")
    for line, area in sorted(area_by_line[sample].items())[:6]:
        print(f"    line {line:3d}: {area:7.0f} m2")
    print(f"  dose {m2.dose_ml_ha.get(sample, 0):.0f} mL/ha vs label {args.dose * 1000:.0f}")

    plot_id = plot_id or (max(cov, key=lambda k: cov[k]) if cov else None)
    if plot_id is None:
        print("no coverage at all")
        return 1
    plot = engine.field.by_id[plot_id]
    print(f"\ntracing {plot_id} ({plot.area_m2:.0f} m2, {plot.width:.0f}x{plot.height:.0f} m)")
    print(f"  {len(trace)} deposit steps, {per_plot_area[plot_id]:.0f} m2 accounted "
          f"({per_plot_area[plot_id] / plot.area_m2:.2f}x its own area)")
    by_wp = collections.defaultdict(float)
    for wp_idx, seg, sw, area, parea in trace:
        by_wp[wp_idx] += area
    print(f"  distinct waypoint indices touched: {len(by_wp)}")
    for wp_idx, area in sorted(by_wp.items())[:12]:
        print(f"    wp {wp_idx:4d}: {area:8.0f} m2 ({area / plot.area_m2:.2f}x plot)")
    if len(by_wp) > 12:
        print(f"    ... {len(by_wp) - 12} more")
    # The smoking gun: the same waypoint leg crediting the plot twice.
    dupes = {k: v for k, v in by_wp.items() if v / plot.area_m2 > 1.05}
    if dupes:
        print(f"\n  DOUBLE-COUNT: {len(dupes)} waypoint legs each credited >105% of the plot")
        for k, v in sorted(dupes.items())[:5]:
            print(f"    wp {k}: {v / plot.area_m2:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())