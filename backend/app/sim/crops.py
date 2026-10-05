"""Crop profiles: phenology, water use, pest and disease characteristics.

Parameters follow FAO / IRRI reference ranges so the simulated behaviour stays
physically plausible (a cotton boll stage needs ~900-1200 GDD, wheat is a
long-day C3 crop, mango is a perennial with year-round canopy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    gdd: float          # cumulative thermal time to enter this stage
    kc: float           # FAO-56 single crop coefficient
    pest_susceptibility: float  # multiplier on pest establishment rate


@dataclass(frozen=True, slots=True)
class CropProfile:
    key: str
    label: str
    t_base: float               # base temperature for GDD (C)
    t_opt_min: float            # temperature band for maximum photosynthesis
    t_opt_max: float
    t_stress: float             # above this, heat stress accrues
    root_depth_m: float
    stages: Sequence[Stage]
    kc_max: float
    # pest
    pest_name: str
    pest_r_max: float           # logistic rate per day at optimum conditions
    pest_opt_temp: float
    pest_temp_width: float
    pest_humidity_weight: float  # how much humidity drives the outbreak
    pest_damage_per_unit: float   # yield loss per unit pressure at full canopy
    # disease
    disease_name: str
    disease_opt_temp: float
    disease_humidity_opt: float
    disease_rate: float
    # appearance
    canopy_color: str
    row_spacing_m: float
    plant_height_m: float
    season_days: int

    def stage_index(self, gdd: float) -> int:
        idx = 0
        for i, st in enumerate(self.stages):
            if gdd >= st.gdd:
                idx = i
        return idx

    def stage(self, gdd: float) -> Stage:
        return self.stages[self.stage_index(gdd)]

    def progress(self, gdd: float) -> float:
        """Fraction (0..1) through the current stage."""
        st = self.stage(gdd)
        nxt = self.stages[self.stage_index(gdd) + 1] if self.stage_index(gdd) + 1 < len(self.stages) else None
        lo, hi = st.gdd, nxt.gdd if nxt else st.gdd * 1.25
        return max(0.0, min(1.0, (gdd - lo) / max(hi - lo, 1e-6)))

    def kc(self, gdd: float) -> float:
        st = self.stage(gdd)
        p = self.progress(gdd)
        nxt = self.stages[self.stage_index(gdd) + 1] if self.stage_index(gdd) + 1 < len(self.stages) else None
        if nxt is None:
            return max(st.kc, self.kc_max * (1.0 - p))
        return st.kc + (nxt.kc - st.kc) * p


COTTON = CropProfile(
    key="cotton", label="Cotton", t_base=15.0, t_opt_min=22, t_opt_max=35, t_stress=38,
    root_depth_m=1.0,
    stages=(
        Stage("emergence", 60, 0.35, 0.8),
        Stage("vegetative", 260, 0.75, 1.0),
        Stage("squaring", 520, 1.05, 1.15),
        Stage("flowering", 760, 1.20, 1.25),
        Stage("boll", 1000, 1.10, 1.0),
        Stage("maturity", 1300, 0.65, 0.5),
    ),
    kc_max=1.2,
    pest_name="bollworm", pest_r_max=0.55, pest_opt_temp=28.0, pest_temp_width=7.0,
    pest_humidity_weight=0.45, pest_damage_per_unit=0.55,
    disease_name="leaf spot", disease_opt_temp=26.0, disease_humidity_opt=0.85, disease_rate=0.055,
    canopy_color="#4a7c3a", row_spacing_m=1.2, plant_height_m=1.4, season_days=165,
)

TOMATO = CropProfile(
    key="tomato", label="Tomato", t_base=10.0, t_opt_min=20, t_opt_max=30, t_stress=36,
    root_depth_m=0.6,
    stages=(
        Stage("establishment", 90, 0.45, 0.9),
        Stage("vegetative", 300, 0.85, 1.0),
        Stage("flowering", 560, 1.10, 1.2),
        Stage("fruit set", 780, 1.05, 1.25),
        Stage("ripening", 1000, 0.85, 0.8),
        Stage("senescence", 1200, 0.55, 0.4),
    ),
    kc_max=1.15,
    pest_name="whitefly", pest_r_max=0.72, pest_opt_temp=30.0, pest_temp_width=8.0,
    pest_humidity_weight=0.30, pest_damage_per_unit=0.60,
    disease_name="early blight", disease_opt_temp=27.0, disease_humidity_opt=0.80, disease_rate=0.070,
    canopy_color="#3f8f4a", row_spacing_m=0.9, plant_height_m=1.1, season_days=120,
)

WHEAT = CropProfile(
    key="wheat", label="Wheat", t_base=8.0, t_opt_min=15, t_opt_max=26, t_stress=33,
    root_depth_m=1.2,
    stages=(
        Stage("germination", 130, 0.40, 0.85),
        Stage("tillering", 420, 0.95, 1.0),
        Stage("stem extension", 820, 1.20, 1.1),
        Stage("heading", 1180, 1.05, 1.0),
        Stage("grain fill", 1420, 0.70, 0.6),
        Stage("ripe", 1650, 0.35, 0.3),
    ),
    kc_max=1.2,
    pest_name="aphid", pest_r_max=0.62, pest_opt_temp=22.0, pest_temp_width=8.0,
    pest_humidity_weight=0.35, pest_damage_per_unit=0.45,
    disease_name="rust", disease_opt_temp=21.0, disease_humidity_opt=0.88, disease_rate=0.085,
    canopy_color="#57a03c", row_spacing_m=0.2, plant_height_m=0.9, season_days=135,
)

MANGO = CropProfile(
    key="mango", label="Mango", t_base=12.0, t_opt_min=20, t_opt_max=34, t_stress=40,
    root_depth_m=1.5,
    stages=(
        Stage("flowering", 220, 0.55, 1.0),
        Stage("fruit set", 700, 0.75, 1.15),
        Stage("fruit growth", 1400, 0.95, 1.1),
        Stage("ripening", 2200, 1.00, 0.85),
        Stage("post harvest", 2700, 0.80, 0.5),
    ),
    kc_max=1.0,
    pest_name="mango hopper", pest_r_max=0.48, pest_opt_temp=27.0, pest_temp_width=7.0,
    pest_humidity_weight=0.40, pest_damage_per_unit=0.50,
    disease_name="anthracnose", disease_opt_temp=26.0, disease_humidity_opt=0.90, disease_rate=0.065,
    canopy_color="#2f6b34", row_spacing_m=8.0, plant_height_m=6.0, season_days=365,
)

CROPS: dict[str, CropProfile] = {c.key: c for c in (COTTON, TOMATO, WHEAT, MANGO)}


def get_crop(key: str) -> CropProfile:
    return CROPS.get(key, COTTON)