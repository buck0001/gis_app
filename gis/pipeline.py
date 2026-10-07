"""GIS processing pipeline (spec sections 23, 32, 33, 40, 41).

* OPERATION REGISTRY: every tool the AI planner / user can invoke.
* EXECUTION: dependency resolution, progress reporting, cancellation,
  per-step history, output validation and sidecar provenance metadata
  (reproducibility, data-source transparency).
* DEM ACQUISITION MODES: 'upload' (user's DEM is the primary source) or
  'acquire' (Copernicus GLO-30 download). Derived products always record
  which source they came from (DIRECT vs DERIVED labelling, spec section 9).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import terrain, hydrology, stats as stats_mod, symbology, validation

DEM_SOURCE = "Copernicus DEM GLO-30 (ESA) via AWS Open Data"
DEM_RESOLUTION = "30 m (1 arc-second)"
UPLOAD_SOURCE = "User-uploaded DEM"


class PipelineCancelled(Exception):
    pass


class PipelineError(RuntimeError):
    """Operation failed; message should contain 'Recommended action:'."""


@dataclass
class LayerOut:
    key: str
    name: str
    group: str                      # Terrain | Hydrology | Environment | ...
    type: str                       # raster | vector
    path: str                       # absolute file path
    source: str
    algorithm: str
    crs: str = ""
    processing: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    units: str = ""
    resolution: str = ""
    geometry_type: str | None = None
    hidden: bool = False
    data_kind: str = "derived"      # direct | derived | model
    method_text: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _read_aoi(ctx) -> dict:
    """Load the prepared AOI dict from the project's aoi.json sidecar."""
    if ctx.aoi is not None:
        return ctx.aoi
    aoi_path = Path(ctx.project_dir) / "aoi.json"
    if not aoi_path.exists():
        raise PipelineError(
            "No AOI found for this project. "
            "Recommended action: create the project AOI first "
            "(draw/upload/search/coordinates)."
        )
    ctx.aoi = json.loads(aoi_path.read_text(encoding="utf-8"))
    return ctx.aoi


def _sub(ctx, name: str) -> Path:
    d = Path(ctx.project_dir) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _out(ctx, key: str, suffix: str) -> Path:
    return _sub(ctx, "outputs") / f"{key}{suffix}"


def _processed(ctx, name: str) -> Path:
    return _sub(ctx, "processed") / name


def _dem_origin(ctx) -> tuple[str, str]:
    """(source_label, origin_phrase) for the active DEM origin."""
    if ctx.dem_mode == "upload":
        return UPLOAD_SOURCE, "uploaded DEM"
    return DEM_SOURCE, "Copernicus DEM GLO-30"


@dataclass
class PipelineContext:
    project_dir: str
    analysis_crs: str
    dem_mode: str = "acquire"                 # acquire | upload
    dem_source_path: Optional[str] = None     # uploaded DEM original file
    dem_report: Optional[dict] = None         # validation report of upload
    aoi: Optional[dict] = None
    params: dict = field(default_factory=dict)
    existing: dict = field(default_factory=dict)   # key -> existing layer dict
    progress_cb: Optional[Callable] = None         # (fraction, message)
    history_cb: Optional[Callable] = None          # (step dict)
    cancel_cb: Optional[Callable] = None           # () -> bool
    outputs: dict = field(default_factory=dict)    # key -> LayerOut

    def check_cancel(self):
        if self.cancel_cb and self.cancel_cb():
            raise PipelineCancelled("Job cancelled by user.")

    def progress(self, frac: float, msg: str):
        if self.progress_cb:
            self.progress_cb(max(0.0, min(1.0, frac)), msg)

    def history(self, tool: str, message: str, detail: dict | None = None):
        if self.history_cb:
            self.history_cb({
                "time": time.strftime("%H:%M"),
                "iso": _now(),
                "tool": tool,
                "message": message,
                "detail": detail or {},
            })


# ---------------------------------------------------------------------------
# DEM acquisition & preprocessing (modes: acquire | upload)
# ---------------------------------------------------------------------------
def op_get_dem(ctx: PipelineContext, params: dict) -> list[LayerOut]:
    """[ACQUIRE MODE] Download + mosaic + clip Copernicus GLO-30 tiles for AOI."""
    from .providers.dem import CopernicusDEMProvider, DEM_DATASET

    if ctx.dem_mode == "upload":
        raise PipelineError(
            "get_dem skipped: this project uses an uploaded DEM as its source. "
            "Recommended action: use preprocess_dem instead."
        )
    aoi = _read_aoi(ctx)
    cache = str(Path(ctx.project_dir).parent.parent / "cache" / "dem")
    prov = CopernicusDEMProvider(cache_dir=cache)
    dest = _sub(ctx, "source")           # acquired DEM is a source file too
    datasets = prov.search(aoi)
    path = prov.download(datasets[0], str(dest), aoi=aoi,
                         progress_cb=lambda f, m: ctx.progress(f, m))
    v = prov.validate(path)
    if not v["ok"]:
        raise PipelineError(
            f"Downloaded DEM failed validation: {v['checks']}. "
            "Recommended action: retry download; if it persists, reduce AOI."
        )
    meta = prov.metadata(path)
    ctx.history("get_dem",
                f"Downloaded {DEM_DATASET.title} ({meta.get('width')}x{meta['height']} "
                "clip)", {"source": DEM_DATASET.source,
                          "tiles": datasets[0].extra.get("tiles")})
    layer = LayerOut(
        key="dem_raw", name="DEM (WGS84 clip)", group="Terrain", type="raster",
        path=path, source=DEM_SOURCE, crs="EPSG:4326",
        algorithm="tile download -> mosaic -> clip to AOI",
        processing=["tiles_downloaded", "mosaicked", "clipped_to_aoi"],
        resolution=DEM_RESOLUTION, units="metres (EGM2008)",
        hidden=True, data_kind="direct",
        method_text=(f"Direct dataset: {DEM_DATASET.title}. Tiles: "
                     f"{', '.join(datasets[0].extra.get('tiles', []))}. "
                     "Clipped to AOI; original CRS preserved (EPSG:4326)."),
        extra={"provider_meta": meta},
    )
    ctx.outputs["dem_raw"] = layer
    return [layer]


