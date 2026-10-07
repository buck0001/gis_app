"""Upload loaders for AOI boundaries.

Supported formats (spec section 3, Option D):
  GeoJSON, Shapefile ZIP, KML/KMZ, GeoPackage, GeoTIFF (extent as boundary).

Every loader returns a GeoJSON geometry in EPSG:4326 plus notes about what
was done to produce it (no silent CRS changes).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from shapely.geometry import box, mapping

from .aoi import AOIError, GEOGRAPHIC_CRS


def _first_polygon_geometry(gdf) -> tuple[dict, str | None]:
    """Merge all features of a GeoDataFrame into one polygonal geometry."""
    from shapely.ops import unary_union

    if gdf.empty:
        raise AOIError("Uploaded file contains no features.")
    if gdf.crs is None:
        raise AOIError(
            "Uploaded vector file has no CRS defined; cannot interpret coordinates safely."
        )
    geom = unary_union(gdf.geometry.values)
    if geom.is_empty:
        raise AOIError("Uploaded file has empty geometry.")
    if geom.geom_type in ("Point", "LineString", "MultiPoint", "MultiLineString"):
        raise AOIError(
            f"Uploaded file contains {geom.geom_type} geometry; a polygon boundary is required."
        )
    note = None
    if gdf.crs.to_epsg() != 4326:
        gdf2 = gdf.to_crs(GEOGRAPHIC_CRS)
        geom = unary_union(gdf2.geometry.values)
        note = f"Reprojected from {gdf.crs.to_string()} to {GEOGRAPHIC_CRS}."
    if not geom.is_valid:
        from shapely.validation import make_valid
        geom = make_valid(geom)
    return mapping(geom), note


def _geojson_dict_to_geometry(data: dict, notes: list[str]) -> tuple[dict, list[str]]:
    """Accept a full GeoJSON document (Feature/FeatureCollection/geometry)."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    gtype = data.get("type")
    if gtype == "FeatureCollection":
        feats = data.get("features", [])
        if not feats:
            raise AOIError("GeoJSON FeatureCollection contains no features.")
        geoms = [shape(f["geometry"]) for f in feats if f.get("geometry")]
        geom = unary_union(geoms)
        notes.append(f"Merged {len(geoms)} GeoJSON features into one boundary.")
    elif gtype == "Feature":
        geom = shape(data["geometry"])
    elif gtype in ("Polygon", "MultiPolygon"):
        geom = shape(data)
    else:
        raise AOIError(f"GeoJSON type '{gtype}' is not a polygon boundary.")

    if geom.geom_type in ("Point", "LineString", "MultiPoint", "MultiLineString"):
        raise AOIError(f"GeoJSON contains {geom.geom_type}; a polygon boundary is required.")
    if not geom.is_valid:
        from shapely.validation import make_valid
        geom = make_valid(geom)
        notes.append("Repaired invalid GeoJSON geometry.")
    return mapping(geom), notes


def load_uploaded_aoi(path: str, original_name: str = "") -> tuple[dict, list[str]]:
    """Load an uploaded file and return (geojson_geometry, notes)."""
    import geopandas as gpd

    p = Path(path)
    if not p.exists():
        raise AOIError(f"Uploaded file not found: {original_name or path}")
    notes: list[str] = []
    suffix = p.suffix.lower()

    # --- Shapefile ZIP -----------------------------------------------------
    if suffix == ".zip":
        with zipfile.ZipFile(p, "r") as zf:
            names = zf.namelist()
            shp_members = [n for n in names if n.lower().endswith(".shp")]
            if shp_members:
                extract_dir = p.parent / (p.stem + "_shp")
                extract_dir.mkdir(parents=True, exist_ok=True)
                zf.extractall(extract_dir)
                gdf = gpd.read_file(str(extract_dir / shp_members[0]))
                geom, note = _first_polygon_geometry(gdf)
                if note:
                    notes.append(note)
                notes.append(f"Extracted shapefile '{shp_members[0]}' from ZIP.")
                return geom, notes
            gpkg_members = [n for n in names if n.lower().endswith(".gpkg")]
            if gpkg_members:
                extract_dir = p.parent / (p.stem + "_zip")
                extract_dir.mkdir(parents=True, exist_ok=True)
                zf.extractall(extract_dir)
                gdf = gpd.read_file(str(extract_dir / gpkg_members[0]))
                geom, note = _first_polygon_geometry(gdf)
                if note:
                    notes.append(note)
                return geom, notes
            geojson_members = [n for n in names
                               if n.lower().endswith((".geojson", ".json"))]
            if geojson_members:
                import json
                data = json.loads(zf.read(geojson_members[0]).decode("utf-8"))
                return _geojson_dict_to_geometry(data, notes)
        raise AOIError(
            "ZIP does not contain a recognisable boundary (.shp / .gpkg / .geojson)."
        )

    # --- GeoJSON -----------------------------------------------------------
    if suffix in (".geojson", ".json"):
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
        return _geojson_dict_to_geometry(data, notes)

    # --- KML / KMZ ---------------------------------------------------------
    if suffix in (".kml", ".kmz"):
        try:
            gdf = gpd.read_file(str(p), driver="KML")
        except Exception as exc:
            raise AOIError(f"Could not read KML/KMZ file: {exc}") from exc
        geom, note = _first_polygon_geometry(gdf)
        if note:
            notes.append(note)
        notes.append("Read with GDAL KML driver.")
        return geom, notes

    # --- GeoPackage --------------------------------------------------------
    if suffix == ".gpkg":
        gdf = gpd.read_file(str(p))
        geom, note = _first_polygon_geometry(gdf)
        if note:
            notes.append(note)
        return geom, notes

    # --- GeoTIFF -> extent boundary ---------------------------------------
    if suffix in (".tif", ".tiff"):
        import rasterio
        with rasterio.open(str(p)) as src:
            b = src.bounds
            raster_crs = src.crs
        if raster_crs is None:
            raise AOIError("GeoTIFF has no CRS; cannot derive a boundary.")
        geom_obj = box(b.left, b.bottom, b.right, b.top)
        if raster_crs.to_epsg() != 4326:
            from pyproj import Transformer
            from shapely.ops import transform as shp_transform
            tr = Transformer.from_crs(raster_crs, GEOGRAPHIC_CRS, always_xy=True)
            geom_obj = shp_transform(tr.transform, geom_obj)
            notes.append(
                f"Raster extent reprojected from {raster_crs.to_string()} to {GEOGRAPHIC_CRS}."
            )
        notes.append("AOI set to the GeoTIFF bounding-box extent (raster is not a boundary file).")
        return mapping(geom_obj), notes

    raise AOIError(
        f"Unsupported file type '{suffix}'. Supported: .geojson .json .zip (shapefile) "
        ".kml .kmz .gpkg .tif"
    )

