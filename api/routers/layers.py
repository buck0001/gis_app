"""Layer catalog endpoints (spec sections 28, 38, 41)."""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from api import db
router = APIRouter(prefix="/api/projects/{project_id}/layers", tags=["layers"])
CATALOG = [
    {"group": "Terrain", "keys": ["elevation", "contours", "slope", "aspect", "hillshade", "curvature", "roughness", "tri", "tpi"]},
    {"group": "Hydrology", "keys": ["breached_dem", "filled_dem", "flow_direction", "flow_accumulation", "streams_raster", "stream_order_raster", "drainage", "stream_order", "watershed", "drainage_density", "distance_to_drainage", "twi"]},
]
def _public(row: dict) -> dict:
    out = dict(row)
    for k in ("processing", "params", "stats", "style", "preview", "extra"):
        out[k] = db.jload(out.get(k))
    out["bbox"] = db.jload(out.get("bbox"), [])
    out["download_url"] = f"/api/projects/{out['project_id']}/layers/{out['id']}/download"
    return out
@router.get("")
def list_layers(project_id: str, include_hidden: bool = False):
    q = "SELECT * FROM layers WHERE project_id=? ORDER BY created_at"
    layers = db.rows(q, (project_id,))
    if not include_hidden:
        layers = [l for l in layers if not l.get("hidden")]
    return [_public(l) for l in layers]
@router.get("/catalog")
def catalog(project_id: str):
    layers = {l["layer_key"]: l for l in db.rows("SELECT layer_key, id, name, hidden FROM layers WHERE project_id=?", (project_id,))}
    groups = []
    for g in CATALOG:
        items = []
        for key in g["keys"]:
            info = layers.get(key)
            items.append({"key": key, "available": info is not None, "id": info["id"] if info else None, "name": info["name"] if info else key})
        groups.append({"group": g["group"], "layers": items})
    return groups
@router.get("/{layer_id}")
def layer_detail(project_id: str, layer_id: str):
    row = db.row("SELECT * FROM layers WHERE id=? AND project_id=?", (layer_id, project_id))
    if not row:
        raise HTTPException(404, "Layer not found.")
    return _public(row)
@router.get("/{layer_id}/download")
def layer_download(project_id: str, layer_id: str):
    row = db.row("SELECT * FROM layers WHERE id=? AND project_id=?", (layer_id, project_id))
    if not row:
        raise HTTPException(404, "Layer not found.")
    p = Path(row["file_path"])
    if not p.exists():
        raise HTTPException(404, "Layer file is missing on the server.")
    media = "image/tiff" if p.suffix.lower() in (".tif", ".tiff") else "application/geopackage+sqlite3" if p.suffix.lower() == ".gpkg" else "application/octet-stream"
    return FileResponse(str(p), media_type=media, filename=f"{row['layer_key']}{p.suffix}")
@router.get("/{layer_id}/geojson")
def layer_geojson(project_id: str, layer_id: str):
    import geopandas as gpd
    row = db.row("SELECT * FROM layers WHERE id=? AND project_id=?", (layer_id, project_id))
    if not row:
        raise HTTPException(404, "Layer not found.")
    if row["type"] != "vector":
        raise HTTPException(400, "Only vector layers can be served as GeoJSON. Rasters use the preview image.")
    try:
        gdf = gpd.read_file(row["file_path"])
        gdf = gdf.to_crs("EPSG:4326")
        if len(gdf) > 8000:
            gdf = gdf.sample(8000, random_state=42)
        return {"type": "FeatureCollection", "features": __import__("json").loads(gdf.to_json())["features"], "crs_note": f"Reprojected from {row['crs']} to EPSG:4326 for display."}
    except Exception as exc:
        raise HTTPException(500, f"Could not read vector layer: {exc}")
class StyleIn(BaseModel):
    style: dict
@router.put("/{layer_id}/style")
def set_style(project_id: str, layer_id: str, body: StyleIn):
    row = db.row("SELECT * FROM layers WHERE id=? AND project_id=?", (layer_id, project_id))
    if not row:
        raise HTTPException(404, "Layer not found.")
    db.execute("UPDATE layers SET style=? WHERE id=?", (db.jdump(body.style), layer_id))
    db.execute("INSERT OR REPLACE INTO layer_styles (layer_id, style_json, updated_at) VALUES (?,?, datetime('now'))", (layer_id, db.jdump(body.style)))
    return {"status": "style_updated"}
