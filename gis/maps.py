"""Professional map generator (spec section 29).

Every generated map contains: title, map frame, legend, north arrow, scale bar,
coordinate grid, source, projection, resolution, date, AOI name, generator
credit and a disclaimer. Output: PNG / PDF / SVG via matplotlib.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
import json
from pathlib import Path
import textwrap

import numpy as np

MAP_BRANDING = {
    "name": "GIS Mapper",
    "author": "buck0001",
    "url": "https://github.com/buck0001/",
}
GENERATOR_LINE = f"{MAP_BRANDING['name']} by {MAP_BRANDING['author']}"


def _nice_distance(length_m: float) -> float:
    """Pick a 'nice' scale-bar length (m) for about 1/4 of the map width."""
    target = max(length_m, 1.0) / 4.0
    exp = math.floor(math.log10(target)) if target > 0 else 0
    base = 10 ** exp
    for mult in (1, 2, 5, 10):
        if mult * base >= target:
            return mult * base
    return 10 * base


def _format_number(value: float) -> str:
    """Format values with grouping and fixed-point notation."""
    if not math.isfinite(value) or value == 0:
        return "0"
    decimals = max(
        0, min(8, 3 - math.floor(math.log10(abs(value))))
    )
    formatted = f"{value:,.{decimals}f}"
    return formatted.rstrip("0").rstrip(".") if decimals else formatted


def _draw_graticule(ax, crs_str: str, bounds4326, minx, miny, maxx, maxy):
    """Draw a lon/lat coordinate grid over the projected map frame."""
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", crs_str, always_xy=True)
    lon0, lat0, lon1, lat1 = bounds4326
    span_lon, span_lat = lon1 - lon0, lat1 - lat0
    step = _nice_distance(max(span_lon, 1e-6) * 111_000) / 111_000
    if step <= 0:
        step = 0.01
    lon = math.floor(lon0 / step) * step
    lats = []
    lat = math.floor(lat0 / step) * step
    while lat <= lat1 + step:
        lats.append(lat)
        lat += step
    while lon <= lon1 + step:
        try:
            xs, ys = tr.transform([lon, lon], [lat0, lat1])
            ax.plot(xs, ys, color="0.45", linewidth=0.5, zorder=2)
            ax.text(xs[0], ys[0], f"{lon:.4g}\u00b0", ha="center", va="top",
                    fontsize=7, color="0.3", clip_on=True)
        except Exception:
            pass
        lon += step
    for lat in lats:
        try:
            xs, ys = tr.transform([lon0, lon1], [lat, lat])
            ax.plot(xs, ys, color="0.45", linewidth=0.5, zorder=2)
            ax.text(xs[0], ys[0], f"{lat:.4g}\u00b0", ha="right", va="center",
                    fontsize=7, color="0.3", clip_on=True, rotation=90)
        except Exception:
            pass



def generate_map(
    out_path: str,
    title: str,
    layer_type: str,                 # raster | vector
    layer_path: str,
    aoi_geojson: dict,
    analysis_crs: str,
    metadata: dict,                  # source/resolution/date/units/processing...
    layer_key: str = "elevation",
    style: dict | None = None,
    fmt: str = "png",
    dpi: int = 300,
    disclaimer: str | None = None,
    flow_direction_arrows: bool = False,
) -> dict:
    """Render a cartographic layout and save it. Returns {'path', 'format'}."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize, ListedColormap, BoundaryNorm
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    import geopandas as gpd
    from shapely.geometry import shape

    from .symbology import style_for, _stretch, _get_cmap

    st = style or style_for(layer_key)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    fig = plt.figure(figsize=(11.69, 8.27))
    ax = fig.add_axes([0.07, 0.24, 0.68, 0.65])
    legend_ax = fig.add_axes([0.79, 0.25, 0.14, 0.58])
    legend_ax.set_axis_off()
    panel = fig.add_axes([0.05, 0.02, 0.92, 0.20])
    panel.axis("off")

    colorbar_obj = None
    shown_labels: list[str] = []
    shown_handles: list = []
    raster_crs = analysis_crs
    raster_details = {}
    original_dem_path = metadata.get("original_dem_path")
    if original_dem_path and Path(original_dem_path).is_file():
        import rasterio
        with rasterio.open(original_dem_path) as src:
            metadata["original_dem"] = {
                "source_file": Path(original_dem_path).name,
                "width": src.width,
                "height": src.height,
                "resolution": [abs(src.transform.a), abs(src.transform.e)],
                "bounds": list(src.bounds),
                "crs": str(src.crs) if src.crs else "Not available",
                "preserved": Path(original_dem_path).resolve()
                != Path(layer_path).resolve(),
            }
    else:
        metadata["original_dem"] = None

    if layer_type == "raster":
        import rasterio
        from rasterio.plot import plotting_extent
        with rasterio.open(layer_path) as src:
            data = src.read(1, masked=True)
            extent = plotting_extent(src)
            transform = src.transform
            nodata = src.nodata
            raster_crs = str(src.crs)
            if src.crs is None:
                raise ValueError(
                    f"Raster layer '{layer_key}' has no CRS; refusing to guess one.")
            raster_details = {
                "width": src.width,
                "height": src.height,
                "resolution": [abs(src.transform.a), abs(src.transform.e)],
                "bounds": list(src.bounds),
            }
        arr = data.astype("float64").filled(np.nan)
        if nodata is not None:
            arr[arr == nodata] = np.nan
        if not np.isfinite(arr).any():
            raise ValueError(
                f"Raster layer '{layer_key}' has no valid cells to render.")
        hillshade_path = metadata.get("hillshade_path")
        metadata["hillshade_underlay_used"] = False
        if (layer_key != "hillshade" and hillshade_path
                and Path(hillshade_path).is_file()):
            with rasterio.open(hillshade_path) as shade_src:
                if shade_src.crs != raster_crs:
                    raise ValueError(
                        "Hillshade underlay CRS does not match the selected layer.")
                shade = shade_src.read(1, masked=True).astype("float32")
                ax.imshow(
                    shade, extent=plotting_extent(shade_src), cmap="gray",
                    interpolation="nearest", alpha=0.48, zorder=1,
                )
                metadata["hillshade_underlay_used"] = True

        if layer_key == "aspect":
            cmap = _get_cmap("twilight").copy()
            cmap.set_under("#bdbdbd")
            im = ax.imshow(
                arr, extent=extent, cmap=cmap,
                norm=Normalize(vmin=0, vmax=360, clip=False),
                interpolation="nearest", alpha=0.68, zorder=2,
            )
            colorbar_obj = im
        elif st.get("kind") == "categorical":
            classes = sorted(st["classes"], key=lambda c: c["value"])
            cmap = ListedColormap([c["color"] for c in classes])
            edges = [c["value"] - 0.5 for c in classes] + [classes[-1]["value"] + 0.5]
            norm = BoundaryNorm(edges, cmap.N)
            show = np.where(np.isfinite(arr), np.clip(arr, edges[0], edges[-1]), np.nan)
            if layer_key == "flow_direction" and flow_direction_arrows:
                rows, cols = np.indices(arr.shape)
                sampled = (
                    (rows % 3 == 0) & (cols % 3 == 0) &
                    np.isfinite(arr) & np.isin(arr, [1, 2, 4, 8, 16, 32, 64, 128])
                )
                sample_rows, sample_cols = np.where(sampled)
                x = transform.c + (sample_cols + 0.5) * transform.a
                y = transform.f + (sample_rows + 0.5) * transform.e
                vectors = {
                    1: (1, 0), 2: (1, -1), 4: (0, -1), 8: (-1, -1),
                    16: (-1, 0), 32: (-1, 1), 64: (0, 1), 128: (1, 1),
                }
                dx = np.zeros(len(sample_rows), dtype="float64")
                dy = np.zeros(len(sample_rows), dtype="float64")
                for code, (east, north) in vectors.items():
                    direction = arr[sample_rows, sample_cols] == code
                    dx[direction] = east * abs(transform.a) * 0.7
                    dy[direction] = north * abs(transform.e) * 0.7
                ax.quiver(
                    x, y, dx, dy, angles="xy", scale_units="xy", scale=1,
                    color="#202020", width=0.0028, headwidth=3.5,
                    headlength=4.5, zorder=4,
                )
                shown_labels.append("D8 flow arrows (every 3 cells)")
                shown_handles.append(Line2D(
                    [], [], color="#202020", marker=">", linestyle="None",
                    label="D8 flow direction"))
                metadata["flow_arrow_stride_cells"] = 3
                colorbar_obj = None
            else:
                ax.imshow(show, extent=extent, cmap=cmap, norm=norm,
                          interpolation="nearest", alpha=0.6, zorder=2)
                colorbar_obj = None
            present = set(np.unique(arr[np.isfinite(arr)]).tolist())
            classes = [c for c in classes if c["value"] in present]
            if layer_key == "flow_direction":
                order = [64, 128, 1, 2, 4, 8, 16, 32]
                classes.sort(key=lambda c: order.index(c["value"])
                             if c["value"] in order else len(order))
            if not (layer_key == "flow_direction" and flow_direction_arrows):
                colorbar_obj = ("categorical", classes)
        else:
            lo, hi = _stretch(arr, st.get("stretch", "minmax"))
            if hi <= lo:
                hi = lo + 1e-9
            if st.get("stretch") == "log":
                norm = matplotlib.colors.LogNorm(vmin=max(lo, 1e-9), vmax=hi)
            else:
                norm = Normalize(vmin=lo, vmax=hi)
            im = ax.imshow(arr, extent=extent,
                           cmap=_get_cmap(st.get("colormap", "viridis")),
                           norm=norm, interpolation="nearest",
                           alpha=1.0 if layer_key == "hillshade" else 0.68,
                           zorder=1 if layer_key == "hillshade" else 2)
            colorbar_obj = None if layer_key == "hillshade" else im
            if layer_key == "flow_accumulation":
                positive = arr[np.isfinite(arr) & (arr > 0)]
                if positive.size:
                    lo = max(float(positive.min()), 1.0)
                    hi = max(float(positive.max()), lo)
                    im.set_norm(matplotlib.colors.LogNorm(vmin=lo, vmax=hi))
    else:
        gdf = gpd.read_file(layer_path)
        if gdf.crs is None:
            raise ValueError(
                f"Vector layer '{layer_key}' has no CRS; refusing to assign one.")
        raster_crs = str(gdf.crs)
        raster_details = {"feature_count": int(len(gdf))}
        hillshade_path = metadata.get("hillshade_path")
        if hillshade_path and Path(hillshade_path).is_file():
            import rasterio
            from rasterio.plot import plotting_extent
            with rasterio.open(hillshade_path) as src:
                if src.crs != gdf.crs:
                    raise ValueError(
                        "Hillshade underlay CRS does not match the selected layer.")
                shade = src.read(1, masked=True).astype("float32")
                ax.imshow(
                    shade, extent=plotting_extent(src), cmap="gray",
                    interpolation="nearest", alpha=0.48, zorder=1,
                )
            metadata["hillshade_underlay_used"] = True
        else:
            metadata["hillshade_underlay_used"] = False
        if layer_key in ("drainage", "stream_order") and "stream_order" in gdf.columns:
            cls_colors = {float(c["value"]): c["color"]
                          for c in st.get("classes", [])}
            for o in sorted(gdf["stream_order"].unique()):
                sub = gdf[gdf["stream_order"] == o]
                if layer_key == "stream_order" and cls_colors:
                    color = cls_colors.get(float(o)) or \
                        cls_colors.get(max((k for k in cls_colors if k <= float(o)),
                                           default=0)) or "#2b6cb0"
                else:
                    color = st.get("line_color", "#2b6cb0")
                lw = max(0.5, min(3.5, 0.45 * float(o) + 0.5))
                sub.plot(ax=ax, color=color, linewidth=lw, label=f"Order {int(o)}")
                shown_labels.append(f"Order {int(o)}")
                shown_handles.append(Line2D(
                    [], [], color=color, linewidth=lw, label=f"Order {int(o)}"))
        elif layer_key == "watershed":
            gdf.plot(ax=ax, facecolor=st.get("fill_color", "#3182bd"),
                     alpha=0.25, edgecolor=st.get("line_color", "#08519c"),
                     linewidth=st.get("line_width", 2))
            shown_labels.append("Watershed boundary")
            shown_handles.append(
                Patch(facecolor=st.get("fill_color", "#3182bd"),
                      edgecolor=st.get("line_color", "#08519c"),
                      alpha=0.25, label="Watershed boundary"))
        else:
            gdf.plot(ax=ax, color=st.get("line_color", "#2b6cb0"),
                     linewidth=st.get("line_width", 1.2))

    aoi = gpd.GeoDataFrame(geometry=[shape(aoi_geojson)], crs="EPSG:4326")
    aoi = aoi.to_crs(raster_crs)
    aoi.boundary.plot(ax=ax, color="#e53e3e", linewidth=1.4, linestyle="--")
    if layer_key == "watershed":
        pour_point = metadata.get("pour_point") or {}
        coordinates = pour_point.get("coordinates_lonlat")
        if coordinates:
            from pyproj import Transformer
            to_map_crs = Transformer.from_crs(
                "EPSG:4326", raster_crs, always_xy=True)
            px, py = to_map_crs.transform(*coordinates)
            ax.scatter(px, py, marker="*", s=120, color="#ffcc00",
                       edgecolor="black", linewidth=0.8, zorder=8)
            shown_labels.append("Pour point")
            shown_handles.append(Line2D(
                [], [], marker="*", linestyle="None", markerfacecolor="#ffcc00",
                markeredgecolor="black", label="Pour point"))

    ax.set_aspect("equal")
    fig.text(0.41, 0.965, title, ha="center", va="top",
             fontsize=14, fontweight="bold")
    bounds = (ax.get_xlim()[0], ax.get_ylim()[0], ax.get_xlim()[1], ax.get_ylim()[1])
    metadata = {
        **metadata,
        "analysis_crs": analysis_crs,
        "units": metadata.get("units") or st.get("units"),
        "raster_details": raster_details,
        "output_dpi": dpi,
        "output_page": "A4 landscape",
        "flow_direction_arrows": flow_direction_arrows,
        "raster_interpolation": "nearest",
        "axis_scientific_notation": False,
        "axis_grid_aligned": True,
        "branding": MAP_BRANDING.copy(),
    }
    return _finish_map(fig, ax, panel, out_path, title, layer_key, st, metadata,
                       analysis_crs, raster_crs, colorbar_obj, shown_labels,
                       shown_handles,
                       aoi_geojson, fmt, dpi, disclaimer, bounds, legend_ax)


