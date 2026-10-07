"""Synthetic-surface unit tests for terrain math (run: python tests/test_terrain.py).

Validates slope/aspect/hillshade against surfaces with known answers BEFORE
any real-data processing - guards against sign-convention bugs.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import terrain  # noqa: E402

FAILURES = []


def _write_dem(path: Path, arr: np.ndarray, cs: float = 30.0,
               crs: str = "EPSG:32631") -> str:
    import rasterio
    from rasterio.transform import from_origin
    h, w = arr.shape
    profile = {
        "driver": "GTiff", "height": h, "width": w, "count": 1,
        "dtype": "float32", "crs": crs, "transform": from_origin(500000, 700000,
                                                                 cs, cs),
        "nodata": -9999.0, "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr.astype("float32"), 1)
    return str(path)


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="gister_"))
    print("== Synthetic terrain tests ==")

    # 1. plane descending to the EAST: z = -0.1 * x(metres)
    h = w = 60
    cs = 30.0
    x = np.tile(np.arange(w) * cs, (h, 1))
    plane = -0.1 * x                      # drops 1 m per 10 m eastwards
    dem1 = _write_dem(tmp / "plane.tif", plane, cs)

    slope = terrain.calculate_slope(dem1, str(tmp / "slope.tif"))
    import rasterio
    with rasterio.open(slope["path"]) as src:
        s = src.read(1, masked=True)
    expected_deg = float(np.degrees(np.arctan(0.1)))
    got = float(np.ma.median(s))
    check("slope of known plane", abs(got - expected_deg) < 0.5,
          f"(expected {expected_deg:.2f} deg, got {got:.2f} deg)")
    check("slope range 0..90", float(s.min()) >= -1e-6 and float(s.max()) <= 90.0)

    aspect = terrain.calculate_aspect(dem1, str(tmp / "aspect.tif"))
    with rasterio.open(aspect["path"]) as src:
        a = src.read(1, masked=True)
    got_a = float(np.ma.median(a))
    check("aspect faces EAST (downslope)", abs(got_a - 90.0) < 3.0,
          f"(expected 90 deg, got {got_a:.1f} deg)")

    hs = terrain.calculate_hillshade(dem1, str(tmp / "hs.tif"))
    with rasterio.open(hs["path"]) as src:
        hv = src.read(1, masked=True)
    check("hillshade value range", float(hv.min()) >= 0 and float(hv.max()) <= 255)

    # 2. flat surface: slope 0, aspect -1 (flat), hillshade ~ 255*sin(45)
    flat = np.full((h, w), 150.0)
    dem2 = _write_dem(tmp / "flat.tif", flat, cs)
    terrain.calculate_slope(dem2, str(tmp / "slope_flat.tif"))
    terrain.calculate_aspect(dem2, str(tmp / "aspect_flat.tif"))
    terrain.calculate_hillshade(dem2, str(tmp / "hs_flat.tif"))
    with rasterio.open(tmp / "slope_flat.tif") as src:
        s2 = src.read(1, masked=True)
    with rasterio.open(tmp / "aspect_flat.tif") as src:
        a2 = src.read(1, masked=True)
    with rasterio.open(tmp / "hs_flat.tif") as src:
        h2 = src.read(1, masked=True)
    check("flat slope ~ 0", float(np.ma.max(np.abs(s2))) < 1e-6,
          f"(max {float(np.ma.max(np.abs(s2))):.6f})")
    check("flat aspect = -1", float(np.ma.median(a2)) == -1.0)
    check("flat hillshade ~ 180", abs(float(np.ma.median(h2)) - 180.3) < 3.0,
          f"(got {float(np.ma.median(h2)):.1f})")

    # 3. north-descending plane: z decreases northwards -> aspect 0 (N)
    y = np.tile((np.arange(h) * cs)[:, None], (1, w))
    plane_n = 0.1 * y                     # higher in the south, drops to north
    dem3 = _write_dem(tmp / "plane_n.tif", plane_n, cs)
    terrain.calculate_aspect(dem3, str(tmp / "aspect_n.tif"))
    with rasterio.open(tmp / "aspect_n.tif") as src:
        a3 = src.read(1, masked=True)
    got_n = float(np.ma.median(a3))
    check("aspect faces NORTH", abs(got_n - 0.0) < 3.0 or abs(got_n - 360.0) < 3.0,
          f"(expected 0/360, got {got_n:.1f})")

    # 4. reproject preserves values (plane spans 0 .. -177 m)
    info = terrain.reproject_dem(dem1, str(tmp / "elev_reproj.tif"),
                                 "EPSG:32631", resolution=30.0)
    with rasterio.open(info["path"]) as src:
        e = src.read(1, masked=True)
    expected_min = float(plane.min())
    expected_max = float(plane.max())
    check("reprojected elevation preserves range",
          abs(float(e.min()) - expected_min) < 5 and
          abs(float(e.max()) - expected_max) < 5,
          f"(expected {expected_min:.1f}..{expected_max:.1f}, got "
          f"{float(e.min()):.1f}..{float(e.max()):.1f})")

    print()
    if FAILURES:
        print(f"FAILURES: {FAILURES}")
        sys.exit(1)
    print("All terrain tests passed.")


if __name__ == "__main__":
    main()
