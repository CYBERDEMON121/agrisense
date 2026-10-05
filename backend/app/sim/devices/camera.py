"""Simulated cameras: synthetic imagery plus a real (numpy) detector.

Two parts:

* ``render`` draws a frame with PIL - sky, soil rows, plants scaled by growth
  stage and canopy, plus lesions drawn in proportion to actual disease pressure,
  so what you see corresponds to the agronomic state.
* ``detect`` runs an HSV segmentation over the rendered pixels with numpy and
  reports lesion bounding boxes.  The detections are therefore *derived from the
  image*, exactly as they would be on real hardware, rather than faked.

Frames are JPEG-encoded and served to the dashboard over MJPEG.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..field import Area, FieldModel, Plot
from ..weather import WeatherState

# Hue windows.  _rgb_to_hsv returns H in 0..179 but S and V normalised 0..1.
# Healthy foliage for every crop lands in hue 52-65; chlorotic leaves fall to
# ~26 and dead tissue to ~20-35, so the healthy class starts at 35 and stressed
# plants drop out of it instead of being counted as fine.
HUE_LEAF_LO = 5
HUE_GREEN = (35, 95)
HUE_LEAF_WILT = (5, 42)
#: Chlorotic and necrotic tissue share a hue band, so brightness separates them:
#: chlorotic foliage is bright yellow, dead tissue is dark.
VAL_CHLOROTIC_MIN = 0.58

#: Chroma gate.  Bare soil is rendered at ~0.20 chroma while foliage - healthy
#: or dead - stays above ~0.35, so this single threshold is what stops the
#: detector from reporting the whole field as blighted.
SAT_FOLIAGE_MIN = 0.35

#: Detection thresholds as a fraction of the canopy footprint.
WILT_ALERT_FRAC = 0.10      # >10% of foliage chlorotic -> water-stress alert
LESION_ALERT_FRAC = 0.035   # >3.5% necrotic tissue -> disease alert
LESION_BOX_FRAC = 0.10      # above this, individual lesion boxes are emitted

#: Colour of fully chlorotic foliage - well outside the healthy green band.
CHLOROTIC_RGB = (222, 200, 60)
#: Colour of dead / diseased foliage.
NECROTIC_RGB = (116, 70, 32)

#: Rendering budgets - keep frame time flat no matter how big the field is.
MAX_ROWS_PER_PLOT = 9
MAX_PLANTS_PER_ROW = 26


def _clamp(v: float, lo: float = 0.0, hi: float = 255.0) -> float:
    return max(lo, min(hi, v))


@dataclass(slots=True)
class Detection:
    label: str
    confidence: float
    bbox: tuple[int, int, int, int]   # x, y, w, h in pixels
    severity: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "confidence": round(self.confidence, 3),
                "bbox": list(self.bbox), "severity": round(self.severity, 3)}


@dataclass(slots=True)
class Camera:
    camera_id: str
    name: str
    kind: str                 # "fixed" | "drone"
    area_id: str | None
    x: float = 0.0            # farm-frame position
    y: float = 0.0
    height_m: float = 6.0
    fov_deg: float = 62.0
    yaw_deg: float = 0.0      # pointing direction
    pitch_deg: float = -18.0
    zoom: float = 1.0
    status: str = "online"
    fps: float = 0.0
    frames: int = 0
    vendor: str = "Hikvision"
    model: str = "DS-2CD2T47"
    fw: str = "5.5.72"
    battery: float = 100.0    # PoV: mains powered, so it stays high
    rng: random.Random = field(default_factory=random.Random)
    last_dets: list[Detection] = field(default_factory=list)
    last_frame: bytes | None = None
    last_ts: float = 0.0

    def device_record(self) -> dict[str, Any]:
        """Registry shape: identity + link quality, as an IoT broker sees it."""
        return {"name": self.name, "kind": "camera", "area": self.area_id,
                "vendor": self.vendor, "model": self.model, "fw": self.fw,
                "status": self.status, "battery": round(self.battery, 1), "rssi": -55,
                "meta": {"kind": self.kind, "height_m": self.height_m,
                         "fov": self.fov_deg, "fps": round(self.fps, 2)}}

    def state(self) -> dict[str, Any]:
        worst = max((d.severity for d in self.last_dets if not d.label.endswith("_summary")
                     and d.label != "ndvi_proxy"), default=0.0)
        return {
            "id": self.camera_id, "name": self.name, "kind": self.kind, "area": self.area_id,
            "status": self.status, "vendor": self.vendor, "model": self.model,
            "fps": round(self.fps, 2), "frames": self.frames,
            "x": round(self.x, 1), "y": round(self.y, 1), "height_m": self.height_m,
            "yaw": round(self.yaw_deg, 1), "pitch": round(self.pitch_deg, 1),
            "fov": self.fov_deg, "battery": round(self.battery, 1), "fw": self.fw,
            "detections": len(self.last_dets),
            "severity": round(worst, 3),
            "ts": self.last_ts,
            # Compact chip list for the dashboard: label + how bad, nothing else.
            "dets": [{"label": d.label, "severity": round(d.severity, 3),
                      "confidence": round(d.confidence, 2)}
                     for d in self.last_dets[:5]],
        }


class CameraArray:
    """Owns every camera and the shared renderer / detector."""

    def __init__(self, farm: FieldModel, rng: random.Random, width: int, height: int) -> None:
        self.farm = farm
        self.rng = rng
        self.width = width
        self.height = height
        self.cameras: dict[str, Camera] = {}
        self._image = Image.new("RGB", (width, height))
        self._draw = ImageDraw.Draw(self._image)
        self._build()

    # ---- layout --------------------------------------------------------
    def _build(self) -> None:
        # Fixed cameras sit outside their field and look at its centre.  Yaw is
        # defined so that the viewing direction is (sin(yaw), cos(yaw)).
        specs = {
            "cam-north": ("North Field overview", "north", 245.0, 415.0, 8.0, 237.0, -22.0),
            "cam-south": ("South Field overview", "south", 155.0, 0.0, 6.0, 313.0, -20.0),
            "cam-east": ("East Field overview", "east", 568.0, 0.0, 7.0, 291.0, -20.0),
            "cam-orchard": ("Orchard watch", "orchard", 365.0, 300.0, 5.0, 180.0, -25.0),
        }
        for cam_id, (name, area_id, x, y, h, yaw, pitch) in specs.items():
            self.cameras[cam_id] = Camera(
                camera_id=cam_id, name=name, kind="fixed", area_id=area_id,
                x=x, y=y, height_m=h, yaw_deg=yaw, pitch_deg=pitch, fov_deg=70.0,
                rng=random.Random(abs(hash(cam_id)) % 2**31),
            )
        # Extra views: the pump house and the farm gate.
        self.cameras["cam-pump"] = Camera(
            camera_id="cam-pump", name="Pump house", kind="fixed", area_id="pumphouse",
            x=270.0, y=380.0, height_m=4.5, yaw_deg=250.0, pitch_deg=-15.0,
            rng=random.Random(11), fov_deg=70.0)
        self.cameras["cam-gate"] = Camera(
            camera_id="cam-gate", name="Farm gate", kind="fixed", area_id="gate",
            x=250.0, y=0.0, height_m=4.0, yaw_deg=0.0, pitch_deg=-12.0,
            rng=random.Random(12), fov_deg=75.0)

    # ---- drone camera is created/destroyed with the flight -------------
    def attach_drone_camera(self, camera_id: str) -> Camera:
        cam = Camera(camera_id=camera_id, name="Drone nose cam", kind="drone", area_id=None,
                     height_m=0.0, fov_deg=84.0, yaw_deg=0.0, pitch_deg=-52.0,
                     rng=random.Random(99))
        self.cameras[camera_id] = cam
        return cam

    def detach(self, camera_id: str) -> None:
        self.cameras.pop(camera_id, None)

    def device_records(self) -> list[dict[str, Any]]:
        return [{"device_id": c.camera_id, **c.device_record(),
                 "meta": {**c.device_record()["meta"],
                          "res": f"{self.width}x{self.height}"}}
                for c in self.cameras.values()]

    # ---- rendering -----------------------------------------------------
    def render(self, cam: Camera, weather: WeatherState, plots: list[Plot],
               dt_real: float, ts: float) -> bytes:
        w, h = self.width, self.height
        img = self._image
        draw = self._draw
        rng = cam.rng
        cam.fps = 0.7 * cam.fps + 0.3 * (1.0 / dt_real if dt_real > 0 else 0)
        cam.frames += 1

        # -- sky gradient, darker and greyer under cloud --------------------
        t_norm = max(0.0, min(1.0, (weather.temp - 18.0) / 20.0))
        top = (int(_clamp(90 + 70 * t_norm - 45 * weather.cloud)),
               int(_clamp(130 + 60 * t_norm - 35 * weather.cloud)),
               int(_clamp(190 + 40 * t_norm - 20 * weather.cloud)))
        bottom = (int(_clamp(150 + 60 * t_norm - 30 * weather.cloud)),
                  int(_clamp(170 + 55 * t_norm - 25 * weather.cloud)),
                  int(_clamp(195 + 35 * t_norm - 15 * weather.cloud)))
        horizon = int(h * (0.42 - 0.06 * math.sin(math.radians(cam.pitch_deg))))
        for y in range(horizon):
            f = y / max(horizon, 1)
            draw.line([(0, y), (w, y)],
                      fill=(int(top[0] + (bottom[0] - top[0]) * f),
                            int(top[1] + (bottom[1] - top[1]) * f),
                            int(top[2] + (bottom[2] - top[2]) * f)))

        # -- rain streaks --------------------------------------------------
        streaks = int(weather.rainfall * 2.2)
        for _ in range(streaks):
            x0 = rng.uniform(0, w)
            y0 = rng.uniform(0, horizon + (h - horizon) * 0.6)
            draw.line([(x0, y0), (x0 - 2, y0 + rng.uniform(6, 16))],
                      fill=(200, 205, 215), width=1)

        # -- ground --------------------------------------------------------
        # Soil is deliberately desaturated (chroma ~0.2): dry earth must never
        # be mistaken for necrotic foliage, which is the same hue but far more
        # chromatic.  Chroma is the discriminator the detector relies on.
        soil_light = _clamp(120 + 26 * t_norm - 30 * weather.cloud)
        soil_base = (int(soil_light * 1.00), int(soil_light * 0.92), int(soil_light * 0.80))
        draw.rectangle([0, horizon, w, h], fill=soil_base)

        # -- plots, back to front so nearer rows occlude farther ones --------
        visible = [p for p in plots if p.canopy > 0.02]
        visible.sort(key=lambda p: p.y)
        for plot in visible:
            self._draw_plot(draw, cam, plot, horizon, weather, rng)

        # -- exposure / weather grade ---------------------------------------
        if weather.cloud > 0.35 or weather.rainfall > 0:
            veil = int(28 * min(1.0, weather.cloud)) + int(weather.rainfall * 1.4)
            if veil > 0:
                overlay = Image.new("RGB", (w, h), (150, 155, 165))
                img.paste(Image.blend(img, overlay, veil / 255.0), (0, 0))
                draw = ImageDraw.Draw(img)
                self._draw = draw
        if weather.solar < 160.0:  # dawn / night: graded blue veil
            strength = max(0.0, min(0.62, (160.0 - weather.solar) / 160.0 * 0.62))
            overlay = Image.new("RGB", (w, h), (18, 24, 48))
            img.paste(Image.blend(img, overlay, strength), (0, 0))
            draw = ImageDraw.Draw(img)
            self._draw = draw

        # -- fixed-camera overlay: timestamp, name, REC dot ---------------
        stamp = time_hhmmss(ts)
        draw.rectangle([0, h - 14, w, h], fill=(10, 12, 14))
        draw.text((4, h - 12), f"{cam.name}  {stamp}", fill=(225, 230, 235))
        if cam.kind == "drone":
            draw.text((w - 66, 4), "REC", fill=(235, 60, 60))

        # -- light blur + sensor noise --------------------------------------
        img = img.filter(ImageFilter.GaussianBlur(radius=0.4))
        arr = np.asarray(img, dtype=np.int16)
        noise = self.rng.gauss(0, 2.6)
        arr += int(noise)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=62, optimize=False)
        cam.last_frame = buf.getvalue()
        cam.last_ts = ts
        return cam.last_frame

    def _draw_plot(self, draw: ImageDraw.ImageDraw, cam: Camera, plot: Plot,
                   horizon: int, weather: WeatherState, rng: random.Random) -> None:
        """Project one plot onto the frame and draw its plants."""
        h, w = self.height, self.width
        # Perspective: distance from the camera along its viewing direction.
        dx, dy = plot.center[0] - cam.x, plot.center[1] - cam.y
        yaw = math.radians(cam.yaw_deg)
        fwd = dx * math.sin(yaw) + dy * math.cos(yaw)
        lat = dx * math.cos(yaw) - dy * math.sin(yaw)
        if fwd < 12.0:
            return
        pitch = math.radians(cam.pitch_deg)
        # Vertical placement: near ground at the bottom, further away higher up.
        ground_y = horizon + (h - horizon) * min(1.0, 14.0 / max(fwd, 14.0)) ** 0.55
        scale = (h * 0.9) / fwd * math.cos(pitch)
        centre_x = w * 0.5 + (lat / max(fwd, 1.0)) * (w / (2 * math.tan(math.radians(cam.fov_deg) / 2)))

        canopy = plot.canopy
        if canopy < 0.03:
            return
        # Half-width of the plot on screen.
        focal = w / (2 * math.tan(math.radians(cam.fov_deg) / 2))
        half_w = max(3.0, (plot.width / 2.0 / max(fwd, 1.0)) * focal)
        # Crop row structure.  Row and plant counts are capped so frame time
        # stays flat no matter how large the field is.
        rows = max(1, min(MAX_ROWS_PER_PLOT, int(plot.height / max(plot.crop.row_spacing_m, 0.15))))
        base_green = _hex_to_rgb(plot.crop.canopy_color)

        # Plant size on screen, then a spacing rule so plants never overlap -
        # overlapping plants hide the lesions of the plant behind them.
        plant_h = (plot.crop.plant_height_m * 0.5) * scale * (0.35 + 0.75 * canopy)
        plant_w = max(1.0, plant_h * (0.55 + 0.25 * canopy))
        spacing = max(plant_w * 2.6, 2.4)
        plants_per_row = max(3, min(MAX_PLANTS_PER_ROW, int(2 * half_w / spacing)))

        # Foliage colour: healthy green -> chlorotic yellow under water/heat
        # stress -> necrotic brown under disease.  The canopy-level tint is what
        # stays visible at field distance; individual lesions are only drawn
        # when a plant is large enough to contain them.
        stress_t = min(0.8, plot.water_stress * 0.95 + plot.heat_stress * 0.5)
        disease_t = min(0.92, plot.disease_pressure * 1.35)
        foliage = _blend(base_green, CHLOROTIC_RGB, stress_t)
        foliage = _blend(foliage, NECROTIC_RGB, disease_t * (1.0 - 0.5 * stress_t))
        foliage = _blend(foliage, (120, 118, 96), 0.30 * (1.0 - plot.health))
        rgb = tuple(int(c) for c in foliage)
        lesion_budget = int(plot.disease_pressure * 26 * (0.4 + canopy))
        draw_lesions = plant_w >= 3.0

        # Draw the far side of the plot first so near rows occlude them.
        order = sorted(range(rows), key=lambda i: -((i / max(rows - 1, 1)) * 2 - 1))
        for ri in order:
            t = ri / max(rows - 1, 1)
            ry = ground_y - t * 26.0 * scale * 0.6 + rng.uniform(-1.5, 1.5) * scale
            for pi in range(plants_per_row):
                px = centre_x - half_w + spacing * (pi + 0.5)
                px += rng.uniform(-0.4, 0.4) * spacing
                draw.ellipse([px - plant_w, ry - plant_h, px + plant_w, ry + plant_w * 0.35],
                             fill=rgb)
                if not draw_lesions:
                    continue
                # Lesions are drawn from a per-plot budget so dense disease
                # cannot blow up the frame cost.
                if lesion_budget <= 0 or rng.random() > 0.5:
                    continue
                n_les = min(lesion_budget, 2)
                lesion_budget -= n_les
                lr = max(1.2, plant_w * 0.32)
                for _ in range(n_les):
                    lx = px + rng.uniform(-plant_w * 0.6, plant_w * 0.6)
                    ly = ry - rng.uniform(0.2, 1.0) * plant_h
                    shade = 118 if plot.disease_pressure < 0.4 else 88
                    draw.ellipse([lx - lr, ly - lr, lx + lr, ly + lr],
                                 fill=(shade, int(shade * 0.60), int(shade * 0.28)))

    # ---- detection (runs on pixels, like a real pipeline) ---------------
    #: Detection scale.  1 = full resolution.  Lesions span only a few pixels
    #: at field distance, so downsampling here loses them entirely.
    DETECT_SCALE = 1

    def detect(self, cam: Camera, frame: bytes | None = None) -> list[Detection]:
        data = frame if frame is not None else cam.last_frame
        if data is None:
            return []
        k = self.DETECT_SCALE
        dw, dh = max(self.width // k, 8), max(self.height // k, 8)
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((dw, dh), Image.BILINEAR)
        arr = np.asarray(img).astype(np.float32)
        hsv = _rgb_to_hsv(arr)
        hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        full_w, full_h = self.width, self.height
        dets: list[Detection] = []

        # NOTE: _rgb_to_hsv returns H in 0..179 but S and V normalised to 0..1,
        # so every V threshold below is a fraction, not an 8-bit level.
        # Partition: any chromatic leaf-like pixel is either healthy green
        # (hue >= 35), bright chlorotic yellow (hue <= 42 and bright) or dark
        # necrotic tissue (hue <= 45 and not bright).  The three classes are
        # mutually exclusive, and bare soil fails the chroma gate outright.
        foliage_like = (sat > SAT_FOLIAGE_MIN) & (hue >= HUE_LEAF_LO) & (hue <= HUE_GREEN[1])
        green = foliage_like & (hue >= HUE_GREEN[0]) & (val > 0.16)
        chlorotic = foliage_like & (hue <= HUE_LEAF_WILT[1]) & (val >= VAL_CHLOROTIC_MIN)
        necrotic_raw = foliage_like & (hue <= HUE_LEAF_WILT[1]) & (val < VAL_CHLOROTIC_MIN)

        green_px = int(green.sum())
        # Canopy footprint = all three foliage states.  Including chlorotic and
        # necrotic classes is what lets a fully wilted or dead crop still be
        # assessed instead of vanishing from the analysis - and necrotic pixels
        # are already chroma-gated, so bare soil cannot enter the footprint.
        footprint = green | chlorotic | necrotic_raw
        fp_px = int(footprint.sum())
        dets.append(Detection("ndvi_proxy", 0.99, (0, 0, full_w, full_h),
                              severity=min(1.0, green_px / float(dw * dh) * 4.0)))

        if fp_px < 80:
            cam.last_dets = dets
            return dets

        # -- chlorosis -----------------------------------------------------
        wilt_frac = float(chlorotic.sum()) / fp_px
        if wilt_frac > WILT_ALERT_FRAC:
            dets.append(Detection("leaf_wilt", confidence=min(0.99, 0.35 + wilt_frac * 2),
                                  bbox=(0, 0, full_w, full_h),
                                  severity=min(1.0, wilt_frac * 2.0)))

        # -- necrotic lesions ----------------------------------------------
        # Bare soil shares the brown hue window with dead tissue, so chroma
        # (SAT_FOLIAGE_MIN) and adjacency to real canopy both have to agree.
        # The erosion is a morphological opening: anti-aliased leaf/soil edges
        # produce 1-2px slivers of dark chromatic pixels that would otherwise
        # read as lesions, while genuine lesion blobs survive intact.
        necrotic = _erode(necrotic_raw & _dilate(footprint, 3) & ~green, 1)
        lesion_frac = int(necrotic.sum()) / max(int(_erode(footprint, 1).sum()), 1)
        if lesion_frac > LESION_ALERT_FRAC:
            severity = min(1.0, lesion_frac * 4.0)
            label = "anthracnose" if cam.area_id == "orchard" else "leaf_blight"
            dets.append(Detection(f"{label}_summary", min(0.99, 0.30 + severity * 0.7),
                                  bbox=(0, 0, full_w, full_h), severity=severity))
            # Individual boxes are only trusted when the burden is clear enough
            # that blobs are separable from anti-aliased leaf edges.
            if lesion_frac > LESION_BOX_FRAC:
                _n, boxes, areas = _connected_components(necrotic, min_area=3)
                compact = [b for b, a in zip(boxes, areas)
                           if a <= 0.01 * dw * dh and b[2] * b[3] <= 0.03 * dw * dh]
                for (bx, by, bw, bh) in compact[:4]:
                    dets.append(Detection(label, min(0.99, 0.35 + severity * 0.6),
                                          (bx * k, by * k, bw * k, bh * k), severity))
        cam.last_dets = dets
        return dets

    # ---- accessors ------------------------------------------------------
    def get(self, camera_id: str) -> Camera | None:
        return self.cameras.get(camera_id)

    def states(self) -> list[dict[str, Any]]:
        return [c.state() for c in self.cameras.values()]


# ---------------------------------------------------------------------------
# numpy helpers (kept module-level so they stay out of the render hot path)
# ---------------------------------------------------------------------------
def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(float(int(value[i:i + 2], 16)) for i in (0, 2, 4))  # type: ignore[return-value]


def _blend(a: Iterable[float], b: tuple[float, float, float], t: float) -> tuple[float, float, float]:
    t = max(0.0, min(1.0, t))
    return tuple(a[i] * (1 - t) + b[i] * t for i in range(3))  # type: ignore[return-value]


def _rgb_to_hsv(arr: np.ndarray) -> np.ndarray:
    """Vectorised RGB->HSV returning H in 0..179, S and V in 0..1."""
    r, g, b = arr[..., 0] / 255.0, arr[..., 1] / 255.0, arr[..., 2] / 255.0
    mx = np.max(arr[..., :3], axis=-1) / 255.0
    mn = np.min(arr[..., :3], axis=-1) / 255.0
    diff = mx - mn
    hue = np.zeros_like(mx)
    with np.errstate(invalid="ignore", divide="ignore"):
        rmask = (diff > 0) & (mx == r)
        gmask = (diff > 0) & (mx == g)
        bmask = (diff > 0) & (mx == b)
        hue[rmask] = ((g[rmask] - b[rmask]) / diff[rmask]) % 6
        hue[gmask] = (b[gmask] - r[gmask]) / diff[gmask] + 2
        hue[bmask] = (r[bmask] - g[bmask]) / diff[bmask] + 4
        hue = hue * 30.0                      # 0..360 -> 0..180
        sat = np.where(mx > 0, diff / np.maximum(mx, 1e-6), 0.0)
    hsv = np.stack([hue, sat, mx], axis=-1)
    return hsv


def _erode(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    out = mask.copy()
    for _ in range(iterations):
        padded = np.pad(out, 1, mode="edge")
        out = (padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2]
               & padded[1:-1, 2:] & padded[1:-1, 1:-1])
    return out


def _dilate(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    out = mask.copy()
    for _ in range(iterations):
        padded = np.pad(out, 1, mode="edge")
        out = (padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2]
               | padded[1:-1, 2:] | padded[1:-1, 1:-1])
    return out


def _connected_components(mask: np.ndarray, min_area: int = 8) -> tuple[int, list[tuple[int, int, int, int]], list[int]]:
    """Lightweight flood-fill labelling (no scipy dependency)."""
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    areas: list[int] = []
    ys, xs = np.nonzero(mask)
    count = 0
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if visited[y0, x0]:
            continue
        count += 1
        stack = [(y0, x0)]
        visited[y0, x0] = True
        minx = maxx = x0
        miny = maxy = y0
        area = 0
        while stack:
            y, x = stack.pop()
            area += 1
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        if area >= min_area:
            boxes.append((minx, miny, maxx - minx + 1, maxy - miny + 1))
            areas.append(area)
    boxes.sort(key=lambda b: -(b[2] * b[3]))
    return count, boxes, areas


def time_hhmmss(ts: float) -> str:
    local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(timezone(timedelta(hours=5.5)))
    return local.strftime("%H:%M:%S")