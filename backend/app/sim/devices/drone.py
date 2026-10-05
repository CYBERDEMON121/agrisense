"""Agricultural spray drone: autopilot, mission planner and deposition model.

Flight is a small state machine driven on the *simulated* clock::

    idle -> preflight -> takeoff -> transit -> spraying -> rtl -> landing -> servicing -> idle

Mission planning generates a boustrophedon (back-and-forth) coverage pattern
across the bounding box of the target plots, with pass lines perpendicular to
the wind so drift is spread across the swath.  Deposition is modelled as a
function of droplet size, airspeed and wind, and the volume actually delivered
to each plot drives ``FieldModel.apply_pesticide`` - so a spray mission only
knocks pest pressure down by what it genuinely covered.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from ...bus import EventBus
from ..field import FieldModel, Plot

#: A plot counts as covered only when the swath has passed over all of it.  It
#: lives at module scope because both ``Mission`` (which reports it) and
#: ``SprayDrone`` (which enforces it) need it.
FULL_COVERAGE = 1.0


def _clip_area(poly: list[tuple[float, float]], x0: float, y0: float,
               w: float, h: float) -> float:
    """Exact area of a convex polygon clipped to an axis-aligned rectangle.

    The deposit step needs this to be right: an oblique swath crossing a plot is
    neither a clean lateral band nor a full chord, so approximating the overlap
    with ``band_width x chord_length`` over-counts every corner of every pass and
    pushed coverage past 1.0.  Sutherland-Hodgman against the four rectangle
    edges is exact and costs nothing at 38 plots.
    """
    x1, y1 = x0 + w, y0 + h

    poly = list(poly)
    # One half-plane at a time.  ``axis`` is the coordinate being bounded; the
    # cut divides by the delta in that *same* coordinate, so the intersection
    # lands on the boundary line rather than on a parallel one.
    for axis, bound, keep_greater in ((0, x0, True), (0, x1, False),
                                      (1, y0, True), (1, y1, False)):
        if not poly:
            return 0.0
        clipped: list[tuple[float, float]] = []
        prev = poly[-1]
        prev_in = prev[axis] >= bound if keep_greater else prev[axis] <= bound
        for cur in poly:
            cur_in = cur[axis] >= bound if keep_greater else cur[axis] <= bound
            if cur_in != prev_in:
                denom = cur[axis] - prev[axis]
                t = 0.0 if abs(denom) < 1e-12 else (bound - prev[axis]) / denom
                clipped.append((prev[0] + (cur[0] - prev[0]) * t,
                                prev[1] + (cur[1] - prev[1]) * t))
            if cur_in:
                clipped.append(cur)
            prev, prev_in = cur, cur_in
        poly = clipped

    if len(poly) < 3:
        return 0.0
    area = 0.0
    for i, (px, py) in enumerate(poly):
        qx, qy = poly[(i + 1) % len(poly)]
        area += px * qy - qx * py
    return abs(area) / 2.0


@dataclass(slots=True)
class Waypoint:
    x: float
    y: float
    alt_m: float = 2.0
    spray: bool = False
    kind: str = "line"      # line | transit | home | land
    idx: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"x": round(self.x, 1), "y": round(self.y, 1), "alt": self.alt_m,
                "spray": self.spray, "kind": self.kind, "idx": self.idx}

@dataclass(slots=True)
class Mission:
    ref: str
    plots: list[str]
    waypoints: list[Waypoint] = field(default_factory=list)
    kind: str = "pesticide"
    product: str = "Emamectin benzoate 5% SG"
    dose_l_ha: float = 0.35
    swath_m: float = 4.5
    trigger: str = "manual"
    created_ts: float = 0.0
    started_ts: float | None = None
    ended_ts: float | None = None
    status: str = "planned"
    target_area_ha: float = 0.0
    wp_idx: int = 0
    distance_m: float = 0.0
    sprayed_litres: float = 0.0
    payload_start_l: float = 0.0
    battery_start: float = 0.0
    coverage: dict[str, float] = field(default_factory=dict)
    #: Litres of spray volume geometrically attributed to each plot (as flown,
    #: i.e. already multiplied by the deposition efficiency of the airframe).
    deposit_l: dict[str, float] = field(default_factory=dict)
    #: mL/ha actually landing on each plot, accumulated over every sortie.
    dose_ml_ha: dict[str, float] = field(default_factory=dict)
    #: For each plot, the indices of the pass lines the plan expects to cross it.
    #: The autopilot uses this to decide a plot is done: on a single oblique
    #: block the last waypoints sweep headland that belongs to no plot, so
    #: counting waypoints instead of plots strands the tail of the field.
    covered_lines: dict[str, set[int]] = field(default_factory=dict)
    #: Which pass line each spray waypoint belongs to, so a flown leg can be
    #: matched back to the plots it was meant to treat.
    line_of_wp: dict[int, int] = field(default_factory=dict)
    #: The boom-off entry waypoint index for each pass line.  The autopilot must
    #: fly to this before the spraying leg, or the cross-field repositioning
    #: happens with the boom open.
    line_entry_wp: dict[int, int] = field(default_factory=dict)
    #: Pass lines actually completed, per plot.
    flown_lines: dict[str, set[int]] = field(default_factory=dict)
    #: Ground area the flown pass lines sweep (length x swath), which on an
    #: oblique heading exceeds the planted area.  Volume accounting uses it.
    raster_m2: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)
    sorties: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "kind": self.kind, "status": self.status,
                "plots": self.plots, "n_plots": len(self.plots),
                "area_ha": round(self.target_area_ha, 2), "product": self.product,
                "dose_l_ha": self.dose_l_ha, "swath_m": self.swath_m, "trigger": self.trigger,
                "waypoints": len(self.waypoints), "wp_idx": self.wp_idx,
                # Geometry for the mission-plan overlay: one straight segment per
                # leg, tagged with the index of the waypoint it starts from, so a
                # client can tell flown passes from ones still to come.
                "waypoint_paths": self.segments(),
                "distance_m": round(self.distance_m, 1),
                "sorties": self.sorties,
                "partially_covered": sorted(k for k, v in self.coverage.items()
                                            if v < FULL_COVERAGE),
                "sprayed_litres": round(self.sprayed_litres, 2),
                "battery_start": round(self.battery_start, 1),
                "created_ts": self.created_ts, "started_ts": self.started_ts,
                "ended_ts": self.ended_ts, "notes": self.notes,
                "coverage": {k: round(v, 3) for k, v in self.coverage.items()},
                "dose_ml_ha": {k: round(v, 1) for k, v in self.dose_ml_ha.items()}}

    def segments(self) -> list[dict[str, Any]]:
        """Straight legs of the plan, for drawing the mission overlay.

        ``idx`` is the index of the waypoint the leg leaves from, so leg ``i`` has
        been flown exactly when ``wp_idx > i``.
        """
        out: list[dict[str, Any]] = []
        for i in range(len(self.waypoints) - 1):
            a, b = self.waypoints[i], self.waypoints[i + 1]
            if math.hypot(b.x - a.x, b.y - a.y) < 0.5:
                continue        # e.g. the pad -> takeoff point pair
            out.append({"x1": round(a.x, 1), "y1": round(a.y, 1),
                        "x2": round(b.x, 1), "y2": round(b.y, 1),
                        "idx": i, "kind": b.kind, "spray": b.spray})
        return out


class SprayDrone:
    """Multirotor spray drone with battery, payload and a coverage autopilot."""

    STATES = ("idle", "preflight", "takeoff", "transit", "spraying", "rtl", "landing",
              "servicing", "abort")

    #: Airframe / mission limits (a 30 L class ag drone).
    CRUISE_MS = 6.0
    SPRAY_MS = 5.0
    MAX_WIND_MS = 8.0           # airframe limit; the automation rule is stricter
    BATTERY_MIN_RETURN = 0.22    # fraction -> start returning home
    BATTERY_LAND = 0.12          # fraction -> must land now
    CAPACITY_L = 30.0
    FLIGHT_MINUTES = 24.0        # empty-tank endurance
    #: Droplet spectrum: VMD in microns, drift risk rises sharply with size.
    DROPLET_VMD_UM = 220.0
    #: Deposition efficiency of a well-tuned coarse droplet at 4 m/s wind.
    BASE_DEPOSITION = 0.88
    #: A field this size cannot be covered in one battery, so missions are
    #: flown as a series of sorties that resume where they left off.
    SORTIE_MINUTES = 12.0
    #: Volumetric coverage at which a plot counts as fully treated.
    FULL_COVERAGE = FULL_COVERAGE
    #: Asymptotic pest kill at an effectively infinite dose.
    MAX_EFFICACY = 0.94
    #: Ground-service refuelling rate (L/min) - a pump at the pad.
    REFILL_LPM = 9.0

    def __init__(self, bus: EventBus, farm: FieldModel, rng: random.Random,
                 pad_x: float = 270.0, pad_y: float = 430.0) -> None:
        self.bus = bus
        self.farm = farm
        self.rng = rng
        self.device_id = "drone-01"
        self.name = "Agridrone (DJI T30 class)"
        self.pad = (pad_x, pad_y)
        self.x, self.y = pad_x, pad_y
        self.z = 0.0
        self.heading = 0.0
        self.speed = 0.0
        self.state = "idle"
        self.battery = 1.0
        self.payload_l = self.CAPACITY_L
        self.mission: Mission | None = None
        self.missions_done = 0
        self.area_sprayed_ha = 0.0
        self.flight_seconds = 0.0
        self.trail: list[tuple[float, float]] = [(pad_x, pad_y)]
        self.auto = True
        self.status = "online"
        self.last_error: str | None = None
        self.service_seconds_left = 0.0
        self._seq = 0
        self._last_deposit_ts: float = 0.0
        self.sorties = 0
        self.resume_mission: Mission | None = None

    # ---- device record ---------------------------------------------------
    def device_record(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "kind": "drone", "name": self.name,
                "area": "landingpad", "vendor": "DJI", "model": "T30",
                "fw": "06.01.0300", "status": self.status, "battery": round(self.battery * 100, 1),
                "rssi": -47, "meta": {"capacity_l": self.CAPACITY_L,
                                      "states": list(self.STATES),
                                      "position": [self.x, self.y, self.z]}}

    # ---- mission planning ------------------------------------------------
    def plan_mission(self, plots: Sequence[Plot], ts: float, trigger: str = "manual",
                     wind_dir: float = 90.0, wind_speed: float = 2.0,
                     kind: str = "pesticide", product: str = "Emamectin benzoate 5% SG",
                     dose_l_ha: float = 0.35) -> Mission:
        """Build a boustrophedon coverage plan for the given plots."""
        self._seq += 1
        mission = Mission(ref=f"M{ts:.0f}-{self._seq:03d}", plots=[p.plot_id for p in plots],
                          kind=kind, product=product, dose_l_ha=dose_l_ha,
                          trigger=trigger, created_ts=ts,
                          target_area_ha=sum(p.area_ha for p in plots))
        mission.payload_start_l = self.payload_l
        mission.battery_start = self.battery

        if not plots:
            mission.status = "empty"
            return mission

        margin = 2.0
        x0 = min(p.x for p in plots) - margin
        x1 = max(p.x + p.width for p in plots) + margin
        y0 = min(p.y for p in plots) - margin
        y1 = max(p.y + p.height for p in plots) + margin

        # Pass lines run perpendicular to the wind so the drift plume is spread
        # across the swath instead of being blown off one edge of the field.
        theta = math.radians(wind_dir + 90.0)
        dx, dy = math.cos(theta), math.sin(theta)      # direction along a pass
        nx, ny = -dy, dx                                # normal to a pass
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

        # Line spacing must use the *effective* treated width (nominal swath
        # plus the wind-widened plume), otherwise overlapping lines would
        # double-apply on every plot.
        swath = self.effective_swath(mission.swath_m, wind_speed)
        # Space pass lines across the plots themselves, not across the bounding
        # box: an oblique heading makes the projected box far wider than the
        # planted area, and spacing on the box both wastes passes over headland
        # and leaves edge plots with too few lines to reach full coverage.
        #
        # Offsets are measured on the pass *normal*.  Projecting them onto the
        # pass direction instead is degenerate - every line collapses onto the
        # same infinite line through the block centre, so plots off that line
        # never get a pass at all.
        # The pass band is the union of the plots' own projections onto the normal,
        # edge to edge.  Padding the centre range by the *largest* half-extent
        # instead (as this once did) extends the band tens of metres past the
        # planted area on an oblique heading, and every one of those extra
        # passes re-sprays the plots nearest the edge.
        lows, highs = [], []
        for p in plots:
            off = (p.center[0] - cx) * nx + (p.center[1] - cy) * ny
            proj = abs(p.width * nx) / 2.0 + abs(p.height * ny) / 2.0
            lows.append(off - proj)
            highs.append(off + proj)
        # Half a line of overlap at each end so the outermost edge of the field
        # is still inside the swath of the first and last pass.
        line_lo, line_hi = min(lows) - swath / 2.0, max(highs) + swath / 2.0
        n_lines = max(1, int(math.ceil((line_hi - line_lo) / swath)))
        # Space pass lines by the swath itself, not by ``range / n_lines``.
        # Rounding the count up and stretching the leftover into the spacing
        # makes adjacent bands overlap, and overlapping bands are real
        # over-dose: volume accounting stays honest but every plot under that
        # overlap gets 1.5x the label rate.  With a fixed spacing the bands
        # tile edge to edge - which is what the raster area bookkeeping assumes.
        step = swath
        lo, hi = line_lo, line_hi

        wps: list[Waypoint] = [
            Waypoint(self.pad[0], self.pad[1], 0.0, False, "home"),
            Waypoint(self.pad[0], self.pad[1], 2.5, False, "takeoff"),
        ]
        idx = 0
        prev_end: tuple[float, float] | None = None
        empty_lines = 0
        mission.covered_lines = {}
        for i in range(n_lines):
            off = lo + (i + 0.5) * step
            px, py = cx + nx * off, cy + ny * off
            # Clip this line to the plots it actually crosses.  Extending every
            # line to the corners of the bounding box sprays the headlands
            # between and beyond the plots, which for a block at an oblique
            # heading roughly doubles the volume - the plan has to follow the
            # treated area, not its bounding box.
            # Which plots this line can actually treat: the swath must reach
            # them (perpendicular offset) *and* the pass must be long enough to
            # cross them (along offset).  Recording every plot unconditionally
            # made the autopilot wait for lines that never touch a plot.
            touched = [p for p in plots
                       if abs((p.center[0] - px) * nx + (p.center[1] - py) * ny)
                       <= abs(p.width * nx) / 2.0 + abs(p.height * ny) / 2.0 + swath / 2.0]
            if not touched:
                empty_lines += 1
                continue
            # Clipped to the plots it crosses, not to a box: the deposit is already
            # a swath-wide rectangle centred on the flown path, so extending the
            # leg by half a swath sprays a full extra swath of headland.
            t0, t1 = self._line_extent(touched, px, py, dx, dy)
            if t1 - t0 < 1.0:
                empty_lines += 1
                continue
            # `t` is a distance along the pass direction (dx, dy).
            start = (px + dx * t0, py + dy * t0)
            end = (px + dx * t1, py + dy * t1)
            # Completion is judged on plots, not waypoints: record this line as
            # a required pass for each plot it genuinely crosses.
            for plot in touched:
                mission.covered_lines.setdefault(plot.plot_id, set()).add(i)
            if i % 2:  # alternate direction: back-and-forth
                start, end = end, start
            if prev_end is not None:
                wps.append(Waypoint(prev_end[0], prev_end[1], 2.5, False, "transit"))
            # The aircraft flies *to* the line start with the boom off, then sprays the
            # single leg from start to end.  Marking both endpoints as spray
            # would treat the cross-field repositioning as a pass, spraying
            # headland and plot corners at the wrong heading.
            mission.line_entry_wp[i] = idx
            mission.line_of_wp[idx] = i
            wps.append(Waypoint(start[0], start[1], 2.0, False, "line", idx))
            idx += 1
            mission.line_of_wp[idx] = i
            wps.append(Waypoint(end[0], end[1], 2.0, True, "line", idx))
            idx += 1
            prev_end = end
        wps.append(Waypoint(self.pad[0], self.pad[1], 3.0, False, "rtl"))
        wps.append(Waypoint(self.pad[0], self.pad[1], 0.0, False, "land"))
        mission.waypoints = wps
        # The airframe sprays its *raster*: every metre flown at one swath width.
        # On an oblique heading that covers more ground than the planted area
        # (the corners of a rotated rectangle sweep twice), and the corner
        # over-spray is real - so the volume to reconcile against is the raster,
        # not the sum of plot areas.
        raster_m2 = 0.0
        for i, wp in enumerate(wps):
            if wp.kind == "line" and wp.spray:
                ent = wps[i - 1]
                raster_m2 += math.hypot(wp.x - ent.x, wp.y - ent.y) * swath
        mission.raster_m2 = raster_m2
        eff = self.deposition_efficiency(wind_speed)
        mission.notes = {"wind_dir": round(wind_dir, 1), "wind_speed": round(wind_speed, 2),
                         "pass_lines": n_lines - empty_lines, "lines_skipped": empty_lines,
                         "spacing_m": round(step, 2), "swath_m": round(swath, 2),
                         "bbox": [round(v, 1) for v in (x0, y0, x1, y1)],
                         "deposition": eff,
                         "raster_ha": round(raster_m2 / 10_000.0, 3),
                         # What the tank *should* hold for this mission: the label
                         # rate over the area actually flown, inflated by the drift
                         # the airframe cannot avoid.  Anything else means the nozzle
                         # flow or the pass geometry is wrong, which is what a
                         # reconciliation check compares.
                         "expected_air_l": round(raster_m2 / 10_000.0
                                                  * mission.dose_l_ha / eff, 2)}
        self.mission = mission
        mission.status = "planned"
        self.bus.publish(self.bus.topic("drone", self.device_id, "mission"), {
            "value": len(wps), "ref": mission.ref, "event": "planned",
            "plots": len(plots), "area_ha": round(mission.target_area_ha, 2),
            "waypoints": len(wps), "trigger": trigger})
        return mission

    @staticmethod
    def _line_extent(plots: Sequence[Plot], px: float, py: float,
                     dx: float, dy: float, half_swath: float = 0.0) -> tuple[float, float]:
        """Where a pass line through ``(px, py)`` actually crosses the plots.

        Returns the ``(t_min, t_max)`` interval along the line direction
        ``(dx, dy)`` that overlaps any target plot, widened by ``half_swath`` so
        the outermost lines still treat the field edge.  ``t`` is measured from
        the pass point, mapping to the point ``(px - dy*t, py + dx*t)``.

        Returns an empty interval when the line misses every plot, which happens
        at the corners of an oblique block and is the reason a bbox-sized plan
        over-sprays.
        """
        best_lo, best_hi = math.inf, -math.inf
        for plot in plots:
            # Perpendicular distance from the plot centre to the line.  The line
            # normal is (-dy, dx) because (dx, dy) is the *direction* of travel
            # along the pass; using the normal for the along-line offset (as
            # this once did) measures the wrong axis entirely and collapses
            # every pass onto a cross-field segment.
            perp = abs((plot.center[0] - px) * -dy + (plot.center[1] - py) * dx)
            # Projected half-width of the plot along the line normal.
            proj = abs(plot.width * dy) / 2.0 + abs(plot.height * dx) / 2.0
            if perp > proj + half_swath:
                continue
            along = (plot.center[0] - px) * dx + (plot.center[1] - py) * dy
            half_along = abs(plot.width * dx) / 2.0 + abs(plot.height * dy) / 2.0
            best_lo = min(best_lo, along - half_along)
            best_hi = max(best_hi, along + half_along)
        if best_hi < best_lo:
            return 0.0, 0.0
        return best_lo, best_hi

    def effective_swath(self, swath_m: float, wind_speed: float) -> float:
        """Width of the treated band actually laid down on the ground.

        The nozzle sprays a cone that shears downwind, so the deposit is wider
        than the boom spacing.  Pass spacing *and* nozzle flow both use this
        value, which keeps the delivered dose at the label rate.
        """
        return swath_m + min(3.0, wind_speed * 0.35)

    def deposition_efficiency(self, wind_speed: float) -> float:
        """Fraction of sprayed volume that lands inside the intended swath.

        Fine droplets are lost to drift; wind shear compounds it.  The exponent
        is calibrated so 4 m/s keeps ~88% and 8 m/s falls to ~60%.
        """
        drift_factor = 1.0 + 0.11 * wind_speed ** 1.35
        return max(0.15, min(0.98, self.BASE_DEPOSITION / drift_factor))

    # ---- commands ---------------------------------------------------------
    def start(self, ts: float) -> bool:
        if self.state not in ("idle", "abort") or not self.mission:
            return False
        if self.mission.status != "planned":
            return False
        self.state = "preflight"
        self.mission.status = "preflight"
        self.mission.started_ts = ts
        return True

    def abort(self, ts: float, reason: str) -> None:
        self.last_error = reason
        if self.mission and self.mission.status not in ("done", "aborted"):
            self.mission.status = "aborted"
            self.mission.ended_ts = ts
            self.mission.notes["abort_reason"] = reason
        self.state = "abort"
        self.bus.publish(self.bus.topic("drone", self.device_id, "event"),
                         {"value": 1, "event": "abort", "reason": reason, "ts": ts})

    def return_home(self, ts: float, reason: str) -> None:
        if self.state in ("rtl", "landing", "idle"):
            return
        self.last_error = reason
        if self.mission and self.mission.status in ("planned", "preflight", "active"):
            self.mission.status = "aborted"
            self.mission.ended_ts = ts
            self.mission.notes["abort_reason"] = reason
        self.state = "rtl"
        self.bus.publish(self.bus.topic("drone", self.device_id, "event"),
                         {"value": 1, "event": "rtl", "reason": reason, "ts": ts})

    # ---- simulation -------------------------------------------------------
    def step(self, ts: float, dt_sim_s: float, wind_speed: float = 2.0,
             wind_dir: float = 90.0, allow_spray: bool = True) -> None:
        """Advance the autopilot by ``dt_sim_s`` simulated seconds."""
        if self.state in ("idle",):
            self._publish_telemetry(ts, wind_speed)
            return

        flying = self.state not in ("servicing",)
        self.flight_seconds += dt_sim_s
        self._update_battery(dt_sim_s, spraying=(self.state == "spraying"))
        if self.battery <= self.BATTERY_LAND and self.state not in ("landing", "rtl"):
            self.return_home(ts, "battery_critical")

        if self.state == "preflight":
            self.state = "takeoff"
        elif self.state == "takeoff":
            self.z = min(2.5, self.z + dt_sim_s * 2.0)
            if self.z >= 2.4:
                self.state = "transit"
        elif self.state in ("transit", "spraying"):
            self._advance_mission(ts, dt_sim_s, wind_speed, wind_dir, allow_spray)
        elif self.state == "abort":
            self.state = "rtl"
        elif self.state == "rtl":
            self._fly_to(self.pad[0], self.pad[1], 3.0, dt_sim_s, speed=9.0)
            if math.hypot(self.x - self.pad[0], self.y - self.pad[1]) < 1.5:
                self.state = "landing"
        elif self.state == "landing":
            self.z = max(0.0, self.z - dt_sim_s * 2.0)
            if self.z <= 0.02:
                self._finish(ts)
        elif self.state == "servicing":
            self.service_seconds_left -= dt_sim_s
            self.battery = min(1.0, self.battery + dt_sim_s / (self.FLIGHT_MINUTES * 60.0) * 3.0)
            self.payload_l = min(self.CAPACITY_L,
                                 self.payload_l + dt_sim_s / 60.0 * self.REFILL_LPM)
            if self.service_seconds_left <= 0:
                # A field is rarely finished in one battery: if the mission
                # still has pass lines left, swap pack and resume.
                if self.resume_mission is not None and self.resume_mission.status == "returning":
                    m = self.resume_mission
                    self.mission = m
                    m.status = "active"
                    m.battery_start = self.battery
                    m.sorties += 1
                    self.sorties += 1
                    self.last_error = None
                    self.state = "transit"
                    self.bus.publish(self.bus.topic("drone", self.device_id, "mission"), {
                        "value": m.wp_idx, "ref": m.ref, "event": "resumed",
                        "sortie": m.sorties, "ts": ts})
                else:
                    self.resume_mission = None
                    self.state = "idle"
                    self.bus.publish(self.bus.topic("drone", self.device_id, "event"),
                                     {"value": 1, "event": "ready", "ts": ts})

        self.speed = min(self.speed, 12.0)
        self.trail.append((self.x, self.y))
        if len(self.trail) > 900:
            del self.trail[:200]
        self._publish_telemetry(ts, wind_speed)

    def _advance_mission(self, ts: float, dt_sim_s: float, wind_speed: float,
                         wind_dir: float, allow_spray: bool) -> None:
        m = self.mission
        if m is None:
            self.state = "rtl"
            return
        if m.status == "planned":
            m.status = "active"
        wp = m.waypoints[m.wp_idx] if m.wp_idx < len(m.waypoints) else None
        if wp is None:
            self.state = "rtl"
            return

        prev_x, prev_y = self.x, self.y
        reached, _moved = self._fly_to(wp.x, wp.y, wp.alt_m, dt_sim_s,
                                        speed=self.SPRAY_MS if wp.spray else self.CRUISE_MS)

        # Spray is emitted continuously along a pass line, not on arrival.
        if wp.spray:
            self.state = "spraying"
            self._spray_pass(ts, dt_sim_s, prev_x, prev_y, wind_speed, wind_dir, allow_spray)
        elif reached:
            self.state = "transit"

        if not reached:
            return
        if wp.kind == "rtl":
            self.state = "rtl"
            return
        if wp.kind == "land":
            self.state = "landing"
            return
        self._mark_line_flown(m, wp)
        m.wp_idx += 1
        if m.wp_idx >= len(m.waypoints):
            self.state = "rtl"
            m.status = "returning"
            return
        # Once every pass line a plot needs has been flown, that plot is done and
        # any waypoint left only sweeps headland.  On an oblique block that tail
        # is long enough to strand plots mid-mission, so skip ahead to the first
        # waypoint that still contributes to an unfinished plot.
        nxt = self._next_useful_wp(m, m.wp_idx)
        if nxt is None:
            m.notes["finished_on"] = f"wp{m.wp_idx}"
            m.wp_idx = len(m.waypoints)
            self.state = "rtl"
            m.status = "returning"
            return
        if nxt > m.wp_idx:
            m.notes.setdefault("skipped_waypoints", 0)
            m.notes["skipped_waypoints"] += nxt - m.wp_idx
            m.wp_idx = nxt

    def _next_useful_wp(self, m: Mission, start: int) -> int | None:
        """First waypoint at or after ``start`` that still treats a plot.

        Returns ``None`` when no unfinished plot has any pass line left, meaning
        the mission is genuinely complete and the drone can land.
        """
        wanted: dict[int, str] = {}
        for pid, lines in m.covered_lines.items():
            done = m.flown_lines.get(pid, set())
            missing = lines - done
            if not missing:
                continue
            for line in missing:
                wanted.setdefault(line, pid)
        if not wanted:
            return None
        for idx in range(start, len(m.waypoints)):
            wp = m.waypoints[idx]
            if wp.kind in ("rtl", "land"):
                return None
            if wp.kind == "line" and wp.spray:
                line = m.line_of_wp.get(wp.idx)
                if line is not None and line in wanted:
                    # Go to the *entry* waypoint of that line first: it carries
                    # the boom off, and the following waypoint flies the leg.
                    # Aiming straight at the exit makes the drone drift across
                    # the field spraying, which both wastes volume and doses
                    # plots it never meant to touch.
                    return max(idx - 1, m.line_entry_wp.get(line, idx - 1))
        return None

    def _mark_line_flown(self, m: Mission, wp: Waypoint) -> None:
        """Record a completed pass line against every plot it was meant for.

        Only the *spraying* waypoint closes a line.  Each line has an entry
        waypoint (boom off) and an exit waypoint (boom on); marking the entry
        would tell the skip-ahead logic the line is done before a drop has
        fallen, and it would skip straight over the spraying leg.
        """
        if not wp.spray:
            return
        line = m.line_of_wp.get(wp.idx)
        if line is None:
            return
        for pid, lines in m.covered_lines.items():
            if line in lines:
                m.flown_lines.setdefault(pid, set()).add(line)

    def _fly_to(self, tx: float, ty: float, alt: float,
                dt_sim_s: float, speed: float) -> tuple[bool, float]:
        """Steer toward a waypoint.  Returns (reached, distance moved)."""
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        self.z += (alt - self.z) * min(1.0, dt_sim_s * 2.0)
        if dist < 0.8:
            self.speed = max(0.0, self.speed - dt_sim_s * 6)
            return True, 0.0
        # Turn toward the waypoint first; heading changes cost time.
        want = math.degrees(math.atan2(dx, dy)) % 360.0
        delta = ((want - self.heading + 540.0) % 360.0) - 180.0
        turn_rate = 150.0 * dt_sim_s
        self.heading = (self.heading + max(-turn_rate, min(turn_rate, delta))) % 360.0
        # Effective speed scales down while turning hard.
        align = max(0.0, 1.0 - abs(delta) / 120.0)
        self.speed = speed * align
        step = min(dist, self.speed * dt_sim_s)
        if step > 0:
            self.x += dx / dist * step
            self.y += dy / dist * step
            if self.mission:
                self.mission.distance_m += step
        return step >= dist, step

    def _spray_pass(self, ts: float, dt_sim_s: float, from_x: float, from_y: float,
                    wind_speed: float, wind_dir: float, allow_spray: bool) -> None:
        """Deposit spray along the current pass line onto the plots below."""
        m = self.mission
        if m is None:
            return
        if not allow_spray:
            return
        if self.payload_l <= 0.02:
            self.state = "rtl"
            if m.status == "active":
                m.status = "returning"
            return
        if wind_speed > self.MAX_WIND_MS:
            self.abort(ts, f"wind {wind_speed:.1f} m/s above airframe limit")
            return

        # Nozzle flow follows from the target dose, swath and airspeed, exactly
        # as a real spray controller sets it:
        #   dose [L/ha] = flow [L/min] / (speed [m/s] * swath [m] * 60 / 1e4)
        # The swath term uses the wind-widened effective width so the deposit
        # is spread at the label rate rather than over-concentrated.
        #
        # That equation gives the volume *in the air*, but the label rate is a
        # ground rate: only `deposition_efficiency` of the spray lands inside
        # the swath, the rest is drift.  A controller compensating for expected
        # drift sprays `target / eff` from the tank, so dividing here is what
        # makes the delivered dose match the label at a given wind speed
        # instead of quietly underspraying by the drift factor.
        eff = self.deposition_efficiency(wind_speed)
        flow_lpm = self.flow_for_dose(m.dose_l_ha / max(eff, 0.05),
                                      self.effective_swath(m.swath_m, wind_speed),
                                      self.SPRAY_MS)
        litres = min(self.payload_l, flow_lpm * dt_sim_s / 60.0)
        # The pump runs while the boom is open, so turning (the airframe slows
        # down but the tank keeps flowing) would dump the swath it never
        # covered.  Meter the volume to the ground actually swept instead:
        # the laid-down deposit can never exceed the pass it rode in on.
        moved = math.hypot(self.x - from_x, self.y - from_y)
        air_rate_lpha = m.dose_l_ha / max(eff, 0.05)
        ground_swept_ha = moved * self.effective_swath(m.swath_m, wind_speed) / 10_000.0
        metered = air_rate_lpha * ground_swept_ha
        litres = min(litres, max(metered, 0.0)) if moved > 0 else 0.0
        self.payload_l -= litres
        m.sprayed_litres += litres
        self._deposit(m, litres, from_x, from_y, self.x, self.y, wind_speed, ts)

    @classmethod
    def flow_for_dose(cls, dose_l_ha: float, swath_m: float, speed_ms: float) -> float:
        """Nozzle flow (L/min) that delivers ``dose_l_ha`` at this geometry."""
        area_rate_ha_min = speed_ms * swath_m * 60.0 / 10_000.0   # ha/min
        return max(0.0, dose_l_ha * area_rate_ha_min)

    def _deposit(self, m: Mission, sprayed_l: float, from_x: float, from_y: float,
                 to_x: float, to_y: float, wind_speed: float, ts: float) -> None:
        """Attribute the swath swept this step to the plots it actually covered.

        Each step the aircraft sweeps a rectangle from where it *was* to where it
        *is*, ``|to - from| x swath`` wide.  Using the real displacement rather
        than the commanded heading matters: the autopilot turns at 150 deg/s
        while still translating, so a rectangle built from ``self.heading``
        drifts off the flown path during every turn and credits ground that was
        already treated - which is what inflated coverage past 1.0.

        That rectangle is clipped against every plot, yielding three
        accumulators per plot:

        * ``coverage``    - geometric swept fraction (a full set of pass lines
          with no overlap drift brings this to 1.0).
        * ``deposit_l``   - litres of tank volume apportioned by swept-area share
          (volume divided among however many plots the plume overlapped, so a
          single pass over a whole block still spends only one pass' worth).
        * ``dose_ml_ha``  - deposit scaled by the deposition efficiency of the
          airframe in the current wind, converted to mL/ha of plot area.

        Coverage therefore stays a clean geometric quantity while dose carries
        the physics, and a plot genuinely flown twice accumulates dose (and
        over-spray) exactly as it would in the field.
        """
        seg_x, seg_y = to_x - from_x, to_y - from_y
        moved_m = math.hypot(seg_x, seg_y)
        if sprayed_l <= 0 or moved_m <= 0:
            return
        eff = self.deposition_efficiency(wind_speed)
        swath_eff = self.effective_swath(m.swath_m, wind_speed)
        half = swath_eff / 2.0
        # Unit vector along the segment actually flown this step.
        hx, hy = seg_x / moved_m, seg_y / moved_m
        px_, py_ = -hy, hx                    # perpendicular to the pass line
        swept_area = max(moved_m * swath_eff, 1e-6)
        # The swept region in world coordinates: a rectangle swept along the
        # segment.  Both it and the plot are polygons, so the overlap is an exact
        # polygon intersection.
        ex, ey = px_ * half, py_ * half
        swept_poly = [(from_x + ex, from_y + ey), (to_x + ex, to_y + ey),
                      (to_x - ex, to_y - ey), (from_x - ex, from_y - ey)]
        # Only the mission's own plots are credited.  Sweeping the whole farm and
        # reporting whatever the plume happened to clip credits a mission with
        # neighbours it never targeted - which then look like plots it failed to
        # cover, and get sprayed a second time by the next sortie.
        for pid in m.plots:
            plot = self.farm.by_id.get(pid)
            if plot is None:
                continue
            # Cheap rejection using the plot's projected half-extents before
            # paying for the polygon clip.
            cx, cy = plot.center
            dx, dy = cx - from_x, cy - from_y
            d_perp = abs(dx * px_ + dy * py_)
            along = dx * hx + dy * hy
            half_along = (abs(plot.width * hx) + abs(plot.height * hy)) / 2.0
            half_perp = (abs(plot.width * px_) + abs(plot.height * py_)) / 2.0
            lateral = max(0.0, min(d_perp + half_perp, half) - max(d_perp - half_perp, -half))
            if lateral <= 0:
                continue
            lo = max(along - half_along, 0.0)
            hi = min(along + half_along, moved_m)
            if hi - lo <= 0:
                continue
            covered_area = _clip_area(swept_poly, plot.x, plot.y, plot.width, plot.height)
            if covered_area <= 0:
                continue
            # Coverage is the fraction of the plot *treated*, so it saturates:
            # overlapping passes re-treat ground that is already done, and that
            # extra chemistry is real over-application (it shows up in dose), not
            # extra coverage.
            m.coverage[pid] = min(1.0, m.coverage.get(pid, 0.0)
                                  + covered_area / plot.area_m2)
            # Volume apportioned to this plot by its share of the swept area.
            vol = sprayed_l * (covered_area / swept_area)
            m.deposit_l[pid] = m.deposit_l.get(pid, 0.0) + vol
            # litres/m2 -> mL/ha  (1 L/m2 = 1e3 mL/m2 = 1e7 mL/ha)
            m.dose_ml_ha[pid] = m.dose_ml_ha.get(pid, 0.0) + \
                vol * eff / max(plot.area_m2, 1.0) * 1.0e7
        self._last_deposit_ts = ts

    #: Dose-response steepness and the dose giving half-maximal kill, as a
    #: fraction of the label rate.
    DOSE_HILL_N = 2.0
    DOSE_EC50 = 0.4

    def efficacy_for_dose(self, dose_ml_ha: float, target_l_ha: float) -> float:
        """Dose-response of a contact + systemic insecticide (Hill equation).

        ``EC50 = 0.4 x label rate`` means spraying at the label rate already
        gives ~86% kill and under-delivery in wind costs control steeply.  This
        is exactly the trade-off the automation rule's wind gate exists to avoid.
        """
        if dose_ml_ha <= 0:
            return 0.0
        k = max(target_l_ha * 1000.0 * self.DOSE_EC50, 1e-6)
        r = dose_ml_ha / k
        return self.MAX_EFFICACY * (r ** self.DOSE_HILL_N) / (1.0 + r ** self.DOSE_HILL_N)

    def _finish(self, ts: float) -> None:
        """Land complete: bank the flown dose into the crop, then service."""
        m = self.mission
        if m is not None and m.status not in ("done", "aborted"):
            # Real dose from what actually landed, not the nominal pass count.
            for plot_id, dose in m.dose_ml_ha.items():
                if self.farm.by_id.get(plot_id) is None:
                    continue
                cov = min(1.0, m.coverage.get(plot_id, 0.0))
                efficacy = self.efficacy_for_dose(dose, m.dose_l_ha)
                before, after = self.farm.apply_pesticide(
                    plot_id, coverage=cov, efficacy=efficacy,
                    dose_ml_ha=dose, ts=ts)
                self.bus.publish(self.bus.topic("drone", self.device_id, "application"), {
                    "value": round(after, 4), "plot": plot_id,
                    "pest_before": round(before, 4), "pest_after": round(after, 4),
                    "dose_ml_ha": round(dose, 1), "target_ml_ha": round(m.dose_l_ha * 1000, 1),
                    "efficacy": round(efficacy, 3), "coverage": round(cov, 3),
                    "litres": round(m.deposit_l.get(plot_id, 0.0), 3),
                    "mission": m.ref, "sortie": m.sorties, "ts": ts})

            incomplete = sorted(k for k, v in m.coverage.items() if v < self.FULL_COVERAGE)
            # Only resume if there are spray passes still to fly: the tail of
            # the plan is just the return/land pair, which would otherwise
            # re-trigger the swap cycle forever on an already-flown field.
            has_lines = any(wp.kind == "line" for wp in m.waypoints[m.wp_idx:])
            m.notes["max_coverage"] = round(max(m.coverage.values(), default=0.0), 3)
            m.notes["mean_coverage"] = round(
                sum(m.coverage.values()) / max(len(m.coverage), 1), 3)
            m.notes["incomplete"] = len(incomplete)
            if incomplete and has_lines and self.auto:
                # Battery/payload exhausted mid-field: swap and resume.
                m.status = "returning"
                self.resume_mission = m
            else:
                m.status = "done" if not incomplete else "partial"
                m.ended_ts = ts
                self.resume_mission = None
                self.area_sprayed_ha += m.target_area_ha * (
                    sum(min(1.0, v) for v in m.coverage.values())
                    / max(len(m.coverage), 1))
                self.missions_done += 1
            self.bus.publish(self.bus.topic("drone", self.device_id, "mission"), {
                "value": 1, "ref": m.ref, "event": "complete",
                "status": m.status, "plots": len(m.dose_ml_ha),
                "sortie": m.sorties, "area_ha": round(m.target_area_ha, 2),
                "sprayed_litres": round(m.sprayed_litres, 2),
                "mean_coverage": m.notes["mean_coverage"],
                "battery_used": round((m.battery_start - self.battery) * 100, 1),
                "deposition": m.notes.get("deposition"), "ts": ts})
            self.mission = None
        self.x, self.y = self.pad
        self.z = 0.0
        self.speed = 0.0
        self.state = "servicing"
        # Charging + refill time scales with how much was used.
        used_pct = 1.0 - self.battery
        self.service_seconds_left = 60.0 * (used_pct * self.FLIGHT_MINUTES * 0.8 + 2.0)
        self.status = "servicing"

    def _update_battery(self, dt_sim_s: float, spraying: bool) -> None:
        """Battery burn: hover + payload + spray pump + wind + payload mass."""
        base = 1.0 / (self.FLIGHT_MINUTES * 60.0)
        payload_factor = 1.0 + 0.55 * (self.payload_l / self.CAPACITY_L)
        spray_factor = 1.25 if spraying else 1.0
        move_factor = 0.85 + 0.3 * (self.speed / max(self.CRUISE_MS, 1e-6))
        self.battery = max(0.0, self.battery - base * dt_sim_s * payload_factor * spray_factor * move_factor)
        if self.battery < self.BATTERY_MIN_RETURN and self.state in ("transit", "spraying"):
            if self.mission:
                self.mission.notes["rlt_reason"] = "battery"
            self.state = "rtl"

    def _publish_telemetry(self, ts: float, wind_speed: float) -> None:
        self.status = "online" if self.state != "servicing" else "servicing"
        self.bus.publish(self.bus.topic("drone", self.device_id, "telemetry"), {
            "value": self.battery * 100, "battery": round(self.battery * 100, 1),
            "payload_l": round(self.payload_l, 2), "state": self.state,
            "x": round(self.x, 1), "y": round(self.y, 1), "z": round(self.z, 2),
            "heading": round(self.heading, 1), "speed": round(self.speed, 2),
            "mission": self.mission.ref if self.mission else None,
            "wind": round(wind_speed, 2), "ts": ts})

    # ---- state ------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Serialisable autopilot state for the API and dashboard."""
        return {
            "id": self.device_id, "name": self.name, "state": self.state,
            "status": self.status, "x": round(self.x, 1), "y": round(self.y, 1),
            "z": round(self.z, 2), "heading": round(self.heading, 1),
            "speed": round(self.speed, 2), "battery": round(self.battery * 100, 1),
            "payload_l": round(self.payload_l, 2), "capacity_l": self.CAPACITY_L,
            "payload_pct": round(100.0 * self.payload_l / self.CAPACITY_L, 1),
            "auto": self.auto, "missions_done": self.missions_done,
            "area_sprayed_ha": round(self.area_sprayed_ha, 2),
            "flight_minutes": round(self.flight_seconds / 60.0, 1),
            "pad": list(self.pad), "trail": [[round(x, 1), round(y, 1)] for x, y in self.trail[-120:]],
            "mission": self.mission.as_dict() if self.mission else None,
            "last_error": self.last_error,
        }