def op_preprocess_dem(ctx: PipelineContext, params: dict) -> list[LayerOut]:
    """[UPLOAD MODE] Validate the uploaded DEM and build the analysis copy.

    The original file in source/ is NEVER modified (spec section 4). All work
    goes into processed/: reproject if required, clip to AOI, optional nodata
    fill only on explicit user request."""
    from . import dem_source as ds

    if ctx.dem_mode != "upload" or not ctx.dem_source_path:
        raise PipelineError(
            "preprocess_dem requires an uploaded DEM. "
            "Recommended action: upload a DEM (or use get_dem for acquisition)."
        )
    aoi = _read_aoi(ctx)
    report = ctx.dem_report or ds.inspect_dem(ctx.dem_source_path)
    if not report["ok_to_proceed"]:
        issues = [i["message"] for i in report["issues"] if i["severity"] == "error"]
        raise PipelineError(
            "Uploaded DEM failed validation: " + "; ".join(issues) +
            " Recommended action: fix the DEM (assign a CRS) or upload another "
            "file."
        )
    nodata_action = params.get("nodata_action", "continue")
    result = ds.preprocess_uploaded_dem(
        source_path=ctx.dem_source_path,
        processed_dir=str(_sub(ctx, "processed")),
        aoi=aoi,
        analysis_crs=ctx.analysis_crs,
        resolution_m=params.get("resolution_m"),
        nodata_action=nodata_action,
        assign_crs=params.get("assign_crs"),
    )
    ctx.history("preprocess_dem",
                f"Prepared analysis copy of uploaded DEM ({result['original_crs']} -> "
                f"{ctx.analysis_crs}, {result['resampling']}, "
                f"nodata_action={nodata_action})", result)
    layer = LayerOut(
        key="dem_ready", name="DEM (analysis copy)", group="Terrain", type="raster",
        path=result["path"], source=UPLOAD_SOURCE, crs=ctx.analysis_crs,
        algorithm="validate -> reproject-if-required -> clip to AOI"
                  + (" -> fill nodata" if nodata_action == "fill" else ""),
        processing=result["processing"],
        params={"original_crs": result["original_crs"],
                "analysis_crs": result["analysis_crs"],
                "original_resolution": result["original_resolution"],
                "new_resolution": result["new_resolution"],
                "resampling": result["resampling"],
                "nodata_action": nodata_action},
        units="metres", resolution=result["new_resolution"],
        hidden=True, data_kind="direct",
        method_text=(
            f"Direct data: user-uploaded DEM ({Path(ctx.dem_source_path).name}). "
            f"Original CRS {result['original_crs']}, analysis CRS {ctx.analysis_crs}; "
            f"original resolution {result['original_resolution']}, analysis grid "
            f"{result['new_resolution']}, resampling {result['resampling']}. "
            "Original file preserved unchanged in source/."),
        extra=result,
    )
    ctx.outputs["dem_ready"] = layer
    return [layer]


# ---------------------------------------------------------------------------
# basic terrain products (spec section 8)
# ---------------------------------------------------------------------------
def origin_dem(ctx) -> str:
    return _dem_origin(ctx)[1]


def _derived_source(ctx) -> tuple[str, str, str]:
    """(source_label, origin_phrase, unused) for derived layers."""
    origin, what = _dem_origin(ctx)
    return (f"Derived from {what}", what, what)


def op_calculate_elevation(ctx: PipelineContext, params: dict) -> list[LayerOut]:
    """Elevation layer.

    ACQUIRE: reproject the WGS84 DEM clip to the analysis CRS.
    UPLOAD:  analysis copy already exists -> register it as Elevation."""
    if ctx.dem_mode == "upload":
        ready = ctx.outputs.get("dem_ready")
        if not ready or not Path(ready.path).exists():
            op_preprocess_dem(ctx, params)
            ready = ctx.outputs["dem_ready"]
        origin, what = _dem_origin(ctx)
        layer = LayerOut(
            key="elevation", name="Elevation", group="Terrain", type="raster",
            path=ready.path, source=f"{origin} (analysis copy)",
            crs=ctx.analysis_crs,
            algorithm=ready.algorithm, processing=list(ready.processing),
            params=dict(ready.params), units="m",
            resolution=ready.resolution, data_kind="direct",
            method_text=(f"Direct data (regridded copy): user-uploaded DEM. "
                         f"{ready.method_text}"),
            extra={"from": "dem_ready", "validation_note":
                   "no reprojection needed beyond analysis-copy step"},
        )
        ctx.outputs["elevation"] = layer
        ctx.history("calculate_elevation",
                    "Elevation layer = analysis copy of uploaded DEM "
                    f"({ctx.analysis_crs})", ready.extra)
        return [layer]

    # acquire mode
    from shapely.geometry import shape
    from shapely.ops import transform as shp_transform
    from pyproj import Transformer

    aoi = _read_aoi(ctx)
    res = float(params.get("resolution_m", 30.0))
    src = ctx.outputs.get("dem_raw")
    src_path = src.path if src else str(_out(ctx, "dem_raw", ".tif"))
    if not Path(src_path).exists():
        raise PipelineError(
            "DEM clip missing. Recommended action: run get_dem first.")
    dst = _out(ctx, "elevation", ".tif")
    tr = Transformer.from_crs("EPSG:4326", ctx.analysis_crs, always_xy=True)
    geom_proj = shp_transform(tr.transform, shape(aoi["geometry"]))
    info = terrain.reproject_dem(src_path, str(dst), ctx.analysis_crs,
                                 resolution=res, aoi_geom_proj=geom_proj)
    ctx.history("calculate_elevation",
                f"Reprojected DEM to {ctx.analysis_crs} at {res:g} m (bilinear)",
                info)
    layer = LayerOut(
        key="elevation", name="Elevation", group="Terrain", type="raster",
        path=str(dst), source=f"{DEM_SOURCE} (reprojected)",
        crs=ctx.analysis_crs,
        algorithm=f"reproject to {ctx.analysis_crs} @ {res:g} m, bilinear",
        processing=["reprojected_to_analysis_crs", "clipped_to_aoi"],
        params={"resolution_m": res, "resampling": "bilinear",
                "original_resolution": DEM_RESOLUTION,
                "new_resolution": f"{res:g} m"},
        units="m", resolution=f"{res:g} m", data_kind="derived",
        method_text=(f"Derived from: Copernicus DEM GLO-30. Method: reproject "
                     f"EPSG:4326 -> {ctx.analysis_crs}, {res:g} m grid, bilinear "
                     "resampling, clipped to AOI."),
        extra=info,
    )
    ctx.outputs["elevation"] = layer
    return [layer]


