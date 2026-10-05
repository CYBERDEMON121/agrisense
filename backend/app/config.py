"""Central configuration for the AgriSense platform.

Every value can be overridden with an ``AGRISENSE_*`` environment variable so the
same code runs the fast simulator (tests, CI) and a full farm deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2]  # /home/kali/subash
DATA_DIR = Path(os.getenv("AGRISENSE_DATA_DIR", BASE_DIR / "backend" / "data"))


def _env(name: str, default: Any) -> Any:
    raw = os.getenv(f"AGRISENSE_{name}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass(slots=True)
class Settings:
    # ---- server -------------------------------------------------------
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env("PORT", 8000))
    reload: bool = field(default_factory=lambda: _env("RELOAD", False))

    # ---- identity -----------------------------------------------------
    farm_id: str = field(default_factory=lambda: _env("FARM_ID", "green-valley-01"))
    farm_name: str = field(default_factory=lambda: _env("FARM_NAME", "Green Valley Farms"))
    latitude: float = field(default_factory=lambda: _env("LATITUDE", 17.3850))  # Hyderabad, IN
    longitude: float = field(default_factory=lambda: _env("LONGITUDE", 78.4867))
    timezone_offset_h: float = field(default_factory=lambda: _env("TZ_OFFSET", 5.5))

    # ---- simulation ---------------------------------------------------
    seed: int = field(default_factory=lambda: _env("SEED", 20261005))
    tick_hz: float = field(default_factory=lambda: _env("TICK_HZ", 2.0))
    start_epoch: float = field(default_factory=lambda: _env("START_EPOCH", 1767238200.0))  # 09:00 IST
    #: 1.0 = real time. 60 = one simulated hour per wall-clock minute.
    default_speed: float = field(default_factory=lambda: _env("SPEED", 60.0))
    camera_hz: float = field(default_factory=lambda: _env("CAMERA_HZ", 2.0))
    camera_width: int = field(default_factory=lambda: _env("CAM_WIDTH", 384))
    camera_height: int = field(default_factory=lambda: _env("CAM_HEIGHT", 216))
    store_frames: bool = field(default_factory=lambda: _env("STORE_FRAMES", False))

    # ---- persistence --------------------------------------------------
    db_path: Path = field(default_factory=lambda: Path(
        _env("DB_PATH", str(DATA_DIR / "agrisense.db"))))
    telemetry_retention_h: float = field(default_factory=lambda: _env("TELEMETRY_RETENTION_H", 48.0))
    write_telemetry: bool = field(default_factory=lambda: _env("WRITE_TELEMETRY", True))

    # ---- device / integration ----------------------------------------
    mqtt_broker: str | None = field(default_factory=lambda: _env("MQTT_BROKER", None))
    mqtt_port: int = field(default_factory=lambda: _env("MQTT_PORT", 1883))
    mqtt_transport: str = field(default_factory=lambda: _env("MQTT_TRANSPORT", "simulated"))

    # ---- thresholds (shared with the automation engine) --------------
    soil_moisture_low: float = 0.28      # depletion fraction that starts irrigation
    soil_moisture_high: float = 0.55     # hysteresis band that stops it again
    pest_spray_threshold: float = 0.55   # pest pressure that opens a spray mission
    pest_confirm_ticks: int = 3          # consecutive ticks above threshold required
    wind_spray_max_ms: float = 6.0       # above this, spraying is unsafe
    rain_hold_min: int = 30              # no missions within N minutes of rain
    daylight_min_sun: float = 150.0      # W/m2 threshold for legal drone ops

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Path) else value
        return out


def _bootstrap_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


_bootstrap_dirs()
settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)