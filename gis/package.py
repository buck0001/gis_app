"""Project export packager (spec section 30).

Builds the standard PROJECT/ folder layout and zips it:
AOI / DEM / HYDROLOGY / MAPS / REPORT (+ processing history + config JSON
for reproducibility, spec section 40).
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path


def _copy(src: str | None, dst_dir: Path, name: str | None = None) -> None:
    if not src:
        return
    p = Path(src)
    if not p.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dst_dir / (name or p.name))


def build_package(project_dir: str, layers: list[dict], aoi: dict,
                  history: list[dict], maps: list[dict],
                  project_meta: dict) -> str:
    root = Path(project_dir)
    pkg = root / "exports" / "project_package"
    if pkg.exists():
        shutil.rmtree(pkg)

    dem_dir = pkg / "DEM"
    hydro_dir = pkg / "HYDROLOGY"
    maps_dir = pkg / "MAPS"
    report_dir = pkg / "REPORT"
    aoi_dir = pkg / "AOI"

    dem_keys = {"dem_ready": "dem_analysis.tif",
                "elevation": "elevation.tif", "slope": "slope.tif",
                "aspect": "aspect.tif", "hillshade": "hillshade.tif",
                "curvature": "curvature.tif", "filled_dem": "filled_dem.tif",
                "breached_dem": "dem_breached.tif"}
    hydro_raster = {"flow_direction": "flow_direction.tif",
                    "flow_accumulation": "flow_accumulation.tif",
                    "drainage_density": "drainage_density.tif",
                    "distance_to_drainage": "distance_to_drainage.tif",
                    "twi": "twi.tif"}
    by_key = {l["layer_key"]: l for l in layers}

    for key, fname in dem_keys.items():
        if key in by_key:
            _copy(by_key[key]["file_path"], dem_dir, fname)
    original_dem = project_meta.get("original_dem_path")
    if not original_dem and "dem_raw" in by_key:
        original_dem = by_key["dem_raw"].get("file_path")
    if original_dem:
        _copy(
            original_dem, dem_dir,
            "dem_original" + Path(original_dem).suffix.lower(),
        )
    for key, fname in hydro_raster.items():
        if key in by_key:
            _copy(by_key[key]["file_path"], hydro_dir, fname)
    if "drainage" in by_key:
        _copy(by_key["drainage"]["file_path"], hydro_dir, "streams.gpkg")
        _copy(by_key["drainage"]["file_path"], hydro_dir,
              "stream_order.gpkg")
    if "watershed" in by_key:
        _copy(by_key["watershed"]["file_path"], hydro_dir,
              "watersheds.gpkg")

    aoi_dir.mkdir(parents=True, exist_ok=True)
    geom = aoi.get("geometry_geojson") or aoi.get("geometry")
    if isinstance(geom, str):
        geom = json.loads(geom)
    (aoi_dir / "boundary.geojson").write_text(
        json.dumps({"type": "Feature", "properties":
                    {"name": aoi.get("name", "AOI")}, "geometry": geom},
                   indent=2), encoding="utf-8")
    try:
        import geopandas as gpd
        from shapely.geometry import shape
        gdf = gpd.GeoDataFrame([{"name": aoi.get("name", "AOI")}],
                               geometry=[shape(geom)], crs="EPSG:4326")
        gdf.to_file(str(aoi_dir / "boundary.gpkg"), driver="GPKG")
    except Exception:
        pass

    for m in maps:
        _copy(m.get("file_path"), maps_dir)
        map_path = Path(m.get("file_path", ""))
        qa_path = map_path.with_suffix(".qa.json")
        _copy(str(qa_path), maps_dir)

    intermediate_dir = root / "processed"
    if intermediate_dir.is_dir():
        destination = pkg / "PROCESSING" / "intermediates"
        destination.mkdir(parents=True, exist_ok=True)
        for item in intermediate_dir.iterdir():
            if item.is_file() and item.suffix.lower() in {
                ".tif", ".tiff", ".gpkg", ".json",
            }:
                _copy(str(item), destination)

    import csv
    with open(pkg / "statistics.csv", "w", newline="",
              encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["layer", "metric", "value"])
        for lyr in layers:
            stats = lyr.get("stats") or {}
            if isinstance(stats, str):
                try:
                    stats = json.loads(stats)
                except Exception:
                    continue
            for k, v in (stats or {}).items():
                if isinstance(v, (int, float, str)):
                    w.writerow([lyr["layer_key"], k, v])

    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "processing_history.json").write_text(
        json.dumps(history, indent=2, default=str), encoding="utf-8")
    config = {"project": project_meta.get("name"),
              "aoi": {"name": aoi.get("name"),
                      "area_km2": aoi.get("area_km2"),
                      "analysis_crs": aoi.get("analysis_crs"),
                      "bbox": aoi.get("bbox")},
              "dem_mode": project_meta.get("dem_mode"),
              "original_dem": project_meta.get("original_dem_path"),
              "layers": [{"key": l["layer_key"], "source": l.get("source"),
                          "algorithm": l.get("algorithm"),
                          "params": l.get("params")} for l in layers]}
    (pkg / "processing_configuration.json").write_text(
        json.dumps(config, indent=2, default=str), encoding="utf-8")
    _write_report_pdf(str(report_dir / "gis_report.pdf"), project_meta,
                      aoi, layers, history)

    zip_path = root / "exports" / "project_package.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(pkg.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(pkg).as_posix())
    return str(zip_path)


def _write_report_pdf(path: str, project_meta: dict, aoi: dict,
                      layers: list[dict], history: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from .maps import GENERATOR_LINE, MAP_BRANDING

    lines = [f"Project : {project_meta.get('name')}",
             f"AOI     : {aoi.get('name')} ({aoi.get('area_km2')} km2)",
             f"CRS     : {aoi.get('analysis_crs')}",
             f"DEM mode: {project_meta.get('dem_mode')}", "",
             "LAYERS", "------"]
    for lyr in layers:
        lines += [f"- {lyr.get('name')} [{lyr.get('data_kind')}]",
                  f"  source: {lyr.get('source')}",
                  f"  method: {lyr.get('algorithm')}"]
    lines += ["", "PROCESSING HISTORY", "------------------"]
    for h in history[-60:]:
        lines.append(f"{h.get('time', '')} {h.get('tool', '')}:"
                     f" {h.get('message', '')}")

    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.08, 0.94, "GIS Project Report", fontsize=16, weight="bold")
    y = 0.89
    for ln in lines:
        chunks = [ln[i:i + 110] for i in range(0, len(ln), 110)] or [""]
        for chunk in chunks:
            if y < 0.05:
                break
            fig.text(0.08, y, chunk, fontsize=8, family="monospace",
                     va="top")
            y -= 0.018
        y -= 0.006
    fig.text(0.08, 0.03, f"{GENERATOR_LINE} ({MAP_BRANDING['url']}). Derived"
             " products are computed from source datasets - see"
             " methodology; outputs labelled susceptibility are not"
             " predictions.", fontsize=7, style="italic")
    with PdfPages(path) as pdf:
        pdf.savefig()
        plt.close("all")
