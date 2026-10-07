"""Exports endpoints: cartographic maps, project ZIP, processing config (spec 29/30/40)."""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from api import db, jobs
router = APIRouter(prefix="/api/projects/{project_id}/exports", tags=["exports"])
class MapIn(BaseModel):
    layer_id: str
    title: Optional[str] = None
    fmt: str = "png"
    flow_direction_arrows: bool = False


def _map_qa_path(file_path: str) -> Path:
    return Path(file_path).with_suffix(".qa.json")


@router.post("/maps")
def make_map(project_id: str, body: MapIn):
    from gis import maps as maps_mod
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    lyr = db.row("SELECT * FROM layers WHERE id=? AND project_id=?", (body.layer_id, project_id))
    if not lyr:
        raise HTTPException(404, "Layer not found.")
    aoi = db.row("SELECT * FROM areas_of_interest WHERE project_id=?", (project_id,))
    if not aoi:
        raise HTTPException(400, "Project AOI is missing.")
    fmt = (body.fmt or "png").lower()
    if fmt not in ("png", "pdf"):
        raise HTTPException(400, "fmt must be png or pdf.")
    if body.flow_direction_arrows and lyr["layer_key"] != "flow_direction":
        raise HTTPException(
            400, "Flow-direction arrows are only available for D8 flow direction.")
    maps_dir = jobs.project_dir(project_id) / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    output_key = lyr["layer_key"] + (
        "_arrows" if body.flow_direction_arrows else "")
    out_path = maps_dir / f"{output_key}.{fmt}"
    extra = db.jload(lyr.get("extra"), {}) or {}
    chain = db.rows(
        "SELECT tool, message FROM processing_steps WHERE job_id=? "
        "AND message NOT LIKE 'START %' ORDER BY seq",
        (lyr.get("job_id"),),
    ) if lyr.get("job_id") else []
    processing_chain = [
        f"{step['tool']}: {step['message']}" for step in chain
        if step.get("message")
    ]
    processing_chain = list(dict.fromkeys(processing_chain))
    if not processing_chain:
        processing_chain = db.jload(lyr.get("processing"), [])
    processing_chain = [lyr.get("source") or "Input data"] + processing_chain
    area_name = (aoi.get("name") or "AOI").removesuffix(" DEM extent")
    title = body.title or f"{lyr['name']}: {area_name}"
    if body.flow_direction_arrows:
        title += " (D8 arrows, every 3 cells)"
    if (proj.get("dem_mode") or "acquire") == "upload":
        original_dem_path = proj.get("dem_source")
    else:
        original = db.row(
            "SELECT file_path FROM layers WHERE project_id=? "
            "AND layer_key='dem_raw'",
            (project_id,),
        )
        original_dem_path = original.get("file_path") if original else None
    meta = {
        "source": lyr.get("source") or "Not available",
        "resolution": lyr.get("resolution"),
        "algorithm": lyr.get("algorithm"),
        "processing": processing_chain,
        "params": db.jload(lyr.get("params"), {}),
        "units": lyr.get("units"),
        "date": lyr.get("created_at"),
        "aoi_name": aoi.get("name"),
        "aoi_area_km2": aoi.get("area_km2"),
        "vertical_datum": extra.get("vertical_datum"),
        "flow_direction_encoding": extra.get("encoding"),
        "flow_direction_verification": extra.get(
            "flow_direction_verification"),
        "original_dem_path": original_dem_path,
        "analysis_crs": aoi.get("analysis_crs"),
        "flow_direction_arrows": body.flow_direction_arrows,
    }
    if lyr["layer_key"] != "hillshade":
        hillshade = db.row(
            "SELECT file_path FROM layers WHERE project_id=? "
            "AND layer_key='hillshade' AND hidden=0",
            (project_id,),
        )
        if hillshade:
            meta["hillshade_path"] = hillshade["file_path"]
    if lyr["layer_key"] in {
        "flow_accumulation", "streams_raster", "stream_order_raster",
        "drainage", "stream_order", "watershed", "drainage_density",
        "distance_to_drainage",
    }:
        stream_layer = db.row(
            "SELECT params FROM layers WHERE project_id=? "
            "AND layer_key='streams_raster'",
            (project_id,),
        )
        stream_params = db.jload(stream_layer.get("params"), {}) \
            if stream_layer else {}
        meta["stream_threshold_cells"] = stream_params.get("threshold_cells")
    if lyr["layer_key"] == "watershed":
        meta["pour_point"] = extra.get("outlet") or \
            db.jload(lyr.get("params"), {}).get("outlet_info")
    try:
        out = maps_mod.generate_map(
            str(out_path),
            title,
            lyr["type"], lyr["file_path"],
            db.jload(aoi["geometry_geojson"]),
            aoi["analysis_crs"], meta,
            layer_key=lyr["layer_key"], fmt=fmt,
            flow_direction_arrows=body.flow_direction_arrows)
    except Exception as exc:
        raise HTTPException(500, f"Map rendering failed: {exc}")
    mid = db.new_id("map")
    db.execute(
        "INSERT INTO maps (id, project_id, layer_id, title, file_path,"
        " format, created_at) VALUES (?,?,?,?,?,?, datetime('now'))",
        (mid, project_id, body.layer_id, body.title or lyr["name"],
         out["path"], fmt))
    return {"id": mid, "title": title,
            "format": fmt,
            "download_url": f"/files/{project_id}/maps/{output_key}.{fmt}",
            "qa_report": out["qa_report"],
            "qa_download_url":
                f"/files/{project_id}/maps/{output_key}.qa.json"}