def _require_elevation(ctx) -> LayerOut:
    elev = ctx.outputs.get("elevation")
    if elev and Path(elev.path).exists():
        return elev
    if ctx.dem_mode == "upload":
        op_preprocess_dem(ctx, {})
        op_calculate_elevation(ctx, {})
    else:
        op_get_dem(ctx, {})
        op_calculate_elevation(ctx, {})
    return ctx.outputs["elevation"]


def op_generate_contours(ctx: PipelineContext, params: dict) -> list[LayerOut]:
    elev = _require_elevation(ctx)
    interval_m = params.get("interval_m", 10.0)
    try:
        interval_m = float(interval_m)
        info = terrain.generate_contours(
            elev.path, str(_out(ctx, "contours", ".gpkg")), interval_m,
        )
    except (TypeError, ValueError) as exc:
        raise PipelineError(
            f"Could not generate contours: {exc} "
            "Recommended action: choose a positive interval that falls within "
            "the DEM elevation range."
        ) from exc

    ctx.history(
        "generate_contours",
        f"Generated {info['contour_count']} contour lines at "
        f"{interval_m:g} m intervals.",
        info,
    )
    src_label, origin = _dem_origin(ctx)
    layer = LayerOut(
        key="contours", name=f"Contours ({interval_m:g} m)", group="Terrain",
        type="vector", path=info["path"], source=src_label,
        crs=ctx.analysis_crs, algorithm="marching-squares elevation contours",
        processing=["elevation_contour_extraction"],
        params={"interval_m": interval_m},
        units="metres elevation", resolution=elev.resolution,
        geometry_type="LineString", data_kind="derived",
        method_text=(
            f"Derived from: {origin} (via Elevation layer). Method: "
            f"marching-squares contours at {interval_m:g} m elevation "
            f"intervals in {ctx.analysis_crs}."
        ),
        extra=info,
    )
    ctx.outputs["contours"] = layer
    return [layer]


def op_calculate_slope(ctx, params):
    elev = _require_elevation(ctx)
    dst = _out(ctx, "slope", ".tif")
    info = terrain.calculate_slope(elev.path, str(dst))
    ctx.history("calculate_slope", "Generated slope (degrees, Horn 3x3)", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="slope", name="Slope", group="Terrain", type="raster", path=str(dst),
        source=src_label, crs=ctx.analysis_crs, algorithm=info["algorithm"],
        processing=["clipped_to_aoi", "horn_3x3"], units="degrees",
        resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (via Elevation layer). Method: "
                     "Horn (1981) 3x3 finite differences on the "
                     f"{ctx.analysis_crs} grid. Valid range 0-90 degrees."),
        extra=info,
    )
    ctx.outputs["slope"] = layer
    return [layer]


def op_calculate_aspect(ctx, params):
    elev = _require_elevation(ctx)
    dst = _out(ctx, "aspect", ".tif")
    info = terrain.calculate_aspect(elev.path, str(dst))
    ctx.history("calculate_aspect", "Generated aspect (degrees from north)", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="aspect", name="Aspect", group="Terrain", type="raster", path=str(dst),
        source=src_label, crs=ctx.analysis_crs, algorithm=info["algorithm"],
        processing=["clipped_to_aoi", "horn_3x3"],
        units="degrees (cw from north; -1 = flat)", resolution=elev.resolution,
        data_kind="derived",
        method_text=(f"Derived from: {origin} (via Elevation). Method: Horn 3x3 "
                     "gradients; aspect = downslope direction in degrees "
                     "clockwise from north. Flat cells = -1."),
        extra=info,
    )
    ctx.outputs["aspect"] = layer
    return [layer]


def op_calculate_hillshade(ctx, params):
    elev = _require_elevation(ctx)
    az = float(params.get("azimuth", 315))
    alt = float(params.get("altitude", 45))
    vert_exag = float(params.get("vert_exag", 3))
    dst = _out(ctx, "hillshade", ".tif")
    info = terrain.calculate_hillshade(elev.path, str(dst), az, alt, vert_exag)
    ctx.history("calculate_hillshade",
                f"Generated hillshade (azimuth {az}, altitude {alt}, "
                f"vertical exaggeration {vert_exag})", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="hillshade", name="Hillshade", group="Terrain", type="raster",
        path=str(dst), source=src_label, crs=ctx.analysis_crs,
        algorithm=info["algorithm"], params=info["params"],
        processing=["clipped_to_aoi", "horn_hillshade"], units="0-255",
        resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (via Elevation). Method: Horn "
                     f"hillshade, light azimuth {az} deg, altitude {alt} deg, "
                     f"vertical exaggeration {vert_exag}. Output 0-255 grayscale."),
        extra=info,
    )
    ctx.outputs["hillshade"] = layer
    return [layer]


# --- advanced terrain (opt-in only, spec section 8) ------------------------
_ADVANCED_TERRAIN = {
    "curvature": ("Curvature", terrain.calculate_curvature,
                  "Laplacian second derivative", "1/m x1000 (+ convex)"),
    "roughness": ("Roughness", terrain.calculate_roughness,
                  "3x3 max elevation difference", "m"),
    "tri": ("TRI", terrain.calculate_tri,
            "TRI (3x3 mean abs diff)", "m"),
}


def _advanced_terrain_op(key: str):
    label, fn, algorithm, units = _ADVANCED_TERRAIN[key]

    def op(ctx, params):
        elev = _require_elevation(ctx)
        dst = _out(ctx, key, ".tif")
        info = fn(elev.path, str(dst))
        ctx.history(f"calculate_{key}", f"Generated {label.lower()}", info)
        src_label, origin, _ = _derived_source(ctx)
        layer = LayerOut(
            key=key, name=label, group="Terrain", type="raster", path=str(dst),
            source=src_label, crs=ctx.analysis_crs, algorithm=algorithm,
            processing=["clipped_to_aoi"], units=units,
            resolution=elev.resolution, data_kind="derived",
            method_text=(f"Derived from: {origin} (via Elevation). "
                         f"Method: {algorithm}."),
            extra=info,
        )
        ctx.outputs[key] = layer
        return [layer]
    op.__name__ = f"op_calculate_{key}"
    return op


