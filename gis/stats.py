"""Statistics engine (spec section 39).

Raster: min/max/mean/median/std/percentiles.
Categorical raster: area + percentage per class.
Vector: feature count, total length, total area, density.
All area/length values come from the PROJECTED analysis CRS grid.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

DEFAULT_PERCENTILES = (5, 25, 50, 75, 95)


def raster_stats(path: str, percentiles=DEFAULT_PERCENTILES) -> dict:
    import rasterio
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True)
        cs = abs(src.transform.a)
        nodata = src.nodata
    data = np.ma.compressed(arr).astype("float64")
    data = data[np.isfinite(data)]
    if data.size == 0:
        return {"valid_pixels": 0, "note": "raster contains no valid pixels"}
    pct = {f"p{p}": round(float(np.percentile(data, p)), 4) for p in percentiles}
    return {
        "min": round(float(data.min()), 4),
        "max": round(float(data.max()), 4),
        "mean": round(float(data.mean()), 4),
        "median": round(float(np.median(data)), 4),
        "std": round(float(data.std()), 4),
        **pct,
        "valid_pixels": int(data.size),
        "cell_size_m": round(float(cs), 3),
        "nodata": nodata,
    }


def categorical_stats(path: str, class_names: dict | None = None,
                      max_classes: int = 64) -> dict:
    """Area and percentage per class for a categorical raster (e.g. stream
    order, LULC). Class names are ONLY attached when supplied - never invented."""
    import rasterio
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True)
        cs = abs(src.transform.a)
        nodata = src.nodata
    data = np.ma.compressed(arr)
    data = data[np.isfinite(data)]
    if data.size == 0:
        return {"classes": [], "note": "raster contains no valid pixels"}
    if data.dtype.kind == "f" and np.allclose(data, np.round(data)):
        data = data.astype("int64")
    uniq, counts = np.unique(data, return_counts=True)
    if uniq.size > max_classes:
        return {"classes": [], "note": f"{uniq.size} unique values - too many for categorical stats"}
    cell_area_km2 = (cs * cs) / 1_000_000.0
    total = int(counts.sum())
    classes = []
    for v, c in zip(uniq.tolist(), counts.tolist()):
        key = str(int(v)) if isinstance(v, (int, np.integer)) or float(v).is_integer() else str(v)
        classes.append({
            "value": int(v) if float(v).is_integer() else float(v),
            "label": (class_names or {}).get(key, (class_names or {}).get(str(v), f"class {key}")),
            "pixels": int(c),
            "area_km2": round(c * cell_area_km2, 4),
            "percent": round(100.0 * c / total, 3),
        })
    return {"classes": classes, "cell_size_m": round(float(cs), 3),
            "nodata": nodata, "total_pixels": total}


def vector_stats(path: str, layer: str | None = None) -> dict:
    import geopandas as gpd
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    if gdf.empty:
        return {"feature_count": 0, "note": "layer contains no features"}
    geom_types = sorted(set(gdf.geom_type.dropna().tolist()))
    out = {
        "feature_count": int(len(gdf)),
        "geometry_types": geom_types,
        "crs": str(gdf.crs),
        "bbox": [round(float(v), 6) for v in gdf.total_bounds],
    }
    if any(t in ("Polygon", "MultiPolygon") for t in geom_types):
        out["total_area_km2"] = round(float(gdf.geometry.area.sum() / 1e6), 4)
    if any(t in ("LineString", "MultiLineString") for t in geom_types):
        out["total_length_km"] = round(float(gdf.geometry.length.sum() / 1000.0), 4)
    if "stream_order" in gdf.columns:
        out["stream_order_max"] = int(gdf["stream_order"].max())
        out["stream_order_breakdown"] = {
            str(int(k)): int(v) for k, v in
            gdf["stream_order"].value_counts().sort_index().items()
        }
    return out


def stats_for_layer(layer_type: str, path: str, categorical: bool = False,
                    class_names: dict | None = None) -> dict:
    """Dispatch statistics by layer type."""
    if layer_type == "raster":
        base = raster_stats(path)
        if categorical and base.get("valid_pixels", 0) > 0:
            base["categorical"] = categorical_stats(path, class_names)
        return base
    return vector_stats(path)
