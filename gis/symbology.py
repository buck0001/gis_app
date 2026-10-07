"""Map symbology (spec section 36).

Central registry of sensible default styles per layer type. Every style is a
plain dict the frontend (MapLibre) or the PNG map generator can consume, and
users can override it (overrides are stored with the layer).
"""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np

CONTINUOUS = "continuous"
CATEGORICAL = "categorical"
LINES = "lines"

STYLES: dict[str, dict] = {
    "elevation": {"kind": CONTINUOUS, "colormap": "viridis", "units": "m",
                  "label": "Elevation", "stretch": "minmax"},
    "filled_dem": {"kind": CONTINUOUS, "colormap": "terrain", "units": "m",
                   "label": "Filled DEM", "stretch": "minmax"},
    "slope": {"kind": CONTINUOUS, "colormap": "YlOrBr", "units": "degrees",
              "label": "Slope", "stretch": "minmax"},
    "aspect": {"kind": CONTINUOUS, "colormap": "twilight", "units": "degrees",
               "label": "Aspect (clockwise from north)", "stretch": "minmax",
               "classes": [
                   {"value": -1, "label": "Flat", "color": "#cccccc"},
                   {"value": 0, "label": "N (0-45)", "color": "#1a9850"},
                   {"value": 45, "label": "NE (45-90)", "color": "#a6d96a"},
                   {"value": 90, "label": "E (90-135)", "color": "#ffd92f"},
                   {"value": 135, "label": "SE (135-180)", "color": "#fdae61"},
                   {"value": 180, "label": "S (180-225)", "color": "#f46d43"},
                   {"value": 225, "label": "SW (225-270)", "color": "#d73027"},
                   {"value": 270, "label": "W (270-315)", "color": "#7b3294"},
                   {"value": 315, "label": "NW (315-360)", "color": "#c51b7d"},
               ]},
    "hillshade": {"kind": CONTINUOUS, "colormap": "gray", "units": "0-255",
                  "label": "Hillshade", "stretch": "minmax"},
    "curvature": {"kind": CONTINUOUS, "colormap": "RdBu_r", "units": "1/m x1000",
                  "label": "Curvature", "stretch": "percentile"},
    "roughness": {"kind": CONTINUOUS, "colormap": "copper", "units": "m",
                  "label": "Roughness", "stretch": "percentile"},
    "tri": {"kind": CONTINUOUS, "colormap": "copper", "units": "m",
            "label": "TRI", "stretch": "percentile"},
    "tpi": {"kind": CONTINUOUS, "colormap": "RdBu_r", "units": "m",
            "label": "TPI", "stretch": "percentile"},
    "flow_direction": {"kind": CATEGORICAL, "colormap": "twilight", "units": "",
                       "label": "Flow direction (D8)", "classes": [
                           {"value": 0, "label": "Flat / undefined", "color": "#bdbdbd"},
                           {"value": 64, "label": "N", "color": "#6a3d9a"},
                           {"value": 128, "label": "NE", "color": "#1f78b4"},
                           {"value": 1, "label": "E", "color": "#00a6ca"},
                           {"value": 2, "label": "SE", "color": "#33a02c"},
                           {"value": 4, "label": "S", "color": "#f1c40f"},
                           {"value": 8, "label": "SW", "color": "#ff7f00"},
                           {"value": 16, "label": "W", "color": "#e31a1c"},
                           {"value": 32, "label": "NW", "color": "#b15928"},
                       ]},
    "flow_accumulation": {"kind": CONTINUOUS, "colormap": "viridis",
                          "units": "cells", "label": "Flow accumulation",
                          "stretch": "log"},
    "drainage_density": {"kind": CONTINUOUS, "colormap": "Blues",
                         "units": "km/km2", "label": "Drainage density",
                         "stretch": "percentile"},
    "distance_to_drainage": {"kind": CONTINUOUS, "colormap": "viridis",
                             "units": "m", "label": "Distance to drainage",
                             "stretch": "percentile"},
    "twi": {"kind": CONTINUOUS, "colormap": "YlGnBu", "units": "dimensionless",
            "label": "Topographic wetness index", "stretch": "percentile"},
    "stream_order": {"kind": CATEGORICAL, "colormap": "winter", "units": "",
                     "label": "Stream order (Strahler)", "classes": [
                         {"value": 1, "label": "Order 1", "color": "#c6dbef"},
                         {"value": 2, "label": "Order 2", "color": "#6baed6"},
                         {"value": 3, "label": "Order 3", "color": "#3182bd"},
                         {"value": 4, "label": "Order 4", "color": "#08519c"},
                         {"value": 5, "label": "Order 5+", "color": "#08306b"},
                     ]},
    "drainage": {"kind": LINES, "label": "Drainage network",
                 "line_color": "#2b6cb0", "line_width": 1.2,
                 "line_width_by_order": True},
    "watershed": {"kind": "polygon", "label": "Watershed",
                  "fill_color": "#3182bd", "fill_opacity": 0.15,
                  "line_color": "#08519c", "line_width": 2},
    "aoi": {"kind": "polygon", "label": "Area of interest",
            "fill_color": "#e53e3e", "fill_opacity": 0.06,
            "line_color": "#e53e3e", "line_width": 2, "line_dash": [6, 4]},
}


