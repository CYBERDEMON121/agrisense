#!/usr/bin/env python3
"""Fly a real mission against the running server and watch it end to end.

This is the check a synthetic frame cannot make: every state transition
(preflight -> transit -> spraying -> rtl -> servicing -> partial/done) has to
survive a full `snapshot()` -- `/api/state` is what the dashboard renders, so a
crash in any of those serialisers is a blank screen for the operator.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8021"
TIMEOUT_S = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{path} -> {resp.status}")
        return json.load(resp)


def post(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{path} -> {resp.status}")
        return json.load(resp)


def main() -> int:
    # Slow the clock right down so each state persists long enough to sample.
    post("/api/sim/speed?speed=240")
    post("/api/sim/pause?paused=false")

    plan = post("/api/drone/mission?area=north&launch=true")
    mission = plan["mission"]
    ref = mission["ref"]
    print(f"launched {ref}: {mission['n_plots']} plots, "
          f"{mission['waypoints']} waypoints, {len(mission['waypoint_paths'])} legs, "
          f"{mission['area_ha']} ha @ {mission['dose_l_ha']} L/ha")

    seen_states: list[str] = []
    seen_status: list[str] = []
    max_wp = 0
    sorties = 0
    coverage_at_end: dict[str, float] = {}
    dose_at_end: dict[str, float] = {}
    litres = 0.0
    errors: list[str] = []
    polls = 0
    started = time.time()

    while time.time() - started < TIMEOUT_S:
        polls += 1
        try:
            # Every endpoint the dashboard polls has to survive the flight.
            frame = get("/api/state")
            for path in ("/api/drone", "/api/missions", "/api/cameras",
                         "/api/irrigation", "/api/summary", "/api/decisions"):
                get(path)
        except (urllib.error.HTTPError, RuntimeError, urllib.error.URLError) as exc:
            errors.append(f"{exc}")
            time.sleep(0.4)
            continue

        drone = frame["drone"]
        m = drone.get("mission") or {}
        if drone["state"] not in seen_states:
            seen_states.append(drone["state"])
            print(f"  [{time.time() - started:6.1f}s] drone -> {drone['state']}")
        if m.get("status") and m["status"] not in seen_status:
            seen_status.append(m["status"])
            print(f"  [{time.time() - started:6.1f}s] mission -> {m['status']} "
                  f"(wp {m.get('wp_idx')}/{m.get('waypoints')}, "
                  f"sortie {m.get('sorties')}, {m.get('sprayed_litres')} L)")
        if m.get("ref") == ref:
            max_wp = max(max_wp, m.get("wp_idx", 0))
            sorties = max(sorties, m.get("sorties", 0))
            litres = m.get("sprayed_litres", litres)
            coverage_at_end = m.get("coverage", coverage_at_end)
            dose_at_end = m.get("dose_ml_ha", dose_at_end)
        if m.get("status") in ("done", "partial", "aborted"):
            break
        time.sleep(0.4)

    print(f"\n{'-' * 62}")
    print(f"polls            {polls}")
    print(f"endpoint errors  {len(errors)}")
    for err in errors[:5]:
        print(f"    {err}")
    print(f"drone states     {' -> '.join(seen_states)}")
    print(f"mission states   {' -> '.join(seen_status)}")
    print(f"waypoints flown  {max_wp}/{mission['waypoints']}")
    print(f"sorties          {sorties + 1}")
    print(f"spray volume     {litres} L")
    if coverage_at_end:
        vals = list(coverage_at_end.values())
        print(f"coverage         min {min(vals):.2f} mean "
              f"{sum(vals) / len(vals):.3f} max {max(vals):.2f}")
        full = sum(1 for v in vals if v >= 0.999)
        print(f"plots at >=99.9% {full}/{len(vals)}")
    if dose_at_end:
        dvals = list(dose_at_end.values())
        print(f"dose mL/ha       min {min(dvals):.0f} mean "
              f"{sum(dvals) / len(dvals):.0f} max {max(dvals):.0f} "
              f"(label {mission['dose_l_ha'] * 1000:.0f})")

    fails = []
    if errors:
        fails.append(f"{len(errors)} endpoint errors during flight")
    for want in ("preflight", "spraying", "rtl", "landing"):
        if want not in seen_states:
            fails.append(f"never entered {want}")
    if "partial" not in seen_status and "done" not in seen_status:
        fails.append("mission never finished")
    if max_wp < mission["waypoints"]:
        fails.append(f"stopped at waypoint {max_wp}/{mission['waypoints']}")
    if not coverage_at_end:
        fails.append("no coverage recorded")
    if coverage_at_end and min(coverage_at_end.values()) < 0.95:
        worst = min(coverage_at_end.items(), key=lambda kv: kv[1])
        fails.append(f"under-covered plot {worst[0]} at {worst[1]:.2f}")
    if coverage_at_end and litres <= 0:
        fails.append("no spray volume recorded")

    print()
    if fails:
        for f in fails:
            print(f"FAIL {f}")
        return 1
    print("mission flew end to end with every dashboard endpoint healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())