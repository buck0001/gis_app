"""End-to-end smoke test of the GIS engine (run: python tests/smoke_e2e.py).

Phase A - ACQUIRE mode: Copernicus DEM download -> full MVP terrain +
          hydrology plan on a real AOI (Auchi area, Edo State, Nigeria).
Phase B - UPLOAD mode: generated GeoTIFF as user DEM; validation rules;
          preprocess; derived products; original-file integrity; coverage %.
Phase C - Map PNG/PDF generation.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gis import aoi as aoi_mod                     # noqa: E402
from gis import pipeline, maps, dem_source         # noqa: E402

FAILURES = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_project(root: Path, name: str, aoi_dict: dict, crs: str,
                 dem_mode: str = "acquire",
                 dem_source_path: str | None = None) -> pipeline.PipelineContext:
    pdir = root / name
    for sub in ("source", "processed", "outputs", "maps", "previews", "exports"):
        (pdir / sub).mkdir(parents=True, exist_ok=True)
    (pdir / "aoi.json").write_text(json.dumps(aoi_dict), encoding="utf-8")
    history: list[dict] = []
    ctx = pipeline.PipelineContext(
        project_dir=str(pdir), analysis_crs=crs, dem_mode=dem_mode,
        dem_source_path=dem_source_path, aoi=aoi_dict,
        history_cb=lambda s: history.append(s),
    )
    ctx._history_store = history
    return ctx


def main():
    root = Path(tempfile.mkdtemp(prefix="gis_smoke_"))
    print(f"Workspace: {root}")

    # AOI: small box near Auchi, Edo State, Nigeria (~5.5 x 5.5 km)
    bbox = [6.05, 7.04, 6.10, 7.09]
    geom = aoi_mod.bbox_to_polygon(*bbox)
    aoi = aoi_mod.prepare_aoi(geom, name="Auchi test AOI")
    check("AOI prepared", 10 < aoi["area_km2"] < 200,
          f"(area {aoi['area_km2']} km2)")
    check("AOI suggested CRS is projected UTM",
          aoi["suggested_crs"] in ("EPSG:32631", "EPSG:32632", "EPSG:32633"),
          f"({aoi['suggested_crs']})")
    crs = aoi["suggested_crs"]

    # ------------------------------------------------------------------ A
    print("\n== Phase A: ACQUIRE mode (Copernicus + Whitebox) ==")
    ctx = make_project(root, "proj_acquire", aoi, crs, "acquire")
    plan = [
        {"tool": "get_dem"},
        {"tool": "calculate_elevation"},
        {"tool": "calculate_slope"},
        {"tool": "calculate_aspect"},
        {"tool": "calculate_hillshade"},
        {"tool": "breach_dem"},
        {"tool": "calculate_flow_direction"},
        {"tool": "calculate_flow_accumulation"},
        {"tool": "extract_streams", "params": {"threshold_cells": 500}},
        {"tool": "calculate_stream_order"},
        {"tool": "extract_drainage_network"},
        {"tool": "delineate_watershed"},
        {"tool": "calculate_drainage_density"},
    ]
    est = pipeline.plan_estimate(ctx, plan)
    print(f"  plan: {len(est['steps'])} steps, est {est['estimated_label']}")
    result = pipeline.run_plan(ctx, plan)
    keys = {l["key"] for l in result["layers"]}
    expected = {"dem_raw", "elevation", "slope", "aspect", "hillshade",
                "breached_dem", "flow_direction", "flow_accumulation",
                "streams_raster", "stream_order_raster", "drainage",
                "stream_order", "watershed", "drainage_density"}
    check("all MVP layers produced", expected <= keys,
          f"missing: {sorted(expected - keys)}")
    check("tools executed", "get_dem" in result["tools_executed"] and
          "delineate_watershed" in result["tools_executed"])
    by_key = {l["key"]: l for l in result["layers"]}
    check("drainage has segments",
          by_key["drainage"]["extra"]["stats"].get("feature_count", 0) > 0,
          f"({by_key['drainage']['extra']['stats'].get('feature_count')} segs)")
    check("drainage total length > 0",
          by_key["drainage"]["extra"]["stats"].get("total_length_km", 0) > 0,
          f"({by_key['drainage']['extra']['stats'].get('total_length_km')} km)")
    wstats = by_key["watershed"]["extra"].get("stats", {})
    check("watershed area > 0", wstats.get("area_km2", 0) > 0,
          f"({wstats.get('area_km2')} km2)")
    check("watershed has elevation stats", "elevation_m" in wstats)
    check("watershed drainage density present",
          "drainage_density_km_km2" in wstats,
          f"({wstats.get('drainage_density_km_km2')} km/km2)")
    check("previews rendered for visible rasters",
          all("preview" in by_key[k]["extra"] for k in
              ("elevation", "slope", "aspect", "hillshade")))
    check("provenance method recorded on drainage",
          "threshold" in by_key["drainage"]["method_text"])
    check("history recorded", len(ctx._history_store) >= 20,
          f"({len(ctx._history_store)} steps)")
    _d8_sanity_check(by_key)

    # ------------------------------------------------------------------ B
    print("\n== Phase B: UPLOAD mode ==")
    import rasterio as rio
    elev_path = Path(by_key["elevation"]["path"])
    updir = root / "uploads"
    updir.mkdir(exist_ok=True)
    uploaded = updir / "Auchi_DEM.tif"
    shutil.copy(elev_path, uploaded)
    orig_hash = sha256(uploaded)

    rep = dem_source.inspect_dem(str(uploaded))
    check("uploaded DEM inspection passes", rep["ok_to_proceed"],
          f"(min {rep['report']['min']}, max {rep['report']['max']})")
    check("report has actual resolution", rep["report"]["resolution_x"] > 0,
          f"({rep['report']['resolution_text']})")

    # CRS-less variant must STOP
    nocrs = updir / "no_crs.tif"
    with rio.open(uploaded) as src:
        prof = src.profile.copy()
        data = src.read(1)
        prof.pop("crs")
    with rio.open(nocrs, "w", **prof) as dst:
        dst.write(data, 1)
    rep2 = dem_source.inspect_dem(str(nocrs))
    check("missing CRS blocks processing", not rep2["ok_to_proceed"],
          f"(issue: {rep2['issues'][0]['rule']})")
    check("missing CRS message matches spec",
          "assign a CRS" in rep2["issues"][0]["message"])

    # coverage: AOI bigger than the DEM footprint
    cov = dem_source.check_aoi_coverage(
        str(uploaded), aoi_mod.bbox_to_polygon(6.00, 7.00, 6.20, 7.15))
    check("coverage warning for oversized AOI",
          cov.get("outside_percent", 0) > 0.5,
          f"(outside {cov.get('outside_percent')}%)")

    ctx2 = make_project(root, "proj_upload", aoi, crs, "upload",
                        dem_source_path=str(uploaded))
    ctx2.dem_report = rep
    plan2 = [{"tool": "calculate_slope"},
             {"tool": "calculate_aspect"},
             {"tool": "breach_dem"},
             {"tool": "calculate_flow_direction"},
             {"tool": "calculate_flow_accumulation"},
             {"tool": "extract_streams", "params": {"threshold_cells": 500}},
             {"tool": "calculate_stream_order"},
             {"tool": "extract_drainage_network"},
             {"tool": "delineate_watershed"}]
    result2 = pipeline.run_plan(ctx2, plan2)
    keys2 = {l["key"] for l in result2["layers"]}
    need2 = {"dem_ready", "elevation", "slope", "drainage", "watershed"}
    check("upload-mode layers produced", need2 <= keys2,
          f"missing {sorted(need2 - keys2)}")
    check("preprocess ran instead of get_dem",
          "preprocess_dem" in result2["tools_executed"] and
          "get_dem" not in result2["tools_executed"])
    by_key2 = {l["key"]: l for l in result2["layers"]}
    check("derived layer cites uploaded DEM",
          "uploaded DEM" in by_key2["slope"]["source"],
          f"({by_key2['slope']['source']})")
    check("elevation marked DIRECT (user upload)",
          by_key2["elevation"]["data_kind"] == "direct")
    check("original uploaded file unchanged", sha256(uploaded) == orig_hash)

    # ------------------------------------------------------------------ C
    print("\n== Phase C: Map generation ==")
    maps_dir = root / "proj_acquire" / "maps"
    meta = {"source": by_key["slope"]["source"], "resolution": "30 m",
            "algorithm": by_key["slope"]["algorithm"],
            "processing": by_key["slope"]["processing"],
            "aoi_name": "Auchi test AOI", "aoi_area_km2": aoi["area_km2"]}
    out = maps.generate_map(
        str(maps_dir / "slope.png"), "Slope - Auchi test AOI", "raster",
        by_key["slope"]["path"], aoi["geometry"], crs, meta,
        layer_key="slope", fmt="png")
    check("map PNG generated", Path(out["path"]).exists() and
          Path(out["path"]).stat().st_size > 10_000,
          f"({Path(out['path']).stat().st_size} bytes)")
    out_pdf = maps.generate_map(
        str(maps_dir / "drainage.pdf"), "Drainage Network - Auchi test AOI",
        "vector", by_key["drainage"]["path"], aoi["geometry"], crs,
        {**meta, "source": by_key["drainage"]["source"],
         "algorithm": by_key["drainage"]["algorithm"]},
        layer_key="drainage", fmt="pdf")
    check("map PDF generated", Path(out_pdf["path"]).exists() and
          Path(out_pdf["path"]).stat().st_size > 5_000,
          f"({Path(out_pdf['path']).stat().st_size} bytes)")

    print()
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print(f"ALL SMOKE TESTS PASSED. Workspace: {root}")


def _d8_sanity_check(by_key: dict):
    """Flow direction must point downslope on the breached DEM (encoding test)."""
    import numpy as np
    import rasterio
    from gis.hydrology import D8_ENCODING
    with rasterio.open(by_key["breached_dem"]["path"]) as f, \
            rasterio.open(by_key["flow_direction"]["path"]) as p:
        z = f.read(1).astype("float64")
        ptr = p.read(1)
        z[z == f.nodata] = np.nan
    rng = np.random.default_rng(42)
    ok_pairs = total = 0
    for _ in range(300):
        r = int(rng.integers(1, z.shape[0] - 1))
        c = int(rng.integers(1, z.shape[1] - 1))
        code = int(ptr[r, c])
        if code not in D8_ENCODING or not np.isfinite(z[r, c]):
            continue
        dr, dc = D8_ENCODING[code]
        r2, c2 = r + dr, c + dc
        if 0 <= r2 < z.shape[0] and 0 <= c2 < z.shape[1] and np.isfinite(z[r2, c2]):
            total += 1
            if z[r2, c2] <= z[r, c] + 1e-6:
                ok_pairs += 1
    check("D8 flow direction points downslope on breached DEM",
          total > 0 and ok_pairs == total, f"({ok_pairs}/{total})")
    check("Strahler order >= 1",
          by_key["stream_order"]["extra"]["stats"]
          .get("stream_order_max", 0) >= 1)


if __name__ == "__main__":
    main()