def op_calculate_tpi(ctx, params):
    elev = _require_elevation(ctx)
    radius = float(params.get("radius_m", 500.0))
    dst = _out(ctx, "tpi", ".tif")
    info = terrain.calculate_tpi(elev.path, str(dst), radius_m=radius)
    ctx.history("calculate_tpi", f"Generated TPI (radius {radius} m)", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="tpi", name="TPI", group="Terrain", type="raster", path=str(dst),
        source=src_label, crs=ctx.analysis_crs, params={"radius_m": radius},
        algorithm=info["algorithm"], processing=["clipped_to_aoi"], units="m",
        resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (via Elevation). Method: TPI = "
                     f"elevation - mean elevation within {radius} m radius."),
        extra=info,
    )
    ctx.outputs["tpi"] = layer
    return [layer]


# ---------------------------------------------------------------------------
# hydrological analysis (spec sections 9, 10, 16, 17)
# ---------------------------------------------------------------------------
def op_fill_dem(ctx, params):
    elev = _require_elevation(ctx)
    dst = _processed(ctx, "dem_filled.tif")
    info = hydrology.fill_dem(elev.path, str(dst))
    ctx.history("fill_dem", "Filled depressions (priority-flood)", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="filled_dem", name="Filled DEM", group="Hydrology", type="raster",
        path=str(dst), source=src_label, crs=ctx.analysis_crs,
        algorithm=info["algorithm"], processing=["hydrological_conditioning"],
        units="m", resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (via Elevation layer). Method: "
                     "priority-flood depression filling. Separate processing "
                     "copy - source DEM files are not modified."),
        extra=info,
    )
    ctx.outputs["filled_dem"] = layer
    return [layer]


def op_breach_dem(ctx, params):
    elev = _require_elevation(ctx)
    dst = _processed(ctx, "dem_breached.tif")
    info = hydrology.breach_dem(elev.path, str(dst))
    ctx.history("breach_dem", "Breached depressions", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="breached_dem", name="Breached DEM", group="Hydrology",
        type="raster", path=str(dst), source=src_label,
        crs=ctx.analysis_crs, algorithm=info["algorithm"],
        processing=["depression_breaching"],
        params=info["parameters"], units="m",
        resolution=elev.resolution, data_kind="derived",
        method_text=(
            f"Derived from: {origin} (via Elevation). Depression breaching "
            "using WhiteboxTools; no filling fallback is applied."
        ),
        extra=info,
    )
    ctx.outputs["breached_dem"] = layer
    return [layer]


def _require_breached(ctx) -> LayerOut:
    breached = ctx.outputs.get("breached_dem")
    if not breached or not Path(breached.path).exists():
        op_breach_dem(ctx, {})
        breached = ctx.outputs["breached_dem"]
    return breached


def op_calculate_flow_direction(ctx, params):
    conditioned = _require_breached(ctx)
    dst = _out(ctx, "flow_direction", ".tif")
    info = hydrology.flow_direction(conditioned.path, str(dst))
    ctx.history("calculate_flow_direction", "Generated D8 flow direction", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="flow_direction", name="Flow Direction", group="Hydrology",
        type="raster", path=str(dst), source=src_label, crs=ctx.analysis_crs,
        algorithm=info["algorithm"], processing=["d8_pointer"],
        units="Classes: 8 (D8), shown as the direction water flows toward",
        resolution=conditioned.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (via Breached DEM). Method: D8 "
                     "flow direction (WhiteboxTools), ESRI encoding "
                     "1=E 2=SE 4=S 8=SW 16=W 32=NW 64=N 128=NE."),
        extra=info,
    )
    ctx.outputs["flow_direction"] = layer
    return [layer]


def op_calculate_flow_accumulation(ctx, params):
    conditioned = _require_breached(ctx)
    ptr = ctx.outputs.get("flow_direction")
    if not ptr or not Path(ptr.path).exists():
        op_calculate_flow_direction(ctx, params)
        ptr = ctx.outputs["flow_direction"]
    dst = _out(ctx, "flow_accumulation", ".tif")
    info = hydrology.flow_accumulation(ptr.path, str(dst))
    ctx.history("calculate_flow_accumulation",
                "Generated D8 flow accumulation", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="flow_accumulation", name="Flow Accumulation", group="Hydrology",
        type="raster", path=str(dst), source=src_label, crs=ctx.analysis_crs,
        algorithm=info["algorithm"], processing=["d8_flow_accumulation"],
        units="cells", resolution=conditioned.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (via Breached DEM). Method: D8 flow "
                     "accumulation (WhiteboxTools), units = upstream cells."),
        extra=info,
    )
    ctx.outputs["flow_accumulation"] = layer
    return [layer]


def op_extract_streams(ctx, params):
    fa = ctx.outputs.get("flow_accumulation")
    if not fa or not Path(fa.path).exists():
        op_calculate_flow_accumulation(ctx, params)
        fa = ctx.outputs["flow_accumulation"]
    threshold_value = params.get(
        "threshold_cells", ctx.params.get("stream_threshold"))
    if threshold_value is None:
        raise PipelineError(
            "No stream accumulation threshold was provided. "
            "Recommended action: choose and record a threshold in cells based "
            "on the study area and hydrological method."
        )
    threshold = int(threshold_value)
    dst = _out(ctx, "streams_raster", ".tif")
    info = hydrology.extract_streams(fa.path, str(dst), threshold)
    ctx.history("extract_streams",
                f"Extracted streams at threshold {threshold} cells", info)
    layer = LayerOut(
        key="streams_raster", name="Stream raster", group="Hydrology",
        type="raster", path=str(dst), source=f"Derived from {origin_dem(ctx)}",
        crs=ctx.analysis_crs, algorithm=info["algorithm"],
        processing=["threshold"], params={"threshold_cells": threshold},
        units="1 = stream", resolution=fa.resolution, hidden=True,
        data_kind="derived",
        method_text=(f"Derived from: {origin_dem(ctx)}. Method: flow "
                     f"accumulation >= {threshold} cells (configurable "
                     "threshold - one size does not fit all AOIs)."),
        extra=info,
    )
    ctx.outputs["streams_raster"] = layer
    return [layer]


