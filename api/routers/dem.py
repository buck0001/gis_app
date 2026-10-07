"""DEM upload endpoints (DEM UPLOAD SYSTEM spec).

POST upload -> inspect + validate (no processing starts before validation).
POST confirm -> {continue | fill_nodata | assign_crs | cancel | upload_another}.
GET footprint / coverage -> map display + AOI/DEM intersection check.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from api import db
from gis import dem_source
router = APIRouter(prefix="/api/projects/{project_id}/dem", tags=["dem"])
ALLOWED = {".tif", ".tiff"}
class ConfirmIn(BaseModel):
    action: str
    epsg: Optional[str] = None
    resolution_m: Optional[float] = None
    nodata_action: str = "continue"
def _proj_or_404(project_id: str) -> dict:
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    return proj
def _src_dir(project_id: str) -> Path:
    d = BASE_DIR / "data" / "storage" / project_id / "source"
    d.mkdir(parents=True, exist_ok=True)
    return d
@router.post("/upload")
async def upload_dem(project_id: str, file: UploadFile = File(...)):
    _proj_or_404(project_id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Only GeoTIFF (.tif/.tiff) is supported for DEM upload, got '{ext}'.")
    dest = _src_dir(project_id) / "uploaded_dem.tif"
    dest.write_bytes(await file.read())
    try:
        result = dem_source.inspect_dem(str(dest))
    except Exception as exc:
        raise HTTPException(400, f"Could not read DEM: {exc}")
    db.execute("UPDATE projects SET dem_mode='upload', dem_source=?, dem_validation=?, dem_confirm=NULL WHERE id=?", (str(dest), db.jdump(result), project_id))
    db.execute("INSERT INTO datasets (id, project_id, provider, title, source, category, resolution, crs, origin, file_path, metadata, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))", (db.new_id("ds"), project_id, "user-upload", file.filename or "uploaded_dem.tif", "User upload", "DEM", result["report"].get("resolution_text"), result["report"].get("crs"), "user_upload", str(dest), db.jdump(result["report"])))
    return result
@router.post("/confirm")
def confirm_dem(project_id: str, body: ConfirmIn):
    proj = _proj_or_404(project_id)
    action = body.action.strip().lower()
    if action not in ("continue", "fill_nodata", "assign_crs", "cancel", "upload_another"):
        raise HTTPException(400, "action must be one of: continue | fill_nodata | assign_crs | cancel | upload_another")
    dest = _src_dir(project_id) / "uploaded_dem.tif"
    if action == "cancel":
        db.execute("UPDATE projects SET dem_confirm=? WHERE id=?", (db.jdump({"action": "cancel"}), project_id))
        return {"status": "cancelled", "message": "Upload cancelled. No files were modified."}
    if action == "upload_another":
        db.execute("UPDATE projects SET dem_confirm=? WHERE id=?", (db.jdump({"action": "upload_another"}), project_id))
        return {"status": "awaiting_new_upload", "message": "Please upload another DEM."}
    if action == "assign_crs":
        if not body.epsg:
            raise HTTPException(400, "epsg is required for assign_crs (e.g. 'EPSG:32632'). Explicit assignment only - the CRS is never guessed.")
        try:
            result = dem_source.inspect_dem(str(dest), assign_crs=body.epsg)
        except Exception as exc:
            raise HTTPException(400, str(exc))
        db.execute("UPDATE projects SET dem_validation=?, dem_confirm=? WHERE id=?", (db.jdump(result), db.jdump({"action": "assign_crs", "epsg": body.epsg}), project_id))
        return result
    if action == "fill_nodata":
        db.execute("UPDATE projects SET dem_confirm=? WHERE id=?", (db.jdump({"action": "fill_nodata"}), project_id))
        return {"status": "confirmed", "nodata_action": "fill", "message": "Nodata will be interpolated on the PROCESSED copy only. The original file stays untouched."}
    report = db.jload(proj.get("dem_validation"), {})
    if report.get("issues"):
        errors = [i for i in report["issues"] if i.get("severity") == "error"]
        if errors:
            raise HTTPException(400, f"Cannot continue: {errors[0]['message']}")
    db.execute("UPDATE projects SET dem_confirm=? WHERE id=?", (db.jdump({"action": "continue", "resolution_m": body.resolution_m, "nodata_action": body.nodata_action}), project_id))
    return {"status": "confirmed", "message": "DEM validated. Ready for analysis."}
@router.get("/footprint")
def dem_footprint(project_id: str):
    proj = _proj_or_404(project_id)
    path = proj.get("dem_source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "No uploaded DEM for this project.")
    confirm = db.jload(proj.get("dem_confirm"), {}) or {}
    try:
        return dem_source.dem_footprint_geojson(path, assign_crs=confirm.get("epsg"))
    except Exception as exc:
        raise HTTPException(400, str(exc))
@router.get("/coverage")
def dem_coverage(project_id: str):
    proj = _proj_or_404(project_id)
    path = proj.get("dem_source")
    if not path or not Path(path).exists():
        raise HTTPException(404, "No uploaded DEM for this project.")
    aoi = db.row("SELECT * FROM areas_of_interest WHERE project_id=?", (project_id,))
    if not aoi:
        raise HTTPException(400, "Set the project AOI first, then check coverage.")
    confirm = db.jload(proj.get("dem_confirm"), {}) or {}
    try:
        return dem_source.check_aoi_coverage(path, db.jload(aoi["geometry_geojson"]), assign_crs=confirm.get("epsg"))
    except Exception as exc:
        raise HTTPException(400, str(exc))