def style_for(layer_key: str) -> dict:
    """Return a copy of the default style for a layer key (never None)."""
    base = STYLES.get(layer_key)
    if base is None:
        base = {"kind": CONTINUOUS, "colormap": "viridis", "label": layer_key,
                "stretch": "minmax"}
    return copy.deepcopy(base)



# ---------------------------------------------------------------------------
# raster -> RGBA preview PNG (browser-friendly, no giant GeoTIFF in browser)
# ---------------------------------------------------------------------------
def _get_cmap(name: str):
    import matplotlib
    return matplotlib.colormaps.get_cmap(name)


def _stretch(data: np.ndarray, mode: str) -> tuple[float, float]:
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0, 1.0
    if mode == "log":
        pos = finite[finite > 0]
        lo = float(pos.min()) if pos.size else 1.0
        hi = float(finite.max())
        return max(lo, 1.0), max(hi, max(lo, 1.0) + 1.0)
    if mode == "percentile":
        return float(np.percentile(finite, 2)), float(np.percentile(finite, 98))
    return float(finite.min()), float(finite.max())


def render_raster_preview(src_path: str, out_png: str, layer_key: str,
                          max_size: int = 1600, style: dict | None = None) -> dict:
    """Render a styled RGBA PNG preview + WGS84 bounds + legend.
    Nodata -> transparent. Suitable for a georeferenced image overlay."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from PIL import Image
    from matplotlib.colors import Normalize, to_rgb

    st = style or style_for(layer_key)
    with rasterio.open(src_path) as src:
        h, w = src.height, src.width
        scale = min(1.0, max_size / max(h, w))
        out_h, out_w = max(1, int(h * scale)), max(1, int(w * scale))
        data = src.read(1, out_shape=(out_h, out_w), masked=True,
                        resampling=Resampling.nearest)
        nodata = src.nodata
        bounds4326 = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)

    filled = data.astype("float64").filled(np.nan)
    arr = filled.copy()
    if nodata is not None:
        arr[filled == nodata] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    valid = np.isfinite(arr)

    legend: list[dict] = []
    rgba = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    if not valid.any():
        Image.fromarray(rgba, mode="RGBA").save(out_png)
        return {"png": str(out_png), "bounds_4326": [round(v, 8) for v in bounds4326],
                "legend": [], "style": st,
                "value_range": {"min": None, "max": None}}

    if st.get("kind") == CATEGORICAL and st.get("classes") \
            and layer_key != "aspect":
        lut: dict[float, tuple] = {}
        for cls in st["classes"]:
            lut[float(cls["value"])] = to_rgb(cls["color"])
        values = np.unique(arr[valid])
        keys, colors = [], []
        for v in values:
            if v in lut:
                keys.append(v); colors.append(lut[v])
            else:
                keys.append(v); colors.append((0.5, 0.5, 0.5))
        key_to_i = {k: i for i, k in enumerate(keys)}
        flat = np.zeros(arr.size, dtype=int)
        for k, i in key_to_i.items():
            flat[arr.ravel() == k] = i
        idx = flat.reshape(arr.shape)
        col_arr = np.array(colors)
        rgba[valid, :3] = (col_arr[idx[valid]] * 255).astype(np.uint8)
        rgba[valid, 3] = 255
        legend = [{"label": c["label"], "color": c["color"]} for c in st["classes"]]
    elif layer_key == "aspect":
        cmap = _get_cmap("twilight").copy()
        cmap.set_under("#bdbdbd")
        rgb = cmap(Normalize(0, 360, clip=False)(arr))[..., :3]
        rgba[valid, :3] = (rgb[valid] * 255).astype(np.uint8)
        rgba[valid, 3] = 255
        legend = [
            {"label": label, "color": "#%02x%02x%02x" % tuple(
                int(c * 255) for c in cmap(value / 360)[:3])}
            for value, label in [(0, "N"), (45, "NE"), (90, "E"),
                                 (135, "SE"), (180, "S"), (225, "SW"),
                                 (270, "W"), (315, "NW"), (360, "N")]
        ]
        legend.append({"label": "Flat", "color": "#bdbdbd"})
    else:
        lo, hi = _stretch(arr, st.get("stretch", "minmax"))
        if hi <= lo:
            hi = lo + 1e-9
        if st.get("stretch") == "log":
            l0, l1 = np.log10(max(lo, 1e-9)), np.log10(max(hi, max(lo, 1e-9) + 1e-9))
            norm = np.clip((np.log10(np.maximum(arr, 1e-9)) - l0) / (l1 - l0 + 1e-12), 0, 1)
        else:
            norm = np.clip((arr - lo) / (hi - lo), 0, 1)
        cmap = _get_cmap(st.get("colormap", "viridis"))
        rgb = cmap(norm)[..., :3]
        rgba[valid, :3] = (rgb[valid] * 255).astype(np.uint8)
        rgba[valid, 3] = 255
        for i in range(5):
            val = lo + (hi - lo) * i / 4.0
            color = cmap(i / 4.0)[:3]
            legend.append({
                "label": f"{val:,.1f}",
                "color": "#%02x%02x%02x" % tuple(int(c * 255) for c in color),
                "value": round(val, 4),
            })
        legend.reverse()

    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(out_png, optimize=True)
    return {
        "png": str(out_png),
        "bounds_4326": [round(v, 8) for v in bounds4326],
        "legend": legend,
        "style": st,
        "value_range": {
            "min": round(float(np.nanmin(arr)), 4),
            "max": round(float(np.nanmax(arr)), 4),
        },
    }