def op_calculate_stream_order(ctx, params):
    sr = ctx.outputs.get("streams_raster")
    fa = ctx.outputs.get("flow_accumulation")
    ptr = ctx.outputs.get("flow_direction")
    if not (sr and fa and ptr and all(Path(p.path).exists()
                                      for p in (sr, fa, ptr))):
        op_extract_streams(ctx, params)
        sr = ctx.outputs["streams_raster"]
        fa = ctx.outputs["flow_accumulation"]
        ptr = ctx.outputs["flow_direction"]
    dst = _out(ctx, "stream_order_raster", ".tif")
    info = hydrology.strahler_order(sr.path, fa.path, ptr.path, str(dst))
    ctx.history("calculate_stream_order",
                f"Computed Strahler stream order (max order {info['max_order']})",
                info)
    layer = LayerOut(
        key="stream_order_raster", name="Stream order raster", group="Hydrology",
        type="raster", path=str(dst), source=f"Derived from {origin_dem(ctx)}",
        crs=ctx.analysis_crs, algorithm=info["algorithm"], processing=["strahler"],
        units="order (0 = not a stream)", resolution=sr.resolution, hidden=True,
        data_kind="derived",
        method_text=(f"Derived from: {origin_dem(ctx)}. Method: Strahler "
                     "ordering on the D8 stream network."),
        extra=info,
    )
    ctx.outputs["stream_order_raster"] = layer
    return [layer]


def op_extract_drainage_network(ctx, params):
    """Vectorise the stream raster + attach Strahler order -> visible layers
    'Drainage Network' and 'Stream Order' (same GeoPackage, two styles)."""
    sr = ctx.outputs.get("streams_raster")
    order = ctx.outputs.get("stream_order_raster")
    elev = ctx.outputs.get("elevation")
    ptr = ctx.outputs.get("flow_direction")
    if not (sr and Path(sr.path).exists()):
        op_extract_streams(ctx, params)
        sr = ctx.outputs["streams_raster"]
    if not (order and Path(order.path).exists()):
        op_calculate_stream_order(ctx, params)
        order = ctx.outputs["stream_order_raster"]
    if not (ptr and Path(ptr.path).exists()):
        op_calculate_flow_direction(ctx, params)
        ptr = ctx.outputs["flow_direction"]
    if not elev:
        elev = _require_elevation(ctx)
    dst = _out(ctx, "drainage", ".gpkg")
    info = hydrology.streams_to_vector(sr.path, order.path, ptr.path, str(dst),
                                       dem_path=elev.path)
    ctx.history("extract_drainage_network",
                f"Vectorised drainage network ({info['segments']} segments, "
                f"{info['total_length_km']} km, max order {info['max_order']})",
                info)
    src_label, origin, _ = _derived_source(ctx)
    threshold_value = sr.params.get("threshold_cells")
    if threshold_value is None:
        raise PipelineError(
            "The drainage layer has no recorded stream threshold. "
            "Recommended action: re-run stream extraction with an explicit "
            "threshold in cells."
        )
    threshold = int(threshold_value)
    drainage = LayerOut(
        key="drainage", name="Drainage Network", group="Hydrology",
        type="vector", path=str(dst), geometry_type="LineString",
        source=src_label, crs=ctx.analysis_crs,
        algorithm="stream raster -> polylines with Strahler order",
        processing=["raster_to_vector", "strahler_order_attribution"],
        params={"threshold_cells": threshold},
        units="m", resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin}. Method: flow direction -> flow "
                     f"accumulation -> stream extraction (threshold {threshold} "
                     "cells) -> raster-to-vector -> Strahler order sampled per "
                     "segment."),
        extra=info,
    )
    stream_order = LayerOut(
        key="stream_order", name="Stream Order", group="Hydrology",
        type="vector", path=str(dst), geometry_type="LineString",
        source=src_label, crs=ctx.analysis_crs,
        algorithm="Strahler stream order on drainage segments",
        processing=["raster_to_vector", "strahler_order_attribution"],
        params={"threshold_cells": threshold},
        units="dimensionless order", resolution=elev.resolution,
        data_kind="derived",
        method_text=(f"Derived from: {origin}. Method: Strahler ordering of "
                     "the extracted drainage network (segments carry "
                     "stream_order attribute, 1..N). Same geometry as the "
                     "Drainage Network layer; styled by order."),
        extra=info,
    )
    ctx.outputs["drainage"] = drainage
    ctx.outputs["stream_order"] = stream_order
    return [drainage, stream_order]


