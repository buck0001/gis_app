"""AOI endpoints: draw/search/upload/coordinates."""
from __future__ import annotations
import json, sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from api import db
from gis import aoi as aoi_mod, uploads
from gis.providers import admin
router = APIRouter(prefix="/api/projects/{project_id}/aoi", tags=["aoi"])
search_router = APIRouter(prefix="/api/aoi", tags=["aoi"])
UPLOAD_EXTS = {".geojson", ".json", ".zip", ".kml", ".kmz", ".gpkg", ".tif", ".tiff"}
class SetAOI(BaseModel):
    geometry: dict
    name: str = "AOI"
    analysis_crs: Optional[str] = None
    src_crs: str = "EPSG:4326"
    input_method: str = "draw"
def _save(project_id: str, prepared: dict, method: str):
    jobs_dir = BASE_DIR / "data" / "storage" / project_id
    jobs_dir.mkdir(parents=True, exist_ok=True)
    db.execute("DELETE FROM areas_of_interest WHERE project_id=?", (project_id,))
    aid = db.new_id("aoi")
    db.execute("INSERT INTO areas_of_interest (id, project_id, name, geometry_geojson, original_crs, analysis_crs, area_km2, perimeter_km, bbox, centroid, fixes, warnings, input_method, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))", (aid, project_id, prepared["name"], json.dumps(prepared["geometry"]), prepared["original_crs"], prepared["analysis_crs"], prepared["area_km2"], prepared["perimeter_km"], json.dumps(prepared["bbox"]), json.dumps(prepared["centroid"]), json.dumps(prepared["fixes"]), json.dumps(prepared["warnings"]), method))
    db.execute("UPDATE projects SET analysis_crs=?, suggested_crs=? WHERE id=?", (prepared["analysis_crs"], prepared["suggested_crs"], project_id))
    row = db.row("SELECT * FROM areas_of_interest WHERE id=?", (aid,))
    out = dict(row)
    out["geometry"] = json.loads(out.pop("geometry_geojson"))
    out["bbox"] = json.loads(out.get("bbox") or "[]")
    out["centroid"] = json.loads(out.get("centroid") or "[]")
    out["fixes"] = json.loads(out.get("fixes") or "[]")
    out["warnings"] = json.loads(out.get("warnings") or "[]")
    return out
@router.post("")
def set_aoi(project_id: str, body: SetAOI):
    if not db.row("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found.")
    try:
        prepared = aoi_mod.prepare_aoi(body.geometry, analysis_crs=body.analysis_crs, name=body.name, src_crs=body.src_crs)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return _save(project_id, prepared, body.input_method)
class BBoxIn(BaseModel):
    minx: float; miny: float; maxx: float; maxy: float
    name: str = "AOI"
    analysis_crs: Optional[str] = None
@router.post("/bbox")
def set_bbox(project_id: str, body: BBoxIn):
    try:
        geom = aoi_mod.bbox_to_polygon(body.minx, body.miny, body.maxx, body.maxy)
        prepared = aoi_mod.prepare_aoi(geom, analysis_crs=body.analysis_crs, name=body.name)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return _save(project_id, prepared, "bbox")
class PointIn(BaseModel):
    lon: float; lat: float; radius_km: float = 5.0
    name: str = "AOI"
    analysis_crs: Optional[str] = None
@router.post("/point")
def set_point(project_id: str, body: PointIn):
    try:
        geom = aoi_mod.point_to_polygon(body.lon, body.lat, body.radius_km)
        prepared = aoi_mod.prepare_aoi(geom, analysis_crs=body.analysis_crs, name=body.name)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return _save(project_id, prepared, "coordinates")
@router.post("/upload")
async def upload_aoi(project_id: str, file: UploadFile = File(...)):
    if not db.row("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in UPLOAD_EXTS:
        raise HTTPException(400, f"Unsupported file type '{ext}'.")
    tmpdir = BASE_DIR / "data" / "storage" / project_id / "source"
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"aoi_upload{ext}"
    tmp.write_bytes(await file.read())
    try:
        geom, notes = uploads.load_uploaded_aoi(str(tmp), file.filename or "")
        prepared = aoi_mod.prepare_aoi(geom, name=Path(file.filename or "upload").stem)
        prepared["warnings"] = list(prepared.get("warnings", [])) + notes
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return _save(project_id, prepared, "upload")
@search_router.get("/search")
def search_places(q: str, limit: int = 6):
    try:
        return admin.search_administrative_area(q, limit=limit)
    except Exception as exc:
        raise HTTPException(502, f"Place search failed: {exc}")