@router.get("/maps")
def list_maps(project_id: str):
    rows = db.rows("SELECT * FROM maps WHERE project_id=? ORDER BY created_at",
                   (project_id,))
    for row in rows:
        row["download_url"] = (
            f"/files/{project_id}/maps/{Path(row['file_path']).name}")
        qa_path = _map_qa_path(row["file_path"])
        row["qa_download_url"] = (
            f"/files/{project_id}/maps/{qa_path.name}")
        row["qa_report"] = (
            db.jload(qa_path.read_text(encoding="utf-8"))
            if qa_path.is_file() else None
        )
    return rows


@router.post("/package")
def make_package(project_id: str):
    from gis import package as pkg_mod
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    aoi = db.row("SELECT * FROM areas_of_interest WHERE project_id=?",
                 (project_id,))
    if not aoi:
        raise HTTPException(400, "Set the project AOI first.")
    layers = db.rows("SELECT * FROM layers WHERE project_id=?", (project_id,))
    if not layers:
        raise HTTPException(400, "No layers to package yet - run an analysis first.")
    for lyr in layers:
        for k in ("params", "stats"):
            lyr[k] = db.jload(lyr.get(k))
    history = jobs.job_history(
        (db.rows("SELECT id FROM processing_jobs WHERE project_id=? ORDER BY"
                 " created_at DESC LIMIT 1", (project_id,)) or [{"id": ""}])[0]["id"]
    ) if db.rows("SELECT id FROM processing_jobs WHERE project_id=? LIMIT 1",
                 (project_id,)) else []
    maps = db.rows("SELECT * FROM maps WHERE project_id=?", (project_id,))
    if (proj.get("dem_mode") or "acquire") == "upload":
        original_dem_path = proj.get("dem_source")
    else:
        original = db.row(
            "SELECT file_path FROM layers WHERE project_id=? "
            "AND layer_key='dem_raw'",
            (project_id,),
        )
        original_dem_path = original.get("file_path") if original else None
    pdir = jobs.project_dir(project_id)
    try:
        zip_path = pkg_mod.build_package(
            str(pdir), layers,
            {"name": aoi.get("name"), "area_km2": aoi.get("area_km2"),
             "analysis_crs": aoi.get("analysis_crs"),
             "geometry": db.jload(aoi["geometry_geojson"])},
            history, maps,
            {"name": proj.get("name"), "dem_mode": proj.get("dem_mode"),
             "original_dem_path": original_dem_path})
    except Exception as exc:
        raise HTTPException(500, f"Packaging failed: {exc}")
    eid = db.new_id("exp")
    db.execute(
        "INSERT INTO exports (id, project_id, kind, file_path,"
        " created_at) VALUES (?,?, 'package',?, datetime('now'))",
        (eid, project_id, zip_path))
    return {"id": eid,
            "download_url": f"/files/{project_id}/exports/project_package.zip"}


@router.get("/config")
def export_config(project_id: str):
    proj = db.row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not proj:
        raise HTTPException(404, "Project not found.")
    aoi = db.row("SELECT * FROM areas_of_interest WHERE project_id=?",
                 (project_id,))
    layers = db.rows(
        "SELECT layer_key, source, algorithm, params FROM layers"
        " WHERE project_id=?", (project_id,))
    return {
        "project": proj.get("name"),
        "aoi": ({
            "name": aoi.get("name"), "area_km2": aoi.get("area_km2"),
            "analysis_crs": aoi.get("analysis_crs"),
            "geometry": db.jload(aoi["geometry_geojson"])} if aoi else None),
        "dem_mode": proj.get("dem_mode"),
        "layers": [{"key": l["layer_key"], "source": l.get("source"),
                    "algorithm": l.get("algorithm"),
                    "params": db.jload(l.get("params"))} for l in layers],
    }
