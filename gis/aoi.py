"""AOI (Area of Interest) processing.

Responsibilities (per spec section 4):
  1. Validate geometry
  2. Fix invalid geometry if necessary
  3. Calculate area
  4. Calculate bounding box
  5. Detect geographic CRS
  6. Determine appropriate projected CRS for analysis
  7. Store both original CRS and analysis CRS

Distance/area calculations are NEVER performed in EPSG:4326.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from pyproj import CRS, Transformer
from shapely.geometry import Polygon, MultiPolygon, mapping, shape, box, Point
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union
from shapely.validation import explain_validity, make_valid

# --- Cost-control limits (spec section 50) ----------------------------------
WARN_AREA_KM2 = 2500.0        # warn user: potentially expensive
MAX_AREA_KM2 = 25000.0        # hard cap for MVP

GEOGRAPHIC_CRS = "EPSG:4326"


class AOIError(ValueError):
    """Raised when an AOI cannot be prepared for analysis."""


def suggest_projected_crs(lon: float, lat: float) -> str:
    """Suggest an appropriate projected CRS (WGS84 UTM zone) for a location.

    For Nigeria this resolves to UTM zone 31N/32N (e.g. Auchi, Edo State
    at ~7.06E -> EPSG:32631).
    """
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    epsg = (32600 if lat >= 0 else 32700) + zone
    return f"EPSG:{epsg}"


def utm_zone_of(lon: float, lat: float) -> str:
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return f"{zone}{'N' if lat >= 0 else 'S'}"


def _to_4326(geom, src_crs: str = GEOGRAPHIC_CRS):
    if src_crs.upper().replace(" ", "") in (GEOGRAPHIC_CRS, "EPSG:4326", "WGS84", "WGS 84"):
        return geom
    tr = Transformer.from_crs(src_crs, GEOGRAPHIC_CRS, always_xy=True)
    return shapely_transform(tr.transform, geom)


def _polygonal(geom):
    """Keep only polygonal parts of a (possibly repaired) geometry."""
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if polys:
            return unary_union(polys)
    return None


def validate_and_fix(geometry_geojson: dict, src_crs: str = GEOGRAPHIC_CRS) -> tuple[Any, list[str], list[str]]:
    """Validate a GeoJSON geometry, repair it when possible.

    Returns (fixed_geometry_in_4326, fixes_applied, warnings).
    """
    fixes: list[str] = []
    warnings: list[str] = []

    try:
        geom = shape(geometry_geojson)
    except Exception as exc:  # pragma: no cover - defensive
        raise AOIError(f"Geometry could not be parsed: {exc}") from exc

    if geom.is_empty:
        raise AOIError("Geometry is empty.")

    if not geom.is_valid:
        reason = explain_validity(geom)
        try:
            repaired = make_valid(geom)
            if repaired is not None and not repaired.is_empty and repaired.is_valid:
                geom = repaired
                fixes.append(f"Invalid geometry repaired with make_valid (was: {reason}).")
            else:
                geom = geom.buffer(0)
                fixes.append(f"Invalid geometry repaired with buffer(0) (was: {reason}).")
        except Exception as exc:
            raise AOIError(f"Geometry is invalid and could not be repaired: {reason}") from exc

    geom = _polygonal(geom)
    if geom is None or geom.is_empty:
        raise AOIError("Only polygon geometries are supported for an AOI (lines/points rejected).")

    if src_crs.upper().replace(" ", "") not in (GEOGRAPHIC_CRS, "EPSG:4326", "WGS84", "WGS 84"):
        geom = _to_4326(geom, src_crs)
        fixes.append(f"Reprojected AOI from {src_crs} to {GEOGRAPHIC_CRS}.")

    if not geom.is_valid:
        geom = geom.buffer(0)
        fixes.append("Final validity pass applied (buffer(0)).")

    if isinstance(geom, MultiPolygon) and len(geom.geoms) > 1:
        warnings.append(
            f"AOI contains {len(geom.geoms)} separate parts; area is the sum of all parts."
        )

    return geom, fixes, warnings


def bbox_to_polygon(minx: float, miny: float, maxx: float, maxy: float) -> dict:
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise AOIError(
            f"Invalid bounding box [{minx}, {miny}, {maxx}, {maxy}]. "
            "Expected WGS84 coordinates: minLon, minLat, maxLon, maxLat."
        )
    return mapping(box(minx, miny, maxx, maxy))


def point_to_polygon(lon: float, lat: float, radius_km: float) -> dict:
    """Create a circular AOI (local equidistant) around a coordinate."""
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise AOIError(f"Coordinate ({lon}, {lat}) is outside valid WGS84 range.")
    max_radius_km = math.sqrt(MAX_AREA_KM2 / math.pi)
    if radius_km <= 0 or radius_km > max_radius_km:
        raise AOIError(f"Radius must be > 0 and <= {max_radius_km:.0f} km (MVP cost limit).")
    aeqd = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
    )
    tr_b = Transformer.from_crs(aeqd, GEOGRAPHIC_CRS, always_xy=True)
    circle = Point(0, 0).buffer(radius_km * 1000.0, resolution=64)
    circle_ll = shapely_transform(tr_b.transform, circle)
    return mapping(circle_ll)


def prepare_aoi(
    geometry_geojson: dict,
    analysis_crs: Optional[str] = None,
    name: str = "AOI",
    src_crs: str = GEOGRAPHIC_CRS,
) -> dict:
    """Full AOI preparation. Returns a JSON-serializable description containing
    original_crs, analysis_crs, area_km2, bbox, centroid, fixes and warnings.
    """
    geom, fixes, warnings = validate_and_fix(geometry_geojson, src_crs=src_crs)

    centroid = geom.centroid
    suggested = suggest_projected_crs(centroid.x, centroid.y)

    chosen = analysis_crs or suggested
    try:
        crs = CRS.from_user_input(chosen)
    except Exception as exc:
        raise AOIError(f"Analysis CRS '{chosen}' is not a valid CRS.") from exc

    if crs.is_geographic:
        raise AOIError(
            "Analysis CRS must be a projected CRS (area/distance cannot be computed "
            f"in a geographic CRS like {chosen}). Suggested: {suggested}."
        )

    if chosen.upper().replace(" ", "") == GEOGRAPHIC_CRS:
        raise AOIError(f"{GEOGRAPHIC_CRS} cannot be used as analysis CRS for measurements.")

    if chosen.upper().replace(" ", "") != suggested.upper().replace(" ", ""):
        warnings.append(
            f"User override: analysis CRS {chosen} differs from suggested {suggested}."
        )

    # Area must be computed in the projected analysis CRS, never in EPSG:4326.
    tr = Transformer.from_crs(GEOGRAPHIC_CRS, chosen, always_xy=True)
    geom_proj = shapely_transform(tr.transform, geom)
    area_km2 = geom_proj.area / 1_000_000.0
    perimeter_km = geom_proj.length / 1000.0

    if area_km2 <= 0:
        raise AOIError("AOI has zero area.")
    if area_km2 > MAX_AREA_KM2:
        raise AOIError(
            f"AOI area {area_km2:,.1f} km2 exceeds the MVP limit of {MAX_AREA_KM2:,.0f} km2. "
            "Please draw a smaller area or split the analysis."
        )
    if area_km2 > WARN_AREA_KM2:
        warnings.append(
            f"This analysis covers {area_km2:,.0f} km2. Processing at 10 m resolution may be "
            "expensive; recommended resolution for this size is 30 m."
        )

    minx, miny, maxx, maxy = geom.bounds
    if utm_zone_of(minx, miny) != utm_zone_of(maxx, maxy):
        warnings.append(
            "AOI spans two UTM zones; analysis uses the zone of the centroid "
            f"({utm_zone_of(centroid.x, centroid.y)})."
        )

    return {
        "name": name,
        "geometry": mapping(geom),
        "original_crs": GEOGRAPHIC_CRS if src_crs.upper().replace(" ", "") in (
            GEOGRAPHIC_CRS, "EPSG:4326", "WGS84", "WGS 84") else src_crs,
        "analysis_crs": chosen,
        "suggested_crs": suggested,
        "area_km2": round(area_km2, 4),
        "perimeter_km": round(perimeter_km, 4),
        "bbox": [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)],
        "centroid": [round(centroid.x, 6), round(centroid.y, 6)],
        "fixes": fixes,
        "warnings": warnings,
    }

    geom = _polygonal(geom)
    if geom is None or geom.is_empty:
        raise AOIError("Only polygon geometries are supported for an AOI (lines/points rejected).")

    if src_crs.upper().replace(" ", "") not in (GEOGRAPHIC_CRS, "EPSG:4326", "WGS84", "WGS 84"):
        geom = _to_4326(geom, src_crs)
        fixes.append(f"Reprojected AOI from {src_crs} to {GEOGRAPHIC_CRS}.")

    if not geom.is_valid:
        geom = geom.buffer(0)
        fixes.append("Final validity pass applied (buffer(0)).")

    if isinstance(geom, MultiPolygon) and len(geom.geoms) > 1:
        warnings.append(
            f"AOI contains {len(geom.geoms)} separate parts; area is the sum of all parts."
        )

    return geom, fixes, warnings
