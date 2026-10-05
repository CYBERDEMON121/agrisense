"""Field model: plots, soil water balance, phenology and pest pressure.

Everything is integrated on the simulated clock in small steps, so behaviour is
identical whether the sim runs at 1x or 600x.

Soil water balance (FAO-56 single-crop-coefficient method)::

    Depletion D(t) = D(t-1) + ETc - Pe - I

    theta(t) = theta_FC - D(t) / root_depth

Phenylogy uses accumulated thermal time (GDD base temperature per crop) and pest
pressure is a logistic process modulated by temperature and humidity, knocked
down by pesticide applications with a decaying residual effect.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from .crops import CropProfile, get_crop
from .weather import WeatherState


@dataclass(slots=True)
class Plot:
    """One irrigated block of crop with homogeneous soil and sensors."""

    plot_id: str
    name: str
    field_id: str
    area_id: str
    crop_key: str
    area_m2: float
    col: int
    row: int
    x: float          # farm-frame metres, origin at the SW corner
    y: float
    width: float
    height: float
    zone: str
    sensor_id: str
    camera_id: str | None = None

    # --- soil ---------------------------------------------------------
    theta: float = 0.26          # volumetric water content m3/m3
    field_capacity: float = 0.31
    wilting_point: float = 0.12
    ec: float = 1.1              # dS/m
    soil_temp: float = 28.0
    #: Per-plot coefficient on incoming water.  Real blocks are never delivered
    #: to perfectly: riser pressure varies across a zone and the emitters are
    #: not identical, so the same commanded depth lands unevenly.  This is the
    #: single most important reason two plots in one zone can need irrigation at
    #: different times.
    irrigation_efficiency: float = 1.0
    #: Coefficient on crop water use.  Slope, aspect and a nearby hedge change
    #: how much a block transpires for the same weather.
    et_factor: float = 1.0
    #: Fraction of rainfall that infiltrates rather than running off.  Low on the
    #: steep, compacted end of a block; high where the soil is open.
    infiltration: float = 0.82
    #: How much water the plot can hold once drainage is accounted for, per unit
    #: area of ground it occupies.  An orchard tile is mostly bare soil between
    #: trees, so it wets and drains far faster than a closed cotton canopy.
    root_area_factor: float = 1.0

    # --- plant --------------------------------------------------------
    gdd: float = 120.0
    health: float = 0.92
    canopy: float = 0.15         # 0..1 canopy cover proxy
    biomass: float = 0.08        # t/ha proxy
    water_stress: float = 0.0    # 0..1 accumulated stress
    heat_stress: float = 0.0
    nutrient: float = 0.85

    # --- pests / disease ----------------------------------------------
    pest_pressure: float = 0.06
    pest_residual_days: float = 0.0   # pesticide protection remaining
    disease_pressure: float = 0.03
    leaf_wetness: float = 0.0

    # --- management ---------------------------------------------------
    irrigated_ts: float | None = None
    last_spray_ts: float | None = None
    spray_count: int = 0
    yield_loss: float = 0.0
    growth_dt: float = 0.0

    # ------------------------------------------------------------------
    @property
    def crop(self) -> CropProfile:
        return get_crop(self.crop_key)

    @property
    def area_ha(self) -> float:
        return self.area_m2 / 10_000.0

    @property
    def theta_fc(self) -> float:
        return self.field_capacity

    @property
    def theta_wp(self) -> float:
        return self.wilting_point

    @property
    def root_depth_m(self) -> float:
        """Effective rooting depth: crop depth scaled by how much ground the
        crop actually occupies."""
        return self.crop.root_depth_m * self.root_area_factor

    @property
    def taw_mm(self) -> float:
        """Total available water in the root zone, mm."""
        return (self.field_capacity - self.wilting_point) * self.root_depth_m * 1000.0

    @property
    def depletion_mm(self) -> float:
        return max(0.0, (self.field_capacity - self.theta) * self.root_depth_m * 1000.0)

    @property
    def depletion_fraction(self) -> float:
        taw = self.taw_mm
        return min(1.0, self.depletion_mm / taw) if taw > 0 else 1.0

    @property
    def stage_index(self) -> int:
        return self.crop.stage_index(self.gdd)

    @property
    def stage_name(self) -> str:
        return self.crop.stage(self.gdd).name

    @property
    def kc(self) -> float:
        return self.crop.kc(self.gdd)

    @property
    def cX(self) -> float:
        """Crop coefficient x normalised stage factor used by the UI."""
        return self.kc

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    @property
    def moisture_fraction(self) -> float:
        """Available soil water as a fraction of field capacity (0..1)."""
        return max(0.0, min(1.0,
                            (self.theta - self.wilting_point)
                            / max(self.field_capacity - self.wilting_point, 1e-6)))

    @property
    def moisture_pct(self) -> float:
        return 100.0 * self.moisture_fraction

    def state(self) -> dict[str, Any]:
        return {
            "id": self.plot_id, "name": self.name, "field": self.area_id, "area_id": self.area_id,
            "crop": self.crop_key, "crop_label": self.crop.label, "zone": self.zone,
            "area_ha": round(self.area_ha, 3), "col": self.col, "row": self.row,
            "x": self.x, "y": self.y, "w": self.width, "h": self.height,
            "moisture": round(self.moisture_pct, 1), "theta": round(self.theta, 4),
            "depletion": round(self.depletion_fraction, 3),
            "temp": round(self.soil_temp, 1), "ec": round(self.ec, 2),
            "health": round(self.health, 3), "pest": round(self.pest_pressure, 3),
            "disease": round(self.disease_pressure, 3), "canopy": round(self.canopy, 3),
            "stage": self.stage_index, "stage_name": self.stage_name,
            "gdd": round(self.gdd, 1), "yield_loss": round(self.yield_loss, 3),
            "water_stress": round(self.water_stress, 3), "heat_stress": round(self.heat_stress, 3),
            "spray_count": self.spray_count, "sensor": self.sensor_id, "camera": self.camera_id,
            "sprayed_recently": self._sprayed_recently,
        }

    @property
    def _sprayed_recently(self) -> bool:
        return self.pest_residual_days > 0.0


#: Each area's character: soil texture, how evenly its riser delivers, where the
#: last irrigation cycle left it, and its baseline pest pressure.  Hand-set
#: rather than drawn, because these are the differences a farmer would point at
#: when asked why the orchard needs watering twice as often as the cotton block.
AREA_CHARACTER: dict[str, dict[str, float]] = {
    # Heavy alluvial clay, the best riser on the farm, planted late.
    "north": {"texture": 0.78, "delivery": 0.95, "start_depletion": 0.42,
              "pest_pressure": 0.05, "disease_pressure": 0.035, "gdd": 96.0},
    # Loam with a worn pump at the far end: uneven water, early whitefly.
    "south": {"texture": 0.55, "delivery": 0.82, "start_depletion": 0.30,
              "pest_pressure": 0.10, "disease_pressure": 0.025, "gdd": 148.0},
    # Sandy loam, wide spacing, a rust belt that follows the low corner.
    "east": {"texture": 0.30, "delivery": 0.90, "start_depletion": 0.22,
             "pest_pressure": 0.04, "disease_pressure": 0.060, "gdd": 205.0},
    # Sandy under 400 m2 tiles: the whole orchard drains, so it swings.
    "orchard": {"texture": 0.22, "delivery": 0.76, "start_depletion": 0.36,
                "pest_pressure": 0.08, "disease_pressure": 0.045, "gdd": 520.0},
}


@dataclass(slots=True)
class Area:

    area_id: str
    name: str
    crop_key: str
    x: float
    y: float
    width: float
    height: float
    zone: str
    camera_id: str
    color: str
    rows: int
    cols: int
    #: Soil texture index, ~0..1 (sandy at the low end, clay at the high end).
    texture: float = 0.5
    #: How evenly this area's irrigation riser delivers, 0..1.
    delivery: float = 0.9
    #: Where the previous irrigation cycle left the root zone, 0..1 of TAW
    #: depleted.  The farm does not start every block at the same moisture.
    start_depletion: float = 0.35
    #: Baseline pest and disease pressure for the area.
    pest_pressure: float = 0.06
    disease_pressure: float = 0.03
    #: Thermal time already accumulated, days after planting.
    gdd: float = 120.0

    def state(self) -> dict[str, Any]:
        return {"id": self.area_id, "name": self.name, "crop": self.crop_key,
                "x": self.x, "y": self.y, "w": self.width, "h": self.height,
                "zone": self.zone, "camera": self.camera_id, "color": self.color,
                "rows": self.rows, "cols": self.cols}


class FieldModel:
    """Owns every plot and advances the agronomic state each simulation step."""

    def __init__(self, rng: random.Random, start_ts: float, latitude: float) -> None:
        self.rng = rng
        self.lat = latitude
        self.plots: list[Plot] = []
        self.by_id: dict[str, Plot] = {}
        self.areas: dict[str, Area] = {}
        self.zones: dict[str, list[str]] = {}
        #: Irrigation water credited to each plot by the irrigation system.
        self._pending_irrigation_mm: dict[str, float] = {}
        self._build_layout()

    # ---- layout -------------------------------------------------------
    def _build_layout(self) -> None:
        specs = [
            # area_id, name, crop, x, y, w, h, cols, rows, zone, camera, colour
            ("north", "North Field", "cotton", 0, 260, 240, 150, 4, 3, "z1", "cam-north", "#4a7c3a"),
            ("south", "South Field", "tomato", 0, 0, 150, 150, 3, 3, "z1", "cam-south", "#3f8f4a"),
            ("east", "East Field", "wheat", 320, 0, 240, 100, 4, 2, "z2", "cam-east", "#7fa03c"),
            ("orchard", "Mango Orchard", "mango", 320, 190, 90, 90, 3, 3, "z3", "cam-orchard", "#2f6b34"),
        ]
        for area_id, name, crop, ax, ay, aw, ah, cols, rows, zone, camera, color in specs:
            self.areas[area_id] = Area(area_id, name, crop, ax, ay, aw, ah, zone, camera,
                                       color, rows, cols)
            # Each area gets its own soil, riser and starting condition.  The
            # values are hand-set rather than drawn because they are the farm's
            # character - a reader comparing two areas should see a deliberate
            # difference (the orchard is sandier and dries out; the cotton block
            # is heavy clay that holds water), not noise.
            area = self.areas[area_id]
            for key, value in AREA_CHARACTER.get(area_id, {}).items():
                setattr(area, key, value)
            pad = 4.0
            pw = (aw - pad * (cols + 1)) / cols
            ph = (ah - pad * (rows + 1)) / rows
            # Orchard plots are individual trees; other crops are blocks.
            for r in range(rows):
                for c in range(cols):
                    pid = f"{area_id}-{r * cols + c + 1:02d}"
                    x = ax + pad + c * (pw + pad)
                    y = ay + pad + (rows - 1 - r) * (ph + pad)   # r=0 at the north edge
                    plot = Plot(
                        plot_id=pid,
                        name=f"{name} {r * cols + c + 1:02d}",
                        field_id=area_id, area_id=area_id, crop_key=crop,
                        area_m2=pw * ph, col=c, row=r, x=x, y=y, width=pw, height=ph,
                        zone=zone, sensor_id=f"sm-{pid}", camera_id=camera,
                    )
                    if crop == "mango":
                        plot.area_m2 = 400.0
                    self.plots.append(plot)
                    self.by_id[pid] = plot
        for zone in {p.zone for p in self.plots}:
            self.zones[zone] = [p.plot_id for p in self.plots if p.zone == zone]
        for plot in self.plots:
            self._vary(plot)

    # ---- spatial heterogeneity ------------------------------------------
    def _vary(self, plot: Plot) -> None:
        """Give every block its own soil, its own water history and its own foci.

        Without this, all plots in an area start from the same numbers and are
        driven by the same farm-wide weather, so they stay identical for the
        whole season: the map would be one flat colour, and "irrigate the driest
        block" would be indistinguishable from "irrigate any block".  Real fields
        are patchy, and the per-block sensors exist precisely to see that.

        Draws are correlated within an area and independent between areas, which
        is how patches actually look - a low corner is a low corner for a run of
        blocks, not for one block and its opposite neighbour.
        """
        area = self.areas[plot.area_id]
        rng = self.rng

        # Soil texture: clay holds more water than sand, and starts wetter.
        texture = area.texture + (plot.col - (area.cols - 1) / 2) * 0.055 \
            + (plot.row - (area.rows - 1) / 2) * -0.035 + rng.gauss(0, 0.045)
        plot.field_capacity = round(_clamp(texture * 0.075 + 0.245, 0.24, 0.38), 4)
        plot.wilting_point = round(_clamp(plot.field_capacity * 0.34 + rng.gauss(0, 0.006),
                                          0.07, 0.17), 4)

        # Hydrophobicity / compaction: how much of the rain a block keeps.
        plot.infiltration = round(_clamp(0.86 - 0.22 * (plot.field_capacity - 0.30)
                                         + rng.gauss(0, 0.03), 0.5, 0.97), 4)

        # Riser pressure falls off along the run and emitters differ, so the same
        # commanded depth lands unevenly.  This is the dominant source of
        # within-zone spread and it never washes out.
        plot.irrigation_efficiency = round(_clamp(
            area.delivery + 0.10 * (plot.col / max(area.cols - 1, 1) - 0.5)
            + rng.gauss(0, 0.035), 0.62, 1.03), 4)

        # Aspect and shelter: a block next to a windbreak transpires less.
        plot.et_factor = round(_clamp(1.0 + rng.gauss(0, 0.055), 0.85, 1.18), 4)

        # An orchard tile is a 400 m2 square of open ground under one tree: the
        # wetted fraction and the root depth are both smaller than a closed
        # block's, so it swings faster between watering and drought.
        if plot.crop_key == "mango":
            plot.root_area_factor = 0.42
        else:
            plot.root_area_factor = round(_clamp(1.0 + rng.gauss(0, 0.09), 0.8, 1.2), 4)

        # Starting moisture: the previous irrigation cycle did not finish
        # everywhere.  Spread it as a per-area fraction of available water rather
        # than in raw theta, so blocks with different soils stay comparable.
        taw = plot.taw_mm
        plot.theta = round(_clamp(
            plot.field_capacity - taw / 1000.0 * _clamp(
                area.start_depletion + rng.gauss(0, 0.16) - 0.05 * plot.row, 0.05, 0.95),
            plot.wilting_point + 0.01, plot.field_capacity), 4)

        plot.ec = round(_clamp(1.05 + rng.gauss(0, 0.28), 0.4, 3.0), 3)
        plot.soil_temp = round(26.0 + rng.gauss(0, 1.4), 2)
        plot.gdd = round(_clamp(area.gdd + rng.gauss(0, 22.0), 0.0, 4000.0), 1)
        plot.canopy = round(_clamp(0.15 + rng.gauss(0, 0.05), 0.04, 0.4), 3)
        plot.biomass = round(_clamp(0.08 + rng.gauss(0, 0.03), 0.02, 0.3), 4)
        plot.health = round(_clamp(0.92 + rng.gauss(0, 0.05), 0.5, 1.0), 4)
        plot.nutrient = round(_clamp(0.85 + rng.gauss(0, 0.07), 0.4, 1.0), 4)

        # Pests arrive as foci - a hotspot plus a diffuse background - so there
        # is always somewhere for the spray rule to be right about, and somewhere
        # for it to be wrong.
        hotspot = max(0.0, rng.gauss(area.pest_pressure + 0.10, 0.13))
        plot.pest_pressure = round(_clamp(hotspot ** 1.4 * 1.6, 0.01, 0.95), 4)
        # Fungal disease establishes where water sits: in the low corner of a
        # block (row 0 is the north edge, so the last row is the low ground) and
        # wherever the block is already struggling.
        low_ground = plot.row / max(area.rows - 1, 1)
        plot.disease_pressure = round(_clamp(
            area.disease_pressure + 0.05 * low_ground + rng.gauss(0, 0.02)
            + 0.04 * plot.depletion_fraction, 0.005, 0.6), 4)

    # ---- simulation step ----------------------------------------------
    def step(self, weather: WeatherState, dt_hours: float) -> None:
        """Advance every plot by ``dt_hours`` of simulated time."""
        if dt_hours <= 0:
            return
        for plot in self.plots:
            self._step_plot(plot, weather, dt_hours)

    def _step_plot(self, plot: Plot, w: WeatherState, dt_hours: float) -> None:
        crop = plot.crop
        dt_days = dt_hours / 24.0

        # --- soil temperature lags air temperature (daily time constant) ---
        tau = 6.0
        k = 1.0 - math.exp(-dt_hours / tau)
        plot.soil_temp += (w.temp - plot.soil_temp) * k + self.rng.gauss(0, 0.03)

        # --- phenology: thermal time accumulation ---
        tmean = plot.soil_temp
        # Thermal time is accumulated per DAY (daily mean temperature), so the
        # hourly increment must be scaled by dt_days.
        gdd_gain = max(0.0, tmean - crop.t_base) * dt_days
        # Water/heat stress slows development.
        stress = max(plot.water_stress, plot.heat_stress)
        plot.gdd += gdd_gain * (1.0 - 0.35 * min(stress, 1.0))
        plot.growth_dt = gdd_gain

        # --- water balance ------------------------------------------------
        # Everything here is per plot on purpose.  Two blocks that share a zone
        # still differ in how much of the water they keep, how fast they use it,
        # and how evenly the riser reaches them - which is precisely why the
        # irrigation rule looks for the driest plot rather than an average.
        etc_daily = w.et0 * plot.kc * plot.et_factor
        etc = etc_daily * dt_days

        # Effective rainfall: a fraction infiltrates, the rest runs off.
        effective_rain = w.rainfall * plot.infiltration
        # Irrigation is applied by the irrigation system; attribute it here.
        gross = self._pending_irrigation_mm.get(plot.plot_id, 0.0)
        self._pending_irrigation_mm[plot.plot_id] = 0.0
        applied = gross * plot.irrigation_efficiency

        root_depth = crop.root_depth_m * plot.root_area_factor
        percolation = 0.0
        new_theta = plot.theta + (effective_rain + applied - etc) / (root_depth * 1000.0)
        if new_theta > plot.field_capacity:
            percolation = (new_theta - plot.field_capacity) * root_depth * 1000.0
            new_theta = plot.field_capacity
        plot.theta = max(plot.wilting_point * 0.92, new_theta)

        # Water stress drives stomatal closure -> health and growth penalty.
        df = plot.depletion_fraction
        target_water_stress = max(0.0, (df - 0.45) / 0.55) ** 1.5
        plot.water_stress += (target_water_stress - plot.water_stress) * min(1.0, dt_hours * 1.5)

        # --- heat stress --------------------------------------------------
        target_heat = max(0.0, (plot.soil_temp - crop.t_stress) / 8.0) ** 1.3
        plot.heat_stress += (target_heat - plot.heat_stress) * min(1.0, dt_hours)

        # --- canopy / biomass ---------------------------------------------
        stage_progress = crop.progress(plot.gdd)
        season_progress = min(1.0, plot.gdd / crop.stages[-1].gdd)
        canopy_target = min(1.0, 0.12 + 1.05 * season_progress) * (1.0 - 0.55 * season_progress ** 4)
        canopy_target = max(0.05, canopy_target)
        plot.canopy += (canopy_target - plot.canopy) * min(1.0, dt_hours * 0.55)
        # Leaf area drives transpiration slightly above the Kc curve.
        photosynthesis = 1.0 if crop.t_opt_min <= tmean <= crop.t_opt_max else max(
            0.0, 1.0 - abs(tmean - (crop.t_opt_min + crop.t_opt_max) / 2) / 10.0)
        gain = photosynthesis * (1.0 - 0.6 * plot.water_stress) * (1.0 - 0.5 * plot.heat_stress)
        plot.biomass = max(0.02, plot.biomass + gain * 0.012 * dt_hours * (0.4 + plot.canopy))

        # --- pests ---------------------------------------------------------
        self._step_pest(plot, w, dt_days)

        # --- disease -------------------------------------------------------
        wetness_target = min(1.0, w.humidity * 0.85 + (0.4 if w.rainfall > 0.05 else 0.0))
        plot.leaf_wetness += (wetness_target - plot.leaf_wetness) * min(1.0, dt_hours * 0.7)
        disease_rate = crop.disease_rate * _bell(w.temp, crop.disease_opt_temp, 9.0) * plot.leaf_wetness
        disease_rate *= (1.0 - 0.7 * min(1.0, plot.pest_pressure))
        plot.disease_pressure = min(1.0, plot.disease_pressure + disease_rate * dt_days * (0.4 + plot.canopy))
        plot.disease_pressure = max(plot.disease_pressure, 0.0)

        # --- health integrates every stress source -------------------------
        pest_damage = crop.pest_damage_per_unit * plot.pest_pressure * plot.canopy
        disease_damage = 0.45 * plot.disease_pressure * plot.canopy
        damage_rate = pest_damage + disease_damage
        # Recovery scales with canopy vigour; stress and damage both cost health.
        recovery = 0.055 * (0.35 + 0.65 * plot.canopy) * (1.0 - plot.water_stress) * (1.0 - plot.heat_stress)
        health_delta = (
            recovery
            - damage_rate * 0.5
            - 0.45 * plot.water_stress
            - 0.30 * plot.heat_stress
            - 0.02 * max(0.0, plot.ec - 2.0)
        ) * dt_days
        plot.health = max(0.02, min(1.0, plot.health + health_delta))
        plot.yield_loss = min(1.0, plot.yield_loss + damage_rate * 0.55 * dt_days)

        # --- salt accumulation / nutrient drawdown ------------------------
        ec_gain = 0.0006 * dt_hours * (etc_daily / 6.0) / max(plot.canopy, 0.15)
        plot.ec = max(0.4, min(3.5, plot.ec + ec_gain))
        plot.nutrient = max(0.25, min(1.0, plot.nutrient - 0.0015 * dt_hours * plot.canopy))

        del stage_progress, percolation, gain

    def _step_pest(self, plot: Plot, w: WeatherState, dt_days: float) -> None:
        crop = plot.crop
        # Logistic growth gated by temperature and humidity.
        temp_factor = _bell(w.temp, crop.pest_opt_temp, crop.pest_temp_width)
        hum_factor = 1.0 - crop.pest_humidity_weight * (1.0 - w.humidity)
        carrying = 1.0
        r = crop.pest_r_max * temp_factor * max(0.15, hum_factor)
        r *= crop.stage(plot.gdd).pest_susceptibility
        r *= 0.5 + 0.5 * plot.canopy          # young/emerged canopy shelters fewer pests
        if plot.pest_residual_days > 0:
            r *= 0.05                        # residual protection suppresses the rate
            plot.pest_residual_days = max(0.0, plot.pest_residual_days - dt_days)
        # Natural control: beneficials are limited by extreme heat.
        natural = 0.02 * max(0.0, 1.0 - max(0.0, w.temp - 36.0) / 6.0)
        p = plot.pest_pressure
        dp = r * p * (1.0 - p / carrying) - natural * p - 0.03 * p * dt_days * 0
        plot.pest_pressure = max(0.0, min(1.0, p + dp * dt_days))

    # ---- management operations -----------------------------------------
    def queue_irrigation(self, plot_id: str, mm: float) -> None:
        self._pending_irrigation_mm[plot_id] = self._pending_irrigation_mm.get(plot_id, 0.0) + mm

    def apply_pesticide(self, plot_id: str, coverage: float, efficacy: float,
                        dose_ml_ha: float, ts: float) -> tuple[float, float]:
        """Return (pressure_before, pressure_after) after a spray pass."""
        plot = self.by_id.get(plot_id)
        if plot is None:
            return 0.0, 0.0
        before = plot.pest_pressure
        effective = max(0.0, min(1.0, coverage))
        kill = efficacy * effective
        plot.pest_pressure = max(0.0, before * (1.0 - kill))
        plot.disease_pressure = max(0.0, plot.disease_pressure * (1.0 - 0.45 * kill))
        # Residual protection lasts longer at a higher dose.
        plot.pest_residual_days = max(plot.pest_residual_days, min(12.0, 2.0 + dose_ml_ha / 220.0))
        plot.last_spray_ts = ts
        plot.spray_count += 1
        return before, plot.pest_pressure

    # ---- aggregates ----------------------------------------------------
    def stats(self) -> dict[str, float]:
        n = len(self.plots)
        if not n:
            return {}
        return {
            "plots": n,
            "moisture_avg": sum(p.moisture_pct for p in self.plots) / n,
            "moisture_min": min(p.moisture_pct for p in self.plots),
            "moisture_max": max(p.moisture_pct for p in self.plots),
            "health_avg": sum(p.health for p in self.plots) / n,
            "pest_avg": sum(p.pest_pressure for p in self.plots) / n,
            "pest_max": max(p.pest_pressure for p in self.plots),
            "disease_avg": sum(p.disease_pressure for p in self.plots) / n,
            "depletion_avg": sum(p.depletion_fraction for p in self.plots) / n,
            "canopy_avg": sum(p.canopy for p in self.plots) / n,
            "area_ha": sum(p.area_ha for p in self.plots),
            "yield_loss_avg": sum(p.yield_loss for p in self.plots) / n,
        }

    def plots_in_area(self, area_id: str) -> list[Plot]:
        return [p for p in self.plots if p.area_id == area_id]

    def plots_in_zone(self, zone: str) -> list[Plot]:
        return [p for p in self.plots if p.zone == zone]

    def worst_plots(self, limit: int = 5) -> list[Plot]:
        return sorted(self.plots, key=lambda p: -p.pest_pressure)[:limit]

    def states(self) -> list[dict[str, Any]]:
        return [p.state() for p in self.plots]


def _bell(x: float, centre: float, width: float) -> float:
    """Gaussian response curve normalised to 1.0 at the centre."""
    return math.exp(-((x - centre) ** 2) / (2.0 * max(width, 1e-6) ** 2))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))