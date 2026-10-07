"""Hydrological analysis from a DEM (spec sections 9, 10, 16).

Backend: WhiteboxTools (mature GIS library, used via the `whitebox` Python
package) for depression filling, D8 flow direction/accumulation, stream
extraction and watershed delineation. Strahler stream ordering and a few
raster products (TWI, distance-to-drainage) are computed with NumPy/SciPy.

All rasters involved are in the PROJECTED analysis CRS (metres).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

NODATA = -9999.0

# Whitebox D8 pointer (ESRI-style) encoding -> (row_offset, col_offset)
D8_ENCODING = {
    1: (0, 1),    # E
    2: (1, 1),    # SE
    4: (1, 0),    # S
    8: (1, -1),   # SW
    16: (0, -1),  # W
    32: (-1, -1), # NW
    64: (-1, 0),  # N
    128: (-1, 1), # NE
}


class HydroError(RuntimeError):
    """Hydrological operation failed; message includes recommended action."""


def _read(path: str) -> tuple[np.ndarray, dict, float]:
    import rasterio
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
    if nodata is not None:
        arr[arr == nodata] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    cs = abs(profile["transform"].a)
    return arr, profile, cs


def _write(path: str, arr: np.ndarray, profile: dict, dtype="float32",
           nodata=NODATA, **tags) -> str:
    import rasterio
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    profile = profile.copy()
    profile.update(dtype=dtype, nodata=nodata, count=1, compress="deflate",
                   tiled=True, blockxsize=256, blockysize=256)
    tmp = str(path) + ".tmp"
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(arr.astype(dtype), 1)
        dst.update_tags(**{k: str(v) for k, v in tags.items()})
    Path(tmp).replace(Path(path))
    return str(path)


class HydroEngine:
    """Wraps WhiteboxTools with consistent error handling (spec section 35)."""

    _wbt = None

    def __init__(self):
        if HydroEngine._wbt is None:
            HydroEngine._wbt = self._init_wbt()
        self.wbt = HydroEngine._wbt

    @staticmethod
    def _init_wbt():
        try:
            import whitebox
        except ImportError as exc:
            raise HydroError(
                "WhiteboxTools Python package is not installed. "
                "Recommended action: pip install whitebox"
            ) from exc
        wbt = whitebox.WhiteboxTools()
        wbt.verbose = False
        # ensure native toolset binary is present (downloads once)
        try:
            exe = wbt.exe_path if hasattr(wbt, "exe_path") else ""
        except Exception:
            exe = ""
        if not exe or not Path(exe).exists():
            try:
                wbt.download_toolset()
            except Exception as exc:
                raise HydroError(
                    f"WhiteboxTools binary could not be downloaded: {exc}. "
                    "Recommended action: check internet access and retry the job."
                ) from exc
        return wbt

    def run(self, tool: str, outputs: list[str], **kwargs):
        """Run a whitebox tool and verify its outputs were created."""
        if not hasattr(self.wbt, tool):
            raise HydroError(
                f"WhiteboxTools does not provide the tool '{tool}'. "
                "Recommended action: update the whitebox package."
            )
        fn = getattr(self.wbt, tool)
        try:
            res = fn(**kwargs)
        except Exception as exc:
            raise HydroError(
                f"WhiteboxTools tool '{tool}' raised an error: {exc}. "
                "Recommended action: inspect DEM for nodata gaps and retry."
            ) from exc
        if isinstance(res, int) and res != 0:
            raise HydroError(
                f"WhiteboxTools tool '{tool}' failed with code {res}. "
                "Recommended action: inspect the DEM and tool parameters; "
                "no alternative conditioning method was applied."
            )
        for out in outputs:
            if not Path(out).exists() or Path(out).stat().st_size == 0:
                raise HydroError(
                    f"WhiteboxTools tool '{tool}' produced no output file. "
                    "Recommended action: verify input DEM has valid data over "
                    "the AOI."
                )
        return True

    def run_variants(self, tool: str, outputs: list[str], variants: list[dict]):
        """Try several kwarg variants of a tool (package version differences)."""
        if not hasattr(self.wbt, tool):
            raise HydroError(
                f"WhiteboxTools does not provide the tool '{tool}'. "
                "Recommended action: pip install -U whitebox"
            )
        fn = getattr(self.wbt, tool)
        last_err: Optional[Exception] = None
        for kw in variants:
            try:
                res = fn(**kw)
            except TypeError as exc:
                last_err = exc
                continue
            except Exception as exc:
                last_err = exc
                continue
            if isinstance(res, int) and res != 0:
                last_err = RuntimeError(f"exit code {res}")
                continue
            if all(Path(o).exists() and Path(o).stat().st_size > 0 for o in outputs):
                return True
            last_err = RuntimeError("output file missing")
        raise HydroError(
            f"WhiteboxTools tool '{tool}' failed ({last_err}). "
            "Recommended action: inspect the DEM and tool parameters; "
            "no alternative conditioning method was applied."
        )


# ---------------------------------------------------------------------------
# core hydrology steps (spec section 9)
# ---------------------------------------------------------------------------
def fill_dem(dem_path: str, out_path: str) -> dict:
    """Explicitly fill depressions; this is not used by the default workflow."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    engine = HydroEngine()
    engine.run_variants("fill_depressions", [out_path], [
        {"dem": dem_path, "output": out_path},
    ])
    return {"path": out_path, "algorithm": "priority-flood depression fill",
            "source_layer": str(dem_path)}


