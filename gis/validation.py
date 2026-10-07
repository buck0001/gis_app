"""Input/output validation and error diagnosis (spec sections 34 & 35)."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def validate_raster(path: str, expect_crs: str | None = None,
                    min_value: float | None = None,
                    max_value: float | None = None,
                    require_all_valid: bool = False) -> dict:
    """Validate a generated raster. Returns {'ok', 'checks'} and raises nothing."""
    import rasterio
    checks: list[dict] = []
    p = Path(path)
    exists = p.exists() and p.stat().st_size > 0
    checks.append({"check": "file_exists", "ok": exists, "detail": str(p.name)})
    if not exists:
        return {"ok": False, "checks": checks}
    with rasterio.open(path) as src:
        checks.append({"check": "crs_present", "ok": src.crs is not None,
                       "detail": str(src.crs)})
        checks.append({"check": "dimensions_gt_1",
                       "ok": src.width > 1 and src.height > 1,
                       "detail": f"{src.width}x{src.height}"})
        if expect_crs and src.crs:
            checks.append({"check": "crs_matches_expected",
                           "ok": str(src.crs) == str(expect_crs),
                           "detail": f"{src.crs} vs {expect_crs}"})
        arr = src.read(1, masked=True)
        data = np.ma.compressed(arr).astype("float64")
        data = data[np.isfinite(data)]
        checks.append({"check": "contains_valid_pixels", "ok": data.size > 0,
                       "detail": f"{data.size} valid pixels"})
        if data.size:
            checks.append({"check": "value_range",
                           "ok": bool((min_value is None or data.min() >= min_value) and
                                      (max_value is None or data.max() <= max_value)),
                           "detail": f"min={data.min():.3f}, max={data.max():.3f} "
                                     f"(expected {min_value}..{max_value})"})
            if require_all_valid:
                frac = data.size / (src.width * src.height)
                checks.append({"check": "coverage", "ok": frac > 0.5,
                               "detail": f"{frac:.0%} valid"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def validate_vector(path: str, layer: str | None = None,
                    min_features: int = 1) -> dict:
    import geopandas as gpd
    checks: list[dict] = []
    p = Path(path)
    exists = p.exists() and p.stat().st_size > 0
    checks.append({"check": "file_exists", "ok": exists, "detail": str(p.name)})
    if not exists:
        return {"ok": False, "checks": checks}
    try:
        gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    except Exception as exc:
        checks.append({"check": "readable", "ok": False, "detail": str(exc)})
        return {"ok": False, "checks": checks}
    checks.append({"check": "readable", "ok": True})
    checks.append({"check": "feature_count", "ok": len(gdf) >= min_features,
                   "detail": f"{len(gdf)} features (min {min_features})"})
    checks.append({"check": "crs_present", "ok": gdf.crs is not None,
                   "detail": str(gdf.crs)})
    if len(gdf):
        invalid = (~gdf.geometry.is_valid).sum()
        checks.append({"check": "geometries_valid", "ok": int(invalid) == 0,
                       "detail": f"{invalid} invalid geometries"})
        non_empty = (~gdf.geometry.is_empty).all()
        checks.append({"check": "geometries_not_empty", "ok": bool(non_empty)})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------------------
# error diagnosis (spec section 35)
# ---------------------------------------------------------------------------
def diagnose(operation: str, exc: Exception) -> dict:
    """Turn an exception into the structured error display required by spec 35."""
    msg = str(exc)
    reason = msg
    action = "Review the input data and parameters, then retry."
    lowered = msg.lower()
    if "recommended action:" in lowered:
        idx = msg.lower().index("recommended action:")
        reason, action = msg[:idx].strip(" ."), msg[idx:].split(":", 1)[1].strip()
    elif "nodata" in lowered or "no-data" in lowered:
        reason = f"{operation} failed: DEM contains invalid/no-data areas."
        action = ("Inspect NoData gaps and the selected conditioning method. "
                  "Do not silently switch from breaching to filling.")
    elif "threshold" in lowered:
        reason = f"{operation} failed: stream threshold problem. {msg}"
        action = ("Select a stream threshold justified by the study method "
                  "and record it in the processing parameters.")
    elif isinstance(exc, MemoryError):
        reason = f"{operation} failed: not enough memory for the AOI at this resolution."
        action = ("Reduce AOI size or select a coarser resolution appropriate "
                  "to the source data and study method.")
    elif "network" in lowered or "timed out" in lowered or "connection" in lowered:
        reason = f"{operation} failed while accessing a data source. {msg}"
        action = "Check internet connection and retry; cached datasets are reused."
    return {
        "operation": operation,
        "reason": reason or f"{operation} failed with unexpected error: {msg}",
        "recommended_action": action,
        "error_type": type(exc).__name__,
    }
