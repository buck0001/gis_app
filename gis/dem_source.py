"""DEM upload system (DEM UPLOAD SYSTEM spec sections 2-8).

Two DEM acquisition modes: UPLOAD (user's DEM is the primary source) and
ACQUIRE (external provider download). This module implements upload-side
inspection, validation rules, AOI/DEM intersection checking and creation of
analysis copies.

Hard rules enforced here:
  * The user's original file is NEVER modified.
  * A missing CRS is never guessed -> processing STOPs until the user
    assigns one explicitly.
  * Suspicious elevation values are reported, never auto-"fixed".
  * Actual pixel size is always read from the raster, never from the
    filename or the user's description.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

MAX_READ_PIXELS = 40_000_000   # decimate statistics reads above this


def _fmt_res(x: float, y: float) -> str:
    def f(v):
        if abs(v - round(v)) < 1e-6:
            return str(int(round(v)))
        return f"{v:.4g}"
    return f"{f(x)} x {f(y)}"


def inspect_dem(path: str, assign_crs: str | None = None) -> dict:
    """Inspect an uploaded DEM and apply the validation rules.

    Returns {ok_to_proceed, report, issues}.
    """
    import rasterio

    p = Path(path)
    issues: list[dict] = []
    if not p.exists() or p.stat().st_size == 0:
        return {
            "ok_to_proceed": False,
            "report": {"name": p.name, "size_bytes": 0},
            "issues": [{"rule": "file", "severity": "error",
                        "message": "File is missing or empty.",
                        "options": ["upload_another"]}],
        }

    with rasterio.open(str(p)) as src:
        crs = src.crs
        assigned = None
        if assign_crs and crs is None:
            from pyproj import CRS as PCRS
            try:
                crs = PCRS.from_user_input(assign_crs)   # validated, not guessed
                assigned = assign_crs
            except Exception as exc:
                return {
                    "ok_to_proceed": False,
                    "report": {"name": p.name},
                    "issues": [{"rule": "crs_assign", "severity": "error",
                                "message": f"'{assign_crs}' is not a valid CRS: {exc}",
                                "options": ["assign_crs", "cancel"]}],
                }
        res_x, res_y = abs(src.transform.a), abs(src.transform.e)
        bounds = list(src.bounds)
        nodata = src.nodata
        dtype = src.dtypes[0]
        tags = src.tags()
        band1 = src.read(1, masked=True)
        width, height, bands = src.width, src.height, src.count
        compress = str(src.profile.get("compress") or "none")
        geographic = bool(crs.is_geographic) if crs else None
        valid_count = int(band1.count())

        if valid_count > MAX_READ_PIXELS:
            band1 = src.read(1, out_shape=(max(1, height // 4),
                                           max(1, width // 4)), masked=True)
        data = np.ma.compressed(band1).astype("float64")
        data = data[np.isfinite(data)]

    vmin = float(data.min()) if data.size else None
    vmax = float(data.max()) if data.size else None
    vmean = float(data.mean()) if data.size else None
    vstd = float(data.std()) if data.size else None

    # --- validation rules (spec section 3) ----------------------------------
    if crs is None:
        issues.append({
            "rule": "crs_missing", "severity": "error",
            "message": ("This DEM does not contain coordinate reference system "
                        "information. Please assign a CRS before continuing."),
            "options": ["assign_crs", "upload_another", "cancel"],
        })
    elif assigned:
        issues.append({
            "rule": "crs_assigned", "severity": "info",
            "message": (f"CRS assigned by user: {assigned}. The original file "
                        "had none; the assignment applies to analysis copies "
                        "only."),
        })

    if nodata is None:
        issues.append({
            "rule": "nodata_undefined", "severity": "info",
            "message": ("No nodata value declared in the file; valid-pixel "
                        "coverage will be used as-is."),
        })
    elif valid_count:
        nodata_frac = 1.0 - (valid_count / float(width * height))
        if nodata_frac > 0.001:
            issues.append({
                "rule": "nodata_detected", "severity": "warning",
                "message": (f"Nodata areas detected ({nodata_frac:.1%} of the "
                            f"raster, value {nodata})."),
                "nodata_fraction": round(nodata_frac, 4),
                "options": ["continue", "fill_nodata", "cancel"],
            })

    if data.size == 0:
        issues.append({
            "rule": "empty_raster", "severity": "error",
            "message": "The DEM contains no valid elevation values.",
            "options": ["upload_another", "cancel"],
        })
    else:
        if vmin < -500 or vmax > 9000 or vmin == vmax:
            issues.append({
                "rule": "suspicious_range", "severity": "warning",
                "message": ("The DEM contains unusual elevation values "
                            f"(min {vmin:.1f}, max {vmax:.1f}). "
                            "Please verify the source before processing."),
                "options": ["continue", "upload_another", "cancel"],
            })

    if geographic:
        issues.append({
            "rule": "geographic_crs", "severity": "info",
            "message": ("The uploaded DEM is in geographic coordinates. A "
                        "projected copy will be created for distance, area and "
                        "terrain processing."),
        })

    ok_to_proceed = not any(i["severity"] == "error" for i in issues)
    report = {
        "name": p.name,
        "size_bytes": p.stat().st_size,
        "width": width, "height": height, "bands": bands,
        "crs": (str(crs) if crs else None),
        "crs_assigned_by_user": assigned,
        "geographic": geographic,
        "resolution_x": res_x, "resolution_y": res_y,
        "resolution_text": _fmt_res(res_x, res_y),
        "resolution_units": ("degrees" if geographic else "metres (CRS units)"),
        "bounds": [round(v, 6) for v in bounds],
        "nodata": nodata,
        "dtype": str(dtype),
        "compression": compress,
        "min": round(vmin, 3) if vmin is not None else None,
        "max": round(vmax, 3) if vmax is not None else None,
        "mean": round(vmean, 3) if vmean is not None else None,
        "std": round(vstd, 3) if vstd is not None else None,
        "elevation_range_text": (f"{vmin:,.0f} - {vmax:,.0f}"
                                 if vmin is not None else "n/a"),
        "vertical_units": (tags.get("VERTICAL_UNITS") or tags.get("units")
                           or tags.get("ZUNITS") or
                           "unknown (not stated in file; not assumed)"),
        "tags": {k: v for k, v in list(tags.items())[:20]},
    }
    return {"ok_to_proceed": ok_to_proceed, "report": report, "issues": issues}


# ---------------------------------------------------------------------------
# footprint & AOI intersection (spec section 5)
# ---------------------------------------------------------------------------
def dem_footprint_geojson(path: str, assign_crs: str | None = None) -> dict:
    """DEM extent as a GeoJSON polygon in EPSG:4326 (for map display)."""
    import rasterio
    from rasterio.warp import transform_bounds
    from shapely.geometry import box, mapping
    from pyproj import CRS as PCRS

    with rasterio.open(path) as src:
        crs = src.crs or (PCRS.from_user_input(assign_crs) if assign_crs else None)
        if crs is None:
            raise ValueError("DEM has no CRS; cannot compute footprint.")
        b = transform_bounds(crs, "EPSG:4326", *src.bounds, densify_pts=21)
    return {"type": "Feature", "properties": {"source": "dem_extent"},
            "geometry": mapping(box(*b))}


def check_aoi_coverage(path: str, aoi_geometry_geojson: dict,
                       assign_crs: str | None = None) -> dict:
    """How much of the AOI is covered by the DEM (spec section 5).

    Returns coverage percentages + the standard warning message when the AOI
    extends beyond the DEM. Never modifies anything.
    """
    import rasterio
    from rasterio.warp import transform_bounds
    from shapely.geometry import box, mapping, shape
    from shapely.ops import transform as shp_transform
    from pyproj import CRS as PCRS, Transformer

    with rasterio.open(path) as src:
        crs = src.crs or (PCRS.from_user_input(assign_crs) if assign_crs else None)
        if crs is None:
            raise ValueError("DEM has no CRS; cannot compute coverage.")
        b = transform_bounds(crs, "EPSG:4326", *src.bounds, densify_pts=21)
    footprint = box(*b)
    aoi = shape(aoi_geometry_geojson)
    if not aoi.is_valid:
        from shapely.validation import make_valid
        aoi = make_valid(aoi)

    # equal-area projection centred on the AOI for honest area ratios
    c = aoi.centroid
    aeqd = (f"+proj=aea +lat_1={c.y - 1} +lat_2={c.y + 1} +lat_0={c.y} "
            f"+lon_0={c.x} +datum=WGS84 +units=m +no_defs")
    tr = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
    aoi_m = shp_transform(tr.transform, aoi)
    fp_m = shp_transform(tr.transform, footprint)
    aoi_area = max(aoi_m.area, 1e-9)
    inter_area = aoi_m.intersection(fp_m).area if aoi_m.intersects(fp_m) else 0.0
    coverage = 100.0 * inter_area / aoi_area
    outside = 100.0 - coverage
    result = {
        "coverage_percent": round(coverage, 1),
        "outside_percent": round(outside, 1),
        "dem_footprint": {"type": "Feature", "properties": {"source": "dem_extent"},
                          "geometry": mapping(footprint)},
        "fully_covered": outside < 0.5,
    }
    if outside >= 0.5:
        result["warning"] = (
            f"Your AOI extends beyond the uploaded DEM by approximately "
            f"{outside:.0f}%.")
        result["options"] = ["continue_with_available_coverage",
                             "upload_another_dem",
                             "acquire_missing_coverage"]
    return result


# ---------------------------------------------------------------------------
# preprocessing (spec sections 4, 6, 7, 8)
# ---------------------------------------------------------------------------
def fill_nodata(processed_path: str) -> dict:
    """Interpolate nodata gaps on a PROCESSED copy only (explicit opt-in)."""
    import rasterio
    from rasterio.fill import fillnodata

    with rasterio.open(processed_path, "r+") as src:
        band = src.read(1)
        mask = np.ones(band.shape, dtype="uint8")
        if src.nodata is not None:
            mask[band == src.nodata] = 0
        filled_px = int((mask == 0).sum())
        if filled_px and filled_px < 0.9 * band.size:
            data, mask_out = fillnodata(band, mask=mask,
                                        max_search_distance=200)
            src.write(data, 1)
            return {"filled_pixels": filled_px, "method": "GDAL nodata "
                    "interpolation (max_search_distance=200 px)"}
        return {"filled_pixels": 0, "method": "skipped",
                "reason": "too many nodata pixels to interpolate safely"
                if filled_px else "no nodata pixels"}


def preprocess_uploaded_dem(source_path: str, processed_dir: str, aoi: dict,
                            analysis_crs: str, resolution_m: Optional[float] = None,
                            nodata_action: str = "continue",
                            assign_crs: Optional[str] = None) -> dict:
    """Build the analysis copy of an uploaded DEM (spec sections 4, 6-8).

    source_path (in source/) is opened read-only; every write goes to
    processed_dir. Reprojection happens only when required; the original
    resolution is preserved unless resolution_m is explicitly provided.
    """
    import rasterio
    from rasterio.warp import (calculate_default_transform, reproject,
                               Resampling)
    from rasterio.mask import mask as rio_mask
    from shapely.geometry import shape, mapping
    from shapely.ops import transform as shp_transform
    from pyproj import CRS as PCRS, Transformer

    out_dir = Path(processed_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(source_path) as src:
        src_crs = src.crs or (PCRS.from_user_input(assign_crs)
                              if assign_crs else None)
        if src_crs is None:
            raise ValueError(
                "DEM has no CRS and none was assigned. "
                "Recommended action: assign a CRS before processing.")
        orig_res_x, orig_res_y = abs(src.transform.a), abs(src.transform.e)
        units = "degrees" if src_crs.is_geographic else "CRS units"
        original_resolution = f"{orig_res_x:.6g} x {orig_res_y:.6g} {units}"
        src_nodata = src.nodata
        profile = src.profile.copy()

    tr_geom = Transformer.from_crs("EPSG:4326", analysis_crs, always_xy=True)
    aoi_proj = shp_transform(tr_geom.transform, shape(aoi["geometry"]))
    target = PCRS.from_user_input(analysis_crs)
    same_crs = (not src_crs.is_geographic) and (src_crs == target)

    processing: list[str] = []
    dst_path = out_dir / "dem_analysis.tif"
    tmp = out_dir / "dem_analysis.tmp.tif"
    fill_info = None
    resampling = ""
    new_resolution = ""

    if same_crs:
        # already in the analysis CRS -> clip only, NO resampling (spec 8)
        with rasterio.open(source_path) as src:
            try:
                img, tr = rio_mask(src, [mapping(aoi_proj)], crop=True,
                                   filled=True,
                                   nodata=src_nodata if src_nodata is not None
                                   else -9999)
            except ValueError as exc:
                raise ValueError(
                    "AOI does not overlap the uploaded DEM. "
                    "Recommended action: check coverage / draw AOI inside DEM. "
                    f"({exc})") from exc
            prof = src.profile.copy()
            prof.update(height=img.shape[1], width=img.shape[2], transform=tr)
        if src_nodata is None:
            prof.update(nodata=-9999)
        with rasterio.open(tmp, "w", **prof) as dst:
            dst.write(img)
        processing.append("clipped_to_aoi")
        resampling = "none (clip only - native grid preserved)"
        new_resolution = f"{orig_res_x:.6g} x {orig_res_y:.6g} {units}"
    else:
        with rasterio.open(source_path) as src:
            transform, w, h = calculate_default_transform(
                src_crs, analysis_crs, src.width, src.height, *src.bounds,
                resolution=resolution_m)   # None -> approx native cell size
            prof = profile.copy()
            prof.update(crs=analysis_crs, transform=transform, width=w, height=h,
                        nodata=(src_nodata if src_nodata is not None else -9999),
                        dtype="float32")
            work = out_dir / "dem_reprojected.tmp.tif"
            with rasterio.open(work, "w", **prof) as dst:
                reproject(source=rasterio.band(src, 1),
                          destination=rasterio.band(dst, 1),
                          src_nodata=src_nodata, dst_nodata=prof["nodata"],
                          resampling=Resampling.bilinear)
            processing.append("reprojected_to_analysis_crs")
        with rasterio.open(work) as src:
            try:
                img, tr = rio_mask(src, [mapping(aoi_proj)], crop=True,
                                   filled=True, nodata=src.nodata)
            except ValueError as exc:
                work.unlink(missing_ok=True)
                raise ValueError(
                    "AOI does not overlap the uploaded DEM. "
                    f"Recommended action: check coverage. ({exc})") from exc
            prof = src.profile.copy()
            prof.update(height=img.shape[1], width=img.shape[2], transform=tr)
        with rasterio.open(tmp, "w", **prof) as dst:
            dst.write(img)
        work.unlink(missing_ok=True)
        processing.append("clipped_to_aoi")
        resampling = "bilinear"
        new_resolution = (f"{abs(transform.a):.6g} x {abs(transform.e):.6g} "
                          "metres (analysis CRS)")

    tmp.replace(dst_path)

    if nodata_action == "fill":
        fill_info = fill_nodata(str(dst_path))
        processing.append("nodata_filled")

    with rasterio.open(dst_path, "r+") as ds:
        ds.update_tags(
            SOURCE=f"User-uploaded DEM ({Path(source_path).name})",
            ORIGINAL_CRS=str(src_crs),
            ANALYSIS_CRS=str(analysis_crs),
            ORIGINAL_RESOLUTION=original_resolution,
            NEW_RESOLUTION=new_resolution,
            RESAMPLING=resampling,
            ORIGINAL_FILE_PRESERVED="yes (read-only source)",
        )

    return {
        "path": str(dst_path),
        "original_crs": str(src_crs),
        "analysis_crs": str(analysis_crs),
        "original_resolution": original_resolution,
        "new_resolution": new_resolution,
        "resampling": resampling,
        "processing": processing,
        "filled": fill_info,
        "source_file": str(source_path),
        "source_file_modified": False,      # guaranteed by construction
    }