def breach_dem(dem_path: str, out_path: str) -> dict:
    """Breach depressions without silently substituting depression filling."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    engine = HydroEngine()
    engine.run_variants("breach_depressions", [out_path], [
        {"dem": dem_path, "output": out_path, "fill_pits": False},
    ])
    return {
        "path": out_path,
        "algorithm": "breach depressions (Lindsay 2016; WhiteboxTools)",
        "source_layer": str(dem_path),
        "parameters": {"fill_pits": False},
    }


def verify_flow_direction(pointer_path: str, dem_path: str,
                          max_samples: int = 512) -> dict:
    """Check sampled D8 pointers lead to equal or lower conditioned cells."""
    pointer, _, _ = _read(pointer_path)
    dem, _, _ = _read(dem_path)
    if pointer.shape != dem.shape:
        return {"passed": False, "reason": "Raster dimensions do not match.",
                "sampled_cells": 0}

    rows, cols = np.indices(pointer.shape)
    valid = np.isfinite(pointer) & np.isfinite(dem)
    downstream_rows = np.full(pointer.shape, -1, dtype=np.int64)
    downstream_cols = np.full(pointer.shape, -1, dtype=np.int64)
    for code, (dr, dc) in D8_ENCODING.items():
        cells = valid & (pointer == code)
        downstream_rows[cells] = rows[cells] + dr
        downstream_cols[cells] = cols[cells] + dc

    inside = (
        (downstream_rows >= 0) & (downstream_rows < dem.shape[0]) &
        (downstream_cols >= 0) & (downstream_cols < dem.shape[1])
    )
    candidates = np.flatnonzero(valid & inside)
    if candidates.size == 0:
        return {"passed": False, "reason": "No decodable in-bounds D8 cells.",
                "sampled_cells": 0}
    sample_positions = np.linspace(
        0, candidates.size - 1, min(max_samples, candidates.size), dtype=int)
    samples = candidates[sample_positions]
    sample_rows, sample_cols = np.unravel_index(samples, pointer.shape)
    to_rows = downstream_rows.ravel()[samples]
    to_cols = downstream_cols.ravel()[samples]
    source_z = dem[sample_rows, sample_cols]
    downstream_z = dem[to_rows, to_cols]
    tolerance = max(1e-6, float(np.nanmax(np.abs(dem))) * 1e-12)
    downhill = downstream_z <= source_z + tolerance
    passed_count = int(np.count_nonzero(downhill))
    return {
        "passed": passed_count == len(samples),
        "encoding": "ESRI D8 powers of two",
        "sampled_cells": int(len(samples)),
        "downstream_or_flat_cells": passed_count,
        "uphill_cells": int(len(samples) - passed_count),
        "tolerance": tolerance,
        "reason": None if passed_count == len(samples) else
        "Some sampled pointer directions lead to higher conditioned elevations.",
    }


def flow_direction(filled_path: str, out_path: str) -> dict:
    """D8 flow direction (ESRI-style encoding: 1=E, 2=SE, 4=S, 8=SW,
    16=W, 32=NW, 64=N, 128=NE)."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    engine = HydroEngine()
    engine.run_variants("d8_pointer", [out_path], [
        {"dem": filled_path, "output": out_path, "esri_pntr": True},
    ])
    return {"path": out_path, "algorithm": "D8 flow direction (WhiteboxTools)",
            "encoding": "ESRI-style powers of 2"}


