"""Terrain analysis from a DEM (spec section 8).

Basic:  elevation (reprojected DEM), slope, aspect, hillshade
Advanced: curvature, roughness, TRI, TPI (only run when explicitly selected)

All computations run in the PROJECTED analysis CRS (metres), never in
EPSG:4326. Algorithm: Horn (1981) 3x3 finite differences for slope/aspect,
standard ESRI-compatible hillshade. Nodata cells propagate to outputs.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

NODATA = -9999.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _profile_with(path: str, arr: np.ndarray, dtype="float32", nodata=NODATA) -> dict:
    import rasterio
    with rasterio.open(path) as src:
        profile = src.profile.copy()
    profile.update(dtype=dtype, nodata=nodata, compress="deflate", tiled=True, count=1)
    return profile


def _write_raster(path: str, arr: np.ndarray, profile: dict, **tags) -> str:
    import rasterio
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    profile = profile.copy()
    if profile.get("tiled"):
        # NOTE: inherited blockxsize may be a *stripe* size (e.g. 60) - always
        # reset to a valid tile size for tiled output.
        profile["blockxsize"] = 256
        profile["blockysize"] = 256
    else:
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
    tmp = str(path) + ".tmp"
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(arr.astype(profile.get("dtype", "float32")), 1)
        if tags:
            dst.update_tags(**{k: str(v) for k, v in tags.items()})
    Path(tmp).replace(Path(path))
    return str(path)


def _read_dem(path: str) -> tuple[np.ndarray, float, dict, dict]:
    """Read DEM -> (array with NaN for nodata, cellsize, profile, tags)."""
    import rasterio
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
        tags = src.tags()
        cs_x = abs(src.transform.a)
        cs_y = abs(src.transform.e)
    if nodata is not None:
        arr[arr == nodata] = np.nan
    arr[~np.isfinite(arr)] = np.nan
    return arr, (cs_x + cs_y) / 2.0, profile, tags


def _pad_edges(a: np.ndarray) -> np.ndarray:
    """3x3 neighbourhood padding by edge replication (NaN edges stay NaN)."""
    return np.pad(a, 1, mode="edge")


def _output_array(nan_arr: np.ndarray) -> np.ndarray:
    out = np.full(nan_arr.shape, NODATA, dtype="float64")
    valid = np.isfinite(nan_arr)
    out[valid] = nan_arr[valid]
    return out


# ---------------------------------------------------------------------------
# reprojection (produces the ELEVATION layer)
# ---------------------------------------------------------------------------
def reproject_dem(src_path: str, dst_path: str, dst_crs: str,
                  resolution: float = 30.0, aoi_geom_proj=None) -> dict:
    """Reproject the DEM clip to the analysis CRS at a fixed resolution,
    optionally masked to the AOI polygon (already in dst_crs)."""
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds,
            resolution=resolution,
        )
        profile = src.profile.copy()
        profile.update(crs=dst_crs, transform=transform, width=width, height=height,
                       nodata=NODATA, dtype="float32", compress="deflate",
                       tiled=True, blockxsize=256, blockysize=256)
        dst = rasterio.open(str(dst_path) + ".tmp", "w", **profile)
        try:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_nodata=src.nodata,
                dst_nodata=NODATA,
                resampling=Resampling.bilinear,
            )
        finally:
            dst.close()
        Path(str(dst_path) + ".tmp").replace(Path(dst_path))

    if aoi_geom_proj is not None:
        from rasterio.mask import mask as rio_mask
        from shapely.geometry import mapping
        masked_img = None
        with rasterio.open(dst_path) as src:
            try:
                img, tr = rio_mask(src, [mapping(aoi_geom_proj)], crop=True,
                                   filled=True, nodata=NODATA)
                prof = src.profile.copy()
            except ValueError:
                pass  # AOI does not intersect -> keep unmasked
            else:
                prof.update(height=img.shape[1], width=img.shape[2], transform=tr)
                masked_img = img[0]
        if masked_img is not None:
            _write_raster(dst_path, masked_img, prof,
                          SOURCE="Copernicus DEM GLO-30 (ESA)",
                          ALGORITHM=f"reprojected to {dst_crs} @ {resolution} m + clipped")

    with rasterio.open(dst_path) as src:
        return {
            "path": dst_path, "crs": str(src.crs), "resolution": resolution,
            "width": src.width, "height": src.height, "resampling": "bilinear",
            "nodata": src.nodata,
        }


def generate_contours(dem_path: str, out_path: str,
                      interval_m: float = 10.0) -> dict:
    """Create contour polylines from a DEM and save them as a GeoPackage."""
    import geopandas as gpd
    import rasterio
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from shapely.geometry import LineString

    if not np.isfinite(interval_m) or interval_m <= 0:
        raise ValueError("Contour interval must be a positive finite number.")

    with rasterio.open(dem_path) as src:
        data = src.read(1, masked=True).astype("float64")
        transform = src.transform
        crs = src.crs

    values = data.compressed()
    if values.size == 0:
        raise ValueError("The elevation raster contains no valid cells.")
    if data.ndim != 2 or min(data.shape) < 2:
        raise ValueError("The elevation raster must be at least 2 by 2 cells.")

    first_level = np.ceil(float(values.min()) / interval_m) * interval_m
    last_level = np.floor(float(values.max()) / interval_m) * interval_m
    if first_level > last_level:
        raise ValueError(
            "No contour elevations fall within the DEM range for this interval."
        )
    levels = np.arange(
        first_level, last_level + interval_m * 0.5, interval_m,
        dtype="float64",
    )

    figure = Figure()
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    contour_set = axis.contour(data, levels=levels)

    geometries = []
    elevations = []
    for level, segments in zip(contour_set.levels, contour_set.allsegs):
        for segment in segments:
            if len(segment) < 2:
                continue
            coordinates = []
            for column, row in segment:
                point = transform @ (
                    float(column) + 0.5, float(row) + 0.5,
                )
                if not coordinates or point != coordinates[-1]:
                    coordinates.append(point)
            if len(coordinates) < 2:
                continue
            line = LineString(coordinates)
            if not line.is_valid or line.length == 0:
                continue
            geometries.append(line)
            elevations.append(float(level))
    figure.clear()

    if not geometries:
        raise ValueError(
            "No contour lines could be generated from the elevation raster."
        )

    contours = gpd.GeoDataFrame(
        {"elevation_m": elevations, "geometry": geometries},
        crs=crs,
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if Path(out_path).exists():
        Path(out_path).unlink()
    contours.to_file(out_path, layer="contours", driver="GPKG")
    return {
        "path": out_path,
        "contour_count": len(contours),
        "interval_m": float(interval_m),
        "min_elevation_m": float(values.min()),
        "max_elevation_m": float(values.max()),
        "crs": str(crs),
    }


def _horn_gradients(arr: np.ndarray, cs: float):
    """Return (dz/dx, dz/dy_north) using Horn's 3x3 kernel."""
    p = _pad_edges(arr)
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, _, f = p[1:-1, :-2], p[1:-1, 1:-1], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((c + 2.0 * f + i) - (a + 2.0 * d + g)) / (8.0 * cs)
    dzdy_n = ((a + 2.0 * b + c) - (g + 2.0 * h + i)) / (8.0 * cs)
    return dzdx, dzdy_n