def op_delineate_watershed(ctx, params):
    ptr = ctx.outputs.get("flow_direction")
    if not ptr or not Path(ptr.path).exists():
        op_calculate_flow_direction(ctx, params)
        ptr = ctx.outputs["flow_direction"]
    fa = ctx.outputs.get("flow_accumulation")
    if not fa or not Path(fa.path).exists():
        op_calculate_flow_accumulation(ctx, params)
        fa = ctx.outputs["flow_accumulation"]
    sr = ctx.outputs.get("streams_raster")
    wsd_tif = _out(ctx, "watershed", ".tif")
    outlet = params.get("outlet")            # optional [lon, lat]
    if outlet:
        import numpy as np
        import rasterio
        from pyproj import Transformer
        with rasterio.open(ptr.path) as src:
            inv = ~src.transform
            tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            x, y = tr.transform(float(outlet[0]), float(outlet[1]))
            col0, row0 = inv * (x, y)
        with rasterio.open(fa.path) as fsrc:
            w = 50
            r0 = max(0, int(row0) - w); r1 = min(fsrc.height, int(row0) + w + 1)
            c0 = max(0, int(col0) - w); c1 = min(fsrc.width, int(col0) + w + 1)
            win = fsrc.read(1, window=((r0, r1), (c0, c1))).astype("float64")
            nd = fsrc.nodata
        if nd is not None:
            win[win == nd] = -np.inf
        win[~np.isfinite(win)] = -np.inf
        rs, cs_ = np.unravel_index(int(np.argmax(win)), win.shape)
        outlet_rc = {
            "row": int(r0 + rs), "col": int(c0 + cs_),
            "initial_row": int(row0), "initial_col": int(col0),
            "accumulation_cells": float(win[rs, cs_]),
            "method": "user outlet snapped to local max flow accumulation "
                      "(50-cell window)",
        }
    else:
        outlet_rc = hydrology.find_outlet(
            fa.path, ptr.path, sr.path if sr else None)
    import rasterio
    from pyproj import Transformer
    with rasterio.open(ptr.path) as src:
        x, y = src.xy(outlet_rc["row"], outlet_rc["col"])
        to_wgs84 = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
        lon, lat = to_wgs84.transform(x, y)
    outlet_rc["coordinates_lonlat"] = [float(lon), float(lat)]
    hydrology.delineate_watershed(ptr.path, outlet_rc, str(wsd_tif),
                                  _sub(ctx, "processed"))
    dst = _out(ctx, "watersheds", ".gpkg")
    poly = hydrology.polygonize_watershed(str(wsd_tif), str(dst))
    elev = ctx.outputs.get("elevation") or _require_elevation(ctx)
    slope = ctx.outputs.get("slope")
    wstats = hydrology.watershed_stats(
        str(dst), ctx.outputs["drainage"].path if ctx.outputs.get("drainage")
        else None, elev.path, slope.path if slope else None)
    ctx.history("delineate_watershed",
                f"Delineated watershed ({poly['area_km2']} km2) - "
                f"{outlet_rc['method']}", {**poly, "stats": wstats})
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="watershed", name="Watershed", group="Hydrology", type="vector",
        path=str(dst), geometry_type="Polygon", source=src_label,
        crs=ctx.analysis_crs,
        algorithm="D8 watershed delineation (auto outlet at AOI boundary) "
                  "-> polygonise",
        processing=["outlet_selection", "watershed_delineation", "polygonise"],
        params={"outlet": outlet or "auto", "outlet_info": outlet_rc},
        units="m", data_kind="derived",
        method_text=(f"Derived from: {origin}. Method: outlet = highest flow "
                     "accumulation among cells whose flow leaves the AOI "
                     "(snapped locally), D8 watershed, raster-to-polygon. "
                     "Stats: " + json.dumps(wstats)),
        extra={**poly, "stats": wstats, "outlet": outlet_rc},
    )
    ctx.outputs["watershed"] = layer
    return [layer]


def op_calculate_drainage_density(ctx, params):
    sr = ctx.outputs.get("streams_raster")
    ptr = ctx.outputs.get("flow_direction")
    elev = ctx.outputs.get("elevation") or _require_elevation(ctx)
    if not (sr and Path(sr.path).exists()):
        op_extract_streams(ctx, params)
        sr = ctx.outputs["streams_raster"]
    if not (ptr and Path(ptr.path).exists()):
        op_calculate_flow_direction(ctx, params)
        ptr = ctx.outputs["flow_direction"]
    window = float(params.get("window_m", 1000.0))
    dst = _out(ctx, "drainage_density", ".tif")
    info = hydrology.drainage_density_raster(elev.path, sr.path, ptr.path,
                                              str(dst), window_m=window)
    ctx.history("calculate_drainage_density",
                f"Generated drainage density raster ({info['algorithm']})", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="drainage_density", name="Drainage Density", group="Hydrology",
        type="raster", path=str(dst), source=src_label, crs=ctx.analysis_crs,
        algorithm=info["algorithm"], processing=["density_window"],
        params={"window_m": window}, units="km/km2",
        resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin}. Method: drainage density = "
                     f"stream length per unit area in a {window:.0f} m moving "
                     "window (km/km2)."),
        extra=info,
    )
    ctx.outputs["drainage_density"] = layer
    return [layer]


def op_calculate_distance_to_drainage(ctx, params):
    sr = ctx.outputs.get("streams_raster")
    elev = ctx.outputs.get("elevation") or _require_elevation(ctx)
    if not (sr and Path(sr.path).exists()):
        op_extract_streams(ctx, params)
        sr = ctx.outputs["streams_raster"]
    dst = _out(ctx, "distance_to_drainage", ".tif")
    info = hydrology.distance_to_drainage(sr.path, str(dst))
    ctx.history("calculate_distance_to_drainage",
                "Generated distance-to-drainage raster (metres)", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="distance_to_drainage", name="Distance to Drainage",
        group="Hydrology", type="raster", path=str(dst), source=src_label,
        crs=ctx.analysis_crs, algorithm=info["algorithm"],
        processing=["euclidean_distance"], units="m",
        resolution=elev.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} drainage network. Method: "
                     "Euclidean distance transform to nearest stream cell "
                     "(metres)."),
        extra=info,
    )
    ctx.outputs["distance_to_drainage"] = layer
    return [layer]


def op_calculate_twi(ctx, params):
    fa = ctx.outputs.get("flow_accumulation")
    slope = ctx.outputs.get("slope")
    if not (fa and Path(fa.path).exists()):
        op_calculate_flow_accumulation(ctx, params)
        fa = ctx.outputs["flow_accumulation"]
    if not (slope and Path(slope.path).exists()):
        op_calculate_slope(ctx, params)
        slope = ctx.outputs["slope"]
    dst = _out(ctx, "twi", ".tif")
    info = hydrology.topographic_wetness_index(fa.path, slope.path, str(dst))
    ctx.history("calculate_twi", "Generated topographic wetness index", info)
    src_label, origin, _ = _derived_source(ctx)
    layer = LayerOut(
        key="twi", name="Topographic Wetness Index", group="Hydrology",
        type="raster", path=str(dst), source=src_label, crs=ctx.analysis_crs,
        algorithm=info["algorithm"], processing=["twi"],
        units="dimensionless", resolution=fa.resolution, data_kind="derived",
        method_text=(f"Derived from: {origin} (flow accumulation + slope). "
                     "Method: TWI = ln(specific upslope area / tan(slope)). "
                     "Interpretation aid, not an observed measurement."),
        extra=info,
    )
    ctx.outputs["twi"] = layer
    return [layer]


# ---------------------------------------------------------------------------
# operation registry (spec section 32 - controlled AI tool calls)
# ---------------------------------------------------------------------------
@dataclass
class OpSpec:
    tool: str
    label: str
    group: str
    fn: Callable
    requires: list                      # tool names; "DEM_TOOL" resolves by mode
    est_seconds: int
    description: str
    params: dict = field(default_factory=dict)
    advanced: bool = False              # advanced terrain metrics (opt-in)