def _finish_map(fig, ax, panel, out_path, title, layer_key, st, metadata,
                analysis_crs, raster_crs, colorbar_obj, shown_labels,
                shown_handles,
                aoi_geojson, fmt, dpi, disclaimer, bounds, legend_ax):
    """Graticule, scale bar, north arrow, legend, info panel, save."""
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    from pyproj import CRS, Transformer
    from rasterio.warp import transform_bounds

    minx, miny, maxx, maxy = bounds
    width, height = maxx - minx, maxy - miny
    crs = CRS.from_user_input(raster_crs)
    geographic = crs.is_geographic
    axis_info = crs.axis_info
    unit_name = axis_info[0].unit_name if axis_info else "unknown units"
    metres_per_unit = (
        axis_info[0].unit_conversion_factor
        if axis_info and not geographic else None
    )
    metadata["crs_datum"] = crs.datum.name if crs.datum else "Not available"
    metadata["crs_verified"] = bool(raster_crs and raster_crs != "None")
    metadata["vertical_datum"] = (
        metadata.get("vertical_datum") or "Not available"
    )
    resolution = metadata.get("raster_details", {}).get("resolution")
    if resolution and metres_per_unit:
        metadata["cell_area_ha"] = (
            resolution[0] * resolution[1] * metres_per_unit ** 2 / 10_000
        )

    # ---- coordinate grid ---------------------------------------------------
    try:
        b4326 = transform_bounds(raster_crs, "EPSG:4326", *bounds, densify_pts=21)
    except Exception:
        b4326 = None
    ax.ticklabel_format(axis="both", style="plain", useOffset=False)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    if geographic:
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.4f}°"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.4f}°"))
        ax.set_xlabel("Longitude (°)")
        ax.set_ylabel("Latitude (°)")
    else:
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.set_xlabel(f"Easting ({unit_name})")
        ax.set_ylabel(f"Northing ({unit_name})")
    ax.grid(True, color="0.45", linewidth=0.5, alpha=0.45, zorder=3)
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    if geographic:
        centre_lat = (miny + maxy) / 2.0
        ax.set_aspect(1 / max(abs(np.cos(np.radians(centre_lat))), 1e-6))
    else:
        ax.set_aspect("equal")

    # ---- scale bar ---------------------------------------------------------
    bar_m = _nice_distance(width * metres_per_unit) if metres_per_unit else 0
    bar = bar_m / metres_per_unit if metres_per_unit else 0
    x0 = maxx - bar - 0.06 * width
    y0 = miny + 0.07 * height
    seg = bar / 2.0
    bh = height * 0.014
    if bar > 0 and bar < width * 0.5:
        for i in range(2):
            ax.add_patch(Rectangle((x0 + i * seg, y0), seg, bh,
                                   facecolor="black" if i % 2 == 0 else "white",
                                   edgecolor="black", linewidth=0.8, zorder=6))
        label = f"{bar_m:,.0f} m" if bar_m < 1000 else f"{bar_m / 1000:g} km"
        ax.text(x0, y0 + bh * 2.2, "0", ha="left", va="bottom", fontsize=8,
                zorder=6, bbox={"facecolor": "white", "alpha": 0.85, "pad": 1})
        ax.text(x0 + bar, y0 + bh * 2.2, label, ha="right", va="bottom",
                fontsize=8, zorder=6,
                bbox={"facecolor": "white", "alpha": 0.85, "pad": 1})

    # ---- true-north arrow --------------------------------------------------
    base_x = maxx - 0.08 * width
    base_y = maxy - 0.25 * height
    to_wgs84 = crs.get_geod()
    to_geo = Transformer.from_crs(raster_crs, "EPSG:4326", always_xy=True)
    from_geo = Transformer.from_crs("EPSG:4326", raster_crs, always_xy=True)
    lon, lat = to_geo.transform(base_x, base_y)
    arrow_length_m = max(
        1.0, height * (metres_per_unit or 111_000.0) * 0.08)
    north_lon, north_lat, _ = to_wgs84.fwd(lon, lat, 0, arrow_length_m)
    north_x, north_y = from_geo.transform(north_lon, north_lat)
    ax.annotate(
        "N", xy=(north_x, north_y), xytext=(base_x, base_y),
        ha="center", va="center", fontsize=10, fontweight="bold",
        arrowprops=dict(arrowstyle="-|>", color="black", lw=1.6),
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8,
              "pad": 1.5},
        zorder=6,
    )

    # ---- legend ------------------------------------------------------------
    if colorbar_obj is not None and not isinstance(colorbar_obj, tuple):
        lo, hi = colorbar_obj.get_clim()
        if st.get("stretch") == "log" and lo > 0 and hi > lo:
            ticks = np.geomspace(lo, hi, 5)
        else:
            ticks = np.linspace(lo, hi, 5)
        legend_ax.set_axis_on()
        cb = fig.colorbar(colorbar_obj, cax=legend_ax, ticks=ticks)
        if layer_key == "aspect":
            cb.set_label("Aspect (degrees clockwise from north)", fontsize=9)
            cb.set_ticks([0, 45, 90, 135, 180, 225, 270, 315, 360])
            cb.set_ticklabels(["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"])
        elif layer_key == "slope":
            ticks = MaxNLocator(nbins=5, steps=[1, 2, 5, 10]).tick_values(
                max(0, float(colorbar_obj.get_clim()[0])),
                float(colorbar_obj.get_clim()[1]),
            )
            ticks = ticks[(ticks >= 0) & (ticks <= colorbar_obj.get_clim()[1])]
            cb.set_ticks(ticks)
            cb.set_ticklabels([f"{tick:g}°" for tick in ticks])
            cb.set_label("Slope (degrees)", fontsize=9)
        elif layer_key == "flow_accumulation":
            cb.set_label("Flow accumulation (cells)", fontsize=9)
            area_per_cell = metadata.get("cell_area_ha")
            cb.set_ticklabels([
                f"{tick:,.0f} cells" +
                (f"\n{tick * area_per_cell:,.2g} ha" if area_per_cell else "")
                for tick in ticks
            ])
        else:
            units = st.get("units")
            label = st.get("label", layer_key)
            cb.set_label(f"{label} ({units})" if units else label, fontsize=9)
            cb.set_ticklabels([_format_number(float(tick)) for tick in ticks])
        cb.ax.tick_params(labelsize=8, length=3, width=0.8)
        cb.outline.set_linewidth(0.7)
    elif isinstance(colorbar_obj, tuple):
        classes = colorbar_obj[1]
        handles = [mpatches.Patch(facecolor=c["color"], edgecolor="0.3",
                                  label=c["label"]) for c in classes]
        if layer_key != "flow_direction":
            handles.extend(shown_handles)
        handles.append(Line2D([], [], color="#e53e3e", linestyle="--",
                              label="AOI boundary"))
        legend_ax.legend(
            handles=handles, loc="upper left", title=st.get("label", "Legend"),
            fontsize=8, title_fontsize=9, framealpha=0.96, facecolor="white",
            edgecolor="0.55", ncol=1, borderpad=0.7, labelspacing=0.4,
            handlelength=1.2,
        )
    elif shown_labels:
        shown_handles.append(Line2D([], [], color="#e53e3e",
                                    linestyle="--", label="AOI boundary"))
        legend_ax.legend(
            handles=shown_handles, loc="upper left",
            title=st.get("label", "Legend"), fontsize=8,
            title_fontsize=9, framealpha=0.96, facecolor="white",
            edgecolor="0.55", ncol=1, borderpad=0.7, labelspacing=0.4,
            columnspacing=1.0, handlelength=1.5,
        )
    else:
        legend_ax.text(0.02, 0.98, "Legend\nNot applicable",
                       transform=legend_ax.transAxes, va="top", fontsize=9)
    if not isinstance(colorbar_obj, tuple) and not shown_labels:
        fig.legend(
            handles=[Line2D([], [], color="#e53e3e", linestyle="--",
                            label="AOI boundary")],
            loc="lower right", bbox_to_anchor=(0.96, 0.18),
            frameon=False, fontsize=8,
        )

    # ---- info panel --------------------------------------------------------
    src = metadata.get("source", "unknown")
    res = metadata.get("resolution", "-")
    units = st.get("units") or metadata.get("units", "-")
    method = metadata.get("algorithm") or metadata.get("processing_method") or \
        "Not available"
    proc = metadata.get("processing", [])
    proc_txt = " → ".join(proc) if isinstance(proc, list) else str(proc)
    date_txt = metadata.get("date") or metadata.get("created_at") or \
        datetime.now(timezone.utc).strftime("%Y-%m-%d")
    aoi_name = metadata.get("aoi_name") or "Not available"
    aoi_area = metadata.get("aoi_area_km2")
    details = metadata.get("raster_details", {})
    original_dem = metadata.get("original_dem")
    processing_steps = metadata.get("processing") or []
    processing_text = (
        " ".join(processing_steps) if isinstance(processing_steps, list)
        else str(processing_steps)
    ).lower()
    original_dem_size = (
        f"{original_dem['width']} × {original_dem['height']}"
        if original_dem else "Not available"
    )
    original_dem_extent = (
        ", ".join(f"{value:,.3f}" for value in original_dem["bounds"])
        if original_dem else "Not available"
    )
    original_dem_resolution = (
        " × ".join(_format_number(value)
                   for value in original_dem["resolution"])
        if original_dem else "Not available"
    )
    src = metadata.get("source") or "Not available"
    res = metadata.get("resolution") or details.get("resolution") or \
        "Not available"
    units = units or "Not available"
    left = [
        f"Source: {src}",
        f"Resolution: {res}    Units: {units}",
        f"Dimensions: {details.get('width', 'Not available')} × "
        f"{details.get('height', 'Not available')}",
        f"CRS: {raster_crs}    Datum: {metadata.get('crs_datum', 'Not available')}",
        f"Analysis CRS: {metadata.get('analysis_crs') or 'Not available'}",
        f"Vertical datum: {metadata.get('vertical_datum') or 'Not available'}",
        f"Original DEM: {original_dem.get('source_file', 'Not available') if original_dem else 'Not available'}",
        f"Original DEM size/resolution: {original_dem_size}; {original_dem_resolution}",
        f"Original DEM extent/CRS: {original_dem_extent}; "
        f"{original_dem.get('crs', 'Not available') if original_dem else 'Not available'}",
    ]
    right = [
        f"Method: {method}",
        f"Processing chain: {proc_txt or method}",
        f"Parameters: {json.dumps(metadata.get('params') or {}, sort_keys=True) or 'Not available'}",
        f"AOI: {aoi_name}" + (f" ({aoi_area:,.1f} km²)" if aoi_area else ""),
        f"Processing date: {date_txt}",
        "Administrative location: Not available",
        "Administrative boundary source: Not available",
    ]
    if layer_key == "flow_direction":
        right.append(
            "D8 encoding: " + str(
                metadata.get("flow_direction_encoding") or "Not available")
        )
        if metadata.get("flow_direction_arrows"):
            right.append("Flow-direction arrows: D8 subsampled every 3 cells")
        verification = metadata.get("flow_direction_verification") or {}
        right.append(
            "D8 numeric verification: " +
            ("PASS" if verification.get("passed") else "NOT VERIFIED")
        )
    if layer_key == "flow_accumulation":
        threshold = metadata.get("stream_threshold_cells")
        right.append(
            "Stream threshold: " +
            (f"{threshold:,} cells" if threshold else "Not available")
        )
    if layer_key == "watershed":
        pour_point = metadata.get("pour_point") or {}
        right.append(
            "Pour point: " +
            (", ".join(f"{v:.6f}" for v in
                       pour_point.get("coordinates_lonlat", []))
             if pour_point.get("coordinates_lonlat") else "Not available")
        )
        right.append(
            "Pour point method: " +
            str(pour_point.get("method") or "Not available")
        )
    panel.text(
        0.0, 0.98, "\n".join(textwrap.fill(line, width=64) for line in left),
        transform=panel.transAxes, fontsize=6.1, va="top", ha="left",
        linespacing=1.05,
    )
    panel.text(
        0.51, 0.98, "\n".join(textwrap.fill(line, width=64) for line in right),
        transform=panel.transAxes, fontsize=6.1, va="top", ha="left",
        linespacing=1.05,
    )
    footer = (
        f"{GENERATOR_LINE} | {MAP_BRANDING['url']} | "
        f"{st.get('label', layer_key)}"
    )
    if disclaimer:
        footer += f" | Disclaimer: {disclaimer}"
    panel.text(0.0, 0.01, footer, transform=panel.transAxes, fontsize=6.5,
               va="bottom", ha="left", style="italic", color="0.35")
    panel.add_patch(Rectangle((0, 0), 1, 1, transform=panel.transAxes,
                              fill=False, edgecolor="0.6", linewidth=0.8))

    fig.savefig(out_path, dpi=dpi, format=fmt,
                facecolor="white")
    qa_report = _make_qa_report(
        out_path, layer_key, metadata, geographic, bool(bar > 0),
        bool(colorbar_obj is not None or shown_labels),
    )
    qa_path = Path(out_path).with_suffix(".qa.json")
    qa_path.write_text(json.dumps(qa_report, indent=2), encoding="utf-8")
    plt.close(fig)
    return {"path": str(out_path), "format": fmt, "title": title,
            "qa_report": qa_report, "qa_path": str(qa_path)}