def calculate_slope(dem_path: str, out_path: str) -> dict:
    """Slope in degrees, Horn 3x3. Valid range: 0..90."""
    arr, cs, _, _ = _read_dem(dem_path)
    dzdx, dzdy = _horn_gradients(arr, cs)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    slope[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(slope), _profile_with(dem_path, slope),
                  ALGORITHM="Horn 3x3 finite differences", UNITS="degrees")
    return {"path": out_path, "units": "degrees", "algorithm": "Horn (1981) 3x3",
            "min_expected": 0.0, "max_expected": 90.0}


def calculate_aspect(dem_path: str, out_path: str) -> dict:
    """Aspect = downslope direction, degrees clockwise from north.
    Flat cells (zero gradient) are stored as -1 (documented in metadata)."""
    arr, cs, _, _ = _read_dem(dem_path)
    dzdx, dzdy_n = _horn_gradients(arr, cs)
    flat = (dzdx == 0) & (dzdy_n == 0)
    aspect = np.degrees(np.arctan2(-dzdx, -dzdy_n)) % 360.0
    aspect[flat] = -1.0
    aspect[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(aspect), _profile_with(dem_path, aspect),
                  ALGORITHM="Horn 3x3 finite differences",
                  UNITS="degrees clockwise from north; -1 = flat")
    return {"path": out_path, "units": "degrees (cw from north; -1 = flat)",
            "algorithm": "Horn (1981) 3x3", "min_expected": -1.0, "max_expected": 360.0}