def _dem_tool(ctx) -> str:
    return "preprocess_dem" if ctx.dem_mode == "upload" else "get_dem"


def _req(tools: list[str]):
    return lambda ctx: [_dem_tool(ctx) if t == "DEM_TOOL" else t for t in tools]


REGISTRY: dict[str, OpSpec] = {}


def _register(tool, label, group, fn, requires, est, desc, params=None,
              advanced=False):
    REGISTRY[tool] = OpSpec(tool=tool, label=label, group=group, fn=fn,
                            requires=requires, est_seconds=est,
                            description=desc, params=params or {},
                            advanced=advanced)


_register("get_dem", "Acquire DEM (Copernicus GLO-30)", "Data", op_get_dem, [],
          45, "Download, mosaic and clip the global 30 m Copernicus DEM "
              "for the AOI (acquire mode).")
_register("preprocess_dem", "Preprocess uploaded DEM", "Data",
          op_preprocess_dem, [], 15,
          "Validate uploaded DEM and build the projected/clipped analysis copy "
          "without touching the original file (upload mode).")
_register("calculate_elevation", "Elevation", "Terrain", op_calculate_elevation,
          ["DEM_TOOL"], 8,
          "Produce the ELEVATION analysis grid in the projected CRS.")
_register("generate_contours", "Generate elevation contours", "Terrain",
          op_generate_contours, ["calculate_elevation"], 8,
          "Generate vector contour lines from the elevation grid. "
          "Params: interval_m (positive elevation interval in metres).",
          params={"interval_m": 10.0})
_register("calculate_slope", "Slope", "Terrain", op_calculate_slope,
          ["calculate_elevation"], 5, "Slope in degrees (Horn 3x3).")
_register("calculate_aspect", "Aspect", "Terrain", op_calculate_aspect,
          ["calculate_elevation"], 5,
          "Aspect in degrees clockwise from north (Horn 3x3).")
_register("calculate_hillshade", "Hillshade", "Terrain", op_calculate_hillshade,
          ["calculate_elevation"], 5,
          "ESRI hillshade (azimuth 315, altitude 45 by default).")
_register("calculate_curvature", "Curvature", "Terrain",
          _advanced_terrain_op("curvature"), ["calculate_elevation"], 8,
          "Laplacian surface curvature (advanced, opt-in).", advanced=True)
_register("calculate_roughness", "Roughness", "Terrain",
          _advanced_terrain_op("roughness"), ["calculate_elevation"], 8,
          "3x3 roughness (advanced, opt-in).", advanced=True)
_register("calculate_tri", "TRI", "Terrain", _advanced_terrain_op("tri"),
          ["calculate_elevation"], 8,
          "Terrain Ruggedness Index (advanced, opt-in).", advanced=True)
_register("calculate_tpi", "TPI", "Terrain", op_calculate_tpi,
          ["calculate_elevation"], 10,
          "Topographic Position Index (advanced, opt-in).", advanced=True)
_register("fill_dem", "Fill depressions (explicit)", "Hydrology", op_fill_dem,
          ["calculate_elevation"], 15,
          "Explicitly fill depressions. Not used by default hydrological tools.")
_register("breach_dem", "Breach depressions", "Hydrology", op_breach_dem,
          ["calculate_elevation"], 15,
          "Breach depressions before D8 flow analysis; no filling fallback.")
_register("calculate_flow_direction", "Flow direction", "Hydrology",
          op_calculate_flow_direction, ["breach_dem"], 12, "D8 flow direction.")
_register("calculate_flow_accumulation", "Flow accumulation", "Hydrology",
          op_calculate_flow_accumulation, ["breach_dem"], 15,
          "D8 flow accumulation (cells).")
_register("extract_streams", "Stream extraction", "Hydrology",
          op_extract_streams, ["calculate_flow_accumulation"], 5,
          "Threshold flow accumulation into a stream network. Params: "
          "threshold_cells (required; select a project-appropriate value).")
_register("calculate_stream_order", "Stream order (Strahler)", "Hydrology",
          op_calculate_stream_order,
          ["extract_streams", "calculate_flow_direction"], 12,
          "Strahler stream ordering on the D8 network.")
_register("extract_drainage_network", "Drainage network", "Hydrology",
          op_extract_drainage_network, ["calculate_stream_order"], 8,
          "Vectorise drainage with order + length attributes.")
_register("delineate_watershed", "Watershed", "Hydrology",
          op_delineate_watershed,
          ["calculate_flow_direction", "extract_drainage_network"], 12,
          "Delineate the catchment draining the AOI (auto outlet, or pass "
          "outlet=[lon,lat]). Includes area/perimeter/elevation/slope/"
          "drainage-density statistics.")
_register("calculate_drainage_density", "Drainage density", "Hydrology",
          op_calculate_drainage_density,
          ["extract_streams", "calculate_flow_direction"], 10,
          "Drainage density raster (km/km2, moving window).")
_register("calculate_distance_to_drainage", "Distance to drainage", "Hydrology",
          op_calculate_distance_to_drainage, ["extract_streams"], 8,
          "Euclidean distance (m) to the drainage network.")
_register("calculate_twi", "Topographic wetness index", "Hydrology",
          op_calculate_twi, ["calculate_flow_accumulation", "calculate_slope"],
          6, "TWI = ln(specific area / tan(slope)).")


# ---------------------------------------------------------------------------
# plan execution (spec sections 23, 24, 34)
# ---------------------------------------------------------------------------
def resolve_plan(ctx: PipelineContext, plan: list[dict]) -> list[dict]:
    """Expand a requested plan with its dependencies (in dependency order)."""
    resolved: list[dict] = []
    seen: set[str] = set()

    def add(tool: str, params: dict):
        if tool not in REGISTRY:
            raise PipelineError(
                f"Unknown tool '{tool}'. Recommended action: use one of the "
                f"registered tools: {', '.join(sorted(REGISTRY))}."
            )
        spec = REGISTRY[tool]
        for req in _req(spec.requires)(ctx):
            add(req, {})
        if tool in seen:
            return
        seen.add(tool)
        resolved.append({"tool": tool, "params": params or spec.params.copy()})

    for item in plan:
        add(item["tool"], item.get("params") or {})
    return resolved