def _make_qa_report(out_path, layer_key, metadata, geographic, scale_bar,
                    has_legend) -> dict:
    """Report export checks and explicitly identify unresolved readiness gaps."""
    details = metadata.get("raster_details", {})
    original_dem = metadata.get("original_dem")
    processing_text = json.dumps(
        metadata.get("processing") or {}, sort_keys=True
    ).lower()
    is_vector = details.get("feature_count") is not None
    has_dimensions = is_vector or (
        details.get("width", 0) > 0 and details.get("height", 0) > 0
    )
    check_list = [
        ("Output file exists", Path(out_path).is_file(),
         "The rendered map file is present."),
        ("Output resolution is 300 DPI", metadata.get("output_dpi") == 300,
         f"Configured output resolution: {metadata.get('output_dpi')} DPI."),
        ("A4 landscape page", metadata.get("output_page") == "A4 landscape",
         f"Page format: {metadata.get('output_page', 'Not available')}."),
        ("Hillshade underlay used where applicable",
         layer_key == "hillshade" or
         bool(metadata.get("hillshade_underlay_used")),
         "Hillshade drawn beneath the analysis layer." if
         metadata.get("hillshade_underlay_used") else
         "No matching hillshade underlay is available."
         if layer_key != "hillshade" else "This map is the hillshade."),
        ("Analysis raster interpolation is nearest",
         metadata.get("raster_interpolation") == "nearest",
         "Analysis cells are not smoothed." if
         metadata.get("raster_interpolation") == "nearest" else
         "Nearest-neighbour rendering is not configured."),
        ("Axis notation is plain and grid follows labelled ticks",
         metadata.get("axis_scientific_notation") is False and
         metadata.get("axis_grid_aligned") is True,
         "Plain numeric axes and tick-aligned grid are configured."),
        ("Central branding is consistent",
         metadata.get("branding") == MAP_BRANDING,
         "GIS Mapper by buck0001 branding is used." if
         metadata.get("branding") == MAP_BRANDING else
         "Central map branding is missing or inconsistent."),
        ("Critical-feature overlap reviewed",
         False,
         "Automated map-feature salience/overlap detection is unavailable; "
         "a cartographic review is required."),
        ("Raster dimensions recorded", has_dimensions,
         (f"Vector features: {details.get('feature_count')}."
          if is_vector else
          f"Dimensions: {details.get('width', 'Not available')} × "
          f"{details.get('height', 'Not available')}.")),
        ("Original DEM extent preserved", bool(
            original_dem and original_dem.get("preserved")),
         "Original source DEM is preserved separately." if original_dem and
         original_dem.get("preserved") else
         "Original DEM source file is not available separately."),
        ("CRS is present", bool(metadata.get("crs_verified")),
         "CRS is read from the displayed data."),
        ("Analysis CRS recorded", bool(metadata.get("analysis_crs")),
         metadata.get("analysis_crs") or "Analysis CRS is not recorded."),
        ("Datum reported or marked unavailable", bool(metadata.get("crs_datum")),
         f"Datum: {metadata.get('crs_datum', 'Not available')}."),
        ("Vertical datum reported or marked unavailable",
         bool(metadata.get("vertical_datum")),
         metadata.get("vertical_datum") or "Not available."),
        ("Processing method recorded", bool(metadata.get("algorithm")),
         metadata.get("algorithm") or "No method supplied in layer metadata."),
        ("Processing chain recorded", bool(metadata.get("processing")),
         "Layer processing metadata is present." if metadata.get("processing")
         else "No complete processing chain supplied in layer metadata."),
        ("Depression breaching recorded for hydrology outputs",
         layer_key not in {
             "flow_direction", "flow_accumulation", "streams_raster",
             "stream_order_raster", "drainage", "stream_order", "watershed",
             "drainage_density", "distance_to_drainage", "twi",
         } or "breach" in processing_text,
         "Breaching step is present in the recorded processing chain."
         if "breach" in processing_text
         else "The hydrology chain does not record depression breaching."),
        ("Obvious hydrological flow-line artifacts inspected",
         False,
         "Automated straight/parallel artifact detection is not available; "
         "inspect the drainage pattern before delivery."),
        ("Units recorded", bool(metadata.get("units")),
         metadata.get("units") or
         "No units supplied in layer metadata."),
        ("Legend is outside map data", has_legend or layer_key == "hillshade",
         "Legend is in the right-hand panel." if has_legend
         else "Hillshade uses illumination metadata rather than a color legend."
         if layer_key == "hillshade" else "This layer has no conventional legend."),
        ("Scale bar supported by projected CRS", scale_bar and not geographic,
         "A scale bar is shown." if scale_bar and not geographic else
         "A projected linear CRS is required to draw a scale bar."),
        ("Scale bar overlay checked for critical-data overlap", False,
         "Automated overlap detection for critical features is not available; "
         "review the scale bar placement."),
        *([
            ("Flow-direction encoding identified",
             bool(metadata.get("flow_direction_encoding")),
             metadata.get("flow_direction_encoding") or "Encoding unavailable."),
            ("Flow directions numerically verified",
             bool((metadata.get("flow_direction_verification") or {}).get("passed")),
             json.dumps(metadata.get("flow_direction_verification") or
                        {"reason": "No numerical verification was recorded."})),
        ] if layer_key == "flow_direction" else []),
        *([
            ("D8 arrow sampling stride recorded",
             metadata.get("flow_arrow_stride_cells") == 3,
             "Arrows use every 3rd raster cell." if
             metadata.get("flow_arrow_stride_cells") == 3 else
             "Arrow sampling stride is unavailable."),
        ] if metadata.get("flow_direction_arrows") else []),
        *([
            ("Stream threshold recorded",
             metadata.get("stream_threshold_cells") is not None,
             (f"{metadata['stream_threshold_cells']:,} cells"
              if metadata.get("stream_threshold_cells") is not None
              else "No stream threshold is available in project metadata.")),
        ] if layer_key in {
            "flow_accumulation", "streams_raster", "stream_order_raster",
            "drainage", "stream_order", "watershed", "drainage_density",
            "distance_to_drainage",
        } else []),
        *([
            ("Watershed pour point and method recorded",
             bool(metadata.get("pour_point")),
             json.dumps(metadata.get("pour_point") or
                        {"reason": "Pour point information is unavailable."})),
        ] if layer_key == "watershed" else []),
        ("Administrative location verified", False,
         "No spatially verified administrative-boundary dataset is available."),
        ("Locator inset verified", False,
         "No verified administrative boundaries are available for a locator."),
    ]
    checks = [
        {"check": label, "status": "PASS" if ok else "FAIL", "detail": detail}
        for label, ok, detail in check_list
    ]
    actions = {
        "Original DEM extent preserved": (
            "No", "Restore or upload the original DEM source file."
        ),
        "Hillshade underlay used where applicable": (
            "No", "Provide a co-registered hillshade layer or review why one is unavailable."
        ),
        "Analysis CRS recorded": (
            "No", "Record the project analysis CRS in export metadata."
        ),
        "Critical-feature overlap reviewed": (
            "No", "Review the map layout for overlap with critical features."
        ),
        "Obvious hydrological flow-line artifacts inspected": (
            "No", "Inspect the drainage pattern for straight or parallel artifacts."
        ),
        "Scale bar overlay checked for critical-data overlap": (
            "No", "Review and move the scale bar if it obscures a critical feature."
        ),
        "Administrative location verified": (
            "No", "Install or provide a verified administrative-boundary dataset."
        ),
        "Locator inset verified": (
            "No", "Provide verified administrative boundaries and generate a locator inset."
        ),
        "Flow directions numerically verified": (
            "No", "Inspect D8 encoding and the breached DEM before delivery."
        ),
        "Flow-direction encoding identified": (
            "No", "Set and verify the D8 encoding used by the processing engine."
        ),
        "Stream threshold recorded": (
            "No", "Re-run stream extraction with an explicitly selected threshold."
        ),
        "Watershed pour point and method recorded": (
            "No", "Record the actual pour point coordinates and delineation method."
        ),
    }
    failed = [
        {
            **check,
            "automatically_correctable": actions.get(
                check["check"], ("No", "Review and correct the stated metadata.")
            )[0] == "Yes",
            "required_action": actions.get(
                check["check"],
                ("No", "Review and correct the stated metadata."),
            )[1],
        }
        for check in checks if check["status"] == "FAIL"
    ]
    return {
        "status": "NOT CLIENT-READY" if failed else "PASS",
        "map": Path(out_path).name,
        "layer_key": layer_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "failed_checks": failed,
        "required_action": "Resolve failed checks before client delivery."
        if failed else None,
    }