def calculate_hillshade(dem_path: str, out_path: str,
                        azimuth: float = 315.0, altitude: float = 45.0,
                        vert_exag: float = 3.0) -> dict:
    """Horn hillshade (0-255), with configurable vertical exaggeration."""
    if vert_exag <= 0:
        raise ValueError("Hillshade vertical exaggeration must be positive.")
    arr, cs, _, _ = _read_dem(dem_path)
    dzdx, dzdy_n = _horn_gradients(arr, cs)
    dzdx *= vert_exag
    dzdy_n *= vert_exag
    slope_r = np.arctan(np.hypot(dzdx, dzdy_n))
    aspect = np.degrees(np.arctan2(-dzdx, -dzdy_n)) % 360.0
    zenith = np.radians(90.0 - altitude)
    az_r = np.radians(azimuth)
    asp_r = np.radians(aspect)
    hs = 255.0 * (np.cos(zenith) * np.cos(slope_r) +
                  np.sin(zenith) * np.sin(slope_r) * np.cos(az_r - asp_r))
    hs = np.clip(hs, 0.0, 255.0)
    hs[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(hs), _profile_with(dem_path, hs),
                  ALGORITHM=(f"Horn hillshade, azimuth={azimuth}, "
                             f"altitude={altitude}, vertical exaggeration={vert_exag}"),
                  UNITS="0-255 (grayscale)")
    return {"path": out_path, "units": "0-255",
            "algorithm": "Horn hillshade",
            "params": {"azimuth": azimuth, "altitude": altitude,
                       "vertical_exaggeration": vert_exag,
                       "resolution": cs},
            "min_expected": 0.0, "max_expected": 255.0}


# ---------------------------------------------------------------------------
# advanced terrain derivatives (opt-in only, spec section 8)
# ---------------------------------------------------------------------------
def calculate_curvature(dem_path: str, out_path: str) -> dict:
    """Surface curvature (Laplacian): -(d2z/dx2 + d2z/dy2).

    Units: 1/m x 1000 for readability. Positive = convex, negative = concave."""
    arr, cs, _, _ = _read_dem(dem_path)
    p = _pad_edges(arr)
    d, e, f = p[1:-1, :-2], p[1:-1, 1:-1], p[1:-1, 2:]
    b, _, h = p[:-2, 1:-1], p[1:-1, 1:-1], p[2:, 1:-1]
    lap = (d + f + b + h - 4.0 * e) / (cs * cs)
    curv = -lap * 1000.0
    curv[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(curv), _profile_with(dem_path, curv),
                  ALGORITHM="Laplacian second derivative",
                  UNITS="1/m x1000; + convex, - concave")
    return {"path": out_path, "units": "1/m x1000 (+ convex)",
            "algorithm": "Laplacian second derivative"}


def calculate_roughness(dem_path: str, out_path: str) -> dict:
    """Roughness = max |centre - neighbour| over the 3x3 window (metres)."""
    arr, _, _, _ = _read_dem(dem_path)
    p = _pad_edges(arr)
    e = p[1:-1, 1:-1]
    diffs = [np.abs(p[k:k + arr.shape[0], l:l + arr.shape[1]] - e)
             for k in range(3) for l in range(3)]
    rough = np.nanmax(np.stack(diffs), axis=0)
    rough[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(rough), _profile_with(dem_path, rough),
                  ALGORITHM="3x3 max elevation difference", UNITS="metres")
    return {"path": out_path, "units": "metres", "algorithm": "3x3 max difference"}


def calculate_tri(dem_path: str, out_path: str) -> dict:
    """Terrain Ruggedness Index: mean |centre - neighbour| over 3x3 (metres)."""
    arr, _, _, _ = _read_dem(dem_path)
    p = _pad_edges(arr)
    e = p[1:-1, 1:-1]
    diffs = [np.abs(p[k:k + arr.shape[0], l:l + arr.shape[1]] - e)
             for k in range(3) for l in range(3)]
    stack = np.stack(diffs)
    valid = np.stack([np.isfinite(s) for s in diffs])
    tri = np.where(valid.sum(axis=0) > 0,
                   np.nansum(np.where(valid, stack, 0.0), axis=0) /
                   np.maximum(valid.sum(axis=0), 1), np.nan)
    tri[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(tri), _profile_with(dem_path, tri),
                  ALGORITHM="3x3 mean absolute elevation difference",
                  UNITS="metres")
    return {"path": out_path, "units": "metres", "algorithm": "TRI (3x3 mean abs diff)"}


def calculate_tpi(dem_path: str, out_path: str, radius_m: float = 500.0) -> dict:
    """Topographic Position Index: elevation - mean elevation within radius."""
    from scipy.ndimage import uniform_filter
    arr, cs, _, _ = _read_dem(dem_path)
    radius_cells = max(1, int(round(radius_m / cs)))
    valid = np.isfinite(arr).astype("float64")
    filled = np.where(np.isfinite(arr), arr, 0.0)
    size = 2 * radius_cells + 1
    mean = uniform_filter(filled, size=size, mode="nearest")
    mean_valid = uniform_filter(valid, size=size, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        local_mean = np.where(mean_valid > 0, mean / np.maximum(mean_valid, 1e-9), np.nan)
    tpi = arr - local_mean
    tpi[~np.isfinite(arr)] = np.nan
    _write_raster(out_path, _output_array(tpi), _profile_with(dem_path, tpi),
                  ALGORITHM=f"TPI, radius={radius_m} m", UNITS="metres")
    return {"path": out_path, "units": "metres",
            "algorithm": f"TPI (elevation - local mean, radius {radius_m} m)"}