def _gpkg_layer_name(path: str, key: str) -> str | None:
    if not path.endswith(".gpkg"):
        return None
    if key in ("drainage", "stream_order"):
        return "streams"
    if key == "watershed":
        return "watersheds"
    try:
        import pyogrio
        info = pyogrio.list_layers(path)
        return str(info[0][0]) if len(info) else None
    except Exception:
        return None


def _finalize_layer(ctx: PipelineContext, layer: LayerOut) -> LayerOut:
    """Validate output, compute statistics, render preview, write provenance."""
    if not Path(layer.path).exists():
        raise PipelineError(
            f"Output file missing for layer '{layer.key}'. "
            "Recommended action: re-run the operation; if it persists, inspect "
            "the DEM for nodata gaps.")

    if layer.type == "raster":
        vr = validation.validate_raster(
            layer.path, expect_crs=layer.crs or None,
            min_value=layer.extra.get("min_expected"),
            max_value=layer.extra.get("max_expected"))
    else:
        vr = validation.validate_vector(layer.path,
                                        layer=_gpkg_layer_name(layer.path,
                                                               layer.key))
    layer.extra["validation"] = vr
    if not vr["ok"]:
        failed = [c for c in vr["checks"] if not c["ok"]]
        raise PipelineError(
            f"Output validation failed for '{layer.name}': {failed}. "
            "Recommended action: check DEM coverage and parameters, then retry.")
    if layer.key == "flow_direction":
        conditioned = ctx.outputs.get("breached_dem")
        if conditioned and Path(conditioned.path).is_file():
            layer.extra["flow_direction_verification"] = (
                hydrology.verify_flow_direction(layer.path, conditioned.path)
            )

    # statistics (spec section 39)
    categorical_keys = {"flow_direction", "stream_order_raster"}
    class_names = None
    domain_stats = layer.extra.get("stats")
    if layer.key in categorical_keys:
        st = symbology.style_for(layer.key)
        class_names = {str(int(c["value"])): c["label"]
                       for c in st.get("classes", [])}
    try:
        if layer.type == "raster":
            computed_stats = stats_mod.stats_for_layer(
                "raster", layer.path, categorical=layer.key in categorical_keys,
                class_names=class_names)
        else:
            computed_stats = stats_mod.vector_stats(
                layer.path, layer=_gpkg_layer_name(layer.path, layer.key))
        if isinstance(domain_stats, dict):
            computed_stats.update(domain_stats)
        layer.extra["stats"] = computed_stats
    except Exception as exc:
        if isinstance(domain_stats, dict):
            layer.extra["stats_error"] = str(exc)
        else:
            layer.extra["stats"] = {"error": str(exc)}

    # preview (raster only; vectors are served as GeoJSON)
    if layer.type == "raster" and not layer.hidden:
        try:
            preview_dir = _sub(ctx, "previews")
            prev = symbology.render_raster_preview(
                layer.path, str(preview_dir / f"{layer.key}.png"), layer.key)
            layer.extra["preview"] = {
                "file": f"previews/{layer.key}.png",
                "bounds_4326": prev["bounds_4326"],
                "legend": prev["legend"],
                "value_range": prev["value_range"],
            }
        except Exception as exc:
            layer.extra["preview"] = {"error": str(exc)}

    # sidecar provenance (reproducibility, spec sections 40 & 41)
    sidecar = Path(layer.path).with_suffix(Path(layer.path).suffix + ".meta.json")
    record = layer.to_dict()
    record["created_at"] = _now()
    record["analysis_crs"] = ctx.analysis_crs
    record["dem_mode"] = ctx.dem_mode
    sidecar.write_text(json.dumps(record, indent=2, default=str),
                       encoding="utf-8")
    return layer


def run_plan(ctx: PipelineContext, plan: list[dict]) -> dict:
    """Execute a processing plan. Returns layers + execution summary.

    Progress = cumulative estimated cost; history streams through
    ctx.history_cb; cancellation is checked between operations.
    """
    resolved = resolve_plan(ctx, plan)
    weights = [REGISTRY[r["tool"]].est_seconds for r in resolved]
    total_w = max(sum(weights), 1)
    done_w = 0
    executed: list[str] = []
    layers: list[LayerOut] = []

    for item, w in zip(resolved, weights):
        ctx.check_cancel()
        tool, params = item["tool"], item["params"]
        spec = REGISTRY[tool]
        label = spec.label
        ctx.progress(done_w / total_w, f"Running: {label}")
        ctx.history(tool, f"START {label}", {"params": params})
        try:
            new_layers = spec.fn(ctx, params)
        except (PipelineCancelled, PipelineError):
            raise
        except Exception as exc:
            from .validation import diagnose
            info = diagnose(label, exc)
            ctx.history(tool, f"FAILED: {label}", info)
            raise PipelineError(
                f"Operation failed:\n{label}\n\nReason:\n{info['reason']}\n\n"
                f"Recommended action:\n{info['recommended_action']}"
            ) from exc
        for lyr in new_layers or []:
            lyr = _finalize_layer(ctx, lyr)
            layers.append(lyr)
        executed.append(tool)
        done_w += w
        ctx.progress(done_w / total_w, f"Completed: {label}")

    ctx.progress(1.0, "Completed")
    return {
        "layers": [l.to_dict() for l in layers],
        "tools_executed": executed,
        "analysis_crs": ctx.analysis_crs,
        "dem_mode": ctx.dem_mode,
    }


def plan_estimate(ctx: PipelineContext, plan: list[dict]) -> dict:
    """Estimated duration + step list for the AI plan preview (spec section 31)."""
    resolved = resolve_plan(ctx, plan)
    seconds = sum(REGISTRY[r["tool"]].est_seconds for r in resolved)
    return {
        "resolved_tools": resolved,
        "estimated_seconds": seconds,
        "estimated_label": (f"{seconds // 60} min {seconds % 60} s"
                            if seconds >= 60 else f"{seconds} s"),
        "steps": [{"tool": r["tool"], "label": REGISTRY[r["tool"]].label,
                   "group": REGISTRY[r["tool"]].group,
                   "description": REGISTRY[r["tool"]].description,
                   "params": r["params"]}
                  for r in resolved],
    }