def flow_accumulation(ptr_path: str, out_path: str) -> dict:
    """D8 flow accumulation in number of upstream cells."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    engine = HydroEngine()
    engine.run_variants("d8_flow_accumulation", [out_path], [
        {"i": ptr_path, "output": out_path, "out_type": "cells",
         "pntr": True, "esri_pntr": True},
    ])
    return {"path": out_path, "algorithm": "D8 flow accumulation (WhiteboxTools)",
            "units": "cells", "pointer_raster": ptr_path}


def extract_streams(fa_path: str, out_path: str,
                    threshold_cells: int) -> dict:
    """Threshold flow accumulation into a stream raster (configurable, spec 9)."""
    if threshold_cells < 1:
        raise HydroError(
            f"Flow accumulation threshold must be >= 1 cell (got {threshold_cells}). "
            "Recommended action: choose one of 500 / 1000 / 5000 / 10000 or a positive custom value."
        )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    engine = HydroEngine()
    engine.run_variants("extract_streams", [out_path], [
        {"flow_accum": fa_path, "output": out_path, "threshold": int(threshold_cells)},
        {"flow_accum": fa_path, "output": out_path, "threshold": float(threshold_cells)},
    ])
    return {"path": out_path, "algorithm": "flow accumulation threshold",
            "threshold_cells": int(threshold_cells),
            "threshold_note": "cells; adjust for AOI size and drainage density"}


# ---------------------------------------------------------------------------
# Strahler stream ordering (NumPy implementation on the D8 network)
# ---------------------------------------------------------------------------
def strahler_order(streams_path: str, fa_path: str, ptr_path: str,
                   out_path: str) -> dict:
    """Compute Strahler stream order raster from stream + flow accumulation +
    D8 pointer rasters. Stream heads = order 1; confluence of two equal-order
    tributaries raises the order by one."""
    import rasterio

    streams, sprof, _ = _read(streams_path)
    fa, _, _ = _read(fa_path)
    ptr, pprof, _ = _read(ptr_path)

    H, W = streams.shape
    if H != fa.shape[0] or W != fa.shape[1] or H != ptr.shape[0]:
        raise HydroError(
            "Stream, flow accumulation and flow direction rasters have different "
            "dimensions. Recommended action: re-run flow accumulation from the "
            "same filled DEM."
        )

    stream = np.isfinite(streams) & (streams > 0.5)
    if not stream.any():
        raise HydroError(
            "No stream cells found - flow accumulation threshold is too high for "
            "this AOI. Recommended action: lower the threshold (try 500 cells)."
        )

    # resolve downstream neighbour for every cell
    rows, cols = np.indices((H, W))
    tr = np.full((H, W), -1, dtype=np.int64)
    tc = np.full((H, W), -1, dtype=np.int64)
    valid_ptr = np.zeros((H, W), dtype=bool)
    for code, (dr, dc) in D8_ENCODING.items():
        m = np.isfinite(ptr) & (ptr == code)
        tr[m] = rows[m] + dr
        tc[m] = cols[m] + dc
        valid_ptr |= m
    inside = (tr >= 0) & (tr < H) & (tc >= 0) & (tc < W)
    valid_ptr &= inside

    stream_idx = np.flatnonzero(stream & np.isfinite(fa))
    if stream_idx.size == 0:
        raise HydroError("Stream raster contains no valid cells.")

    target = np.full(H * W, -1, dtype=np.int64)
    r_flat = rows.ravel()
    c_flat = cols.ravel()
    src_idx = r_flat * W + c_flat
    tgt_idx = np.where(valid_ptr.ravel(), tr.ravel() * W + tc.ravel(), -1)
    target[src_idx] = tgt_idx

    fa_flat = fa.ravel()
    # upstream lists (stream -> stream links only)
    upstream: dict[int, list[int]] = {}
    for s in stream_idx:
        t = target[s]
        if t >= 0 and stream.ravel()[t]:
            upstream.setdefault(int(t), []).append(int(s))

    order_flat = np.zeros(H * W, dtype=np.float64)
    for s in sorted(stream_idx.tolist(), key=lambda k: (fa_flat[k], k)):
        ups = upstream.get(int(s), [])
        uo = [order_flat[u] for u in ups if order_flat[u] > 0]
        if not uo:
            order_flat[s] = 1.0
        else:
            m = max(uo)
            if uo.count(m) >= 2:
                order_flat[s] = m + 1.0
            else:
                order_flat[s] = m

    order = order_flat.reshape(H, W)
    order[~stream] = 0.0
    order[~np.isfinite(streams)] = NODATA
    _write(out_path, order, sprof, dtype="float32",
           SOURCE="Derived: D8 flow accumulation + Strahler ordering",
           ALGORITHM="Strahler stream order on D8 network",
           UNITS="dimensionless order (0 = not a stream)")
    max_order = int(np.nanmax(order[order > 0])) if (order > 0).any() else 0
    if max_order < 1:
        raise HydroError("Stream ordering produced no ordered streams.")
    return {"path": out_path, "algorithm": "Strahler stream order",
            "max_order": max_order, "stream_cells": int(stream.sum())}


# ---------------------------------------------------------------------------
# vectorisation of the drainage network
# ---------------------------------------------------------------------------
def streams_to_vector(streams_path: str, order_path: str, ptr_path: str,
                      out_path: str,
                      dem_path: Optional[str] = None) -> dict:
    """Convert the stream raster to polylines (GeoPackage) and attach the
    Strahler order (max sampled along each segment) plus length attributes."""
    import geopandas as gpd
    import rasterio

    engine = HydroEngine()
    shp = Path(out_path).with_suffix(".shp")
    for f in Path(out_path).parent.glob(shp.stem + ".*"):
        f.unlink()
    engine.run_variants("raster_streams_to_vector", [str(shp)], [
        {"streams": streams_path, "d8_pntr": ptr_path, "output": str(shp),
         "esri_pntr": True},
    ])

    gdf = gpd.read_file(str(shp))
    if gdf.empty:
        raise HydroError(
            "Stream vectorisation produced no features. "
            "Recommended action: lower the flow accumulation threshold."
        )
    if gdf.crs is None:
        with rasterio.open(streams_path) as src:
            gdf = gdf.set_crs(src.crs)

    # sample the Strahler order raster at every vertex of every segment
    with rasterio.open(order_path) as ord_src:
        orders = []
        for geom in gdf.geometry:
            coords = []
            geoms = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
            for g in geoms:
                coords.extend(list(g.coords))
            vals = [v[0] for v in ord_src.sample([(c[0], c[1]) for c in coords])]
            vals = [v for v in vals if v is not None and v > 0]
            orders.append(int(max(vals)) if vals else 1)
    gdf["stream_order"] = orders
    gdf["length_m"] = gdf.geometry.length
    gdf["segment_id"] = range(1, len(gdf) + 1)
    gdf = gdf[["segment_id", "stream_order", "length_m", "geometry"]]

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(out_path).exists():
        Path(out_path).unlink()
    gdf.to_file(str(out_path), layer="streams", driver="GPKG")
    total_km = float(gdf["length_m"].sum() / 1000.0)
    return {"path": out_path, "segments": int(len(gdf)),
            "total_length_km": round(total_km, 3),
            "max_order": int(gdf["stream_order"].max()),
            "crs": str(gdf.crs)}


# ---------------------------------------------------------------------------
# outlet detection & watershed delineation (spec section 10)
# ---------------------------------------------------------------------------
def find_outlet(fa_path: str, ptr_path: str, streams_path: Optional[str] = None
                ) -> dict:
    """Auto-select the pour point: highest-accumulation cell whose flow leaves
    the raster (i.e. the AOI outlet), then snap to maximum accumulation in a
    local window."""
    fa, _, _ = _read(fa_path)
    ptr, _, _ = _read(ptr_path)
    H, W = fa.shape
    rows, cols = np.indices((H, W))
    leaving = np.zeros((H, W), dtype=bool)
    for code, (dr, dc) in D8_ENCODING.items():
        m = np.isfinite(ptr) & (ptr == code)
        tr2, tc2 = rows[m] + dr, cols[m] + dc
        out = (tr2 < 0) | (tr2 >= H) | (tc2 < 0) | (tc2 >= W)
        mm = np.zeros((H, W), dtype=bool)
        mm[m] = out
        leaving |= mm
    cand = leaving & np.isfinite(fa)
    if streams_path and Path(streams_path).exists():
        s, _, _ = _read(streams_path)
        stream_mask = np.isfinite(s) & (s > 0.5)
        if (cand & stream_mask).any():
            cand = cand & stream_mask
    if not cand.any():
        cand = np.isfinite(fa)
    fa_masked = np.where(cand, fa, -np.inf)
    r0, c0 = np.unravel_index(np.argmax(fa_masked), fa.shape)
    # Snap to the maximum accumulation in a local window.
    rad = max(3, min(50, min(H, W) // 8))
    r1, r2 = max(0, r0 - rad), min(H, r0 + rad + 1)
    c1, c2 = max(0, c0 - rad), min(W, c0 + rad + 1)
    window = fa[r1:r2, c1:c2].copy()
    window[~np.isfinite(window)] = -np.inf
    rs, cs_ = np.unravel_index(np.argmax(window), window.shape)
    r_snap, c_snap = r1 + rs, c1 + cs_
    return {
        "row": int(r_snap), "col": int(c_snap),
        "initial_row": int(r0), "initial_col": int(c0),
        "accumulation_cells": float(fa[r_snap, c_snap]),
        "method": "max flow accumulation at AOI boundary flow-exit cells, "
                  "snapped within local window",
    }


def polygonize_watershed(wtif: str, out_gpkg: str) -> dict:
    """Convert the watershed raster to polygons (GeoPackage)."""
    import rasterio
    from rasterio import features
    import geopandas as gpd
    from shapely.geometry import shape

    with rasterio.open(wtif) as src:
        data = src.read(1)
        nodata = src.nodata
        transform = src.transform
        crs = src.crs
    mask_arr = np.ones(data.shape, dtype=bool)
    if nodata is not None:
        mask_arr &= (data != nodata)
    mask_arr &= (data > 0)
    if not mask_arr.any():
        raise HydroError(
            "Watershed delineation produced an empty raster (outlet may be "
            "invalid). Recommended action: re-run with a different outlet point."
        )
    shapes = [(geom, int(val)) for geom, val in
              features.shapes(data.astype("int32"), mask=mask_arr, transform=transform)]
    if not shapes:
        raise HydroError("No watershed polygons could be created from the raster.")
    gdf = gpd.GeoDataFrame(
        {"basin_id": [v for _, v in shapes]},
        geometry=[shape(g) for g, _ in shapes], crs=crs,
    )
    gdf["area_km2"] = gdf.geometry.area / 1e6
    Path(out_gpkg).parent.mkdir(parents=True, exist_ok=True)
    if Path(out_gpkg).exists():
        Path(out_gpkg).unlink()
    gdf.to_file(out_gpkg, layer="watersheds", driver="GPKG")
    total_area = float(gdf["area_km2"].sum())
    return {"path": out_gpkg, "features": int(len(gdf)),
            "area_km2": round(total_area, 4), "crs": str(crs)}


def watershed_stats(watershed_gpkg: str, streams_gpkg: Optional[str],
                    elevation_path: str, slope_path: Optional[str]) -> dict:
    """Area, perimeter, elevation/slope statistics and drainage density of the
    delineated catchment (spec section 10)."""
    import rasterio
    from rasterio.mask import mask as rio_mask
    import geopandas as gpd
    from shapely.geometry import mapping

    basin = gpd.read_file(watershed_gpkg, layer="watersheds")
    geom = basin.union_all() if hasattr(basin, "union_all") else basin.unary_union
    area_km2 = geom.area / 1e6
    perimeter_km = geom.length / 1000.0
    stats: dict = {
        "area_km2": round(area_km2, 4),
        "perimeter_km": round(perimeter_km, 4),
    }

    def _zonal(path):
        if not path or not Path(path).exists():
            return None
        with rasterio.open(path) as src:
            nd = src.nodata if src.nodata is not None else NODATA
            try:
                img, _ = rio_mask(src, [mapping(geom)], crop=True, filled=True, nodata=nd)
            except ValueError:
                return None
        arr = img[0].astype("float64")
        arr[arr == nd] = np.nan
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return None
        return {
            "min": round(float(arr.min()), 3), "max": round(float(arr.max()), 3),
            "mean": round(float(arr.mean()), 3),
            "median": round(float(np.median(arr)), 3),
            "std": round(float(arr.std()), 3),
        }

    elev = _zonal(elevation_path)
    if elev:
        stats["elevation_m"] = elev
    if slope_path:
        sl = _zonal(slope_path)
        if sl:
            stats["slope_degrees"] = sl

    if streams_gpkg and Path(streams_gpkg).exists():
        streams = gpd.read_file(streams_gpkg, layer="streams")
        try:
            clipped = gpd.clip(streams, basin)
        except Exception:
            clipped = streams
        length_km = float(clipped.geometry.length.sum() / 1000.0) if len(clipped) else 0.0
        stats["drainage_length_km"] = round(length_km, 3)
        if area_km2 > 0:
            stats["drainage_density_km_km2"] = round(length_km / area_km2, 4)
        stats["stream_segments"] = int(len(clipped))
    return stats


# ---------------------------------------------------------------------------
# drainage density, distance, TWI (spec sections 16, 17, 9)
# ---------------------------------------------------------------------------
def drainage_density_raster(dem_path: str, streams_path: str, ptr_path: str,
                            out_path: str, window_m: float = 1000.0) -> dict:
    """Drainage density raster (km/km2), uniform moving window over stream
    network length. Uses Whitebox when available, NumPy fallback otherwise."""
    import rasterio
    engine = HydroEngine()
    if hasattr(engine.wbt, "drainage_density"):
        try:
            engine.run_variants("drainage_density", [out_path], [
                {"dem": dem_path, "streams": streams_path, "output": out_path},
                {"dem": dem_path, "stream_raster": streams_path, "output": out_path},
            ])
            return {"path": out_path, "algorithm": "WhiteboxTools drainage density",
                    "units": "km/km2", "window_m": window_m}
        except HydroError:
            pass  # fall through to the NumPy implementation

    s, _, _ = _read(streams_path)
    ptr, _, _ = _read(ptr_path)
    dem, demprof, cs = _read(dem_path)
    if s.shape != dem.shape or ptr.shape != dem.shape:
        raise HydroError(
            "DEM, stream and flow-direction rasters differ in size. "
            "Recommended action: regenerate hydrology products from the same DEM grid."
        )
    stream = np.isfinite(s) & (s > 0.5)
    link = np.full(s.shape, cs, dtype="float64")
    for code in (2, 8, 32, 128):          # diagonal D8 codes
        link[np.isfinite(ptr) & (ptr == code)] = cs * np.sqrt(2.0)
    link[~stream] = 0.0
    from scipy.ndimage import uniform_filter
    window_cells = max(3, int(round(window_m / cs)) * 2 + 1)
    mean_len = uniform_filter(link, size=window_cells, mode="constant")
    density = mean_len * 1000.0 / (cs * cs)          # km/km2
    density[~np.isfinite(dem)] = NODATA
    _write(out_path, np.where(np.isfinite(density), density, NODATA), demprof,
           SOURCE="Derived: drainage network (Copernicus DEM via D8)",
           ALGORITHM=f"uniform moving-window drainage density, window={window_m} m",
           UNITS="km/km2")
    return {"path": out_path,
            "algorithm": f"uniform moving-window drainage density ({window_m} m window)",
            "units": "km/km2"}


def distance_to_drainage(streams_path: str, out_path: str) -> dict:
    """Euclidean distance (metres) from every cell to the nearest stream cell."""
    from scipy.ndimage import distance_transform_edt
    s, sprof, cs = _read(streams_path)
    stream = np.isfinite(s) & (s > 0.5)
    if not stream.any():
        raise HydroError(
            "No stream cells present for distance analysis. "
            "Recommended action: lower the flow accumulation threshold first."
        )
    dist = distance_transform_edt(~stream, sampling=cs)
    _write(out_path, dist.astype("float32"), sprof,
           SOURCE="Derived: drainage network distance",
           ALGORITHM="Euclidean distance transform", UNITS="metres")
    return {"path": out_path, "units": "metres",
            "algorithm": "Euclidean distance transform (SciPy)"}


def topographic_wetness_index(fa_path: str, slope_path: str, out_path: str) -> dict:
    """TWI = ln( specific upslope area / tan(slope) )."""
    fa, fprof, cs = _read(fa_path)
    slope, _, _ = _read(slope_path)
    if fa.shape != slope.shape:
        raise HydroError(
            "Flow accumulation and slope rasters differ in size. "
            "Recommended action: regenerate slope from the same DEM grid."
        )
    area_m2 = np.maximum(fa, 1.0) * cs * cs
    slope_deg = np.clip(np.where(np.isfinite(slope), slope, 0.0), 0.05, 90.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        twi = np.log(area_m2 / np.tan(np.radians(slope_deg)))
    twi[~np.isfinite(fa) | ~np.isfinite(slope)] = NODATA
    _write(out_path, np.where(np.isfinite(twi), twi, NODATA), fprof,
           SOURCE="Derived: flow accumulation + slope",
           ALGORITHM="TWI = ln(a / tan(beta))", UNITS="dimensionless")
    return {"path": out_path, "algorithm": "TWI = ln(specific area / tan slope)",
            "units": "dimensionless"}


def delineate_watershed(ptr_path: str, outlet_rc: dict, out_tif: str,
                        workdir: Path) -> dict:
    """Delineate the upstream catchment for one outlet point."""
    import rasterio
    import geopandas as gpd
    from shapely.geometry import Point

    with rasterio.open(ptr_path) as src:
        transform = src.transform
        crs = src.crs
    x, y = transform * (outlet_rc["col"] + 0.5, outlet_rc["row"] + 0.5)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pts_shp = workdir / "outlet.shp"
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(x, y)], crs=crs)
    gdf.to_file(str(pts_shp))

    engine = HydroEngine()
    engine.run_variants("watershed", [out_tif], [
        {"d8_pntr": ptr_path, "pour_pts": str(pts_shp), "output": out_tif,
         "esri_pntr": True},
    ])
    return {"path": out_tif, "outlet_xy": [round(x, 2), round(y, 2)],
            "outlet_crs": str(crs), "outlet_info": outlet_rc}
